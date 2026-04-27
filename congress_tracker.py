"""
CongressTracker — disabled, no reliable free current data source available.
Gate 2A (congress signal) is bypassed. Gate 2B (squeeze breakout) is the primary entry qualifier.
"""
import logging
log = logging.getLogger("OptionsAgent")

class CongressTracker:
    def get_congress_scores(self, tickers=None, days_back=30) -> dict:
        log.info("  CongressTracker: disabled (no free current data source)")
        return {}

    def get_congress_watchlist(self, days_back=30, min_score=2) -> list:
        return []
