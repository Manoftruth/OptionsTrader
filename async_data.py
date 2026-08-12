"""
async_data.py — Asynchronous market data layer for OptionsAgent
================================================================

Replaces the serial yfinance calls in the scan path with concurrent aiohttp
requests straight to Yahoo's chart/options JSON endpoints.

Why this exists
---------------
The old scan did ~5 blocking network calls per ticker (3 bar downloads +
earnings calendar + option chain), strictly serial, for 9 tickers. That is
~45 round trips end to end. At 200-400 ms each that is 15-30 s of pure wait
per cycle, during which the agent is blind. Concurrency turns that into
roughly the latency of the single slowest request.

Design decisions worth knowing
------------------------------
1. **Direct endpoints first, yfinance fallback second.** yfinance is not
   async and never will be cleanly. So the fast path talks to
   query2.finance.yahoo.com directly. But Yahoo breaks/rotates these
   endpoints without notice, and this agent trades real money — so every
   method falls back to the equivalent yfinance call executed in a worker
   thread via ``asyncio.to_thread``. The fallback is still concurrent; it
   is just thread-backed instead of event-loop-backed. You lose speed, not
   data.

2. **One session, one cookie/crumb bootstrap, shared across the scan.**
   Yahoo hands out a cookie + crumb pair; re-fetching it per request is what
   triggers the 401/"Invalid Crumb" errors the old code kept retrying past.

3. **A semaphore caps in-flight requests.** Hammering Yahoo with 50
   simultaneous requests gets you rate limited (429) and then you have no
   data at all, which is worse than being slow. Default 8.

4. **Every public coroutine is failure-tolerant.** A dead ticker returns an
   empty DataFrame / empty dict, never an exception that kills the gather.

Requires: aiohttp>=3.9
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

log = logging.getLogger("OptionsAgent")

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False
    log.warning("aiohttp not installed — async data layer will run in "
                "thread-fallback mode only. `pip install aiohttp`")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_CHART_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")

# ── yfinance is NOT thread-safe ────────────────────────────────────────────────
# v5.2 CRITICAL FIX. yfinance keeps global session/cache state, so calling
# yf.download() from several threads at once can return one ticker's frame for
# another ticker's request. Running the fallback under asyncio.to_thread made
# exactly that happen: on a rate-limited host the direct HTTP path fails, every
# ticker falls back to yfinance simultaneously, and the frames cross-assign.
#
# Observed in production on 2026-08-12: the regime detector reported SPY at
# 14.79 (VIX's value) and 93.07 (TLT's value) in consecutive runs while SPY was
# actually 772.16, and VIX at 79.6 while it was 14.8. That fabricated a 3-vote
# CRASH classification on a calm tape.
#
# The fallback is now serialised behind this lock. Slower, correct.
_YF_LOCK = threading.Lock()


class AsyncMarketData:
    """Concurrent market data fetcher with a synchronous convenience wrapper."""

    def __init__(self, max_concurrency: int = 8, timeout_secs: float = 12.0,
                 cache_ttl: float = 45.0, tradier=None):
        # v5.3: optional TradierData instance. When present it is tried FIRST
        # for every symbol, with Yahoo kept as a per-symbol fallback. Tradier
        # is authenticated and does not throttle data-center IPs, which is what
        # made Yahoo unusable from EC2 (~10s per request) and what triggered
        # the v5.2 frame-corruption incident.
        self.tradier = tradier
        self.source_log: dict[str, str] = {}
        self.max_concurrency = max_concurrency
        self.timeout_secs = timeout_secs
        self.cache_ttl = cache_ttl
        self._session: "aiohttp.ClientSession | None" = None
        self._sem: asyncio.Semaphore | None = None
        self._crumb: str | None = None
        self._crumb_fetched_at: float = 0.0
        # (key) -> (timestamp, value). Guards against re-fetching SPY five
        # times in one cycle because five different code paths want it.
        self._cache: dict[str, tuple[float, Any]] = {}

    # ── session lifecycle ──────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncMarketData":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if not AIOHTTP_AVAILABLE:
            self._sem = asyncio.Semaphore(self.max_concurrency)
            return
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_secs)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": _UA, "Accept": "application/json"},
                connector=aiohttp.TCPConnector(limit=self.max_concurrency * 2,
                                               ttl_dns_cache=300),
            )
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── low level ──────────────────────────────────────────────────────────

    async def _ensure_crumb(self) -> None:
        """Bootstrap Yahoo cookie + crumb once per ~30 min.

        Failure here is non-fatal: the chart endpoint usually works without a
        crumb, and the options endpoint will fall back to yfinance.
        """
        if not AIOHTTP_AVAILABLE or self._session is None:
            return
        if self._crumb and (time.time() - self._crumb_fetched_at) < 1800:
            return
        try:
            async with self._session.get("https://fc.yahoo.com") as r:
                await r.read()  # sets the A1/A3 cookies
            async with self._session.get(
                    "https://query2.finance.yahoo.com/v1/test/getcrumb") as r:
                if r.status == 200:
                    crumb = (await r.text()).strip()
                    if crumb and len(crumb) < 32 and "<" not in crumb:
                        self._crumb = crumb
                        self._crumb_fetched_at = time.time()
                        log.debug("Yahoo crumb acquired")
        except Exception as e:
            log.debug(f"Crumb bootstrap failed (non-fatal): {e}")

    async def _get_json(self, url: str, params: dict | None = None,
                        retries: int = 2) -> dict | None:
        if not AIOHTTP_AVAILABLE or self._session is None:
            return None
        assert self._sem is not None
        params = dict(params or {})
        if self._crumb:
            params.setdefault("crumb", self._crumb)
        for attempt in range(retries):
            try:
                async with self._sem:
                    async with self._session.get(url, params=params) as r:
                        if r.status == 200:
                            return await r.json(content_type=None)
                        if r.status in (401, 403) and attempt == 0:
                            # crumb went stale — force a refresh and retry once
                            self._crumb = None
                            await self._ensure_crumb()
                            continue
                        if r.status == 429:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        log.debug(f"HTTP {r.status} for {url}")
                        return None
            except asyncio.TimeoutError:
                log.debug(f"Timeout on {url} (attempt {attempt + 1})")
            except Exception as e:
                log.debug(f"Request error {url}: {e}")
            await asyncio.sleep(0.4 * (attempt + 1))
        return None

    # ── cache ──────────────────────────────────────────────────────────────

    def _cache_get(self, key: str):
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < self.cache_ttl:
            return hit[1]
        return None

    def _cache_put(self, key: str, value) -> None:
        self._cache[key] = (time.time(), value)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── bars ───────────────────────────────────────────────────────────────

    @staticmethod
    def _chart_to_frame(payload: dict, expect_symbol: str | None = None) -> pd.DataFrame:
        """Yahoo chart JSON → OHLCV DataFrame identical in shape to yfinance.

        v5.2: verifies the payload's own symbol against what we asked for.
        This is defence in depth — it catches a mismatch no matter where it
        came from (connection reuse, a redirect, a cached proxy response, a
        library bug). Silently accepting someone else's price series is the
        worst possible failure for a trading system: everything downstream
        looks healthy and the numbers are simply wrong.
        """
        try:
            result = payload["chart"]["result"][0]

            if expect_symbol:
                got = (result.get("meta") or {}).get("symbol")
                if got and got.upper() != expect_symbol.upper():
                    log.error(
                        f"SYMBOL MISMATCH: asked for {expect_symbol}, Yahoo "
                        f"returned {got}. Discarding — refusing to trade on it."
                    )
                    return pd.DataFrame()

            ts = result.get("timestamp")
            quote = result["indicators"]["quote"][0]
            if not ts:
                return pd.DataFrame()
            df = pd.DataFrame({
                "Open":   quote.get("open"),
                "High":   quote.get("high"),
                "Low":    quote.get("low"),
                "Close":  quote.get("close"),
                "Volume": quote.get("volume"),
            }, index=pd.to_datetime(ts, unit="s", utc=True))
            # Prefer split/dividend adjusted closes when Yahoo supplies them,
            # matching yfinance's auto_adjust=True behaviour.
            adj = result["indicators"].get("adjclose")
            if adj and adj[0].get("adjclose"):
                adj_series = pd.Series(adj[0]["adjclose"], index=df.index)
                if adj_series.notna().any():
                    ratio = (adj_series / df["Close"]).replace(
                        [float("inf"), float("-inf")], pd.NA)
                    for col in ("Open", "High", "Low", "Close"):
                        df[col] = df[col] * ratio
            return df.dropna()
        except Exception as e:
            log.debug(f"Chart parse error: {e}")
            return pd.DataFrame()

    async def get_bars(self, ticker: str, interval: str, period: str) -> pd.DataFrame:
        """Concurrent replacement for ``yf.download(ticker, interval, period)``."""
        key = f"bars:{ticker}:{interval}:{period}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # ── Tradier first ──────────────────────────────────────────────────
        if self.tradier is not None:
            try:
                df = await self.tradier.get_bars(ticker, interval, period)
                if not df.empty:
                    self.source_log[f"{ticker}:{interval}"] = "tradier"
                    self._cache_put(key, df)
                    return df
                log.debug(f"Tradier had no {interval} data for {ticker} — "
                          f"falling back to Yahoo")
            except Exception as e:
                log.debug(f"Tradier bars failed for {ticker}: {e}")

        payload = None
        for host in _CHART_HOSTS:
            payload = await self._get_json(
                f"{host}/v8/finance/chart/{ticker}",
                {"interval": interval, "range": period,
                 "includePrePost": "false", "events": "div,splits"},
            )
            if payload:
                break

        df = self._chart_to_frame(payload, expect_symbol=ticker) if payload else pd.DataFrame()

        if df.empty:
            df = await self._yf_bars_fallback(ticker, interval, period)

        if not df.empty:
            self.source_log[f"{ticker}:{interval}"] = "yahoo"
            self._cache_put(key, df)
        return df

    @staticmethod
    async def _yf_bars_fallback(ticker: str, interval: str,
                                period: str) -> pd.DataFrame:
        """Thread-backed yfinance fallback. Slower, but never leaves us blind."""
        def _sync() -> pd.DataFrame:
            try:
                import yfinance as yf
                # Serialised: see the _YF_LOCK note at the top of this module.
                with _YF_LOCK:
                    df = yf.download(ticker, interval=interval, period=period,
                                     progress=False, auto_adjust=True)
                if df is None or df.empty:
                    return pd.DataFrame()
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df.dropna()
            except Exception as e:
                log.debug(f"yfinance fallback failed {ticker} {interval}: {e}")
                return pd.DataFrame()

        try:
            df = await asyncio.to_thread(_sync)
            if not df.empty:
                log.debug(f"Used yfinance fallback for {ticker} {interval}")
            return df
        except Exception:
            return pd.DataFrame()

    async def get_many_bars(self, requests_: Iterable[tuple[str, str, str]]
                            ) -> dict[tuple[str, str, str], pd.DataFrame]:
        """Fetch many (ticker, interval, period) triples concurrently."""
        reqs = list(requests_)
        results = await asyncio.gather(
            *(self.get_bars(t, i, p) for t, i, p in reqs),
            return_exceptions=True,
        )
        out: dict[tuple[str, str, str], pd.DataFrame] = {}
        for req, res in zip(reqs, results):
            out[req] = res if isinstance(res, pd.DataFrame) else pd.DataFrame()
            if isinstance(res, Exception):
                log.debug(f"Bar fetch exception for {req}: {res}")
        return out

    # ── option chains ──────────────────────────────────────────────────────

    async def get_option_chain(self, ticker: str, expiration: int | None = None
                               ) -> dict:
        """Return ``{"expirations": [...], "calls": DataFrame, "puts": DataFrame}``.

        ``expiration`` is a unix epoch from the ``expirations`` list; ``None``
        means nearest expiry.
        """
        key = f"chain:{ticker}:{expiration}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        if self.tradier is not None:
            try:
                out = await self.tradier.get_option_chain(ticker)
                if out and (not out["calls"].empty or not out["puts"].empty):
                    self._cache_put(key, out)
                    return out
            except Exception as e:
                log.debug(f"Tradier chain failed for {ticker}: {e}")

        params = {}
        if expiration:
            params["date"] = str(expiration)
        payload = None
        for host in _CHART_HOSTS:
            payload = await self._get_json(f"{host}/v7/finance/options/{ticker}",
                                           params)
            if payload:
                break

        out = self._parse_chain(payload, expect_symbol=ticker) if payload else {}
        if not out or (out["calls"].empty and out["puts"].empty):
            out = await self._yf_chain_fallback(ticker)

        if out and (not out["calls"].empty or not out["puts"].empty):
            self._cache_put(key, out)
        return out or {"expirations": [], "calls": pd.DataFrame(),
                       "puts": pd.DataFrame(), "spot": 0.0}

    @staticmethod
    def _parse_chain(payload: dict, expect_symbol: str | None = None) -> dict:
        try:
            res = payload["optionChain"]["result"][0]
            if expect_symbol:
                got = res.get("underlyingSymbol") or (res.get("quote") or {}).get("symbol")
                if got and got.upper() != expect_symbol.upper():
                    log.error(f"CHAIN SYMBOL MISMATCH: asked {expect_symbol}, got {got}")
                    return {}
            expirations = res.get("expirationDates", []) or []
            spot = float(res.get("quote", {}).get("regularMarketPrice", 0) or 0)
            opts = res.get("options") or [{}]
            calls = pd.DataFrame(opts[0].get("calls", []) or [])
            puts = pd.DataFrame(opts[0].get("puts", []) or [])
            for frame in (calls, puts):
                for col in ("volume", "openInterest", "bid", "ask",
                            "impliedVolatility", "strike", "lastPrice"):
                    if col in frame.columns:
                        frame[col] = pd.to_numeric(frame[col],
                                                   errors="coerce").fillna(0.0)
                    else:
                        frame[col] = 0.0
            return {"expirations": expirations, "calls": calls,
                    "puts": puts, "spot": spot}
        except Exception as e:
            log.debug(f"Chain parse error: {e}")
            return {}

    @staticmethod
    async def _yf_chain_fallback(ticker: str) -> dict:
        def _sync() -> dict:
            try:
                import yfinance as yf
                with _YF_LOCK:
                    tk = yf.Ticker(ticker)
                    exps = tk.options
                    if not exps:
                        return {}
                    chain = tk.option_chain(exps[0])
                epochs = []
                for e in exps:
                    try:
                        epochs.append(int(datetime.strptime(e, "%Y-%m-%d")
                                          .replace(tzinfo=timezone.utc).timestamp()))
                    except Exception:
                        pass
                calls = chain.calls.copy()
                puts = chain.puts.copy()
                for frame in (calls, puts):
                    for col in ("volume", "openInterest", "bid", "ask",
                                "impliedVolatility", "strike"):
                        if col in frame.columns:
                            frame[col] = pd.to_numeric(
                                frame[col], errors="coerce").fillna(0.0)
                        else:
                            frame[col] = 0.0
                return {"expirations": epochs, "calls": calls,
                        "puts": puts, "spot": 0.0}
            except Exception as e:
                log.debug(f"yfinance chain fallback failed {ticker}: {e}")
                return {}

        try:
            return await asyncio.to_thread(_sync)
        except Exception:
            return {}

    # ── quotes ─────────────────────────────────────────────────────────────

    async def get_quotes(self, tickers: list[str]) -> dict[str, float]:
        """Batch last-price lookup. One request for the whole list."""
        if not tickers:
            return {}
        payload = None
        for host in _CHART_HOSTS:
            payload = await self._get_json(f"{host}/v7/finance/quote",
                                           {"symbols": ",".join(tickers)})
            if payload:
                break
        out: dict[str, float] = {}
        try:
            for q in payload["quoteResponse"]["result"]:
                sym = q.get("symbol")
                px = q.get("regularMarketPrice")
                if sym and px is not None:
                    out[sym] = float(px)
        except Exception:
            pass
        # Fill anything the batch endpoint missed using 1d bars.
        missing = [t for t in tickers if t not in out]
        if missing:
            frames = await self.get_many_bars((t, "1d", "5d") for t in missing)
            for (t, _, _), df in frames.items():
                if not df.empty:
                    try:
                        out[t] = float(df["Close"].iloc[-1])
                    except Exception:
                        pass
        return out

    # ── earnings ───────────────────────────────────────────────────────────

    async def has_earnings_within(self, ticker: str, days: int = 2) -> bool:
        """True if an earnings date falls within ``days``. Fails open (False)."""
        key = f"earn:{ticker}:{days}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        payload = None
        for host in _CHART_HOSTS:
            payload = await self._get_json(
                f"{host}/v10/finance/quoteSummary/{ticker}",
                {"modules": "calendarEvents"},
            )
            if payload:
                break
        result = False
        try:
            ev = (payload["quoteSummary"]["result"][0]["calendarEvents"]
                  ["earnings"]["earningsDate"])
            now = datetime.now(timezone.utc)
            for d in ev:
                raw = d.get("raw") if isinstance(d, dict) else d
                if raw is None:
                    continue
                when = datetime.fromtimestamp(int(raw), tz=timezone.utc)
                if abs((when - now).days) <= days:
                    result = True
                    break
        except Exception:
            result = False
        self._cache_put(key, result)
        return result

    # ── synchronous bridge ─────────────────────────────────────────────────

    def run(self, coro):
        """Run a coroutine from synchronous code, managing session lifecycle.

        Safe to call from the agent's normal blocking loop. Do NOT call from
        inside a running event loop.
        """
        async def _wrapped():
            await self.open()
            await self._ensure_crumb()
            try:
                return await coro
            finally:
                await self.close()
        return asyncio.run(_wrapped())


# ── Async Tradier client ───────────────────────────────────────────────────────

class AsyncTradierClient:
    """Async read-only Tradier calls (quotes, chains, expirations).

    Order placement deliberately stays on the synchronous client in agent.py.
    Concurrency plus order submission plus shared capital accounting is how
    you end up double-filling a position. Reads are parallel; writes are not.
    """

    def __init__(self, token: str, account_id: str, sandbox: bool = True,
                 max_concurrency: int = 6, timeout_secs: float = 10.0):
        self.base = ("https://sandbox.tradier.com/v1" if sandbox
                     else "https://api.tradier.com/v1")
        self.account_id = account_id
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/json"}
        self.max_concurrency = max_concurrency
        self.timeout_secs = timeout_secs
        self._session: "aiohttp.ClientSession | None" = None
        self._sem: asyncio.Semaphore | None = None

    async def open(self) -> None:
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp required for AsyncTradierClient")
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

    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self._session is not None and self._sem is not None
        try:
            async with self._sem:
                async with self._session.get(f"{self.base}{path}",
                                             params=params) as r:
                    if r.status != 200:
                        log.warning(f"Tradier HTTP {r.status} on {path}")
                        return {}
                    return await r.json(content_type=None)
        except Exception as e:
            log.warning(f"Tradier async error on {path}: {e}")
            return {}

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Batch option/equity quotes — one call for up to a few hundred symbols."""
        if not symbols:
            return {}
        data = await self._get("/markets/quotes",
                               {"symbols": ",".join(symbols), "greeks": "true"})
        quotes = data.get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        return {q.get("symbol"): q for q in quotes if q.get("symbol")}

    async def get_expirations(self, symbol: str) -> list[str]:
        data = await self._get("/markets/options/expirations", {"symbol": symbol})
        dates = data.get("expirations", {}).get("date", [])
        if isinstance(dates, str):
            dates = [dates]
        return dates or []

    async def get_chain(self, symbol: str, expiration: str) -> list[dict]:
        data = await self._get("/markets/options/chains",
                               {"symbol": symbol, "expiration": expiration,
                                "greeks": "true"})
        opts = (data.get("options") or {}).get("option", [])
        if isinstance(opts, dict):
            opts = [opts]
        return opts or []

    async def get_chains_many(self, pairs: list[tuple[str, str]]
                              ) -> dict[tuple[str, str], list[dict]]:
        """Fetch several (symbol, expiration) chains at once."""
        res = await asyncio.gather(*(self.get_chain(s, e) for s, e in pairs),
                                   return_exceptions=True)
        return {p: (r if isinstance(r, list) else []) for p, r in zip(pairs, res)}


__all__ = ["AsyncMarketData", "AsyncTradierClient", "AIOHTTP_AVAILABLE"]
