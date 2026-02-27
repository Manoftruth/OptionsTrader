"""
InsiderTracker v3 - Market-Wide SEC Form 4 Scanner

Scans the ENTIRE market for insider buying daily instead of
checking a fixed watchlist. Returns ranked buy signals.

Flow:
1. Pull today's Form 4 filings from SEC EDGAR daily index
2. Filter for open-market purchases (transaction code "P")
3. Filter for meaningful size ($50K+)
4. Score by transaction size, role, and filing count
5. Return top tickers ranked by score -> passed to signal engine
"""

import requests
import logging
import re
import time
from datetime import datetime, timedelta

log = logging.getLogger("OptionsAgent")

SEC_HEADERS = {
    "User-Agent": "OptionsAgent research contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}

_CIK_TO_TICKER: dict = {}
_DAILY_CACHE:   dict = {}

CSUITE = ["CEO","CFO","COO","CTO","PRESIDENT","CHAIRMAN",
          "CHIEF EXECUTIVE","CHIEF FINANCIAL","DIRECTOR","FOUNDER","EVP","SVP"]
MIN_BUY_VALUE  = 50_000
MAX_RESULTS    = 20


class InsiderTracker:

    # ── CIK <-> Ticker map ─────────────────────────────────────────────────
    def _load_ticker_map(self) -> dict:
        global _CIK_TO_TICKER
        if _CIK_TO_TICKER:
            return _CIK_TO_TICKER
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json",
                             headers=SEC_HEADERS, timeout=15)
            for entry in r.json().values():
                cik    = str(entry.get("cik_str","")).zfill(10)
                ticker = entry.get("ticker","").upper()
                if cik and ticker:
                    _CIK_TO_TICKER[cik] = ticker
            log.info(f"  Loaded {len(_CIK_TO_TICKER)} ticker mappings from SEC")
        except Exception as e:
            log.warning(f"Ticker map error: {e}")
        return _CIK_TO_TICKER

    # ── Daily filing index ─────────────────────────────────────────────────
    def _get_form4_filings_today(self) -> list:
        """
        Use SEC EDGAR full-text search API for same-day Form 4 filings.
        Falls back to previous days if today has no filings yet.
        """
        results = []
        for days_ago in range(3):
            date = datetime.now() - timedelta(days=days_ago)
            date_str = date.strftime("%Y-%m-%d")
            try:
                url = (f"https://efts.sec.gov/LATEST/search-index"
                       f"?forms=4&dateRange=custom"
                       f"&startdt={date_str}&enddt={date_str}"
                       f"&from=0&size=200")
                r = requests.get(url, headers=SEC_HEADERS, timeout=20)
                if r.status_code != 200:
                    continue
                data = r.json()
                hits = data.get("hits", {}).get("hits", [])
                if not hits:
                    continue
                for hit in hits:
                    try:
                        doc_id = hit["_id"]
                        acc = doc_id.split(":")[0]
                        ciks = hit["_source"].get("ciks", [])
                        if not ciks:
                            continue
                        cik = ciks[-1].zfill(10)
                        results.append((cik, acc))
                    except:
                        continue
                if results:
                    log.info(f"  {len(results)} Form 4 filings found for {date_str}")
                    return results
            except Exception as e:
                log.debug(f"EFTS index error: {e}")
        return results



    # ── Parse single Form 4 XML ────────────────────────────────────────────
    def _parse_form4(self, cik: str, accession: str) -> dict | None:
        try:
            cik_int    = int(cik)
            acc_nodash = accession.replace("-","")

            # Try common XML filenames
            xml = None
            for name in [f"{accession}.xml","form4.xml","wk-form4.xml"]:
                url = (f"https://www.sec.gov/Archives/edgar/data/"
                       f"{cik_int}/{acc_nodash}/{name}")
                r = requests.get(url, headers=SEC_HEADERS, timeout=6)
                if r.status_code == 200 and "<ownershipDocument>" in r.text:
                    xml = r.text
                    break

            # Scrape index for XML filename if needed
            if not xml:
                idx = requests.get(
                    f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/",
                    headers=SEC_HEADERS, timeout=6)
                if idx.status_code == 200:
                    for fname in re.findall(r'href="([^"]+\.xml)"', idx.text):
                        xurl = (f"https://www.sec.gov/Archives/edgar/data/"
                                f"{cik_int}/{acc_nodash}/{fname.split('/')[-1]}")
                        xr = requests.get(xurl, headers=SEC_HEADERS, timeout=5)
                        if xr.status_code == 200 and "<ownershipDocument>" in xr.text:
                            xml = xr.text
                            break

            if not xml:
                return None

            def first(tag):
                m = re.search(rf"<{tag}[^>]*>\s*(.*?)\s*</{tag}>",
                              xml, re.DOTALL|re.IGNORECASE)
                return m.group(1).strip() if m else ""

            ticker = first("issuerTradingSymbol").upper()
            role   = first("officerTitle").upper()

            codes  = re.findall(r"<transactionCode>\s*(\w)\s*</transactionCode>", xml)
            sblks  = re.findall(r"<transactionShares>(.*?)</transactionShares>", xml, re.DOTALL)
            pblks  = re.findall(r"<transactionPricePerShare>(.*?)</transactionPricePerShare>", xml, re.DOTALL)

            buys, sells = [], []
            for i, code in enumerate(codes):
                if code not in ("P","S"):
                    continue
                try:
                    sv = re.search(r"<value>([\d.]+)</value>", sblks[i] if i<len(sblks) else "")
                    pv = re.search(r"<value>([\d.]+)</value>", pblks[i] if i<len(pblks) else "")
                    shares = float(sv.group(1)) if sv else 0
                    price  = float(pv.group(1)) if pv else 0
                    entry  = {"shares":shares,"price":price,"value":shares*price,"role":role}
                    (buys if code=="P" else sells).append(entry)
                except:
                    continue

            if not buys and not sells:
                return None

            return {
                "ticker": ticker,
                "role":   role,
                "buys":   buys,
                "sells":  sells,
                "total_buy_value":  sum(b["value"] for b in buys),
                "total_sell_value": sum(s["value"] for s in sells),
            }
        except Exception as e:
            log.debug(f"Form4 parse error ({accession}): {e}")
            return None

    # ── Main public method ─────────────────────────────────────────────────
    def get_todays_insider_buys(self) -> list:
        """
        Scans all Form 4 filings today and returns ranked insider buy signals.
        Results are cached for the rest of the trading day.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if today in _DAILY_CACHE:
            log.info("  Using cached insider buys for today")
            return _DAILY_CACHE[today]

        log.info("🏦 Scanning market-wide Form 4 filings from SEC EDGAR...")
        ticker_map = self._load_ticker_map()
        filings    = self._get_form4_filings_today()

        if not filings:
            log.warning("  No Form 4 filings found (weekend or index not updated yet)")
            return []

        aggregated: dict = {}

        for cik, accession in filings[:300]:  # process up to 300 filings
            try:
                time.sleep(0.12)
                parsed = self._parse_form4(cik, accession)
                if not parsed or not parsed["buys"]:
                    continue

                ticker = parsed["ticker"] or ticker_map.get(cik, "")
                if not ticker or len(ticker) > 6:
                    continue

                val = parsed["total_buy_value"]
                if val < MIN_BUY_VALUE:
                    continue

                if ticker not in aggregated:
                    aggregated[ticker] = {
                        "ticker": ticker,
                        "total_buy_value": 0,
                        "filing_count": 0,
                        "roles": set(),
                        "buys": []
                    }
                aggregated[ticker]["total_buy_value"] += val
                aggregated[ticker]["filing_count"]    += 1
                aggregated[ticker]["roles"].add(parsed["role"])
                aggregated[ticker]["buys"].extend(parsed["buys"])

            except Exception as e:
                log.debug(f"Filing error: {e}")

        ticker_list = ", ".join(sorted(aggregated.keys()))
        log.info(f"  {len(aggregated)} tickers with qualifying insider purchases today: {ticker_list}")
        # Score and rank
        ranked = []
        for ticker, d in aggregated.items():
            score, reasons = 0, []
            val = d["total_buy_value"]

            score += 2
            reasons.append(f"🏦 Insider bought ${val:,.0f} today")

            if d["filing_count"] >= 2:
                score += 1
                reasons.append(f"🏦 {d['filing_count']} insiders buying same stock")

            if   val >= 500_000: score += 2; reasons.append(f"🏦 Massive buy (${val:,.0f})")
            elif val >= 100_000: score += 1; reasons.append(f"🏦 Large buy (${val:,.0f})")

            for role in d["roles"]:
                if any(t in role for t in CSUITE):
                    score += 1
                    reasons.append(f"🏦 {role} personally buying")
                    break

            ranked.append({
                "ticker":          ticker,
                "score":           min(score, 5),
                "total_buy_value": val,
                "filing_count":    d["filing_count"],
                "roles":           list(d["roles"]),
                "reasons":         reasons,
                "direction":       "CALL"
            })

        ranked.sort(key=lambda x: (x["score"], x["total_buy_value"]), reverse=True)
        top = ranked[:MAX_RESULTS]
        _DAILY_CACHE[today] = top
        return top

    def get_scores_for_watchlist(self, watchlist: list) -> dict:
        """
        Legacy method — kept for compatibility with signal_engine.py.
        Now enriches the fixed watchlist WITH market-wide insider data.
        """
        # Get market-wide buys first
        market_buys = {b["ticker"]: b for b in self.get_todays_insider_buys()}

        scores = {}
        for ticker in watchlist:
            if ticker in market_buys:
                scores[ticker] = market_buys[ticker]
            else:
                scores[ticker] = {"score": 0, "reasons": [], "buys": [], "sells": []}
        return scores


def get_insider_watchlist() -> list:
    """Returns today's top insider buy tickers as a plain list."""
    return [b["ticker"] for b in InsiderTracker().get_todays_insider_buys()]


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s")

    print("InsiderTracker v3 — Full Market Scanner\n")
    tracker = InsiderTracker()
    buys    = tracker.get_todays_insider_buys()

    if not buys:
        print("No significant insider purchases found today.")
        print("(Normal on weekends / early morning before SEC index updates)")
    else:
        print(f"Top {len(buys)} insider buy signals today:\n")
        for i, b in enumerate(buys, 1):
            print(f"{i:2}. {b['ticker']:<8}  Score:{b['score']}/5  "
                  f"${b['total_buy_value']:>12,.0f}  "
                  f"({b['filing_count']} filing(s))")
            for reason in b["reasons"]:
                print(f"      {reason}")
            print()
