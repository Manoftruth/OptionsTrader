"""
OptionsAgent - Performance Report Generator
Run this after your sandbox period to generate a summary report.
Share the output with Claude for analysis and tuning.

Usage:
    python report.py > performance_report.txt
"""

import json
import re
import requests
from datetime import datetime, timedelta
from collections import defaultdict


def load_trades(path="trades.json") -> list:
    trades = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except:
                        continue
    except FileNotFoundError:
        print(f"WARNING: {path} not found")
    return trades


def parse_log(path="agent.log") -> dict:
    """Extract key events from agent.log without loading the whole file."""
    stats = {
        "total_cycles":        0,
        "no_signal_cycles":    0,
        "blocked_trades":      0,
        "kill_switch_fires":   0,
        "stop_losses":         0,
        "take_profits":        0,
        "errors":              0,
        "regime_counts":       defaultdict(int),
        "blocked_reasons":     defaultdict(int),
        "signal_scores_seen":  [],
        "tickers_scanned":     defaultdict(int),
    }

    try:
        with open(path) as f:
            for line in f:
                if "OptionsAgent cycle" in line:
                    stats["total_cycles"] += 1
                elif "No high-confidence signals" in line:
                    stats["no_signal_cycles"] += 1
                elif "Risk manager blocked" in line:
                    stats["blocked_trades"] += 1
                    reason = line.split("Risk manager blocked:")[-1].strip() if "Risk manager blocked:" in line else "unknown"
                    stats["blocked_reasons"][reason[:50]] += 1
                elif "KILL SWITCH" in line:
                    stats["kill_switch_fires"] += 1
                elif "STOP LOSS" in line:
                    stats["stop_losses"] += 1
                elif "TAKE PROFIT" in line:
                    stats["take_profits"] += 1
                elif "ERROR" in line or "error" in line.lower():
                    stats["errors"] += 1
                elif "Market regime:" in line:
                    regime = line.split("Market regime:")[-1].strip().lower()
                    stats["regime_counts"][regime] += 1
                elif "score=" in line:
                    m = re.search(r"score=([\d.]+)", line)
                    if m:
                        stats["signal_scores_seen"].append(float(m.group(1)))
                elif "✅" in line and "ticker" not in line.lower():
                    m = re.search(r"✅ (\w+):", line)
                    if m:
                        stats["tickers_scanned"][m.group(1)] += 1

    except FileNotFoundError:
        print(f"WARNING: {path} not found")

    return stats


def analyze_trades(trades: list) -> dict:
    if not trades:
        return {}

    by_ticker    = defaultdict(list)
    by_date      = defaultdict(list)
    by_direction = defaultdict(list)

    for t in trades:
        ticker    = t.get("ticker", "UNKNOWN")
        direction = t.get("direction", "UNKNOWN")
        cost      = float(t.get("total_cost", 0))
        time_str  = t.get("time", "")

        by_ticker[ticker].append(t)
        by_direction[direction].append(t)
        if time_str:
            date = time_str[:10]
            by_date[date].append(t)

    scores           = [t.get("signal", {}).get("score", 0)          for t in trades if isinstance(t.get("signal"), dict)]
    insider_bonuses  = [t.get("signal", {}).get("insider_bonus", 0)  for t in trades if isinstance(t.get("signal"), dict)]
    catalyst_bonuses = [t.get("signal", {}).get("catalyst_bonus", 0) for t in trades if isinstance(t.get("signal"), dict)]

    return {
        "total_trades":       len(trades),
        "total_deployed":     sum(float(t.get("total_cost", 0)) for t in trades),
        "by_ticker":          {k: len(v) for k, v in sorted(by_ticker.items(), key=lambda x: len(x[1]), reverse=True)},
        "by_direction":       {k: len(v) for k, v in by_direction.items()},
        "by_date":            {k: len(v) for k, v in sorted(by_date.items())},
        "avg_score":          sum(scores) / len(scores) if scores else 0,
        "avg_trade_cost":     sum(float(t.get("total_cost", 0)) for t in trades) / len(trades),
        "avg_contracts":      sum(int(t.get("contracts", 1)) for t in trades) / len(trades),
        "insider_helped":     sum(1 for b in insider_bonuses if b > 0),
        "catalyst_helped":    sum(1 for b in catalyst_bonuses if b > 0),
        "avg_insider_bonus":  sum(insider_bonuses)  / len(insider_bonuses)  if insider_bonuses  else 0,
        "avg_catalyst_bonus": sum(catalyst_bonuses) / len(catalyst_bonuses) if catalyst_bonuses else 0,
    }


# ── P&L from Tradier ──────────────────────────────────────────────────────────

def fetch_pnl_from_tradier(trades: list) -> dict:
    """Query Tradier for filled orders and calculate P&L by matching buy/sell pairs."""
    try:
        from config import CONFIG
    except ImportError:
        return {"error": "Could not import config.py"}

    token      = CONFIG.get("tradier_token", "")
    account_id = CONFIG.get("account_id", "")
    sandbox    = CONFIG.get("sandbox", True)

    if not token or not account_id or "YOUR_" in token:
        return {"error": "Tradier credentials not set in config.py"}

    base_url = "https://sandbox.tradier.com" if sandbox else "https://api.tradier.com"
    headers  = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }

    try:
        resp = requests.get(
            f"{base_url}/v1/accounts/{account_id}/orders",
            headers=headers,
            params={"includeTags": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"Tradier API error: {e}"}

    raw_orders = data.get("orders", {})
    if not raw_orders or raw_orders == "null":
        return {"error": "No orders returned from Tradier"}

    order_list = raw_orders.get("order", [])
    if isinstance(order_list, dict):
        order_list = [order_list]

    # Only care about filled option orders
    filled = [o for o in order_list if o.get("status") == "filled" and o.get("class") == "option"]

    if not filled:
        return {"error": "No filled option orders found in Tradier account"}

    # Group by option symbol → match buys to sells
    by_symbol = defaultdict(list)
    for o in filled:
        symbol = o.get("option_symbol") or o.get("symbol", "UNKNOWN")
        by_symbol[symbol].append(o)

    trade_pnls   = []
    total_profit = 0.0
    total_loss   = 0.0

    for symbol, orders in by_symbol.items():
        buys  = [o for o in orders if o.get("side") in ("buy_to_open",  "buy")]
        sells = [o for o in orders if o.get("side") in ("sell_to_close", "sell")]

        if not buys or not sells:
            continue

        avg_buy  = sum(float(o.get("avg_fill_price", 0)) * int(o.get("quantity", 1)) for o in buys)  / sum(int(o.get("quantity", 1)) for o in buys)
        avg_sell = sum(float(o.get("avg_fill_price", 0)) * int(o.get("quantity", 1)) for o in sells) / sum(int(o.get("quantity", 1)) for o in sells)
        qty      = min(sum(int(o.get("quantity", 1)) for o in buys),
                       sum(int(o.get("quantity", 1)) for o in sells))

        pnl     = (avg_sell - avg_buy) * qty * 100  # options multiplier
        pct     = ((avg_sell - avg_buy) / avg_buy * 100) if avg_buy > 0 else 0

        # Extract ticker from option symbol (first letters before digits)
        m = re.match(r"([A-Z]+)", symbol)
        ticker = m.group(1) if m else symbol

        trade_pnls.append({
            "symbol": symbol,
            "ticker": ticker,
            "pnl":    pnl,
            "pct":    pct,
            "qty":    qty,
            "buy":    avg_buy,
            "sell":   avg_sell,
        })

        if pnl >= 0:
            total_profit += pnl
        else:
            total_loss += pnl

    if not trade_pnls:
        return {"error": "Could not match buy/sell pairs — trades may still be open"}

    wins   = [t for t in trade_pnls if t["pnl"] >= 0]
    losses = [t for t in trade_pnls if t["pnl"] <  0]
    net    = total_profit + total_loss

    return {
        "trade_pnls":    sorted(trade_pnls, key=lambda x: x["pnl"], reverse=True),
        "total_profit":  total_profit,
        "total_loss":    total_loss,
        "net_pnl":       net,
        "win_count":     len(wins),
        "loss_count":    len(losses),
        "avg_win":       sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0,
        "avg_loss":      sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
        "best_trade":    max(trade_pnls, key=lambda x: x["pnl"])  if trade_pnls else None,
        "worst_trade":   min(trade_pnls, key=lambda x: x["pnl"])  if trade_pnls else None,
        "win_rate":      len(wins) / len(trade_pnls) * 100         if trade_pnls else 0,
    }


def print_pnl_section(pnl: dict):
    sep2 = "-" * 40
    print(f"\n💵 P&L SUMMARY  (live from Tradier)")
    print(sep2)

    if "error" in pnl:
        print(f"  ⚠️  {pnl['error']}")
        return

    net = pnl["net_pnl"]
    net_emoji = "✅" if net >= 0 else "❌"

    print(f"  Gross profit:      ${pnl['total_profit']:>8.2f}")
    print(f"  Gross loss:        ${pnl['total_loss']:>8.2f}")
    print(f"  {net_emoji} Net P&L:          ${net:>8.2f}")
    print(f"  Win rate:           {pnl['win_rate']:.0f}%  ({pnl['win_count']}W / {pnl['loss_count']}L)")
    print(f"  Avg win:           ${pnl['avg_win']:>8.2f}")
    print(f"  Avg loss:          ${pnl['avg_loss']:>8.2f}")

    if pnl["best_trade"]:
        b = pnl["best_trade"]
        print(f"  Best trade:         {b['ticker']:<6} +${b['pnl']:.2f}  ({b['pct']:+.0f}%)")
    if pnl["worst_trade"]:
        w = pnl["worst_trade"]
        print(f"  Worst trade:        {w['ticker']:<6}  ${w['pnl']:.2f}  ({w['pct']:+.0f}%)")

    if pnl["trade_pnls"]:
        print(f"\n  {'SYMBOL':<25} {'QTY':>4}  {'BUY':>6}  {'SELL':>6}  {'P&L':>8}  {'%':>7}")
        print(f"  {'-'*25} {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}")
        for t in pnl["trade_pnls"]:
            arrow = "▲" if t["pnl"] >= 0 else "▼"
            print(f"  {t['symbol']:<25} {t['qty']:>4}  {t['buy']:>6.2f}  {t['sell']:>6.2f}  {arrow}${abs(t['pnl']):>7.2f}  {t['pct']:>+6.0f}%")


def print_report(trades: list, log_stats: dict, trade_stats: dict, pnl: dict):
    sep  = "=" * 60
    sep2 = "-" * 40

    print(sep)
    print("  OPTIONSAGENT SANDBOX PERFORMANCE REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)

    # ── Agent Activity ─────────────────────────────────────────────────────
    print("\n📊 AGENT ACTIVITY")
    print(sep2)
    cycles   = log_stats.get("total_cycles", 0)
    no_sig   = log_stats.get("no_signal_cycles", 0)
    hit_rate = ((cycles - no_sig) / cycles * 100) if cycles > 0 else 0
    print(f"  Total scan cycles:      {cycles}")
    print(f"  Cycles with no signal:  {no_sig} ({100-hit_rate:.1f}%)")
    print(f"  Signal hit rate:        {hit_rate:.1f}%")
    print(f"  Trades blocked:         {log_stats.get('blocked_trades', 0)}")
    print(f"  Kill switch fires:      {log_stats.get('kill_switch_fires', 0)}")
    print(f"  Stop losses triggered:  {log_stats.get('stop_losses', 0)}")
    print(f"  Take profits triggered: {log_stats.get('take_profits', 0)}")
    print(f"  Errors in log:          {log_stats.get('errors', 0)}")

    # ── Market Regime ──────────────────────────────────────────────────────
    regimes = log_stats.get("regime_counts", {})
    if regimes:
        print("\n📈 MARKET REGIME DURING TRADING")
        print(sep2)
        total_r = sum(regimes.values())
        for regime, count in sorted(regimes.items()):
            pct = count / total_r * 100 if total_r > 0 else 0
            print(f"  {regime.upper():<12} {count:>4} cycles  ({pct:.0f}%)")

    # ── Trade Summary ──────────────────────────────────────────────────────
    if not trades:
        print("\n⚠️  NO TRADES RECORDED IN trades.json")
        print("  The agent may not have found qualifying signals yet.")
        print("  Try lowering min_signal_score in config.py")
        return

    print(f"\n💰 TRADE SUMMARY")
    print(sep2)
    print(f"  Total trades executed:  {trade_stats['total_trades']}")
    print(f"  Total capital deployed: ${trade_stats['total_deployed']:,.2f}")
    print(f"  Avg cost per trade:     ${trade_stats['avg_trade_cost']:.2f}")
    print(f"  Avg contracts per trade:{trade_stats['avg_contracts']:.1f}")
    print(f"  Avg signal score:       {trade_stats['avg_score']:.1f}/32")

    # ── P&L ───────────────────────────────────────────────────────────────
    print_pnl_section(pnl)

    # ── Direction Breakdown ────────────────────────────────────────────────
    print(f"\n📉 CALLS vs PUTS")
    print(sep2)
    for direction, count in trade_stats["by_direction"].items():
        pct = count / trade_stats["total_trades"] * 100
        print(f"  {direction:<8} {count:>3} trades ({pct:.0f}%)")

    # ── Top Tickers ────────────────────────────────────────────────────────
    print(f"\n🎯 MOST TRADED TICKERS")
    print(sep2)
    for ticker, count in list(trade_stats["by_ticker"].items())[:10]:
        pct = count / trade_stats["total_trades"] * 100
        print(f"  {ticker:<8} {count:>3} trades ({pct:.0f}%)")

    # ── Daily Activity ─────────────────────────────────────────────────────
    print(f"\n📅 TRADES BY DATE")
    print(sep2)
    for date, count in trade_stats["by_date"].items():
        bar = "█" * count
        print(f"  {date}  {count:>2} trades  {bar}")

    # ── Bonus Signal Analysis ──────────────────────────────────────────────
    print(f"\n🏦 BONUS SIGNAL BREAKDOWN")
    print(sep2)
    total = trade_stats["total_trades"]
    print(f"  Insider bonus helped:   {trade_stats['insider_helped']:>3}/{total} trades "
          f"(avg +{trade_stats['avg_insider_bonus']:.1f} pts)")
    print(f"  Catalyst bonus helped:  {trade_stats['catalyst_helped']:>3}/{total} trades "
          f"(avg +{trade_stats['avg_catalyst_bonus']:.1f} pts)")

    # ── Blocked Trade Reasons ──────────────────────────────────────────────
    blocked = log_stats.get("blocked_reasons", {})
    if blocked:
        print(f"\n🚫 WHY TRADES WERE BLOCKED")
        print(sep2)
        for reason, count in sorted(blocked.items(), key=lambda x: x[1], reverse=True)[:8]:
            print(f"  {count:>3}x  {reason}")

    # ── Recommendations ────────────────────────────────────────────────────
    print(f"\n💡 AUTO-ANALYSIS")
    print(sep2)

    if trade_stats["total_trades"] == 0:
        print("  ⚠️  No trades fired — min_signal_score may be too high.")
        print("      Try lowering it by 2 points in config.py")
    elif trade_stats["total_trades"] < 5:
        print("  ⚠️  Very few trades — agent is being very selective.")
        print("      Consider lowering min_signal_score slightly.")
    elif trade_stats["total_trades"] > 50:
        print("  ⚠️  High trade count — agent may be overtrading.")
        print("      Consider raising min_signal_score by 2 points.")

    sl = log_stats.get("stop_losses", 0)
    tp = log_stats.get("take_profits", 0)
    if sl + tp > 0:
        tp_rate = tp / (sl + tp) * 100
        print(f"\n  Take profit rate: {tp_rate:.0f}% of closed trades")
        if tp_rate < 30:
            print("  ⚠️  Low take-profit rate — consider lowering take_profit_pct")
            print("      or raising stop_loss_pct to give trades more room")
        elif tp_rate > 70:
            print("  ✅ Strong take-profit rate — strategy is working well")

    # P&L recommendation
    if "net_pnl" in pnl:
        net = pnl["net_pnl"]
        deployed = trade_stats["total_deployed"]
        roi = (net / deployed * 100) if deployed > 0 else 0
        print(f"\n  ROI on deployed capital: {roi:+.1f}%")
        if net > 0:
            print(f"  ✅ Net profitable — strategy is generating positive returns")
        else:
            print(f"  ⚠️  Net negative — review losing trades before going live")

    ks = log_stats.get("kill_switch_fires", 0)
    if ks > 3:
        print(f"\n  ⚠️  Kill switch fired {ks} times — daily_loss_limit may be too tight")
        print("      or strategy is underperforming. Review losing trades carefully.")

    print(f"\n{sep}")
    print("  Share this report for analysis and config tuning.")
    print(sep)


if __name__ == "__main__":
    print("Loading trade data...", flush=True)
    trades      = load_trades()
    log_stats   = parse_log()
    trade_stats = analyze_trades(trades)
    print("Fetching P&L from Tradier...", flush=True)
    pnl         = fetch_pnl_from_tradier(trades)
    print_report(trades, log_stats, trade_stats, pnl)
