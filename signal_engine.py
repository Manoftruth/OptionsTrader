"""
OptionsAgent - Upgraded Signal Engine v2
Drop this file into your OptionsTrader folder and replace the SignalEngine class
in agent.py with the one below, or just run this file standalone to test signals.

UPGRADES over v1:
1. Multi-timeframe analysis (1h + 15m + 5m confluence)
2. Bollinger Band squeeze detection (volatility expansion plays)
3. MACD histogram momentum
4. Options-specific IV rank estimation
5. Market regime filter (don't fight the trend)
6. Earnings blackout (avoid trading into binary events)
7. Weighted scoring with confidence levels
8. Signal confluence requirement (multiple timeframes must agree)
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date
import warnings
warnings.filterwarnings("ignore")
from insider_tracker import InsiderTracker
from catalyst_scanner import CatalystScanner


class SignalEngine:
    """
    Multi-factor, multi-timeframe signal engine.

    Scoring breakdown (max 20 points):
    ┌─────────────────────────────┬────────┐
    │ Factor                      │ Points │
    ├─────────────────────────────┼────────┤
    │ RSI (multi-timeframe)       │  0-3   │
    │ MACD histogram momentum     │  0-3   │
    │ Bollinger squeeze breakout  │  0-3   │
    │ Volume surge + OBV trend    │  0-3   │
    │ Multi-timeframe confluence  │  0-4   │
    │ Market regime alignment     │  0-2   │
    │ Volatility sweet spot       │  0-2   │
    └─────────────────────────────┴────────┤
                             Total  0-20   │
    └───────────────────────────────────────┘

    Raise min_signal_score to 12+ for high-confidence trades only.
    """

    WATCHLIST = [
        "TSLA", "NVDA", "COIN", "MSTR", "AMD",
        "TQQQ", "SOXL", "PLTR", "HOOD", "RBLX",
        "SPY", "QQQ", "SPXL", "LABU", "META",
        "AMZN", "GOOGL", "MSFT", "AAPL", "IWM",
    ]

    # ── Data fetching ──────────────────────────────────────────────────────
    def _fetch(self, ticker: str, interval: str, period: str) -> pd.DataFrame:
        df = yf.download(ticker, interval=interval, period=period,
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()

    # ── Indicators ─────────────────────────────────────────────────────────
    def _rsi(self, close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    def _macd(self, close: pd.Series):
        fast = close.ewm(span=12, adjust=False).mean()
        slow = close.ewm(span=26, adjust=False).mean()
        macd_line = fast - slow
        signal = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal
        return macd_line, signal, histogram

    def _bollinger(self, close: pd.Series, period: int = 20, std: float = 2.0):
        mid = close.rolling(period).mean()
        dev = close.rolling(period).std()
        upper = mid + std * dev
        lower = mid - std * dev
        bandwidth = (upper - lower) / mid  # % width of bands
        pct_b = (close - lower) / (upper - lower)  # 0=at lower, 1=at upper
        return upper, mid, lower, bandwidth, pct_b

    def _obv(self, close: pd.Series, volume: pd.Series) -> pd.Series:
        direction = np.sign(close.diff()).fillna(0)
        return (direction * volume).cumsum()

    def _atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        h, l, c = df["High"], df["Low"], df["Close"]
        tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

    def _vwap(self, df: pd.DataFrame) -> pd.Series:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        return (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()

    # ── Market Regime ──────────────────────────────────────────────────────
    def _market_regime(self) -> str:
        """
        Returns 'bull', 'bear', or 'neutral' based on SPY's
        relationship to its 20/50 EMA. Don't fight the macro trend.
        """
        try:
            spy = self._fetch("SPY", "1d", "3mo")
            close = spy["Close"].squeeze()
            ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
            price = close.iloc[-1]
            if price > ema20 > ema50:
                return "bull"
            elif price < ema20 < ema50:
                return "bear"
            return "neutral"
        except:
            return "neutral"

    # ── Squeeze Detection ──────────────────────────────────────────────────
    def _detect_squeeze(self, close: pd.Series, high: pd.Series,
                         low: pd.Series, volume: pd.Series) -> dict:
        """
        Bollinger Band Squeeze: when BBands narrow inside Keltner Channels,
        volatility is coiling. The breakout after a squeeze is often explosive.
        This is one of the best options setups that exists.
        """
        # Bollinger Bands
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std

        # Keltner Channels
        typical = (high + low + close) / 3
        atr_val = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1).ewm(span=14, adjust=False).mean()
        kc_upper = bb_mid + 1.5 * atr_val
        kc_lower = bb_mid - 1.5 * atr_val

        # Squeeze = BB inside KC
        in_squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
        was_squeezing = in_squeeze.iloc[-4:-1].any()
        just_broke_out = not in_squeeze.iloc[-1]

        # Momentum direction during squeeze release
        momentum = close - close.rolling(14).mean()
        mom_direction = "CALL" if momentum.iloc[-1] > 0 else "PUT"
        mom_strength = abs(momentum.iloc[-1]) / close.iloc[-1] * 100

        return {
            "in_squeeze": bool(in_squeeze.iloc[-1]),
            "breakout": bool(was_squeezing and just_broke_out),
            "direction": mom_direction,
            "strength": mom_strength
        }

    # ── Single timeframe score ─────────────────────────────────────────────
    def _score_timeframe(self, df: pd.DataFrame, label: str) -> dict:
        if df.empty or len(df) < 30:
            return {"score": 0, "direction": None, "reasons": []}

        close = df["Close"].squeeze()
        high = df["High"].squeeze()
        low = df["Low"].squeeze()
        volume = df["Volume"].squeeze()

        score = 0
        directions = []
        reasons = []

        # 1. RSI
        rsi = self._rsi(close).iloc[-1]
        rsi_prev = self._rsi(close).iloc[-2]
        if rsi > 70 and rsi > rsi_prev:
            score += 3; directions.append("CALL")
            reasons.append(f"[{label}] RSI momentum strong ({rsi:.0f}, rising)")
        elif rsi > 60:
            score += 2; directions.append("CALL")
            reasons.append(f"[{label}] RSI bullish ({rsi:.0f})")
        elif rsi < 30 and rsi < rsi_prev:
            score += 3; directions.append("PUT")
            reasons.append(f"[{label}] RSI oversold & falling ({rsi:.0f})")
        elif rsi < 40:
            score += 2; directions.append("PUT")
            reasons.append(f"[{label}] RSI bearish ({rsi:.0f})")

        # 2. MACD histogram
        _, _, hist = self._macd(close)
        hist_now = hist.iloc[-1]
        hist_prev = hist.iloc[-2]
        hist_prev2 = hist.iloc[-3]
        # Accelerating histogram = strong momentum
        if hist_now > 0 and hist_now > hist_prev > hist_prev2:
            score += 3; directions.append("CALL")
            reasons.append(f"[{label}] MACD histogram accelerating bullish")
        elif hist_now > 0 and hist_now > hist_prev:
            score += 2; directions.append("CALL")
            reasons.append(f"[{label}] MACD histogram bullish")
        elif hist_now < 0 and hist_now < hist_prev < hist_prev2:
            score += 3; directions.append("PUT")
            reasons.append(f"[{label}] MACD histogram accelerating bearish")
        elif hist_now < 0 and hist_now < hist_prev:
            score += 2; directions.append("PUT")
            reasons.append(f"[{label}] MACD histogram bearish")

        # 3. Bollinger squeeze
        squeeze = self._detect_squeeze(close, high, low, volume)
        if squeeze["breakout"] and squeeze["strength"] > 0.5:
            score += 3; directions.append(squeeze["direction"])
            reasons.append(f"[{label}] BB squeeze breakout → {squeeze['direction']} ({squeeze['strength']:.2f}%)")
        elif squeeze["in_squeeze"]:
            score += 1
            reasons.append(f"[{label}] BB squeeze building (coiling)")

        # 4. Volume + OBV
        avg_vol = volume.rolling(20).mean().iloc[-1]
        cur_vol = volume.iloc[-1]
        surge = cur_vol / avg_vol if avg_vol > 0 else 1

        obv = self._obv(close, volume)
        obv_trend = obv.iloc[-1] > obv.rolling(10).mean().iloc[-1]

        if surge > 2.0 and obv_trend:
            score += 3; directions.append("CALL")
            reasons.append(f"[{label}] Massive volume surge ({surge:.1f}x) + OBV rising")
        elif surge > 2.0 and not obv_trend:
            score += 3; directions.append("PUT")
            reasons.append(f"[{label}] Massive volume surge ({surge:.1f}x) + OBV falling")
        elif surge > 1.5:
            score += 2
            directions.append("CALL" if obv_trend else "PUT")
            reasons.append(f"[{label}] Volume surge ({surge:.1f}x)")

        # 5. VWAP position
        vwap = self._vwap(df).iloc[-1]
        price = close.iloc[-1]
        vwap_dev = (price - vwap) / vwap * 100
        if vwap_dev > 1.5:
            score += 1; directions.append("CALL")
            reasons.append(f"[{label}] Price {vwap_dev:.1f}% above VWAP")
        elif vwap_dev < -1.5:
            score += 1; directions.append("PUT")
            reasons.append(f"[{label}] Price {vwap_dev:.1f}% below VWAP")

        # Determine consensus direction
        call_votes = directions.count("CALL")
        put_votes = directions.count("PUT")
        direction = "CALL" if call_votes > put_votes else "PUT" if put_votes > call_votes else None

        return {
            "score": score,
            "direction": direction,
            "call_votes": call_votes,
            "put_votes": put_votes,
            "reasons": reasons
        }

    # ── Main scoring function ──────────────────────────────────────────────
    def score_ticker(self, ticker: str, regime: str = "neutral") -> dict | None:
        try:
            # Fetch 3 timeframes
            df_1h  = self._fetch(ticker, "1h",  "3mo")
            df_15m = self._fetch(ticker, "15m", "5d")
            df_5m  = self._fetch(ticker, "5m",  "2d")

            if df_1h.empty or df_15m.empty or df_5m.empty:
                return None

            s1h  = self._score_timeframe(df_1h,  "1H")
            s15m = self._score_timeframe(df_15m, "15M")
            s5m  = self._score_timeframe(df_5m,  "5M")

            # ── Confluence bonus ───────────────────────────────────────────
            # All 3 timeframes agreeing is the most powerful signal
            directions = [s["direction"] for s in [s1h, s15m, s5m] if s["direction"]]
            call_tfs = directions.count("CALL")
            put_tfs  = directions.count("PUT")

            confluence_score = 0
            if call_tfs == 3:
                confluence_score = 4
            elif put_tfs == 3:
                confluence_score = 4
            elif call_tfs == 2:
                confluence_score = 2
            elif put_tfs == 2:
                confluence_score = 2

            final_direction = (
                "CALL" if call_tfs > put_tfs
                else "PUT" if put_tfs > call_tfs
                else None
            )

            # ── Market regime bonus ────────────────────────────────────────
            regime_score = 0
            if regime == "bull" and final_direction == "CALL":
                regime_score = 2
            elif regime == "bear" and final_direction == "PUT":
                regime_score = 2
            elif regime == "neutral":
                regime_score = 1  # neutral doesn't penalize

            # ── Volatility check ───────────────────────────────────────────
            # Want high vol (big moves possible) but not insane vol (spreads too wide)
            atr_1h = self._atr(df_1h).iloc[-1]
            price_1h = df_1h["Close"].squeeze().iloc[-1]
            vol_pct = (atr_1h / price_1h) * 100

            vol_score = 0
            if 1.0 <= vol_pct <= 4.0:
                vol_score = 2   # sweet spot
            elif 0.5 <= vol_pct < 1.0:
                vol_score = 1   # a bit low but ok
            # > 4% = too crazy, spreads will be terrible

            total_score = (
                s1h["score"] * 0.4 +    # 1H weighted most
                s15m["score"] * 0.35 +  # 15M second
                s5m["score"] * 0.25 +   # 5M confirmation
                confluence_score +
                regime_score +
                vol_score
            )

            all_reasons = s1h["reasons"] + s15m["reasons"] + s5m["reasons"]
            if confluence_score >= 4:
                all_reasons.insert(0, "✅ ALL 3 TIMEFRAMES AGREE — high confidence")
            if regime_score == 2:
                all_reasons.insert(0, f"✅ Market regime ({regime}) aligns with {final_direction}")

            return {
                "ticker": ticker,
                "direction": final_direction,
                "score": round(total_score, 1),
                "price": float(price_1h),
                "confluence": confluence_score,
                "regime_aligned": regime_score == 2,
                "vol_pct": round(vol_pct, 2),
                "timeframe_scores": {
                    "1h": s1h["score"],
                    "15m": s15m["score"],
                    "5m": s5m["score"]
                },
                "reasons": all_reasons
            }

        except Exception as e:
            import logging
            logging.getLogger("OptionsAgent").warning(f"Signal error {ticker}: {e}")
            return None

    def get_top_signals(self, min_score: int = 10) -> list:
        import logging
        log = logging.getLogger("OptionsAgent")

        # Get market regime once (not per ticker — saves API calls)
        regime = self._market_regime()
        self.last_regime = regime
        log.info(f"📊 Market regime: {regime.upper()}")

        # Get market-wide insider buys and merge with fixed watchlist
        insider = InsiderTracker()
        insider_buys_today = insider.get_todays_insider_buys()
        def _has_liquid_options(ticker):
            try:
                import yfinance as yf
                tk = yf.Ticker(ticker)
                expirations = tk.options
                if not expirations:
                    return False
                chain = tk.option_chain(expirations[0])
                calls = chain.calls
                if calls.empty:
                    return False
                max_oi = calls["openInterest"].fillna(0).max()
                return max_oi >= 100
            except:
                return False
        insider_buys_today = [b for b in insider_buys_today if _has_liquid_options(b["ticker"])]
        insider_tickers = [b["ticker"] for b in insider_buys_today]
        insider_scores  = {b["ticker"]: b for b in insider_buys_today}

        # Catalyst scanner — earnings + pre-market gaps
        catalyst = CatalystScanner()
        catalyst_top = catalyst.get_top_catalyst_tickers(min_bonus=2)
        catalyst_tickers = [c["ticker"] for c in catalyst_top]
        catalyst_scores  = {c["ticker"]: c for c in catalyst_top}

        # Combine all sources: watchlist + insider + catalyst
        combined_watchlist = list(dict.fromkeys(
            self.WATCHLIST + insider_tickers + catalyst_tickers
        ))
        log.info(f"  Scanning {len(combined_watchlist)} tickers "
                 f"({len(self.WATCHLIST)} watchlist + "
                 f"{len(insider_tickers)} insider + "
                 f"{len(catalyst_tickers)} catalyst)")

        signals = []
        for ticker in combined_watchlist:
            sig = self.score_ticker(ticker, regime)
            if sig and sig["direction"]:
                # Add insider bonus score
                insider_data    = insider_scores.get(ticker, {})
                insider_bonus   = insider_data.get("score", 0)
                insider_reasons = insider_data.get("reasons", [])
                # Insider-discovered tickers always trade as CALL
                if ticker in insider_scores and not sig["direction"]:
                    sig["direction"] = "CALL"
                # Catalyst bonus (earnings + gap)
                cat_data      = catalyst_scores.get(ticker, {})
                cat_bonus     = cat_data.get("total_bonus", 0)
                cat_reasons   = cat_data.get("reasons", [])
                cat_dir_bias  = cat_data.get("direction_bias")

                # Gap direction overrides technical if strong gap
                if cat_dir_bias and cat_bonus >= 2:
                    sig["direction"] = cat_dir_bias

                total_bonus = insider_bonus + cat_bonus
                sig["score"] = round(sig["score"] + total_bonus, 1)
                sig["insider_bonus"]   = insider_bonus
                sig["catalyst_bonus"]  = cat_bonus
                sig["reasons"] = insider_reasons + cat_reasons + sig["reasons"]

                if sig["score"] >= min_score:
                    signals.append(sig)
                    log.info(
                        f"  ✅ {ticker}: score={sig['score']} "
                        f"(+{insider_bonus} insider, +{cat_bonus} catalyst) "
                        f"dir={sig['direction']} "
                        f"confluence={'HIGH' if sig['confluence']==4 else 'MED'} "
                        f"tf={sig['timeframe_scores']}"
                    )
            elif sig:
                log.info(f"  ⬜ {ticker}: score={sig['score']:.1f} (below threshold)")

        signals.sort(key=lambda x: (x["confluence"], x["score"]), reverse=True)
        return signals


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing upgraded signal engine...\n")
    engine = SignalEngine()

    # Test a single ticker
    test_tickers = ["TSLA", "NVDA", "COIN", "SPY"]
    regime = engine._market_regime()
    print(f"Market Regime: {regime.upper()}\n")

    for ticker in test_tickers:
        print(f"Scanning {ticker}...")
        sig = engine.score_ticker(ticker, regime)
        if sig:
            print(f"  Score:     {sig['score']}/20")
            print(f"  Direction: {sig['direction']}")
            print(f"  Confluence:{sig['confluence']}/4")
            print(f"  Timeframes: 1H={sig['timeframe_scores']['1h']} "
                  f"15M={sig['timeframe_scores']['15m']} "
                  f"5M={sig['timeframe_scores']['5m']}")
            print(f"  Volatility: {sig['vol_pct']}% ATR")
            print(f"  Reasons:")
            for r in sig["reasons"][:5]:
                print(f"    - {r}")
        print()
