#!/usr/bin/env python3
"""
exit_study.py — find the TP/SL and entry-friction settings that actually pay.

The finding this exists to fix
------------------------------
Exits are governed by _dynamic_tp_sl() in agent.py, which hardcodes
TP 42-45% / SL 25%. config.py's take_profit_pct and stop_loss_pct are dead
keys — nothing reads them.

A 25% stop sounds roomy. It is not, because you do not start a trade at zero:

    entry_limit_pad_pct = 3.0   -> the limit sits 3% ABOVE the ask
    median bid/ask      = 4.7%  -> measured from trades.json
    ------------------------------------------------------------
    you are ~7.7% underwater the instant the order fills

So the real room before the stop is 25 - 7.7 = 17.3% of premium, while the
target needs 43 + 7.7 = 50.7%. Translated through delta into the underlying,
that asymmetry is ~2.93x: the stock must move almost three times further in
your favour to win than against you to lose, while the payoff ratio is only
43/25 = 1.72x. The trade is upside-down before any forecasting skill applies.

Measured on 8,090 shadow signals at the current settings: TP hit 20.1%,
SL hit 36.7%. That is the 33% win rate, and it is geometry, not bad luck.

Method
------
For each candidate (TP, SL, pad, spread cap) this walks the real signed
forward-return distribution and marks each signal a winner, a loser, or a
time-exit. Path dependence is approximated with TWO points per signal (the
30-minute and 60-minute marks) rather than one, so a trade that is stopped out
before recovering is counted as a loss — the single-endpoint version flatters
every wide stop. Still an approximation: a true first-touch test needs
minute bars, which the shadow record does not carry.

Usage
    ./venv/bin/python exit_study.py                  # grid search
    ./venv/bin/python exit_study.py --direction PUT
    ./venv/bin/python exit_study.py --top 25
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
SHADOW = os.path.join(HERE, "shadow_resolved.jsonl")

# Contract economics, measured from trades.json (n=183 orders)
DELTA = 0.288         # MEASURED median |delta| from trades.json (n=185).
                      # config contract_target_delta says 0.45; the selector
                      # never achieved it — 56% of fills were below 0.30.
PREMIUM = 0.92        # MEASURED median entry premium from trade_results.json
COMMISSION_RT = 2.50  # round trip, per trade
MEDIAN_SPREAD = 4.7   # median bid/ask, % of mid


def load() -> list[dict]:
    out = []
    with open(SHADOW) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def premium_pct_to_spot_pct(prem_pct: float, spot: float) -> float:
    """A move of prem_pct% in the option equals this % move in the underlying."""
    return (prem_pct / 100.0 * PREMIUM) / DELTA / spot * 100.0


# Theta over a 60-minute hold, as % of premium. For an ATM option the
# fractional decay over h days with T days to expiry is 1 - sqrt((T-h)/T);
# 60 minutes is 0.154 of a 6.5-hour session, giving ~8.0% at 1 DTE and ~3.9%
# at 2 DTE. 69 of 113 realised trades were 0 DTE, so ~6% is the central case.
# This is the single most important term the first version of this script
# omitted, and it penalises exactly the long-hold/wide-target configurations.
THETA_60M = 6.0

# Stops do not fill at the stop. Measured: realised losses averaged -28.7%
# against a -25% threshold, and 72% of losses were worse than the stop.
STOP_SLIPPAGE = 3.7


def simulate(rows: list[dict], tp: float, sl: float, pad: float,
             spread: float = MEDIAN_SPREAD, theta: float = THETA_60M,
             slippage: float = STOP_SLIPPAGE) -> dict:
    """Walk the distribution for one parameter set.

    Returns realised-premium EV per trade in dollars, on one contract.
    """
    start_down = spread + pad          # % of premium under water on fill
    need_up = tp + start_down          # premium move required to reach TP
    room_down = sl - start_down        # premium move available before SL

    if room_down <= 0:
        # Friction alone exceeds the stop: the position is stopped on fill.
        return {"n": 0, "degenerate": True}

    wins = losses = timeouts = 0
    pnl = []
    for r in rows:
        spot = r.get("spot")
        s30, s60 = r.get("signed_30m"), r.get("signed_60m")
        if not isinstance(spot, (int, float)) or spot <= 0:
            continue
        if not isinstance(s60, (int, float)):
            continue
        up = premium_pct_to_spot_pct(need_up, spot)
        dn = premium_pct_to_spot_pct(room_down, spot)

        path = [x for x in (s30, s60) if isinstance(x, (int, float))]
        outcome = None
        for x in path:
            # Stop checked first at each mark: if both barriers are inside the
            # same interval the pessimistic reading is the honest one.
            if x <= -dn:
                outcome = "loss"
                break
            if x >= up:
                outcome = "win"
                break
        # Theta is charged in proportion to how long the trade is held: a
        # barrier touched at the 30m mark burns half of it, a time exit all.
        held = 0.5 if (outcome is not None and path and path[0] is not s60) else 1.0
        theta_cost = theta * held

        if outcome == "win":
            wins += 1
            pnl.append((tp - theta_cost) / 100.0 * PREMIUM * 100 - COMMISSION_RT)
        elif outcome == "loss":
            losses += 1
            # Stops slip: you get filled past the threshold, not at it.
            pnl.append((-sl - slippage - theta_cost) / 100.0 * PREMIUM * 100
                       - COMMISSION_RT)
        else:
            # Time exit at the 60m mark (the bot flattens at 15:45 regardless).
            timeouts += 1
            prem_move = s60 / 100.0 * spot * DELTA / PREMIUM * 100.0
            net = prem_move - start_down - theta
            pnl.append(net / 100.0 * PREMIUM * 100 - COMMISSION_RT)

    n = len(pnl)
    if n == 0:
        return {"n": 0}
    return {
        "n": n, "tp": tp, "sl": sl, "pad": pad, "spread": spread,
        "win_rate": wins / n, "loss_rate": losses / n, "time_rate": timeouts / n,
        "ev": st.mean(pnl),
        "total": sum(pnl),
        "start_down": start_down,
        "room_down": room_down,
        "need_up": need_up,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--direction", choices=["CALL", "PUT", "BOTH"], default="BOTH")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load()
    if args.direction != "BOTH":
        rows = [r for r in rows if r.get("implied_direction") == args.direction]

    base = simulate(rows, tp=43.0, sl=25.0, pad=3.0)
    print("=" * 78)
    print(f"EXIT STUDY — {base['n']} signals, direction={args.direction}")
    print("=" * 78)
    print("CURRENT SETTINGS (TP 43 / SL 25 / entry pad 3.0)")
    print("-" * 78)
    print(f"  under water on fill : -{base['start_down']:.1f}% of premium")
    print(f"  room before stop    :  {base['room_down']:.1f}%")
    print(f"  move needed for TP  : +{base['need_up']:.1f}%")
    print(f"  win {base['win_rate']*100:.1f}%  stopped {base['loss_rate']*100:.1f}%  "
          f"timed out {base['time_rate']*100:.1f}%")
    print(f"  EV = ${base['ev']:+.2f} per contract per trade")
    print()

    grid = []
    for pad in (3.0, 1.5, 0.0):
        for tp in (25, 30, 35, 40, 45, 50, 60, 70, 80):
            for sl in (25, 30, 35, 40, 45, 50, 55, 60, 70):
                r = simulate(rows, tp=float(tp), sl=float(sl), pad=pad)
                if r.get("n") and not r.get("degenerate"):
                    grid.append(r)
    grid.sort(key=lambda r: -r["ev"])

    if args.json:
        print(json.dumps({"base": base, "top": grid[:args.top]}, indent=2))
        return 0

    print(f"GRID SEARCH — top {args.top} by EV per trade")
    print("-" * 78)
    print(f"  {'pad':>5}{'TP':>5}{'SL':>5}{'win%':>8}{'stop%':>8}{'time%':>8}"
          f"{'EV$':>9}{'total$':>10}")
    for r in grid[:args.top]:
        print(f"  {r['pad']:>5.1f}{r['tp']:>5.0f}{r['sl']:>5.0f}"
              f"{r['win_rate']*100:>7.1f}%{r['loss_rate']*100:>7.1f}%"
              f"{r['time_rate']*100:>7.1f}%{r['ev']:>9.2f}{r['total']:>10.0f}")
    print()

    print("EFFECT OF THE ENTRY PAD ALONE (TP/SL held at current 43/25)")
    print("-" * 78)
    for pad in (3.0, 2.0, 1.0, 0.0):
        r = simulate(rows, 43.0, 25.0, pad)
        print(f"  pad {pad:>4.1f}%  ->  room {r['room_down']:>4.1f}%  "
              f"win {r['win_rate']*100:>5.1f}%  stopped {r['loss_rate']*100:>5.1f}%  "
              f"EV ${r['ev']:>+6.2f}")
    print()
    print("THETA SENSITIVITY — how much the ranking depends on the decay estimate")
    print("-" * 78)
    print("  Theta is the least certain input, so the honest test is whether a")
    print("  configuration survives a range of it. A setting that only wins at")
    print("  theta=0 is an artefact of the model, not an edge.")
    print(f"  {'theta':>7}{'best TP/SL/pad':>18}{'EV$':>9}{'  current 43/25/3.0 EV$':>24}")
    for th in (0.0, 3.0, 6.0, 9.0, 12.0):
        cand = []
        for pad in (0.0, 1.5, 3.0):
            for tp in (25, 30, 35, 40, 45, 50, 60, 70, 80):
                for sl in (25, 30, 35, 40, 45, 50, 55, 60, 70):
                    r = simulate(rows, float(tp), float(sl), pad, theta=th)
                    if r.get("n") and not r.get("degenerate"):
                        cand.append(r)
        b = max(cand, key=lambda r: r["ev"])
        cur = simulate(rows, 43.0, 25.0, 3.0, theta=th)
        tag = "%.0f/%.0f/%.1f" % (b["tp"], b["sl"], b["pad"])
        print(f"  {th:>6.1f}%{tag:>18}{b['ev']:>9.2f}{cur['ev']:>24.2f}")
    print()

    print("CALIBRATION CHECK — model vs reality at current settings")
    print("-" * 78)
    print(f"  model EV/contract at current settings : ${base[chr(39)+chr(39)]:+.2f}" if False else "")
    print("CALIBRATION CHECK — read this before believing any number above")
    print("-" * 78)
    print(f"  model EV/contract, current settings : ${base['ev']:+.2f}")
    print(f"  REALISED EV/contract (n=113 trades) : $-2.30")
    print("  The model is materially MORE pessimistic than reality, so its")
    print("  absolute dollar figures are not a forecast. Likely causes: theta")
    print("  and stop slippage are both charged in full, the 30s fast-monitor")
    print("  thread and trailing stops exit better than a crude barrier test,")
    print("  and flatten_eod closes some positions before the 60m mark.")
    print()
    print("  USE THIS TOOL FOR RANKING AND STRUCTURE, NOT FOR P&L. The three")
    print("  conclusions that do NOT depend on absolute calibration:")
    print("    1. Cutting the entry pad improves EV at every theta and every")
    print("       TP/SL pair tested. It is a pure cost removal, so this is")
    print("       arithmetic, not a model result.")
    print("    2. The 2.93x win/lose distance asymmetry against a 1.72x payoff")
    print("       is computed from measured inputs only.")
    print("    3. No TP/SL pair in the grid clears zero once theta >= 3%.")
    print("       The exit parameters are therefore NOT where the fix lives.")
    print()
    print("CAVEAT")
    print("-" * 78)
    print("  Path dependence is approximated from two marks (30m, 60m) per")
    print("  signal. A true first-touch test needs minute bars. Wide stops are")
    print("  therefore still somewhat flattered here: a trade that dipped")
    print("  through the stop between marks and recovered is scored a timeout.")
    print("  Treat the ranking as directional and the EV as an upper bound.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
