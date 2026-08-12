#!/usr/bin/env python3
"""
test_order.py — place ONE controlled deep-ITM order, live, with confirmation.

Why this exists
---------------
Crash mode is armed, but the deep-ITM path has never placed a live order. The
preflight 4b table proves a contract is *reachable*; it does not prove the
round trip works — order accepted, filled, position visible, P&L computed,
exit submitted. Those are different things, and the first time you find out
should not be during a selloff with the agent running unattended.

This runs the agent's REAL code — OptionsSelector.select_deep_itm,
deep_itm.build_deep_itm, RiskManager.can_trade, TradierClient.place_order —
on exactly one contract. No trading logic is duplicated or bypassed, so what
you observe here is what crash mode will do.

Safety
------
  * Nothing is sent until you type CONFIRM at the prompt.
  * Dry run by default. --live is required to transmit.
  * Refuses to trade outside market hours.
  * Hard cost ceiling (--max-cost, default $300).
  * One contract by default; --contracts to override.

Usage
-----
    python3 test_order.py                  # dry run, shows what it would buy
    python3 test_order.py --live           # asks for CONFIRM, then buys
    python3 test_order.py --watch          # follow the open position's P&L
    python3 test_order.py --close --live   # sell it back
"""

import argparse
import sys
import time
from datetime import datetime

import pytz

from config import CONFIG
import deep_itm
from agent import TradierClient, OptionsSelector, RiskManager, log

ET = pytz.timezone("America/New_York")
BAR = "═" * 68


def market_open() -> tuple[bool, str]:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False, f"weekend ({now:%A})"
    mins = now.hour * 60 + now.minute
    if mins < 9 * 60 + 30:
        return False, f"pre-market ({now:%H:%M} ET)"
    if mins >= 16 * 60:
        return False, f"after hours ({now:%H:%M} ET)"
    return True, f"{now:%H:%M} ET"


def find_position(client: TradierClient, symbol: str | None = None) -> list[dict]:
    pos = client.get_positions()
    raw = pos.get("positions") if isinstance(pos, dict) else None
    if not isinstance(raw, dict):
        return []
    items = raw.get("position", [])
    if isinstance(items, dict):
        items = [items]
    return [p for p in items if symbol is None or p.get("symbol") == symbol]


def show_pnl(client: TradierClient, positions: list[dict]) -> None:
    if not positions:
        print("  no open positions")
        return
    syms = [p.get("symbol") for p in positions]
    quotes = client.get_quotes(syms)
    for p in positions:
        sym = p.get("symbol")
        qty = int(p.get("quantity", 0))
        basis = float(p.get("cost_basis", 0))
        entry = basis / (qty * 100) if qty else 0
        q = quotes.get(sym, {})
        bid = float(q.get("bid", 0) or 0)
        pnl_pct = ((bid - entry) / entry * 100) if entry else 0
        pnl_usd = (bid - entry) * 100 * qty
        print(f"  {sym}  qty {qty}  entry ${entry:.2f}  bid ${bid:.2f}  "
              f"{pnl_pct:+.1f}%  ${pnl_usd:+.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually transmit the order (still asks for CONFIRM)")
    ap.add_argument("--close", action="store_true", help="sell the open test position")
    ap.add_argument("--watch", action="store_true", help="follow P&L, no orders")
    ap.add_argument("--max-cost", type=float, default=300.0)
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--etf", default=None, help="override crash_underlyings")
    args = ap.parse_args()

    client = TradierClient(CONFIG["tradier_token"],
                           sandbox=CONFIG.get("sandbox", True))

    print(BAR)
    print("  test_order.py — ONE controlled deep-ITM order")
    print(BAR)
    mode = "LIVE" if not CONFIG.get("sandbox", True) else "SANDBOX"
    print(f"  account   : {CONFIG['account_id']}  ({mode})")

    # ── watch ──────────────────────────────────────────────────────────────
    if args.watch:
        print("\n  Watching. Ctrl-C to stop.\n")
        try:
            while True:
                print(f"  [{datetime.now(ET):%H:%M:%S}]")
                show_pnl(client, find_position(client))
                time.sleep(15)
        except KeyboardInterrupt:
            return 0

    # ── close ──────────────────────────────────────────────────────────────
    if args.close:
        positions = find_position(client)
        if not positions:
            print("  Nothing open to close.")
            return 0
        print("\n  Open positions:")
        show_pnl(client, positions)
        if not args.live:
            print("\n  Dry run — add --live to actually sell.")
            return 0
        if input("\n  Type CONFIRM to SELL all of the above: ").strip() != "CONFIRM":
            print("  Aborted.")
            return 1
        for p in positions:
            sym, qty = p.get("symbol"), abs(int(p.get("quantity", 0)))
            ticker = "".join(c for c in sym[:6] if c.isalpha())
            r = client.place_order(symbol=ticker, option_symbol=sym,
                                   side="sell_to_close", quantity=qty,
                                   order_type="market")
            print(f"  SELL {qty}x {sym} → {r}")
        return 0

    # ── preconditions ──────────────────────────────────────────────────────
    ok, when = market_open()
    print(f"  market    : {'OPEN' if ok else 'CLOSED'} ({when})")
    if not ok and args.live:
        print("\n  Refusing to send an order outside market hours. An option "
              "order queued overnight fills at whatever the open brings.")
        return 1

    try:
        bal = client.get_account_balances().get("balances", {})
        equity = float(bal.get("total_equity", 0) or 0)
        print(f"  equity    : ${equity:,.2f}")
    except Exception as e:
        print(f"  ✗ cannot reach Tradier: {e}")
        return 1

    existing = find_position(client)
    if existing:
        print(f"\n  ⚠ {len(existing)} position(s) already open:")
        show_pnl(client, existing)
        print("  This adds another. Use --close first if that is not what you want.")

    # ── build the trade using the agent's own selector ──────────────────────
    print(f"\n{BAR}\n  SELECTING (agent's own deep-ITM path)\n{BAR}")

    if args.etf:
        CONFIG["crash_underlyings"] = [args.etf]
    # --max-cost must actually raise the ceiling, not just cap it. The selector
    # budgets from crash_max_trade_size, so setting only `capital` would leave
    # the real limit at $250 and make "--max-cost 400" silently do nothing.
    CONFIG["crash_max_trade_size"] = float(args.max_cost)
    print(f"  budget     : ${args.max_cost:.2f} per contract")
    print(f"  underlyings: {CONFIG.get('crash_underlyings', ['SQQQ'])}")

    selector = OptionsSelector(client)
    risk = RiskManager(client)

    # A synthetic signal. Only the fields the selector reads are set — the
    # scoring engine is not involved, because what is being tested is the
    # execution path, not signal generation.
    signal = {
        "ticker": "QQQ", "direction": "PUT", "score": 99.0, "price": 0.0,
        "confluence": 4, "vix_size_mult": 1.0, "atr": 0.0,
        "reasons": ["manual test order"],
    }
    overrides = {"size_mult": 1.0, "crash_mode": True,
                 "allow_calls": False, "allow_puts": True}

    trade = selector.select_deep_itm(signal, capital=args.max_cost,
                                     overrides=overrides)
    if not trade:
        print("\n  No qualifying deep-ITM contract right now.")
        print("  Same result crash mode would give — it stands down rather than")
        print("  buying near-the-money. Try --max-cost 400 (which does raise the")
        print("  real budget, not just the cap), or run when the nearest expiry")
        print("  is closer — extrinsic scales with sqrt(time).")
        return 1

    d = trade["deep_itm"]
    if args.contracts != trade["contracts"]:
        trade["contracts"] = args.contracts
        trade["total_cost"] = round(args.contracts * d["ask"] * 100, 2)

    print(f"\n{BAR}\n  ORDER TO PLACE\n{BAR}")
    print(f"  {trade['ticker']} {d['strike']} CALL  exp {trade['expiry']}")
    print(f"  symbol       : {trade['option_symbol']}")
    print(f"  contracts    : {trade['contracts']}  @ ${d['ask']:.2f}")
    print(f"  TOTAL COST   : ${trade['total_cost']:.2f}   ← your maximum loss")
    print(f"  delta        : {d['delta']:.2f}")
    print(f"  intrinsic    : ${d['intrinsic']:.2f}")
    print(f"  extrinsic    : ${d['extrinsic']:.2f}  ({d['extrinsic_pct']:.0f}% "
          f"of premium — this is the part a vol crush destroys)")
    print(f"  breakeven    : {d['breakeven']}")
    print(f"  TP / SL      : +{CONFIG.get('deep_itm_take_profit_pct', 35)}% / "
          f"-{CONFIG.get('deep_itm_stop_loss_pct', 30)}%")
    print(f"  direction    : this is a CALL on an INVERSE ETF — it profits when "
          f"the Nasdaq FALLS")

    if trade["total_cost"] > args.max_cost:
        print(f"\n  ✗ ${trade['total_cost']:.2f} exceeds --max-cost "
              f"${args.max_cost:.2f}. Refusing.")
        return 1

    can, reason = risk.can_trade(trade, equity, "crash", overrides)
    print(f"\n  risk manager : {'PASS' if can else 'BLOCK — ' + reason}")
    if not can and args.live:
        print("  Refusing to bypass the risk manager.")
        return 1

    if not args.live:
        print(f"\n{BAR}")
        print("  DRY RUN — nothing sent. Re-run with --live to place it.")
        print(BAR)
        return 0

    # ── confirm ────────────────────────────────────────────────────────────
    print(f"\n{BAR}")
    print(f"  This spends ${trade['total_cost']:.2f} of REAL money on "
          f"{CONFIG['account_id']}.")
    print(BAR)
    if input("  Type CONFIRM to send: ").strip() != "CONFIRM":
        print("  Aborted. Nothing sent.")
        return 1

    # LIMIT, not market. Tradier rejects market orders on options when it
    # cannot establish a quote — "There is no price. Security symbol: ..."
    pad = 1.0 + float(CONFIG.get("entry_limit_pad_pct", 3.0)) / 100.0
    limit_px = round(d["ask"] * pad, 2)
    print(f"  limit price  : ${limit_px:.2f}  (ask ${d['ask']:.2f} + "
          f"{CONFIG.get('entry_limit_pad_pct', 3.0)}%)")
    result = client.place_order(
        symbol=trade["ticker"], option_symbol=trade["option_symbol"],
        side="buy_to_open", quantity=trade["contracts"],
        order_type="limit", price=limit_px)
    print(f"\n  ✓ submitted: {result}")

    order_id = str((result or {}).get("order", {}).get("id", ""))

    # ── wait for the fill ──────────────────────────────────────────────────
    print("\n  Waiting for fill...")
    for i in range(20):
        time.sleep(3)
        o = client.get_order(order_id)
        status = (o.get("status") or "").lower()
        if status == "filled":
            print(f"\n  ✓ FILLED @ ${o.get('avg_fill_price')} after {(i+1)*3}s")
            show_pnl(client, find_position(client, trade["option_symbol"]))
            break
        if status in ("rejected", "canceled", "expired", "error"):
            print(f"\n  ✗ {status.upper()}: "
                  f"{o.get('reason_description') or 'no reason given'}")
            return 1
        print(f"    ...{(i + 1) * 3}s  ({status or 'pending'})")
    else:
        print("\n  ⚠ Still working. Cancelling so it cannot fill later "
              "unattended.")
        print(f"  {client.cancel_order(order_id)}")
        return 1

    print(f"\n{BAR}")
    print("  Position is open. The running agent WILL see it on its next cycle")
    print("  and manage it — it reads entry price from Tradier's cost basis and")
    print("  applies take-profit and stop-loss. That is the exit half of the")
    print("  test. If you would rather control the exit yourself:")
    print("      sudo systemctl stop optionstrader")
    print()
    print("  Follow it :  python3 test_order.py --watch")
    print("  Close it  :  python3 test_order.py --close --live")
    print(BAR)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted. No order sent.")
        raise SystemExit(1)
