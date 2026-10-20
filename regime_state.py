"""
regime_state.py — remember what regime we were in, and make leaving one hard

The gap this fills
------------------
crash_mode.assess() is stateless. It counts votes fresh every cycle and
classifies from that alone; `prior_regime` is only the 20/50 EMA read from the
SAME cycle, not the previous cycle's verdict. Two consequences, both bad in
exactly the market this system is being aimed at:

**Whipsaw.** VIX oscillating around 28.0 flips the regime cycle to cycle. The
agent would alternate between buying index calls and buying inverse-ETF calls
every few minutes, paying the spread each way.

**Bear-market rallies.** The moment crash votes fall from 2 to 1, the regime
returns to BULL and the agent immediately resumes buying calls. October and
November 2008 contained several +10% bounces inside a -40% decline. A
stateless classifier buys the top of every one of them. The sharpest rallies
in history happen during bear markets, and that is not a coincidence —
forced short covering into thin books is what makes them sharp.

The three states
----------------
    NORMAL    trade as configured
    CRASH     downside structures; entered on votes >= crash_votes_required
    RECOVERY  stand down; entered when a crash ENDS, exited only on proof

Entering CRASH is deliberately easy — one cycle, because crashes are fast and
being late is the expensive error. Leaving it is deliberately hard, because
being early is the expensive error there. The asymmetry is the whole point.

RECOVERY exists because "the crash stopped" and "it is safe to buy calls" are
different claims separated by weeks. It requires calm across several distinct
TRADING DAYS, not cycles — sixty cycles inside one quiet afternoon is one
observation, not sixty.

State persists to disk. A restart mid-crash must not forget it was in one;
that lesson already cost a working kill switch this week.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("OptionsAgent")

STATE_FILE = "regime_state.json"

DEFAULTS = {
    "regime_state_machine":     True,
    # False: CRASH exits straight to NORMAL, no RECOVERY stand-down.
    "recovery_enabled":         False,
    # Leaving CRASH: votes must fall to at most this, and stay there.
    "crash_exit_votes":         1,
    "crash_exit_cycles":        3,
    # Leaving RECOVERY: all of these, on this many distinct trading days.
    "recovery_confirm_days":    3,
    "recovery_max_vix":         22.0,
    "recovery_max_term":        0.95,   # back in contango
    "recovery_require_uptrend": True,   # SPY back above its 20d EMA
}


def _path() -> Path:
    # v5.21: redirectable so the offline suite never reads or writes the live
    # agent's regime memory. See the note in agent.py's _state_path.
    import os
    base = os.environ.get("OPTIONSTRADER_STATE_DIR")
    return (Path(base) if base else Path(__file__).parent) / STATE_FILE


class RegimeState:
    """Persistent NORMAL → CRASH → RECOVERY → NORMAL machine."""

    def __init__(self, cfg: dict | None = None):
        self.conf = {**DEFAULTS, **(cfg or {})}
        self.state = "normal"
        self.entered = None
        self.calm_cycles = 0
        self.confirm_days: list[str] = []
        self.last_crash_day = None
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            p = _path()
            if not p.exists():
                return
            d = json.loads(p.read_text())
            self.state = d.get("state", "normal")
            self.entered = d.get("entered")
            self.calm_cycles = int(d.get("calm_cycles", 0))
            self.confirm_days = list(d.get("confirm_days", []))
            self.last_crash_day = d.get("last_crash_day")
            if self.state != "normal":
                log.info(f"   🧭 Restored regime state: {self.state.upper()} "
                         f"(since {self.entered})")
        except Exception as e:
            log.warning(f"regime state unreadable ({e}) — starting at NORMAL")

    def _save(self) -> None:
        try:
            _path().write_text(json.dumps({
                "state": self.state, "entered": self.entered,
                "calm_cycles": self.calm_cycles,
                "confirm_days": self.confirm_days,
                "last_crash_day": self.last_crash_day,
            }))
        except Exception as e:
            log.warning(f"could not persist regime state ({e})")

    # ── transitions ────────────────────────────────────────────────────────

    def _enter(self, state: str, why: str) -> None:
        self.state = state
        self.entered = datetime.now().isoformat(timespec="seconds")
        self.calm_cycles = 0
        self.confirm_days = []
        log.warning(f"🧭 REGIME STATE → {state.upper()}: {why}")
        self._save()

    def update(self, rep, vix: float, term: float,
               above_ema20: bool, today: str | None = None) -> str:
        """Advance the machine one cycle. Returns the EFFECTIVE regime."""
        if not self.conf.get("regime_state_machine", True):
            return rep.regime

        today = today or datetime.now().date().isoformat()
        votes = getattr(rep, "crash_votes", 0)
        crashing = rep.regime == "crash"

        # A crash re-arms from any state, immediately. Late is expensive.
        if crashing:
            if self.state != "crash":
                self._enter("crash", f"{votes} crash votes")
            self.last_crash_day = today
            self.calm_cycles = 0
            self._save()
            return "crash"

        if self.state == "crash":
            if votes <= self.conf["crash_exit_votes"]:
                self.calm_cycles += 1
                need = self.conf["crash_exit_cycles"]
                if self.calm_cycles >= need:
                    if not self.conf.get("recovery_enabled", True):
                        self._enter("normal",
                                    f"crash votes at {votes} for {need} cycles — "
                                    f"RECOVERY disabled, resuming normal trading")
                        return rep.regime
                    self._enter("recovery",
                                f"crash votes at {votes} for {need} cycles — "
                                f"standing down until the tape proves itself")
                    return "recovery"
                log.info(f"   🧭 CRASH holding: votes {votes}, calm "
                         f"{self.calm_cycles}/{need}")
            else:
                if self.calm_cycles:
                    log.info(f"   🧭 CRASH: votes back to {votes} — calm "
                             f"counter reset")
                self.calm_cycles = 0
            self._save()
            return "crash"

        if self.state == "recovery":
            if not self.conf.get("recovery_enabled", True):
                self._enter("normal", "RECOVERY disabled in config")
                return rep.regime
            ok, why = self._recovery_ok(vix, term, above_ema20)
            if ok:
                self._last_block = None
                if today not in self.confirm_days:
                    self.confirm_days.append(today)
                    log.info(f"   🧭 RECOVERY: clean session {today} "
                             f"({len(self.confirm_days)}/"
                             f"{self.conf['recovery_confirm_days']})")
                    self._save()
                if len(self.confirm_days) >= self.conf["recovery_confirm_days"]:
                    self._enter("normal",
                                f"{len(self.confirm_days)} clean sessions — "
                                f"resuming normal trading")
                    return rep.regime
            else:
                if self.confirm_days:
                    log.info(f"   🧭 RECOVERY: {why} — confirmation reset "
                             f"(was {len(self.confirm_days)} day(s))")
                    self.confirm_days = []
                    self._save()
                elif why != getattr(self, "_last_block", None):
                    log.info(f"   🧭 RECOVERY: not clean yet — {why}")
                self._last_block = why
            return "recovery"

        return rep.regime

    def _recovery_ok(self, vix, term, above_ema20) -> tuple[bool, str]:
        c = self.conf
        if vix > c["recovery_max_vix"]:
            return False, f"VIX {vix:.1f} still above {c['recovery_max_vix']}"
        if term > c["recovery_max_term"]:
            return False, (f"term structure {term:.3f} not back in contango "
                           f"(< {c['recovery_max_term']})")
        if c["recovery_require_uptrend"] and not above_ema20:
            return False, "SPY still below its 20-day EMA"
        return True, "calm"

    # ── how the effective regime changes the rules ─────────────────────────

    def apply(self, effective: str, overrides: dict) -> dict:
        """RECOVERY stands the agent down without pretending it is a crash."""
        if effective != "recovery":
            return overrides
        ov = dict(overrides)
        ov["allow_calls"] = False
        ov["allow_puts"] = False
        ov["size_mult"] = 0.0
        ov["crash_mode"] = False
        need = self.conf["recovery_confirm_days"]
        ov["reason"] = (f"RECOVERY — {len(self.confirm_days)}/{need} clean "
                        f"sessions since the crash; not buying either side yet")
        return ov

    def describe(self) -> str:
        if self.state == "normal":
            return "NORMAL"
        if self.state == "crash":
            return (f"CRASH since {self.entered} "
                    f"(calm {self.calm_cycles}/{self.conf['crash_exit_cycles']})")
        return (f"RECOVERY since {self.entered} "
                f"({len(self.confirm_days)}/{self.conf['recovery_confirm_days']} "
                f"clean sessions)")


__all__ = ["RegimeState", "DEFAULTS", "STATE_FILE"]
