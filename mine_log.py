#!/usr/bin/env python3
"""
mine_log.py — reconstruct a labelled signal dataset from agent.log

Why
---
The score does not predict profit. Across 74 matched trades the correlation
between signal score and dollar P&L was -0.164 (p = 0.162) — no evidence of
predictive value, in the wrong direction. But that says nothing about *which*
components are dead. RSI, MACD, VWAP, volume surge, the squeeze gate, the
unusual-options-flow gate and the 3/3 confluence requirement all fold into one
number before anything is recorded, so their individual contributions are
invisible.

Rewriting the formula without that breakdown would just replace one
unvalidated score with another.

Your agent.log already contains what is needed. Every accepted signal logs its
components:

    ✅ NVDA: score=14.2 dir=CALL squeeze=False uvol=True vix_mult=1.00x
             sl=40.0% atr=2.181 tf={'1h': 5, '15m': 5, '5m': 2}

and every closed position logs its outcome:

    💰 REALIZED P&L: NVDA260812C00225000 | Entry $1.30 → Exit $1.85 | +42.3% | $+55.00

This joins them. Read-only: it opens agent.log and writes a CSV. It never
touches the agent, the broker, or config.

    python3 mine_log.py                    # analyse agent.log
    python3 mine_log.py --log agent.log --csv signals.csv
"""

import argparse
import ast
import csv
import math
import random
import re
from collections import defaultdict

TS   = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
SIG  = re.compile(r"✅\s+([A-Z]{1,6}):\s*score=([\d.]+)\s+dir=(CALL|PUT)")
FLD  = {
    "squeeze":     re.compile(r"squeeze=(True|False)"),
    "uvol":        re.compile(r"uvol=(True|False)"),
    "trend_bonus": re.compile(r"trend_bonus=(-?[\d.]+)"),
    "vix_mult":    re.compile(r"vix_mult=([\d.]+)x"),
    "sl_hint":     re.compile(r"sl=([\d.]+)%"),
    "atr":         re.compile(r"atr=([\d.]+)"),
}
TF   = re.compile(r"tf=(\{[^}]*\})")
EXEC = re.compile(r"EXECUTING:\s+Buy\s+(\d+)x\s+([A-Z0-9]+)")
CAND = re.compile(r"Trade candidate:\s+([A-Z0-9]{10,})")
PNL  = re.compile(
    r"REALIZED P&L:\s+([A-Z0-9]+)\s+\|\s+Entry \$([\d.]+)\s*→\s*Exit \$([\d.]+)"
    r"\s*\|\s*([+-]?[\d.]+)%\s*\|\s*\$([+-]?[\d.]+)")
REG  = re.compile(r"REGIME:\s+(\w+)|Market regime:\s+(\w+)", re.I)
VIX  = re.compile(r"VIX[:\s]+([\d.]+)")


def ticker_of(option_symbol: str) -> str:
    m = re.match(r"^([A-Z]+)", option_symbol)
    return m.group(1) if m else option_symbol[:4]


def parse(path: str) -> list[dict]:
    """Walk the log once, carrying the most recent signal per ticker."""
    pending: dict[str, dict] = {}      # ticker -> last accepted signal
    open_pos: dict[str, dict] = {}     # option_symbol -> signal snapshot
    regime, vix = "unknown", None
    rows: list[dict] = []
    stats = defaultdict(int)

    with open(path, "r", errors="replace") as fh:
        for line in fh:
            ts = TS.match(line)
            when = f"{ts.group(1)} {ts.group(2)}" if ts else ""

            r = REG.search(line)
            if r:
                regime = (r.group(1) or r.group(2) or "unknown").lower()
            v = VIX.search(line)
            if v and "VIX" in line and "term" not in line:
                try:
                    fv = float(v.group(1))
                    if 5 <= fv <= 150:
                        vix = fv
                except ValueError:
                    pass

            s = SIG.search(line)
            if s:
                stats["signals"] += 1
                d = {"ticker": s.group(1), "score": float(s.group(2)),
                     "direction": s.group(3), "regime": regime, "vix": vix,
                     "signal_time": when}
                for name, rx in FLD.items():
                    m = rx.search(line)
                    if m:
                        val = m.group(1)
                        d[name] = (val == "True") if val in ("True", "False") \
                            else float(val)
                t = TF.search(line)
                if t:
                    try:
                        for k, val in ast.literal_eval(t.group(1)).items():
                            d[f"tf_{k}"] = float(val)
                    except Exception:
                        pass
                pending[d["ticker"]] = d
                continue

            e = EXEC.search(line) or CAND.search(line)
            if e:
                sym = e.group(2) if e.re is EXEC else e.group(1)
                sig = pending.get(ticker_of(sym))
                if sig:
                    open_pos[sym] = {**sig, "option_symbol": sym,
                                     "entry_time": when}
                    stats["executions_matched"] += 1
                else:
                    stats["executions_unmatched"] += 1
                continue

            p = PNL.search(line)
            if p:
                sym = p.group(1)
                stats["exits"] += 1
                sig = open_pos.pop(sym, None)
                if not sig:
                    stats["exits_unmatched"] += 1
                    continue
                rows.append({
                    **sig,
                    "exit_time": when,
                    "entry_px": float(p.group(2)),
                    "exit_px":  float(p.group(3)),
                    "pnl_pct":  float(p.group(4)),
                    "pnl_usd":  float(p.group(5)),
                })

    print("  parse summary:")
    for k in ("signals", "executions_matched", "executions_unmatched",
              "exits", "exits_unmatched"):
        print(f"    {k:<22} {stats[k]}")
    return rows


# ── statistics ────────────────────────────────────────────────────────────────

def corr(x, y):
    n = len(x)
    if n < 3:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return num / den if den else 0.0


def perm_p(x, y, obs, n_iter=10000, seed=11):
    rnd = random.Random(seed)
    ys = list(y)
    hits = 0
    for _ in range(n_iter):
        rnd.shuffle(ys)
        if abs(corr(x, ys)) >= abs(obs):
            hits += 1
    return hits / n_iter


def analyse(rows: list[dict]) -> None:
    if len(rows) < 20:
        print(f"\n  Only {len(rows)} joined trades — too few to conclude "
              f"anything. Nothing here will be trustworthy.")
        return

    print(f"\n{'='*66}\n  DATASET: {len(rows)} trades with both a score and an "
          f"outcome\n{'='*66}")
    pnl = [r["pnl_pct"] for r in rows]
    wins = sum(1 for p in pnl if p > 0)
    print(f"  win rate {100*wins/len(pnl):.1f}%   mean {sum(pnl)/len(pnl):+.1f}%"
          f"   median {sorted(pnl)[len(pnl)//2]:+.1f}%")

    # ── continuous features ──
    print(f"\n  CONTINUOUS FEATURES vs P&L%")
    print(f"  {'feature':<16}{'n':>5}{'r':>9}{'p':>8}   verdict")
    print("  " + "-" * 58)
    cont = ["score", "atr", "vix", "vix_mult", "trend_bonus", "sl_hint",
            "tf_1h", "tf_15m", "tf_5m"]
    results = []
    for f in cont:
        pairs = [(r[f], r["pnl_pct"]) for r in rows if isinstance(r.get(f), (int, float))]
        if len(pairs) < 20:
            continue
        xs = [a for a, _ in pairs]
        ys = [b for _, b in pairs]
        if len(set(xs)) < 3:
            continue
        r_ = corr(xs, ys)
        p_ = perm_p(xs, ys, r_)
        verdict = "PREDICTIVE" if p_ < 0.05 else ("weak" if p_ < 0.20 else "noise")
        results.append((abs(r_), f, len(pairs), r_, p_, verdict))
        print(f"  {f:<16}{len(pairs):>5}{r_:>+9.3f}{p_:>8.3f}   {verdict}")

    # ── binary features ──
    print(f"\n  BINARY FEATURES  (mean P&L% when true vs false)")
    print(f"  {'feature':<16}{'n_true':>8}{'true':>9}{'n_false':>9}{'false':>9}"
          f"{'gap':>9}")
    print("  " + "-" * 62)
    for f in ["squeeze", "uvol"]:
        t = [r["pnl_pct"] for r in rows if r.get(f) is True]
        fl = [r["pnl_pct"] for r in rows if r.get(f) is False]
        if len(t) < 5 or len(fl) < 5:
            continue
        mt, mf = sum(t)/len(t), sum(fl)/len(fl)
        print(f"  {f:<16}{len(t):>8}{mt:>+9.1f}{len(fl):>9}{mf:>+9.1f}"
              f"{mt-mf:>+9.1f}")

    # direction and regime
    for key in ("direction", "regime"):
        groups = defaultdict(list)
        for r in rows:
            groups[r.get(key, "?")].append(r["pnl_pct"])
        print(f"\n  BY {key.upper()}")
        for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(v) < 5:
                continue
            w = sum(1 for p in v if p > 0)
            print(f"    {k:<12} n={len(v):>4}  win {100*w/len(v):>3.0f}%  "
                  f"mean {sum(v)/len(v):+.1f}%")

    # ── the headline ──
    print(f"\n{'='*66}\n  RANKED BY INFORMATION CONTENT\n{'='*66}")
    if not results:
        print("  nothing measurable")
        return
    for _, f, n, r_, p_, verdict in sorted(results, reverse=True):
        bar = "█" * min(30, int(abs(r_) * 100))
        print(f"  {f:<14} r={r_:+.3f} p={p_:.3f} {bar} {verdict}")
    strong = [x for x in results if x[4] < 0.05]
    print(f"\n  {len(strong)} of {len(results)} features show a statistically "
          f"significant relationship.")
    if not strong:
        print("  → No component predicts outcome in this sample. Reweighting")
        print("    them cannot help; the inputs themselves need to change.")
    else:
        print("  → Rebuild the score from these, and drop the rest:")
        for x in strong:
            print(f"      {x[1]}  (r={x[3]:+.3f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="agent.log")
    ap.add_argument("--csv", default="signal_dataset.csv")
    args = ap.parse_args()

    print(f"Mining {args.log} ...")
    rows = parse(args.log)
    if not rows:
        print("\n  No signal→outcome pairs found. The log may predate the")
        print("  current log format, or use a different one.")
        return 1

    keys = sorted({k for r in rows for k in r})
    with open(args.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"\n  wrote {len(rows)} rows → {args.csv}")

    analyse(rows)
    print(f"\n  Send me {args.csv} for the rebuild.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
