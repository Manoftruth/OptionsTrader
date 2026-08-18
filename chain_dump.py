#!/usr/bin/env python3
"""
chain_dump.py — show exactly which filter is killing every contract

Why this exists
---------------
On 2026-08-18 a 21.4-score AAPL CALL — top band, squeeze and unusual flow both
true — produced this:

    💵 AAPL: budget $255 caps contract price at $2.55
    ⚠️  No contract found for AAPL — checked 56 CALL options
    ⚠️    Rejected AAPL260819C00205000: ask=$107.80 delta=1.00 ...

Three deep-ITM strikes, all obviously over a $2.55 ceiling, and nothing about
the strikes that actually mattered. The agent's own diagnostic could not
distinguish "the budget is too small" from "the delta band is wrong" from
"Tradier returned no greeks", and those want three different fixes.

This dumps the real chain with every filter evaluated per contract, so the
answer is one command away instead of one restart-and-wait away.

    ./venv/bin/python chain_dump.py AAPL
    ./venv/bin/python chain_dump.py AAPL --budget 255
    ./venv/bin/python chain_dump.py SPY --put

Read-only. Quotes and chains only — it cannot place an order.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from config import CONFIG
from agent import TradierClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--put", action="store_true", help="dump puts instead of calls")
    ap.add_argument("--budget", type=float, default=None,
                    help="sized budget in dollars (default: max_trade_size)")
    ap.add_argument("--dte", type=int, default=None,
                    help="min days to expiry (default: config min_days_to_expiry)")
    ap.add_argument("--all", action="store_true",
                    help="every strike, not just the 25 nearest the money")
    a = ap.parse_args()

    ticker = a.ticker.upper()
    side = "put" if a.put else "call"

    client = TradierClient(CONFIG["tradier_token"], sandbox=CONFIG["sandbox"])

    q = client.get_quote(ticker)
    quote = (q or {}).get("quotes", {}).get("quote", {}) or {}
    spot = float(quote.get("last") or quote.get("close") or 0)
    if spot <= 0:
        print(f"No spot price for {ticker} — is the market open and the token live?")
        return 1

    dte = a.dte if a.dte is not None else int(CONFIG.get("min_days_to_expiry", 0))
    exps = (client.get_options_expirations(ticker) or {}) \
        .get("expirations", {}).get("date", [])
    if isinstance(exps, str):
        exps = [exps]
    today = datetime.now().date()
    expiry = None
    for e in sorted(exps):
        if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= dte:
            expiry = e
            break
    if not expiry:
        print(f"No expiry at least {dte} day(s) out. Available: {exps[:6]}")
        return 1

    days = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days

    # Budget → the ceiling the selector actually applies.
    budget = a.budget if a.budget is not None else float(CONFIG.get("max_trade_size", 0))
    min_px = float(CONFIG.get("min_contract_price", 0.20))
    cfg_max = float(CONFIG.get("max_contract_price", 0))
    affordable = budget / 100.0
    ceiling = min(cfg_max, affordable)

    print("=" * 78)
    print(f"  {ticker} {side.upper()}S  spot ${spot:.2f}  expiry {expiry} ({days} DTE)")
    print("=" * 78)
    print(f"  budget ${budget:.0f} → affords ${affordable:.2f}/contract")
    print(f"  config max_contract_price ${cfg_max:.2f}")
    print(f"  EFFECTIVE CEILING ${ceiling:.2f}   (min ${min_px:.2f})")
    print(f"  delta band 0.20-0.70, spread <35%")
    if affordable < cfg_max:
        print(f"  ⚠️  budget binds before the config cap — the search is "
              f"${cfg_max - affordable:.2f}/contract tighter than configured")

    chain = client.get_options_chain(ticker, expiry) or {}
    opts = (chain.get("options") or {}).get("option") or []
    if isinstance(opts, dict):
        opts = [opts]
    opts = [o for o in opts if (o.get("option_type") or "").lower() == side]
    if not opts:
        print(f"\nEmpty {side} chain for {expiry}.")
        return 1

    strike_offset = 1.02 if side == "call" else 0.98
    target = round(spot * strike_offset / 5) * 5

    rows = []
    null_greeks = 0
    for o in opts:
        strike = float(o.get("strike", 0))
        ask = float(o.get("ask") or 0)
        bid = float(o.get("bid") or 0)
        g = o.get("greeks")
        if g is None:
            null_greeks += 1
        delta = abs(float((g or {}).get("delta", 0) or 0))
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid * 100 if mid > 0 else 999.0

        fails = []
        if ask <= 0:
            fails.append("no-ask")
        elif ask < min_px:
            fails.append("too-cheap")
        elif ask > ceiling:
            fails.append(f"over-ceiling(${ask:.2f}>${ceiling:.2f})")
        if delta > 0.01:
            if delta < 0.20:
                fails.append(f"delta-low({delta:.2f})")
            elif delta > 0.70:
                fails.append(f"delta-high({delta:.2f})")
        elif abs(strike - spot) / spot > 0.05:
            fails.append("no-greeks-and->5%-away")
        if mid > 0 and spread > 35:
            fails.append(f"spread({spread:.0f}%)")

        rows.append((abs(strike - target), strike, bid, ask, delta, spread, fails))

    rows.sort()
    show = rows if a.all else rows[:25]

    print(f"\n  target strike {target:.0f} — {len(show)} of {len(rows)} strikes, "
          f"nearest first\n")
    print(f"  {'strike':>8} {'bid':>7} {'ask':>7} {'cost':>8} {'delta':>6} "
          f"{'sprd':>6}  verdict")
    print("  " + "-" * 74)
    passing = []
    for _, strike, bid, ask, delta, spread, fails in show:
        verdict = "✅ ELIGIBLE" if not fails else "✗ " + ", ".join(fails)
        print(f"  {strike:>8.1f} {bid:>7.2f} {ask:>7.2f} {ask*100:>8.0f} "
              f"{delta:>6.2f} {spread:>5.0f}%  {verdict}")
    passing = [r for r in rows if not r[6]]

    print("\n" + "=" * 78)
    if passing:
        d, strike, bid, ask, delta, spread, _ = passing[0]
        n = int(budget // (ask * 100)) or 1
        print(f"  {len(passing)} eligible. Selector would take strike {strike:.1f} "
              f"@ ${ask:.2f} × {n} = ${n * ask * 100:.0f}")
    else:
        # Which single constraint is doing the damage?
        tally: dict[str, int] = {}
        for r in rows:
            for f in r[6]:
                tally[f.split("(")[0]] = tally.get(f.split("(")[0], 0) + 1
        print("  NOTHING ELIGIBLE. Rejections by cause:")
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<28} {v}")

        # The useful question: does raising the ceiling alone fix it?
        cheapest_ok = None
        for r in sorted(rows, key=lambda r: r[3]):
            others = [f for f in r[6] if not f.startswith("over-ceiling")]
            if not others and r[3] > 0:
                cheapest_ok = r
                break
        if cheapest_ok:
            _, strike, bid, ask, delta, spread, _ = cheapest_ok
            print(f"\n  Cheapest contract that passes EVERY filter except the")
            print(f"  ceiling: strike {strike:.1f} @ ${ask:.2f} (delta {delta:.2f})")
            print(f"  = ${ask*100:.0f}/contract. You need max_trade_size "
                  f"≥ ${ask*100:.0f}")
            print(f"  at 1.0x — and more to survive the 0.85x VIX multiplier: "
                  f"${ask*100/0.85:.0f}.")
        else:
            print(f"\n  No strike passes the delta/spread filters at ANY price.")
            print(f"  The ceiling is not the problem — the delta band or the")
            print(f"  greeks feed is. {null_greeks}/{len(rows)} contracts came")
            print(f"  back with greeks=null.")

    if null_greeks:
        print(f"\n  ⚠️  {null_greeks}/{len(rows)} contracts have greeks=null.")
        print(f"     Those fall back to a ±5%-of-spot strike proximity rule.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
