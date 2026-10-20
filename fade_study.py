#!/usr/bin/env python3
"""
fade_study.py — is the v5.24 CALL fade (config fade_calls) making money?

Joins realised exits (trade_results.json) to the orders that opened them
(trades.json, which marks faded entries with "faded": true) and compares:

    FADE  — CALL signals traded as PUTs
    PUT   — genuine PUT signals
    CALL  — calls bought (none while fade_calls is on)

Breakeven win rate at the realised win/loss sizes is ~37.9% (CLAUDE.md,
v5.22). Win rate alone misleads here: the fade wins more often but its
losers can be large, so read net $ and avg % too.

Usage
-----
    ./venv/bin/python fade_study.py                    # since the fade went live
    ./venv/bin/python fade_study.py --since 2026-11-01
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
FADE_LIVE = "2026-10-21"
BREAKEVEN_WIN = 0.379
MIN_TRADES = 30      # below this, any verdict is noise


def load_jsonl(name: str) -> list[dict]:
    out = []
    try:
        with open(os.path.join(HERE, name)) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def describe(label: str, rs: list[dict]) -> None:
    if not rs:
        print(f"  {label:<6} n=0")
        return
    wins = sum(1 for r in rs if r["pnl_dollars"] > 0)
    pct = [r["pnl_pct"] for r in rs]
    ci = 1.96 * st.stdev(pct) / math.sqrt(len(pct)) if len(pct) > 1 else float("nan")
    print(f"  {label:<6} n={len(rs):<4} win {wins / len(rs):6.1%}  "
          f"net ${sum(r['pnl_dollars'] for r in rs):9.2f}  "
          f"avg {st.mean(pct):+6.1f}% ±{ci:.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Realised P&L of the CALL fade.")
    ap.add_argument("--since", default=FADE_LIVE, help="YYYY-MM-DD (exit date)")
    args = ap.parse_args()

    entries: dict[str, dict] = {}
    for o in load_jsonl("trades.json"):
        if o.get("option_symbol"):
            entries[o["option_symbol"]] = o          # latest order wins

    groups: dict[str, list] = {"FADE": [], "PUT": [], "CALL": []}
    unmatched = 0
    for r in load_jsonl("trade_results.json"):
        if r.get("exit_time", "")[:10] < args.since:
            continue
        o = entries.get(r.get("option_symbol"))
        if o is None:
            unmatched += 1
            continue
        if o.get("faded"):
            groups["FADE"].append(r)
        else:
            groups["PUT" if o.get("direction") == "PUT" else "CALL"].append(r)

    print(f"Realised exits since {args.since}  "
          f"(breakeven win rate ~{BREAKEVEN_WIN:.1%})\n")
    for k, rs in groups.items():
        describe(k, rs)
    if unmatched:
        print(f"\n  ({unmatched} exits had no matching order in trades.json)")

    fade = groups["FADE"]
    print()
    if len(fade) < MIN_TRADES:
        print(f"Verdict: too early — {len(fade)} fade trades, need {MIN_TRADES}+.")
    elif sum(r["pnl_dollars"] for r in fade) < 0:
        print("Verdict: the fade is LOSING money. Set fade_calls False.")
    else:
        print("Verdict: the fade is net positive so far. Keep it; recheck at 60+.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
