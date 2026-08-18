"""
shadow.py — measure the signals the agent REJECTS

The blind spot
--------------
Every dataset we have describes trades that *passed* every gate. 229 of them.
So we can measure the score, and we did — a weak positive gradient across
quartiles, nothing significant on its own.

What we cannot measure is the gates themselves. 3/3 confluence rejects most
tickers on most cycles; Gate 2 (squeeze or unusual options flow) rejects most
of what survives. Those are the strictest filters in the system and their value
is completely unknown, because a rejected signal leaves no trace and produces
no outcome. It could be discarding the best setups and we would never know.

What this does
--------------
Records every rejected signal with the rejection stage and the underlying's
price at that moment, then comes back later and measures what the underlying
actually did.

Forward return of the *underlying* is deliberately the metric, not simulated
option P&L. Option P&L folds in strike selection, implied volatility and theta
— three sources of noise that have nothing to do with whether the signal read
direction correctly. The question here is narrow: **when the agent said "this
looks like a CALL setup" and then rejected it, did the stock go up?**

Compare that hit rate against accepted signals and each gate's value becomes
measurable rather than assumed.

Cost: one JSONL append per rejected signal, and one batch of bar fetches on
resolution. No orders, no broker calls, no effect on trading.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("OptionsAgent")

PENDING = "shadow_pending.jsonl"
RESOLVED = "shadow_resolved.jsonl"

# How far forward to measure. The agent's own time exit is 90 minutes, so
# these bracket the horizon it actually trades on.
HORIZONS_MIN = (30, 60, 90)


def _path(name: str) -> Path:
    return Path(__file__).parent / name


class ShadowLogger:
    """Append-only record of rejected signals, resolved later against bars."""

    def __init__(self, enabled: bool = True, dedupe_minutes: float = 30.0):
        self.enabled = enabled
        # v5.8: the same signal recurs every cycle until the setup changes, so
        # one NVDA CALL got logged ~10 times in half an hour. That inflates n
        # while adding no information — the records are the same observation,
        # and treating them as independent makes a noisy result look
        # statistically solid. Suppress repeats of the same
        # (ticker, stage, direction) within this window.
        self.dedupe_minutes = dedupe_minutes
        self._last_seen: dict[tuple, datetime] = {}

    # ── recording ──────────────────────────────────────────────────────────

    def record(self, entries: list[dict], regime: str = "", vix: float = 0.0) -> None:
        """Append rejected-signal traces. Never raises — this is telemetry."""
        if not self.enabled or not entries:
            return
        now = datetime.now(timezone.utc)
        try:
            with open(_path(PENDING), "a") as fh:
                for e in entries:
                    if not e.get("spot"):
                        continue          # unusable without a reference price

                    key = (e.get("ticker"), e.get("stage"), e.get("direction"))
                    last = self._last_seen.get(key)
                    if last and (now - last).total_seconds() < self.dedupe_minutes * 60:
                        continue          # same observation, already counted
                    self._last_seen[key] = now

                    fh.write(json.dumps({
                        "ts": now.isoformat(),
                        "ticker": e.get("ticker"),
                        "stage": e.get("stage"),        # where it was rejected
                        "implied_direction": e.get("direction"),
                        "score": e.get("score"),
                        "call_tfs": e.get("call_tfs"),
                        "put_tfs": e.get("put_tfs"),
                        "spot": e.get("spot"),
                        "regime": regime,
                        "vix": vix,
                        "accepted": bool(e.get("accepted", False)),
                    }) + "\n")
        except Exception as ex:
            log.debug(f"shadow record failed (non-fatal): {ex}")

    # ── resolution ─────────────────────────────────────────────────────────

    async def resolve(self, md) -> int:
        """Measure forward returns for records old enough to judge.

        Reads pending, resolves what is ripe, rewrites the remainder. Bars come
        from the same data layer the agent already uses, so this adds a handful
        of cached requests rather than new load.
        """
        if not self.enabled or not _path(PENDING).exists():
            return 0

        try:
            pending = [json.loads(l) for l in open(_path(PENDING)) if l.strip()]
        except Exception as e:
            log.warning(f"shadow: cannot read pending ({e})")
            return 0
        if not pending:
            return 0

        now = datetime.now(timezone.utc)
        ripe_cut = timedelta(minutes=max(HORIZONS_MIN))
        ripe = [p for p in pending
                if (now - datetime.fromisoformat(p["ts"])) >= ripe_cut]
        still = [p for p in pending
                 if (now - datetime.fromisoformat(p["ts"])) < ripe_cut]
        if not ripe:
            return 0

        bars: dict[str, object] = {}
        for t in {p["ticker"] for p in ripe if p.get("ticker")}:
            try:
                bars[t] = await md.get_bars(t, "5m", "5d")
            except Exception:
                pass

        # v5.9: the benchmark. Without it "signed return" is mostly beta.
        # Observed on day 3: EVERY rejection stage showed >50% hit rate at the
        # 90-minute horizon — 59%, 61%, 76%, 64%. Gates cannot all be good at
        # once. The market simply drifted up, and since BULL regime blocks
        # PUTs, nearly every signal is CALL-implied, so the drift flatters all
        # of them equally. Excess return over SPY isolates whatever the signal
        # knew that the market did not.
        try:
            spy_bars = await md.get_bars("SPY", "5m", "5d")
        except Exception:
            spy_bars = None

        resolved = 0
        try:
            with open(_path(RESOLVED), "a") as fh:
                for p in ripe:
                    out = self._measure(p, bars.get(p["ticker"]), spy_bars)
                    if out:
                        fh.write(json.dumps(out) + "\n")
                        resolved += 1
        except Exception as e:
            log.warning(f"shadow: cannot write resolved ({e})")
            return 0

        try:
            with open(_path(PENDING), "w") as fh:
                for p in still:
                    fh.write(json.dumps(p) + "\n")
        except Exception:
            pass

        if resolved:
            log.info(f"👁️  Shadow: resolved {resolved} rejected signals "
                     f"({len(still)} still pending)")
        return resolved

    @staticmethod
    def _bench_return(spy_df, t0, minutes) -> float | None:
        """SPY's raw % move over the same window, or None if unavailable."""
        if spy_df is None or getattr(spy_df, "empty", True):
            return None
        try:
            idx = spy_df.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            at = spy_df[idx >= t0]
            after = spy_df[idx >= t0 + timedelta(minutes=minutes)]
            if at.empty or after.empty:
                return None
            a = float(at["Close"].iloc[0])
            b = float(after["Close"].iloc[0])
            return (b - a) / a * 100 if a else None
        except Exception:
            return None

    @staticmethod
    def _measure(rec: dict, df, spy_df=None) -> dict | None:
        """Forward return at each horizon, signed by the signal, plus excess."""
        if df is None or getattr(df, "empty", True):
            return None
        try:
            import pandas as pd
            t0 = datetime.fromisoformat(rec["ts"])
            idx = df.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            spot = float(rec["spot"])
            if spot <= 0:
                return None

            out = {k: rec[k] for k in
                   ("ts", "ticker", "stage", "implied_direction", "score",
                    "regime", "vix", "accepted", "spot")}
            got = False
            for h in HORIZONS_MIN:
                target = t0 + timedelta(minutes=h)
                after = df[idx >= target]
                if after.empty:
                    continue
                px = float(after["Close"].iloc[0])
                raw = (px - spot) / spot * 100
                # Sign it by what the signal claimed: positive means the
                # underlying moved the way the signal pointed.
                signed = raw if rec.get("implied_direction") == "CALL" else -raw
                out[f"ret_{h}m"] = round(raw, 4)
                out[f"signed_{h}m"] = round(signed, 4)

                # Excess over SPY, signed the same way. For a SPY signal this
                # is ~0 by construction, which is correct — a SPY CALL that
                # rode the index up predicted nothing the index did not.
                bench = ShadowLogger._bench_return(spy_df, t0, h)
                if bench is not None:
                    ex = raw - bench
                    out[f"excess_{h}m"] = round(
                        ex if rec.get("implied_direction") == "CALL" else -ex, 4)
                got = True
            return out if got else None
        except Exception:
            return None


# ── reporting ─────────────────────────────────────────────────────────────────

def report() -> None:
    """Compare rejected signals against accepted ones, by rejection stage."""
    path = _path(RESOLVED)
    if not path.exists():
        print("No resolved shadow records yet. Let it run a few sessions.")
        return
    rows = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if len(rows) < 30:
        print(f"Only {len(rows)} resolved records — too few to read anything "
              f"into. Come back after a few hundred.")
        return

    days = {r["ts"][:10] for r in rows if r.get("ts")}
    if len(days) < 5:
        print(f"\n  ⚠️  Records span only {len(days)} trading day(s). Signals")
        print(f"     from one session are not independent — they share a")
        print(f"     regime, a direction bias and an intraday drift. Treat")
        print(f"     everything below as provisional until this reads 10+.")

    print("=" * 68)
    print(f"  SHADOW REPORT — {len(rows)} signals, accepted and rejected")
    print("=" * 68)
    print("  'signed' = underlying move in the signal's claimed direction.")
    print("  Positive mean + hit rate above 50% = the signal had directional")
    print("  information. At or below 50% = the gate rejecting it costs nothing.")

    use_excess = sum(1 for r in rows if any(k.startswith("excess_") for k in r))
    metric = "excess" if use_excess >= len(rows) * 0.5 else "signed"
    if metric == "excess":
        print("\n  Using EXCESS return over SPY — raw signed return mostly")
        print("  measures market drift, since BULL regime makes nearly every")
        print("  signal a CALL.")
    else:
        print(f"\n  ⚠️  Only {use_excess}/{len(rows)} records carry a benchmark;")
        print(f"     falling back to raw signed return, which is beta-heavy.")

    for h in HORIZONS_MIN:
        key = f"{metric}_{h}m"
        have = [r for r in rows if key in r]
        if len(have) < 20:
            continue
        print(f"\n  ── {h} minutes forward ({metric}) ──")
        if h == max(HORIZONS_MIN) and len(have) < len(rows) * 0.7:
            print(f"     ⚠️  only {len(have)}/{len(rows)} records reach this")
            print(f"        horizon — late-session signals cannot resolve, so")
            print(f"        this row is biased toward morning setups.")
        print(f"  {'stage':<22}{'n':>6}{'hit%':>8}{'mean':>9}{'median':>9}")
        print("  " + "-" * 54)
        groups: dict[str, list] = {}
        for r in have:
            k = "ACCEPTED (traded)" if r.get("accepted") else \
                f"rejected: {r.get('stage', '?')}"
            groups.setdefault(k, []).append(r[key])
        for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(v) < 10:
                continue
            hit = 100 * sum(1 for x in v if x > 0) / len(v)
            med = sorted(v)[len(v) // 2]
            # A binomial hit rate needs ~30 before it means much; below that
            # the number moves several points per observation.
            flag = "  ← n too small" if len(v) < 30 else ""
            print(f"  {k:<22}{len(v):>6}{hit:>7.0f}%{sum(v)/len(v):>+9.2f}"
                  f"{med:>+9.2f}{flag}")

    print("\n  Read it this way: if a rejection stage shows a hit rate near or")
    print("  above ACCEPTED, that gate is throwing away good signals. If it")
    print("  sits well below 50%, the gate is earning its keep.")


if __name__ == "__main__":
    report()
