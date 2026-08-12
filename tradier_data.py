"""
tradier_data.py — market data from Tradier instead of Yahoo
============================================================

Why
---
Yahoo rate-limits data-center IPs hard. On EC2 a single SPY daily fetch took
9-10 seconds and a full watchlist scan took ~51s, versus sub-second on a
residential connection. That is not merely slow — it is what triggered the
v5.2 data-corruption incident: the direct HTTP path failed under throttling,
every ticker fell back to yfinance simultaneously, and yfinance's non-
thread-safe global state cross-assigned frames between symbols. The regime
detector read SPY at 14.79 and VIX at 79.6, and called a CRASH on a tape with
VIX at 14.8.

You already pay for a Tradier brokerage account, and Tradier serves the same
data under an authenticated, rate-limit-documented API. Moving the equity and
option requests there removes the throttling, and with it an entire class of
failure.

Endpoint mapping
----------------
The agent asks for bars as ``(interval, period)`` — a yfinance-shaped
contract. Tradier splits this across two endpoints:

    1d   → /markets/history  interval=daily     (full lifetime available)
    15m  → /markets/timesales interval=15min    (40 days, open session)
    5m   → /markets/timesales interval=5min     (40 days, open session)
    1m   → /markets/timesales interval=1min     (20 days, open session)
    1h   → /markets/timesales interval=15min, resampled to 1h

There is no native hourly interval, hence the resample. The signal engine asks
for ``1h`` over ``3mo``; 40 days of 15-minute bars aggregates to roughly 260
hourly bars, and the indicators need 30. So the shorter lookback costs
nothing that matters.

Output is byte-compatible with AsyncMarketData: a DataFrame indexed by UTC
timestamps with Open/High/Low/Close/Volume columns. Nothing downstream of
``get_bars`` needs to know which vendor answered.

Indices
-------
Tradier's coverage is equities, ETFs and options. ``^VIX``, ``^VIX3M``,
``^TNX`` and ``^IRX`` may not resolve. Rather than guess, every symbol is
attempted on Tradier and falls back to Yahoo on an empty result — and the
source actually used is recorded in ``sources`` so preflight can show you
which vendor served what. Four index requests per cycle is far below any
throttling threshold, so the fallback is sustainable rather than a liability.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

log = logging.getLogger("OptionsAgent")

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False

# Tradier drops the caret Yahoo uses for indices. Attempted, not assumed.
INDEX_ALIASES = {"^VIX": "VIX", "^VIX3M": "VIX3M", "^TNX": "TNX", "^IRX": "IRX"}

# interval → (tradier endpoint, tradier interval, resample rule)
_INTERVAL_MAP: dict[str, tuple[str, str, str | None]] = {
    "1d":  ("history",   "daily", None),
    "1wk": ("history",   "weekly", None),
    "1mo": ("history",   "monthly", None),
    "1h":  ("timesales", "15min", "1h"),
    "60m": ("timesales", "15min", "1h"),
    "30m": ("timesales", "15min", "30min"),
    "15m": ("timesales", "15min", None),
    "5m":  ("timesales", "5min", None),
    "1m":  ("timesales", "1min", None),
}

# Documented Tradier intraday retention, open-session (days).
_MAX_INTRADAY_DAYS = {"1min": 20, "5min": 40, "15min": 40}


def _period_to_days(period: str) -> int:
    """'3mo' → 90, '5d' → 5, '1y' → 365."""
    p = period.strip().lower()
    try:
        if p.endswith("mo"):
            return int(float(p[:-2]) * 30)
        if p.endswith("y"):
            return int(float(p[:-1]) * 365)
        if p.endswith("d"):
            return int(float(p[:-1]))
        if p.endswith("wk"):
            return int(float(p[:-2]) * 7)
    except ValueError:
        pass
    return 30


class TradierData:
    """Async Tradier market-data client shaped like AsyncMarketData."""

    def __init__(self, token: str, sandbox: bool = False,
                 max_concurrency: int = 6, timeout_secs: float = 15.0):
        self.base = ("https://sandbox.tradier.com/v1" if sandbox
                     else "https://api.tradier.com/v1")
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/json"}
        self.max_concurrency = max_concurrency
        self.timeout_secs = timeout_secs
        self._session: "aiohttp.ClientSession | None" = None
        self._sem: asyncio.Semaphore | None = None
        # symbol -> "tradier" | "yahoo-fallback". Diagnostics only.
        self.sources: dict[str, str] = {}

    async def open(self) -> None:
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp required for TradierData")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_secs),
                headers=self.headers,
            )
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ── low level ──────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict) -> dict | None:
        if self._session is None or self._sem is None:
            return None
        try:
            async with self._sem:
                async with self._session.get(f"{self.base}{path}",
                                             params=params) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status == 401:
                        log.error("Tradier 401 — check tradier_token and the "
                                  "sandbox flag (they must match).")
                        return None
                    log.debug(f"Tradier HTTP {r.status} on {path} {params}")
                    return None
        except asyncio.TimeoutError:
            log.debug(f"Tradier timeout on {path} {params}")
        except Exception as e:
            log.debug(f"Tradier error on {path}: {e}")
        return None

    # ── parsing ────────────────────────────────────────────────────────────

    @staticmethod
    def _as_list(v) -> list:
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    def _history_to_frame(self, payload: dict) -> pd.DataFrame:
        try:
            days = self._as_list((payload.get("history") or {}).get("day"))
            if not days:
                return pd.DataFrame()
            df = pd.DataFrame(days)
            idx = pd.to_datetime(df["date"], utc=True)
            out = pd.DataFrame({
                "Open":   pd.to_numeric(df["open"], errors="coerce"),
                "High":   pd.to_numeric(df["high"], errors="coerce"),
                "Low":    pd.to_numeric(df["low"], errors="coerce"),
                "Close":  pd.to_numeric(df["close"], errors="coerce"),
                "Volume": pd.to_numeric(df.get("volume", 0), errors="coerce"),
            })
            out.index = idx
            return out.dropna(subset=["Close"]).sort_index()
        except Exception as e:
            log.debug(f"Tradier history parse error: {e}")
            return pd.DataFrame()

    def _timesales_to_frame(self, payload: dict) -> pd.DataFrame:
        try:
            rows = self._as_list((payload.get("series") or {}).get("data"))
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            # Prefer the epoch field — unambiguous, no timezone guessing.
            if "timestamp" in df.columns:
                idx = pd.to_datetime(pd.to_numeric(df["timestamp"],
                                                   errors="coerce"),
                                     unit="s", utc=True)
            else:
                idx = pd.to_datetime(df["time"], utc=True)
            out = pd.DataFrame({
                "Open":   pd.to_numeric(df.get("open"), errors="coerce"),
                "High":   pd.to_numeric(df.get("high"), errors="coerce"),
                "Low":    pd.to_numeric(df.get("low"), errors="coerce"),
                "Close":  pd.to_numeric(df.get("close", df.get("price")),
                                        errors="coerce"),
                "Volume": pd.to_numeric(df.get("volume", 0), errors="coerce"),
            })
            out.index = idx
            return out.dropna(subset=["Close"]).sort_index()
        except Exception as e:
            log.debug(f"Tradier timesales parse error: {e}")
            return pd.DataFrame()

    @staticmethod
    def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
        if df.empty:
            return df
        try:
            out = df.resample(rule).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            })
            return out.dropna(subset=["Close"])
        except Exception as e:
            log.debug(f"Resample to {rule} failed: {e}")
            return df

    # ── public ─────────────────────────────────────────────────────────────

    async def get_bars(self, ticker: str, interval: str,
                       period: str) -> pd.DataFrame:
        """Same contract as AsyncMarketData.get_bars. Empty frame on failure."""
        endpoint, tv_interval, resample_rule = _INTERVAL_MAP.get(
            interval, ("timesales", "5min", None))

        symbol = INDEX_ALIASES.get(ticker, ticker)
        days = _period_to_days(period)
        now = datetime.now(timezone.utc)

        if endpoint == "history":
            start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            payload = await self._get("/markets/history", {
                "symbol": symbol, "interval": tv_interval,
                "start": start, "end": now.strftime("%Y-%m-%d"),
            })
            df = self._history_to_frame(payload) if payload else pd.DataFrame()
        else:
            # Clamp to what Tradier actually retains. Asking for 90 days of
            # 15-minute bars silently returns 40; being explicit keeps the
            # logs honest about what the indicators are seeing.
            cap = _MAX_INTRADAY_DAYS.get(tv_interval, 40)
            span = min(days, cap)
            if days > cap:
                log.debug(f"{ticker} {interval}/{period}: clamped to {cap}d "
                          f"(Tradier {tv_interval} retention)")
            start = (now - timedelta(days=span)).strftime("%Y-%m-%d %H:%M")
            payload = await self._get("/markets/timesales", {
                "symbol": symbol, "interval": tv_interval,
                "start": start, "end": now.strftime("%Y-%m-%d %H:%M"),
                "session_filter": "open",
            })
            df = self._timesales_to_frame(payload) if payload else pd.DataFrame()
            if resample_rule and not df.empty:
                df = self._resample(df, resample_rule)

        self.sources[ticker] = "tradier" if not df.empty else "unavailable"
        return df

    async def get_many_bars(self, requests_) -> dict:
        reqs = list(requests_)
        res = await asyncio.gather(*(self.get_bars(t, i, p) for t, i, p in reqs),
                                   return_exceptions=True)
        return {r: (v if isinstance(v, pd.DataFrame) else pd.DataFrame())
                for r, v in zip(reqs, res)}

    async def get_option_chain(self, ticker: str,
                               expiration: str | None = None) -> dict:
        """Chain in AsyncMarketData's shape: expirations / calls / puts / spot."""
        symbol = INDEX_ALIASES.get(ticker, ticker)
        if expiration is None:
            exp_payload = await self._get("/markets/options/expirations",
                                          {"symbol": symbol})
            dates = self._as_list(
                (exp_payload or {}).get("expirations", {}).get("date"))
            if not dates:
                return {"expirations": [], "calls": pd.DataFrame(),
                        "puts": pd.DataFrame(), "spot": 0.0}
            expiration = sorted(dates)[0]
            all_exps = sorted(dates)
        else:
            all_exps = [expiration]

        payload = await self._get("/markets/options/chains", {
            "symbol": symbol, "expiration": expiration, "greeks": "true"})
        opts = self._as_list((payload or {}).get("options", {}).get("option"))
        if not opts:
            return {"expirations": all_exps, "calls": pd.DataFrame(),
                    "puts": pd.DataFrame(), "spot": 0.0}

        df = pd.DataFrame(opts)
        # Normalise to the Yahoo-ish column names the signal engine expects.
        df["openInterest"] = pd.to_numeric(df.get("open_interest", 0),
                                           errors="coerce").fillna(0)
        for col in ("volume", "bid", "ask", "strike"):
            df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0)

        kind = df.get("option_type", pd.Series(dtype=str)).astype(str).str.lower()
        return {
            "expirations": all_exps,
            "calls": df[kind == "call"].copy(),
            "puts":  df[kind == "put"].copy(),
            "spot":  0.0,
            "raw":   opts,          # for the deep-ITM / spread selectors
        }

    async def has_earnings_within(self, ticker: str, days: int = 2) -> bool:
        """Tradier has no earnings calendar. Fails open — the caller falls back."""
        return False

    async def get_quotes(self, tickers: list) -> dict:
        if not tickers:
            return {}
        syms = [INDEX_ALIASES.get(t, t) for t in tickers]
        payload = await self._get("/markets/quotes",
                                  {"symbols": ",".join(syms), "greeks": "false"})
        quotes = self._as_list((payload or {}).get("quotes", {}).get("quote"))
        out = {}
        rev = {INDEX_ALIASES.get(t, t): t for t in tickers}
        for q in quotes:
            sym = q.get("symbol")
            last = q.get("last")
            if sym and last is not None:
                out[rev.get(sym, sym)] = float(last)
        return out


__all__ = ["TradierData", "INDEX_ALIASES"]
