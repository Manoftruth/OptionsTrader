#!/usr/bin/env python3
"""
affordability.py — what can this account actually trade?

The question this answers
-------------------------
On 2026-08-18 a 21.4-score AAPL CALL found exactly one eligible contract in a
56-strike chain, and it cleared the price ceiling by eleven cents:

    strike 312.5   ask $1.24   delta 0.15   ✗ delta too low
    strike 310.0   ask $2.44   delta 0.29   ✅ eligible
    strike 307.5   ask $4.10   delta 0.49   ✗ over the $2.55 ceiling

That is not a filter that needs loosening. It is a $255 budget meeting a $311
underlying. The delta band and the price ceiling have almost no overlap, and
which side of the gap a strike lands on changes with a thirty-cent move in the
stock — so the agent looks broken at 10:43 and fine at 10:47.

Widening the delta band to "fix" it would just buy the far-OTM tail on
purpose: delta 0.08 for $59, the exact profile behind the 43% stop-out rate
and the -94% outlier in the 229-trade sample.

So the honest question is not "which filter is wrong" but "which of these
underlyings can this budget trade at all". This measures that directly: for
every ticker on the watchlist, the cheapest contract that clears the delta and
spread filters, and therefore the per-trade budget each name actually requires.

    ./venv/bin/python affordability.py
    ./venv/bin/python affordability.py --budget 400
    ./venv/bin/python affordability.py --dte 7
    ./venv/bin/python affordability.py --tickers F,SOFI,PLTR,RIVN

Read-only. Quotes and chains only — it cannot place an order.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from config import CONFIG
from agent import TradierClient

MIN_DELTA = 0.20
MAX_DELTA = 0.70
MAX_SPREAD = 35.0

# The VIX size multiplier is almost never 1.00. Budgeting as though it were
# means every "affordable" name is affordable only on the calmest days.
TYPICAL_VIX_MULT = 0.85


def cheapest_eligible(client, ticker: str, dte: int, min_px: float,
                      side: str = "call") -> dict | None:
    """Cheapest contract clearing the delta and spread filters, at any price."""
    q = client.get_quote(ticker)
    quote = (q or {}).get("quotes", {}).get("quote", {}) or {}
    spot = float(quote.get("last") or quote.get("close") or 0)
    if spot <= 0:
        return None

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
        return None
    days = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days

    chain = client.get_options_chain(ticker, expiry) or {}
    opts = (chain.get("options") or {}).get("option") or []
    if isinstance(opts, dict):
        opts = [opts]
    opts = [o for o in opts if (o.get("option_type") or "").lower() == side]

    best = None
    for o in opts:
        ask = float(o.get("ask") or 0)
        bid = float(o.get("bid") or 0)
        delta = abs(float((o.get("greeks") or {}).get("delta", 0) or 0))
        if ask <= 0 or ask < min_px:
            continue
        if not (MIN_DELTA <= delta <= MAX_DELTA):
            continue
        mid = (ask + bid) / 2
        if mid > 0 and (ask - bid) / mid * 100 > MAX_SPREAD:
            continue
        if best is None or ask < best["ask"]:
            best = {"strike": float(o.get("strike", 0)), "ask": ask,
                    "delta": delta}

    return {"ticker": ticker, "spot": spot, "expiry": expiry, "days": days,
            "best": best, "n_strikes": len(opts)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=None,
                    help="per-trade budget (default: config max_trade_size)")
    ap.add_argument("--dte", type=int, default=None,
                    help="min days to expiry (default: config)")
    ap.add_argument("--tickers", default=None,
                    help="comma-separated override for the watchlist")
    ap.add_argument("--put", action="store_true")
    a = ap.parse_args()

    side = "put" if a.put else "call"
    budget = a.budget if a.budget is not None else float(CONFIG.get("max_trade_size", 0))
    dte = a.dte if a.dte is not None else int(CONFIG.get("min_days_to_expiry", 0))
    min_px = float(CONFIG.get("min_contract_price", 0.20))
    tickers = ([t.strip().upper() for t in a.tickers.split(",")] if a.tickers
               else list(CONFIG.get("watchlist", [])))

    client = TradierClient(CONFIG["tradier_token"], sandbox=CONFIG["sandbox"])

    print("=" * 78)
    print(f"  AFFORDABILITY — {side}s, {dte}+ DTE, delta "
          f"{MIN_DELTA:.2f}-{MAX_DELTA:.2f}, spread <{MAX_SPREAD:.0f}%")
    print("=" * 78)
    print(f"  per-trade budget ${budget:.0f}  (at the usual "
          f"{TYPICAL_VIX_MULT:.2f}x VIX haircut: ${budget * TYPICAL_VIX_MULT:.0f})")
    print(f"  capital_limit ${CONFIG.get('capital_limit', 0):.0f}")
    print()
    print(f"  {'ticker':<8}{'spot':>9}{'DTE':>5}{'strike':>9}{'ask':>8}"
          f"{'cost':>8}{'delta':>7}  verdict")
    print("  " + "-" * 74)

    rows = []
    for t in tickers:
        try:
            r = cheapest_eligible(client, t, dte, min_px, side)
        except Exception as e:
            print(f"  {t:<8}{'—':>9}   error: {e}")
            continue
        if not r:
            print(f"  {t:<8}{'—':>9}   no quote or no expiry")
            continue
        b = r["best"]
        if not b:
            print(f"  {t:<8}{r['spot']:>9.2f}{r['days']:>5}"
                  f"{'—':>9}{'—':>8}{'—':>8}{'—':>7}  "
                  f"nothing in the delta band at any price")
            rows.append((t, r["spot"], None, None))
            continue
        cost = b["ask"] * 100
        need = cost / TYPICAL_VIX_MULT
        ok = cost <= budget * TYPICAL_VIX_MULT
        verdict = "✅ tradeable" if ok else f"needs ${need:.0f}/trade"
        print(f"  {t:<8}{r['spot']:>9.2f}{r['days']:>5}{b['strike']:>9.1f}"
              f"{b['ask']:>8.2f}{cost:>8.0f}{b['delta']:>7.2f}  {verdict}")
        rows.append((t, r["spot"], cost, need))

    priced = [r for r in rows if r[2]]
    ok = [r for r in priced if r[2] <= budget * TYPICAL_VIX_MULT]

    print("\n" + "=" * 78)
    print(f"  {len(ok)}/{len(tickers)} names tradeable at ${budget:.0f}/trade "
          f"after the {TYPICAL_VIX_MULT:.2f}x haircut.")
    if priced:
        need_all = max(r[3] for r in priced)
        need_half = sorted(r[3] for r in priced)[len(priced) // 2]
        print(f"  Median name needs ${need_half:.0f}/trade. "
              f"The whole list needs ${need_all:.0f}.")
    if len(ok) < len(tickers):
        print()
        print("  What this means, plainly: the cheapest contract that clears the")
        print("  delta band IS the budget requirement. Below it the account is")
        print("  not buying a cheaper version of the same trade — it is buying a")
        print("  different, worse trade (delta 0.05-0.15, which needs a large")
        print("  fast move just to break even, and expires worthless otherwise).")
        print()
        print("  Three honest options, in order of how much I'd trust them:")
        print("    1. Trade cheaper underlyings. A $30-80 stock puts delta-0.40")
        print("       contracts in the $60-200 range, which this account can")
        print("       actually size into. Run:")
        print("         ./venv/bin/python affordability.py --tickers F,SOFI,PLTR,HOOD,RIVN,NIO")
        print("    2. Raise per-trade budget to the number above and accept 1-2")
        print("       concurrent positions instead of 4. Fewer, realer bets.")
        print("    3. Add capital. Your call entirely — I am not advising on")
        print("       position size or account funding, only reporting what the")
        print("       current numbers can and cannot buy.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
