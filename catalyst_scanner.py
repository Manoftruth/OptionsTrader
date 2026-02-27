"""
CatalystScanner - Earnings Calendar + Pre-Market Gap Scanner

Two of the highest-probability options setups:

1. EARNINGS CATALYST
   Options premiums inflate before earnings (IV expansion).
   Big moves happen post-earnings (IV crush = sell AFTER, buy BEFORE).
   Strategy: buy options 1-3 days before earnings, exit before/at announcement.
   Bonus points for tickers reporting in next 1-3 days.

2. PRE-MARKET GAP
   Stocks gapping 3%+ pre-market continue in the same direction ~65% of the time.
   Volume confirms the move. First 30 minutes of trading = highest momentum.
   Strategy: buy calls on gap-ups, puts on gap-downs, ride the continuation.
   Bonus points for large pre-market gaps with volume confirmation.

Combined scoring adds up to +6 bonus points on top of everything else.
"""

import requests
import logging
import re
import time
import json
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd

log = logging.getLogger("OptionsAgent")

SEC_HEADERS = {"User-Agent": "OptionsAgent research contact@example.com"}

_EARNINGS_CACHE: dict = {}
_GAP_CACHE: dict = {}
CACHE_TTL = 1800  # 30 min cache


class CatalystScanner:
    ETF_SKIPLIST = {
        "SPY","QQQ","IWM","GLD","SLV","XLF","XLE","XLK","XLV","XLI",
        "TQQQ","SOXL","SPXL","LABU","UVXY","ARKK","SQQQ","TLT","HYG",
        "VXX","VIXY","SVXY","SDOW","UDOW","UPRO","SPXU","SPXS","NDAQ","PARA", "SQ", 
    }

    # ── Earnings Calendar ──────────────────────────────────────────────────
    def get_earnings_dates(self, tickers: list) -> dict:
        """
        Returns dict of ticker -> days_until_earnings (None if not found).
        Uses yfinance which pulls from Yahoo Finance earnings calendar.
        """
        now = time.time()
        results = {}

        for ticker in tickers:
            if ticker in self.ETF_SKIPLIST:
                results[ticker] = None
                continue
            # Check cache
            if ticker in _EARNINGS_CACHE:
                cached_time, cached_val = _EARNINGS_CACHE[ticker]
                if now - cached_time < CACHE_TTL:
                    results[ticker] = cached_val
                    continue

            try:
                stock = yf.Ticker(ticker)
                cal = stock.calendar

                days = None
                if cal is not None and not cal.empty:
                    # calendar is a DataFrame with columns like 'Earnings Date'
                    if hasattr(cal, 'columns'):
                        for col in cal.columns:
                            if "earnings" in col.lower() or "date" in col.lower():
                                val = cal[col].iloc[0] if len(cal) > 0 else None
                                if val is not None:
                                    try:
                                        if hasattr(val, 'date'):
                                            earn_date = val.date()
                                        else:
                                            earn_date = pd.Timestamp(val).date()
                                        today = datetime.now().date()
                                        days = (earn_date - today).days
                                    except:
                                        pass
                                break
                    # Sometimes calendar is a dict-like object
                    elif hasattr(cal, 'get'):
                        earn_date_raw = cal.get("Earnings Date", [None])[0]
                        if earn_date_raw:
                            try:
                                earn_date = pd.Timestamp(earn_date_raw).date()
                                today = datetime.now().date()
                                days = (earn_date - today).days
                            except:
                                pass

                results[ticker] = days
                _EARNINGS_CACHE[ticker] = (now, days)

                if days is not None and 0 <= days <= 5:
                    log.info(f"  📅 {ticker}: earnings in {days} day(s)!")

            except Exception as e:
                # ETFs and funds don't have earnings calendars — suppress 404s silently
                if "404" not in str(e) and "Not Found" not in str(e):
                    log.debug(f"Earnings fetch error {ticker}: {e}")
                results[ticker] = None
                _EARNINGS_CACHE[ticker] = (now, None)

            time.sleep(0.05)

        return results

    def score_earnings(self, ticker: str, days_until: int | None) -> dict:
        """
        Score a ticker based on proximity to earnings.
        Returns score bonus and reason string.
        """
        if days_until is None:
            return {"score": 0, "reason": None}

        # Too far out — not useful for short-term options
        if days_until > 7 or days_until < 0:
            return {"score": 0, "reason": None}

        if days_until == 0:
            # Earnings TODAY — highest IV, biggest move potential
            return {
                "score": 4,
                "reason": f"🔥 EARNINGS TODAY — maximum volatility expected"
            }
        elif days_until == 1:
            return {
                "score": 3,
                "reason": f"📅 Earnings TOMORROW — IV inflating now"
            }
        elif days_until <= 3:
            return {
                "score": 2,
                "reason": f"📅 Earnings in {days_until} days — building premium"
            }
        else:
            return {
                "score": 1,
                "reason": f"📅 Earnings in {days_until} days"
            }

    # ── Pre-Market Gap Scanner ─────────────────────────────────────────────
    def get_premarket_gaps(self, tickers: list) -> dict:
        """
        Scans for pre-market price gaps vs previous close.
        Returns dict of ticker -> gap_data dict.

        Only meaningful during pre-market hours (4am-9:30am ET)
        and first hour of trading (9:30-10:30am ET).
        """
        now = time.time()
        results = {}

        for ticker in tickers:
            # Check cache
            if ticker in _GAP_CACHE:
                cached_time, cached_val = _GAP_CACHE[ticker]
                if now - cached_time < 300:  # 5 min cache for gaps
                    results[ticker] = cached_val
                    continue

            try:
                stock = yf.Ticker(ticker)

                # Get pre-market quote
                info = stock.fast_info

                prev_close  = float(info.previous_close or 0)
                current     = float(info.last_price or 0)
                pre_volume  = float(info.three_month_average_volume or 0)

                if prev_close <= 0 or current <= 0:
                    results[ticker] = None
                    continue

                gap_pct = ((current - prev_close) / prev_close) * 100

                # Get today's intraday volume vs average
                try:
                    hist = yf.download(ticker, period="2d", interval="1m",
                                       progress=False, auto_adjust=True)
                    if not hist.empty:
                        today = datetime.now().date()
                        today_data = hist[hist.index.date == today]
                        today_volume = float(today_data["Volume"].sum()) if not today_data.empty else 0
                        avg_daily_vol = float(info.three_month_average_volume or 1)
                        # Normalize: how much of daily avg volume traded already today
                        vol_ratio = today_volume / (avg_daily_vol / 6.5) if avg_daily_vol > 0 else 1
                    else:
                        vol_ratio = 1
                except:
                    vol_ratio = 1

                gap_data = {
                    "gap_pct": gap_pct,
                    "prev_close": prev_close,
                    "current": current,
                    "vol_ratio": vol_ratio,
                    "direction": "CALL" if gap_pct > 0 else "PUT"
                }

                results[ticker] = gap_data
                _GAP_CACHE[ticker] = (now, gap_data)

            except Exception as e:
                log.debug(f"Gap scan error {ticker}: {e}")
                results[ticker] = None

        return results

    def score_gap(self, ticker: str, gap_data: dict | None) -> dict:
        """
        Score a ticker based on pre-market gap size and volume.
        """
        if not gap_data:
            return {"score": 0, "reason": None, "direction": None}

        gap_pct   = gap_data["gap_pct"]
        vol_ratio = gap_data["vol_ratio"]
        direction = gap_data["direction"]
        abs_gap   = abs(gap_pct)

        # Small gaps are noise
        if abs_gap < 2.0:
            return {"score": 0, "reason": None, "direction": None}

        score = 0
        reason = None

        if abs_gap >= 8.0:
            score = 3
            reason = (f"🚀 MASSIVE pre-market gap {gap_pct:+.1f}% "
                      f"(vol {vol_ratio:.1f}x avg)")
        elif abs_gap >= 5.0:
            score = 2
            reason = (f"⚡ Large pre-market gap {gap_pct:+.1f}% "
                      f"(vol {vol_ratio:.1f}x avg)")
        elif abs_gap >= 2.0:
            score = 1
            reason = (f"📈 Pre-market gap {gap_pct:+.1f}%")

        # Volume confirmation doubles conviction
        if vol_ratio >= 2.0 and score > 0:
            score += 1
            reason += f" — HIGH VOLUME CONFIRMATION"

        return {"score": min(score, 3), "reason": reason, "direction": direction}

    # ── Combined scan ──────────────────────────────────────────────────────
    def scan(self, tickers: list) -> dict:
        """
        Run full catalyst scan on a list of tickers.
        Returns dict of ticker -> {earnings_score, gap_score, total_bonus,
                                   direction_bias, reasons}
        """
        log.info(f"⚡ CatalystScanner: scanning {len(tickers)} tickers...")

        earnings_dates = self.get_earnings_dates(tickers)
        gap_data       = self.get_premarket_gaps(tickers)

        results = {}
        for ticker in tickers:
            e_result = self.score_earnings(ticker, earnings_dates.get(ticker))
            g_result = self.score_gap(ticker, gap_data.get(ticker))

            total   = e_result["score"] + g_result["score"]
            reasons = []
            if e_result["reason"]:
                reasons.append(e_result["reason"])
            if g_result["reason"]:
                reasons.append(g_result["reason"])

            # Gap direction overrides if strong
            direction_bias = g_result["direction"] if g_result["score"] >= 2 else None

            results[ticker] = {
                "earnings_score":  e_result["score"],
                "gap_score":       g_result["score"],
                "total_bonus":     min(total, 6),  # cap at +6
                "direction_bias":  direction_bias,
                "gap_pct":         gap_data.get(ticker, {}).get("gap_pct", 0) if gap_data.get(ticker) else 0,
                "days_to_earnings": earnings_dates.get(ticker),
                "reasons":         reasons
            }

            if total > 0:
                log.info(
                    f"  ⚡ {ticker}: +{total} catalyst bonus "
                    f"(earnings={e_result['score']}, gap={g_result['score']})"
                )

        return results

    def get_top_catalyst_tickers(self, min_bonus: int = 2) -> list:
        """
        Scan a broad universe and return tickers with strong catalyst scores.
        These get added to the watchlist automatically.
        """
        # Broad universe for catalyst scanning — 150+ tickers
        universe = [
            # Mega-cap tech
            "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA",
            # Semiconductors
            "AMD","INTC","QCOM","MU","AVGO","AMAT","KLAC","LRCX",
            "MRVL","ON","SMCI","ARM","ASML","TSM","NXPI","TXN",
            # High-volatility tech
            "COIN","MSTR","HOOD","PLTR","RBLX","SNAP","UBER","LYFT",
            "SHOP","ABNB","DASH","PTON","RIVN","LCID","NIO","XPEV",
            "NFLX","SPOT","PINS","TWLO","ZM","DOCN","GTLB","BILL",
            # AI / cloud
            "ORCL","CRM","NOW","SNOW","DDOG","MDB","NET","ZS",
            "CRWD","S","PANW","OKTA","HUBS","CFLT","AI","SOUN",
            # Finance / crypto adjacent
            "PYPL","AFRM","SOFI","UPST","LC","NU","MELI",
            "GS","MS","JPM","BAC","C","WFC","SCHW","IBKR",
            # Biotech / pharma
            "MRNA","BNTX","NVAX","BIIB","GILD","REGN","VRTX",
            "LLY","PFE","ABBV","BMY","AMGN","ILMN","INCY","EXAS",
            # Consumer / retail
            "TGT","WMT","COST","LULU","NKE","DKNG","MGM","WYNN",
            # Energy
            "XOM","CVX","OXY","SLB","FSLR","ENPH","PLUG","BE",
            # Meme / high short interest
            "GME","AMC","BBAI","MVIS",
            # ETFs with options
            "SPY","QQQ","IWM","GLD","SLV","XLF","XLE",
            "TQQQ","SOXL","SPXL","LABU","UVXY","ARKK",
            # Other high-movers
            "DIS","BA","GE","CAT","LMT","RTX","XYZ",
        ]
        # Deduplicate while preserving order
        seen = set()
        universe = [t for t in universe if not (t in seen or seen.add(t))]

        log.info(f"⚡ Scanning {len(universe)} tickers for earnings/gap catalysts...")
        results = self.scan(universe)

        top = [
            {"ticker": t, **v}
            for t, v in results.items()
            if v["total_bonus"] >= min_bonus
        ]
        top.sort(key=lambda x: x["total_bonus"], reverse=True)
        return top


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s")

    print("CatalystScanner — Earnings + Pre-Market Gap Scanner\n")
    scanner = CatalystScanner()

    test_tickers = ["TSLA", "NVDA", "COIN", "AMD", "PLTR", "META", "AAPL"]

    print("Checking earnings dates...")
    earnings = scanner.get_earnings_dates(test_tickers)
    for t, days in earnings.items():
        if days is not None:
            print(f"  {t}: earnings in {days} day(s)")
        else:
            print(f"  {t}: no earnings found in next 7 days")

    print("\nChecking pre-market gaps...")
    gaps = scanner.get_premarket_gaps(test_tickers)
    for t, g in gaps.items():
        if g:
            print(f"  {t}: {g['gap_pct']:+.2f}% gap | vol ratio: {g['vol_ratio']:.1f}x")
        else:
            print(f"  {t}: no gap data")

    print("\nTop catalyst tickers right now:")
    top = scanner.get_top_catalyst_tickers(min_bonus=1)
    if top:
        for c in top[:10]:
            print(f"  {c['ticker']:<8} bonus=+{c['total_bonus']} "
                  f"earnings={c['earnings_score']} gap={c['gap_score']}")
            for r in c["reasons"]:
                print(f"    {r}")
    else:
        print("  No strong catalysts right now (normal outside market hours)")
