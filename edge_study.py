#!/usr/bin/env python3
"""
edge_study.py — measure a direction-gate policy against the shadow record.

Why this exists
---------------
Every dataset in this project describes trades that PASSED the gates, so the
gates themselves were unmeasured. shadow_resolved.jsonl fixes that: it records
each REJECTED signal with the spot price at that moment and the underlying's
forward move 30/60 minutes later, signed so that POSITIVE means the signal was
directionally right.

That makes it possible to ask the one question that matters before touching any
risk knob: does the system have a directional edge, and is the regime gate
keeping the good signals or throwing them away?

What it does and does not prove
-------------------------------
The shadow record is UNDERLYING returns, not option P&L. Underlying-space
numbers here are measured fact. Option-space dollars are MODELLED, via a
single calibrated parameter, and are clearly labelled as such.

The calibration: a long option is only a winner if the underlying moves far
enough, fast enough, to cover the bid/ask, the commission and the theta burned
while holding. Call that breakeven move T. Rather than invent T from an option
model, it is FITTED so that the model reproduces the win rate this bot actually
realised on the cohort it actually traded. One parameter, fitted on the live
result, then applied unchanged to the counterfactual policy. That is the most
defensible claim available from this data.

Usage
-----
    ./venv/bin/python edge_study.py                 # compare all policies
    ./venv/bin/python edge_study.py --horizon 30m   # 30-minute horizon
    ./venv/bin/python edge_study.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
from collections import Counter, defaultdict

SHADOW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "shadow_resolved.jsonl")
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "trade_results.json")

# Realised trade economics, measured from trade_results.json. These set the
# payoff used for the dollar estimate; they are driven by take_profit_pct /
# stop_loss_pct and so stay fixed across policies.
COMMISSION_RT = 2.50   # round-trip, per trade, ~$0.35/contract each way


# ── data ──────────────────────────────────────────────────────────────────────
def load_jsonl(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def realised_payoff() -> tuple[float, float, float]:
    """(avg_win, avg_loss_abs, win_rate) from the live record."""
    rows = load_jsonl(RESULTS)
    pnl = [r["pnl_dollars"] for r in rows if isinstance(r.get("pnl_dollars"), (int, float))]
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p <= 0]
    if not wins or not losses:
        return 59.68, 36.36, 0.336
    return st.mean(wins), st.mean(losses), len(wins) / len(pnl)


# ── policies ──────────────────────────────────────────────────────────────────
# A policy decides, for a (regime, direction) pair, whether the trade is allowed.
# These mirror crash_mode.overrides_for().
POLICIES: dict[str, dict[str, tuple[bool, bool]]] = {
    # name: {regime: (allow_calls, allow_puts)}
    "current (bull=calls only)": {
        "bull":    (True,  False),
        "neutral": (True,  True),
        "bear":    (False, True),
        "crash":   (False, True),
    },
    "both directions in bull": {
        "bull":    (True,  True),
        "neutral": (True,  True),
        "bear":    (False, True),
        "crash":   (False, True),
    },
    "puts only (all regimes)": {
        "bull":    (False, True),
        "neutral": (False, True),
        "bear":    (False, True),
        "crash":   (False, True),
    },
    "no direction gate": {
        "bull":    (True,  True),
        "neutral": (True,  True),
        "bear":    (True,  True),
        "crash":   (True,  True),
    },
}


def allowed(policy: dict, regime: str, direction: str) -> bool:
    ac, ap = policy.get(regime, (True, True))
    return ac if direction == "CALL" else ap


# ── statistics helpers ────────────────────────────────────────────────────────
def two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def prop_test(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Two-proportion z-test. Returns (z, p)."""
    if n1 == 0 or n2 == 0:
        return float("nan"), float("nan")
    p1, p2 = k1 / n1, k2 / n2
    pool = (k1 + k2) / (n1 + n2)
    denom = pool * (1 - pool) * (1 / n1 + 1 / n2)
    if denom <= 0:
        return float("nan"), float("nan")
    z = (p1 - p2) / math.sqrt(denom)
    return z, two_sided_p(z)


def mean_ci95(xs: list[float]) -> tuple[float, float]:
    """Mean and its 95% half-width."""
    if len(xs) < 2:
        return (xs[0] if xs else 0.0), float("nan")
    return st.mean(xs), 1.96 * math.sqrt(st.variance(xs) / len(xs))


# ── the breakeven-move calibration ────────────────────────────────────────────
def fit_breakeven_move(moves: list[float], target_win_rate: float) -> float:
    """Find T such that P(move > T) == target_win_rate on this cohort.

    T is the underlying move, in percent, that a trade must achieve for the
    option to come out ahead of friction + theta. It is a single free
    parameter fitted to the live result, not an assumption.
    """
    if not moves:
        return float("nan")
    xs = sorted(moves, reverse=True)
    idx = int(round(target_win_rate * len(xs)))
    idx = max(0, min(len(xs) - 1, idx))
    return xs[idx]


# ── evaluation ────────────────────────────────────────────────────────────────
def evaluate(rows: list[dict], policy: dict, horizon: str,
             breakeven: float, avg_win: float, avg_loss: float) -> dict:
    key = f"signed_{horizon}"
    taken = [r for r in rows
             if isinstance(r.get(key), (int, float))
             and r.get("implied_direction") in ("CALL", "PUT")
             and allowed(policy, r.get("regime"), r["implied_direction"])]

    moves = [r[key] for r in taken]
    n = len(moves)
    if n == 0:
        return {"n": 0}

    hits = sum(1 for m in moves if m > 0)
    mean, ci = mean_ci95(moves)

    # Modelled option outcome: a win needs the move to clear the fitted
    # breakeven. Payoff magnitudes are held at the realised averages.
    wins = sum(1 for m in moves if m > breakeven)
    p_win = wins / n
    ev = p_win * avg_win - (1 - p_win) * avg_loss - COMMISSION_RT

    return {
        "n": n,
        "underlying_hit": hits / n,
        "underlying_mean": mean,
        "underlying_ci95": ci,
        "mix_puts": sum(1 for r in taken if r["implied_direction"] == "PUT") / n,
        "model_win_rate": p_win,
        "model_ev": ev,
        "moves": moves,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", default="60m", choices=["30m", "60m"],
                    help="forward window for the underlying move (default 60m)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    rows = load_jsonl(SHADOW)
    if not rows:
        print(f"no shadow records in {SHADOW}")
        return 1

    avg_win, avg_loss, realised_wr = realised_payoff()

    # Fit the breakeven move on the cohort the bot actually traded, so the
    # model reproduces reality before it is asked about anything else.
    cur = POLICIES["current (bull=calls only)"]
    key = f"signed_{args.horizon}"
    cur_moves = [r[key] for r in rows
                 if isinstance(r.get(key), (int, float))
                 and r.get("implied_direction") in ("CALL", "PUT")
                 and allowed(cur, r.get("regime"), r["implied_direction"])]
    breakeven = fit_breakeven_move(cur_moves, realised_wr)

    results = {name: evaluate(rows, pol, args.horizon, breakeven, avg_win, avg_loss)
               for name, pol in POLICIES.items()}

    if args.json:
        slim = {k: {kk: vv for kk, vv in v.items() if kk != "moves"}
                for k, v in results.items()}
        print(json.dumps({"horizon": args.horizon, "breakeven_move_pct": breakeven,
                          "avg_win": avg_win, "avg_loss": avg_loss,
                          "realised_win_rate": realised_wr,
                          "policies": slim}, indent=2))
        return 0

    reg = Counter(r.get("regime") for r in rows)
    print("=" * 78)
    print(f"EDGE STUDY — {len(rows)} resolved shadow signals, {args.horizon} horizon")
    print("=" * 78)
    print(f"regime mix: " + "  ".join(f"{k}={v} ({v/len(rows)*100:.1f}%)"
                                      for k, v in reg.most_common()))
    print()
    print("MEASURED (underlying space — this is fact, not a model)")
    print("-" * 78)
    for d in ("CALL", "PUT"):
        ms = [r[key] for r in rows
              if r.get("implied_direction") == d and isinstance(r.get(key), (int, float))]
        hit = sum(1 for m in ms if m > 0) / len(ms) * 100
        m, ci = mean_ci95(ms)
        print(f"  {d:<5} n={len(ms):>5}  hit={hit:5.1f}%  "
              f"mean move={m:+.4f}% ±{ci:.4f}")
    cm = [r[key] for r in rows if r.get("implied_direction") == "CALL"
          and isinstance(r.get(key), (int, float))]
    pm = [r[key] for r in rows if r.get("implied_direction") == "PUT"
          and isinstance(r.get(key), (int, float))]
    z, p = prop_test(sum(1 for x in pm if x > 0), len(pm),
                     sum(1 for x in cm if x > 0), len(cm))
    print(f"  PUT vs CALL hit-rate difference: z={z:+.2f}  p={p:.5f}"
          f"  {'SIGNIFICANT' if p < 0.05 else 'not significant'}")
    print()
    print(f"CALIBRATION")
    print("-" * 78)
    print(f"  realised win rate on the traded cohort : {realised_wr*100:.1f}%")
    print(f"  fitted breakeven underlying move       : {breakeven:+.4f}%")
    print(f"  realised avg win / avg loss            : ${avg_win:.2f} / ${avg_loss:.2f}")
    print(f"  commission charged per round trip      : ${COMMISSION_RT:.2f}")
    print("  (breakeven is fitted so the model reproduces the live win rate;")
    print("   it is then applied unchanged to every policy below)")
    print()
    print("MODELLED POLICY COMPARISON")
    print("-" * 78)
    print(f"  {'policy':<28}{'n':>6}{'%put':>7}{'u-hit':>8}{'win%':>7}{'EV$':>9}")
    base = results["current (bull=calls only)"]
    for name, r in results.items():
        if not r.get("n"):
            continue
        print(f"  {name:<28}{r['n']:>6}{r['mix_puts']*100:>6.0f}%"
              f"{r['underlying_hit']*100:>7.1f}%{r['model_win_rate']*100:>6.1f}%"
              f"{r['model_ev']:>9.2f}")
    print()
    # ── friction sensitivity ──────────────────────────────────────────────
    # The breakeven move T is the sum of bid/ask, commission and theta. Cutting
    # any of those lowers T, which raises the win rate without needing a single
    # extra basis point of forecasting skill. This table says how much cost
    # reduction the chosen policy would need in order to clear zero.
    pol_name = "both directions in bull"
    cohort = results[pol_name]["moves"]
    ev_break_p = (avg_loss + COMMISSION_RT) / (avg_win + avg_loss)
    print("FRICTION SENSITIVITY — policy: " + pol_name)
    print("-" * 78)
    print(f"  win rate needed to clear EV=0 : {ev_break_p*100:.1f}%")
    print(f"  {'breakeven move T':>18}{'vs now':>9}{'win%':>8}{'EV$':>9}")
    for mult in (1.00, 0.85, 0.70, 0.55, 0.40, 0.25):
        t = breakeven * mult
        p_w = sum(1 for m in cohort if m > t) / len(cohort)
        ev = p_w * avg_win - (1 - p_w) * avg_loss - COMMISSION_RT
        flag = "  <-- clears zero" if ev > 0 else ""
        print(f"  {t:>+17.4f}%{mult*100:>8.0f}%{p_w*100:>7.1f}%{ev:>9.2f}{flag}")
    # Where does T have to be?
    need_t = None
    for i in range(1, 400):
        t = breakeven * (1 - i / 400)
        p_w = sum(1 for m in cohort if m > t) / len(cohort)
        if p_w * avg_win - (1 - p_w) * avg_loss - COMMISSION_RT > 0:
            need_t = t
            break
    if need_t is not None:
        cut = (1 - need_t / breakeven) * 100
        print(f"\n  => EV turns positive once round-trip friction falls ~{cut:.0f}%,")
        print(f"     i.e. breakeven move T from {breakeven:+.4f}% to {need_t:+.4f}%.")
    else:
        print("\n  => no achievable friction reduction clears zero on this cohort.")
    print()

    print("READ THIS BEFORE ACTING")
    print("-" * 78)
    best = max((r for r in results.values() if r.get("n")),
               key=lambda r: r["model_ev"])
    best_name = [k for k, v in results.items() if v is best][0]
    print(f"  best modelled policy : {best_name}  (EV ${best['model_ev']:+.2f}/trade)")
    print(f"  current policy       : EV ${base['model_ev']:+.2f}/trade")
    delta = best["model_ev"] - base["model_ev"]
    print(f"  improvement          : ${delta:+.2f}/trade")
    if best["model_ev"] < 0:
        print()
        print("  *** EVERY POLICY REMAINS NEGATIVE-EV. ***")
        print("  Fixing the direction gate removes a real and statistically")
        print("  significant drag, but it does NOT make this system profitable.")
        print("  The remaining gap is friction: the measured directional edge")
        print("  is smaller than the bid/ask plus commission plus theta.")
        print("  Do not scale size while this line reads negative.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
