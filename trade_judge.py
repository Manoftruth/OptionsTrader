"""
TradeJudge - LLM-powered final gate before trade execution.

Uses Claude Haiku to evaluate a trade signal against recent news
and market context, returning BUY or SKIP with a reason.

Enable/disable via config.py:
    "use_trade_judge": True,       # set False to bypass entirely
    "anthropic_api_key": "sk-ant-...",

Cost: ~$0.001 per call (Claude Haiku) — negligible.
"""

import logging
import requests
import yfinance as yf
from config import CONFIG

log = logging.getLogger("OptionsAgent")


class TradeJudge:
    """
    LLM gate that sits between signal scoring and order execution.

    Usage:
        judge = TradeJudge()
        should_trade, reason = judge.judge(trade, signal, regime, vix, spy_chg)
        if not should_trade:
            return  # skip this trade
    """

    def __init__(self):
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.api_key = CONFIG.get("anthropic_api_key", "")
        self.enabled = bool(self.api_key) and CONFIG.get("use_trade_judge", False)

        if not CONFIG.get("use_trade_judge", False):
            log.info("TradeJudge: disabled (use_trade_judge=False in config)")
        elif not self.api_key:
            log.info("TradeJudge: disabled (no anthropic_api_key in config)")
        else:
            log.info("TradeJudge: enabled — Claude Haiku will gate all trades")

    def _fetch_news(self, ticker: str) -> str:
        """Fetch recent headlines for ticker via yfinance."""
        try:
            tk = yf.Ticker(ticker)
            news = tk.news
            if not news:
                return "No recent news found."
            headlines = []
            for item in news[:5]:
                title = item.get("content", {}).get("title") or item.get("title", "")
                if title:
                    headlines.append(f"- {title}")
            return "\n".join(headlines) if headlines else "No recent news found."
        except:
            return "Could not fetch news."

    def judge(self, trade: dict, signal: dict, regime: str,
              vix: float, spy_chg: float) -> tuple[bool, str]:
        """
        Evaluate a trade using Claude Haiku.

        Returns:
            (should_trade: bool, reason: str)
            Falls back to (True, reason) if disabled or API fails — never blocks on error.
        """
        if not self.enabled:
            return True, "TradeJudge disabled — proceeding"

        ticker    = trade["ticker"]
        direction = trade["direction"]
        score     = signal.get("score", 0)
        confluence = signal.get("confluence", 0)
        reasons   = signal.get("reasons", [])[:6]
        news      = self._fetch_news(ticker)

        prompt = f"""You are a risk-aware options trading assistant. Evaluate this trade and respond with ONLY "BUY" or "SKIP" followed by one sentence explaining why.

TRADE: {ticker} {direction} option
Score: {score}/20 | Confluence: {confluence}/4 timeframes agree
Market: SPY {spy_chg:+.1f}% today | VIX: {vix:.1f} | Regime: {regime.upper()}
Capital at risk: ${trade['total_cost']:.0f}

Technical reasons:
{chr(10).join(reasons)}

Recent {ticker} news:
{news}

Rules:
- SKIP if news contains earnings surprise, SEC investigation, CEO resignation, or major negative catalyst
- SKIP if direction contradicts strong news sentiment
- SKIP if VIX > 23 and score < 17
- BUY if technicals are strong and news is neutral or positive
- When uncertain, BUY (trust the signal engine)

Respond with exactly: BUY <one sentence> or SKIP <one sentence>"""

        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            body = {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 60,
                "messages": [{"role": "user", "content": prompt}]
            }
            resp = requests.post(self.api_url, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"].strip()
            log.info(f"🤖 TradeJudge ({ticker} {direction}): {text}")
            should_trade = text.upper().startswith("BUY")
            return should_trade, text
        except Exception as e:
            log.warning(f"TradeJudge API error: {e} — defaulting to BUY")
            return True, f"LLM gate error ({e}) — proceeding with trade"
