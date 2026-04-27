"""
OptionsAgent - Performance Report Generator
Run this after your sandbox period to generate a summary report.
Share the output with Claude for analysis and tuning.

Usage:
    python report.py                        # all time
    python report.py --since 2026-03-18     # filter from a date forward
"""

import argparse
import json
import re
import requests
from datetime import datetime, timedelta
from collections import defaultdict


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only include data on or after this date (YYYY-MM-DD)",
    )
    args = parser.parse_args()
    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
            print(f"  [filter] Reporting from {since} onward", flush=True)
        except ValueError:
            print(f"  [filter] Invalid --since date '{args.since}', ignoring filter", flush=True)
    return since


def load_trades(path="trades.json", since=None) -> list:
    trades = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        t = json.loads(line)
                        if since:
                            time_str = t.get("time", "")
                            if time_str and datetime.strptime(time_str[:10], "%Y-%m-%d").date() < since:
                                continue
                        trades.append(t)
                    except:
                        continue
    except FileNotFoundError:
        print(f"WARNING: {path} not found")
    return trades


def parse_log(path="agent.log", since=None) -> dict:
    """Extract key events from agent.log without loading the whole file."""
    stats = {
        "total_cycles":        0,
        "no_signal_cycles":    0,
        "blocked_trades":      0,
        "kill_switch_fires":   0,
        "stop_losses":         0,
        "take_profits":        0,
        "errors":              0,
        "vix_skipped":         0,
        "regime_counts":       defaultdict(int),
        "blocked_reasons":     defaultdict(int),
        "signal_scores_seen":  [],
        "tickers_scanned":     defaultdict(int),
    }

    false_positive_patterns = [
        "no high-confidence",
        "no signal",
        "blocked",
        "regime",
        "kill switch",
        "stop loss",
        "take profit",
    ]

    # Matches a leading timestamp like: 2026-03-18 09:32:01 or 2026-03-18T09:32:01
    # Also catches bare dates anywhere on the line as a fallback.
    ts_re   = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")

    current_line_date = None  # last known date, carried forward for timestamp-less lines

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                # --- Always update date BEFORE the skip check ---
                # Prefer a leading timestamp (most reliable); fall back to any date in line.
                tm = ts_re.match(line)
                if tm:
                    try:
                        current_line_date = datetime.strptime(tm.group(1), "%Y-%m-%d").date()
                    except ValueError:
                        pass
                else:
                    dm = date_re.search(line)
                    if dm:
                        try:
                            current_line_date = datetime.strptime(dm.group(1), "%Y-%m-%d").date()
                        except ValueError:
                            pass

                # Apply date filter — skip lines before `since`
                if since and current_line_date and current_line_date < since:
                    continue

                line_lower = line.lower()

                if "OptionsAgent cycle" in line or "[CYCLE]" in line or "--- scan cycle" in line.lower():
                    stats["total_cycles"] += 1

                if "no high-confidence signals" in line_lower or "no signals found" in line_lower:
                    stats["no_signal_cycles"] += 1

                elif "risk manager blocked" in line_lower:
                    stats["blocked_trades"] += 1
                    reason = line.split("Risk manager blocked:")[-1].strip() if "Risk manager blocked:" in line else "unknown"
                    stats["blocked_reasons"][reason[:60]] += 1

                elif "kill switch" in line_lower and any(w in line_lower for w in ("fired", "triggered", "activated", "🚨")):
                    stats["kill_switch_fires"] += 1

                elif ("✅ take profit:" in line_lower or "🎯 take profit:" in line_lower
                      or ("take profit" in line_lower and "selling" in line_lower)):
                    stats["take_profits"] += 1

                elif ("🛑 stop loss:" in line_lower or "❌ stop loss:" in line_lower
                      or ("stop loss" in line_lower and "selling" in line_lower)):
                    stats["stop_losses"] += 1

                elif "vix" in line_lower and "skipping scan" in line_lower:
                    stats["vix_skipped"] += 1

                elif line.strip().startswith("ERROR") or " ERROR " in line or "[ERROR]" in line:
                    if not any(fp in line_lower for fp in false_positive_patterns):
                        stats["errors"] += 1

                if "market regime:" in line_lower:
                    regime = line.split(":")[-1].strip().lower().split()[0]
                    if regime in ("bull", "bear", "neutral"):
                        stats["regime_counts"][regime] += 1

                if "score=" in line:
                    m = re.search(r"score=([\d.]+)", line)
                    if m:
                        stats["signal_scores_seen"].append(float(m.group(1)))

                if "✅" in line:
                    m = re.search(r"✅\s+([A-Z]{1,6})[:\s]", line)
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

    scores           = [float(t.get("score", 0)) for t in trades if t.get("score") is not None]
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

def fetch_pnl_from_tradier(trades: list, since=None) -> dict:
    """
    Query Tradier account history to calculate P&L.
    If `since` is set, only include trades on or after that date.
    """
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

    print(f"  [Tradier] Using {'SANDBOX' if sandbox else 'LIVE'} endpoint", flush=True)

    all_events = []
    for limit in [500]:
        try:
            resp = requests.get(
                f"{base_url}/v1/accounts/{account_id}/history",
                headers=headers,
                params={"limit": limit, "type": "trade"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return {"error": f"Tradier API error: {e}"}

        history = data.get("history", {})
        if not history or history == "null":
            if sandbox:
                print("  [Tradier] Sandbox returned nothing — trying LIVE endpoint...", flush=True)
                try:
                    resp2 = requests.get(
                        f"https://api.tradier.com/v1/accounts/{account_id}/history",
                        headers=headers,
                        params={"limit": limit, "type": "trade"},
                        timeout=15,
                    )
                    resp2.raise_for_status()
                    data = resp2.json()
                    history = data.get("history", {})
                except Exception as e:
                    return {"error": f"Tradier live fallback error: {e}"}

        if not history or history == "null":
            return {"error": "No trade history found in Tradier account."}

        events = history.get("event", [])
        if isinstance(events, dict):
            events = [events]
        all_events.extend(events)

    # Apply date filter to Tradier events
    if since:
        all_events = [
            e for e in all_events
            if e.get("date", "")[:10] >= str(since)
        ]
        print(f"  [Tradier] After date filter ({since}): {len(all_events)} events remain", flush=True)

    option_events = [
        e for e in all_events
        if e.get("type") == "trade" and e.get("trade", {}).get("trade_type") == "option"
    ]

    print(f"  [Tradier] {len(all_events)} history events, {len(option_events)} option trades", flush=True)

    if not option_events:
        return {"error": f"No option trades found in history ({len(all_events)} total events were non-option or empty)."}

    by_symbol = defaultdict(list)
    for e in option_events:
        trade  = e.get("trade", {})
        symbol = trade.get("symbol", "UNKNOWN")
        by_symbol[symbol].append({
            "symbol":     symbol,
            "date":       e.get("date", ""),
            "amount":     float(e.get("amount", 0)),
            "price":      float(trade.get("price", 0)),
            "quantity":   float(trade.get("quantity", 0)),
            "commission": float(trade.get("commission", 0)),
        })

    trade_pnls   = []
    total_profit = 0.0
    total_loss   = 0.0
    open_trades  = []

    for symbol, events in by_symbol.items():
        buys  = [e for e in events if e["quantity"] > 0]
        sells = [e for e in events if e["quantity"] < 0]

        if not buys:
            continue
        if not sells:
            open_trades.append(symbol)
            continue

        buy_qty  = sum(e["quantity"]       for e in buys)
        sell_qty = sum(abs(e["quantity"])  for e in sells)

        avg_buy  = sum(e["price"] * e["quantity"]       for e in buys)  / buy_qty
        avg_sell = sum(e["price"] * abs(e["quantity"])  for e in sells) / sell_qty
        qty      = min(buy_qty, sell_qty)

        gross_pnl   = (avg_sell - avg_buy) * qty * 100
        commissions = sum(e["commission"] for e in events)
        pnl         = gross_pnl - commissions
        pct         = ((avg_sell - avg_buy) / avg_buy * 100) if avg_buy > 0 else 0

        m = re.match(r"([A-Z]+)\d", symbol)
        ticker = m.group(1) if m else symbol

        trade_pnls.append({
            "symbol":      symbol,
            "ticker":      ticker,
            "pnl":         pnl,
            "pct":         pct,
            "qty":         qty,
            "buy":         avg_buy,
            "sell":        avg_sell,
            "commissions": commissions,
        })

        if pnl >= 0:
            total_profit += pnl
        else:
            total_loss += pnl

    if not trade_pnls:
        msg = "Could not match any buy/sell pairs in history"
        if open_trades:
            msg += f" — {len(open_trades)} positions may still be open: {', '.join(open_trades[:5])}"
        return {"error": msg}

    wins  = [t for t in trade_pnls if t["pnl"] >= 0]
    losses = [t for t in trade_pnls if t["pnl"] <  0]
    net   = total_profit + total_loss
    total_commissions = sum(t["commissions"] for t in trade_pnls)

    result = {
        "trade_pnls":        sorted(trade_pnls, key=lambda x: x["pnl"], reverse=True),
        "total_profit":      total_profit,
        "total_loss":        total_loss,
        "net_pnl":           net,
        "total_commissions": total_commissions,
        "win_count":         len(wins),
        "loss_count":        len(losses),
        "avg_win":           sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0,
        "avg_loss":          sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
        "best_trade":        max(trade_pnls, key=lambda x: x["pnl"])  if trade_pnls else None,
        "worst_trade":       min(trade_pnls, key=lambda x: x["pnl"])  if trade_pnls else None,
        "win_rate":          len(wins) / len(trade_pnls) * 100         if trade_pnls else 0,
    }

    if open_trades:
        result["open_positions"] = open_trades

    return result


def print_pnl_section(pnl: dict):
    sep2 = "-" * 40
    print(f"\n💵 P&L SUMMARY  (live from Tradier)")
    print(sep2)

    if "error" in pnl:
        print(f"  ⚠️  {pnl['error']}")
        return

    net       = pnl["net_pnl"]
    net_emoji = "✅" if net >= 0 else "❌"

    print(f"  Gross profit:      ${pnl['total_profit']:>8.2f}")
    print(f"  Gross loss:        ${pnl['total_loss']:>8.2f}")
    print(f"  Commissions paid:  ${pnl.get('total_commissions', 0):>8.2f}")
    print(f"  {net_emoji} Net P&L:          ${net:>8.2f}")
    print(f"  Win rate:           {pnl['win_rate']:.0f}%  ({pnl['win_count']}W / {pnl['loss_count']}L)")
    print(f"  Avg win:           ${pnl['avg_win']:>8.2f}")
    print(f"  Avg loss:          ${pnl['avg_loss']:>8.2f}")

    if pnl.get("best_trade"):
        b = pnl["best_trade"]
        print(f"  Best trade:         {b['ticker']:<6} +${b['pnl']:.2f}  ({b['pct']:+.0f}%)")
    if pnl.get("worst_trade"):
        w = pnl["worst_trade"]
        print(f"  Worst trade:        {w['ticker']:<6}  ${w['pnl']:.2f}  ({w['pct']:+.0f}%)")

    open_pos = pnl.get("open_positions", [])
    if open_pos:
        print(f"\n  ⏳ {len(open_pos)} open position(s) — no closing trade found yet:")

    if pnl.get("trade_pnls") or open_pos:
        print(f"\n  {'SYMBOL':<25} {'QTY':>4}  {'BUY':>6}  {'SELL':>6}  {'P&L':>8}  {'%':>7}")
        print(f"  {'-'*25} {'-'*4}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*7}")
        for t in pnl.get("trade_pnls", []):
            arrow = "▲" if t["pnl"] >= 0 else "▼"
            print(f"  {t['symbol']:<25} {t['qty']:>4}  {t['buy']:>6.2f}  {t['sell']:>6.2f}  {arrow}${abs(t['pnl']):>7.2f}  {t['pct']:>+6.0f}%")
        for sym in open_pos:
            print(f"  {sym:<25}    ?       ?       ?      OPEN        ?")


def print_report(trades: list, log_stats: dict, trade_stats: dict, pnl: dict, since=None):
    sep  = "=" * 60
    sep2 = "-" * 40

    print(sep)
    print("  OPTIONSAGENT PERFORMANCE REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if since:
        print(f"  Period:    {since} → today")
    print(sep)

    print("\n📊 AGENT ACTIVITY")
    print(sep2)
    cycles   = log_stats.get("total_cycles", 0)
    no_sig   = log_stats.get("no_signal_cycles", 0)
    no_sig   = min(no_sig, cycles)
    hit_rate = ((cycles - no_sig) / cycles * 100) if cycles > 0 else 0
    print(f"  Total scan cycles:      {cycles}")
    print(f"  Cycles with no signal:  {no_sig} ({100 - hit_rate:.1f}%)")
    print(f"  Signal hit rate:        {hit_rate:.1f}%")
    vix_skip = log_stats.get("vix_skipped", 0)
    if vix_skip:
        print(f"  VIX-skipped cycles:     {vix_skip} ({vix_skip/cycles*100:.1f}% of cycles)" if cycles else f"  VIX-skipped cycles:     {vix_skip}")
    print(f"  Trades blocked:         {log_stats.get('blocked_trades', 0)}")
    print(f"  Kill switch fires:      {log_stats.get('kill_switch_fires', 0)}")
    print(f"  Stop losses triggered:  {log_stats.get('stop_losses', 0)}")
    print(f"  Take profits triggered: {log_stats.get('take_profits', 0)}")
    print(f"  Errors in log:          {log_stats.get('errors', 0)}")

    regimes = log_stats.get("regime_counts", {})
    if regimes:
        print("\n📈 MARKET REGIME DURING TRADING")
        print(sep2)
        total_r = sum(regimes.values())
        for regime, count in sorted(regimes.items()):
            pct = count / total_r * 100 if total_r > 0 else 0
            print(f"  {regime.upper():<12} {count:>4} cycles  ({pct:.0f}%)")

    if not trades:
        print("\n⚠️  NO TRADES RECORDED IN trades.json")
        if since:
            print(f"  No trades found on or after {since}.")
        else:
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

    print_pnl_section(pnl)

    print(f"\n📉 CALLS vs PUTS")
    print(sep2)
    for direction, count in trade_stats["by_direction"].items():
        pct = count / trade_stats["total_trades"] * 100
        print(f"  {direction:<8} {count:>3} trades ({pct:.0f}%)")

    print(f"\n🎯 MOST TRADED TICKERS")
    print(sep2)
    for ticker, count in list(trade_stats["by_ticker"].items())[:10]:
        pct = count / trade_stats["total_trades"] * 100
        print(f"  {ticker:<8} {count:>3} trades ({pct:.0f}%)")

    print(f"\n📅 TRADES BY DATE")
    print(sep2)
    for date, count in trade_stats["by_date"].items():
        bar = "█" * count
        print(f"  {date}  {count:>2} trades  {bar}")

    print(f"\n🏦 BONUS SIGNAL BREAKDOWN")
    print(sep2)
    total = trade_stats["total_trades"]
    print(f"  Insider bonus helped:   {trade_stats['insider_helped']:>3}/{total} trades "
          f"(avg +{trade_stats['avg_insider_bonus']:.1f} pts)")
    print(f"  Catalyst bonus helped:  {trade_stats['catalyst_helped']:>3}/{total} trades "
          f"(avg +{trade_stats['avg_catalyst_bonus']:.1f} pts)")

    blocked = log_stats.get("blocked_reasons", {})
    if blocked:
        print(f"\n🚫 WHY TRADES WERE BLOCKED")
        print(sep2)
        for reason, count in sorted(blocked.items(), key=lambda x: x[1], reverse=True)[:8]:
            print(f"  {count:>3}x  {reason}")

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

    if "net_pnl" in pnl:
        net      = pnl["net_pnl"]
        deployed = trade_stats["total_deployed"]
        roi      = (net / deployed * 100) if deployed > 0 else 0
        print(f"\n  ROI on deployed capital: {roi:+.1f}%")
        if net > 0:
            print(f"  ✅ Net profitable — strategy is generating positive returns")
        else:
            print(f"  ⚠️  Net negative — review losing trades before scaling up")

    ks = log_stats.get("kill_switch_fires", 0)
    if ks > 3:
        print(f"\n  ⚠️  Kill switch fired {ks} times — daily_loss_limit may be too tight")
        print("      or strategy is underperforming. Review losing trades carefully.")

    print(f"\n{sep}")
    print("  Share this report for analysis and config tuning.")
    print(sep)


if __name__ == "__main__":
    since = parse_args()

    print("Loading trade data...", flush=True)
    trades      = load_trades(since=since)
    log_stats   = parse_log(since=since)
    trade_stats = analyze_trades(trades)
    print("Fetching P&L from Tradier...", flush=True)
    pnl         = fetch_pnl_from_tradier(trades, since=since)
    print_report(trades, log_stats, trade_stats, pnl, since=since)
