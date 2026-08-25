#!/usr/bin/env python3
"""
add_v516_keys.py — add the v5.13-v5.16 settings to config.py, safely.

config.py holds live broker and API credentials, so it is never overwritten
wholesale. This inserts only missing keys, writes to a temp file, confirms the
result still compiles AND still contains the credential keys, and only then
swaps it in — keeping a timestamped backup either way.

Re-runnable. Keys already present are left exactly as they are.

    ./venv/bin/python add_v516_keys.py            # show what would change
    ./venv/bin/python add_v516_keys.py --apply
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.py"
ANCHOR = '"max_trade_size"'

NEW_KEYS = [
    ('"contract_target_delta"', '0.45',
     "v5.16 aim at mid-band delta, not spot*1.02 (0.0 = old behaviour)"),
    ('"respect_config_watchlist"', 'True',
     "v5.14 actually scan the watchlist below instead of 9 hardcoded megacaps"),
    ('"scale_available_with_gains"', 'False',
     "v5.13 OFF: deployable cash stays capped at capital_limit"),
    ('"gain_reinvest_pct"', '0.75',
     "v5.13 fraction of gains deployable when the above is True"),
]

# If any of these vanish from the file, something went badly wrong — abort.
MUST_SURVIVE = ("tradier_token", "watchlist", "capital_limit", "max_trade_size")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not CONFIG_PATH.exists():
        print(f"No config.py at {CONFIG_PATH}")
        return 1
    src = CONFIG_PATH.read_text()

    missing = [(k, v, c) for k, v, c in NEW_KEYS if k not in src]
    if not missing:
        print("All v5.13-v5.16 keys already present. Nothing to do.")
        return 0

    anchor_idx = None
    for i, line in enumerate(src.splitlines()):
        if ANCHOR in line:
            anchor_idx = i
            break
    if anchor_idx is None:
        print(f"Could not find {ANCHOR} to anchor the insert. Add by hand:")
        for k, v, c in missing:
            print(f"    {k:<30} {v},   # {c}")
        return 1

    lines = src.splitlines(keepends=True)
    block = "".join(f"    {k+':':<32}{v},   # {c}\n" for k, v, c in missing)
    out = "".join(lines[:anchor_idx + 1]) + block + "".join(lines[anchor_idx + 1:])

    print(f"Inserting {len(missing)} key(s) after the {ANCHOR} line:\n")
    print(block)

    if not a.apply:
        print("Dry run. Re-run with --apply to write.")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(out)
        tmp = fh.name
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        print(f"REFUSING TO WRITE — result does not compile:\n{e}")
        return 1
    for key in MUST_SURVIVE:
        if key not in out:
            print(f"REFUSING TO WRITE — '{key}' missing from the result.")
            return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = CONFIG_PATH.with_suffix(f".py.bak_{stamp}")
    shutil.copy2(CONFIG_PATH, backup)
    shutil.move(tmp, CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)
    print(f"Written. Backup at {backup.name}")
    print("Verify:  ./venv/bin/python -c 'from config import CONFIG; "
          "print(CONFIG[\"respect_config_watchlist\"], "
          "CONFIG[\"contract_target_delta\"])'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
