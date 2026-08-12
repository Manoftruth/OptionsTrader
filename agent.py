"""
OptionsAgent - Autonomous High-Volatility Options Trading Agent
Broker: Tradier
Strategy: Maximum aggression - momentum breakouts, volatility plays, 0DTE options
IMPROVEMENTS v4:
1. VIX filter — skip trades when VIX > 25
2. SPY trend alignment — no CALLs on down days, no PUTs on up days
3. Confluence requirement lowered to 2/3 timeframes (more opportunities)
4. Score-based position sizing — higher score = more contracts
5. Tighter stop loss — 25% instead of 33%
6. Time-based exit — force close after 90 minutes of no movement
7. Removed last-30-min theta block — full trading hours
8. Hard capital cap — safe for margin accounts
9. Capital scaling raised to 75% of gains above base
10. SPY/QQQ prioritized on strong regime days

BUG FIXES v4.1:
- FIX 1: pending_close no longer discarded immediately in _close_position
          (was add+discard in same call — guard was completely useless)
- FIX 2: pending_close cleared in check_and_exit once Tradier confirms position gone
- FIX 3: pending_tickers set in run_once prevents same-ticker duplicate in one cycle
- FIX 4: Fast monitor log suppression removed — TP/SL/trailing stops now always logged
"""
import os
import re
import time
import json
import asyncio
import logging
import threading
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from config import CONFIG
from signal_engine import SignalEngine

# ── v5 modules ─────────────────────────────────────────────────────────────────
import crash_mode
import deep_itm
import spreads as spreads_mod
from async_data import AsyncMarketData, AIOHTTP_AVAILABLE
from tradier_data import TradierData
import pytz

class EasternFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        eastern = pytz.timezone("America/New_York")
        ct = datetime.fromtimestamp(record.created, eastern)
        return ct.strftime("%Y-%m-%d %H:%M:%S ET")

_handler_file   = logging.FileHandler("agent.log")
_handler_stream = logging.StreamHandler()
_formatter = EasternFormatter("%(asctime)s [%(levelname)s] %(message)s")
_handler_file.setFormatter(_formatter)
_handler_stream.setFormatter(_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_handler_file, _handler_stream])
log = logging.getLogger("OptionsAgent")


# ── Tradier API Client ─────────────────────────────────────────────────────────
class TradierClient:
    def __init__(self, token: str, sandbox: bool = True):
        self.token = token
        self.base = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

    def _get(self, path: str, params: dict = None):
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict = None):
        r = requests.post(f"{self.base}{path}", headers=self.headers, data=data)
        r.raise_for_status()
        return r.json()

    def get_account_balances(self):
        return self._get(f"/accounts/{CONFIG['account_id']}/balances")

    def get_positions(self):
        return self._get(f"/accounts/{CONFIG['account_id']}/positions")

    def get_quote(self, symbol: str):
        return self._get("/markets/quotes", {"symbols": symbol, "greeks": "true"})

    def get_options_chain(self, symbol: str, expiration: str):
        return self._get("/markets/options/chains", {
            "symbol": symbol,
            "expiration": expiration,
            "greeks": "true"
        })

    def get_options_expirations(self, symbol: str):
        return self._get("/markets/options/expirations", {"symbol": symbol})

    def place_order(self, symbol: str, option_symbol: str, side: str, quantity: int,
                    order_type: str = "market", price: float = None):
        data = {
            "class": "option",
            "symbol": symbol,
            "option_symbol": option_symbol,
            "side": side,
            "quantity": str(quantity),
            "type": order_type,
            "duration": "day",
        }
        if order_type == "limit" and price:
            data["price"] = str(round(price, 2))
        return self._post(f"/accounts/{CONFIG['account_id']}/orders", data)

    def place_multileg_order(self, payload: dict):
        """Submit a Tradier class=multileg order (vertical spreads).

        Requires options approval **level 3**. A level-2 account gets an
        error back from this endpoint; the caller is responsible for falling
        back to a single-leg trade rather than retrying.
        """
        return self._post(f"/accounts/{CONFIG['account_id']}/orders", payload)

    def get_quotes(self, symbols: list) -> dict:
        """Batch quote lookup — one request instead of one per symbol."""
        if not symbols:
            return {}
        data = self._get("/markets/quotes",
                         {"symbols": ",".join(symbols), "greeks": "true"})
        quotes = data.get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        return {q.get("symbol"): q for q in quotes if q.get("symbol")}

    def cancel_order(self, order_id: str):
        return requests.delete(
            f"{self.base}/accounts/{CONFIG['account_id']}/orders/{order_id}",
            headers=self.headers
        ).json()

    def get_orders(self):
        return self._get(f"/accounts/{CONFIG['account_id']}/orders")

    def get_order(self, order_id) -> dict:
        """Single order detail — includes status and reason_description.

        Submission is not execution. Tradier returns {'status':'ok'} with an
        order id for anything it accepts into the queue; the order can still be
        rejected a second later by the risk layer. This is how you find out.
        """
        try:
            resp = self._get(f"/accounts/{CONFIG['account_id']}/orders/{order_id}")
            return (resp or {}).get("order", {}) or {}
        except Exception as e:
            log.warning(f"Could not fetch order {order_id}: {e}")
            return {}


# ── VIX / SPY Market Filters ───────────────────────────────────────────────────
def get_vix() -> float:
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d", interval="5m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except:
        pass
    return 0.0

def get_spy_day_change_pct() -> float:
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="2d", interval="1d")
        if len(hist) >= 2:
            prev_close = float(hist["Close"].iloc[-2])
            cur_close = float(hist["Close"].iloc[-1])
            return (cur_close - prev_close) / prev_close * 100
    except:
        pass
    return 0.0


# ── Options Selector ───────────────────────────────────────────────────────────
class OptionsSelector:
    def __init__(self, client: TradierClient):
        self.client = client

    def get_nearest_expiry(self, ticker: str, days_out: int = 1) -> Optional[str]:
        try:
            resp = self.client.get_options_expirations(ticker)
            if not resp or not isinstance(resp, dict):
                return None
            expirations = resp.get("expirations", {}).get("date", [])
            if not expirations:
                return None
            today = datetime.now().date()
            for exp in sorted(expirations):
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                if (exp_date - today).days >= days_out:
                    return exp
        except Exception as e:
            log.warning(f"Expiry error for {ticker}: {e}")
        return None

    def select_deep_itm(self, signal: dict, capital: float,
                        overrides: dict | None = None) -> Optional[dict]:
        """Level-2 downside structure: a deep ITM CALL on an inverse ETF.

        Two translations happen here.

        **Direction.** A bearish signal on SPY/QQQ becomes a *call* on an
        inverse ETF. Buying a call is options level 2; a put spread is level
        3. The economics of "long SQQQ call" and "long QQQ put" are close
        enough for this purpose, and the approval requirement is not.

        **Depth.** Rather than a fixed strike offset, ask the chain what the
        budget can actually reach — see deep_itm.build_deep_itm. Searching
        across several expiries matters more than it looks: extrinsic value
        scales with the square root of time, so the same budget that buys a
        0.78-delta contract expiring tomorrow may reach nothing at all on a
        Friday-expiry chain four days out.

        Returns None rather than quietly buying an at-the-money contract. A
        near-the-money option after a vol spike is the exact position this
        whole path exists to avoid.
        """
        ov = overrides or {}
        if signal["direction"] != "PUT":
            return None      # only the bearish side routes through an inverse ETF

        underlyings = CONFIG.get("crash_underlyings", ["SQQQ"])
        max_expiries = int(CONFIG.get("crash_max_expiries_to_scan", 3))
        # Deep ITM contracts cost several times an OTM lottery ticket, so crash
        # mode gets its own per-trade cap. Your normal max_trade_size is left
        # alone — this only applies to the downside structure.
        #
        # crash_max_trade_size is the FINAL budget: it is not multiplied by
        # size_mult or vix_size_mult on top. Choosing a smaller cap IS the
        # size reduction. Applying the multipliers as well would take $250 down
        # to $100, which reaches no qualifying contract at all — the agent would
        # run every cycle and silently never trade. Same double-counting trap as
        # stacking the VIX and regime multipliers.
        explicit = "crash_max_trade_size" in CONFIG
        per_trade = float(CONFIG.get("crash_max_trade_size",
                                     CONFIG.get("max_trade_size", capital)))
        budget = min(capital, per_trade)
        if not explicit:
            budget *= min(float(ov.get("size_mult", 1.0)),
                          float(signal.get("vix_size_mult", 1.0) or 1.0))
        if budget <= 0:
            return None
        log.info(f"  💵 Deep ITM budget: ${budget:.0f} "
                 f"({'crash_max_trade_size' if explicit else 'sized from max_trade_size'})")

        for etf in underlyings:
            try:
                q = self.client.get_quote(etf)
                spot = float(q.get("quotes", {}).get("quote", {}).get("last", 0) or 0)
                if spot <= 0:
                    log.info(f"  ⬜ {etf}: no quote — skipping")
                    continue

                resp = self.client.get_options_expirations(etf)
                dates = resp.get("expirations", {}).get("date", []) if resp else []
                if isinstance(dates, str):
                    dates = [dates]
                today = datetime.now().date()
                usable = [d for d in sorted(dates)
                          if (datetime.strptime(d, "%Y-%m-%d").date() - today).days
                          >= CONFIG.get("min_days_to_expiry", 0)][:max_expiries]

                for exp in usable:
                    chain = self.client.get_options_chain(etf, exp)
                    options = (chain.get("options") or {}).get("option", []) \
                        if isinstance(chain, dict) else []
                    if isinstance(options, dict):
                        options = [options]
                    if not options:
                        continue

                    pick = deep_itm.build_deep_itm(
                        options, spot, "CALL", budget,
                        cfg=CONFIG.get("deep_itm_cfg"))
                    if not pick:
                        continue

                    dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
                    log.info(
                        f"  🔄 Routing {signal['ticker']} PUT signal → "
                        f"{etf} CALL (inverse ETF, level 2). "
                        f"{dte}DTE, {pick['extrinsic_pct']:.0f}% of the premium "
                        f"is volatility value."
                    )
                    if etf != underlyings[0]:
                        log.info(f"  ↩️  Note: {underlyings[0]} had no reachable "
                                 f"structure at ${budget:.0f}; used {etf} instead.")

                    return {
                        "ticker":        etf,
                        "direction":     "CALL",
                        "structure":     "deep_itm",
                        "option_symbol": pick["option_symbol"],
                        "strike":        pick["strike"],
                        "expiry":        exp,
                        "ask":           pick["ask"],
                        "bid":           pick["bid"],
                        "delta":         pick["delta"],
                        "contracts":     pick["contracts"],
                        "total_cost":    pick["total_cost"],
                        "deep_itm":      pick,
                        "source_signal_ticker": signal["ticker"],
                        "signal":        signal,
                    }
            except Exception as e:
                log.warning(f"Deep ITM selection error on {etf}: {e}")

        log.info(f"  ⬜ No deep ITM structure reachable at ${budget:.0f} across "
                 f"{underlyings}. Raise max_trade_size or add a cheaper inverse "
                 f"ETF to crash_underlyings.")
        return None

    def select_spread(self, signal: dict, capital: float,
                      overrides: dict | None = None) -> Optional[dict]:
        """Build a vertical debit spread for this signal, or None.

        Used when the regime layer sets ``prefer_spreads`` — i.e. implied
        volatility is already elevated, so a naked long option would be
        paying peak vega. Returns a trade dict shaped like select_contract's
        output so the execution path can treat both the same way.
        """
        ov = overrides or {}
        ticker = signal["ticker"]
        direction = signal["direction"]
        spot = signal["price"]

        expiry = self.get_nearest_expiry(ticker, days_out=CONFIG["min_days_to_expiry"])
        if not expiry:
            return None
        try:
            chain = self.client.get_options_chain(ticker, expiry)
            options = (chain.get("options") or {}).get("option", []) if isinstance(chain, dict) else []
            if isinstance(options, dict):
                options = [options]
            if not options:
                return None

            # Same rule as select_contract: tighter of the two, never the
            # product. A vertical's max loss is exactly the debit paid, so
            # max_trade_size is already the correct risk unit here.
            budget = min(capital, CONFIG.get("max_trade_size", capital)) * min(
                float(ov.get("size_mult", 1.0)),
                float(signal.get("vix_size_mult", 1.0) or 1.0),
            )
            if budget <= 0:
                return None

            spread = spreads_mod.build_debit_spread(
                options, spot, direction, budget,
                atr=float(signal.get("atr", 0) or 0),
                cfg=CONFIG.get("spread_cfg"),
            )
            if not spread:
                return None

            return {
                "ticker":        ticker,
                "direction":     direction,
                "structure":     "vertical_debit",
                "option_symbol": spread["long_symbol"],   # long leg, for logging
                "spread":        spread,
                "strike":        spread["long_strike"],
                "expiry":        expiry,
                "ask":           spread["debit"],
                "bid":           spread["debit"],
                "contracts":     spread["contracts"],
                "total_cost":    spread["total_cost"],
                "signal":        signal,
            }
        except Exception as e:
            log.warning(f"Spread selection error for {ticker}: {e}")
            return None

    def select_contract(self, signal: dict, capital: float,
                        overrides: dict | None = None) -> Optional[dict]:
        ov = overrides or {}
        ticker = signal["ticker"]
        direction = signal["direction"]
        current_price = signal["price"]

        strike_offset = 1.02 if direction == "CALL" else 0.98
        target_strike = round(current_price * strike_offset / 5) * 5

        expiry = self.get_nearest_expiry(ticker, days_out=CONFIG["min_days_to_expiry"])
        if not expiry:
            log.warning(f"No expiry found for {ticker}")
            return None

        # v5: premium ceiling is regime-aware. A flat $3.00 cap silently
        # rejected every put once IV expanded, which is why the agent found
        # "no suitable contract" on exactly the days it should have traded.
        max_price = float(ov.get("max_contract_price",
                                 CONFIG["max_contract_price"]))

        try:
            chain = self.client.get_options_chain(ticker, expiry)
            if not chain:
                return None
            options_data = chain.get("options") if isinstance(chain, dict) else None
            if not options_data:
                return None
            options = options_data.get("option", [])
            if not options:
                return None
            if isinstance(options, dict):
                options = [options]

            side_options = [o for o in options if o.get("option_type", "").lower() == direction.lower()]
            if not side_options:
                return None

            best = None
            best_diff = float("inf")
            for opt in side_options:
                try:
                    strike = float(opt.get("strike", 0))
                    ask    = float(opt.get("ask", 0))
                    bid    = float(opt.get("bid", 0))
                    delta  = abs(float(opt.get("greeks", {}).get("delta", 0) or 0))

                    if ask <= 0 or ask < CONFIG.get("min_contract_price", 0.20) or ask > max_price:
                        continue
                    # Only apply delta filter if greeks look valid
                    if delta > 0.01:
                        if delta < 0.20 or delta > 0.70:
                            continue
                    else:
                        # No valid delta — use strike proximity as proxy
                        # Accept strikes within 5% of current price
                        price_diff_pct = abs(strike - current_price) / current_price
                        if price_diff_pct > 0.05:
                            continue

                    mid = (ask + bid) / 2
                    if mid > 0:
                        spread_pct = (ask - bid) / mid
                        if spread_pct > 0.35:
                            continue

                    diff = abs(strike - target_strike)
                    if diff < best_diff:
                        best_diff = diff
                        best = opt
                except:
                    continue

            if not best:
                log.warning(f"No contract found for {ticker} — checked {len(side_options)} {direction} options. Reasons: "
                            f"ask<=0 or >max_price, delta out of 0.20-0.70 range, or spread >10%")
                # Debug: show why top candidates were rejected
                for opt in side_options[:3]:
                    try:
                        ask   = float(opt.get("ask", 0))
                        bid   = float(opt.get("bid", 0))
                        delta = abs(float(opt.get("greeks", {}).get("delta", 0)))
                        mid   = (ask + bid) / 2 if (ask + bid) > 0 else 1
                        spread_pct = (ask - bid) / mid
                        log.warning(f"  Rejected {opt.get('symbol','?')}: ask=${ask:.2f} delta={delta:.2f} spread={spread_pct*100:.1f}% max_price=${CONFIG['max_contract_price']}")
                    except:
                        pass
                return None

            ask_price = float(best["ask"])

            # ── Score-based position sizing ──
            score = signal.get("score", 13)
            confluence = signal.get("confluence", 0)

            if score >= 17 and confluence >= 4:
                size_multiplier = 1.0       # max size — all 3 TFs agree + very high score
            elif score >= 16:
                size_multiplier = 0.85
            elif score >= 15.5:
                size_multiplier = 0.70
            else:
                size_multiplier = 0.55      # half size for borderline signals

            # v5 FIX: vix_size_mult was computed by the signal engine on every
            # cycle and then never applied to anything. Volatility-scaled
            # sizing only exists if you actually multiply by it.
            #
            # Take the TIGHTER of the two, do not compound them. Both the VIX
            # ladder and the regime multiplier are proxies for the same thing —
            # market stress — so multiplying them double-counts it. In crash
            # mode that product is 0.45 x 0.40 = 0.18x, which on a $125 per-trade
            # cap leaves $22 of budget and silently rejects every contract. The
            # agent would look like it was running while never trading.
            vix_mult = float(signal.get("vix_size_mult", 1.0) or 1.0)
            regime_mult = float(ov.get("size_mult", 1.0))
            size_multiplier *= min(vix_mult, regime_mult)

            max_spend = min(capital, CONFIG.get("max_trade_size", capital)) * size_multiplier
            if max_spend < ask_price * 100:
                log.info(f"  ⬜ {ticker}: sized budget ${max_spend:.2f} < one contract "
                         f"${ask_price * 100:.2f} (vix {vix_mult:.2f}x, regime {regime_mult:.2f}x)")
                return None
            contracts = max(1, int(max_spend / (ask_price * 100)))
            total_cost = contracts * ask_price * 100

            if total_cost > capital:
                contracts = max(1, contracts - 1)
                total_cost = contracts * ask_price * 100

            return {
                "ticker": ticker,
                "direction": direction,
                "structure": "single",
                "option_symbol": best["symbol"],
                "strike": float(best["strike"]),
                "expiry": expiry,
                "ask": ask_price,
                "bid": float(best.get("bid", 0)),
                "delta": best.get("greeks", {}).get("delta"),
                "contracts": contracts,
                "total_cost": total_cost,
                "signal": signal
            }
        except Exception as e:
            log.warning(f"Contract selection error for {ticker}: {e}")
            return None


# ── Risk Manager ───────────────────────────────────────────────────────────────
class RiskManager:
    """
    Enforces hard limits:
    - Max capital deployed at once (scales with 75% of gains)
    - Max concurrent positions
    - Daily loss limit kill switch
    - VIX filter — no trades when VIX > 25
    - SPY trend filter — no CALLs on down days, no PUTs on up days
    - HARD CAP: never exceed capital_limit (margin-safe)
    """

    def __init__(self, client: TradierClient):
        self.client = client
        self._start_of_day_capital: float = None
        self._last_reset_date: str = None
        self._killed_today: bool = False
        self._vix_cache: tuple = (0, 0.0)
        self._spy_cache: tuple = (0, 0.0)

    def _reset_if_new_day(self, current_capital: float):
        today = datetime.now().date().isoformat()
        if self._last_reset_date != today:
            self._start_of_day_capital = current_capital
            self._last_reset_date = today
            self._killed_today = False
            log.info(f"New trading day. Starting capital: ${current_capital:.2f} | "
                     f"Daily loss limit: ${self._dynamic_daily_loss_limit():.2f}")

    def _get_vix_cached(self) -> float:
        now = time.time()
        if now - self._vix_cache[0] > 300:
            self._vix_cache = (now, get_vix())
        return self._vix_cache[1]

    def _get_spy_cached(self) -> float:
        now = time.time()
        if now - self._spy_cache[0] > 300:
            self._spy_cache = (now, get_spy_day_change_pct())
        return self._spy_cache[1]

    def get_available_capital(self) -> float:
        if CONFIG.get("sandbox", True):
            return float(CONFIG["capital_limit"])
        try:
            bal = self.client.get_account_balances()
            balances = bal.get("balances", {})
            if isinstance(balances, dict):
                cash = balances.get("cash", {})
                if isinstance(cash, dict):
                    raw = float(cash.get("cash_available", CONFIG["capital_limit"]))
                    return min(raw, float(CONFIG["capital_limit"]))  # HARD CAP
                total = balances.get("total_cash", balances.get("cash_available", 0))
                return min(float(total), float(CONFIG["capital_limit"]))  # HARD CAP
            return float(CONFIG["capital_limit"])
        except Exception as e:
            log.error(f"Balance fetch error: {e}")
            return float(CONFIG["capital_limit"])

    def get_account_balance(self) -> float:
        """Real account equity for P&L and kill switch tracking — NOT capped.
        Cap only applies to get_available_capital() to prevent margin usage."""
        if CONFIG.get("sandbox", True):
            return float(CONFIG["capital_limit"])
        try:
            bal = self.client.get_account_balances()
            balances = bal.get("balances", {})
            if isinstance(balances, dict):
                total = balances.get("total_equity",
                        balances.get("total_cash",
                        balances.get("cash_available", CONFIG["capital_limit"])))
                return float(total)  # NO CAP — real balance for accurate P&L tracking
            return float(CONFIG["capital_limit"])
        except Exception as e:
            log.error(f"Balance fetch error: {e}")
            return float(CONFIG["capital_limit"])

    def _dynamic_capital_limit(self) -> float:
        """Base capital + 75% of gains above initial capital (raised from 50%)."""
        base = float(CONFIG["capital_limit"])
        balance = self.get_account_balance()
        gains = max(0, balance - base)
        return base + (gains * 0.75)

    def _dynamic_max_positions(self) -> int:
        balance = self.get_account_balance()
        if balance >= 5000:
            return 5
        elif balance >= 2500:
            return 4
        else:
            return 3

    def _dynamic_daily_loss_limit(self) -> float:
        balance = self.get_account_balance()
        return round(balance * 0.14, 2)

    def get_open_position_count(self) -> int:
        try:
            pos = self.client.get_positions()
            if not isinstance(pos, dict):
                return 0
            positions = pos.get("positions", {}).get("position", [])
            if isinstance(positions, dict):
                positions = [positions]
            return len(positions)
        except:
            return 0

    def get_open_tickers(self) -> set:
        try:
            pos = self.client.get_positions()
            if not isinstance(pos, dict):
                return set()
            positions = pos.get("positions", {}).get("position", [])
            if isinstance(positions, dict):
                positions = [positions]
            tickers = set()
            for p in positions:
                symbol = p.get("symbol", "")
                m = re.match(r'^([A-Z]+)', symbol)
                if m:
                    tickers.add(m.group(1))
            return tickers
        except:
            return set()

    def check_daily_loss_limit(self, current_capital: float) -> tuple[bool, str]:
        portfolio_value = self.get_account_balance()
        if portfolio_value == float(CONFIG["capital_limit"]) and CONFIG.get("sandbox", True):
            portfolio_value = current_capital

        self._reset_if_new_day(portfolio_value)

        if self._killed_today:
            return True, "KILL SWITCH ACTIVE — agent shut down for today."

        if self._start_of_day_capital is None:
            return False, "OK"

        daily_loss = self._start_of_day_capital - portfolio_value
        daily_loss_pct = (daily_loss / self._start_of_day_capital * 100) if self._start_of_day_capital > 0 else 0

        if daily_loss >= self._dynamic_daily_loss_limit():
            self._killed_today = True
            msg = (f"KILL SWITCH FIRED — portfolio down ${daily_loss:.2f} "
                   f"({daily_loss_pct:.1f}%) from day start. "
                   f"No more trades today. (limit: ${self._dynamic_daily_loss_limit():.2f})")
            log.warning(f"🔴 {msg}")
            return True, msg

        limit = self._dynamic_daily_loss_limit()
        remaining = limit - daily_loss
        pnl = -daily_loss
        pnl_pct = -daily_loss_pct
        log.info(f"🛡️  Daily P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
                 f"Limit: ${limit:.2f} | ${remaining:.2f} remaining before kill switch fires")
        return False, "OK"

    def can_trade(self, trade: dict, available_capital: float, regime: str = "neutral",
                  overrides: dict | None = None) -> tuple[bool, str]:
        ov = overrides or {}
        killed, reason = self.check_daily_loss_limit(available_capital)
        if killed:
            return False, reason

        if available_capital < CONFIG["min_capital_to_trade"]:
            return False, f"Insufficient capital: ${available_capital:.2f}"

        open_positions = self.get_open_position_count()
        if open_positions >= self._dynamic_max_positions():
            return False, f"Max positions reached ({open_positions})"

        if trade["total_cost"] > available_capital * 1.01:  # 1% buffer for settlement rounding
            return False, f"Trade cost ${trade['total_cost']:.2f} > available ${available_capital:.2f}"

        if trade["total_cost"] > self._dynamic_capital_limit():
            return False, f"Trade exceeds dynamic capital limit ${self._dynamic_capital_limit():.2f}"

        direction = trade.get("signal", {}).get("direction", "")
        score = trade.get("signal", {}).get("score", 0)

        # ── VIX filter ──
        # v5: the ceiling comes from the regime overrides. In crash mode it is
        # raised to ~70 so the agent stops disabling itself in high vol; the
        # protection moves to smaller size + defined-risk spreads instead of a
        # blanket "don't trade".
        max_vix = float(ov.get("max_vix", CONFIG.get("max_vix", 28)))
        vix = self._get_vix_cached()
        if vix > max_vix:
            log.info(f"⚠️  VIX={vix:.1f} > {max_vix} — skipping trade")
            return False, f"VIX too high ({vix:.1f})"

        # ── SPY trend alignment ──
        # In crash mode SPY is down hard by definition, so the "no PUTs on an
        # up day" rule is kept but the "no CALLs on a down day" rule is
        # redundant with the direction gate.
        spy_chg = self._get_spy_cached()
        if spy_chg < -1.0 and direction == "CALL":
            return False, f"Blocked CALL — SPY down {spy_chg:.1f}% today"
        if spy_chg > 1.0 and direction == "PUT" and regime != "crash":
            return False, f"Blocked PUT — SPY up {spy_chg:.1f}% today"

        # ── Regime filter ──
        if not ov.get("allow_calls", True) and direction == "CALL":
            return False, f"Hard block: CALL in {regime.upper()} regime"
        if not ov.get("allow_puts", True) and direction == "PUT":
            return False, f"Hard block: PUT in {regime.upper()} regime"
        if regime == "bear" and direction == "CALL":
            return False, f"Hard block: CALL in BEAR regime"
        if regime == "bull" and direction == "PUT":
            return False, f"Hard block: PUT in BULL regime"

        # ── Defined-risk sanity check for spreads ──
        # A vertical's maximum loss is the debit paid. If that somehow exceeds
        # the per-trade cap, something upstream miscalculated — refuse.
        sp = trade.get("spread")
        if sp and sp.get("max_loss", 0) > CONFIG.get("max_trade_size", 1e9) * 1.5:
            return False, (f"Spread max loss ${sp['max_loss']:.0f} exceeds "
                           f"1.5x max_trade_size")

        return True, "OK"


# ── Position Monitor ───────────────────────────────────────────────────────────
class PositionMonitor:
    def __init__(self, client: TradierClient):
        self.client = client
        self.entry_prices: dict = {}
        self.peak_prices: dict = {}
        self.entry_times: dict = {}
        self.recently_closed: set = set()
        self.pending_close: set = set()         # symbols with close order placed, waiting for Tradier fill
        self.pending_close_times: dict = {}     # symbol → timestamp when close order was placed
        self.pending_close_order_ids: dict = {} # symbol → Tradier order ID of the close order
        self.daily_realized_pnl: float = 0.0
        self.time_extended: set = set()   # symbols that have already had time extension
        # v5: open vertical spreads, keyed by long-leg symbol. Tracked as one
        # unit — a spread's P&L is meaningless leg by leg.
        self.spread_positions: dict = {}
        # v5: check_and_exit is called from BOTH the main cycle and the 30s
        # fast-monitor thread. Without this, two threads can read the same
        # position, both decide to close it, and both submit a sell — leaving
        # you short. The re-entrant lock makes the whole exit pass atomic.
        self._lock = threading.RLock()
        self._load_entry_prices()

    def _past_flatten_time(self) -> bool:
        """True once we are inside the end-of-day flatten window."""
        if not CONFIG.get("flatten_eod", True):
            return False
        try:
            hh, mm = str(CONFIG.get("close_all_by_et", "15:45")).split(":")
            now = datetime.now(pytz.timezone("America/New_York"))
            if now.weekday() >= 5:
                return False
            return (now.hour * 60 + now.minute) >= (int(hh) * 60 + int(mm)) \
                and now.hour < 16
        except Exception:
            return False

    def _entry_prices_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "entry_prices.json")

    def _load_entry_prices(self):
        try:
            path = self._entry_prices_path()
            if os.path.exists(path):
                with open(path, "r") as f:
                    self.entry_prices = json.load(f)
                log.info(f"Loaded {len([k for k in self.entry_prices if not k.endswith('_score') and not k.endswith('_time')])} entry price records from disk")
                for k, v in self.entry_prices.items():
                    if k.endswith("_time"):
                        symbol = k[:-5]
                        try:
                            self.entry_times[symbol] = datetime.fromisoformat(v)
                        except:
                            pass
        except Exception as e:
            log.warning(f"Could not load entry prices: {e}")
        try:
            path = self._entry_prices_path()
            if os.path.exists(path + ".peaks"):
                with open(path + ".peaks") as f:
                    self.peak_prices = json.load(f)
                log.info(f"Loaded {len(self.peak_prices)} peak price records from disk")
        except Exception as e:
            log.warning(f"Could not load peak prices: {e}")
        try:
            path = self._entry_prices_path() + ".spreads"
            if os.path.exists(path):
                with open(path) as f:
                    self.spread_positions = json.load(f)
                log.info(f"Loaded {len(self.spread_positions)} open spread record(s) from disk")
        except Exception as e:
            log.warning(f"Could not load spread positions: {e}")

    def _save_entry_prices(self):
        try:
            path = self._entry_prices_path()
            with open(path, "w") as f:
                json.dump(self.entry_prices, f)
            with open(path + ".peaks", "w") as f:
                json.dump(self.peak_prices, f)
            with open(path + ".spreads", "w") as f:
                json.dump(self.spread_positions, f)
        except Exception as e:
            log.warning(f"Could not save entry prices: {e}")

    def record_entry(self, option_symbol: str, entry_price: float, score: float = 13,
                     tp_sl_override: dict = None, spread: dict = None):
        with self._lock:
            self.entry_prices[option_symbol] = entry_price
            self.entry_prices[option_symbol + "_score"] = score
            self.entry_prices[option_symbol + "_time"] = datetime.now().isoformat()
            if tp_sl_override:
                self.entry_prices[option_symbol + "_tp"] = tp_sl_override.get("tp")
                self.entry_prices[option_symbol + "_sl"] = tp_sl_override.get("sl")
                log.info(f"📌 Custom TP/SL for {option_symbol}: TP={tp_sl_override.get('tp')}% SL={tp_sl_override.get('sl')}%")
            if spread:
                self.spread_positions[option_symbol] = {
                    **spread,
                    "entry_debit": entry_price,
                    "opened_at": datetime.now().isoformat(),
                }
                log.info(f"📌 Tracking spread {spread['long_strike']}/{spread['short_strike']} "
                         f"as one unit — max loss ${spread['max_loss']:.0f}, "
                         f"max profit ${spread['max_profit']:.0f}")
            self.peak_prices[option_symbol] = entry_price
            self.entry_times[option_symbol] = datetime.now()
            self._save_entry_prices()

    def _dynamic_tp_sl(self, signal_score: float, option_symbol: str = None) -> tuple:
        # Check for TradeJudge override first
        if option_symbol:
            tp = self.entry_prices.get(option_symbol + "_tp")
            sl = self.entry_prices.get(option_symbol + "_sl")
            if tp or sl:
                tp = tp or (45.0 if signal_score >= 16 else 42.0)
                sl = sl or 25.0
                return float(tp), float(sl)
        if signal_score >= 16:
            return 45.0, 25.0
        elif signal_score >= 14:
            return 42.0, 25.0
        else:
            return 38.0, 25.0

    def check_and_exit(self):
        """Thread-safe entry point. Held for the whole pass, deliberately.

        The main cycle and the 30s fast-monitor thread both call this. Locking
        per-mutation would still let two threads each read "P&L is -30%, close
        it" and each submit a sell. Locking the whole pass means only one
        thread ever evaluates and acts on a given position state.
        """
        with self._lock:
            self._check_spread_exits()
            return self._check_and_exit_impl()

    # ── Spread exits (v5) ──────────────────────────────────────────────────
    def _check_spread_exits(self):
        """Evaluate open verticals as single units.

        A spread's P&L is (long bid - short ask) versus the debit paid.
        Marking the legs independently is meaningless and will trip a stop on
        the long leg while the short leg is quietly offsetting it.
        """
        if not self.spread_positions:
            return
        for long_sym, sp in list(self.spread_positions.items()):
            try:
                short_sym = sp.get("short_symbol")
                if not short_sym:
                    continue
                quotes = self.client.get_quotes([long_sym, short_sym])
                lq, sq = quotes.get(long_sym), quotes.get(short_sym)
                if not lq or not sq:
                    log.info(f"⏳ Spread {long_sym}: quotes unavailable this pass")
                    continue

                mark = spreads_mod.spread_mark(lq, sq)
                if not mark["valid"]:
                    continue

                entry_debit = float(sp.get("entry_debit", sp.get("debit", 0)))
                pnl_pct = spreads_mod.spread_pnl_pct(entry_debit, mark)

                width = float(sp.get("width", 0))
                # Max value of a vertical is its width. Taking profit at ~70%
                # of the theoretical maximum avoids fighting for the last few
                # cents, which is where the bid/ask eats the whole edge.
                max_gain_pct = ((width - entry_debit) / entry_debit * 100) if entry_debit else 0
                tp_pct = float(CONFIG.get("spread_take_profit_pct",
                                          max(35.0, max_gain_pct * 0.70)))
                sl_pct = float(CONFIG.get("spread_stop_loss_pct", 55.0))

                peak = self.peak_prices.get(long_sym, entry_debit)
                if mark["exit_value"] > peak:
                    self.peak_prices[long_sym] = mark["exit_value"]

                if pnl_pct >= tp_pct:
                    log.info(f"✅ SPREAD TAKE PROFIT: {long_sym} {pnl_pct:+.1f}% "
                             f"(target +{tp_pct:.0f}%)")
                    self._close_spread(long_sym, sp, mark)
                elif pnl_pct <= -sl_pct:
                    log.info(f"🛑 SPREAD STOP: {long_sym} {pnl_pct:+.1f}% "
                             f"(stop -{sl_pct:.0f}%)")
                    self._close_spread(long_sym, sp, mark)
                else:
                    log.info(f"Spread {sp.get('long_strike')}/{sp.get('short_strike')} "
                             f"{sp.get('direction')}: {pnl_pct:+.1f}% "
                             f"(value ${mark['exit_value']:.2f} vs debit ${entry_debit:.2f} "
                             f"| TP +{tp_pct:.0f}% SL -{sl_pct:.0f}%)")
            except Exception as e:
                log.error(f"Spread monitor error {long_sym}: {e}")

    def _close_spread(self, long_sym: str, sp: dict, mark: dict):
        ticker = sp.get("underlying") or (re.match(r'^([A-Z]+)', long_sym).group(1)
                                          if re.match(r'^([A-Z]+)', long_sym) else long_sym[:6])
        try:
            # Cross the spread slightly to actually get out. A vertical that
            # will not fill is a vertical you still own.
            limit = max(0.01, mark["exit_value"] * 0.95)
            payload = spreads_mod.close_spread_payload(ticker, sp, limit)
            result = self.client.place_multileg_order(payload)
            entry_debit = float(sp.get("entry_debit", sp.get("debit", 0)))
            qty = int(sp.get("contracts", 1))
            pnl = (mark["exit_value"] - entry_debit) * 100 * qty
            self.daily_realized_pnl += pnl
            log.info(f"{'💰' if pnl >= 0 else '💸'} SPREAD CLOSED: {ticker} "
                     f"{sp.get('long_strike')}/{sp.get('short_strike')} | "
                     f"${pnl:+.2f} | Day total: ${self.daily_realized_pnl:+.2f}")
            log.info(f"Close order: {result}")
            self._write_trade_result(long_sym, ticker, qty, entry_debit,
                                     mark["exit_value"],
                                     spreads_mod.spread_pnl_pct(entry_debit, mark), pnl)
            self.spread_positions.pop(long_sym, None)
            self.peak_prices.pop(long_sym, None)
            self.entry_times.pop(long_sym, None)
            self.entry_prices.pop(long_sym, None)
            self.entry_prices.pop(long_sym + "_score", None)
            self.entry_prices.pop(long_sym + "_time", None)
            self._save_entry_prices()
            self.recently_closed.add(ticker)
        except Exception as e:
            log.error(f"❌ Failed to close spread {long_sym}: {e} — "
                      f"MANUAL INTERVENTION MAY BE REQUIRED (short leg still open)")

    def _spread_leg_symbols(self) -> set:
        legs = set()
        for long_sym, sp in self.spread_positions.items():
            legs.add(long_sym)
            if sp.get("short_symbol"):
                legs.add(sp["short_symbol"])
        return legs

    def _check_and_exit_impl(self):
        spread_legs = self._spread_leg_symbols()
        try:
            pos_resp = self.client.get_positions()
            if not isinstance(pos_resp, dict):
                return
            raw_positions = pos_resp.get("positions", None)
            if not raw_positions or raw_positions == "null" or not isinstance(raw_positions, dict):
                # No open positions — clear any stale pending_close entries
                if self.pending_close:
                    log.info(f"✅ All positions closed — clearing pending_close: {self.pending_close}")
                    self.pending_close.clear()
                    self.pending_close_times.clear()
                    self.pending_close_order_ids.clear()
                return
            positions = raw_positions.get("position", [])
            if isinstance(positions, dict):
                positions = [positions]

            # ── FIX 2: Clear pending_close for symbols no longer in Tradier positions ──
            # This means the sell order filled and the position is gone
            current_symbols = {pos.get("symbol", "") for pos in positions}
            confirmed_closed = self.pending_close - current_symbols
            if confirmed_closed:
                log.info(f"✅ Sell confirmed by Tradier (no longer in positions): {confirmed_closed}")
                self.pending_close -= confirmed_closed
                for _sym in confirmed_closed:
                    self.pending_close_times.pop(_sym, None)
                    self.pending_close_order_ids.pop(_sym, None)

            for pos in positions:
                symbol      = pos.get("symbol", "")

                # ── Skip legs belonging to a tracked spread ──
                # _check_spread_exits already evaluated these as a unit. Letting
                # the single-leg logic touch them would sell the long leg on its
                # own and leave a naked short put open.
                if symbol in spread_legs:
                    continue

                # ── Skip if close order pending — wait for fill, or timeout and go market ──
                if symbol in self.pending_close:
                    elapsed = time.time() - self.pending_close_times.get(symbol, time.time())
                    max_wait = CONFIG.get("close_order_timeout_secs", 90)
                    if elapsed < max_wait:
                        log.info(f"⏳ {symbol} — close order pending ({elapsed:.0f}s / {max_wait}s)")
                        continue
                    # Don't cancel/resubmit outside market hours — orders can't fill anyway
                    _now_et = datetime.now(pytz.timezone("America/New_York"))
                    _market_open = (
                        (_now_et.hour == 9 and _now_et.minute >= 30) or
                        (10 <= _now_et.hour <= 15)
                    )
                    if not _market_open:
                        log.info(f"⏳ {symbol} — close order pending ({elapsed:.0f}s), market closed — holding until open")
                        continue
                    log.warning(f"⚠️  CLOSE TIMEOUT: {symbol} unfilled after {elapsed:.0f}s — canceling and going market")
                    order_id = self.pending_close_order_ids.get(symbol)
                    if order_id:
                        try:
                            self.client.cancel_order(order_id)
                            log.info(f"🗑️  Canceled stale close order {order_id} for {symbol}")
                        except Exception as ce:
                            log.warning(f"Could not cancel order {order_id}: {ce} — submitting market order anyway")
                    self.pending_close.discard(symbol)
                    self.pending_close_times.pop(symbol, None)
                    self.pending_close_order_ids.pop(symbol, None)
                    try:
                        quote_resp  = self.client.get_quote(symbol)
                        quotes      = quote_resp.get("quotes", {}).get("quote", {})
                        current_bid = float(quotes.get("bid", 0))
                        if current_bid > 0:
                            m2 = re.match(r'^([A-Z]+)', symbol)
                            tkr = m2.group(1) if m2 else symbol[:6]
                            qty2 = int(pos.get("quantity", 0))
                            mkt_result = self.client.place_order(
                                symbol=tkr,
                                option_symbol=symbol,
                                side="sell_to_close",
                                quantity=qty2,
                                order_type="market"
                            )
                            self.pending_close.add(symbol)
                            self.pending_close_times[symbol] = time.time()
                            new_oid = str(mkt_result.get("order", {}).get("id", ""))
                            if new_oid:
                                self.pending_close_order_ids[symbol] = new_oid
                            log.info(f"📤 Market close resubmitted for {symbol}: {mkt_result}")
                        else:
                            log.error(f"❌ Could not get bid for {symbol} — manual intervention needed")
                    except Exception as me:
                        log.error(f"Failed to resubmit market close for {symbol}: {me}")
                    continue

                qty         = int(pos.get("quantity", 0))
                cost_basis  = float(pos.get("cost_basis", 0))
                entry_price = cost_basis / (qty * 100) if qty > 0 else 0

                quote_resp  = self.client.get_quote(symbol)
                quotes      = quote_resp.get("quotes", {}).get("quote", {})
                current_bid = float(quotes.get("bid", 0) or 0)
                current_ask = float(quotes.get("ask", 0) or 0)
                last_px     = float(quotes.get("last", 0) or 0)

                if entry_price <= 0:
                    continue

                # ── v5.6: a zero bid must NOT skip the position ──────────
                # The old code did `if current_bid <= 0: continue`, silently
                # and with no log line. But a bid of zero is not a missing
                # quote — it is a dying option with no buyers, which is
                # exactly the moment the stop loss needs to fire. The
                # position became invisible to every exit rule while it
                # collapsed, then got sold for scraps.
                #
                # This is the mechanism behind the loss tail: a configured
                # 25% stop produced realised losses of -52%, -61%, -72%,
                # -89% and -94% across 146 trades.
                force_market = False
                if current_bid <= 0:
                    force_market = True
                    if last_px > 0:
                        mark, src = last_px, "last"
                    elif current_ask > 0:
                        # No bid at all. Half the ask is a deliberately
                        # pessimistic stand-in — better to over-estimate the
                        # loss and act than to under-estimate it and sit.
                        mark, src = current_ask / 2, "half-ask"
                    else:
                        log.error(f"🚨 {symbol}: NO bid, ask or last — cannot "
                                  f"price this position. CHECK MANUALLY.")
                        continue
                    log.warning(f"⚠️  {symbol}: bid is ZERO — pricing from "
                                f"{src} (${mark:.2f}). Exits will use market "
                                f"orders; a limit at the bid is meaningless "
                                f"when there is no bid.")
                    current_bid = mark

                pnl_pct   = (current_bid - entry_price) / entry_price * 100
                sig_score = self.entry_prices.get(symbol + "_score", 13)
                tp_pct, sl_pct = self._dynamic_tp_sl(sig_score, option_symbol=symbol)

                if symbol not in self.peak_prices:
                    self.peak_prices[symbol] = entry_price
                if current_bid > self.peak_prices[symbol]:
                    self.peak_prices[symbol] = current_bid

                peak_pnl_pct = (self.peak_prices[symbol] - entry_price) / entry_price * 100

                # ── v5.6: End-of-day flatten ─────────────────────────
                # There was no EOD close at all: the loop traded until 15:59
                # and whatever was open rode overnight. On weekly and 0DTE
                # options an overnight gap cannot be stopped at any price —
                # the 25% stop never gets a chance to fire, because the move
                # happens while the market is shut. Given the 90-minute time
                # exit, holding overnight was never the intent anyway.
                if self._past_flatten_time():
                    log.info(f"🌆 EOD FLATTEN: {symbol} at "
                             f"{CONFIG.get('close_all_by_et', '15:45')} ET "
                             f"({pnl_pct:+.1f}%) — not carrying options "
                             f"overnight")
                    self._close_position(symbol, qty, current_bid,
                                         use_market=True)
                    continue

                # ── Time-based exit ──
                entry_time = self.entry_times.get(symbol)
                if entry_time:
                    minutes_held = (datetime.now() - entry_time).total_seconds() / 60
                    if minutes_held >= 90 and abs(pnl_pct) < 10:
                        # Only extend once per position
                        if symbol not in self.time_extended and pnl_pct > 0:
                            log.info(f"⏱️  TIME LIMIT reached but P&L is positive ({pnl_pct:+.1f}%) — extending 30min (one time only)")
                            self.entry_times[symbol] = datetime.now() - timedelta(minutes=60)
                            self.time_extended.add(symbol)
                            continue
                        log.info(f"⏱️  TIME EXIT: {symbol} held {minutes_held:.0f}min ({pnl_pct:+.1f}%) | Selling {qty} contracts")
                        self._close_position(symbol, qty, current_bid,
                                             use_market=force_market)
                        continue

                # Trailing stop
                if peak_pnl_pct >= 30.0:
                    if peak_pnl_pct >= 60.0:
                        trail_pct = 10.0
                    elif peak_pnl_pct >= 45.0:
                        trail_pct = 15.0
                    else:
                        trail_pct = 20.0
                    pullback_from_peak = (self.peak_prices[symbol] - current_bid) / self.peak_prices[symbol] * 100
                    if pullback_from_peak >= trail_pct:
                        log.info(
                            f"🔒 TRAILING STOP: {symbol} peaked at +{peak_pnl_pct:.1f}%, "
                            f"pulled back {pullback_from_peak:.1f}% (trail: {trail_pct}%) | Selling {qty} contracts"
                        )
                        self._close_position(symbol, qty, current_bid,
                                             use_market=force_market)
                        continue

                if pnl_pct >= tp_pct:
                    log.info(f"✅ TAKE PROFIT: {symbol} +{pnl_pct:.1f}% (threshold: +{tp_pct}%) | Selling {qty} contracts")
                    self._close_position(symbol, qty, current_bid,
                                         use_market=force_market)
                elif pnl_pct <= -sl_pct:
                    log.info(f"🛑 STOP LOSS: {symbol} {pnl_pct:.1f}% (threshold: -{sl_pct}%) | Selling {qty} contracts")
                    self._close_position(symbol, qty, current_bid,
                                         use_market=force_market)
                else:
                    trail_info = f" | Peak: +{peak_pnl_pct:.1f}%" if peak_pnl_pct > 5 else ""
                    time_info = f" | Held: {int((datetime.now() - entry_time).total_seconds() / 60)}min" if entry_time else ""
                    log.info(
                        f"Position {symbol}: P&L {pnl_pct:+.1f}% "
                        f"(TP: +{tp_pct}% | SL: -{sl_pct}%{trail_info}{time_info})"
                    )

        except Exception as e:
            log.error(f"Position monitor error: {e}")

    def _close_position(self, option_symbol: str, qty: int, bid: float,
                        use_market: bool = False):
        m = re.match(r'^([A-Z]+)', option_symbol)
        ticker = m.group(1) if m else option_symbol[:6]
        try:
            if use_market or bid <= 0.05:
                # No bid, or a bid so small a limit is pointless. Get out.
                result = self.client.place_order(
                    symbol=ticker, option_symbol=option_symbol,
                    side="sell_to_close", quantity=qty, order_type="market")
            else:
                result = self.client.place_order(
                    symbol=ticker,
                    option_symbol=option_symbol,
                    side="sell_to_close",
                    quantity=qty,
                    order_type="limit",
                    price=round(bid * 0.98, 2)
                )
            entry_price = self.entry_prices.get(option_symbol, bid)
            pnl_per_contract = (bid - entry_price) * 100
            total_pnl = pnl_per_contract * qty
            pnl_pct = ((bid - entry_price) / entry_price * 100) if entry_price else 0
            pnl_emoji = "💰" if total_pnl >= 0 else "💸"
            self.daily_realized_pnl += total_pnl
            log.info(f"{pnl_emoji} REALIZED P&L: {option_symbol} | Entry ${entry_price:.2f} → Exit ${bid:.2f} | {pnl_pct:+.1f}% | ${total_pnl:+.2f} ({qty} contracts) | Day total: ${self.daily_realized_pnl:+.2f}")
            log.info(f"Close order placed: {result}")

            # ── Add to pending_close — guard stays active until Tradier confirms fill ──
            self.pending_close.add(option_symbol)
            self.pending_close_times[option_symbol] = time.time()
            close_order_id = str(result.get("order", {}).get("id", ""))
            if close_order_id:
                self.pending_close_order_ids[option_symbol] = close_order_id

            # ── Write to trade_results.json for TradeJudge history ──
            self._write_trade_result(option_symbol, ticker, qty, entry_price, bid, pnl_pct, total_pnl)

            # Clear local tracking data now that close order is placed
            self.entry_prices.pop(option_symbol, None)
            self.entry_prices.pop(option_symbol + "_score", None)
            self.entry_prices.pop(option_symbol + "_time", None)
            self.entry_prices.pop(option_symbol + "_tp", None)
            self.entry_prices.pop(option_symbol + "_sl", None)
            self.peak_prices.pop(option_symbol, None)
            self.entry_times.pop(option_symbol, None)
            self.time_extended.discard(option_symbol)
            # NOTE: intentionally NOT discarding from pending_close here
            self._save_entry_prices()
            m = re.match(r'^([A-Z]+)', option_symbol)
            if m:
                self.recently_closed.add(m.group(1))
        except Exception as e:
            log.error(f"Failed to close {option_symbol}: {e}")

    def _write_trade_result(self, option_symbol: str, ticker: str, qty: int,
                             entry_price: float, exit_price: float,
                             pnl_pct: float, pnl_dollars: float):
        """Append closed trade result to trade_results.json for TradeJudge history."""
        try:
            # Determine direction from option symbol (C=CALL, P=PUT)
            direction = "CALL" if "C" in option_symbol.split(ticker)[-1][:8] else "PUT"
            record = {
                "ticker":        ticker,
                "option_symbol": option_symbol,
                "direction":     direction,
                "contracts":     qty,
                "entry_price":   round(entry_price, 2),
                "exit_price":    round(exit_price, 2),
                "pnl_pct":       round(pnl_pct, 2),
                "pnl_dollars":   round(pnl_dollars, 2),
                "exit_time":     datetime.now(pytz.timezone("America/New_York")).isoformat(),
                "regime":        getattr(self, "last_regime", "unknown"),
            }
            results_file = Path(__file__).parent / "trade_results.json"
            with open(results_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.warning(f"Could not write trade result: {e}")


# ── Trade Judge (LLM Gate) ─────────────────────────────────────────────────────
from trade_judge import TradeJudge

# ── Main Agent Loop ────────────────────────────────────────────────────────────
class OptionsAgent:
    def __init__(self):
        self.client   = TradierClient(CONFIG["tradier_token"], sandbox=CONFIG["sandbox"])
        self.signals  = SignalEngine()
        self.selector = OptionsSelector(self.client)
        self.risk     = RiskManager(self.client)
        self.monitor  = PositionMonitor(self.client)
        self.judge    = TradeJudge()
        self.trades_today: list = []
        self.ticker_cooldown: dict = {}
        self.premarket_watchlist: list = []  # populated by run_premarket_scan()
        self.last_regime_report = None       # most recent crash_mode.RegimeReport

    # ── Regime + concurrent scan (v5) ──────────────────────────────────────
    def _regime_and_signals(self):
        """Assess the regime and scan the watchlist in a single async pass.

        Returns ``(RegimeReport, overrides, signals)``.

        Both halves share one HTTP session and one event loop, so the regime
        inputs (SPY/VIX/VIX3M/HYG/IEF/curve) and every ticker's bars, chain
        and earnings check go out together instead of ~45 serial round trips.

        Any failure degrades to the original synchronous path. Slow beats
        blind — this decides whether real money buys options.
        """
        if CONFIG.get("use_async_scan", True) and AIOHTTP_AVAILABLE:
            try:
                return asyncio.run(self._regime_and_signals_async())
            except Exception as e:
                log.warning(f"Async scan failed ({e}) — falling back to sync path")

        # ── Synchronous fallback ──
        regime_str = self.signals._market_regime()
        rep = crash_mode.RegimeReport(regime=regime_str, prior_regime=regime_str,
                                      vix=self.risk._get_vix_cached(), degraded=True)
        rep.reasons.append("⚠️ Sync fallback — crash detection unavailable this cycle")
        ov = crash_mode.overrides_for(rep, CONFIG)
        ov["crash_mode"] = rep.regime == "crash"
        crash_mode.log_report(rep)
        sigs = self.signals.get_top_signals(min_score=CONFIG["min_signal_score"])
        return rep, ov, sigs

    async def _regime_and_signals_async(self):
        # v5.3: Tradier is the primary data source. Yahoo throttles data-centre
        # IPs to ~10s per request, which is both slow and — as v5.2 showed —
        # actively dangerous, since the failures push every symbol onto a
        # shared fallback path at once.
        td = None
        if CONFIG.get("use_tradier_data", True) and AIOHTTP_AVAILABLE:
            try:
                td = TradierData(CONFIG["tradier_token"],
                                 sandbox=CONFIG.get("sandbox", True),
                                 max_concurrency=int(CONFIG.get("scan_concurrency", 8)))
                await td.open()
            except Exception as e:
                log.warning(f"Tradier data layer unavailable ({e}) — using Yahoo")
                td = None

        md = AsyncMarketData(
            max_concurrency=int(CONFIG.get("scan_concurrency", 8)),
            timeout_secs=float(CONFIG.get("scan_timeout_secs", 12.0)),
            cache_ttl=float(CONFIG.get("scan_cache_ttl", 45.0)),
            daily_cache_ttl=float(CONFIG.get("daily_cache_ttl", 1800.0)),
            tradier=td,
        )
        await md.open()
        await md._ensure_crumb()
        try:
            rep = await crash_mode.assess(md, CONFIG.get("crash_cfg"))
            crash_mode.log_report(rep)

            ov = crash_mode.overrides_for(rep, CONFIG)
            ov["crash_mode"] = rep.regime == "crash"
            log.info(f"🎛️  Rules: {ov.get('reason','')} | "
                     f"calls={ov.get('allow_calls')} puts={ov.get('allow_puts')} "
                     f"size={ov.get('size_mult'):.2f}x "
                     f"spreads={ov.get('prefer_spreads')} "
                     f"maxATR={ov.get('max_atr_pct', 5.0)}%")

            min_score = float(ov.get("min_signal_score", CONFIG["min_signal_score"]))
            sigs = await self.signals.get_top_signals_async(
                md, min_score, regime=rep.regime, vix=rep.vix, overrides=ov)
            by_source: dict[str, int] = {}
            for v in md.source_log.values():
                by_source[v] = by_source.get(v, 0) + 1
            if by_source:
                log.info(f"📡 Data sources this cycle: {by_source}")
            return rep, ov, sigs
        finally:
            await md.close()
            if td is not None:
                await td.close()

    def _boost_index_signals(self, signals: list, regime: str, spy_chg: float) -> list:
        """
        On strong regime days, boost SPY/QQQ to front of queue.
        Index options have best liquidity and tightest spreads.
        """
        index_tickers = {"SPY", "QQQ", "TQQQ", "SPXL"}
        boosted = []
        others = []
        for sig in signals:
            if sig["ticker"] in index_tickers:
                # Boost index on strongly trending days
                if (regime == "bear" and sig["direction"] == "PUT" and spy_chg < -0.5) or \
                   (regime == "bull" and sig["direction"] == "CALL" and spy_chg > 0.5):
                    sig["score"] = round(sig["score"] + 1.0, 1)
                    log.info(f"📈 Index boost: {sig['ticker']} score +1.0 (regime aligned)")
                boosted.append(sig)
            else:
                others.append(sig)
        # Re-sort with boosted scores
        return sorted(boosted + others, key=lambda x: (x.get("confluence", 0), x["score"]), reverse=True)

    def run_premarket_scan(self):
        """
        Dedicated pre-market scan (9:00-9:29am ET).
        Finds the strongest gap plays before open and caches them
        so run_once() prioritizes them at market open.
        """
        log.info("🌅 PRE-MARKET SCAN — finding best gap plays before open...")
        try:
            # Use catalyst scanner directly for pre-market gaps
            from catalyst_scanner import CatalystScanner
            scanner = CatalystScanner()
            catalysts = scanner.get_top_catalyst_tickers(min_bonus=2)

            if not catalysts:
                log.info("🌅 Pre-market: no strong gap plays found.")
                self.premarket_watchlist = []
                return

            # Sort by bonus score, keep top 5
            catalysts.sort(key=lambda x: x.get("total_bonus", 0), reverse=True)
            top = catalysts[:5]
            self.premarket_watchlist = [c["ticker"] for c in top]

            log.info(f"🌅 Pre-market top plays: {self.premarket_watchlist}")
            for c in top:
                log.info(
                    f"  🎯 {c['ticker']}: bonus={c.get('total_bonus',0)} "
                    f"dir={c.get('direction_bias','?')} — {', '.join(c.get('reasons',[])[:2])}"
                )
        except Exception as e:
            log.error(f"Pre-market scan error: {e}")
            self.premarket_watchlist = []

    def _should_reenter(self, ticker: str, signal: dict) -> bool:
        """
        After cooldown expires, decide if we should re-enter a ticker.
        Re-entry allowed if:
        - Signal score is >= min_score + 1 (higher bar for re-entry)
        - Confluence is HIGH (4/4)
        - We made money on the last trade for this ticker
        """
        min_score = CONFIG["min_signal_score"] + 1.0  # higher bar
        score = signal.get("score", 0)
        confluence = signal.get("confluence", 0)

        if score < min_score:
            log.info(f"Re-entry {ticker}: score {score} below re-entry threshold {min_score}")
            return False
        if confluence < 4:
            log.info(f"Re-entry {ticker}: confluence {confluence}/4 — need 3/3 for re-entry")
            return False

        # Check last trade result
        try:
            results_file = Path(__file__).parent / "trade_results.json"
            if results_file.exists():
                last_result = None
                with open(results_file) as f:
                    for line in f:
                        try:
                            t = json.loads(line.strip())
                            if t.get("ticker") == ticker:
                                last_result = t
                        except:
                            continue
                if last_result and last_result.get("pnl_pct", 0) < -20:
                    log.info(f"Re-entry {ticker}: last trade was {last_result['pnl_pct']:.1f}% — skipping re-entry after big loss")
                    return False
        except:
            pass

        log.info(f"✅ Re-entry approved for {ticker}: score={score}, confluence={confluence}/4")
        return True

    def run_once(self):
        log.info("=" * 60)
        log.info(f"[CYCLE] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        balance = self.risk.get_account_balance()
        start = self.risk._start_of_day_capital or balance
        day_pnl = balance - start
        day_pnl_pct = (day_pnl / start * 100) if start > 0 else 0

        vix = self.risk._get_vix_cached()
        spy_chg = self.risk._get_spy_cached()
        log.info(f"💰 Account: ${balance:.2f} | Day P&L: ${day_pnl:+.2f} ({day_pnl_pct:+.1f}%) | Cash: ${self.risk.get_available_capital():.2f}")
        log.info(f"📈 SPY: {spy_chg:+.2f}% today | VIX: {vix:.1f}")

        log.info("Checking open positions...")
        self.monitor.check_and_exit()

        for ticker in self.monitor.recently_closed:
            cooldown_secs = 600 if ticker in getattr(self, "_last_loss_tickers", set()) else 400
            self.ticker_cooldown[ticker] = datetime.now().timestamp() + cooldown_secs
            log.info(f"Cooldown set for {ticker} — no re-entry for 400s")
        self.monitor.recently_closed.clear()

        capital = self.risk.get_available_capital()
        cap_limit = self.risk._dynamic_capital_limit()
        log.info(f"Available capital: ${capital:.2f} (limit: ${cap_limit:.2f})")
        effective_capital = min(capital, cap_limit)

        if effective_capital < CONFIG["min_capital_to_trade"]:
            log.info("Not enough capital to trade. Skipping signal scan.")
            return

        # ── Regime assessment + concurrent scan (v5) ──────────────────────────
        t_scan = time.time()
        regime_rep, overrides, top_signals = self._regime_and_signals()
        regime = regime_rep.regime
        self.last_regime_report = regime_rep
        log.info(f"⏱️  Regime + scan completed in {time.time() - t_scan:.1f}s")

        # ── Data integrity halt (v5.2) ────────────────────────────────────────
        # If the market data failed its sanity checks, skip the cycle outright.
        # Open positions were already evaluated above by check_and_exit, which
        # prices from the broker rather than from this feed — so exits still
        # work while entries are frozen. Never open a position on data we
        # cannot vouch for.
        if overrides.get("halt"):
            log.error(f"🛑 CYCLE HALTED — {overrides.get('reason')}")
            log.error("   No new positions this cycle. Existing positions are "
                      "still monitored via broker quotes.")
            return

        # VIX pre-check — the ceiling is now regime-aware. In crash mode it is
        # raised so the agent stays awake; protection moves to size + spreads.
        max_vix = float(overrides.get("max_vix", CONFIG.get("max_vix", 28)))
        if vix > max_vix:
            log.info(f"⚠️  VIX={vix:.1f} > {max_vix} — skipping scan, market too chaotic")
            return

        if not top_signals:
            log.info("No high-confidence signals found this cycle.")
            return

        log.info(f"{len(top_signals)} signal(s) passed filters. Top: {top_signals[0]['ticker']} "
                 f"({top_signals[0]['direction']}, score={top_signals[0]['score']})")

        open_tickers = self.risk.get_open_tickers()
        now_ts = datetime.now().timestamp()
        trade = None
        best_signal = None
        # Iterate signals — already sorted by score from signal engine
        pending_tickers: set = set()

        # Boost premarket watchlist tickers to front of queue

        for sig in top_signals:
            ticker = sig["ticker"]
            if ticker in open_tickers:
                log.info(f"Skipping {ticker} — position already open")
                continue
            # ── FIX 3: Block same ticker from being selected twice in one cycle ──
            if ticker in pending_tickers:
                log.info(f"Skipping {ticker} — already selected for execution this cycle")
                continue
            cooldown_until = self.ticker_cooldown.get(ticker, 0)
            if now_ts < cooldown_until:
                remaining = int(cooldown_until - now_ts)
                # Check re-entry eligibility even during cooldown if signal is very strong
                if remaining < 60 and self._should_reenter(ticker, sig):
                    log.info(f"🔄 Re-entry: overriding final {remaining}s cooldown for {ticker}")
                else:
                    log.info(f"Skipping {ticker} — cooldown ({remaining}s remaining)")
                    continue
            # ── Structure selection ────────────────────────────────────────
            # When IV is already elevated, a naked long option is a bet that
            # volatility keeps rising. A debit spread is a bet on direction.
            # Prefer the spread and fall back to a single leg if one cannot
            # be built (thin chain, no reward:risk, level-2 account).
            t = None
            structure = CONFIG.get("crash_structure", "deep_itm")
            if overrides.get("prefer_defined_risk"):
                if structure == "deep_itm":
                    t = self.selector.select_deep_itm(sig, effective_capital, overrides)
                elif structure == "spread":
                    t = self.selector.select_spread(sig, effective_capital, overrides)
                if not t and CONFIG.get("crash_allow_single_leg_fallback", False):
                    log.info(f"  ↩️  {ticker}: no {structure} structure — "
                             f"falling back to a plain single leg")
                elif not t:
                    # Deliberate: an at-the-money option bought after a vol
                    # spike is the position this whole path exists to avoid.
                    # Standing down beats taking the trade we ruled out.
                    log.info(f"  ⬜ {ticker}: no {structure} structure available "
                             f"and single-leg fallback is disabled — standing down")
                    continue
            if not t:
                t = self.selector.select_contract(sig, effective_capital, overrides)

            if t:
                if t.get("structure") == "deep_itm":
                    d = t["deep_itm"]
                    log.info(
                        f"Trade candidate: {t['ticker']} CALL {d['strike']} DEEP ITM "
                        f"(from {t['source_signal_ticker']} PUT signal) | "
                        f"{t['contracts']}x @ ${d['ask']:.2f} | "
                        f"Total: ${t['total_cost']:.2f} | delta {d['delta']:.2f} | "
                        f"intrinsic ${d['intrinsic']:.2f} + extrinsic ${d['extrinsic']:.2f} "
                        f"({d['extrinsic_pct']:.0f}%) | BE {d['breakeven']}"
                    )
                elif t.get("structure") == "vertical_debit":
                    sp = t["spread"]
                    log.info(
                        f"Trade candidate: {ticker} {sp['direction']} "
                        f"{sp['long_strike']}/{sp['short_strike']} SPREAD | "
                        f"{t['contracts']}x @ ${sp['debit']:.2f} debit | "
                        f"Total: ${t['total_cost']:.2f} | "
                        f"Max loss ${sp['max_loss']:.0f} / max profit ${sp['max_profit']:.0f} "
                        f"| R:R {sp['reward_risk']:.2f} | BE {sp['breakeven']}"
                    )
                else:
                    spread_pct = (t['ask'] - t['bid']) / ((t['ask'] + t['bid']) / 2) * 100 if t.get('bid') else 0
                    log.info(
                        f"Trade candidate: {t['option_symbol']} | {t['contracts']} contracts "
                        f"@ ${t['ask']:.2f} | Total: ${t['total_cost']:.2f} | Spread: {spread_pct:.1f}%"
                    )
                can_trade, reason = self.risk.can_trade(t, effective_capital, regime, overrides)
                if not can_trade:
                    log.info(f"Risk manager blocked: {reason} — trying next signal")
                    continue
                trade = t
                best_signal = sig
                break
            log.info(f"No suitable contract for {ticker} — trying next signal")
        if not trade:
            log.info("No suitable contracts found for any signals this cycle.")
            return

        # ── LLM gate — final sanity check before execution ──
        should_trade, size_multiplier, judge_reason, tp_sl_override = self.judge.judge(
            trade, best_signal, regime,
            self.risk._get_vix_cached(),
            self.risk._get_spy_cached()
        )
        if not should_trade:
            log.info(f"🤖 TradeJudge BLOCKED: {judge_reason}")
            return

        # Apply confidence-based size multiplier from TradeJudge
        if size_multiplier != 1.0:
            original_contracts = trade["contracts"]
            trade["contracts"] = max(1, round(trade["contracts"] * size_multiplier))
            trade["total_cost"] = trade["contracts"] * trade["ask"] * 100
            if trade["contracts"] != original_contracts:
                log.info(f"🤖 TradeJudge resized: {original_contracts} → {trade['contracts']} contracts ({size_multiplier}x)")

        is_spread = trade.get("structure") == "vertical_debit"
        if is_spread:
            sp = trade["spread"]
            log.info(f"EXECUTING: {trade['contracts']}x {trade['ticker']} "
                     f"{sp['long_strike']}/{sp['short_strike']} {sp['direction']} "
                     f"debit spread @ ${sp['debit']:.2f}")
        else:
            log.info(f"EXECUTING: Buy {trade['contracts']}x {trade['option_symbol']} @ market")
        try:
            # ── FIX 3: Set cooldown and pending_tickers BEFORE place_order ──
            # Prevents any overlap if execution is slow or threading races occur
            self.ticker_cooldown[trade["ticker"]] = datetime.now().timestamp() + 400
            pending_tickers.add(trade["ticker"])
            log.info(f"Cooldown set for {trade['ticker']} — no re-entry for 400s (entry lock)")

            if is_spread:
                payload = spreads_mod.open_spread_payload(trade["ticker"], trade["spread"])
                try:
                    result = self.client.place_multileg_order(payload)
                except Exception as me:
                    # Almost always options approval level 2 rejecting a
                    # multileg order. Fall back to the single long leg rather
                    # than retrying — a half-filled spread is a naked short.
                    log.warning(f"⚠️  Multileg order rejected ({me}) — "
                                f"falling back to single long leg. If this repeats, "
                                f"your Tradier account likely needs options level 3.")
                    trade = self.selector.select_contract(best_signal, effective_capital, overrides)
                    if not trade:
                        log.info("No single-leg fallback available — skipping cycle.")
                        return
                    is_spread = False
                    # Re-apply the judge's sizing decision; the fallback
                    # rebuilt the trade from scratch and lost it.
                    if size_multiplier != 1.0:
                        trade["contracts"] = max(1, round(trade["contracts"] * size_multiplier))
                        trade["total_cost"] = trade["contracts"] * trade["ask"] * 100
                    result = self.client.place_order(
                        symbol=trade["ticker"],
                        option_symbol=trade["option_symbol"],
                        side="buy_to_open",
                        quantity=trade["contracts"],
                        order_type="market",
                    )
            else:
                # v5.5: LIMIT, not market.
                #
                # Tradier rejects market orders on options whenever it cannot
                # establish a quote at that instant — "There is no price.
                # Security symbol: ...". Observed live on SQQQ260814C00036000
                # while the chain was quoting $1.38. Exits here have always
                # used limit orders; entries were the inconsistency.
                #
                # It is also simply better execution. A market order on an
                # option with a wide spread fills wherever the book happens to
                # be, which on a 3x ETF in a selloff is nowhere good.
                pad = 1.0 + float(CONFIG.get("entry_limit_pad_pct", 3.0)) / 100.0
                limit_px = round(trade["ask"] * pad, 2)
                log.info(f"📐 Limit buy @ ${limit_px:.2f} "
                         f"(ask ${trade['ask']:.2f} + "
                         f"{CONFIG.get('entry_limit_pad_pct', 3.0)}%)")
                result = self.client.place_order(
                    symbol=trade["ticker"],
                    option_symbol=trade["option_symbol"],
                    side="buy_to_open",
                    quantity=trade["contracts"],
                    order_type="limit",
                    price=limit_px,
                )

            # ── Confirm the order actually lives ──────────────────────────
            # Without this the agent records an entry, writes trades.json and
            # sets a cooldown for a position it does not own.
            order_id = str((result or {}).get("order", {}).get("id", ""))
            filled, why = self._await_fill(order_id)
            if not filled:
                log.error(f"❌ ORDER DID NOT FILL: {why}")
                log.error("   No entry recorded. The agent owns nothing from "
                          "this cycle.")
                return

            score = best_signal.get("score", 13)
            # Merge sl_hint from signal engine into tp_sl_override if TradeJudge didn't set SL
            # Deep ITM moves differently from an OTM lottery ticket: high delta
            # on a 3x ETF means a 1% index move is already ~20-30% on the
            # contract, so the default OTM take-profit/stop percentages are the
            # wrong scale. Use crash-specific levels instead.
            if trade.get("structure") == "deep_itm":
                tp_sl_override = {
                    "tp": float(CONFIG.get("deep_itm_take_profit_pct", 35.0)),
                    "sl": float(CONFIG.get("deep_itm_stop_loss_pct", 30.0)),
                }
                log.info(f"📊 Deep ITM TP/SL: +{tp_sl_override['tp']}% / "
                         f"-{tp_sl_override['sl']}%")

            sl_hint = best_signal.get("sl_hint")
            if sl_hint and (tp_sl_override is None or tp_sl_override.get("sl") is None):
                tp_sl_override = tp_sl_override or {}
                tp_sl_override["sl"] = sl_hint
                log.info(f"📊 SL scaled to contract price: {sl_hint}%")
            spread_rec = None
            if is_spread:
                spread_rec = {**trade["spread"], "underlying": trade["ticker"]}
            self.monitor.record_entry(trade["option_symbol"], trade["ask"], score,
                                      tp_sl_override=tp_sl_override, spread=spread_rec)
            self.monitor.last_regime = regime
            self.trades_today.append({**trade, "result": result, "time": datetime.now().isoformat()})
            log.info(f"Order submitted: {json.dumps(result, indent=2)}")

            with open("trades.json", "a") as f:
                f.write(json.dumps({
                    **{k: v for k, v in trade.items() if k != "signal"},
                    "score": score,
                    "direction": trade["direction"],
                    "time": datetime.now().isoformat()
                }) + "\n")

        except Exception as e:
            log.error(f"Order execution failed: {e}")

    def _await_fill(self, order_id: str, timeout: float = None) -> tuple[bool, str]:
        """Poll Tradier until the order fills, is rejected, or we give up.

        Returns (filled, explanation). A partial fill counts as filled — the
        position monitor sizes from the broker's quantity, not ours.
        """
        if not order_id:
            return False, "no order id returned by Tradier"
        timeout = timeout or float(CONFIG.get("fill_timeout_secs", 45))
        deadline = time.time() + timeout
        last = "unknown"

        while time.time() < deadline:
            o = self.client.get_order(order_id)
            status = (o.get("status") or "").lower()
            last = status or last

            if status == "filled":
                px = o.get("avg_fill_price")
                log.info(f"✅ FILLED {o.get('exec_quantity')} @ ${px}")
                return True, "filled"
            if status in ("rejected", "canceled", "expired", "error"):
                reason = o.get("reason_description") or "no reason given"
                return False, f"{status.upper()} — {reason}"
            if status == "partially_filled":
                log.info(f"◐ Partial fill: {o.get('exec_quantity')}/"
                         f"{o.get('quantity')}")
                return True, "partially filled"
            time.sleep(2)

        # Still working at the deadline. Cancel rather than leave a resting
        # order that could fill minutes later into a market we no longer read.
        log.warning(f"⏱️  Order {order_id} unfilled after {timeout:.0f}s "
                    f"(status: {last}) — cancelling")
        try:
            self.client.cancel_order(order_id)
        except Exception as e:
            log.error(f"   Cancel failed: {e} — CHECK TRADIER MANUALLY")
        return False, f"timed out unfilled after {timeout:.0f}s"

    def _write_daily_summary(self):
        eastern = pytz.timezone("America/New_York")
        today = datetime.now(eastern).strftime("%Y-%m-%d")
        if not self.trades_today:
            log.info(f"📊 Daily Summary ({today}): No trades executed today.")
            return

        log.info(f"📊 ══════════════ DAILY SUMMARY {today} ══════════════")
        log.info(f"   Total trades executed: {len(self.trades_today)}")
        total_deployed = sum(t.get("total_cost", 0) for t in self.trades_today)
        log.info(f"   Total capital deployed: ${total_deployed:.2f}")
        for t in self.trades_today:
            log.info(
                f"   {t['ticker']} {t['direction']} | "
                f"{t['contracts']}x {t['option_symbol']} @ ${t['ask']:.2f} | "
                f"${t['total_cost']:.2f}"
            )
        log.info(f"📊 ════════════════════════════════════════════════════")

    def _fast_monitor_loop(self):
        """Runs every 30s to check exits only — independent of main scan cycle."""
        import threading
        def _loop():
            while True:
                time.sleep(30)
                try:
                    # ── FIX 4: Removed log suppression — TP/SL/trailing stops always logged ──
                    # Previously setLevel(WARNING) was silencing all INFO logs in the fast monitor,
                    # meaning take profits and stop losses fired here were invisible in the log.
                    self.monitor.check_and_exit()
                    for ticker in list(self.monitor.recently_closed):
                        self.ticker_cooldown[ticker] = datetime.now().timestamp() + 400
                        log.info(f"Cooldown set for {ticker} — no re-entry for 400s")
                    self.monitor.recently_closed.clear()
                except Exception as e:
                    log.error(f"Fast monitor error: {e}")
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        log.info("⚡ Fast position monitor started (30s interval)")

    def run_loop(self):
        log.info("OptionsAgent starting — v5 (crash-aware, async scan)")
        log.info(f"   Capital limit:    ${self.risk._dynamic_capital_limit():.2f} (dynamic)")
        log.info(f"   Capital scaling:  75% of gains reinvested")
        log.info(f"   Take profit:      dynamic (score>=16: +45%, >=14: +42%, else: +38%)")
        log.info(f"   Stop loss:        25% single leg | "
                 f"{CONFIG.get('spread_stop_loss_pct', 55)}% spread")
        log.info(f"   Max positions:    {self.risk._dynamic_max_positions()} (dynamic)")
        log.info(f"   Confluence req:   3/3 timeframes must agree")
        log.info(f"   Time exit:        force close after 90min no movement")
        log.info(f"   Sandbox:          {CONFIG['sandbox']}")
        log.info("   ── v5 ──")
        log.info(f"   Async scan:       {'ON' if CONFIG.get('use_async_scan', True) and AIOHTTP_AVAILABLE else 'OFF (sync fallback)'}"
                 f" | concurrency {CONFIG.get('scan_concurrency', 8)}")
        log.info(f"   Crash mode:       {'ARMED' if CONFIG.get('crash_mode_enabled', False) else 'DISABLED (set crash_mode_enabled=True to arm)'}")
        log.info(f"   Detection:        VIX level + VIX/VIX3M term structure + "
                 f"SPY 20d drawdown + credit (HYG/IEF)")
        _struct = CONFIG.get("crash_structure", "deep_itm")
        log.info(f"   In crash:         PUTs only, {_struct}, "
                 f"max VIX ceiling lifted to {crash_mode.DEFAULTS['crash_max_vix']:.0f}")
        if _struct == "deep_itm":
            log.info(f"   Structure:        deep ITM CALLs on "
                     f"{CONFIG.get('crash_underlyings', ['SQQQ'])} "
                     f"(inverse ETF = long call = options level 2)")
            log.info(f"   Budget/trade:     ${CONFIG.get('crash_max_trade_size', 250):.0f} "
                     f"| max extrinsic "
                     f"{CONFIG.get('deep_itm_cfg', {}).get('max_extrinsic_pct', 40)}% of premium")
            log.info(f"   If unreachable:   "
                     f"{'falls back to a single leg' if CONFIG.get('crash_allow_single_leg_fallback') else 'STANDS DOWN (will not buy near-the-money)'}")
        elif _struct == "spread":
            log.warning("   ⚠️  crash_structure='spread' requires Tradier options "
                        "LEVEL 3. On level 2 it degrades to a single leg.")
        if not CONFIG.get("crash_mode_enabled", False):
            log.warning("   ⚠️  Crash mode is DISABLED — the agent will still go "
                        "flat in high vol exactly as it did before. This is the "
                        "safe default; arm it deliberately.")
        if CONFIG.get("crash_mode_enabled", False) and not CONFIG.get("sandbox", True):
            log.warning("   ⚠️  LIVE + crash mode armed. Run preflight.py first to "
                        "confirm a structure is actually reachable at your budget.")

        last_summary_date = None
        self._fast_monitor_loop()

        while True:
            try:
                eastern = pytz.timezone("America/New_York")
                now     = datetime.now(eastern)
                hour    = now.hour
                minute  = now.minute
                weekday = now.weekday()

                if weekday >= 5:
                    log.info("Weekend. Markets closed. Sleeping 1hr.")
                    time.sleep(3600)
                    continue

                if hour == 16 and minute < 5 and last_summary_date != now.date():
                    self._write_daily_summary()
                    self.trades_today = []
                    last_summary_date = now.date()

                market_open = (
                    (hour == 9 and minute >= 30) or
                    (10 <= hour <= 14) or
                    (hour == 15 and minute <= 59)
                )

                # Pre-market scan window: 9:00-9:29am
                premarket = (hour == 9 and minute < 30)
                if premarket:
                    if not getattr(self, '_premarket_scanned_today', None) == now.date():
                        self.run_premarket_scan()
                        self._premarket_scanned_today = now.date()
                    log.info(f"Pre-market hours ({hour}:{minute:02d} ET). Sleeping 2min.")
                    time.sleep(120)
                    continue

                if not market_open:
                    # Clear premarket watchlist after market closes
                    if hour >= 16:
                        self.premarket_watchlist = []
                    log.info(f"Outside market hours ({hour}:{minute:02d} ET). Sleeping 5min.")
                    time.sleep(300)
                    continue

                self.run_once()

            except KeyboardInterrupt:
                log.info("Agent stopped by user.")
                break
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}")

            time.sleep(CONFIG["scan_interval_seconds"])


if __name__ == "__main__":
    agent = OptionsAgent()
    agent.run_loop()
