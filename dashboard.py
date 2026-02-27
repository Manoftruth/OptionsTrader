"""
OptionsAgent Real-Time Dashboard
Run this alongside agent.py to get a live web dashboard at http://localhost:5000
"""

from flask import Flask, jsonify, render_template_string
import json
import os
import requests
from datetime import datetime
from config import CONFIG

app = Flask(__name__)

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OptionsAgent Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #050810;
    --panel: #0a0f1e;
    --border: #0ff3;
    --green: #00ff88;
    --red: #ff3355;
    --yellow: #ffcc00;
    --blue: #00cfff;
    --dim: #334;
    --text: #c8d8e8;
    --glow-green: 0 0 10px #00ff8888;
    --glow-red: 0 0 10px #ff335588;
    --glow-blue: 0 0 10px #00cfff88;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Share Tech Mono', monospace;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Animated grid background */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(0,207,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,207,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  .container { position: relative; z-index: 1; padding: 20px; max-width: 1400px; margin: 0 auto; }

  /* Header */
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 16px 0 24px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }

  .logo {
    font-family: 'Orbitron', monospace;
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 4px;
    color: var(--blue);
    text-shadow: var(--glow-blue);
  }

  .logo span { color: var(--green); text-shadow: var(--glow-green); }

  .status-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 12px;
  }

  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: var(--glow-green);
    animation: pulse 2s infinite;
  }

  .status-dot.offline { background: var(--red); box-shadow: var(--glow-red); animation: none; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  /* Stat cards */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 24px;
  }

  .stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 20px;
    position: relative;
    overflow: hidden;
  }

  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--blue), transparent);
  }

  .stat-label {
    font-size: 10px;
    letter-spacing: 3px;
    color: #556;
    text-transform: uppercase;
    margin-bottom: 10px;
  }

  .stat-value {
    font-family: 'Orbitron', monospace;
    font-size: 28px;
    font-weight: 700;
    color: var(--blue);
    text-shadow: var(--glow-blue);
  }

  .stat-value.positive { color: var(--green); text-shadow: var(--glow-green); }
  .stat-value.negative { color: var(--red); text-shadow: var(--glow-red); }
  .stat-value.warning { color: var(--yellow); text-shadow: 0 0 10px #ffcc0088; }

  .stat-sub { font-size: 11px; color: #445; margin-top: 6px; }

  /* Main layout */
  .main-grid {
    display: grid;
    grid-template-columns: 1fr 380px;
    gap: 20px;
    margin-bottom: 20px;
  }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 20px;
  }

  .panel-title {
    font-family: 'Orbitron', monospace;
    font-size: 11px;
    letter-spacing: 4px;
    color: var(--blue);
    text-transform: uppercase;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }

  /* Chart */
  .chart-wrap { position: relative; height: 280px; }

  /* Positions table */
  .positions-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .positions-table th {
    text-align: left;
    padding: 8px 6px;
    font-size: 9px;
    letter-spacing: 2px;
    color: #445;
    border-bottom: 1px solid var(--dim);
  }
  .positions-table td { padding: 10px 6px; border-bottom: 1px solid #0a0f1e; }
  .positions-table tr:hover td { background: #0d1525; }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    font-size: 10px;
    letter-spacing: 1px;
    font-weight: bold;
  }
  .badge.call { background: #00ff8822; color: var(--green); border: 1px solid #00ff8844; }
  .badge.put  { background: #ff335522; color: var(--red);   border: 1px solid #ff335544; }

  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }

  /* Trade log */
  .trade-log { max-height: 320px; overflow-y: auto; }
  .trade-log::-webkit-scrollbar { width: 4px; }
  .trade-log::-webkit-scrollbar-track { background: var(--bg); }
  .trade-log::-webkit-scrollbar-thumb { background: var(--border); }

  .trade-entry {
    padding: 10px 0;
    border-bottom: 1px solid #0d1220;
    font-size: 11px;
    display: grid;
    grid-template-columns: 80px 60px 1fr auto;
    gap: 8px;
    align-items: center;
  }

  .trade-time { color: #334; font-size: 10px; }
  .trade-ticker { color: var(--blue); font-weight: bold; }
  .trade-detail { color: #667; }
  .trade-cost { color: var(--yellow); }

  .empty-state {
    text-align: center;
    color: #334;
    padding: 40px;
    font-size: 12px;
    letter-spacing: 2px;
  }

  /* Refresh indicator */
  .refresh-bar {
    height: 2px;
    background: var(--green);
    box-shadow: var(--glow-green);
    width: 100%;
    animation: shrink 10s linear infinite;
    transform-origin: left;
  }

  @keyframes shrink {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
  }

  .footer {
    text-align: center;
    font-size: 10px;
    color: #223;
    letter-spacing: 3px;
    padding: 20px 0;
    border-top: 1px solid var(--border);
    margin-top: 20px;
  }

  @media (max-width: 900px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .main-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo">OPTIONS<span>AGENT</span></div>
    <div class="status-bar">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusText">CONNECTING...</span>
      <span style="color:#334">|</span>
      <span id="clockEl"></span>
    </div>
  </header>

  <div class="refresh-bar"></div>

  <!-- Stat Cards -->
  <div class="stats-grid" style="margin-top:16px">
    <div class="stat-card">
      <div class="stat-label">Account Value</div>
      <div class="stat-value" id="accountValue">--</div>
      <div class="stat-sub">cash available</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total P&L</div>
      <div class="stat-value" id="totalPnl">--</div>
      <div class="stat-sub" id="totalPnlPct">--</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value warning" id="openPositions">--</div>
      <div class="stat-sub">active trades</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trades Today</div>
      <div class="stat-value" id="tradesToday">--</div>
      <div class="stat-sub">executed orders</div>
    </div>
  </div>

  <!-- Chart + Trade Log -->
  <div class="main-grid">
    <div class="panel">
      <div class="panel-title">⬡ Portfolio Value Over Time</div>
      <div class="chart-wrap">
        <canvas id="pnlChart"></canvas>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">⬡ Recent Trades</div>
      <div class="trade-log" id="tradeLog">
        <div class="empty-state">NO TRADES YET</div>
      </div>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="panel">
    <div class="panel-title">⬡ Open Positions</div>
    <div id="positionsContainer">
      <div class="empty-state">NO OPEN POSITIONS</div>
    </div>
  </div>

  <div class="footer">OPTIONSAGENT v1.0 &nbsp;|&nbsp; AUTO-REFRESH EVERY 10s &nbsp;|&nbsp; PAPER TRADING MODE</div>
</div>

<script>
// ── Clock ──────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clockEl').textContent =
    now.toLocaleTimeString('en-US', {hour12: false, timeZone: 'America/New_York'}) + ' ET';
}
setInterval(updateClock, 1000);
updateClock();

// ── Chart Setup ────────────────────────────────────────────────────────────
const ctx = document.getElementById('pnlChart').getContext('2d');
const pnlChart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'Portfolio Value',
      data: [],
      borderColor: '#00ff88',
      backgroundColor: 'rgba(0,255,136,0.05)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: '#00ff88',
      pointBorderColor: '#050810',
      fill: true,
      tension: 0.4
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#0a0f1e',
        borderColor: '#0ff3',
        borderWidth: 1,
        titleColor: '#00cfff',
        bodyColor: '#00ff88',
        callbacks: {
          label: ctx => '$' + ctx.parsed.y.toFixed(2)
        }
      }
    },
    scales: {
      x: {
        ticks: { color: '#334', font: { family: 'Share Tech Mono', size: 10 }, maxTicksLimit: 8 },
        grid: { color: '#0a0f1e' }
      },
      y: {
        ticks: {
          color: '#445',
          font: { family: 'Share Tech Mono', size: 10 },
          callback: v => '$' + v.toFixed(0)
        },
        grid: { color: '#0d1220' }
      }
    }
  }
});

let chartHistory = [];

// ── Fetch & Render ─────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const res = await fetch('/api/data');
    const data = await res.json();

    document.getElementById('statusDot').className = 'status-dot';
    document.getElementById('statusText').textContent = 'AGENT LIVE';

    // Stat cards
    const cash = data.cash || 0;
    document.getElementById('accountValue').textContent = '$' + cash.toFixed(2);

    const pnl = data.total_pnl || 0;
    const pnlEl = document.getElementById('totalPnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'positive' : 'negative');

    const startVal = data.starting_capital || cash;
    const pct = startVal > 0 ? ((pnl / startVal) * 100).toFixed(2) : '0.00';
    document.getElementById('totalPnlPct').textContent = (pnl >= 0 ? '+' : '') + pct + '% all time';

    document.getElementById('openPositions').textContent = data.open_positions || 0;
    document.getElementById('tradesToday').textContent = data.trades_today || 0;

    // Chart
    const now = new Date().toLocaleTimeString('en-US', {hour12: false, hour:'2-digit', minute:'2-digit'});
    chartHistory.push({ t: now, v: cash + (data.position_value || 0) });
    if (chartHistory.length > 60) chartHistory.shift();
    pnlChart.data.labels = chartHistory.map(d => d.t);
    pnlChart.data.datasets[0].data = chartHistory.map(d => d.v);
    pnlChart.update('none');

    // Positions
    renderPositions(data.positions || []);

    // Trade log
    renderTrades(data.recent_trades || []);

  } catch(e) {
    document.getElementById('statusDot').className = 'status-dot offline';
    document.getElementById('statusText').textContent = 'AGENT OFFLINE';
  }
}

function renderPositions(positions) {
  const el = document.getElementById('positionsContainer');
  if (!positions.length) {
    el.innerHTML = '<div class="empty-state">NO OPEN POSITIONS</div>';
    return;
  }
  let html = `<table class="positions-table">
    <thead><tr>
      <th>SYMBOL</th><th>TYPE</th><th>QTY</th><th>ENTRY</th><th>CURRENT</th><th>P&L</th><th>P&L %</th>
    </tr></thead><tbody>`;
  for (const p of positions) {
    const pnlClass = p.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pnlSign = p.pnl >= 0 ? '+' : '';
    const typeClass = p.type === 'CALL' ? 'call' : 'put';
    html += `<tr>
      <td style="color:#00cfff;font-weight:bold">${p.symbol}</td>
      <td><span class="badge ${typeClass}">${p.type}</span></td>
      <td>${p.quantity}</td>
      <td>$${p.entry_price.toFixed(2)}</td>
      <td>$${p.current_price.toFixed(2)}</td>
      <td class="${pnlClass}">${pnlSign}$${p.pnl.toFixed(2)}</td>
      <td class="${pnlClass}">${pnlSign}${p.pnl_pct.toFixed(1)}%</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderTrades(trades) {
  const el = document.getElementById('tradeLog');
  if (!trades.length) {
    el.innerHTML = '<div class="empty-state">NO TRADES YET</div>';
    return;
  }
  el.innerHTML = trades.slice().reverse().map(t => {
    const time = new Date(t.time).toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:false});
    return `<div class="trade-entry">
      <span class="trade-time">${time}</span>
      <span class="trade-ticker">${t.ticker}</span>
      <span class="trade-detail">${t.direction} ${t.contracts}x @ $${t.ask.toFixed(2)}</span>
      <span class="trade-cost">$${t.total_cost.toFixed(2)}</span>
    </div>`;
  }).join('');
}

// Fetch immediately then every 10 seconds
fetchData();
setInterval(fetchData, 10000);
</script>
</body>
</html>
'''

def get_tradier_data():
    """Fetch live account data from Tradier."""
    headers = {
        "Authorization": f"Bearer {CONFIG['tradier_token']}",
        "Accept": "application/json"
    }
    base = "https://sandbox.tradier.com/v1" if CONFIG["sandbox"] else "https://api.tradier.com/v1"

    result = {
        "cash": 0,
        "position_value": 0,
        "total_pnl": 0,
        "open_positions": 0,
        "positions": [],
        "starting_capital": CONFIG["capital_limit"]
    }

    try:
        # Balances
        r = requests.get(f"{base}/accounts/{CONFIG['account_id']}/balances", headers=headers)
        bal = r.json().get("balances", {})
        result["cash"] = float(bal.get("cash", {}).get("cash_available", 0))

        # Positions
        r = requests.get(f"{base}/accounts/{CONFIG['account_id']}/positions", headers=headers)
        positions = r.json().get("positions", {}).get("position", [])
        if isinstance(positions, dict):
            positions = [positions]

        result["open_positions"] = len(positions)

        for pos in positions:
            symbol = pos.get("symbol", "")
            qty = int(pos.get("quantity", 0))
            cost_basis = float(pos.get("cost_basis", 0))
            entry_price = cost_basis / (qty * 100) if qty > 0 else 0

            # Get current quote
            try:
                qr = requests.get(f"{base}/markets/quotes",
                    headers=headers, params={"symbols": symbol, "greeks": "true"})
                quote = qr.json().get("quotes", {}).get("quote", {})
                current = float(quote.get("last", entry_price) or entry_price)
            except:
                current = entry_price

            pnl = (current - entry_price) * qty * 100
            pnl_pct = ((current - entry_price) / entry_price * 100) if entry_price > 0 else 0
            result["position_value"] += current * qty * 100
            result["total_pnl"] += pnl

            # Guess direction from symbol
            direction = "CALL" if "C" in symbol[-10:] else "PUT"

            result["positions"].append({
                "symbol": symbol,
                "type": direction,
                "quantity": qty,
                "entry_price": entry_price,
                "current_price": current,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })
    except Exception as e:
        print(f"Tradier fetch error: {e}")

    return result


def load_trades():
    """Load trade history from trades.json."""
    trades = []
    if os.path.exists("trades.json"):
        with open("trades.json") as f:
            for line in f:
                try:
                    trades.append(json.loads(line.strip()))
                except:
                    pass
    return trades


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/data")
def api_data():
    live = get_tradier_data()
    trades = load_trades()

    # Count trades today
    today = datetime.now().date().isoformat()
    trades_today = sum(1 for t in trades if t.get("time", "").startswith(today))

    return jsonify({
        **live,
        "trades_today": trades_today,
        "recent_trades": trades[-20:] if trades else []
    })


if __name__ == "__main__":
    print("🖥️  Dashboard running at http://localhost:5000")
    print("   Open this in your browser while agent.py is running")
    app.run(debug=False, port=5000)