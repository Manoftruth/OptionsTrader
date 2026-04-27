"""
OptionsAgent - Signal Engine v4.2
Fixes over v4.1:
1. None guard in _score_timeframe before passing df — prevents TypeError NoneType not subscriptable
2. yfinance 401/crumb errors handled with retry + backoff in _fetch
3. Dead tickers (CFLT, EXAS, SQ, PARA, NKLA) removed from watchlist
4. Series ambiguity error fixed — .bool() replaced with explicit scalar checks

Architecture:
  GATE 1 — Regime (hard block)
    BEAR    → PUTs only | min score 14
    BULL    → CALLs only | min score 14
    NEUTRAL → SPY+QQQ only, both directions | min score 14

  GATE 2 — Entry qualifier (must pass ONE)
    A) Unusual options volume: volume/OI ratio > 3x on ATM strikes,
       directional skew > 1.5x calls vs puts or vice versa
    B) BB squeeze breakout on 1H timeframe

  GATE 3 — Confluence: 3/3 timeframes must agree

  GATE 4 — Earnings blackout: skip if earnings within 2 days

  SCORING (max ~24)
    Squeeze breakout 1H:      +5
    Unusual options volume:   +4
    Volume surge 2x+:         +3
    VWAP aligned (0.8%+):     +2
    RSI extreme:              +2
    MACD accelerating:        +2
    3/3 confluence bonus:     +4
    Regime aligned:           +2
    Intraday trend bonus:     +1 or +2

  Signal also returns:
    vix_size_mult  — position size multiplier based on VIX
    sl_hint        — suggested SL% scaled to contract price
"""

import time
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import warnings
import logging
warnings.filterwarnings("ignore")

from congress_tracker import CongressTracker

log = logging.getLogger("OptionsAgent")

# FIX 3: Removed dead/delisted tickers — CFLT, EXAS, SQ, PARA, NKLA were generating
# constant "possibly delisted" noise. These are no longer valid 0DTE targets.
FULL_WATCHLIST    = ["SPY", "QQQ", "TSLA", "NVDA", "AMD", "META", "AMZN", "MSFT", "AAPL"]
NEUTRAL_WATCHLIST = ["SPY", "QQQ"]

ET = pytz.timezone("America/New_York")


class SignalEngine:

    WATCHLIST = FULL_WATCHLIST

    # ── Data fetching ──────────────────────────────────────────────────────────
    def _fetch(self, ticker: str, interval: str, period: str) -> pd.DataFrame:
        """
        FIX 2: Added retry with backoff for yfinance 401/crumb errors.
        Yahoo Finance rotates crumbs and occasionally returns 401 Unauthorized.
        A single retry after a short sleep resolves the vast majority of these.
        """
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                df = yf.download(ticker, interval=interval, period=period,
                                 progress=False, auto_adjust=True)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    if col in df.columns and isinstance(df[col], pd.DataFrame):
                        df[col] = df[col].iloc[:, 0]
                result = df.dropna()
                if result.empty and attempt < max_attempts - 1:
                    log.debug(f"Empty result for {ticker} {interval}, retrying...")
                    time.sleep(1.5)
                    continue
                return result
            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "Unauthorized" in err_str or "Invalid Crumb" in err_str:
                    if attempt < max_attempts - 1:
                        log.debug(f"yfinance 401/crumb error for {ticker}, retrying in 2s...")
                        time.sleep(2.0)
                        continue
                log.warning(f"Data fetch error {ticker} {interval}: {e}")
                return pd.DataFrame()
        return pd.DataFrame()

    @staticmethod
    def _scalar(v) -> float:
        while hasattr(v, "iloc"):
            v = v.iloc[-1]
        return float(v)

    @staticmethod
    def _to_series(col) -> pd.Series:
        s = col.squeeze()
        return s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s

    # ── Indicators ─────────────────────────────────────────────────────────────
    def _rsi(self, close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    def _macd(self, close: pd.Series) -> pd.Series:
        fast = close.ewm(span=12, adjust=False).mean()
        slow = close.ewm(span=26, adjust=False).mean()
        macd_line = fast - slow
        signal = macd_line.ewm(span=9, adjust=False).mean()
        return macd_line - signal

    def _vwap(self, df: pd.DataFrame) -> pd.Series:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()

    def _atr(self, df: pd.DataFrame, period: int = 14) -> float:
        h = self._to_series(df["High"])
        l = self._to_series(df["Low"])
        c = self._to_series(df["Close"])
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        return float(tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1])

    # ── Earnings blackout (Gate 4) ─────────────────────────────────────────────
    def _has_earnings_soon(self, ticker: str, days: int = 2) -> bool:
        try:
            tk = yf.Ticker(ticker)
            cal = tk.calendar
            if cal is None or cal.empty:
                return False
            if "Earnings Date" in cal.index:
                earn_dates = cal.loc["Earnings Date"]
                for d in earn_dates:
                    if pd.isna(d):
                        continue
                    earn_dt = pd.Timestamp(d).tz_localize(None)
                    now = pd.Timestamp.now()
                    if abs((earn_dt - now).days) <= days:
                        return True
            return False
        except Exception:
            return False

    # ── Unusual options volume (Gate 2A) ───────────────────────────────────────
    def _detect_unusual_options_volume(self, ticker: str, price: float) -> dict:
        """
        Detects unusual options flow — the best free intraday signal available.

        Signal fires when:
        1. Volume/OI ratio > 3x on ATM strikes (aggressive buying today)
        2. Directional skew: call vol vs put vol > 1.5x in one direction
        """
        try:
            tk = yf.Ticker(ticker)
            expirations = tk.options
            if not expirations:
                return {"detected": False}

            exp = expirations[0]  # nearest expiry
            chain = tk.option_chain(exp)
            calls = chain.calls.copy()
            puts  = chain.puts.copy()

            calls["volume"]       = calls["volume"].fillna(0)
            calls["openInterest"] = calls["openInterest"].fillna(0)
            puts["volume"]        = puts["volume"].fillna(0)
            puts["openInterest"]  = puts["openInterest"].fillna(0)

            # ATM strikes within 3% of price
            atm_range = 0.03
            atm_calls = calls[abs(calls["strike"] - price) / price < atm_range]
            atm_puts  = puts[abs(puts["strike"]  - price) / price < atm_range]

            if atm_calls.empty and atm_puts.empty:
                return {"detected": False}

            call_vol = float(atm_calls["volume"].sum())
            put_vol  = float(atm_puts["volume"].sum())
            call_oi  = float(atm_calls["openInterest"].sum())
            put_oi   = float(atm_puts["openInterest"].sum())

            total_vol = call_vol + put_vol
            if total_vol < 100:
                return {"detected": False}

            call_vol_oi = call_vol / call_oi if call_oi > 0 else 0
            put_vol_oi  = put_vol  / put_oi  if put_oi  > 0 else 0

            if call_vol > 0 and put_vol > 0:
                skew = call_vol / put_vol
            elif call_vol > 0:
                skew = 3.0
            else:
                skew = 1/3

            score = 0
            direction = None
            reasons = []

            if call_vol_oi > 5.0 and skew > 1.5:
                score += 4
                direction = "CALL"
                reasons.append(f"🔥 Unusual CALL flow: vol/OI={call_vol_oi:.1f}x skew={skew:.1f}x")
            elif call_vol_oi > 3.0 and skew > 1.5:
                score += 3
                direction = "CALL"
                reasons.append(f"⚡ Elevated CALL flow: vol/OI={call_vol_oi:.1f}x skew={skew:.1f}x")
            elif put_vol_oi > 5.0 and skew < 0.67:
                score += 4
                direction = "PUT"
                reasons.append(f"🔥 Unusual PUT flow: vol/OI={put_vol_oi:.1f}x skew={1/skew:.1f}x")
            elif put_vol_oi > 3.0 and skew < 0.67:
                score += 3
                direction = "PUT"
                reasons.append(f"⚡ Elevated PUT flow: vol/OI={put_vol_oi:.1f}x skew={1/skew:.1f}x")

            if score == 0:
                return {"detected": False}

            return {
                "detected":    True,
                "direction":   direction,
                "score":       score,
                "call_vol":    call_vol,
                "put_vol":     put_vol,
                "call_vol_oi": call_vol_oi,
                "put_vol_oi":  put_vol_oi,
                "skew":        skew,
                "reasons":     reasons,
            }

        except Exception as e:
            log.warning(f"Options volume detect error {ticker}: {e}")
            return {"detected": False}

    # ── Squeeze detection (Gate 2B) ────────────────────────────────────────────
    def _detect_squeeze(self, df: pd.DataFrame) -> dict:
        if len(df) < 30:
            return {"breakout": False, "in_squeeze": False, "direction": None, "strength": 0}

        close = self._to_series(df["Close"])
        high  = self._to_series(df["High"])
        low   = self._to_series(df["Low"])

        bb_mid   = close.rolling(20).mean()
        bb_std   = close.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std

        atr_series = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1).ewm(span=14, adjust=False).mean()
        kc_upper = bb_mid + 1.5 * atr_series
        kc_lower = bb_mid - 1.5 * atr_series

        in_squeeze     = (bb_upper < kc_upper) & (bb_lower > kc_lower)
        # FIX 4: was_squeezing used .any() on a slice which can raise Series ambiguity.
        # Explicitly convert to bool via .any() then bool() to be safe.
        was_squeezing  = bool(in_squeeze.iloc[-6:-1].any())
        just_broke_out = not bool(in_squeeze.iloc[-1])

        momentum = close - close.rolling(14).mean()
        # FIX 4: Use explicit float scalar comparison instead of Series boolean
        mom_val  = float(momentum.iloc[-1])
        mom_dir  = "CALL" if mom_val > 0 else "PUT"
        mom_str  = abs(mom_val) / float(close.iloc[-1]) * 100

        return {
            "breakout":   bool(was_squeezing and just_broke_out),
            "in_squeeze": bool(in_squeeze.iloc[-1]),
            "direction":  mom_dir,
            "strength":   mom_str,
        }

    # ── Market regime ──────────────────────────────────────────────────────────
    def _market_regime(self) -> str:
        try:
            spy   = self._fetch("SPY", "1d", "3mo")
            close = self._to_series(spy["Close"])
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            price = float(close.iloc[-1])
            if price > ema20 > ema50:
                return "bull"
            elif price < ema20 < ema50:
                return "bear"
            return "neutral"
        except Exception:
            return "neutral"

    # ── Intraday trend bonus ───────────────────────────────────────────────────
    def _intraday_trend_bonus(self, direction: str, vix: float,
                               spy_5m: pd.DataFrame | None = None) -> tuple[int, list]:
        """
        +1 if SPY is up/down 1%+ intraday and aligns with signal direction.
        +2 if SPY is up/down 2%+ AND VIX < 22 (confirmed low-fear trending day).
        Accepts pre-fetched spy_5m to avoid redundant download.
        """
        try:
            df = spy_5m if (spy_5m is not None and not spy_5m.empty) else self._fetch("SPY", "5m", "1d")
            if df.empty or len(df) < 2:
                return 0, []
            close      = self._to_series(df["Close"])
            open_price = float(close.iloc[0])
            cur_price  = float(close.iloc[-1])
            chg_pct    = (cur_price - open_price) / open_price * 100

            vix_low = vix < 22.0

            if direction == "CALL" and chg_pct >= 1.0:
                if chg_pct >= 2.0 and vix_low:
                    return 2, [f"📈 Strong intraday trend: SPY {chg_pct:+.1f}% + VIX {vix:.1f} → +2"]
                return 1, [f"📈 Intraday trend: SPY {chg_pct:+.1f}% → +1"]
            elif direction == "PUT" and chg_pct <= -1.0:
                if chg_pct <= -2.0 and not vix_low:
                    return 2, [f"📉 Strong intraday trend: SPY {chg_pct:+.1f}% + VIX {vix:.1f} elevated → +2"]
                return 1, [f"📉 Intraday trend: SPY {chg_pct:+.1f}% → +1"]

            return 0, []
        except Exception as e:
            log.warning(f"Intraday trend bonus error: {e}")
            return 0, []

    # ── VIX-based size multiplier ──────────────────────────────────────────────
    def _vix_size_multiplier(self, vix: float) -> float:
        if vix < 15:
            return 1.0
        elif vix < 20:
            return 0.85
        elif vix < 25:
            return 0.70
        elif vix < 30:
            return 0.50
        else:
            return 0.0

    # ── Contract price scaled SL hint ─────────────────────────────────────────
    def _sl_hint(self, estimated_ask: float) -> float:
        if estimated_ask < 0.30:
            return 40.0
        elif estimated_ask < 0.60:
            return 33.0
        elif estimated_ask < 1.00:
            return 28.0
        else:
            return 25.0

    # ── Single timeframe scoring ───────────────────────────────────────────────
    def _score_timeframe(self, df: pd.DataFrame, label: str) -> dict:
        # FIX 1: Explicit None check before any operations on df.
        # yfinance returns None in some edge cases (not just empty DataFrame).
        # This was causing TypeError: 'NoneType' object is not subscriptable
        # for META, AMZN, SPY, QQQ on certain fetch failures.
        if df is None or df.empty or len(df) < 30:
            return {"score": 0, "direction": None, "reasons": [],
                    "call_votes": 0, "put_votes": 0}

        close  = self._to_series(df["Close"])
        volume = self._to_series(df["Volume"])

        # FIX 4: Guard against Series with all NaN after to_series conversion
        if close is None or len(close) < 30:
            return {"score": 0, "direction": None, "reasons": [],
                    "call_votes": 0, "put_votes": 0}

        score      = 0
        directions = []
        reasons    = []

        # 1. RSI
        rsi      = float(self._rsi(close).iloc[-1])
        rsi_prev = float(self._rsi(close).iloc[-2])
        if rsi > 70 and rsi > rsi_prev:
            score += 2; directions.append("CALL")
            reasons.append(f"[{label}] RSI overbought & rising ({rsi:.0f})")
        elif rsi > 60:
            score += 1; directions.append("CALL")
            reasons.append(f"[{label}] RSI bullish ({rsi:.0f})")
        elif rsi < 30 and rsi < rsi_prev:
            score += 2; directions.append("PUT")
            reasons.append(f"[{label}] RSI oversold & falling ({rsi:.0f})")
        elif rsi < 40:
            score += 1; directions.append("PUT")
            reasons.append(f"[{label}] RSI bearish ({rsi:.0f})")

        # 2. MACD histogram
        hist      = self._macd(close)
        hist_now  = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        hist_p2   = float(hist.iloc[-3])
        if hist_now > 0 and hist_now > hist_prev > hist_p2:
            score += 2; directions.append("CALL")
            reasons.append(f"[{label}] MACD accelerating bullish")
        elif hist_now > 0 and hist_now > hist_prev:
            score += 1; directions.append("CALL")
            reasons.append(f"[{label}] MACD bullish")
        elif hist_now < 0 and hist_now < hist_prev < hist_p2:
            score += 2; directions.append("PUT")
            reasons.append(f"[{label}] MACD accelerating bearish")
        elif hist_now < 0 and hist_now < hist_prev:
            score += 1; directions.append("PUT")
            reasons.append(f"[{label}] MACD bearish")

        # 3. Volume surge
        avg_vol = float(volume.rolling(20).mean().iloc[-1])
        cur_vol = float(volume.iloc[-1])
        surge   = cur_vol / avg_vol if avg_vol > 0 else 1.0
        obv     = (np.sign(close.diff()).fillna(0) * volume).cumsum()
        # FIX 4: Use explicit float scalar comparison to avoid Series ambiguity
        obv_up  = float(obv.iloc[-1]) > float(obv.rolling(10).mean().iloc[-1])
        if surge > 2.0:
            score += 3
            directions.append("CALL" if obv_up else "PUT")
            reasons.append(f"[{label}] Volume surge {surge:.1f}x ({'bullish' if obv_up else 'bearish'})")
        elif surge > 1.5:
            score += 1
            directions.append("CALL" if obv_up else "PUT")
            reasons.append(f"[{label}] Volume elevated {surge:.1f}x")

        # 4. VWAP — threshold lowered from 1.5% to 0.8%
        try:
            vwap     = float(self._vwap(df).iloc[-1])
            price    = float(close.iloc[-1])
            vwap_dev = (price - vwap) / vwap * 100
            if vwap_dev > 0.8:
                score += 2; directions.append("CALL")
                reasons.append(f"[{label}] Price {vwap_dev:.1f}% above VWAP")
            elif vwap_dev < -0.8:
                score += 2; directions.append("PUT")
                reasons.append(f"[{label}] Price {vwap_dev:.1f}% below VWAP")
        except Exception:
            pass

        call_votes = directions.count("CALL")
        put_votes  = directions.count("PUT")
        direction  = (
            "CALL" if call_votes > put_votes
            else "PUT" if put_votes > call_votes
            else None
        )

        return {
            "score":      score,
            "direction":  direction,
            "call_votes": call_votes,
            "put_votes":  put_votes,
            "reasons":    reasons,
        }

    # ── Main scoring function ──────────────────────────────────────────────────
    def score_ticker(self, ticker: str, regime: str = "neutral",
                     vix: float = 20.0) -> dict | None:
        try:
            df_1h  = self._fetch(ticker, "1h",  "3mo")
            df_15m = self._fetch(ticker, "15m", "5d")
            df_5m  = self._fetch(ticker, "5m",  "2d")

            if df_1h is None or df_15m is None or df_5m is None:
                return None
            if df_1h.empty or df_15m.empty or df_5m.empty:
                return None

            price_1h = self._scalar(df_1h["Close"].iloc[-1])

            # ── GATE 4: Earnings blackout ──────────────────────────────────────
            if ticker not in ("SPY", "QQQ", "TQQQ", "QLD", "SOXL", "SPXL", "LABU") and self._has_earnings_soon(ticker, days=2):
                log.info(f"  ⬜ {ticker}: EARNINGS BLACKOUT — skipping")
                return None

            s1h  = self._score_timeframe(df_1h,  "1H")
            s15m = self._score_timeframe(df_15m, "15M")
            s5m  = self._score_timeframe(df_5m,  "5M")

            # ── GATE 3: Confluence — 3/3 required ─────────────────────────────
            directions = [s["direction"] for s in [s1h, s15m, s5m] if s["direction"]]
            call_tfs   = directions.count("CALL")
            put_tfs    = directions.count("PUT")

            if call_tfs == 3:
                final_direction  = "CALL"
                confluence_score = 4
            elif put_tfs == 3:
                final_direction  = "PUT"
                confluence_score = 4
            else:
                log.info(f"  ⬜ {ticker}: confluence FAILED ({call_tfs}C/{put_tfs}P) — need 3/3")
                return None

            # ── GATE 2: Entry qualifier ────────────────────────────────────────
            squeeze  = self._detect_squeeze(df_1h)
            uvol     = self._detect_unusual_options_volume(ticker, price_1h)

            has_squeeze = squeeze["breakout"] and squeeze["strength"] > 0.3
            has_uvol    = uvol["detected"] and uvol.get("direction") == final_direction

            if has_squeeze and squeeze["direction"] != final_direction:
                has_squeeze = False

            if not has_squeeze and not has_uvol:
                log.info(f"  ⬜ {ticker}: Gate 2 FAILED — no squeeze or unusual options volume")
                return None

            # ── Scoring ────────────────────────────────────────────────────────
            tech_score = (
                s1h["score"]  * 0.4 +
                s15m["score"] * 0.35 +
                s5m["score"]  * 0.25
            )

            bonus_score   = confluence_score
            bonus_reasons = ["✅ ALL 3 TIMEFRAMES AGREE"]

            if has_squeeze:
                bonus_score += 5
                bonus_reasons.append(
                    f"🔥 BB squeeze breakout → {final_direction} ({squeeze['strength']:.2f}%)"
                )

            uvol_bonus = 0
            if has_uvol:
                uvol_bonus    = uvol.get("score", 3)
                bonus_score  += uvol_bonus
                bonus_reasons.extend(uvol.get("reasons", []))

            regime_score = 0
            if (regime == "bull" and final_direction == "CALL") or \
               (regime == "bear" and final_direction == "PUT"):
                regime_score = 2
                bonus_reasons.insert(0, f"✅ Regime ({regime.upper()}) aligned with {final_direction}")
            bonus_score += regime_score

            spy_5m_for_trend = df_5m if ticker == "SPY" else None
            trend_bonus, trend_reasons = self._intraday_trend_bonus(final_direction, vix, spy_5m_for_trend)
            bonus_score  += trend_bonus
            bonus_reasons.extend(trend_reasons)

            total_score = round(tech_score + bonus_score, 1)

            # Volatility sanity check
            atr_val = self._atr(df_1h)
            vol_pct = (atr_val / price_1h * 100) if price_1h > 0 else 0
            if vol_pct > 5.0:
                log.info(f"  ⬜ {ticker}: ATR too high ({vol_pct:.1f}%) — spreads too wide")
                return None

            vix_mult = self._vix_size_multiplier(vix)

            try:
                tk    = yf.Ticker(ticker)
                exp   = tk.options[0]
                chain = tk.option_chain(exp)
                side  = chain.calls if final_direction == "CALL" else chain.puts
                atm   = side.iloc[(side["strike"] - price_1h).abs().argsort()[:1]]
                est_ask = float(atm["ask"].iloc[0]) if not atm.empty else 0.50
            except Exception:
                est_ask = 0.50
            sl_pct = self._sl_hint(est_ask)

            all_reasons = bonus_reasons + s1h["reasons"] + s15m["reasons"] + s5m["reasons"]

            return {
                "ticker":           ticker,
                "direction":        final_direction,
                "score":            total_score,
                "price":            price_1h,
                "confluence":       confluence_score,
                "regime_aligned":   regime_score == 2,
                "trend_bonus":      trend_bonus,
                "vol_pct":          round(vol_pct, 2),
                "has_squeeze":      has_squeeze,
                "has_uvol":         has_uvol,
                "uvol_bonus":       uvol_bonus,
                "vix_size_mult":    vix_mult,
                "sl_hint":          sl_pct,
                "timeframe_scores": {
                    "1h":  s1h["score"],
                    "15m": s15m["score"],
                    "5m":  s5m["score"],
                },
                "reasons": all_reasons,
            }

        except Exception as e:
            import traceback
            log.warning(f"Signal error {ticker}: {e}\n{traceback.format_exc()}")
            return None

    # ── Top signals ────────────────────────────────────────────────────────────
    def get_top_signals(self, min_score: int = 14) -> list:
        regime = self._market_regime()
        self.last_regime = regime
        log.info(f"📊 Market regime: {regime.upper()}")

        try:
            vix_df = yf.download("^VIX", period="1d", interval="5m",
                                 progress=False, auto_adjust=True)
            vix = float(self._to_series(vix_df["Close"]).iloc[-1])
        except Exception:
            vix = 20.0
        log.info(f"📊 VIX: {vix:.1f} | Size mult: {self._vix_size_multiplier(vix):.2f}x")

        if regime == "neutral":
            watchlist = NEUTRAL_WATCHLIST
            threshold = min_score
            log.info(f"  NEUTRAL regime — {watchlist}, min score {threshold}")
        else:
            watchlist = FULL_WATCHLIST
            threshold = min_score
            log.info(f"  {regime.upper()} regime — {len(watchlist)} tickers, min score {threshold}")

        signals = []
        for ticker in watchlist:
            sig = self.score_ticker(ticker, regime, vix)
            if sig is None:
                continue

            if regime == "bear" and sig["direction"] == "CALL":
                log.info(f"  ⬜ {ticker}: BEAR regime — blocking CALL")
                continue
            if regime == "bull" and sig["direction"] == "PUT":
                log.info(f"  ⬜ {ticker}: BULL regime — blocking PUT")
                continue

            if sig["score"] < threshold:
                log.info(f"  ⬜ {ticker}: score={sig['score']:.1f} below threshold {threshold}")
                continue

            signals.append(sig)
            log.info(
                f"  ✅ {ticker}: score={sig['score']} dir={sig['direction']} "
                f"squeeze={sig['has_squeeze']} uvol={sig['has_uvol']} "
                f"trend_bonus={sig['trend_bonus']} "
                f"vix_mult={sig['vix_size_mult']:.2f}x sl={sig['sl_hint']}% "
                f"tf={sig['timeframe_scores']}"
            )

        signals.sort(key=lambda x: x["score"], reverse=True)
        return signals


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    print("Signal Engine v4.2\n")
    engine  = SignalEngine()
    signals = engine.get_top_signals(min_score=14)
    if not signals:
        print("No signals passed all gates.")
    else:
        for s in signals:
            print(f"\n{s['ticker']} {s['direction']} — score {s['score']}")
            print(f"  Squeeze: {s['has_squeeze']} | UVol: {s['has_uvol']} | Trend bonus: {s['trend_bonus']}")
            print(f"  VIX mult: {s['vix_size_mult']:.2f}x | SL hint: {s['sl_hint']}%")
            for r in s["reasons"][:6]:
                print(f"  {r}")
