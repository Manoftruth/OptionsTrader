#!/usr/bin/env bash
# setup_ec2.sh — bootstrap OptionsTrader on a fresh EC2 instance.
#
#   bash deploy/setup_ec2.sh
#
# Idempotent: safe to re-run after a redeploy.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
echo "▶ App directory: $APP_DIR"

# ── 1. Timezone ────────────────────────────────────────────────────────────────
# This matters more than it looks. Market-hours checks use pytz and are correct
# either way, but RiskManager._reset_if_new_day and PositionMonitor.entry_times
# call datetime.now() with no timezone. On a UTC box the "new trading day"
# reset — which clears the daily loss kill switch — would roll at 00:00 UTC,
# i.e. 8pm ET, in the middle of the evening rather than overnight.
CURRENT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
if [ "$CURRENT_TZ" != "America/New_York" ]; then
  echo "▶ Setting system timezone to America/New_York (was: $CURRENT_TZ)"
  sudo timedatectl set-timezone America/New_York
else
  echo "✓ Timezone already America/New_York"
fi

# ── 2. Python ──────────────────────────────────────────────────────────────────
# The codebase uses PEP 604 unions (dict | None), so 3.10 is the floor.
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    V=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])')
    if [ "$V" -ge 310 ]; then PY="$c"; break; fi
  fi
done

if [ -z "$PY" ]; then
  echo "▶ No Python >= 3.10 found — installing"
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3.11 python3.11-pip
    PY=python3.11
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-venv python3-pip
    PY=python3
  else
    echo "✗ Could not install Python automatically. Install 3.10+ and re-run."
    exit 1
  fi
fi
echo "✓ Using $PY ($($PY --version))"

# Debian/Ubuntu ship venv separately
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get install -y "${PY}-venv" >/dev/null 2>&1 || true
fi

# ── 3. Virtualenv ──────────────────────────────────────────────────────────────
if [ ! -d venv ]; then
  echo "▶ Creating venv"
  "$PY" -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
echo "▶ Installing requirements"
./venv/bin/pip install -r requirements.txt -q
echo "✓ Dependencies installed"

# ── 4. Config ──────────────────────────────────────────────────────────────────
if grep -q "YOUR_TRADIER_TOKEN_HERE" config.py 2>/dev/null; then
  echo ""
  echo "⚠  config.py still has PLACEHOLDER credentials."
  echo "   Edit it before starting the service:"
  echo "     nano $APP_DIR/config.py"
  echo ""
fi

echo "▶ Splicing v5 config keys (safe to re-run, only adds what is missing)"
./venv/bin/python splice_config.py config.py _config_v5_block.py || true

# ── 5. Verify ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ Running offline test suite"
./venv/bin/python test_v5.py || {
  echo "✗ Tests failed — do NOT start the service until this is resolved."
  exit 1
}

# ── 6. Log rotation ────────────────────────────────────────────────────────────
# agent.log reached 14 MB locally. On a small EBS volume an unbounded log is a
# genuine way to take the instance down.
if [ -d /etc/logrotate.d ]; then
  echo "▶ Installing logrotate rule"
  sudo tee /etc/logrotate.d/optionstrader >/dev/null <<EOF
$APP_DIR/agent.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
  echo "✓ agent.log will rotate daily, 14 days retained"
fi

# ── 7. systemd ─────────────────────────────────────────────────────────────────
echo "▶ Installing systemd unit"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$(whoami)|g" \
    deploy/optionstrader.service | sudo tee /etc/systemd/system/optionstrader.service >/dev/null
sudo systemctl daemon-reload
echo "✓ Unit installed (NOT started — start it deliberately)"

cat <<EOF

────────────────────────────────────────────────────────────────
Setup complete. Nothing is running yet.

Next:
  1. Fill in credentials:   nano $APP_DIR/config.py
  2. Live dry run:          ./venv/bin/python preflight.py
     (reads real data, places ZERO orders — check section 4b)
  3. Start:                 sudo systemctl start optionstrader
     Enable at boot:        sudo systemctl enable optionstrader
  4. Watch:                 journalctl -u optionstrader -f
     or:                    tail -f $APP_DIR/agent.log
  5. Stop:                  sudo systemctl stop optionstrader

Reminder: crash_mode_enabled ships False. The agent detects and logs
a crash regime but will not act on it until you arm it.
────────────────────────────────────────────────────────────────
EOF
