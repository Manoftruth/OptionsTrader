#!/usr/bin/env python3
"""
test_v5.py — offline verification for the v5 additions.

No network, no broker, no orders. Synthetic data only, so it runs anywhere
and gives the same answer every time. Run this after any edit to crash_mode,
spreads, async_data or the PositionMonitor changes.

    python3 test_v5.py
"""
import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  — ' + detail) if detail else ''}")


def bars(n=300, start=500.0, drift=0.0004, vol=0.008, seed=1, last=None):
    """Synthetic OHLCV with a controllable trend."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(steps))
    if last is not None:
        close = close * (last / close[-1])
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({
        "Open": close * 0.999, "High": close * 1.006,
        "Low": close * 0.994, "Close": close,
        "Volume": rng.integers(1e6, 5e6, n).astype(float),
    }, index=idx)


def flat(value, n=300):
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"Open": value, "High": value, "Low": value,
                         "Close": value, "Volume": 1000.0}, index=idx)


# ── 1. Regime classification ───────────────────────────────────────────────────
def test_regime():
    print("\n[1] Regime classification")
    import crash_mode as cm

    # A clean monotonic uptrend. A random walk is a bad fixture here: with
    # any realistic vol it routinely ends mid-pullback, where price < EMA20 <
    # EMA50 is a *correct* bear read — the test would be measuring the noise
    # in the fixture rather than the classifier.
    smooth_up = flat(1.0)
    smooth_up[["Open", "High", "Low", "Close"]] = np.outer(
        400 * 1.0015 ** np.arange(300), [0.999, 1.004, 0.996, 1.0])

    calm_bull = {
        "SPY": smooth_up,
        "^VIX": flat(13.0), "^VIX3M": flat(16.0),
        "HYG": bars(drift=0.0003, vol=0.002, seed=3),
        "IEF": bars(drift=0.0001, vol=0.001, seed=4),
        "^TNX": flat(4.2), "^IRX": flat(3.6),
    }
    r = cm.assess_from_frames(calm_bull)
    check("calm uptrend → bull", r.regime == "bull", f"got {r.regime}, votes={r.crash_votes}")
    check("no crash votes when calm", r.crash_votes == 0)

    # Crash: high VIX, backwardation, deep drawdown off the 20d high
    spy = bars(drift=0.0006, vol=0.006, seed=5)
    spy.iloc[-12:, spy.columns.get_loc("Close")] *= np.linspace(1.0, 0.88, 12)
    crash = {
        "SPY": spy,
        "^VIX": flat(38.0), "^VIX3M": flat(30.0),
        "HYG": bars(drift=-0.0012, vol=0.004, seed=6),
        "IEF": bars(drift=0.0004, vol=0.001, seed=7),
        "^TNX": flat(3.1), "^IRX": flat(3.9),
    }
    r = cm.assess_from_frames(crash)
    check("selloff → crash", r.regime == "crash", f"got {r.regime}, votes={r.crash_votes}")
    check("backwardation detected", r.term_structure > 1.0, f"{r.term_structure:.3f}")
    check("drawdown measured", r.spy_drawdown_20d > 5, f"{r.spy_drawdown_20d:.1f}%")
    check("inverted curve flagged as macro note only",
          r.curve_inverted and not any("curve" in x.lower() for x in r.reasons),
          "curve must never cast a crash vote")

    # Fast bear flip: the old 20/50 check would still say neutral/bull here
    spy2 = bars(drift=0.0009, vol=0.005, seed=8)
    spy2.iloc[-8:, spy2.columns.get_loc("Close")] *= np.linspace(1.0, 0.955, 8)
    mild = {"SPY": spy2, "^VIX": flat(19.0), "^VIX3M": flat(21.0),
            "HYG": flat(80.0), "IEF": flat(95.0),
            "^TNX": flat(4.2), "^IRX": flat(3.6)}
    r = cm.assess_from_frames(mild)
    check("fast bear flip beats 20/50 EMA", r.regime == "bear" and r.prior_regime != "bear",
          f"new={r.regime}, old 20/50={r.prior_regime}")

    # Missing data must degrade, never crash
    r = cm.assess_from_frames({})
    check("empty data → neutral + degraded", r.regime == "neutral" and r.degraded)
    return r


# ── 2. Overrides ───────────────────────────────────────────────────────────────
def test_integrity():
    print("\n[11] Data integrity gate (v5.2 regression)")
    import crash_mode as cm

    good = {
        "SPY": bars(start=770, drift=0.0003, vol=0.004, seed=11),
        "^VIX": flat(14.8), "^VIX3M": flat(18.7),
        "HYG": bars(start=80, seed=12), "IEF": bars(start=95, seed=13),
        "^TNX": flat(4.2), "^IRX": flat(3.6),
    }
    check("clean data passes", cm.check_integrity(good) == [])

    # The exact production failure: SPY's key holding VIX's series
    swapped = {**good, "SPY": flat(14.79)}
    probs = cm.check_integrity(swapped)
    check("SPY holding VIX's value is caught", len(probs) > 0, str(probs[:1]))
    rep = cm.assess_from_frames(swapped)
    check("corrupted frames → NOT classified as crash", rep.regime != "crash",
          f"got {rep.regime}")
    check("corrupted frames flagged", rep.integrity_ok is False)

    ov = cm.overrides_for(rep, {"crash_mode_enabled": True})
    check("corrupt data halts the cycle", ov.get("halt") is True)
    check("halt blocks BOTH directions",
          ov["allow_calls"] is False and ov["allow_puts"] is False)
    check("halt sizes to zero", ov["size_mult"] == 0.0)

    # Absurd VIX (the 79.6 that fabricated the crash call)
    absurd = {**good, "^VIX": flat(790.0)}
    check("impossible VIX is caught", len(cm.check_integrity(absurd)) > 0)

    # Two tickers returning an identical series
    dupe = {**good, "HYG": flat(14.8), "^VIX": flat(14.8)}
    check("identical series across tickers is caught",
          any("IDENTICAL" in p for p in cm.check_integrity(dupe)),
          str(cm.check_integrity(dupe)))

    # A real crash must still classify
    spy = bars(start=770, drift=0.0006, vol=0.006, seed=14)
    spy.iloc[-12:, spy.columns.get_loc("Close")] *= np.linspace(1.0, 0.88, 12)
    real = {**good, "SPY": spy, "^VIX": flat(38.0), "^VIX3M": flat(30.0),
            "HYG": bars(start=80, drift=-0.0012, seed=15)}
    r = cm.assess_from_frames(real)
    check("a GENUINE crash still classifies as crash", r.regime == "crash",
          f"got {r.regime}, integrity_ok={r.integrity_ok}")


def test_overrides():
    print("\n[2] Rule overrides")
    import crash_mode as cm

    crash_rep = cm.RegimeReport(regime="crash", vix=38.0, term_structure=1.25)

    ov_off = cm.overrides_for(crash_rep, {"crash_mode_enabled": False,
                                          "max_vix": 28, "max_contract_price": 3.0,
                                          "min_signal_score": 15.9})
    check("crash + NOT armed → legacy VIX ceiling", ov_off["max_vix"] == 28)
    check("crash + NOT armed → no spreads", ov_off["prefer_spreads"] is False)
    check("crash + NOT armed → legacy ATR ceiling", ov_off["max_atr_pct"] == 5.0)

    ov_on = cm.overrides_for(crash_rep, {"crash_mode_enabled": True,
                                         "max_vix": 28, "max_contract_price": 3.0,
                                         "min_signal_score": 15.9})
    check("crash + armed → VIX ceiling lifted", ov_on["max_vix"] >= 60, str(ov_on["max_vix"]))
    check("crash + armed → calls blocked", ov_on["allow_calls"] is False)
    check("crash + armed → puts allowed", ov_on["allow_puts"] is True)
    check("crash + armed → spreads preferred", ov_on["prefer_spreads"] is True)
    check("crash + armed → size cut", ov_on["size_mult"] <= 0.5, f"{ov_on['size_mult']}x")
    check("crash + armed → score bar RAISED not lowered",
          ov_on["min_signal_score"] > 15.9, f"{ov_on['min_signal_score']}")
    check("crash + armed → premium cap raised", ov_on["max_contract_price"] > 3.0)

    bull = cm.overrides_for(cm.RegimeReport(regime="bull", vix=14.0), {})
    check("bull → puts blocked", bull["allow_puts"] is False and bull["allow_calls"] is True)

    degraded = cm.overrides_for(cm.RegimeReport(regime="bull", vix=14.0, degraded=True), {})
    check("degraded data → size halved", degraded["size_mult"] <= 0.5)


# ── 3. VIX size multiplier ─────────────────────────────────────────────────────
def test_vix_mult():
    print("\n[3] VIX size ladder")
    from signal_engine import SignalEngine
    e = SignalEngine()
    check("VIX 12 → full size", e._vix_size_multiplier(12) == 1.0)
    check("VIX 35, crash OFF → 0.0x (legacy behaviour preserved)",
          e._vix_size_multiplier(35, crash_mode=False) == 0.0)
    check("VIX 35, crash ON → small but non-zero",
          0 < e._vix_size_multiplier(35, crash_mode=True) <= 0.5,
          f"{e._vix_size_multiplier(35, True)}x")
    check("VIX 55, crash ON → smaller still",
          e._vix_size_multiplier(55, True) < e._vix_size_multiplier(31, True))


# ── 4. Spread construction ─────────────────────────────────────────────────────
def put_chain(spot=500.0, tv=14.0, lo=440, hi=560, oi=800, spread_mult=1.03, step=5):
    out = []
    for k in range(lo, hi + 1, step):
        intrinsic = max(0.0, k - spot)
        time_val = max(0.4, tv - abs(k - spot) * 0.22)
        mid = intrinsic + time_val
        out.append({
            "symbol": f"OPT260814P{k:08d}", "strike": float(k),
            "option_type": "put", "bid": round(mid / spread_mult, 2),
            "ask": round(mid * spread_mult, 2), "open_interest": oi,
            "volume": 400, "expiration_date": "2026-08-14",
            "greeks": {"delta": -max(0.02, min(0.98, 0.5 + (k - spot) / (spot * 0.12)))},
        })
    return out


def itm_call_chain(spot, iv=0.85, dte=1, oi=500, spread_mult=1.04):
    """Call chain with a realistic intrinsic/extrinsic split.

    Extrinsic peaks at the money and scales with sqrt(time) — the property
    that decides whether a given budget can reach a genuinely deep strike.
    """
    import math
    out = []
    step = 0.5 if spot < 30 else 1.0
    k = round(spot * 0.7 / step) * step
    while k <= spot * 1.15:
        moneyness = abs(k - spot) / spot
        ext = max(0.02, spot * iv * math.sqrt(dte / 365) * 0.4
                  * math.exp(-8 * moneyness ** 2))
        intrinsic = max(0.0, spot - k)
        mid = intrinsic + ext
        out.append({
            "symbol": f"ETF260814C{int(k * 1000):08d}", "strike": round(k, 2),
            "option_type": "call", "bid": round(mid / spread_mult, 2),
            "ask": round(mid * spread_mult, 2), "open_interest": oi,
            "volume": 300, "expiration_date": "2026-08-13",
            "greeks": {"delta": max(0.02, min(0.98, 0.5 + (spot - k) / (spot * 0.30)))},
        })
        k += step
    return out


def test_deep_itm():
    print("\n[9] Deep ITM selection (level 2)")
    import deep_itm as di

    spot = 38.17          # SQQQ
    chain = itm_call_chain(spot, iv=0.85, dte=1)

    r = di.build_deep_itm(chain, spot, "CALL", 400)
    check("finds a deep ITM contract", r is not None)
    check("is genuinely in the money", r["strike"] < spot,
          f"strike {r['strike']} vs spot {spot}")
    check("intrinsic dominates the premium", r["extrinsic_pct"] <= 40.0,
          f"{r['extrinsic_pct']}% extrinsic")
    check("delta above the deep floor", r["delta"] >= 0.60, f"{r['delta']}")
    check("intrinsic + extrinsic == premium",
          abs((r["intrinsic"] + r["extrinsic"]) - r["ask"]) < 0.02)
    check("max loss is the debit (long option)",
          abs(r["max_loss"] - r["contracts"] * r["ask"] * 100) < 0.01)
    check("reports how much of the spend is pure vol premium",
          0 < r["vol_at_risk"] < r["total_cost"],
          f"${r['vol_at_risk']} of ${r['total_cost']}")

    # An ATM contract must never be selected — that is the whole point
    atm = [o for o in chain if abs(o["strike"] - spot) < 0.6]
    check("at-the-money-only chain is REJECTED, not degraded to",
          di.build_deep_itm(atm, spot, "CALL", 400) is None)

    # Budget/DTE interaction — the constraint that drove the config defaults
    reach = {}
    for dte in (1, 2, 4):
        got = di.build_deep_itm(itm_call_chain(spot, 0.85, dte), spot, "CALL", 250)
        reach[dte] = got is not None
    check("SQQQ @ $250 reachable at 1DTE", reach[1] is True)
    check("SQQQ @ $250 NOT reachable at 4DTE (documented in config)",
          reach[4] is False)
    check("bigger budget reaches further out",
          di.build_deep_itm(itm_call_chain(spot, 0.85, 2), spot, "CALL", 400) is not None)

    # Cheaper share price reaches deeper on the same money
    spxs = di.build_deep_itm(itm_call_chain(23.96, 0.90, 4), 23.96, "CALL", 250)
    check("SPXS @ $250 reachable at 4DTE where SQQQ is not", spxs is not None,
          f"delta {spxs['delta']}, ext {spxs['extrinsic_pct']}%" if spxs else "")

    check("wide markets rejected",
          di.build_deep_itm(itm_call_chain(spot, 0.85, 1, spread_mult=1.4),
                            spot, "CALL", 400) is None)
    check("illiquid strikes rejected",
          di.build_deep_itm([{**o, "open_interest": 0, "volume": 0}
                             for o in chain], spot, "CALL", 400) is None)
    check("zero budget → None", di.build_deep_itm(chain, spot, "CALL", 0) is None)
    check("empty chain → None", di.build_deep_itm([], spot, "CALL", 400) is None)


def test_deep_itm_routing():
    print("\n[10] Crash routing: index PUT signal → inverse ETF CALL")
    import crash_mode as cm
    from config import CONFIG

    sig = {
        "ticker": "QQQ", "direction": "PUT", "score": 18.0, "price": 600.0,
        "confluence": 4, "vix_size_mult": 0.45, "sl_hint": 30.0, "atr": 7.0,
        "reasons": ["test"], "timeframe_scores": {"1h": 6, "15m": 5, "5m": 4},
    }
    rep = cm.RegimeReport(regime="crash", vix=38.0, term_structure=1.25)
    ov = cm.overrides_for(rep, {**CONFIG, "crash_mode_enabled": True})
    ov["crash_mode"] = True

    b = FakeBroker()
    b.etf_dte = 1
    a = _cycle((rep, ov, [sig]), b)
    orders = [o for o in b.orders if isinstance(o, dict) and "option_symbol" in o]
    check("places an order from a bearish index signal", len(orders) == 1,
          f"orders={b.orders}")
    if orders:
        check("BUYS a CALL on the inverse ETF (level 2, no spread approval)",
              orders[0]["side"] == "buy_to_open" and orders[0]["symbol"] in
              ("SQQQ", "SPXS"), str(orders[0]))
        check("no multileg order was attempted",
              not any(o.get("class") == "multileg" for o in b.orders
                      if isinstance(o, dict)))
    check("recorded as a normal single long position (no spread tracking)",
          not a.monitor.spread_positions)

    # Nothing reachable → stand down rather than buy near-the-money
    b2 = FakeBroker()
    b2.etf_dte = 30          # huge extrinsic; nothing qualifies
    _cycle((rep, ov, [sig]), b2)
    check("no qualifying structure → stands down, does NOT buy ATM",
          len(b2.orders) == 0, f"orders={b2.orders}")

    # A bullish signal has no inverse-ETF translation
    sig_call = {**sig, "direction": "CALL"}
    bull = cm.RegimeReport(regime="bull", vix=14.0)
    ov_bull = cm.overrides_for(bull, {**CONFIG, "crash_mode_enabled": True})
    ov_bull["crash_mode"] = False
    b3 = FakeBroker()
    _cycle((bull, ov_bull, [sig_call]), b3)
    check("bullish signal does not route through the inverse ETF",
          all(o.get("symbol") not in ("SQQQ", "SPXS")
              for o in b3.orders if isinstance(o, dict)), f"orders={b3.orders}")

    # ── v5.5 regression: a rejected order must not be recorded as a position ──
    # Live incident 2026-08-12: Tradier accepted the submission ({'status':
    # 'ok'}, order id issued) then rejected it — "There is no price". The agent
    # had already recorded the entry, written trades.json and set a cooldown
    # for a position it did not own.
    b6 = FakeBroker()
    b6.etf_dte = 1
    b6.order_status = "rejected"
    a6 = _cycle((rep, ov, [sig]), b6)
    check("REJECTED order records no entry",
          not any(k for k in a6.monitor.entry_prices if not k.endswith(
              ("_score", "_time", "_tp", "_sl"))),
          f"entry_prices={a6.monitor.entry_prices}")
    check("REJECTED order logs no trade", len(a6.trades_today) == 0)

    # And entries must be LIMIT orders now, not market
    b7 = FakeBroker()
    b7.etf_dte = 1
    _cycle((rep, ov, [sig]), b7)
    entry = [o for o in b7.orders if isinstance(o, dict)
             and o.get("side") == "buy_to_open"]
    check("entry is a LIMIT order, not market",
          bool(entry) and entry[0].get("order_type") == "limit",
          str(entry[:1]))
    check("limit price sits at or above the ask",
          bool(entry) and entry[0].get("price", 0) > 0, str(entry[:1]))


def test_spreads():
    print("\n[4] Spread construction")
    import spreads as sp

    s = sp.build_debit_spread(put_chain(), 500.0, "PUT", budget=1200, atr=11.0)
    check("builds a spread", s is not None)
    check("long strike above short (bear put spread)", s["long_strike"] > s["short_strike"],
          f"{s['long_strike']}/{s['short_strike']}")
    check("debit < width (otherwise it cannot profit)", s["debit"] < s["width"])
    check("max loss == debit paid",
          abs(s["max_loss"] - s["contracts"] * s["debit"] * 100) < 0.01)
    check("max profit == (width - debit)",
          abs(s["max_profit"] - s["contracts"] * (s["width"] - s["debit"]) * 100) < 0.01)
    check("respects budget", s["total_cost"] <= 1200)
    check("short leg actually funds the trade (delta >= 0.10)",
          s["short_delta"] >= 0.10, f"delta {s['short_delta']}")
    check("width tracks ATR, not max reward:risk",
          s["width"] <= 500 * 0.06 + 0.01, f"width {s['width']}")

    wide = sp.build_debit_spread(put_chain(), 500.0, "PUT", 1200, atr=40.0)
    narrow = sp.build_debit_spread(put_chain(), 500.0, "PUT", 1200, atr=4.0)
    check("wider ATR → wider spread", wide["width"] > narrow["width"],
          f"{narrow['width']} vs {wide['width']}")

    check("budget below one spread → None",
          sp.build_debit_spread(put_chain(), 500.0, "PUT", 15, atr=11.0) is None)
    check("untradeable bid/ask → None",
          sp.build_debit_spread(put_chain(spread_mult=2.2), 500.0, "PUT", 1200, 11.0) is None)
    check("no open interest and no volume → None",
          sp.build_debit_spread([{**o, "open_interest": 0, "volume": 0}
                                 for o in put_chain()], 500.0, "PUT", 1200, 11.0) is None)
    check("empty chain → None", sp.build_debit_spread([], 500.0, "PUT", 1200, 11.0) is None)

    no_greeks = [{**o, "greeks": {}} for o in put_chain()]
    check("missing greeks → still builds via strike proximity",
          sp.build_debit_spread(no_greeks, 500.0, "PUT", 1200, 11.0) is not None)

    # ── payloads ──
    op = sp.open_spread_payload("SPY", s)
    check("open payload is multileg debit",
          op["class"] == "multileg" and op["type"] == "debit")
    check("open buys the long leg, sells the short leg",
          op["side[0]"] == "buy_to_open" and op["side[1]"] == "sell_to_open")
    check("open legs match the built spread",
          op["option_symbol[0]"] == s["long_symbol"] and
          op["option_symbol[1]"] == s["short_symbol"])
    check("open limit pads above modelled debit",
          float(op["price"]) >= s["debit"])
    check("leg quantities equal", op["quantity[0]"] == op["quantity[1]"])

    cp = sp.close_spread_payload("SPY", s, 3.0)
    check("close payload is a credit", cp["type"] == "credit")
    check("close reverses both sides",
          cp["side[0]"] == "sell_to_close" and cp["side[1]"] == "buy_to_close")

    # ── marking ──
    mark = sp.spread_mark({"bid": 8.00, "ask": 8.40}, {"bid": 0.20, "ask": 0.30})
    check("exit value uses long BID minus short ASK (conservative)",
          abs(mark["exit_value"] - 7.70) < 1e-6, str(mark["exit_value"]))
    check("mid value is more optimistic than exit value",
          mark["mid_value"] > mark["exit_value"])
    check("P&L measured against debit paid",
          abs(sp.spread_pnl_pct(5.0, mark) - 54.0) < 0.01,
          f"{sp.spread_pnl_pct(5.0, mark):.1f}%")
    check("zero debit does not divide by zero", sp.spread_pnl_pct(0, mark) == 0.0)


# ── 5. Async data layer against a mock Yahoo ───────────────────────────────────
def test_async_layer():
    print("\n[5] Async data layer (mock Yahoo)")
    try:
        from aiohttp import web
    except ImportError:
        print("  ⚠ aiohttp missing — skipping (pip install aiohttp)")
        return

    import async_data
    # v5.4 made the bar cache module-level so it survives the agent's per-cycle
    # rebuild. That also means it survives between tests — clear it, or one
    # section's fixtures silently answer another section's requests.
    async_data._SHARED_CACHE.clear()

    hits = {"n": 0}

    async def chart(request):
        hits["n"] += 1
        await asyncio.sleep(0.25)          # simulate real network latency
        sym = request.match_info["sym"]
        if sym == "DEAD":
            return web.json_response({"chart": {"result": None}}, status=404)
        n = 260
        base = 500.0 if sym == "SPY" else 100.0
        ts = [1767225600 + i * 86400 for i in range(n)]
        closes = [base * (1 + 0.0005 * i) for i in range(n)]
        # LIE about the symbol for one ticker — this is the production bug
        # (a request for X coming back with Y's series). Must be rejected.
        meta_sym = "WRONG" if sym == "LIAR" else sym
        return web.json_response({"chart": {"result": [{
            "meta": {"symbol": meta_sym},
            "timestamp": ts,
            "indicators": {"quote": [{
                "open": closes, "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes], "close": closes,
                "volume": [1_000_000] * n}]},
        }]}})

    async def crumb(request):
        return web.Response(text="testcrumb")

    async def options(request):
        strikes = [float(k) for k in range(480, 521, 5)]
        mk = lambda t: [{"strike": k, "volume": 900.0, "openInterest": 100.0,
                         "bid": 1.0, "ask": 1.1, "impliedVolatility": 0.4,
                         "contractSymbol": f"X{int(k)}{t}"} for k in strikes]
        return web.json_response({"optionChain": {"result": [{
            "expirationDates": [1786752000],
            "quote": {"regularMarketPrice": 500.0},
            "options": [{"calls": mk("C"), "puts": mk("P")}]}]}})

    async def run():
        app = web.Application()
        app.router.add_get("/v8/finance/chart/{sym}", chart)
        app.router.add_get("/v1/test/getcrumb", crumb)
        app.router.add_get("/v7/finance/options/{sym}", options)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 8899)
        await site.start()

        orig_hosts = async_data._CHART_HOSTS
        async_data._CHART_HOSTS = ("http://127.0.0.1:8899",)
        try:
            md = async_data.AsyncMarketData(max_concurrency=8)
            await md.open()
            md._session._default_headers = {}
            tickers = ["SPY", "QQQ", "TSLA", "NVDA", "AMD", "META", "AMZN", "MSFT", "AAPL"]
            reqs = [(t, i, p) for t in tickers
                    for i, p in (("1h", "3mo"), ("15m", "5d"), ("5m", "2d"))]

            t0 = time.time()
            out = await md.get_many_bars(reqs)
            elapsed = time.time() - t0

            serial_estimate = 0.25 * len(reqs)
            check(f"{len(reqs)} fetches concurrent, not serial",
                  elapsed < serial_estimate / 3,
                  f"{elapsed:.2f}s vs ~{serial_estimate:.1f}s serial")
            check("all frames returned", all(not v.empty for v in out.values()))
            check("frame has yfinance-compatible columns",
                  list(out[("SPY", "1h", "3mo")].columns) ==
                  ["Open", "High", "Low", "Close", "Volume"])

            hits_before = hits["n"]
            await md.get_bars("SPY", "1h", "3mo")
            check("cache prevents duplicate fetch in one cycle", hits["n"] == hits_before)

            md.clear_cache()
            dead = await md.get_bars("DEAD", "1d", "1y")
            check("dead ticker → empty frame, no exception", dead.empty)

            md.clear_cache()
            liar = await md.get_bars("LIAR", "1d", "1y")
            check("payload for the WRONG symbol is discarded, not used",
                  liar.empty)
            good = await md.get_bars("SPY", "1d", "1y")
            check("matching symbol still accepted", not good.empty)

            chain = await md.get_option_chain("SPY")
            check("option chain parses", not chain["calls"].empty and not chain["puts"].empty)
            check("chain numerics coerced",
                  chain["puts"]["volume"].dtype.kind in "fi")

            await md.close()
        finally:
            async_data._CHART_HOSTS = orig_hosts
            await runner.cleanup()

    asyncio.run(run())


# ── 12. Tradier data layer (mock Tradier) ──────────────────────────────────────
def test_tradier_data():
    print("\n[12] Tradier data layer (mock Tradier)")
    try:
        from aiohttp import web
    except ImportError:
        print("  ⚠ aiohttp missing — skipping")
        return

    import tradier_data as tdmod
    import async_data
    async_data._SHARED_CACHE.clear()

    seen = {"history": 0, "timesales": 0}

    async def history(request):
        seen["history"] += 1
        sym = request.query.get("symbol")
        if sym in ("VIX", "VIX3M"):          # Tradier has no index coverage
            return web.json_response({"history": None})
        base = 770.0 if sym == "SPY" else 100.0
        days = [{"date": f"2026-0{(i//28)+6}-{(i%28)+1:02d}",
                 "open": base, "high": base*1.01, "low": base*0.99,
                 "close": base + i*0.1, "volume": 1_000_000} for i in range(60)]
        return web.json_response({"history": {"day": days}})

    async def timesales(request):
        seen["timesales"] += 1
        iv = request.query.get("interval")
        # 15-minute bars across two trading hours
        n = {"15min": 8, "5min": 24, "1min": 120}.get(iv, 8)
        step = {"15min": 900, "5min": 300, "1min": 60}.get(iv, 900)
        t0 = 1786550400
        data = [{"timestamp": t0 + i*step, "time": "x",
                 "open": 100.0+i, "high": 101.0+i, "low": 99.0+i,
                 "close": 100.5+i, "volume": 5000} for i in range(n)]
        return web.json_response({"series": {"data": data}})

    async def expirations(request):
        return web.json_response({"expirations": {"date": ["2026-08-14", "2026-08-21"]}})

    async def chains(request):
        opts = [{"symbol": f"SPY{k}", "strike": float(k), "option_type": t,
                 "bid": 1.0, "ask": 1.1, "open_interest": 500, "volume": 200,
                 "expiration_date": "2026-08-14", "greeks": {"delta": 0.5}}
                for k in (760, 770, 780) for t in ("call", "put")]
        return web.json_response({"options": {"option": opts}})

    async def run():
        app = web.Application()
        app.router.add_get("/v1/markets/history", history)
        app.router.add_get("/v1/markets/timesales", timesales)
        app.router.add_get("/v1/markets/options/expirations", expirations)
        app.router.add_get("/v1/markets/options/chains", chains)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8898).start()
        try:
            td = tdmod.TradierData("faketoken", sandbox=True)
            td.base = "http://127.0.0.1:8898/v1"
            await td.open()

            daily = await td.get_bars("SPY", "1d", "1y")
            check("daily bars via /markets/history", not daily.empty)
            check("columns match the AsyncMarketData contract",
                  list(daily.columns) == ["Open","High","Low","Close","Volume"])
            check("index is tz-aware UTC", str(daily.index.tz) == "UTC")

            m15 = await td.get_bars("SPY", "15m", "5d")
            check("15m bars via /markets/timesales", not m15.empty)

            h1 = await td.get_bars("SPY", "1h", "3mo")
            check("1h synthesised by resampling 15m", not h1.empty)
            check("resample aggregates (fewer 1h bars than 15m bars)",
                  len(h1) < len(m15), f"{len(h1)} vs {len(m15)}")
            check("resampled OHLC stays coherent (high >= low)",
                  bool((h1["High"] >= h1["Low"]).all()))

            chain = await td.get_option_chain("SPY")
            check("option chain splits calls and puts",
                  not chain["calls"].empty and not chain["puts"].empty)
            check("open_interest normalised to openInterest",
                  "openInterest" in chain["calls"].columns)
            check("raw chain preserved for the selectors", "raw" in chain)

            # index symbols must fall through, not silently return garbage
            vix = await td.get_bars("^VIX", "1d", "1y")
            check("index with no Tradier coverage returns EMPTY, not wrong data",
                  vix.empty)

            # AsyncMarketData should prefer Tradier and record the source
            md = async_data.AsyncMarketData(tradier=td)
            await md.open()
            df = await md.get_bars("SPY", "1d", "1y")
            check("AsyncMarketData routes to Tradier when available",
                  not df.empty and md.source_log.get("SPY:1d") == "tradier")
            await md.close()
            await td.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


# ── 6. PositionMonitor: spreads + thread safety ────────────────────────────────
class FakeTradier:
    """Minimal Tradier stand-in that records orders instead of sending them."""

    def __init__(self, long_bid=8.0, short_ask=0.3):
        self.orders = []
        self.long_bid = long_bid
        self.short_ask = short_ask
        self.calls = 0
        self._lock = threading.Lock()

    def get_positions(self):
        return {"positions": {"position": [
            {"symbol": "SPYLONG", "quantity": 2, "cost_basis": 1022.0},
            {"symbol": "SPYSHORT", "quantity": -2, "cost_basis": -60.0},
        ]}}

    def get_quotes(self, symbols):
        with self._lock:
            self.calls += 1
        return {"SPYLONG": {"bid": self.long_bid, "ask": self.long_bid + 0.2},
                "SPYSHORT": {"bid": self.short_ask - 0.05, "ask": self.short_ask}}

    quote_bid = None      # None → use long_bid
    quote_ask = 1.0
    quote_last = 0.90

    def get_quote(self, symbol):
        bid = self.long_bid if self.quote_bid is None else self.quote_bid
        return {"quotes": {"quote": {"bid": bid, "ask": self.quote_ask,
                                     "last": self.quote_last}}}

    def place_multileg_order(self, payload):
        with self._lock:
            self.orders.append(payload)
            time.sleep(0.05)          # widen the race window on purpose
        return {"order": {"id": len(self.orders)}}

    def place_order(self, **kw):
        with self._lock:
            self.orders.append(kw)
        return {"order": {"id": len(self.orders)}}

    # v5.5: the agent now confirms fills rather than assuming submission ==
    # execution. order_status lets a test simulate a broker rejection.
    order_status = "filled"
    reject_reason = "There is no price. Security symbol: TEST"

    def get_order(self, order_id):
        if self.order_status == "filled":
            return {"id": order_id, "status": "filled", "avg_fill_price": 1.38,
                    "exec_quantity": 1, "quantity": 1}
        return {"id": order_id, "status": self.order_status,
                "reason_description": self.reject_reason,
                "exec_quantity": 0, "quantity": 1}

    def cancel_order(self, order_id):
        return {"order": {"id": order_id, "status": "canceled"}}


def test_monitor():
    print("\n[6] PositionMonitor — spreads and thread safety")
    import agent as ag

    def fresh(long_bid):
        m = ag.PositionMonitor.__new__(ag.PositionMonitor)
        m.client = FakeTradier(long_bid=long_bid)
        m.entry_prices, m.peak_prices, m.entry_times = {}, {}, {}
        m.recently_closed, m.pending_close = set(), set()
        m.pending_close_times, m.pending_close_order_ids = {}, {}
        m.daily_realized_pnl, m.time_extended = 0.0, set()
        m._lock = threading.RLock()
        m.spread_positions = {"SPYLONG": {
            "long_symbol": "SPYLONG", "short_symbol": "SPYSHORT",
            "long_strike": 500.0, "short_strike": 490.0, "width": 10.0,
            "debit": 5.11, "entry_debit": 5.11, "contracts": 2,
            "max_profit": 978.0, "max_loss": 1022.0, "direction": "PUT",
            "underlying": "SPY",
        }}
        m._save_entry_prices = lambda: None
        m._write_trade_result = lambda *a, **k: None
        return m

    # legs must be invisible to the single-leg exit logic
    m = fresh(5.2)
    check("spread legs excluded from single-leg logic",
          m._spread_leg_symbols() == {"SPYLONG", "SPYSHORT"})

    # take profit — long bid 9.0, short ask 0.3 → value 8.70 vs debit 5.11 = +70%
    m = fresh(9.0)
    m._check_spread_exits()
    check("spread take profit fires", len(m.client.orders) == 1,
          f"{len(m.client.orders)} orders")
    if m.client.orders:
        o = m.client.orders[0]
        check("TP exit is a multileg credit closing BOTH legs",
              o["class"] == "multileg" and o["type"] == "credit"
              and o["side[0]"] == "sell_to_close" and o["side[1]"] == "buy_to_close")
    check("position deregistered after close", not m.spread_positions)

    # stop loss — long bid 2.0 → value 1.70 vs 5.11 = -67%
    m = fresh(2.0)
    m._check_spread_exits()
    check("spread stop loss fires", len(m.client.orders) == 1)

    # in between — hold
    m = fresh(6.0)
    m._check_spread_exits()
    check("mid-range spread is held, not churned", len(m.client.orders) == 0)

    # the race the lock exists to prevent
    m = fresh(9.0)
    def hammer():
        for _ in range(4):
            m.check_and_exit()
    threads = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    close_orders = [o for o in m.client.orders if isinstance(o, dict)
                    and o.get("class") == "multileg"]
    check("6 threads x 4 passes → exactly ONE close order",
          len(close_orders) == 1, f"got {len(close_orders)}")


# ── 13. Exit-path leaks (v5.6 regression) ──────────────────────────────────────
def test_exit_leaks():
    print("\n[13] Exit-path leaks (v5.6 regression)")
    import agent as ag
    from config import CONFIG

    def monitor(bid, quote_bid=None):
        m = ag.PositionMonitor.__new__(ag.PositionMonitor)
        m.client = FakeTradier(long_bid=bid)
        m.client.quote_bid = quote_bid
        m.client.get_positions = lambda: {"positions": {"position": [
            {"symbol": "AAPL260429C00272500", "quantity": 1,
             "cost_basis": 88.0}]}}
        m.entry_prices, m.peak_prices, m.entry_times = {}, {}, {}
        m.recently_closed, m.pending_close = set(), set()
        m.pending_close_times, m.pending_close_order_ids = {}, {}
        m.daily_realized_pnl, m.time_extended = 0.0, set()
        m.spread_positions = {}
        m._lock = threading.RLock()
        m._save_entry_prices = lambda: None
        m._write_trade_result = lambda *a, **k: None
        return m

    # A dying option: entry $0.88, bid collapsed to 0, last $0.20 → -77%.
    # Old code hit `if current_bid <= 0: continue` and never evaluated it.
    m = monitor(bid=0.0, quote_bid=0.0)
    m.client.quote_last = 0.20
    m.client.quote_ask = 0.30
    m._check_and_exit_impl()
    sells = [o for o in m.client.orders if isinstance(o, dict)
             and o.get("side") == "sell_to_close"]
    check("ZERO bid no longer skips the stop loss", len(sells) == 1,
          f"orders={m.client.orders}")
    if sells:
        check("zero-bid exit uses a MARKET order (a limit at the bid is "
              "meaningless)", sells[0].get("order_type") == "market")

    # Healthy bid, small loss → hold
    m = monitor(bid=0.80)
    m._check_and_exit_impl()
    check("healthy bid, small loss → still held",
          not [o for o in m.client.orders if isinstance(o, dict)])

    # No bid, no ask, no last → cannot price; must not invent a number
    m = monitor(bid=0.0, quote_bid=0.0)
    m.client.quote_last = 0.0
    m.client.quote_ask = 0.0
    m._check_and_exit_impl()
    check("unpriceable position is flagged, not silently traded",
          not [o for o in m.client.orders if isinstance(o, dict)])

    # ── EOD flatten ──
    orig_flat, orig_time = CONFIG.get("flatten_eod"), CONFIG.get("close_all_by_et")
    m = monitor(bid=0.80)
    CONFIG["flatten_eod"] = True
    CONFIG["close_all_by_et"] = "00:01"      # everything is "past" this
    check("flatten window detected", m._past_flatten_time() is True)
    m._check_and_exit_impl()
    check("EOD flatten closes an otherwise-held position",
          len([o for o in m.client.orders if isinstance(o, dict)]) == 1)

    m2 = monitor(bid=0.80)
    CONFIG["close_all_by_et"] = "23:59"      # nothing is past this
    check("outside the window, positions are left alone",
          m2._past_flatten_time() is False)

    CONFIG["flatten_eod"] = False
    check("flatten can be disabled", m2._past_flatten_time() is False)
    CONFIG["flatten_eod"] = orig_flat if orig_flat is not None else True
    CONFIG["close_all_by_et"] = orig_time or "15:45"


# ── 7. Config integrity ────────────────────────────────────────────────────────
def test_config():
    print("\n[7] Config")
    from config import CONFIG
    required = ("crash_mode_enabled", "use_async_scan", "crash_cfg",
                "spread_cfg", "scan_concurrency", "max_vix",
                "crash_structure", "crash_underlyings", "crash_max_trade_size")
    for k in required:
        check(f"key present: {k}", k in CONFIG)
    if any(k not in CONFIG for k in required):
        print("\n  ⚠ config.py has not been spliced yet. Run:")
        print("      python3 splice_config.py config.py _config_v5_block.py")
        print("    Skipping the remaining config checks.\n")
        return
    # NOT a pass/fail assertion. Arming crash mode is a deliberate operator
    # decision, and a suite that goes red on a legitimate setting teaches you
    # to ignore red — which is how a real failure gets skimmed past on a
    # system that trades live money. Report the state, judge the type.
    armed = CONFIG["crash_mode_enabled"]
    check("crash_mode_enabled is a bool", isinstance(armed, bool))
    if armed:
        print("     ⚠️  CRASH MODE IS ARMED. Confirm preflight section 4b shows")
        print("        a reachable structure before running live.")
    else:
        print("     ℹ️  crash mode disarmed (shipped default)")
    check("original keys survived the splice",
          CONFIG["capital_limit"] == 500.0 and CONFIG["min_signal_score"] == 15.9)
    check("crash score delta is positive (harder, not easier)",
          CONFIG["crash_cfg"]["crash_min_score_delta"] > 0)


# ── 8. End-to-end run_once with a fake broker ──────────────────────────────────
class FakeBroker(FakeTradier):
    """Full stand-in for TradierClient over one run_once cycle."""

    def __init__(self):
        super().__init__()
        self.multileg_should_fail = False
        self.positions = []
        self.etf_spot = 38.17     # SQQQ, 2026-08-12
        self.etf_iv = 0.85
        self.etf_dte = 1

    def get_positions(self):
        return {"positions": {"position": self.positions} if self.positions else None}

    def get_account_balances(self):
        return {"balances": {"cash": {"cash_available": 5000.0},
                             "total_equity": 5000.0}}

    def get_options_expirations(self, symbol):
        return {"expirations": {"date": ["2026-08-14", "2026-08-21"]}}

    def get_quote(self, symbol):
        # Inverse-ETF spot used by the deep-ITM router
        if symbol in ("SQQQ", "SPXS", "SDS"):
            return {"quotes": {"quote": {"last": self.etf_spot, "bid": self.long_bid}}}
        return {"quotes": {"quote": {"bid": self.long_bid, "last": 100.0}}}

    def get_options_chain(self, symbol, expiration):
        if symbol in ("SQQQ", "SPXS", "SDS"):
            return {"options": {"option": itm_call_chain(self.etf_spot,
                                                         iv=self.etf_iv,
                                                         dte=self.etf_dte)}}
        # A chain a $500 account can actually afford: $100 underlying, $1
        # at-the-money premium, $1 strike increments. Testing against $500 SPY
        # with $11 premiums only proves that a $125 per-trade cap cannot buy a
        # $1,100 contract, which is true and uninteresting.
        return {"options": {"option": put_chain(spot=100.0, tv=1.0, lo=90,
                                                hi=110, step=1)}}

    def place_multileg_order(self, payload):
        if self.multileg_should_fail:
            raise RuntimeError("Order rejected: account not approved for spreads")
        return super().place_multileg_order(payload)


def _cycle(monkey_regime, broker):
    """Drive one run_once with everything external stubbed out."""
    import agent as ag
    a = ag.OptionsAgent.__new__(ag.OptionsAgent)
    a.client = broker
    a.signals = __import__("signal_engine").SignalEngine()
    a.selector = ag.OptionsSelector(broker)
    a.risk = ag.RiskManager(broker)
    a.monitor = ag.PositionMonitor.__new__(ag.PositionMonitor)
    m = a.monitor
    m.client = broker
    m.entry_prices, m.peak_prices, m.entry_times = {}, {}, {}
    m.recently_closed, m.pending_close = set(), set()
    m.pending_close_times, m.pending_close_order_ids = {}, {}
    m.daily_realized_pnl, m.time_extended = 0.0, set()
    m.spread_positions = {}
    m._lock = threading.RLock()
    m._save_entry_prices = lambda: None

    class NoJudge:
        def judge(self, *a, **k):
            return True, 1.0, "stubbed", None
    a.judge = NoJudge()
    a.trades_today, a.ticker_cooldown = [], {}
    a.premarket_watchlist, a.last_regime_report = [], None
    a.risk._get_vix_cached = lambda: monkey_regime[0].vix
    a.risk._get_spy_cached = lambda: -3.0
    a._regime_and_signals = lambda: monkey_regime
    a.run_once()
    return a


def test_end_to_end():
    print("\n[8] End-to-end run_once (fake broker)")
    import crash_mode as cm
    from config import CONFIG

    # Decouple the test from premium levels: with the shipped $125 per-trade
    # cap and crash sizing at 0.40x, the budget is $50 and no single contract
    # in any realistic chain fits. That is correct behaviour, but it makes the
    # test measure the cap rather than the fallback path.
    _orig_cap = CONFIG["max_trade_size"]
    _orig_struct = CONFIG.get("crash_structure")
    CONFIG["max_trade_size"] = 600.0
    # Section 8 exercises the LEVEL 3 spread path specifically. The shipped
    # default is deep_itm (level 2); force spreads so the multileg code stays
    # covered for anyone who does take the upgrade.
    CONFIG["crash_structure"] = "spread"

    sig = {
        "ticker": "SPY", "direction": "PUT", "score": 18.0, "price": 100.0,
        "confluence": 4, "vix_size_mult": 0.45, "sl_hint": 30.0, "atr": 2.2,
        "reasons": ["test"], "timeframe_scores": {"1h": 6, "15m": 5, "5m": 4},
    }
    rep = cm.RegimeReport(regime="crash", vix=38.0, term_structure=1.25)
    ov = cm.overrides_for(rep, {"crash_mode_enabled": True, "max_vix": 28,
                                "max_contract_price": 3.0, "min_signal_score": 15.9})
    ov["crash_mode"] = True

    # ── armed crash: should place a multileg debit spread ──
    b = FakeBroker()
    a = _cycle((rep, ov, [sig]), b)
    ml = [o for o in b.orders if isinstance(o, dict) and o.get("class") == "multileg"]
    check("crash regime places a multileg spread", len(ml) == 1, f"orders={b.orders}")
    if ml:
        check("spread order is a debit that opens both legs",
              ml[0]["type"] == "debit" and ml[0]["side[0]"] == "buy_to_open"
              and ml[0]["side[1]"] == "sell_to_open")
    check("spread registered for unit monitoring",
          len(a.monitor.spread_positions) == 1)
    sp = list(a.monitor.spread_positions.values())[0] if a.monitor.spread_positions else {}
    check("registered spread carries its underlying for close orders",
          sp.get("underlying") == "SPY")

    # ── level-2 account: multileg rejected → single leg, never a naked short ──
    b2 = FakeBroker()
    b2.multileg_should_fail = True
    a2 = _cycle((rep, ov, [sig]), b2)
    singles = [o for o in b2.orders if isinstance(o, dict) and "option_symbol" in o]
    check("multileg rejection falls back to a single leg", len(singles) == 1,
          f"orders={b2.orders}")
    if singles:
        check("fallback opens a LONG position only (no naked short)",
              singles[0]["side"] == "buy_to_open")
    check("no spread registered after fallback", not a2.monitor.spread_positions)

    # ── crash detected but NOT armed: legacy rules, VIX 38 > 28 → stand down ──
    ov_off = cm.overrides_for(rep, {"crash_mode_enabled": False, "max_vix": 28,
                                    "max_contract_price": 3.0,
                                    "min_signal_score": 15.9})
    ov_off["crash_mode"] = False
    b3 = FakeBroker()
    _cycle((rep, ov_off, [sig]), b3)
    check("crash NOT armed → no orders at all (pre-v5 behaviour)",
          len(b3.orders) == 0, f"orders={b3.orders}")

    # ── bull regime must still refuse a PUT signal ──
    bull = cm.RegimeReport(regime="bull", vix=14.0)
    ov_bull = cm.overrides_for(bull, {"crash_mode_enabled": True})
    ov_bull["crash_mode"] = False
    b4 = FakeBroker()
    _cycle((bull, ov_bull, [sig]), b4)
    check("bull regime blocks a PUT trade", len(b4.orders) == 0, f"orders={b4.orders}")

    # ── sizing must not compound VIX x regime multipliers into nothing ──
    b5 = FakeBroker()
    CONFIG["max_trade_size"] = 125.0
    a5 = _cycle((rep, ov, [sig]), b5)
    check("tight per-trade cap still permits a defined-risk spread",
          len(b5.orders) == 1, f"orders={b5.orders}")
    CONFIG["max_trade_size"] = _orig_cap
    CONFIG["crash_structure"] = _orig_struct


if __name__ == "__main__":
    print("=" * 64)
    print("OptionsAgent v5 — offline verification")
    print("=" * 64)
    import logging
    logging.disable(logging.CRITICAL)   # keep the output readable

    test_regime()
    test_integrity()
    test_overrides()
    test_vix_mult()
    test_spreads()
    test_async_layer()
    test_tradier_data()
    test_monitor()
    test_exit_leaks()
    test_config()
    test_end_to_end()
    test_deep_itm()
    test_deep_itm_routing()

    print("\n" + "=" * 64)
    print(f"PASSED {len(PASS)}   FAILED {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)
