#!/usr/bin/env python3
"""
preflight.py — live dry run. Reads real data, places ZERO orders.

Run this before arming crash mode, and again on the first genuinely volatile
day. It answers the questions that offline tests cannot:

  • does the async data layer actually reach Yahoo from this machine?
  • how much faster is the concurrent scan than the old serial one?
  • what does the regime detector say about the tape right now?
  • at your budget, is ANY qualifying downside structure reachable
    on the live chains — and on which days?

Nothing here submits an order. The only Tradier calls made are GETs.

    python3 preflight.py
"""
import asyncio
import logging
import sys
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("OptionsAgent")

BANNER = "═" * 66


def section(title):
    print(f"\n{BANNER}\n  {title}\n{BANNER}")


def main() -> int:
    problems, warnings_ = [], []

    # ── 1. Dependencies ────────────────────────────────────────────────────
    section("1. DEPENDENCIES")
    try:
        import aiohttp
        print(f"  ✓ aiohttp {aiohttp.__version__}")
    except ImportError:
        print("  ✗ aiohttp NOT INSTALLED — the async scan will silently fall "
              "back to the old serial path.\n    Fix: pip install aiohttp")
        problems.append("aiohttp missing")
    for mod in ("pandas", "numpy", "yfinance", "requests", "pytz"):
        try:
            __import__(mod)
            print(f"  ✓ {mod}")
        except ImportError:
            print(f"  ✗ {mod} missing")
            problems.append(f"{mod} missing")

    from config import CONFIG
    from async_data import AsyncMarketData, AIOHTTP_AVAILABLE
    import crash_mode
    import spreads as spreads_mod
    from signal_engine import SignalEngine

    # ── 2. Config sanity ───────────────────────────────────────────────────
    section("2. CONFIG")
    armed = CONFIG.get("crash_mode_enabled", False)
    live = not CONFIG.get("sandbox", True)
    print(f"  sandbox            : {CONFIG.get('sandbox')}"
          f"{'   ← LIVE MONEY' if live else ''}")
    print(f"  crash_mode_enabled : {armed}"
          f"{'' if armed else '   ← detection only; agent still goes flat in high vol'}")
    print(f"  use_async_scan     : {CONFIG.get('use_async_scan', True)}")
    print(f"  capital_limit      : ${CONFIG.get('capital_limit', 0):.2f}")
    print(f"  max_trade_size     : ${CONFIG.get('max_trade_size', 0):.2f}")
    print(f"  min_signal_score   : {CONFIG.get('min_signal_score')}")

    if "YOUR_TRADIER" in str(CONFIG.get("tradier_token", "")):
        print("  ⚠ tradier_token is still the placeholder")
        warnings_.append("Tradier token not set")

    print(f"  crash_structure    : {CONFIG.get('crash_structure', 'deep_itm')}")
    print(f"  crash_underlyings  : {CONFIG.get('crash_underlyings', ['SQQQ'])}")

    # A crash budget that cannot reach any contract is worse than useless —
    # the agent looks like it is running while rejecting everything. Section
    # 4b tests this against live chains; this is just the headline number.
    crash_budget = float(CONFIG.get("crash_max_trade_size",
                                    CONFIG.get("max_trade_size", 0)))
    print(f"  → crash budget     : ${crash_budget:.2f} per trade "
          f"(final; not further scaled by the VIX/regime multipliers)")
    if crash_budget < 150:
        print(f"    ⚠ deep ITM contracts on inverse ETFs typically run "
              f"$150-400. See section 4b for what this actually reaches.")
        warnings_.append("crash budget may be too small for a deep ITM contract")

    # ── 3. Data layer + regime ─────────────────────────────────────────────
    section("3. LIVE DATA + REGIME")
    if not AIOHTTP_AVAILABLE:
        print("  ⚠ skipping (aiohttp missing)")
        rep = None
    else:
        md_ref = [None]

        async def probe():
            td = None
            if CONFIG.get("use_tradier_data", True):
                try:
                    from tradier_data import TradierData
                    td = TradierData(CONFIG["tradier_token"],
                                     sandbox=CONFIG.get("sandbox", True))
                    await td.open()
                except Exception as e:
                    print(f"  ⚠ Tradier data layer unavailable: {e}")
                    td = None
            md = AsyncMarketData(
                max_concurrency=int(CONFIG.get("scan_concurrency", 8)),
                tradier=td)
            md_ref[0] = md
            await md.open()
            await md._ensure_crumb()
            try:
                t0 = time.time()
                spy = await md.get_bars("SPY", "1d", "1mo")
                dt = time.time() - t0
                if spy.empty:
                    print(f"  ✗ SPY daily bars came back EMPTY ({dt:.2f}s) — "
                          f"Yahoo unreachable or the endpoint changed.")
                    return None, None
                print(f"  ✓ SPY daily bars: {len(spy)} rows in {dt:.2f}s "
                      f"(last close {float(spy['Close'].iloc[-1]):.2f})")

                t0 = time.time()
                rep = await crash_mode.assess(md, CONFIG.get("crash_cfg"))
                print(f"  ✓ regime assessed in {time.time() - t0:.2f}s")

                engine = SignalEngine()
                td_note = "Tradier (primary)" if CONFIG.get("use_tradier_data", True) else "Yahoo only"
                print(f"  data source policy: {td_note}")
                ov = crash_mode.overrides_for(rep, CONFIG)
                ov["crash_mode"] = rep.regime == "crash"
                t0 = time.time()
                sigs = await engine.get_top_signals_async(
                    md, CONFIG["min_signal_score"], rep.regime, rep.vix, ov)
                scan_dt = time.time() - t0
                return rep, (ov, sigs, scan_dt)
            finally:
                await md.close()
                if td is not None:
                    await td.close()

        rep, extra = asyncio.run(probe())
        if rep is None:
            problems.append("Yahoo data unreachable")
        else:
            crash_mode.log_report(rep)
            ov, sigs, scan_dt = extra
            print(f"\n  Rules this cycle: {ov.get('reason')}")
            print(f"    calls={ov.get('allow_calls')} puts={ov.get('allow_puts')} "
                  f"size={ov.get('size_mult'):.2f}x spreads={ov.get('prefer_spreads')} "
                  f"maxVIX={ov.get('max_vix')} maxATR={ov.get('max_atr_pct', 5.0)}%")
            by_source: dict = {}
            for v in getattr(md_ref[0], "source_log", {}).values():
                by_source[v] = by_source.get(v, 0) + 1
            if by_source:
                print(f"  📡 Served by: {by_source}")
                if by_source.get("yahoo", 0) > by_source.get("tradier", 0):
                    print("     ⚠ Mostly Yahoo — Tradier calls are failing. "
                          "Check the token, and that sandbox matches it.")
            print(f"\n  ⚡ Full watchlist scan: {scan_dt:.1f}s")
            print(f"     The serial v4 path made ~5 blocking calls per ticker; "
                  f"budget 0.3-0.5s each.")
            print(f"  Signals passing every gate: {len(sigs)}")
            for s in sigs[:5]:
                print(f"    • {s['ticker']} {s['direction']} score={s['score']} "
                      f"atr={s.get('atr')} vixmult={s['vix_size_mult']:.2f}x")

    # ── 4. Tradier (read-only) ─────────────────────────────────────────────
    section("4. TRADIER — READ ONLY, NO ORDERS")
    try:
        from agent import TradierClient
        client = TradierClient(CONFIG["tradier_token"],
                               sandbox=CONFIG.get("sandbox", True))
        bal = client.get_account_balances()
        balances = bal.get("balances", {}) if isinstance(bal, dict) else {}
        print(f"  ✓ credentials accepted "
              f"({'sandbox' if CONFIG.get('sandbox', True) else 'LIVE'})")
        if balances:
            print(f"    total_equity: {balances.get('total_equity')}  "
                  f"option_bp: {balances.get('option_buying_power')}")

        pos = client.get_positions()
        raw = pos.get("positions") if isinstance(pos, dict) else None
        n = 0
        if isinstance(raw, dict):
            p = raw.get("position", [])
            n = len(p) if isinstance(p, list) else 1
        print(f"  ✓ open positions: {n}")

        exps = client.get_options_expirations("SPY")
        dates = exps.get("expirations", {}).get("date", [])
        if not dates:
            print("  ✗ no SPY expirations returned")
            problems.append("Tradier chain data unavailable")
        else:
            exp = sorted(dates)[0]
            chain = client.get_options_chain("SPY", exp)
            opts = (chain.get("options") or {}).get("option", [])
            if isinstance(opts, dict):
                opts = [opts]
            print(f"  ✓ SPY chain {exp}: {len(opts)} contracts")

    except Exception as e:
        print(f"  ✗ Tradier error: {e}")
        problems.append(f"Tradier: {e}")
        client = None

    # ── 4b. What the downside structure can actually reach ─────────────────
    section("4b. REACHABLE DOWNSIDE STRUCTURE — live chains, still no orders")
    structure = CONFIG.get("crash_structure", "deep_itm")
    print(f"  crash_structure = {structure}")

    if structure == "deep_itm" and client is not None:
        import deep_itm
        from datetime import datetime as _dt

        budget = float(CONFIG.get("crash_max_trade_size",
                                  CONFIG.get("max_trade_size", 125)))
        print(f"  budget per crash trade: ${budget:.0f}\n")
        print(f"  {'ETF':<7}{'DTE':<6}{'strike':<9}{'ask':<8}{'delta':<8}"
              f"{'extrinsic':<11}{'cost':<8}verdict")
        print("  " + "-" * 66)

        any_reachable = False
        for etf in CONFIG.get("crash_underlyings", ["SQQQ"]):
            try:
                q = client.get_quote(etf)
                spot = float(q.get("quotes", {}).get("quote", {})
                             .get("last", 0) or 0)
                if spot <= 0:
                    print(f"  {etf:<7}no quote")
                    continue
                exp_resp = client.get_options_expirations(etf)
                dates = exp_resp.get("expirations", {}).get("date", []) or []
                if isinstance(dates, str):
                    dates = [dates]
                today = _dt.now().date()
                for exp in sorted(dates)[:4]:
                    dte = (_dt.strptime(exp, "%Y-%m-%d").date() - today).days
                    ch = client.get_options_chain(etf, exp)
                    o = (ch.get("options") or {}).get("option", [])
                    if isinstance(o, dict):
                        o = [o]
                    logging.disable(logging.INFO)
                    pick = deep_itm.build_deep_itm(
                        o, spot, "CALL", budget, cfg=CONFIG.get("deep_itm_cfg"))
                    logging.disable(logging.NOTSET)
                    if pick:
                        any_reachable = True
                        print(f"  {etf:<7}{dte:<6}{pick['strike']:<9.1f}"
                              f"${pick['ask']:<7.2f}{pick['delta']:<8.2f}"
                              f"{pick['extrinsic_pct']:<11.0f}"
                              f"${pick['total_cost']:<7.0f}✓ tradeable")
                    else:
                        print(f"  {etf:<7}{dte:<6}{'—':<9}{'—':<8}{'—':<8}"
                              f"{'—':<11}{'—':<8}✗ nothing qualifies")
            except Exception as e:
                print(f"  {etf:<7}error: {e}")

        print(f"\n  ETF spot reference: this table is the ground truth — the")
        print(f"  numbers in config.py were modelled, these are live chains.")
        if not any_reachable:
            print(f"\n  ⚠ NOTHING is reachable at ${budget:.0f} on any listed ETF.")
            print(f"    Crash mode would arm and then never find a structure.")
            print(f"    Fix: raise crash_max_trade_size, add a cheaper inverse")
            print(f"    ETF to crash_underlyings, or relax max_extrinsic_pct.")
            warnings_.append("no reachable deep-ITM structure at current budget")
        else:
            print(f"\n  Rows marked ✗ are days crash mode would stand down rather")
            print(f"  than buy a near-the-money contract. That is intended —")
            print(f"  but if most rows are ✗, raise the budget.")

    elif structure == "spread":
        print("  ⚠ crash_structure='spread' needs Tradier options LEVEL 3.")
        print("    On level 2 the multileg order is rejected and the agent")
        print("    falls back to a single leg — no naked short, but no vol")
        print("    protection either. Set crash_structure='deep_itm' instead.")
        warnings_.append("spread structure selected — requires level 3")

    # ── 5. Verdict ─────────────────────────────────────────────────────────
    section("5. VERDICT")
    if problems:
        print("  BLOCKERS:")
        for p in problems:
            print(f"    ✗ {p}")
    if warnings_:
        print("  WARNINGS:")
        for w in warnings_:
            print(f"    ⚠ {w}")
    if not problems and not warnings_:
        print("  ✓ Everything checks out.")

    print("\n  Reminders before arming crash mode:")
    print("    1. crash_structure='deep_itm' buys a long CALL on an inverse ETF.")
    print("       That is options level 2 — no spread approval needed.")
    print("    2. Section 4b is the number that matters. If most rows read ✗,")
    print("       crash mode will arm and then stand down most days. Raise")
    print("       crash_max_trade_size or add a cheaper inverse ETF.")
    print("    3. crash_mode_enabled=False ships as the default. Arm it on a")
    print("       calm day, after watching the log classify a regime correctly.")
    print("    4. Inverse ETFs reset daily and bleed if held through chop. The")
    print("       90-minute time exit keeps that small; overnight holds do not.")
    print("    5. NO ORDERS WERE PLACED BY THIS SCRIPT.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
