# OptionsAgent: project notes for Claude Code

Automated short-dated options bot on EC2. Tradier API (LIVE endpoint), Claude Haiku
TradeJudge gate, four-gate signal engine (regime, entry qualifier, 3/3 timeframe
confluence, earnings blackout), scan loop roughly every 200s, catalyst and insider
screens via yfinance around 09:00 ET.

## Files that matter

- `agent.log`: logrotate daily, `.1` uncompressed, `.2` to `.14` gzipped, 14-day retention.
- `trades.json`: JSONL, one record per ORDER, not per position. Fields: ticker,
  direction, structure, option_symbol, strike, expiry, ask, bid, delta, contracts,
  total_cost, score, time. Spread records add a `spread` object, deep-ITM records add
  a `deep_itm` object and `source_signal_ticker`.
- `trades.json.bak`: DELETED 2026-10-20 at the owner's request. It was the pre-cleanup copy holding
  the 74 test-pollution records described below; the cleanup itself is documented there.
- `risk_state.json`: daily loss and kill-switch state.
- `report.py`: `./venv/bin/python report.py --since YYYY-MM-DD`
- `test.py`, `test_newfeatures.py`: run `run_once` against a fake broker and a mock Tradier.

## Hard rules

1. Never run `test.py` or `test_newfeatures.py` without `import _sandbox` as the first
   line. On 2026-08-12, 08-18 and 08-25 the tests wrote 74 mock-chain candidates into the
   production `trades.json` (symbol roots `OPT` and `ETF` instead of the ticker, SPY
   strikes at $100, deltas of exactly 0.5 and 0.333, bid equal to ask, past-dated
   expiries). Removed on 2026-09-10 with
   `jq -c 'select(.option_symbol[0:(.ticker|length)] == .ticker)'`.
2. Never run tests during market hours on this box. The live agent writes the same files.
3. Tradier is the source of truth for fills. `trades.json` records orders at signal time
   at the ask, so its counts run higher than Tradier positions (multi-order fills) and
   its costs differ slightly.

## Diagnosis as of 2026-09-10

Source: Tradier closed trades Apr 8 to Sep 4, 159 positions. Feb and Mar fell off the
500-event cap in report.py and are not in any of these numbers.

- Window total: +$1,342 gross, +$1,097 after $246 commissions. Excluding one META 655C
  trade (+$1,064, +612%): +$33 on 158 positions and $37k deployed. Expectancy is about
  zero. Historical win rate 43 to 46%, breakeven about 43% at a $72 average win and
  $52 average loss.
- Since Aug 18: 20 positions, 5W / 15L (25%), -$415 gross, about -$500 with commissions
  and the 9/10 QQQ put. Exits behaved in this window (losses capped between -9% and -34%,
  two take-profits near +50%). It is a win-rate problem, not an exit problem.
- 28 of the 29 orders since Aug 18 were calls.
- More than half the positions since Aug 18 were held overnight at 1 to 2 DTE, because
  HOOD, PLTR, RBLX, SOXL, TQQQ and SPXL expire Fridays only and the mega-caps only
  Mon/Wed/Fri. The overnight groups (Aug 19, Aug 28, Sep 4) account for about $390 of
  the $440 loss. RESOLVED 2026-09-10: the owner confirms 1-2 DTE overnight holds
  are intended and allowed. This is a known, accepted cost, not a bug to fix.
- Nine trades historically blew through the 38% stop (NVDA 7/1 at -94%, AMD 7/22 at -89%),
  none since Aug 18.
- Sizing: $167 average order, 2.6 contracts, on roughly $680 of equity. Aug 19 had $898
  in two AAPL calls and a 9-lot TSLA ($693) on about $1,150 of equity. The $70 daily
  loss limit cannot function at that size.
- MSFT is 2/10 all-time (-$411). SPY 13/9 (+$443) is the only consistent name.
- "Errors in log: 42" is entirely yfinance at 09:00:34 to 09:00:37 (12x CFLT and 3x EXAS,
  both delisted, plus Yahoo 401 crumb/blocked responses). Zero errors during the session.
  Insider bonus 0/29 and catalyst bonus 0/29: those screens score tickers the bot never trades.
- 2026-09-07 (Labor Day): full-size log. The bot ran a complete session against a closed market.
- `report.py` mixes three windows. AGENT ACTIVITY comes from today's `agent.log` only;
  TRADE SUMMARY is `trades.json` filtered by `--since`; P&L is the last 500 Tradier history
  events filtered by `--since`. Its auto-analysis ("low take-profit rate, give trades more
  room") is derived from today's log and is wrong.
- Open question: has the `vertical_debit` / `deep_itm` structure path ever placed a live
  order? Only single-leg symbols have ever filled at Tradier.

## Punch list, in order

1. Test isolation. `_sandbox.py` in the project root, `import _sandbox` as the first line
   of both test files. Set `FIXTURES` to the real config filename. If the guard trips, the
   named writer builds its path from `__file__` and needs a path override.
2. Position cap. Add
   `cap_contracts(contracts, ask, equity, max_pct=0.15) -> min(contracts, int((equity*max_pct)//(ask*100)))`
   immediately before order placement. Pull `equity` from Tradier balances each cycle, not
   from `risk_state.json`. A result of 0 means skip the trade. Never round up to 1.
3. `report.py`:
   a. Validate on load: drop records whose `option_symbol` root is not the ticker or whose
      `expiry` is before the record date, and print how many were dropped.
   b. One window for all three sections. AGENT ACTIVITY should parse the rotated logs for
      the `--since` range, or be labeled "today only".
   c. Page Tradier history with start/end instead of taking the first 500, and include
      `type=option` events (expiration, assignment) so expired-worthless positions count as losses.
   d. Hard-fail on an unparseable `--since`.
   e. Count "trades executed" from Tradier fills, not from `trades.json`.
4. Logging. `logging.getLogger("yfinance").setLevel(logging.CRITICAL)`. Wrap `yf.download`
   in a per-day dead-ticker cache. Remove CFLT and EXAS from the screen universe.
5. Hours and holiday gate. Use Tradier `GET /v1/markets/clock` (`clock.state` is one of
   premarket, open, postmarket, closed). Trade only on `open`. Let the 09:00 screen run
   on `premarket`.
6. Log retention. `grep -l agent.log /etc/logrotate.d/*`, then `rotate 14` to `rotate 90`.
7. Expiry policy. SUPERSEDED 2026-10-20: the owner does NOT want positions held
   overnight. `flatten_eod: True` / `close_all_by_et: "15:45"` enforce it, and v5.23
   blocks new entries inside the flatten window. The 09-10 text below is history.
   Previously: DECIDED 2026-09-10: 1-2 DTE overnight holds ARE allowed. No code
   change. The same-day-expiry restriction described here is explicitly not wanted, so
   do not re-open this. Note the tension on record: the overnight groups (Aug 19,
   Aug 28, Sep 4) account for roughly $390 of the $440 loss since Aug 18, and that is
   accepted.

The live agent will not pick up edits until it is restarted. Restart after the close.

## v5.22 — 2026-10-20: why it was losing, and what changed

The request was "run as aggressively as possible to make as much money as
possible." Measurement said those two goals pointed in opposite directions, so
the aggression knobs were left alone and the edge was fixed instead. All
guardrails stay intact.

### What the data actually said

Realised, from `trade_results.json` (113 closed trades, Mar-Oct):

| window | n | win rate | net | EV/trade |
|---|---|---|---|---|
| all time | 113 | 33.6% | -$742 | -$6.56 |
| since 2026-08-18 | 36 | 19.4% | -$722 | -$20.07 |
| October | 7 | 14.3% | -$110 | -$15.77 |

Breakeven needs 37.9% at the realised win/loss sizes. Expectancy was not
"about zero" as the 09-10 diagnosis read — it was clearly negative and getting
worse. Scaling size would have multiplied the loss rate, not the profit:
expected time to zero on $609 at max aggression was 1-2 trading days.

Win rate is FLAT across every expiry bucket (0 DTE 33.3%, 1 DTE 34.5%,
3-7 DTE 36.4%). A flat win rate everywhere is the signature of no edge, not a
cost problem in one bucket. `min_days_to_expiry` was therefore left at 0 —
0 DTE was the least-bad bucket at -$1.01 EV, so punch-list item 7 stands and
is NOT re-opened.

### The direction bias — the one real finding

From `shadow_resolved.jsonl`, 8,115 scored signals with measured forward
underlying returns:

|  | n | 60m hit | mean 60m move |
|---|---|---|---|
| CALL | 4192 | 46.3% | -0.0805% |
| PUT | 3898 | 51.8% | +0.0952% |

z = +4.94, p < 0.00001. PUTs were the only side with a positive edge, and the
bot placed 28 of its last 29 orders as CALLs.

Cause, confirmed: `_market_regime()` is a DAILY measure (SPY > EMA20 > EMA50
on daily bars) and returned "bull" for 93.5% of signals. `crash_mode` mapped
bull to calls-only. So a daily trend reading was vetoing intraday 30-60 minute
PUT signals — a timescale mismatch. 783 bull-regime PUT signals were discarded
with a +0.102% mean forward move, the best cohort in the dataset.

Corollary worth keeping in mind: CALLs have a significantly NEGATIVE edge, so
the 3/3 confluence of RSI + MACD + volume reads as a momentum-EXHAUSTION
detector that works contrarian. Not acted on; noted.

### The exit geometry — why 33% and not 46%

`agent._dynamic_tp_sl()` hardcoded TP 38/42/45 and SL 25 while `config.py`
said 38/38 and was never read. With `entry_limit_pad_pct` at 3.0 and a 4.7%
median bid/ask, a filled position marked ~7.7% under water immediately. So the
real room before the stop was 17.3% while the target needed 50.7% — a 2.93x
distance asymmetry against a 1.72x payoff. The trade was upside-down before
any forecasting skill applied. Measured losses averaged -28.7% against the
-25% stop, with 72% finishing worse than it: the stop sits inside the noise.

Tight stops nonetheless WIN here, monotonically (SL 25 -$7.21, SL 38 -$9.98,
SL 45 -$11.06 per contract). A wider stop converts losers into TIME EXITS
(44.8% -> 58.1% of trades), and a time exit on a 0-2 DTE contract pays a full
~6% of premium in decay. Do not "give trades more room."

### Changes made

1. `crash_mode.py` — bull regime no longer blocks PUTs. New key
   `bull_allow_puts` (default True) restores the old behaviour if needed.
2. `signal_engine.get_top_signals()` — now accepts and honours `overrides`.
   It had hardcoded "bear blocks CALL, bull blocks PUT", which matched NEITHER
   branch for regime=="crash", so a sync-fallback cycle during a selloff
   permitted CALLS — exactly what crash mode exists to forbid, reachable
   precisely when the data layer degrades. `agent._regime_and_signals()` was
   computing `ov` and discarding it at the call site; it now passes it,
   including the crash-raised `min_signal_score`.
3. `entry_limit_pad_pct` 3.0 -> 0.0. The limit sat 3% ABOVE the ask. A limit
   AT the ask already crosses and fills; the pad bought nothing and gave away
   ~40% of the gross edge on every entry. `fill_timeout_secs` already cancels
   unfilled entries, so the risk is a missed fill, not a resting order.
4. Four dead config keys wired up: `daily_loss_limit` (was ignored; real limit
   was balance*0.14 = $85.32 while the file said $75 — now the tighter of the
   two wins, with `daily_loss_pct` added), `max_concurrent_positions` (was a
   hardcoded floor of 3; config said 2), `take_profit_pct` and `stop_loss_pct`
   (were hardcoded). `max_position_pct` and `max_spread_pct` were read via
   `.get()` with invisible defaults and are now stated explicitly.
   stop_loss_pct is set to 25, NOT the file's old 38, so wiring the key up
   changes no behaviour — and 25 is what the data prefers anyway.
5. Risk checks now fail CLOSED. `get_open_position_count()` returned 0 from a
   bare `except:`, so any transient Tradier error reported "flat" and the
   max-positions check passed unconditionally. It returns -1 (unknown) and
   `can_trade()` refuses to enter blind. `get_open_tickers()` returned an
   empty set the same way, which disabled the duplicate-position guard; it
   returns None and the cycle skips entries. Tradier's literal "null" for a
   flat account is still read as a true zero.
6. `max_spread_pct` 0.35 -> 0.15. Across 183 live orders the spread was median
   4.7%, p90 18.2%, and NOT ONE exceeded 35% — the gate never bound.
7. Spread recap bug: `trade["total_cost"] = _capped * trade["ask"] * 100` used
   the long leg's ask instead of the debit, inflating a vertical's cost so it
   failed capital checks on a cost it never incurred. On a $609 account the
   15% cap binds on nearly every order, so this ran nearly every time a spread
   was tried. Candidate answer to the standing open question about why only
   single-leg symbols have ever filled.

### Modelled effect

| scenario | win rate | EV/trade |
|---|---|---|
| baseline | 33.6% | -$6.57 |
| + direction fix | 37.1% | -$3.19 |
| + entry pad removed | 41.0% | **+$0.55** |

The baseline reproduces the realised -$6.56 to within a cent, which is why the
counterfactuals are worth anything. But +$0.55 is MARGINAL and inside model
error. The correct reading is "roughly breakeven," not "profitable."

### New tools

- `edge_study.py` — direction-gate policy comparison over the shadow record.
  Fits one free parameter (the breakeven underlying move) to the realised win
  rate, then applies it unchanged to counterfactual policies.
- `exit_study.py` — TP/SL and entry-friction grid search. Models theta
  (~6% of premium per 60m hold) and stop slippage, and approximates path
  dependence from the 30m and 60m marks. Its absolute EV is MORE pessimistic
  than reality (-$9.04 vs -$2.30 per contract), so use it for ranking and
  structure, not as a P&L forecast. It prints this caveat itself.

Run both after any change to the gates, exits or friction settings.

### Still open

- Delta targeting is not achieving its target: realised median |delta| is
  0.288 against `contract_target_delta` 0.45, with 56% of fills below 0.30.
  v5.16 was meant to fix the strike-proximity bias; the data may predate it.
  Worth re-checking against fills since 09-10.
- RESOLVED in v5.23: `_past_flatten_time()` clock is injectable (`now_et`).
- `recovery_enabled: False` was added to `regime_state.py` on 10-20 (not by
  this pass). It makes CRASH exit straight to NORMAL with no stand-down, and
  it is what the two failing regime-state tests are asserting against.

## v5.23 — 2026-10-20 (evening): bugs that undid or bypassed v5.22

1. `RiskManager.can_trade()` still hard-blocked "bull + PUT" (and "bear + CALL")
   off the raw daily regime, so v5.22's `bull_allow_puts` never reached an order.
   Removed; the overrides are now the only direction gate.
2. The 15% equity cap now bounds the contract SEARCH (`max_cost` on
   `select_contract` / `select_spread` / `select_deep_itm`), and an over-cap
   candidate moves to the next signal. Before, the selector aimed under
   `max_trade_size`, the cap rejected the pick after the paid TradeJudge call, and
   the cycle ended. Likely cause of the 0.29 realised median delta.
3. `sl_hint` (28-40% stops on cheap contracts) no longer overrides `stop_loss_pct`.
   Gated behind `use_sl_hint`, default False.
4. `_await_fill()` cancels the unfilled remainder of a partial fill, and keeps
   positions that filled before a cancel or timeout.
5. EOD flatten is anchored on Tradier's real close (`session_close_minutes`), so
   on early-close days 15:45 becomes 12:45. No new entries inside the window.
   `PositionMonitor.now_et()` is injectable; the flatten tests now pass at any hour.
6. The 30s fast exit monitor only acts while `market_state() == "open"`.

7. `regime_align_bonus` (config, default 2.0 = unchanged) replaces the hardcoded
   +2 for bull+CALL / bear+PUT. Every scored signal now logs `regime_bonus` in
   the shadow file. After ~2 weeks run `./venv/bin/python regime_bonus_study.py`;
   it prints a verdict once there are 100+ marginal signals.
8. `contract_target_delta` 0.45 -> 0.25. Realised (113 trades): |delta| >= 0.30
   won 8/42 (19%, -$709) vs 30/71 (42%, +$250) below, z~2.5. Same direction
   before and since 08-18 and in both the <$1 and >=$1 price bands. Fix 2 made
   0.45 actually reachable, which would have steered into the worst bucket.
9. `trade_results.json` exit records now use the broker cost basis as the entry
   price (was the ask at signal time), so TradeJudge history and the studies
   see the real fill.

Account-size constraint, not a bug: on ~$609 equity the 15% cap is ~$91, so
contracts above ~$0.91 cannot be bought. Realised, contracts under $1 lost in
both periods. Raising max_position_pct is a risk decision for the owner.
