"""
TradeJudge - LLM-powered final gate before trade execution.

v4.1 fixes:
- None guard on Anthropic API response content before calling .get()
  Prevents: 'NoneType' object has no attribute 'get' — defaulting to BUY 1.0x
- Safer content extraction with explicit type checks at each level

v4 improvements:
- Smarter prompting: aggressive but genuinely critical
- Judge earns its keep: actually uses history + news to filter
- STRONG_BUY only fires on genuinely high-conviction setups
- SKIP reserved for real edge cases the signal engine can't see
- Still defaults to trading — just not blindly

Enable/disable via config.py:
    "use_trade_judge": True,
    "anthropic_api_key": "sk-ant-...",

Cost: ~$0.002 per call (2 API calls for multi-turn) — still negligible.
"""

import json
import logging
import requests
import yfinance as yf
from pathlib import Path
from config import CONFIG

log = logging.getLogger("OptionsAgent")

CONFIDENCE_SIZING = {
    "STRONG_BUY":  1.25,
    "BUY":         1.0,
    "SKIP":        0.0,
    "STRONG_SKIP": 0.0,
}

# TP/SL hints per confidence level — TradeJudge can override these via prompt
TP_SL_DEFAULTS = {
    "STRONG_BUY":  {"tp": 50.0, "sl": 25.0},
    "BUY":         {"tp": 42.0, "sl": 25.0},
    "SKIP":        None,
    "STRONG_SKIP": None,
}


class TradeJudge:

    RESULTS_FILE = Path(__file__).parent / "trade_results.json"

    def __init__(self):
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.api_key = CONFIG.get("anthropic_api_key", "")
        self.enabled = bool(self.api_key) and CONFIG.get("use_trade_judge", False)

        if not CONFIG.get("use_trade_judge", False):
            log.info("TradeJudge: disabled (use_trade_judge=False in config)")
        elif not self.api_key:
            log.info("TradeJudge: disabled (no anthropic_api_key in config)")
        else:
            log.info("TradeJudge: enabled — multi-turn reasoning + history (v4.1)")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_ticker_history(self, ticker: str, direction: str = "") -> str:
        try:
            if not self.RESULTS_FILE.exists():
                return f"No closed trade history yet for {ticker}."
            realized = []
            with open(self.RESULTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                        if t.get("ticker") == ticker:
                            realized.append(t)
                    except:
                        continue
            if not realized:
                return f"No prior closed trades on {ticker}."
            wins   = [t for t in realized if t.get("pnl_pct", 0) > 0]
            losses = [t for t in realized if t.get("pnl_pct", 0) <= 0]
            avg_win  = sum(t["pnl_pct"] for t in wins)  / len(wins)  if wins  else 0
            avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
            net_pnl  = sum(t.get("pnl_dollars", 0) for t in realized)

            direction_trades = [t for t in realized if t.get("direction") == direction]
            lines = [
                f"{ticker}: {len(wins)}W / {len(losses)}L "
                f"({len(wins)/len(realized)*100:.0f}% win rate) | Net P&L: ${net_pnl:+.0f}",
                f"Avg win: +{avg_win:.1f}% | Avg loss: {avg_loss:.1f}%",
            ]
            if direction_trades:
                dir_wins   = [t for t in direction_trades if t.get("pnl_pct", 0) > 0]
                dir_losses = [t for t in direction_trades if t.get("pnl_pct", 0) <= 0]
                dir_avg    = sum(t["pnl_pct"] for t in direction_trades) / len(direction_trades)
                lines.append(
                    f"  {ticker} {direction} specifically: {len(dir_wins)}W / {len(dir_losses)}L "
                    f"| Avg: {dir_avg:+.1f}%"
                )
            for t in realized[-5:]:
                lines.append(
                    f"  {t.get('exit_time','')[:10]} {t.get('direction','?')} "
                    f"→ {t.get('pnl_pct',0):+.1f}%"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Could not load history: {e}"

    # ── Sector mapping ─────────────────────────────────────────────────────

    SECTOR_MAP = {
        # Tech
        "NVDA": ("Technology", "XLK"), "AMD": ("Technology", "XLK"),
        "PLTR": ("Technology", "XLK"), "MSTR": ("Technology", "XLK"),
        "HOOD": ("Technology", "XLK"), "COIN": ("Technology", "XLK"),
        "TSLA": ("Technology", "XLK"), "MSFT": ("Technology", "XLK"),
        "AAPL": ("Technology", "XLK"), "GOOGL": ("Technology", "XLK"),
        "META": ("Technology", "XLK"), "AMZN": ("Technology", "XLK"),
        "CRM":  ("Technology", "XLK"), "NOW":  ("Technology", "XLK"),
        "CRWD": ("Technology", "XLK"), "SNOW": ("Technology", "XLK"),
        "DDOG": ("Technology", "XLK"), "ZS":   ("Technology", "XLK"),
        "ZM":   ("Technology", "XLK"), "RBLX": ("Technology", "XLK"),
        "AI":   ("Technology", "XLK"), "BBAI": ("Technology", "XLK"),
        # Semiconductors
        "SOXL": ("Semiconductors", "SOXX"), "INTC": ("Semiconductors", "SOXX"),
        "SMCI": ("Semiconductors", "SOXX"),
        # Leveraged index
        "TQQQ": ("Nasdaq", "QQQ"), "SPXL": ("S&P500", "SPY"),
        "LABU": ("Biotech", "XBI"),
        # Finance
        "SOFI": ("Finance", "XLF"), "LC": ("Finance", "XLF"),
        "AFRM": ("Finance", "XLF"),
        # Energy/Defense
        "BE":   ("Energy", "XLE"), "PLUG": ("Energy", "XLE"),
        "LMT":  ("Defense", "XAR"), "RTX":  ("Defense", "XAR"),
        # Healthcare/Biotech
        "NVAX": ("Biotech", "XBI"),
        # EV/Auto
        "XPEV": ("EV", "KARS"), "LCID": ("EV", "KARS"),
        # Retail
        "TGT":  ("Retail", "XRT"),
        # Volatility
        "UVXY": ("Volatility", "VXX"),
        # Index ETFs — self-referential
        "SPY":  ("S&P500", "SPY"), "QQQ": ("Nasdaq", "QQQ"),
        "IWM":  ("SmallCap", "IWM"),
    }

    def _fetch_sector_context(self, ticker: str) -> str:
        try:
            sector, etf = self.SECTOR_MAP.get(ticker, ("Unknown", None))
            if not etf or etf == ticker:
                return f"Sector: {sector} (no ETF benchmark available)"

            data = yf.download(etf, period="2d", interval="1d", progress=False, auto_adjust=True)
            if len(data) < 2:
                return f"Sector: {sector} ({etf} data unavailable)"

            close = data["Close"].squeeze()
            prev  = float(close.iloc[-2])
            curr  = float(close.iloc[-1])
            chg   = (curr - prev) / prev * 100

            if chg > 1.0:
                sentiment = "BULLISH"
            elif chg < -1.0:
                sentiment = "BEARISH"
            else:
                sentiment = "NEUTRAL"

            return (
                f"Sector: {sector} | {etf}: {chg:+.2f}% today → {sentiment}\n"
                f"  {'✅ Sector tailwind' if (sentiment == 'BULLISH' and ticker != 'UVXY') else '⚠️ Sector headwind' if sentiment == 'BEARISH' else '➡️ Sector neutral'}"
            )
        except Exception as e:
            return f"Sector context unavailable: {e}"

    def _fetch_news(self, ticker: str) -> str:
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

    def _call_api(self, messages: list, max_tokens: int = 200) -> str:
        """
        FIX: Added explicit None/type guards at every level of the response.

        Root cause of 'NoneType' object has no attribute 'get':
          data.get("content") returned None when the API response was malformed
          or the request timed out and returned a non-standard payload.
          Previously the code called content[0].get("text") without checking
          whether content itself was None or whether content[0] was a dict.
        """
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": messages
        }
        resp = requests.post(self.api_url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # FIX: Guard 1 — content key missing or None
        content = data.get("content")
        if not content:
            raise ValueError(f"API response missing content field: {data}")

        # FIX: Guard 2 — content is not a list (unexpected response shape)
        if not isinstance(content, list):
            raise ValueError(f"API response content is not a list: {type(content)} — {content}")

        # FIX: Guard 3 — first block is None or not a dict
        first_block = content[0]
        if first_block is None:
            raise ValueError(f"API response content[0] is None")
        if not isinstance(first_block, dict):
            raise ValueError(f"API response content[0] is not a dict: {type(first_block)}")

        # FIX: Guard 4 — text key missing or None
        text = first_block.get("text")
        if not text:
            raise ValueError(f"No text in API response content block: {first_block}")

        return text.strip()

    # ── Main judge ─────────────────────────────────────────────────────────

    def judge(self, trade: dict, signal: dict, regime: str,
              vix: float, spy_chg: float) -> tuple[bool, float, str]:
        """
        Multi-turn reasoning:
          Turn 1 — Claude analyzes using data the signal engine can't see
          Turn 2 — Claude gives final STRONG_BUY/BUY/SKIP/STRONG_SKIP decision

        Returns (should_trade, size_multiplier, reason)
        """
        if not self.enabled:
            return True, 1.0, "TradeJudge disabled — proceeding", None

        ticker     = trade["ticker"]
        direction  = trade["direction"]
        score      = signal.get("score", 0)
        confluence = signal.get("confluence", 0)
        reasons    = signal.get("reasons", [])[:6]
        news       = self._fetch_news(ticker)
        history    = self._load_ticker_history(ticker, direction)
        sector     = self._fetch_sector_context(ticker)

        context = f"""You are a 0DTE options trading analyst. The signal engine has already done the technical work — RSI, MACD, VWAP, BB across 3 timeframes. Your job is to add context it CANNOT see: news catalysts, sector momentum, and trade history patterns.

TRADE SIGNAL: {ticker} {direction} option
Score: {score}/20 | Confluence: {confluence}/4 timeframes
Market: SPY {spy_chg:+.1f}% | VIX: {vix:.1f} | Regime: {regime.upper()}
Capital at risk: ${trade['total_cost']:.0f}

Technical reasons (from signal engine):
{chr(10).join(reasons)}

{sector}

Recent {ticker} news:
{news}

{ticker} trade history:
{history}"""

        try:
            # ── Turn 1: Analysis ──────────────────────────────────────────
            turn1_prompt = context + """

You are looking for one of three things the signal engine cannot detect:
1. A NEWS CATALYST that directly contradicts the trade direction (earnings miss, halt, SEC action, CEO fired)
2. A SECTOR that is strongly moving against the trade direction (>2% opposite)
3. A HISTORY PATTERN showing this specific ticker+direction consistently loses (e.g. we're 0-4 on TSLA PUTs)

If none of those are present, the trade is fine. Do not invent caution — the signal engine is already filtering weak setups.
Write 2-3 sentences focused only on what you found or didn't find. Be direct."""

            messages = [{"role": "user", "content": turn1_prompt}]
            analysis = self._call_api(messages, max_tokens=250)
            log.info(f"🤖 TradeJudge analysis ({ticker}): {analysis}")

            # ── Turn 2: Decision ──────────────────────────────────────────
            messages += [
                {"role": "assistant", "content": analysis},
                {"role": "user", "content": f"""Give your final decision based on what you found.

STRONG_BUY — score >= 17 AND sector tailwind AND positive news AND profitable history on this ticker+direction
BUY — catalyst and sector support the direction, no significant red flags
SKIP — any of: news contradicts direction, sector moving >1.5% against trade, history shows 2+ losses on this ticker+direction with no wins, post-earnings gap already fully priced in with no follow-through catalyst
STRONG_SKIP — multiple red flags, obvious bad setup, or 3+ consecutive losses on this ticker+direction

Do NOT default to BUY. Treat each trade as innocent until proven guilty, but actually scrutinize it.
Ask yourself: would a experienced trader look at this setup and feel confident, or are there real reasons to doubt it?
BEAR market regime means CALLs need extra justification — is there a genuine catalyst or just technicals?
Elevated VIX (>20) means wider spreads and faster moves against you — factor that into conviction.

After your decision, optionally nudge TP/SL by a small amount if the setup warrants it.
You can only adjust by -10 to +10 from the default. Be conservative — only nudge if you have a specific reason.
- Post-earnings gap already priced in: TP_ADJ=-10 (take profits a bit earlier)
- Strong momentum with sector tailwind: TP_ADJ=+5 (let it run a little longer)
- High uncertainty or choppy price action: SL_ADJ=-5 (cut losses a bit faster)
- High conviction, strong fundamental backing: SL_ADJ=+5 (give it more room)

Format: STRONG_BUY/BUY/SKIP/STRONG_SKIP | TP_ADJ=X | SL_ADJ=Y | one sentence reason
Only include adjustments you actually have conviction on. If no adjustment needed, omit them entirely.
Example: BUY | TP_ADJ=-10 | post-earnings gap priced in, take profits early
Example: BUY | strong momentum with no blockers

Current score for reference: {score}/20"""}
            ]
            decision = self._call_api(messages, max_tokens=60)

            # Strip markdown formatting before parsing
            decision_clean = decision.replace("**", "").replace("*", "").strip()

            # Parse confidence
            confidence = "BUY"
            for level in ["STRONG_BUY", "STRONG_SKIP", "BUY", "SKIP"]:
                if decision_clean.upper().startswith(level):
                    confidence = level
                    break

            size_multiplier = CONFIDENCE_SIZING.get(confidence, 1.0)
            should_trade    = size_multiplier > 0

            # Parse optional TP/SL nudges
            import re as _re
            tp_adj_match = _re.search(r"TP_ADJ=([+-]?\d+(?:\.\d+)?)", decision_clean, _re.IGNORECASE)
            sl_adj_match = _re.search(r"SL_ADJ=([+-]?\d+(?:\.\d+)?)", decision_clean, _re.IGNORECASE)
            tp_sl_override = None
            if tp_adj_match or sl_adj_match:
                defaults = TP_SL_DEFAULTS.get(confidence, {"tp": 42.0, "sl": 25.0})
                tp_base = defaults.get("tp", 42.0)
                sl_base = defaults.get("sl", 25.0)
                tp_adj = float(tp_adj_match.group(1)) if tp_adj_match else 0.0
                sl_adj = float(sl_adj_match.group(1)) if sl_adj_match else 0.0
                tp_adj = max(-10.0, min(10.0, tp_adj))
                sl_adj = max(-10.0, min(10.0, sl_adj))
                tp_sl_override = {
                    "tp": round(tp_base + tp_adj, 1),
                    "sl": round(sl_base + sl_adj, 1)
                }
                log.info(f"🤖 TradeJudge TP/SL nudge: base TP={tp_base}%→{tp_sl_override['tp']}% SL={sl_base}%→{tp_sl_override['sl']}% (adj: TP{tp_adj:+.0f} SL{sl_adj:+.0f})")

            log.info(
                f"🤖 TradeJudge decision ({ticker} {direction}): {confidence} "
                f"(size: {size_multiplier}x) — {decision}"
            )
            return should_trade, size_multiplier, decision, tp_sl_override

        except Exception as e:
            log.warning(f"TradeJudge API error: {e} — defaulting to BUY 1.0x")
            return True, 1.0, f"LLM gate error ({e}) — proceeding", None
