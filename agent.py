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
"""

import os
import re
import time
import json
import logging
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
from config import CONFIG
from signal_engine import SignalEngine

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

    def cancel_order(self, order_id: str):
        return requests.delete(
            f"{self.base}/accounts/{CONFIG['account_id']}/orders/{order_id}",
            headers=self.headers
        ).json()

    def get_orders(self):
        return self._get(f"/accounts/{CONFIG['account_id']}/orders")


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

    def select_contract(self, signal: dict, capital: float) -> Optional[dict]:
        ticker = signal["ticker"]
        direction = signal["direction"]
        current_price = signal["price"]

        strike_offset = 1.02 if direction == "CALL" else 0.98
        target_strike = round(current_price * strike_offset / 5) * 5

        expiry = self.get_nearest_expiry(ticker, days_out=CONFIG["min_days_to_expiry"])
        if not expiry:
            log.warning(f"No expiry found for {ticker}")
            return None

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
                    delta  = abs(float(opt.get("greeks", {}).get("delta", 0)))

                    if ask <= 0 or ask > CONFIG["max_contract_price"]:
                        continue
                    if delta < 0.20 or delta > 0.70:
                        continue

                    mid = (ask + bid) / 2
                    if mid > 0:
                        spread_pct = (ask - bid) / mid
                        if spread_pct > 0.20:
                            continue

                    diff = abs(strike - target_strike)
                    if diff < best_diff:
                        best_diff = diff
                        best = opt
                except:
                    continue

            if not best:
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

            max_spend = min(capital, CONFIG.get("max_trade_size", capital)) * size_multiplier
            contracts = max(1, int(max_spend / (ask_price * 100)))
            total_cost = contracts * ask_price * 100

            if total_cost > capital:
                contracts = max(1, contracts - 1)
                total_cost = contracts * ask_price * 100

            return {
                "ticker": ticker,
                "direction": direction,
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

    def can_trade(self, trade: dict, available_capital: float, regime: str = "neutral") -> tuple[bool, str]:
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
        vix = self._get_vix_cached()
        if vix > 25:
            log.info(f"⚠️  VIX={vix:.1f} > 25 — skipping trade")
            return False, f"VIX too high ({vix:.1f})"

        # ── SPY trend alignment ──
        spy_chg = self._get_spy_cached()
        if spy_chg < -1.0 and direction == "CALL" and score < 17:
            return False, f"Blocked CALL — SPY down {spy_chg:.1f}% today (need score 17+, have {score})"
        if spy_chg > 1.0 and direction == "PUT" and score < 17:
            return False, f"Blocked PUT — SPY up {spy_chg:.1f}% today (need score 17+, have {score})"

        # ── Regime filter ──
        if regime == "bear" and direction == "CALL" and score < 16:
            return False, f"Blocked CALL in BEAR regime (score {score} < 16)"
        if regime == "bull" and direction == "PUT" and score < 16:
            return False, f"Blocked PUT in BULL regime (score {score} < 16)"

        return True, "OK"


# ── Position Monitor ───────────────────────────────────────────────────────────
class PositionMonitor:
    def __init__(self, client: TradierClient):
        self.client = client
        self.entry_prices: dict = {}
        self.peak_prices: dict = {}
        self.entry_times: dict = {}
        self.recently_closed: set = set()
        self.pending_close: set = set()   # symbols with close order already placed
        self.daily_realized_pnl: float = 0.0
        self._load_entry_prices()

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

    def _save_entry_prices(self):
        try:
            with open(self._entry_prices_path(), "w") as f:
                json.dump(self.entry_prices, f)
        except Exception as e:
            log.warning(f"Could not save entry prices: {e}")

    def record_entry(self, option_symbol: str, entry_price: float, score: float = 13):
        self.entry_prices[option_symbol] = entry_price
        self.entry_prices[option_symbol + "_score"] = score
        self.entry_prices[option_symbol + "_time"] = datetime.now().isoformat()
        self.peak_prices[option_symbol] = entry_price
        self.entry_times[option_symbol] = datetime.now()
        self._save_entry_prices()

    def _dynamic_tp_sl(self, signal_score: float) -> tuple:
        if signal_score >= 16:
            return 45.0, 25.0
        elif signal_score >= 14:
            return 42.0, 25.0
        else:
            return 38.0, 25.0

    def check_and_exit(self):
        try:
            pos_resp = self.client.get_positions()
            if not isinstance(pos_resp, dict):
                return
            raw_positions = pos_resp.get("positions", None)
            if not raw_positions or raw_positions == "null" or not isinstance(raw_positions, dict):
                return
            positions = raw_positions.get("position", [])
            if isinstance(positions, dict):
                positions = [positions]

            for pos in positions:
                symbol      = pos.get("symbol", "")

                # Skip if we already placed a close order this cycle
                if symbol in self.pending_close:
                    log.info(f"⏳ Skipping {symbol} — close order already pending")
                    continue

                qty         = int(pos.get("quantity", 0))
                cost_basis  = float(pos.get("cost_basis", 0))
                entry_price = cost_basis / (qty * 100) if qty > 0 else 0

                quote_resp  = self.client.get_quote(symbol)
                quotes      = quote_resp.get("quotes", {}).get("quote", {})
                current_bid = float(quotes.get("bid", 0))

                if entry_price <= 0 or current_bid <= 0:
                    continue

                pnl_pct   = (current_bid - entry_price) / entry_price * 100
                sig_score = self.entry_prices.get(symbol + "_score", 13)
                tp_pct, sl_pct = self._dynamic_tp_sl(sig_score)

                if symbol not in self.peak_prices:
                    self.peak_prices[symbol] = entry_price
                if current_bid > self.peak_prices[symbol]:
                    self.peak_prices[symbol] = current_bid

                peak_pnl_pct = (self.peak_prices[symbol] - entry_price) / entry_price * 100

                # ── Time-based exit ──
                entry_time = self.entry_times.get(symbol)
                if entry_time:
                    minutes_held = (datetime.now() - entry_time).total_seconds() / 60
                    if minutes_held >= 90 and abs(pnl_pct) < 10:
                        log.info(f"⏱️  TIME EXIT: {symbol} held {minutes_held:.0f}min with no movement ({pnl_pct:+.1f}%) | Selling {qty} contracts")
                        self._close_position(symbol, qty, current_bid)
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
                        self._close_position(symbol, qty, current_bid)
                        continue

                if pnl_pct >= tp_pct:
                    log.info(f"✅ TAKE PROFIT: {symbol} +{pnl_pct:.1f}% (threshold: +{tp_pct}%) | Selling {qty} contracts")
                    self._close_position(symbol, qty, current_bid)
                elif pnl_pct <= -sl_pct:
                    log.info(f"🛑 STOP LOSS: {symbol} {pnl_pct:.1f}% (threshold: -{sl_pct}%) | Selling {qty} contracts")
                    self._close_position(symbol, qty, current_bid)
                else:
                    trail_info = f" | Peak: +{peak_pnl_pct:.1f}%" if peak_pnl_pct > 5 else ""
                    time_info = f" | Held: {int((datetime.now() - entry_time).total_seconds() / 60)}min" if entry_time else ""
                    log.info(
                        f"Position {symbol}: P&L {pnl_pct:+.1f}% "
                        f"(TP: +{tp_pct}% | SL: -{sl_pct}%{trail_info}{time_info})"
                    )

        except Exception as e:
            log.error(f"Position monitor error: {e}")

    def _close_position(self, option_symbol: str, qty: int, bid: float):
        m = re.match(r'^([A-Z]+)', option_symbol)
        ticker = m.group(1) if m else option_symbol[:6]
        try:
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
            self.pending_close.add(option_symbol)  # prevent repeat close attempts
            self.entry_prices.pop(option_symbol, None)
            self.entry_prices.pop(option_symbol + "_score", None)
            self.entry_prices.pop(option_symbol + "_time", None)
            self.peak_prices.pop(option_symbol, None)
            self.entry_times.pop(option_symbol, None)
            self.pending_close.discard(option_symbol)  # clear once confirmed gone
            self._save_entry_prices()
            m = re.match(r'^([A-Z]+)', option_symbol)
            if m:
                self.recently_closed.add(m.group(1))
        except Exception as e:
            log.error(f"Failed to close {option_symbol}: {e}")


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
            self.ticker_cooldown[ticker] = datetime.now().timestamp() + 400
            log.info(f"Cooldown set for {ticker} — no re-entry for 400s")
        self.monitor.recently_closed.clear()

        capital = self.risk.get_available_capital()
        cap_limit = self.risk._dynamic_capital_limit()
        log.info(f"Available capital: ${capital:.2f} (limit: ${cap_limit:.2f})")
        effective_capital = min(capital, cap_limit)

        if effective_capital < CONFIG["min_capital_to_trade"]:
            log.info("Not enough capital to trade. Skipping signal scan.")
            return

        # VIX pre-check
        if vix > 25:
            log.info(f"⚠️  VIX={vix:.1f} — skipping scan, market too chaotic")
            return

        log.info(f"Scanning watchlist: {CONFIG['watchlist']}")
        top_signals = self.signals.get_top_signals(min_score=CONFIG["min_signal_score"])

        if not top_signals:
            log.info("No high-confidence signals found this cycle.")
            return

        # ── Confluence filter — require at least 2/3 timeframes to agree ──
        top_signals = [s for s in top_signals if s.get("confluence", 0) >= 2]
        if not top_signals:
            log.info("No signals passed confluence filter (need 2/3 timeframes to agree).")
            return

        # ── Boost index options on strong trend days ──
        regime = getattr(self.signals, 'last_regime', 'neutral')
        top_signals = self._boost_index_signals(top_signals, regime, spy_chg)

        log.info(f"{len(top_signals)} signal(s) passed filters. Top: {top_signals[0]['ticker']} "
                 f"({top_signals[0]['direction']}, score={top_signals[0]['score']})")

        open_tickers = self.risk.get_open_tickers()
        now_ts = datetime.now().timestamp()
        trade = None
        best_signal = None
        for sig in top_signals:
            ticker = sig["ticker"]
            if ticker in open_tickers:
                log.info(f"Skipping {ticker} — position already open")
                continue
            cooldown_until = self.ticker_cooldown.get(ticker, 0)
            if now_ts < cooldown_until:
                remaining = int(cooldown_until - now_ts)
                log.info(f"Skipping {ticker} — cooldown ({remaining}s remaining)")
                continue
            t = self.selector.select_contract(sig, effective_capital)
            if t:
                trade = t
                best_signal = sig
                break
            log.info(f"No suitable contract for {ticker} — trying next signal")

        if not trade:
            log.info("No suitable contracts found for any signals this cycle.")
            return

        spread_pct = (trade['ask'] - trade['bid']) / ((trade['ask'] + trade['bid']) / 2) * 100 if trade.get('bid') else 0
        log.info(
            f"Trade candidate: {trade['option_symbol']} | {trade['contracts']} contracts "
            f"@ ${trade['ask']:.2f} | Total: ${trade['total_cost']:.2f} | Spread: {spread_pct:.1f}%"
        )

        can_trade, reason = self.risk.can_trade(trade, effective_capital, regime)
        if not can_trade:
            log.info(f"Risk manager blocked: {reason}")
            return

        # ── LLM gate — final sanity check before execution ──
        should_trade, judge_reason = self.judge.judge(
            trade, best_signal, regime,
            self.risk._get_vix_cached(),
            self.risk._get_spy_cached()
        )
        if not should_trade:
            log.info(f"🤖 TradeJudge BLOCKED: {judge_reason}")
            return

        log.info(f"EXECUTING: Buy {trade['contracts']}x {trade['option_symbol']} @ market")
        try:
            result = self.client.place_order(
                symbol=trade["ticker"],
                option_symbol=trade["option_symbol"],
                side="buy_to_open",
                quantity=trade["contracts"],
                order_type="market"
            )
            score = best_signal.get("score", 13)
            self.monitor.record_entry(trade["option_symbol"], trade["ask"], score)
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

    def run_loop(self):
        log.info("OptionsAgent starting — MAXIMUM AGGRESSION MODE v4")
        log.info(f"   Capital limit:    ${self.risk._dynamic_capital_limit():.2f} (dynamic)")
        log.info(f"   Capital scaling:  75% of gains reinvested (raised from 50%)")
        log.info(f"   Take profit:      dynamic (score>=16: +45%, >=14: +42%, else: +38%)")
        log.info(f"   Stop loss:        25% (tightened from 33%)")
        log.info(f"   Max positions:    {self.risk._dynamic_max_positions()} (dynamic)")
        log.info(f"   Confluence req:   2/3 timeframes must agree (relaxed from 3/3)")
        log.info(f"   VIX filter:       skip trades when VIX > 25")
        log.info(f"   SPY filter:       no CALLs on SPY <-1%, no PUTs on SPY >+1%")
        log.info(f"   Index boost:      SPY/QQQ/TQQQ/SPXL prioritized on trend days")
        log.info(f"   Time exit:        force close after 90min no movement")
        log.info(f"   Theta block:      REMOVED — full trading hours until 4pm")
        log.info(f"   Sandbox:          {CONFIG['sandbox']}")

        last_summary_date = None

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
                if not market_open:
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
