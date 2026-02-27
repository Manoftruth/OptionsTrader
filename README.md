# OptionsAgent 🔥
### Autonomous High-Volatility Options Trading Agent

**Strategy:** Maximum aggression — momentum/breakout signals → slightly OTM weekly options → auto exit on TP/SL

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get your Tradier API token
1. Sign up free at https://developer.tradier.com/
2. Create a **Paper Trading** account (sandbox) — no real money needed to test
3. Copy your API token and Account ID

### 3. Configure the agent
Edit `config.py`:
```python
"tradier_token": "YOUR_TRADIER_API_TOKEN",
"account_id":    "YOUR_ACCOUNT_ID",
"sandbox":       True,    # ← Keep True until you're confident
"capital_limit": 250.00,  # ← Set to your budget
```

### 4. Run the agent
```bash
python agent.py
```

---

## How It Works

```
Every 5 minutes during market hours (9:30 AM - 3:45 PM ET):

1. MONITOR  → Check all open positions for TP/SL triggers
2. SCAN     → Score every ticker in watchlist using 5 indicators
3. SIGNAL   → Pick highest-scoring ticker with strong directional bias
4. SELECT   → Find best slightly-OTM weekly option contract
5. RISK     → Verify capital limits and position count
6. EXECUTE  → Place market order via Tradier API
```

## Signal Scoring (max 9 points)
| Indicator | Points | Condition |
|---|---|---|
| RSI | 1-2 | >70 overbought or <30 oversold |
| VWAP Deviation | 1 | Price >1% from VWAP |
| Volume Surge | 2 | Current volume >1.5x average |
| Price Momentum | 1 | >1% move last 5 bars |
| ATR Volatility | 1 | High relative volatility |

Minimum score of **4** required to trigger a trade.

## Exit Rules
- **Take Profit:** +80% gain → sell immediately
- **Stop Loss:** -50% loss → sell immediately
- **Market close:** All positions checked every cycle

## Capital Safety
- `capital_limit`: Agent will NEVER deploy more than this total
- `max_trade_size`: Per-trade maximum
- `max_concurrent_positions`: Max open positions at once

---

## Going Live (Real Money)

When you're ready to trade real money:

1. Open a live Tradier account at https://brokerage.tradier.com/
2. Fund it with your chosen amount (e.g. $250)
3. Enable options trading (Level 2 approval needed)
4. Update `config.py`:
   ```python
   "sandbox": False,
   "tradier_token": "YOUR_LIVE_TOKEN",
   "account_id":    "YOUR_LIVE_ACCOUNT_ID",
   ```
5. Start with `capital_limit` set low ($50-100) to validate live execution

---

## Files
| File | Purpose |
|---|---|
| `agent.py` | Main agent — all trading logic |
| `config.py` | All settings and parameters |
| `agent.log` | Real-time trade log |
| `trades.json` | Trade history (appended each execution) |

---

## ⚠️ Risk Warning
Options trading carries substantial risk. 0DTE and weekly options can expire worthless.
The `capital_limit` setting is your hard protection — never set it above what you're
willing to lose entirely. Always paper trade first.
