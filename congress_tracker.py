"""
CongressTracker v3 - Congressional Stock Trade Signal Booster

Sources:
- Senate: GitHub (timothycarambat) — nested structure, free, no auth
- House:  disclosures.house.gov — official XML, free, no auth
"""

import requests
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from collections import defaultdict

log = logging.getLogger("OptionsAgent")

_CONGRESS_CACHE: dict = {}
CACHE_TTL = 3600

NOTABLE_TRADERS = {
    "Nancy Pelosi", "Paul Pelosi", "Dan Crenshaw", "Tommy Tuberville",
    "David Rouzer", "Markwayne Mullin", "Josh Gottheimer", "Brian Higgins",
    "Michael McCaul", "Ro Khanna", "Pat Fallon",
}


def _parse_date(date_str: str):
    """Try multiple date formats."""
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except:
            continue
    return None


def _parse_amount(amount_str: str) -> float:
    if not amount_str:
        return 0
    try:
        cleaned = str(amount_str).replace("$", "").replace(",", "").strip()
        if " - " in cleaned:
            parts = cleaned.split(" - ")
            return (float(parts[0].strip()) + float(parts[1].strip())) / 2
        elif "-" in cleaned and not cleaned.startswith("-"):
            parts = cleaned.split("-")
            return (float(parts[0].strip()) + float(parts[1].strip())) / 2
        elif "+" in cleaned:
            return float(cleaned.replace("+", "").strip())
        else:
            return float(cleaned)
    except:
        return 0


class CongressTracker:

    def _fetch_senate_trades(self, days_back: int = 180) -> list:
        """
        Senate data from GitHub — nested structure.
        Uses days_back=180 by default since 45-day disclosure lag means
        very recent trades may not appear yet.
        """
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/timothycarambat/"
                "senate-stock-watcher-data/master/aggregate/all_transactions.json",
                timeout=20
            )
            if r.status_code != 200:
                log.warning(f"Senate GitHub HTTP {r.status_code}")
                return []

            data = r.json()
            cutoff = datetime.now() - timedelta(days=days_back)
            results = []
            total_checked = 0

            for txn in data:
                total_checked += 1
                try:
                    date_str = txn.get("transaction_date", "")
                    if not date_str or date_str == "--":
                        continue

                    trade_date = _parse_date(date_str)
                    if trade_date is None or trade_date < cutoff:
                        continue

                    ticker = txn.get("ticker", "").upper().strip()
                    if not ticker or ticker == "--" or len(ticker) > 6:
                        continue

                    results.append({
                        "source":  "Senate",
                        "name":    txn.get("senator", "Unknown"),
                        "ticker":  ticker,
                        "type":    txn.get("type", ""),
                        "amount":  txn.get("amount", ""),
                        "date":    date_str,
                    })
                except:
                    continue

            log.info(f"  Senate: {len(results)} trades in last {days_back} days "
                     f"(checked {total_checked} total transactions)")
            return results

        except Exception as e:
            log.warning(f"Senate fetch error: {e}")
            return []

    def _fetch_house_trades(self, days_back: int = 180) -> list:
        """
        House data from the official House disclosure site.
        Downloads the annual XML/CSV report for the current year.
        """
        results = []
        year = datetime.now().year
        cutoff = datetime.now() - timedelta(days=days_back)

        try:
            # Official House Financial Disclosures - FD XML report
            url = (f"https://disclosures.house.gov/public_disc/financial-pdfs/"
                   f"{year}FD.zip")
            # This is a ZIP, try the periodic transaction reports instead
            # PTR = Periodic Transaction Report (within 45 days of trade)
            ptr_url = (f"https://disclosures.house.gov/public_disc/ptr-pdfs/"
                       f"{year}PTR.zip")

            # Actually use the search XML which is more accessible
            search_url = (
                "https://disclosures.house.gov/FinancialDisclosure/"
                "ViewMemberSearchResult"
            )
            r = requests.get(
                "https://disclosures.house.gov/public_disc/ptr-pdfs/"
                f"{year}/{year}PTRs.xml",
                timeout=15
            )

            if r.status_code == 200 and "<" in r.text:
                try:
                    root = ET.fromstring(r.text)
                    for member in root.findall(".//Member"):
                        name = (member.findtext("Name") or
                                member.findtext("Last") or "Unknown")
                        for txn in member.findall(".//Transaction"):
                            try:
                                ticker = (txn.findtext("Ticker") or "").upper().strip()
                                if not ticker or ticker == "--" or len(ticker) > 6:
                                    continue
                                date_str = txn.findtext("TransactionDate") or ""
                                trade_date = _parse_date(date_str)
                                if trade_date is None or trade_date < cutoff:
                                    continue
                                txn_type = txn.findtext("Type") or ""
                                results.append({
                                    "source":  "House",
                                    "name":    name,
                                    "ticker":  ticker,
                                    "type":    txn_type,
                                    "amount":  txn.findtext("Amount") or "",
                                    "date":    date_str,
                                })
                            except:
                                continue
                except ET.ParseError:
                    pass

            log.info(f"  House: {len(results)} trades in last {days_back} days")
            return results

        except Exception as e:
            log.warning(f"House fetch error: {e}")
            return []

    def get_all_trades(self, days_back: int = 180) -> list:
        cache_key = f"congress_{days_back}"
        now = time.time()
        if cache_key in _CONGRESS_CACHE:
            cached_time, cached_data = _CONGRESS_CACHE[cache_key]
            if now - cached_time < CACHE_TTL:
                return cached_data

        log.info("🏛️  Fetching congressional stock disclosures...")
        senate = self._fetch_senate_trades(days_back)
        house  = self._fetch_house_trades(days_back)
        all_trades = senate + house
        log.info(f"  Total: {len(all_trades)} congressional trades found")

        _CONGRESS_CACHE[cache_key] = (now, all_trades)
        return all_trades

    def get_congress_scores(self, tickers: list = None, days_back: int = 180) -> dict:
        all_trades = self.get_all_trades(days_back)
        if not all_trades:
            return {}

        ticker_data = defaultdict(lambda: {
            "buys": [], "sells": [], "members": set(),
            "senators": set(), "notable": set(),
        })

        for trade in all_trades:
            ticker = trade.get("ticker", "").upper().strip()
            if not ticker or len(ticker) > 6 or not ticker.isalpha():
                continue
            if tickers and ticker not in tickers:
                continue

            trade_type = trade.get("type", "").lower()
            is_buy  = any(w in trade_type for w in
                          ["purchase","buy","bought"])
            is_sell = any(w in trade_type for w in
                          ["sale","sell","sold"])
            if not is_buy and not is_sell:
                continue

            name   = trade.get("name", "Unknown")
            source = trade.get("source", "House")
            amount = _parse_amount(trade.get("amount", ""))

            entry = {"name": name, "source": source,
                     "amount": amount, "date": trade.get("date", "")}

            if is_buy:
                ticker_data[ticker]["buys"].append(entry)
                ticker_data[ticker]["members"].add(name)
                if source == "Senate":
                    ticker_data[ticker]["senators"].add(name)
                if name in NOTABLE_TRADERS:
                    ticker_data[ticker]["notable"].add(name)
            elif is_sell:
                ticker_data[ticker]["sells"].append(entry)

        results = {}
        for ticker, data in ticker_data.items():
            score, reasons = 0, []

            if data["buys"]:
                score += 2
                mc  = len(data["members"])
                val = sum(b["amount"] for b in data["buys"])
                reasons.append(
                    f"🏛️  {mc} Congress member(s) bought {ticker} (~${val:,.0f})"
                )
                if mc >= 2:
                    score += 1
                    reasons.append(f"🏛️  {mc} members buying same stock")
                if data["senators"]:
                    score += 1
                    reasons.append(
                        f"🏛️  Senator(s): {', '.join(list(data['senators'])[:2])}"
                    )
                if data["notable"]:
                    score += 1
                    reasons.append(
                        f"🏛️  HIGH PROFILE: {', '.join(list(data['notable'])[:2])}"
                    )
            elif data["sells"]:
                score -= 1
                reasons.append(f"🏛️  Congress selling {ticker}")

            if score != 0:
                results[ticker] = {
                    "score":     max(min(score, 4), -1),
                    "reasons":   reasons,
                    "buys":      data["buys"],
                    "sells":     data["sells"],
                    "members":   list(data["members"]),
                    "direction": "CALL" if data["buys"] else "PUT",
                }

        return results

    def get_congress_watchlist(self, days_back: int = 180,
                               min_score: int = 2) -> list:
        scores = self.get_congress_scores(days_back=days_back)
        ranked = [{"ticker": t, **v} for t, v in scores.items()
                  if v["score"] >= min_score]
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("CongressTracker v3 — Congressional Stock Disclosure Scanner\n")

    tracker  = CongressTracker()
    watchlist = tracker.get_congress_watchlist(days_back=180, min_score=2)

    if not watchlist:
        # Show raw count even if below threshold
        all_scores = tracker.get_congress_scores(days_back=180)
        if all_scores:
            print(f"Found {len(all_scores)} tickers but none met min_score=2.")
            print("Top results:")
            for t, v in sorted(all_scores.items(),
                               key=lambda x: x[1]["score"], reverse=True)[:10]:
                print(f"  {t}: score={v['score']} buys={len(v['buys'])}")
        else:
            print("No congressional trades found.")
            print("Note: Senators have up to 45 days to disclose.")
            print("Try again tomorrow or check back after market hours.")
    else:
        print(f"Top {len(watchlist)} tickers being bought by Congress:\n")
        for i, t in enumerate(watchlist[:15], 1):
            print(f"{i:2}. {t['ticker']:<8} Score:{t['score']}/4  "
                  f"({len(t['buys'])} buy(s))")
            for r in t["reasons"]:
                print(f"      {r}")
            print()