"""
Quick tests for:
1. Pre-market gap scanner
2. Re-entry logic

Run from ~/OptionsTrader:
    python3 test_new_features.py
"""

# ── Test isolation: MUST precede every project import (hard rule 1). ────────
# chdir()s into a temp dir, redirects OPTIONSTRADER_STATE_DIR, and aborts at
# exit if any production state file changed. See _sandbox.py.
import _sandbox  # noqa: F401

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime

# Basic logging so we can see output
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("OptionsAgent")

print("=" * 60)
print("  TESTING NEW FEATURES")
print("=" * 60)

# ── Test 1: Pre-market scanner ─────────────────────────────────
print("\n🌅 TEST 1: Pre-market gap scanner")
print("-" * 40)
try:
    from agent import OptionsAgent

    # Mock the heavy dependencies so we don't need live API
    with patch("agent.TradierClient"), \
         patch("agent.SignalEngine"), \
         patch("agent.OptionsSelector"), \
         patch("agent.RiskManager"), \
         patch("agent.PositionMonitor"), \
         patch("agent.TradeJudge"):

        agent = OptionsAgent()

        # Verify premarket_watchlist starts empty
        assert agent.premarket_watchlist == [], "premarket_watchlist should start empty"
        print("  ✅ premarket_watchlist initialized as empty list")

        # Mock catalyst scanner to return fake gap plays
        fake_catalysts = [
            {"ticker": "NVDA", "total_bonus": 4, "direction_bias": "CALL", "reasons": ["gap +8%", "earnings beat"]},
            {"ticker": "TSLA", "total_bonus": 3, "direction_bias": "CALL", "reasons": ["gap +5%"]},
            {"ticker": "COIN", "total_bonus": 2, "direction_bias": "PUT",  "reasons": ["gap -3%"]},
        ]

        with patch("catalyst_scanner.CatalystScanner") as MockScanner:
            MockScanner.return_value.get_top_catalyst_tickers.return_value = fake_catalysts
            agent.run_premarket_scan()

        assert len(agent.premarket_watchlist) > 0, "premarket_watchlist should be populated"
        assert "NVDA" in agent.premarket_watchlist, "NVDA should be in premarket watchlist"
        print(f"  ✅ Pre-market watchlist populated: {agent.premarket_watchlist}")

        # Verify top ticker is highest bonus
        assert agent.premarket_watchlist[0] == "NVDA", "Highest bonus ticker should be first"
        print("  ✅ Tickers sorted by bonus score (NVDA first)")

        print("  ✅ TEST 1 PASSED\n")

except Exception as e:
    print(f"  ❌ TEST 1 FAILED: {e}")
    import traceback; traceback.print_exc()


# ── Test 2: Re-entry logic ─────────────────────────────────────
print("🔄 TEST 2: Re-entry logic")
print("-" * 40)
try:
    from agent import OptionsAgent
    from config import CONFIG

    with patch("agent.TradierClient"), \
         patch("agent.SignalEngine"), \
         patch("agent.OptionsSelector"), \
         patch("agent.RiskManager"), \
         patch("agent.PositionMonitor"), \
         patch("agent.TradeJudge"):

        agent = OptionsAgent()

        # Derive the fixtures from config, not magic numbers: _should_reenter
        # gates on CONFIG["min_signal_score"] + 1.0, and that config value has
        # been retuned before (15.9 -> 14.1), which silently turned a "weak"
        # fixture into a passing one.
        REENTRY_BAR = CONFIG["min_signal_score"] + 1.0
        STRONG = round(REENTRY_BAR + 2.0, 2)   # comfortably over the bar
        WEAK   = round(REENTRY_BAR - 0.5, 2)   # just under the bar

        # Strong signal — should allow re-entry
        strong_signal = {"score": STRONG, "confluence": 4}
        result = agent._should_reenter("AMZN", strong_signal)
        assert result == True, "Strong signal should allow re-entry"
        print(f"  ✅ Strong signal (score={STRONG}, confluence=4): re-entry APPROVED")

        # Weak score — should block re-entry
        weak_signal = {"score": WEAK, "confluence": 4}
        result = agent._should_reenter("AMZN", weak_signal)
        assert result == False, "Weak score should block re-entry"
        print(f"  ✅ Weak signal (score={WEAK} < bar {REENTRY_BAR}): re-entry BLOCKED")

        # Low confluence — should block re-entry
        low_confluence = {"score": STRONG, "confluence": 2}
        result = agent._should_reenter("AMZN", low_confluence)
        assert result == False, "Low confluence should block re-entry"
        print(f"  ✅ Low confluence (2/4): re-entry BLOCKED")

        # Write a fake bad trade result and test blocking
        results_file = Path("trade_results.json")
        test_record = {
            "ticker": "PLTR",
            "direction": "CALL",
            "pnl_pct": -35.0,
            "pnl_dollars": -52.0,
            "exit_time": datetime.now().isoformat()
        }
        with open(results_file, "w") as f:
            f.write(json.dumps(test_record) + "\n")

        strong_signal_pltr = {"score": STRONG, "confluence": 4}
        result = agent._should_reenter("PLTR", strong_signal_pltr)
        assert result == False, "Should block re-entry after big loss"
        print(f"  ✅ After -35% loss on PLTR: re-entry BLOCKED")

        # Write a winning trade and test allowing re-entry
        win_record = {
            "ticker": "COIN",
            "direction": "CALL",
            "pnl_pct": 86.0,
            "pnl_dollars": 181.0,
            "exit_time": datetime.now().isoformat()
        }
        with open(results_file, "w") as f:
            f.write(json.dumps(win_record) + "\n")

        result = agent._should_reenter("COIN", strong_signal)
        assert result == True, "Should allow re-entry after winning trade"
        print(f"  ✅ After +86% win on COIN: re-entry APPROVED")

        # Cleanup test file
        results_file.unlink(missing_ok=True)

        print("  ✅ TEST 2 PASSED\n")

except Exception as e:
    print(f"  ❌ TEST 2 FAILED: {e}")
    import traceback; traceback.print_exc()

print("=" * 60)
print("  ALL TESTS COMPLETE")
print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  v5.22 regression tests — risk checks must fail CLOSED, config must be live
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  TEST 3: v5.22 — fail-closed risk checks and live config keys")
print("=" * 60)

_v522_fails = []


def _v522(label, cond, detail=""):
    if cond:
        print(f"  ✅ {label}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ❌ {label}" + (f"  — {detail}" if detail else ""))
        _v522_fails.append(label)


try:
    import agent as ag
    import crash_mode as cm
    from config import CONFIG

    # ── 1. Risk checks must fail CLOSED, not open ─────────────────────────
    # Before v5.22 both of these swallowed every exception and reported
    # "nothing is open", so a transient Tradier error let the agent stack
    # positions past its ceiling and double up on a ticker it already held.
    class _BrokenClient:
        def get_positions(self):
            raise RuntimeError("simulated Tradier outage")

    class _FlatClient:
        def get_positions(self):
            return {"positions": "null"}      # Tradier's genuine "flat"

    class _JunkClient:
        def get_positions(self):
            return ["unexpected", "shape"]

    rm = ag.RiskManager.__new__(ag.RiskManager)

    rm.client = _BrokenClient()
    _v522("broken broker → position count is UNKNOWN (-1), not 0",
          rm.get_open_position_count() == -1)
    _v522("broken broker → open tickers is None, not an empty set",
          rm.get_open_tickers() is None)

    rm.client = _JunkClient()
    _v522("malformed payload → UNKNOWN, not 0",
          rm.get_open_position_count() == -1)

    rm.client = _FlatClient()
    _v522("a genuinely flat account still reports 0, not UNKNOWN",
          rm.get_open_position_count() == 0,
          "Tradier sends the string 'null' when flat")
    _v522("a genuinely flat account reports an empty ticker set",
          rm.get_open_tickers() == set())

    # ── 2. Config keys that used to be dead must now be read ──────────────
    # The sandbox chdir()s into a temp dir, so resolve from the module
    # file rather than the working directory.
    src_agent = open(ag.__file__).read()
    for key in ("max_concurrent_positions", "daily_loss_limit",
                "take_profit_pct", "stop_loss_pct"):
        _v522(f"config key '{key}' is actually read by agent.py",
              f'"{key}"' in src_agent)

    _v522("the tighter of dollar and percent daily loss limit wins",
          'min(pct_limit' in src_agent)

    # ── 3. The bull PUT block is gone by default, and still switchable ────
    bull = cm.overrides_for(cm.RegimeReport(regime="bull", vix=14.0), {})
    _v522("bull regime allows PUTs by default", bull["allow_puts"] is True)
    off = cm.overrides_for(cm.RegimeReport(regime="bull", vix=14.0),
                           {"bull_allow_puts": False})
    _v522("bull_allow_puts=False still restores the block",
          off["allow_puts"] is False)

    # ── 4. The sync fallback must honour crash overrides ──────────────────
    # It used to hardcode "bear blocks CALL, bull blocks PUT", which matched
    # neither branch for regime=="crash" — so a degraded-data cycle during a
    # selloff permitted CALLS, the exact trade crash mode exists to forbid.
    import inspect
    import signal_engine as se
    sig = inspect.signature(se.SignalEngine.get_top_signals)
    _v522("sync get_top_signals accepts overrides", "overrides" in sig.parameters)
    sync_src = inspect.getsource(ag.OptionsAgent._regime_and_signals)
    _v522("sync call site passes overrides instead of discarding them",
          "overrides=ov" in sync_src)
    gts = inspect.getsource(se.SignalEngine.get_top_signals)
    _v522("sync direction gate is override-driven, not hardcoded by regime",
          "_allow_calls" in gts and "_allow_puts" in gts)
    _v522("the old hardcoded bull PUT block is gone",
          'regime == "bull" and sig["direction"] == "PUT"' not in gts)

    # ── 5. The entry pad must not give away edge ──────────────────────────
    _v522("entry_limit_pad_pct is 0 — no paying above the ask",
          float(CONFIG.get("entry_limit_pad_pct", 3.0)) == 0.0,
          f"is {CONFIG.get('entry_limit_pad_pct')}")

    # ── 6. Spread recap must cost the debit, not the long leg's ask ───────
    _v522("capped spread cost uses the per-unit debit",
          'trade["total_cost"] = _capped * _unit * 100' in src_agent)

    if _v522_fails:
        print(f"\n  ❌ TEST 3 FAILED: {len(_v522_fails)} check(s): {_v522_fails}")
    else:
        print("\n  ✅ TEST 3 PASSED\n")

except Exception as e:
    print(f"  ❌ TEST 3 FAILED: {e}")
    import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  v5.23 regression tests — bull PUTs, cap-bounded search, fills, EOD
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  TEST 4: v5.23 — bull PUTs reach execution, cap bounds the search")
print("=" * 60)

_v523_fails = []


def _v523(label, cond, detail=""):
    if cond:
        print(f"  ✅ {label}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"  ❌ {label}" + (f"  — {detail}" if detail else ""))
        _v523_fails.append(label)


try:
    import inspect
    import agent as ag
    import crash_mode as cm
    from config import CONFIG

    # ── 1. can_trade() must not re-block bull PUTs off the raw regime ─────
    rm = ag.RiskManager.__new__(ag.RiskManager)
    rm.check_daily_loss_limit = lambda c: (False, "OK")
    rm.get_open_position_count = lambda: 0
    rm._dynamic_max_positions = lambda: 2
    rm._dynamic_capital_limit = lambda: 1000.0
    rm._get_vix_cached = lambda: 15.0
    rm._get_spy_cached = lambda: 0.2
    put = {"total_cost": 50.0, "signal": {"direction": "PUT", "score": 16}}
    bull_ov = cm.overrides_for(cm.RegimeReport(regime="bull", vix=15.0), {})
    ok, why = rm.can_trade(put, 500.0, "bull", bull_ov)
    _v523("bull-regime PUT passes can_trade when overrides allow it", ok, why)
    off_ov = cm.overrides_for(cm.RegimeReport(regime="bull", vix=15.0),
                              {"bull_allow_puts": False})
    ok, why = rm.can_trade(put, 500.0, "bull", off_ov)
    _v523("bull_allow_puts=False still blocks it, via the overrides", not ok, why)

    # ── 2. The equity cap bounds contract SEARCH, not just the result ─────
    class _ChainClient:
        def get_options_chain(self, ticker, expiry):
            return {"options": {"option": [
                {"symbol": "T45", "option_type": "put", "strike": 100.0,
                 "ask": 2.50, "bid": 2.45, "greeks": {"delta": -0.45}},
                {"symbol": "T30", "option_type": "put", "strike": 97.0,
                 "ask": 0.80, "bid": 0.78, "greeks": {"delta": -0.30}},
            ]}}

    sel = ag.OptionsSelector.__new__(ag.OptionsSelector)
    sel.client = _ChainClient()
    sel.get_nearest_expiry = lambda t, days_out=0: "2026-10-21"
    sig = {"ticker": "TST", "direction": "PUT", "price": 100.0, "score": 20.0,
           "confluence": 4, "vix_size_mult": 1.0}
    _orig_td = CONFIG.get("contract_target_delta")
    CONFIG["contract_target_delta"] = 0.45      # pin: independent of config.py
    uncapped = sel.select_contract(sig, 500.0, {"max_contract_price": 3.0})
    capped = sel.select_contract(sig, 500.0, {"max_contract_price": 3.0},
                                 max_cost=91.0)
    CONFIG["contract_target_delta"] = _orig_td
    _v523("without the cap the selector reaches the $2.50 0.45-delta contract",
          uncapped and uncapped["option_symbol"] == "T45")
    _v523("with a $91 cap it picks what the cap can actually buy",
          capped and capped["option_symbol"] == "T30"
          and capped["total_cost"] <= 91.0,
          f"{capped and capped['option_symbol']} ${capped and capped['total_cost']}")

    run_src = inspect.getsource(ag.OptionsAgent.run_once)
    _v523("every selector call in run_once is cap-bounded",
          run_src.count("max_cost=_order_cap") == 4)
    _v523("an over-cap candidate moves to the next signal",
          "trying next signal" in run_src.split("can_trade(t,")[0].split("max_cost=_order_cap")[-1])

    # ── 3. sl_hint no longer overrides stop_loss_pct by default ───────────
    _v523("use_sl_hint defaults off", not CONFIG.get("use_sl_hint", False))
    _v523("sl_hint is gated on use_sl_hint",
          'CONFIG.get("use_sl_hint", False)' in run_src)

    # ── 4. Partial fills cancel the remainder ─────────────────────────────
    class _FillClient:
        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.cancels = []

        def get_order(self, oid):
            s, q = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"status": s, "exec_quantity": q, "quantity": 3}

        def cancel_order(self, oid):
            self.cancels.append(oid)

    a = ag.OptionsAgent.__new__(ag.OptionsAgent)
    a.client = _FillClient([("partially_filled", 1)])
    filled, why = a._await_fill("42", timeout=5)
    _v523("partial fill counts as filled", filled, why)
    _v523("partial fill cancels the unfilled remainder", a.client.cancels == ["42"])

    a.client = _FillClient([("canceled", 1)])
    filled, why = a._await_fill("43", timeout=5)
    _v523("partly filled then canceled is still a position we own", filled, why)

    a.client = _FillClient([("open", 0), ("filled", 1)])
    filled, why = a._await_fill("44", timeout=0.5)
    _v523("a fill that lands between the last poll and the cancel is kept",
          filled and a.client.cancels == ["44"], why)

    # ── 6. The fast exit monitor is gated on market hours ─────────────────
    fm_src = inspect.getsource(ag.OptionsAgent._fast_monitor_loop)
    _v523("fast monitor only acts while the market is open",
          'market_state(self.client) != "open"' in fm_src)

    # ── 7. regime_align_bonus is config, and the shadow log keeps it ──────
    _v523("overrides carry regime_align_bonus from config",
          cm.overrides_for(cm.RegimeReport(regime="bull", vix=15.0),
                           {"regime_align_bonus": 0.0})["regime_align_bonus"] == 0.0)
    import shadow as _sh
    _v523("resolved shadow records keep regime_bonus",
          'out["regime_bonus"]' in inspect.getsource(_sh))

    # ── v5.24: CALL fade ──────────────────────────────────────────────────
    a = ag.OptionsAgent.__new__(ag.OptionsAgent)
    sigs = [{"ticker": "AAA", "direction": "CALL", "score": 16.0, "reasons": ["rsi up"]},
            {"ticker": "BBB", "direction": "PUT", "score": 15.0, "reasons": ["rsi dn"]}]
    out = a._fade_calls(sigs, {"allow_puts": True})
    _v523("fade: a CALL signal becomes a faded PUT",
          out[0]["direction"] == "PUT" and out[0].get("faded") is True)
    _v523("fade: TradeJudge is told the trade is contrarian",
          out[0]["reasons"][0].startswith("🔄 CONTRARIAN FADE")
          and out[0]["reasons"][1] == "rsi up")
    _v523("fade: genuine PUT signals pass through unchanged",
          out[1] is sigs[1] and not out[1].get("faded"))
    _v523("fade: original signal dict is not mutated", sigs[0]["direction"] == "CALL")
    out = a._fade_calls(sigs, {"allow_puts": False})
    _v523("fade: with PUTs blocked a CALL is dropped, never bought as a CALL",
          [s["ticker"] for s in out] == ["BBB"])
    _v523("fade: run_once applies it behind the fade_calls switch",
          'CONFIG.get("fade_calls", False)' in run_src)

    # ── 5b. No new entries inside the flatten window ──────────────────────
    _v523("run_once stops entering once the flatten window opens",
          "self.monitor._past_flatten_time()" in run_src)

    if _v523_fails:
        print(f"\n  ❌ TEST 4 FAILED: {len(_v523_fails)} check(s): {_v523_fails}")
    else:
        print("\n  ✅ TEST 4 PASSED\n")

except Exception as e:
    print(f"  ❌ TEST 4 FAILED: {e}")
    import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  v5.25 — equity floor and phone alerts
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("  TEST 5: v5.25 — equity floor and alerts")
print("=" * 60)

_v525_fails = []


def _v525(label, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        _v525_fails.append(label)


try:
    import os as _os
    import agent as ag
    import alerts
    from config import CONFIG

    _v525("the sandbox disables alerts, so tests never reach the phone",
          alerts.enabled() is False)

    sent = []
    _real_send = alerts.send
    alerts.send = lambda *a, **k: sent.append((a, k)) or True

    class _Bal:
        def __init__(self, eq): self.eq = eq
        def get_account_balances(self):
            if self.eq is None:
                raise RuntimeError("simulated outage")
            return {"balances": {"total_equity": self.eq}}

    orig = {k: CONFIG.get(k) for k in ("equity_floor", "sandbox")}
    CONFIG["equity_floor"], CONFIG["sandbox"] = 100.0, False
    rm = ag.RiskManager.__new__(ag.RiskManager)
    rm._floor_path().unlink(missing_ok=True)

    rm.client = _Bal(250.0)
    halted, why = rm.check_equity_floor()
    _v525("above the floor: trading continues", not halted, why)

    rm.client = _Bal(None)
    halted, why = rm.check_equity_floor()
    _v525("equity unreadable: no entries (fails closed, not $500 fallback)", halted, why)
    _v525("an unreadable balance does NOT trip the persistent halt",
          not rm._floor_path().exists())

    rm.client = _Bal(99.0)
    halted, why = rm.check_equity_floor()
    _v525("below the floor: halted", halted, why)
    _v525("the halt is written to disk", rm._floor_path().exists())
    _v525("tripping the floor sends an urgent alert",
          any("floor" in a[0].lower() and k.get("priority") == "urgent" for a, k in sent))

    rm.client = _Bal(5000.0)
    halted, why = rm.check_equity_floor()
    _v525("the halt survives a recovery until the file is deleted", halted, why)
    rm._floor_path().unlink()
    halted, _ = rm.check_equity_floor()
    _v525("deleting the file resumes trading", not halted)

    CONFIG["equity_floor"] = None
    rm.client = _Bal(1.0)
    _v525("equity_floor None disables it", rm.check_equity_floor() == (False, "OK"))
    rm._floor_path().unlink(missing_ok=True)
    CONFIG.update(orig)

    # ── after the close: summary + overnight check ────────────────────────
    a = ag.OptionsAgent.__new__(ag.OptionsAgent)
    a.trades_today = [{"ticker": "SPY", "direction": "PUT", "contracts": 1,
                       "option_symbol": "SPYP", "ask": 0.8, "total_cost": 80.0}]
    a.risk = ag.RiskManager.__new__(ag.RiskManager)
    a.risk.get_account_balance = lambda: 620.0
    a.risk._start_of_day_capital = 609.0
    a.risk.get_open_position_count = lambda: 1
    sent.clear()
    a._after_close()
    titles = [x[0][0] for x in sent]
    _v525("after close: a day summary is sent",
          any("summary" in t for t in titles) and "+11.00" in sent[0][0][1], titles)
    _v525("after close: a leftover position raises an URGENT alert",
          any("open after close" in a_[0] and k.get("priority") == "urgent"
              for a_, k in sent))
    _v525("after close: today's trade list is cleared", a.trades_today == [])
    a.risk.get_open_position_count = lambda: 0
    sent.clear()
    a._after_close()
    _v525("after close, flat: summary only, no overnight alert",
          [x[0][0] for x in sent] == ["OptionsAgent: day summary"])

    # ── log lines that ask for a human go to the phone ────────────────────
    sent.clear()
    ag.log.error("❌ Could not get bid for X — manual intervention needed")
    ag.log.info("ordinary info line")
    _v525("a 'manual intervention' log line becomes an urgent alert",
          len(sent) == 1 and sent[0][1].get("priority") == "urgent")
    alerts.send = _real_send

    # ── dedupe, with sending forced on but the network stubbed ────────────
    posts = []
    _env = _os.environ.pop("OPTIONSTRADER_ALERTS")
    _cfg = {k: CONFIG.get(k) for k in ("alerts_enabled", "alert_ntfy_topic")}
    CONFIG["alerts_enabled"], CONFIG["alert_ntfy_topic"] = True, "test-topic"
    _post = alerts.requests.post
    alerts.requests.post = lambda *a, **k: posts.append(a)
    alerts._last_sent.clear()
    first = alerts.send("t", "same message")
    second = alerts.send("t", "same message")
    import time as _t; _t.sleep(0.2)
    _v525("identical alerts within 30 min are sent once",
          first and not second and len(posts) == 1)
    _v525("alerts go to the configured topic", posts and posts[0][0].endswith("/test-topic"))
    alerts.requests.post = _post
    CONFIG.update(_cfg)
    _os.environ["OPTIONSTRADER_ALERTS"] = _env

    if _v525_fails:
        print(f"\n  ❌ TEST 5 FAILED: {len(_v525_fails)} check(s): {_v525_fails}")
    else:
        print("\n  ✅ TEST 5 PASSED\n")

except Exception as e:
    print(f"  ❌ TEST 5 FAILED: {e}")
    import traceback; traceback.print_exc()
