# Changelog

## v5.1 — Level-2 downside structure
*Replaces the v5.0 spread path after confirming Tradier options level 3 was not
being pursued.*

### Added
- **`deep_itm.py`** — deep in-the-money contract selection, the level-2
  substitute for a debit spread.
  - Gates on **extrinsic value as a % of premium** (default max 40%), not on
    delta. Extrinsic is the part of an option that is pure implied volatility
    and time — it is what a vol crush destroys. Intrinsic is arithmetic and
    survives. Filtering on the ratio measures the exposure directly instead of
    inferring it from moneyness.
  - Delta floor 0.60 / ceiling 0.92 as a secondary sanity check.
  - Returns `None` rather than degrading to a near-the-money contract.
  - Reports `vol_at_risk` — how many dollars of the spend are volatility
    premium rather than intrinsic value.
- **Inverse-ETF routing** (`OptionsSelector.select_deep_itm`). A bearish
  SPY/QQQ signal is executed as a **long CALL on SQQQ/SPXS**. A long call is
  options level 2; a put spread is level 3. Scoring still runs on the index —
  only the execution instrument changes.
- Multi-expiry search. Extrinsic scales with √time, so a budget that reaches a
  0.78-delta contract expiring tomorrow may reach nothing four days out. The
  selector scans up to `crash_max_expiries_to_scan` expiries per underlying.
- `crash_max_trade_size` — separate per-trade cap for crash trades only. Normal
  `max_trade_size` is untouched.
- `crash_allow_single_leg_fallback` (default `False`) — when no qualifying deep
  ITM contract exists, stand down rather than buy near-the-money.
- `deep_itm_take_profit_pct` / `deep_itm_stop_loss_pct` (35% / 30%). High delta
  on a 3x ETF means a 1% index move is already ~20-30% on the contract, so the
  OTM percentages were the wrong scale.
- `preflight.py` section 4b — runs the deep-ITM selector against **live
  chains** across every configured ETF and expiry, and prints which days are
  actually tradeable at your budget.
- Tests 9 and 10 in `test_v5.py` (deep ITM selection, crash routing).

### Fixed
- **Crash budget double-counting.** `crash_max_trade_size` was being multiplied
  by `size_mult` and `vix_size_mult` on top, turning $250 into $100 — which
  reaches no qualifying contract at all. The agent would have run every cycle
  and silently never traded. When set explicitly, it is now the final budget.
  (Same bug class as the v5.0 VIX/regime fix below.)
- `splice_config.py` is now **section-aware and re-runnable**. The v5.0 version
  bailed entirely if any v5 key was present, which would have skipped every new
  v5.1 key for anyone who already spliced.

### Changed
- `crash_structure` config key selects `deep_itm` (default) / `spread` /
  `single`.
- `overrides_for()` emits `prefer_defined_risk`; `prefer_spreads` retained as a
  compatibility alias.
- Spread code (`spreads.py`, multileg orders) is **retained but dormant** —
  switch `crash_structure` to `"spread"` in one line if level 3 is ever
  approved. Still covered by tests.

### Measured constraint (documented in `config.py`)
At a $250 budget, modelled on 2026-08-12 prices (SQQQ $38.17, SPXS $23.96):

| underlying | 1DTE | 2DTE | 4DTE |
|---|---|---|---|
| SQQQ | ✓ 0.60Δ | ✗ | ✗ |
| SPXS | ✓ 0.77Δ | ✓ 0.70Δ | ✓ 0.70Δ |

SQQQ alone at $250 only works the day before expiry, which is why SPXS is
listed second in `crash_underlyings`. Remove it for SQQQ-only, or raise
`crash_max_trade_size` to ~$400. `preflight.py` 4b supersedes this table with
live data.

---

## v5.0 — Crash-aware regime layer + async scan

### The problem
The v4 agent did not lose money in a selloff — it **switched itself off**. Five
independent gates fired at once:

| gate | v4 behaviour |
|---|---|
| `run_once` VIX pre-check | `VIX > 28` → skip the entire scan |
| `_vix_size_multiplier` | `VIX >= 30` → position size `0.0x` |
| `score_ticker` ATR check | `ATR > 5%` of price → reject every ticker |
| `max_contract_price` | `$3.00` → excludes most puts once IV expands |
| `_market_regime` | 20/50 daily EMA cross — lags a fast selloff by 1-3 weeks, and BULL regime *hard blocks PUTs* |

### Added
- **`crash_mode.py`** — regime classification into BULL / NEUTRAL / BEAR /
  CRASH, plus per-regime rule overrides. Detection inputs:
  - SPY 5/20 EMA (fast), 20/50 EMA (the old slow check, retained for logging
    the divergence), 200 SMA, drawdown from the rolling 20-day high
  - **VIX / VIX3M term structure.** Backwardation (ratio > 1.0) means spot vol
    is bid over three-month vol — the best free "this is a real event, not a
    dip" signal, and it flips days before a moving-average cross
  - **HYG / IEF credit ratio** — credit confirming equity weakness is what a
    recession looks like in market data, versus an equity-only growth scare
  - **10y vs 13w curve** — reported as macro context only, never gates a trade.
    An inverted curve leads recessions by 6-24 months, which is useless for
    timing a weekly option
  - CRASH requires 2+ votes; a degraded data read halves position size
- **`async_data.py`** — concurrent market data via aiohttp against Yahoo's
  chart/options endpoints, with a shared cookie/crumb bootstrap, a concurrency
  semaphore, per-cycle caching, and a **yfinance fallback in a worker thread**
  so a Yahoo endpoint change degrades speed rather than blinding the agent.
- `SignalEngine.get_top_signals_async` + `score_core`. Sync and async paths call
  **one** scoring implementation — trading logic is never duplicated.
- `preflight.py` — live dry run, places zero orders.
- `test_v5.py` — offline verification, no network or broker.
- `splice_config.py` — inserts config keys without overwriting a file that
  holds live credentials.

### Fixed
- **`vix_size_mult` was computed every cycle and never applied to anything.**
  Volatility-scaled sizing did not exist. It is now multiplied into the sizing
  decision.
- **VIX × regime multiplier compounding.** Once applied, the two stacked to
  `0.45 × 0.40 = 0.18x`, leaving a $22 budget against a $125 cap that silently
  rejected every contract. Now takes the tighter of the two, never the product.
- **Race between the 30s fast-monitor thread and the main cycle.** Both call
  `check_and_exit`; both could read the same position, decide to close it, and
  submit a sell — leaving a short. An `RLock` now makes the whole exit pass
  atomic. Verified with 6 threads × 4 passes → exactly one close order.

### Changed
- `_vix_size_multiplier` floors at 0.35-0.45x in crash mode instead of 0.0x.
  **Outside crash mode the original ladder is unchanged.**
- ATR ceiling, VIX ceiling, and premium cap are regime-variable.
- In crash mode the signal score bar goes **up** by 1.0, not down — chaos
  produces more signals, not better ones.
- `crash_mode_enabled` ships **`False`**. The agent detects and logs a crash,
  then declines to act, until armed deliberately.

### Not changed
The entry logic is untouched: `_score_timeframe` (RSI / MACD / VWAP / volume
surge and every threshold), the 3/3 timeframe confluence requirement, Gate 2
(squeeze breakout or unusual options flow), the scoring weights
(1H 0.4 / 15M 0.35 / 5M 0.25), all bonuses, the TradeJudge LLM gate, and the
single-leg TP / SL / trailing / 90-minute time exits.

### Open item
Across 74 closed trades in `trade_results.json`: 40.5% win rate, median
−21.7%, net +$180. Split by side — **PUTs: 25 trades, +$219. CALLs: 49 trades,
−$39.** The call side is a net loser over a reasonable sample while the put
side carries the book. Not acted on; it is an entry-logic question, not a
regime-layer one.
