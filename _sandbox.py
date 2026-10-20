"""
Test sandbox for OptionsTrader.

Make this the FIRST import in test.py and test_newfeatures.py, before any
project import:

    import _sandbox  # noqa: F401

It does three things:

  1. chdir() into a fresh temp directory, so anything that opens a state file
     by relative path (trades.json, ...) writes there and not into the
     production copy.

  2. Points OPTIONSTRADER_STATE_DIR at that same directory. agent.RiskManager
     (risk_state.json), regime_state (regime_state.json) and shadow
     (shadow_pending.jsonl, shadow_resolved.jsonl) resolve their paths from
     __file__ unless this is set, so chdir() alone would miss them.

  3. Hashes the production state files before the run and aborts with exit
     code 2 if any of them changed by the time the interpreter exits. That
     catches writers that build their path from __file__ and have no override
     yet, which neither chdir() nor the env var can redirect.

That category was empty as of 2026-09-10. entry_prices.json (plus its .peaks
and .spreads sidecars) via OptionsAgent._entry_prices_path, and
trade_results.json via agent.py and trade_judge.TradeJudge.RESULTS_FILE, were
the last known members; all four now resolve through agent._state_dir() and
honour OPTIONSTRADER_STATE_DIR. They stay in PROTECTED as a backstop, and the
guard stays because the list of __file__-based writers was assembled by grep
and may not be complete.

Run tests outside market hours. If the live agent is writing trades.json at the
same time, the guard will fire on the agent's own writes.
"""
import atexit
import hashlib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))

# Production state files tests must never touch.
PROTECTED = [
    "trades.json",
    "risk_state.json",
    "regime_state.json",
    "entry_prices.json",
    "entry_prices.json.peaks",
    "entry_prices.json.spreads",
    "trade_results.json",
    "shadow_pending.jsonl",
    "shadow_resolved.jsonl",
]

# Read-only inputs the code expects to find in the working directory.
# config.py is imported off sys.path, not opened, so this copy is belt-and-braces.
FIXTURES = ["config.py"]


def _digest(name):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


_before = {name: _digest(name) for name in PROTECTED}

SANDBOX = tempfile.mkdtemp(prefix="oa-test-")
for name in FIXTURES:
    src = os.path.join(ROOT, name)
    if os.path.exists(src):
        shutil.copy(src, SANDBOX)
os.chdir(SANDBOX)
os.environ["OPTIONSTRADER_STATE_DIR"] = SANDBOX
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
print(f"[sandbox] cwd={SANDBOX}")


@atexit.register
def _verify_untouched():
    changed = [n for n in PROTECTED if _digest(n) != _before[n]]
    if changed:
        sys.stderr.write(
            f"\n*** TESTS MODIFIED PRODUCTION STATE: {changed} ***\n"
            f"*** find the writer and give it a path override ***\n"
            f"*** (pattern: honour OPTIONSTRADER_STATE_DIR, as in "
            f"agent.RiskManager._state_path) ***\n"
        )
        sys.stderr.flush()
        os._exit(2)
