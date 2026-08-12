# Deploying to EC2

## 1. Copy the folder up

From your Mac, in the directory holding the extracted `OptionsTrader/`:

```bash
# Amazon Linux AMIs use ec2-user; Ubuntu AMIs use ubuntu.
scp -i ~/.ssh/YOUR_KEY.pem -r OptionsTrader ec2-user@YOUR_EC2_HOST:~/
```

Or push the tarball and extract on the far side — faster over a slow link:

```bash
scp -i ~/.ssh/YOUR_KEY.pem optionstrader-v5.1.tar.gz ec2-user@YOUR_EC2_HOST:~/
ssh -i ~/.ssh/YOUR_KEY.pem ec2-user@YOUR_EC2_HOST 'tar xzf optionstrader-v5.1.tar.gz'
```

For later updates, `rsync` only sends what changed and skips the junk:

```bash
rsync -avz --delete \
  --exclude venv --exclude .git --exclude __pycache__ \
  --exclude 'agent.log*' --exclude config.py \
  -e "ssh -i ~/.ssh/YOUR_KEY.pem" \
  ./OptionsTrader/ ec2-user@YOUR_EC2_HOST:~/OptionsTrader/
```

`config.py` is excluded on purpose — you do not want a local edit overwriting
the credentials on the server, or vice versa.

## 2. Bootstrap

```bash
ssh -i ~/.ssh/YOUR_KEY.pem ec2-user@YOUR_EC2_HOST
cd ~/OptionsTrader
bash deploy/setup_ec2.sh
```

That sets the timezone, installs Python 3.10+ and a venv, splices the v5 config
keys, runs the 104-test offline suite, installs logrotate, and installs a
systemd unit. **It does not start anything.**

## 3. Credentials

```bash
nano ~/OptionsTrader/config.py
```

Set `tradier_token`, `account_id`, `anthropic_api_key`, and `sandbox`.

```bash
chmod 600 ~/OptionsTrader/config.py
```

## 4. Dry run before you start anything

```bash
cd ~/OptionsTrader && ./venv/bin/python preflight.py
```

Reads live data, places zero orders. **Section 4b is the one that matters** —
it shows whether your crash budget reaches a qualifying deep-ITM contract on
the real chains, and on which days. Rows marked ✗ are days crash mode would
stand down.

## 5. Run it

```bash
sudo systemctl start optionstrader     # start now
sudo systemctl enable optionstrader    # and on boot
journalctl -u optionstrader -f         # follow
sudo systemctl stop optionstrader      # stop
```

---

## Things that bite on EC2 specifically

**Timezone.** `setup_ec2.sh` sets the box to `America/New_York`. Market-hours
checks use pytz and are correct either way, but `RiskManager._reset_if_new_day`
and `PositionMonitor.entry_times` call bare `datetime.now()`. On a UTC box the
new-trading-day reset — which clears the daily-loss kill switch — would roll at
00:00 UTC, i.e. 8pm ET. Don't skip this step.

**Clean state.** `entry_prices.json`, `entry_prices.json.peaks` and
`.spreads` are deliberately **not** in the bundle. They describe positions open
in your Mac's view of the account. Starting the server with stale entries means
it monitors positions it does not have. It rebuilds from Tradier on first
cycle. `trades.json` and `trade_results.json` **are** included — TradeJudge
reads the history and the per-ticker win rates feed its decisions.

**Two agents, one account.** If the Mac instance is still running, stop it
before starting the EC2 one. Two agents against the same Tradier account will
both see the same positions, both decide to close, and both submit sells. The
lock added in v5.0 protects against threads within one process, not against a
second process.

**Disk.** `agent.log` reached 14 MB locally over a few months. Logrotate is
installed by the setup script. If you skipped it, watch `df -h`.

**Outbound network.** The agent needs HTTPS to `api.tradier.com` (or
`sandbox.tradier.com`), `query1/query2.finance.yahoo.com`, and
`api.anthropic.com` if TradeJudge is on. Default EC2 egress allows all of this;
a locked-down security group or a private subnet without a NAT gateway will
not.

**Instance size.** t3.micro is enough — the workload is network-bound, not CPU.
The async scan holds 8 concurrent connections. Do not raise
`scan_concurrency` above ~12; Yahoo rate limits, and then you have no data,
which is worse than being slow.

**Market holidays.** The loop skips weekends but not NYSE holidays. On a
holiday it scans, finds nothing tradeable, and burns API calls. Harmless, just
noisy.
