#!/usr/bin/env python3
"""
shadow_stats.py — put confidence intervals on the shadow report

Why this is separate from shadow.py
-----------------------------------
shadow.py prints hit rates. A hit rate without an interval invites exactly the
mistake this project has already made twice: reading a gradient as a finding.
The CALL/PUT split looked like a real edge until it came out at p=0.246. The
regime_block gate read 23%, then 48%, then 41% across successive samples — the
same gate, three different stories, because n was small and nobody put an
interval on it.

So this does the arithmetic properly:

  * exact two-sided binomial test against 50%, not a normal approximation
  * Wilson intervals, which behave at small n where Wald intervals do not
  * ACCEPTED vs each rejection stage, which is the comparison that matters
  * Benjamini-Hochberg across the whole family, because 5 stages x 3 horizons
    is 15 tests and roughly one will clear p<0.05 by luck alone
  * a DAY-CLUSTERED bootstrap, because signals from one session are not
    independent draws — they share a regime, a drift and a direction bias

That last one is the big one. Treating 1,222 same-week signals as 1,222
independent observations is how a coin flip starts to look like a strategy.

    ./venv/bin/python shadow_stats.py
    ./venv/bin/python shadow_stats.py --horizon 60
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

RESOLVED = Path(__file__).parent / "shadow_resolved.jsonl"

# Pre-registered before looking at this sample. Stated here so it cannot be
# quietly relaxed once the numbers look encouraging.
MIN_DAYS = 10
MIN_ACCEPTED = 50

BOOT = 10_000
SEED = 20260818


# ── inference helpers ────────────────────────────────────────────────────────

def binom_p_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value (sum of outcomes no more likely)."""
    if n == 0:
        return 1.0
    obs = math.comb(n, k) * p ** k * (1 - p) ** (n - k)
    tot = 0.0
    for i in range(n + 1):
        pr = math.comb(n, i) * p ** i * (1 - p) ** (n - i)
        if pr <= obs * (1 + 1e-9):
            tot += pr
    return min(1.0, tot)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — honest at small n, unlike Wald."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def bh_fdr(pvals: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg. Returns a keep/reject flag per input p-value."""
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    keep = [False] * m
    thresh = 0
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= rank / m * alpha:
            thresh = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= thresh:
            keep[idx] = True
    return keep


def icc_anova(by_day: dict[str, list[float]]) -> float:
    """Intra-cluster correlation for a binary outcome, ANOVA estimator.

    How much of the variation in hit rate is BETWEEN days rather than within
    them. Signals from one session share a regime, an intraday drift and a
    direction bias, so this is almost never zero — and every naive interval
    assumes it is exactly zero.
    """
    sizes, rates = [], []
    for d, vals in by_day.items():
        if vals:
            sizes.append(len(vals))
            rates.append(sum(1 for v in vals if v > 0) / len(vals))
    k = len(sizes)
    N = sum(sizes)
    if k < 2 or N <= k:
        return 0.0
    p = sum(n * r for n, r in zip(sizes, rates)) / N
    msb = sum(n * (r - p) ** 2 for n, r in zip(sizes, rates)) / (k - 1)
    msw = sum(n * r * (1 - r) for n, r in zip(sizes, rates)) / (N - k)
    m0 = (N - sum(n * n for n in sizes) / N) / (k - 1)
    if msb + (m0 - 1) * msw <= 0:
        return 0.0
    return max(0.0, min(1.0, (msb - msw) / (msb + (m0 - 1) * msw)))


def design_effect_ci(by_day: dict[str, list[float]]) -> tuple:
    """Wilson interval on the EFFECTIVE sample size after clustering.

    deff = 1 + (avg cluster size - 1) * ICC;  n_eff = n / deff

    This is the survey-statistics standard and is far better behaved than a
    bootstrap over 8 clusters. It can only widen the interval, which is the
    correct direction — correlated observations carry less information than
    independent ones, never more.
    """
    flat = [v for vals in by_day.values() for v in vals]
    n = len(flat)
    if n == 0:
        return (float("nan"),) * 5
    k = sum(1 for vals in by_day.values() if vals)
    icc = icc_anova(by_day)
    m_bar = n / max(1, k)
    deff = max(1.0, 1 + (m_bar - 1) * icc)
    n_eff = max(2.0, n / deff)
    p = sum(1 for v in flat if v > 0) / n
    lo, hi = wilson(round(p * n_eff), round(n_eff))
    return p, lo, hi, n_eff, icc


def cluster_bootstrap_hit(by_day: dict[str, list[float]], reps: int = BOOT) -> tuple:
    """Resample DAYS, not signals. Returns (point, lo, hi) for hit rate."""
    days = list(by_day)
    if len(days) < 2:
        return (float("nan"),) * 3
    rng = random.Random(SEED)
    flat = [v for d in days for v in by_day[d]]
    point = sum(1 for v in flat if v > 0) / len(flat)
    out = []
    for _ in range(reps):
        pick = [rng.choice(days) for _ in days]
        vals = [v for d in pick for v in by_day[d]]
        if vals:
            out.append(sum(1 for v in vals if v > 0) / len(vals))
    out.sort()
    return point, out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def cluster_bootstrap_diff(a_by_day, b_by_day, reps: int = BOOT) -> tuple:
    """Day-clustered CI for (hit rate A - hit rate B), sharing the day draw."""
    days = sorted(set(a_by_day) | set(b_by_day))
    if len(days) < 2:
        return (float("nan"),) * 4
    rng = random.Random(SEED + 1)

    def rate(by_day, picked):
        vals = [v for d in picked for v in by_day.get(d, [])]
        return (sum(1 for v in vals if v > 0) / len(vals)) if vals else None

    point = (rate(a_by_day, days) or 0) - (rate(b_by_day, days) or 0)
    out = []
    for _ in range(reps):
        pick = [rng.choice(days) for _ in days]
        ra, rb = rate(a_by_day, pick), rate(b_by_day, pick)
        if ra is not None and rb is not None:
            out.append(ra - rb)
    if not out:
        return (point, float("nan"), float("nan"), 1.0)
    out.sort()
    lo = out[int(0.025 * len(out))]
    hi = out[int(0.975 * len(out))]
    # Two-sided bootstrap p: how often the resampled difference crosses zero.
    side = sum(1 for v in out if v <= 0) / len(out)
    p = 2 * min(side, 1 - side)
    return point, lo, hi, min(1.0, p)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=None,
                    help="only this horizon in minutes (default: all present)")
    ap.add_argument("--metric", default="excess", choices=["excess", "signed"])
    ap.add_argument("--cohort", default=None,
                    help="analyse this config cohort instead of the largest")
    a = ap.parse_args()

    if not RESOLVED.exists():
        print("No shadow_resolved.jsonl yet.")
        return 1
    rows = []
    for line in open(RESOLVED):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        print("shadow_resolved.jsonl is empty.")
        return 1

    # v5.15: never pool across config cohorts. A settings change that alters
    # which signals can exist creates a different population; averaging them
    # produces a number describing nothing.
    cohorts: dict[str, list] = defaultdict(list)
    for r in rows:
        cohorts[r.get("cohort", "legacy")].append(r)
    if len(cohorts) > 1:
        print("=" * 74)
        print(f"  ⚠️  {len(cohorts)} CONFIG COHORTS in shadow_resolved.jsonl")
        print("=" * 74)
        for c, rs in sorted(cohorts.items(), key=lambda kv: -len(kv[1])):
            ds = sorted({r["ts"][:10] for r in rs if r.get("ts")})
            print(f"    {c:<10} {len(rs):>6} signals  "
                  f"{len(ds)} day(s)  {ds[0] if ds else '?'} → "
                  f"{ds[-1] if ds else '?'}")
        biggest = max(cohorts, key=lambda c: len(cohorts[c]))
        print(f"\n  Analysing the largest cohort only: {biggest} "
              f"({len(cohorts[biggest])} signals).")
        print("  Signals from different cohorts came from different watchlists")
        print("  or thresholds — pooling them measures the config change, not")
        print("  the gates. Re-run per cohort with --cohort <id> if needed.")
        if a.cohort:
            biggest = a.cohort
        rows = cohorts[biggest]

    days = sorted({r["ts"][:10] for r in rows if r.get("ts")})
    per_day = defaultdict(int)
    for r in rows:
        per_day[r["ts"][:10]] += 1

    print("=" * 74)
    print(f"  SHADOW STATISTICS — {len(rows)} resolved signals, "
          f"{len(days)} trading day(s)")
    print("=" * 74)
    print("  signals per day: " + ", ".join(
        f"{d[5:]}={per_day[d]}" for d in days))

    horizons = sorted({int(k.split("_")[1][:-1])
                       for r in rows for k in r
                       if k.startswith(f"{a.metric}_")})
    if a.horizon:
        horizons = [h for h in horizons if h == a.horizon]

    # ── pre-registered adequacy gate ────────────────────────────────────────
    acc_n = {h: sum(1 for r in rows
                    if r.get("accepted") and f"{a.metric}_{h}m" in r)
             for h in horizons}
    best_acc = max(acc_n.values()) if acc_n else 0
    ready = len(days) >= MIN_DAYS and best_acc >= MIN_ACCEPTED

    print(f"\n  Pre-registered adequacy gate: {MIN_DAYS}+ days AND "
          f"{MIN_ACCEPTED}+ ACCEPTED signals")
    print(f"    days      {len(days):>4}  {'✅' if len(days) >= MIN_DAYS else '❌'}")
    print(f"    accepted  {best_acc:>4}  "
          f"{'✅' if best_acc >= MIN_ACCEPTED else '❌'}")
    if not ready:
        print("    → NOT YET READ-WORTHY. Everything below is printed so the")
        print("      trend is visible, but the gate was set before this sample")
        print("      and relaxing it now, because the numbers finally look")
        print("      encouraging, is the whole failure mode.")

    all_p: list[float] = []
    all_label: list[str] = []

    for h in horizons:
        key = f"{a.metric}_{h}m"
        have = [r for r in rows if key in r]
        if len(have) < 20:
            continue

        groups: dict[str, list] = defaultdict(list)
        bydays: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for r in have:
            g = "ACCEPTED" if r.get("accepted") else f"rej:{r.get('stage','?')}"
            groups[g].append(r[key])
            bydays[g][r["ts"][:10]].append(r[key])

        print(f"\n  ── {h} minutes forward ({a.metric} over SPY) ──")
        print(f"  {'group':<20}{'n':>5}{'hit%':>7}"
              f"{'naive 95% CI':>16}{'p':>8}"
              f"{'n_eff':>8}{'clustered 95% CI':>19}{'ICC':>7}")
        print("  " + "-" * 88)

        for g, vals in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            n = len(vals)
            k = sum(1 for v in vals if v > 0)
            if n < 10:
                continue
            lo, hi = wilson(k, n)
            p = binom_p_two_sided(k, n)
            _, clo, chi, neff, icc = design_effect_ci(bydays[g])
            all_p.append(p)
            all_label.append(f"{h}m {g} vs 50%")
            print(f"  {g:<20}{n:>5}{100*k/n:>6.0f}%"
                  f"{f'[{100*lo:.0f}, {100*hi:.0f}]':>16}{p:>8.3f}"
                  f"{neff:>8.0f}"
                  f"{f'[{100*clo:.0f}, {100*chi:.0f}]':>19}{icc:>7.3f}")

        n_clusters = len({d for g in bydays for d in bydays[g] if bydays[g][d]})
        if n_clusters < 20:
            print(f"\n    ⚠️  {n_clusters} trading days is too few clusters for")
            print(f"       cluster-robust inference to be reliable (the usual")
            print(f"       floor is 30-50). n_eff and the clustered CI are the")
            print(f"       best available correction, not a guarantee. Where")
            print(f"       ICC is ~0 the two intervals coincide, which means")
            print(f"       'no measurable day effect at this sample size' —")
            print(f"       NOT 'no day effect'.")

        # The comparison that actually answers "is this gate costing us?"
        if "ACCEPTED" in groups and len(groups["ACCEPTED"]) >= 10:
            print(f"\n    ACCEPTED minus each rejection stage "
                  f"(day-clustered, 95% CI):")
            for g in sorted(groups):
                if g == "ACCEPTED" or len(groups[g]) < 10:
                    continue
                d, dlo, dhi, dp = cluster_bootstrap_diff(
                    bydays["ACCEPTED"], bydays[g])
                all_p.append(dp)
                all_label.append(f"{h}m ACCEPTED vs {g}")
                verdict = ("gate looks valuable" if dlo > 0 else
                           "gate looks costly" if dhi < 0 else
                           "indistinguishable")
                print(f"      vs {g:<22}{100*d:>+6.1f}pp  "
                      f"[{100*dlo:>+5.1f}, {100*dhi:>+5.1f}]  "
                      f"p={dp:<6.3f} {verdict}")

    # ── multiple comparisons ────────────────────────────────────────────────
    if all_p:
        keep = bh_fdr(all_p, alpha=0.05)
        survivors = [(l, p) for l, p, k in zip(all_label, all_p, keep) if k]
        print("\n" + "=" * 74)
        print(f"  MULTIPLE COMPARISONS — {len(all_p)} tests, "
              f"Benjamini-Hochberg at FDR 5%")
        raw = sum(1 for p in all_p if p < 0.05)
        print(f"    {raw} test(s) below p<0.05 raw; "
              f"~{0.05 * len(all_p):.1f} expected by chance alone.")
        if survivors:
            print(f"    {len(survivors)} survive FDR correction:")
            for l, p in sorted(survivors, key=lambda x: x[1]):
                print(f"      {l:<42} p={p:.4f}")
        else:
            print(f"    NONE survive correction. Every apparent effect here is")
            print(f"    consistent with noise across {len(all_p)} simultaneous")
            print(f"    tests.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
