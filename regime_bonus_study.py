#!/usr/bin/env python3
"""
regime_bonus_study.py — is the +2 regime-alignment score bonus worth keeping?

What the bonus does
-------------------
signal_engine.score_core adds CONFIG["regime_align_bonus"] (default 2) to any
signal that agrees with the DAILY regime: bull+CALL, bear+PUT. v5.22 found the
daily regime says nothing about the next hour, and in a bull tape (93.5% of
signals) the bonus lifts CALLs over the trading floor that equal-strength PUTs
miss. CALLs are the side with the negative measured edge.

How this measures it
--------------------
Since v5.23 every scored signal in the shadow log carries the bonus it got
(`regime_bonus`), and "score" already includes it. So the record can be
re-scored as if the bonus were 0: score - regime_bonus.

Setting the bonus to 0 only REMOVES trades — the aligned signals whose score
sits within `bonus` of the floor. Those marginal signals are the whole
question: if their forward move is no better than zero, the bonus is buying
trades with no edge and should go to 0.

Caveat: the agent takes the top-ranked signal each cycle, so removing the bonus
also re-orders who wins a cycle (PUTs rank higher). This study measures the
marginal signals themselves, not that re-ordering.

Usage
-----
    ./venv/bin/python regime_bonus_study.py               # 60-minute horizon
    ./venv/bin/python regime_bonus_study.py --horizon 30m
"""
from __future__ import annotations

import argparse
import sys

from edge_study import SHADOW, load_jsonl, mean_ci95
import scoring

# Below this many marginal signals the answer is noise. Don't act on it.
MIN_MARGINAL = 100


def trade_floor() -> float:
    """Lowest score that actually gets sized: the larger of min_signal_score
    and the score_size_curve's zero band (14.7 as of v5.22)."""
    try:
        from config import CONFIG
    except Exception:
        return scoring.floor_score()
    return max(float(CONFIG.get("min_signal_score", 0)),
               scoring.floor_score(CONFIG.get("score_size_curve")))


def describe(label: str, moves: list[float]) -> None:
    if not moves:
        print(f"  {label:<38} n=0")
        return
    hit = sum(1 for m in moves if m > 0) / len(moves)
    mean, ci = mean_ci95(moves)
    print(f"  {label:<38} n={len(moves):<5} hit {hit:6.1%}   "
          f"mean {mean:+.4f}% ± {ci:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--horizon", default="60m", choices=("30m", "60m"))
    args = ap.parse_args()
    key = f"signed_{args.horizon}"

    rows = [r for r in load_jsonl(SHADOW)
            if r.get("score") is not None and r.get("regime_bonus") is not None
            and r.get(key) is not None]
    floor = trade_floor()
    print(f"Trading floor {floor}  |  horizon {args.horizon}  |  "
          f"{len(rows)} scored signals carrying regime_bonus (v5.23+)\n")
    if not rows:
        print("Nothing recorded yet. regime_bonus is logged from v5.23 on —\n"
              "let the agent run a couple of weeks, then re-run this.")
        return 0

    with_bonus = [r for r in rows if r["score"] >= floor]
    no_bonus = [r for r in rows if r["score"] - r["regime_bonus"] >= floor]
    marginal = [r for r in with_bonus if r["score"] - r["regime_bonus"] < floor]

    print("Signals that clear the floor:")
    describe("bonus as configured", [r[key] for r in with_bonus])
    describe("bonus = 0", [r[key] for r in no_bonus])
    print()
    print("By direction, bonus = 0:")
    for d in ("CALL", "PUT"):
        describe(f"  {d}", [r[key] for r in no_bonus if r["implied_direction"] == d])
    print()
    print("THE QUESTION — signals that trade ONLY because of the bonus:")
    describe("marginal (removed by bonus = 0)", [r[key] for r in marginal])
    print()

    if len(marginal) < MIN_MARGINAL:
        print(f"Verdict: too early. {len(marginal)} marginal signals, need "
              f"{MIN_MARGINAL}+ before the mean means anything.")
        return 0
    mean, ci = mean_ci95([r[key] for r in marginal])
    if mean + ci < 0:
        print("Verdict: the bonus adds signals that are significantly WRONG. "
              "Set regime_align_bonus to 0.")
    elif mean <= 0:
        print("Verdict: the bonus adds signals with no measurable edge. "
              "Set regime_align_bonus to 0 — it only adds trades and costs.")
    elif mean - ci > 0:
        print("Verdict: the marginal signals have a real positive edge. Keep it.")
    else:
        print("Verdict: positive but inside the noise. Keep it and re-check later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
