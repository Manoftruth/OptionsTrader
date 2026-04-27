"""
Quick tests for:
1. Pre-market gap scanner
2. Re-entry logic

Run from ~/OptionsTrader:
    python3 test_new_features.py
"""

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

    with patch("agent.TradierClient"), \
         patch("agent.SignalEngine"), \
         patch("agent.OptionsSelector"), \
         patch("agent.RiskManager"), \
         patch("agent.PositionMonitor"), \
         patch("agent.TradeJudge"):

        agent = OptionsAgent()

        # Strong signal — should allow re-entry
        strong_signal = {"score": 18.5, "confluence": 4}
        result = agent._should_reenter("AMZN", strong_signal)
        assert result == True, "Strong signal should allow re-entry"
        print(f"  ✅ Strong signal (score=18.5, confluence=4): re-entry APPROVED")

        # Weak score — should block re-entry
        weak_signal = {"score": 15.5, "confluence": 4}
        result = agent._should_reenter("AMZN", weak_signal)
        assert result == False, "Weak score should block re-entry"
        print(f"  ✅ Weak signal (score=15.5): re-entry BLOCKED")

        # Low confluence — should block re-entry
        low_confluence = {"score": 18.5, "confluence": 2}
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

        strong_signal_pltr = {"score": 18.5, "confluence": 4}
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
