"""
crash_mode.py — Regime detection and downside rule overrides
=============================================================

The problem this solves
-----------------------
The agent as originally written *switches itself off* in exactly the market
you want it awake for:

  * ``run_once`` returns early when VIX > max_vix (28)
  * ``_vix_size_multiplier`` returns 0.0x when VIX >= 30
  * ``score_ticker`` rejects any ticker whose ATR is > 5% of price
  * ``max_contract_price`` of $3.00 excludes most puts once IV expands
  * regime only flips bearish on a 20/50 EMA daily cross, which lags a fast
    selloff by one to three weeks — and BULL regime *hard blocks PUTs*

In a genuine drawdown all five fire at once and the agent sits in cash.

What this module does
---------------------
Classifies the tape into BULL / NEUTRAL / BEAR / CRASH using signals that
move faster than a 20/50 daily cross, then hands back an explicit set of
rule overrides for that regime. Nothing here places trades or mutates the
agent — it returns data, the agent decides.

Detection inputs (all free, all from the existing data layer):

  1. **Price structure** — SPY 5/20 EMA (fast), 20/50 EMA (the old slow
     check), 200 SMA, and drawdown from the rolling 20-day high.
  2. **VIX term structure** — ``VIX / VIX3M``. When spot vol trades above
     three-month vol (backwardation, ratio > 1.0) the market is paying up
     for immediate protection. This is the single best "this is a real
     event, not a dip" signal available without paid data, and it flips days
     before a moving-average cross does.
  3. **Credit** — ``HYG / IEF`` ratio. High-yield underperforming Treasuries
     is what an actual recession looks like in market data, as opposed to an
     equity-only growth scare.
  4. **Curve** — 10y (``^TNX``) vs 13w (``^IRX``). Reported as macro context
     only; it never gates a trade. An inverted curve has led recessions by
     6-24 months, which is useless for sizing a weekly option.

A word on the trade you get out of this
---------------------------------------
Once VIX is already elevated, buying naked long puts means buying peak
implied volatility. You can be exactly right on direction and still lose,
because a single stabilising day crushes vega faster than delta earns. That
is why ``prefer_spreads`` flips on above ``spread_iv_threshold`` — a put
debit spread is short the expensive leg, so it cares far less about the vol
crush. It caps the upside too. That is the trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger("OptionsAgent")

# Tickers the detector needs. Fetched concurrently in one batch.
REGIME_TICKERS = ["SPY", "^VIX", "^VIX3M", "HYG", "IEF", "QQQ", "IWM"]

# v5.4: the yield curve is OFF by default.
#
# It was only ever macro context — it never casts a crash vote, because an
# inverted curve leads recessions by 6-24 months and has no timing value for a
# weekly option. In production both symbols were also returning nothing from
# Yahoo (which is what produced the nonsensical "-757.15pp" reading), while
# still costing ~9s each under throttling: ~18 seconds per cycle for a log line
# that cannot influence a trade. Enable with cfg["fetch_yield_curve"]=True.
CURVE_TICKERS = ["^TNX", "^IRX"]

DEFAULTS: dict[str, Any] = {
    # crash classification
    "crash_vix":              28.0,   # spot VIX above this counts as one vote
    "crash_backwardation":    1.00,   # VIX/VIX3M above this counts as one vote
    "crash_drawdown_pct":     5.0,    # SPY % below its 20d high counts as one vote
    "crash_votes_required":   2,      # how many votes to declare CRASH
    # fast bear flip
    "bear_drawdown_pct":      3.0,
    # rule overrides while in crash
    "crash_max_vix":          70.0,   # effectively "don't self-disable"
    "crash_max_atr_pct":      12.0,
    "crash_max_contract_price": 12.00,
    "crash_size_mult":        0.40,   # deliberately small — vol is the risk
    "crash_min_score_delta":  1.0,    # demand a *higher* score, not lower
    # data integrity bounds (v5.2)
    "vix_floor":              5.0,
    "vix_ceiling":            150.0,
    "spy_floor":              50.0,
    # macro context only — never gates a trade. See CURVE_TICKERS.
    "fetch_yield_curve":      False,
    # spreads
    "spread_iv_threshold":    22.0,   # VIX above this → prefer debit spreads
}


@dataclass
class RegimeReport:
    regime: str = "neutral"              # bull | neutral | bear | crash
    prior_regime: str = "neutral"        # what the old 20/50 EMA logic said
    vix: float = 20.0
    vix3m: float = 20.0
    term_structure: float = 1.0          # VIX / VIX3M — >1.0 is backwardation
    spy_price: float = 0.0
    spy_drawdown_20d: float = 0.0        # % below rolling 20d high (positive number)
    spy_drawdown_52w: float = 0.0
    above_200d: bool = True
    ema_fast_cross_down: bool = False    # 5 EMA < 20 EMA
    credit_ratio_chg_20d: float = 0.0    # HYG/IEF 20d % change
    credit_stress: bool = False
    curve_spread: float = 0.0            # 10y minus 13w, in percentage points
    curve_inverted: bool = False
    crash_votes: int = 0
    reasons: list[str] = field(default_factory=list)
    macro_notes: list[str] = field(default_factory=list)
    degraded: bool = False               # True if data was missing/stale
    integrity_ok: bool = True            # False = frames look cross-assigned/absurd
    integrity_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_crash(self) -> bool:
        return self.regime == "crash"

    @property
    def is_risk_off(self) -> bool:
        return self.regime in ("bear", "crash")


# ── helpers ────────────────────────────────────────────────────────────────────

def _series(df: pd.DataFrame, col: str = "Close") -> pd.Series | None:
    if df is None or df.empty or col not in df.columns:
        return None
    s = df[col]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    return s if len(s) else None


def _last(s: pd.Series | None, default: float = 0.0) -> float:
    try:
        return float(s.iloc[-1])
    except Exception:
        return default


def _ema(s: pd.Series, span: int) -> float:
    return float(s.ewm(span=span, adjust=False).mean().iloc[-1])


# ── data integrity ─────────────────────────────────────────────────────────────

def check_integrity(frames: dict[str, pd.DataFrame],
                    cfg: dict | None = None) -> list[str]:
    """Catch corrupted / cross-assigned market data before it votes.

    v5.2. Added after a live incident: the concurrent fetch returned one
    ticker's price series under another ticker's key, and the detector
    reported SPY at 14.79 (VIX's number) and VIX at 79.6, producing a 3-vote
    CRASH call on a tape with VIX at 14.8.

    async_data.py now verifies symbols at the source and serialises the
    non-thread-safe yfinance fallback, which fixes the cause. These checks are
    the second layer: whatever the cause, absurd inputs must not reach a
    sizing decision. Returns a list of problems — empty means clean.
    """
    conf = {**DEFAULTS, **(cfg or {})}
    problems: list[str] = []

    def last(t):
        s_ = _series(frames.get(t, pd.DataFrame()))
        return None if s_ is None else float(s_.iloc[-1])

    vix, vix3m, spy = last("^VIX"), last("^VIX3M"), last("SPY")

    # Absolute plausibility. VIX has never closed below 8 or above 90.
    if vix is not None and not (conf["vix_floor"] <= vix <= conf["vix_ceiling"]):
        problems.append(f"VIX {vix:.2f} outside plausible "
                        f"[{conf['vix_floor']}, {conf['vix_ceiling']}]")
    if vix3m is not None and not (conf["vix_floor"] <= vix3m <= conf["vix_ceiling"]):
        problems.append(f"VIX3M {vix3m:.2f} outside plausible range")
    if spy is not None and spy < conf["spy_floor"]:
        problems.append(f"SPY {spy:.2f} below plausible floor "
                        f"{conf['spy_floor']} — almost certainly another "
                        f"ticker's series")

    # Cross-assignment leaves two different tickers holding an identical
    # series. Genuine instruments do not print the same last five closes.
    tails = {}
    for t in ("SPY", "^VIX", "^VIX3M", "HYG", "IEF", "QQQ", "IWM"):
        s_ = _series(frames.get(t, pd.DataFrame()))
        if s_ is not None and len(s_) >= 5:
            tails[t] = tuple(round(float(x), 6) for x in s_.iloc[-5:])
    seen: dict[tuple, str] = {}
    for t, tail in tails.items():
        if tail in seen:
            problems.append(f"{t} and {seen[tail]} returned an IDENTICAL price "
                            f"series — frames are cross-assigned")
        else:
            seen[tail] = t

    return problems


# ── core assessment ────────────────────────────────────────────────────────────

def assess_from_frames(frames: dict[str, pd.DataFrame],
                       cfg: dict | None = None) -> RegimeReport:
    """Classify the regime from already-fetched daily frames.

    ``frames`` maps ticker → daily OHLCV DataFrame (1y of history is plenty).
    Pure function: no network, no globals. That makes it unit-testable, which
    matters when the output decides whether real money buys puts.
    """
    conf = {**DEFAULTS, **(cfg or {})}
    rep = RegimeReport()

    spy = _series(frames.get("SPY", pd.DataFrame()))
    if spy is None or len(spy) < 60:
        rep.degraded = True
        rep.reasons.append("⚠️ SPY history unavailable — defaulting to NEUTRAL")
        return rep

    rep.spy_price = _last(spy)
    ema5, ema20, ema50 = _ema(spy, 5), _ema(spy, 20), _ema(spy, 50)
    sma200 = float(spy.rolling(min(200, len(spy))).mean().iloc[-1])

    rep.above_200d = rep.spy_price > sma200
    rep.ema_fast_cross_down = ema5 < ema20

    high_20d = float(spy.rolling(20).max().iloc[-1])
    rep.spy_drawdown_20d = max(0.0, (high_20d - rep.spy_price) / high_20d * 100)
    high_52w = float(spy.rolling(min(252, len(spy))).max().iloc[-1])
    rep.spy_drawdown_52w = max(0.0, (high_52w - rep.spy_price) / high_52w * 100)

    # ── legacy 20/50 classification, kept so we can log the divergence ──
    if rep.spy_price > ema20 > ema50:
        rep.prior_regime = "bull"
    elif rep.spy_price < ema20 < ema50:
        rep.prior_regime = "bear"
    else:
        rep.prior_regime = "neutral"

    # ── volatility ──
    vix_s = _series(frames.get("^VIX", pd.DataFrame()))
    vix3m_s = _series(frames.get("^VIX3M", pd.DataFrame()))
    rep.vix = _last(vix_s, 20.0) or 20.0
    rep.vix3m = _last(vix3m_s, rep.vix) or rep.vix
    rep.term_structure = (rep.vix / rep.vix3m) if rep.vix3m > 0 else 1.0
    if vix_s is None:
        rep.degraded = True
        rep.macro_notes.append("VIX data missing — assuming 20.0")

    # ── credit ──
    hyg = _series(frames.get("HYG", pd.DataFrame()))
    ief = _series(frames.get("IEF", pd.DataFrame()))
    if hyg is not None and ief is not None and len(hyg) > 25 and len(ief) > 25:
        n = min(len(hyg), len(ief))
        ratio = (hyg.iloc[-n:].to_numpy() / ief.iloc[-n:].to_numpy())
        if len(ratio) > 21 and ratio[-21] > 0:
            rep.credit_ratio_chg_20d = float((ratio[-1] - ratio[-21]) / ratio[-21] * 100)
            rep.credit_stress = rep.credit_ratio_chg_20d < -1.5

    # ── curve (context only) ──
    tnx = _series(frames.get("^TNX", pd.DataFrame()))
    irx = _series(frames.get("^IRX", pd.DataFrame()))
    if tnx is not None and irx is not None:
        rep.curve_spread = round(_last(tnx) - _last(irx), 2)
        rep.curve_inverted = rep.curve_spread < 0

    # ── data integrity gate (v5.2) ──
    # Runs before any vote is cast. Corrupt inputs must never reach a sizing
    # decision, no matter how confident the rest of the pipeline looks.
    problems = check_integrity(frames, cfg)
    if problems:
        rep.integrity_ok = False
        rep.degraded = True
        rep.integrity_notes = problems
        rep.regime = "neutral"
        for p in problems:
            log.error(f"🚨 DATA INTEGRITY: {p}")
        log.error("🚨 Refusing to classify a regime from this data — "
                  "the cycle will be skipped.")
        return rep

    # ── crash votes ──
    votes = 0
    if rep.vix >= conf["crash_vix"]:
        votes += 1
        rep.reasons.append(f"VIX {rep.vix:.1f} ≥ {conf['crash_vix']}")
    if rep.term_structure >= conf["crash_backwardation"]:
        votes += 1
        rep.reasons.append(
            f"VIX term structure in BACKWARDATION ({rep.term_structure:.3f}) — "
            f"spot vol bid over 3-month")
    if rep.spy_drawdown_20d >= conf["crash_drawdown_pct"]:
        votes += 1
        rep.reasons.append(
            f"SPY {rep.spy_drawdown_20d:.1f}% below its 20-day high")
    if (not rep.above_200d) and rep.ema_fast_cross_down:
        votes += 1
        rep.reasons.append("SPY below 200d SMA with 5<20 EMA — trend broken")
    if rep.credit_stress:
        votes += 1
        rep.reasons.append(
            f"Credit stress: HYG/IEF {rep.credit_ratio_chg_20d:+.1f}% over 20d")
    rep.crash_votes = votes

    # ── classification ──
    if votes >= conf["crash_votes_required"]:
        rep.regime = "crash"
    elif rep.prior_regime == "bear":
        rep.regime = "bear"
    elif (rep.spy_drawdown_20d >= conf["bear_drawdown_pct"]
          and rep.ema_fast_cross_down):
        rep.regime = "bear"
        rep.reasons.append(
            f"Fast bear flip: {rep.spy_drawdown_20d:.1f}% off 20d high + 5<20 EMA "
            f"(slow 20/50 check still says {rep.prior_regime.upper()})")
    elif rep.prior_regime == "bull" and not rep.ema_fast_cross_down:
        rep.regime = "bull"
    else:
        rep.regime = "neutral"

    # ── macro context — never gates a trade, just tells you where you are ──
    if rep.curve_inverted:
        rep.macro_notes.append(
            f"Yield curve inverted ({rep.curve_spread:+.2f}pp, 10y-13w). "
            f"Historically leads recessions by 6-24 months — no timing value "
            f"for weekly options.")
    if not rep.above_200d:
        rep.macro_notes.append("SPY below its 200-day average.")
    if rep.credit_ratio_chg_20d < 0:
        rep.macro_notes.append(
            f"HYG/IEF {rep.credit_ratio_chg_20d:+.1f}% over 20d "
            f"({'credit confirming equity weakness' if rep.credit_stress else 'mild'}).")
    if rep.spy_drawdown_52w > 10:
        rep.macro_notes.append(
            f"SPY {rep.spy_drawdown_52w:.1f}% off its 52-week high — correction territory.")

    return rep


async def assess(md, cfg: dict | None = None) -> RegimeReport:
    """Fetch every regime input concurrently and classify.

    ``md`` is an open ``AsyncMarketData``.
    """
    conf = {**DEFAULTS, **(cfg or {})}
    tickers = list(REGIME_TICKERS)
    if conf.get("fetch_yield_curve", False):
        tickers += CURVE_TICKERS

    reqs = [(t, "1d", "1y") for t in tickers]
    frames_by_req = await md.get_many_bars(reqs)
    frames = {t: frames_by_req.get((t, "1d", "1y"), pd.DataFrame())
              for t in tickers}

    # Spot VIX from a 5m bar is fresher than the daily close mid-session.
    try:
        vix_intraday = await md.get_bars("^VIX", "5m", "1d")
        if not vix_intraday.empty:
            frames["^VIX"] = pd.concat([frames["^VIX"], vix_intraday.tail(1)])
    except Exception:
        pass

    return assess_from_frames(frames, cfg)


# ── rule overrides ─────────────────────────────────────────────────────────────

def overrides_for(rep: RegimeReport, base_config: dict) -> dict:
    """Translate a regime into concrete rule changes for this cycle.

    Returns a dict the agent merges over CONFIG for the duration of one cycle.
    Keys not present mean "leave the configured value alone".
    """
    conf = {**DEFAULTS, **base_config}
    ov: dict[str, Any] = {}

    # Corrupt data → trade nothing this cycle. Not "trade smaller": nothing.
    if not rep.integrity_ok:
        return {
            "halt": True,
            "allow_calls": False,
            "allow_puts": False,
            "size_mult": 0.0,
            "prefer_defined_risk": False,
            "prefer_spreads": False,
            "reason": "HALTED — market data failed integrity checks: "
                      + "; ".join(rep.integrity_notes),
        }

    # Crash mode is opt-in. Until it is armed the agent behaves exactly as it
    # did before — it detects and logs the crash, then declines to act on it.
    # This is deliberate: arming a new downside strategy should be a decision
    # you make on a calm day, not something that switches itself on the first
    # time VIX prints 30.
    if rep.regime == "crash" and not base_config.get("crash_mode_enabled", False):
        log.warning(
            "🔻 CRASH regime detected but crash_mode_enabled=False — "
            "trading with pre-v5 rules (the agent will go flat in high vol). "
            "Set crash_mode_enabled=True in config.py to act on this."
        )
        downgraded = RegimeReport(**{**rep.to_dict(), "regime": "bear"})
        ov = overrides_for(downgraded, {**base_config, "crash_mode_enabled": True})
        ov["max_vix"] = float(base_config.get("max_vix", 28))
        ov["max_atr_pct"] = 5.0
        ov["max_contract_price"] = float(base_config.get("max_contract_price", 3.0))
        ov["prefer_spreads"] = False
        ov["prefer_defined_risk"] = False
        ov["reason"] = "CRASH detected but crash mode NOT armed — legacy rules"
        return ov

    if rep.regime == "crash":
        # Stay awake. Trade small. Puts only. Defined risk.
        ov["max_vix"] = conf["crash_max_vix"]
        ov["max_atr_pct"] = conf["crash_max_atr_pct"]
        ov["max_contract_price"] = conf["crash_max_contract_price"]
        ov["allow_calls"] = False
        ov["allow_puts"] = True
        ov["size_mult"] = conf["crash_size_mult"]
        # "Defined risk" here means: a structure chosen so that a volatility
        # collapse cannot destroy the whole position. With options level 3
        # that is a debit spread; at level 2 it is a deep in-the-money option,
        # which is mostly intrinsic value. config["crash_structure"] picks.
        ov["prefer_defined_risk"] = True
        ov["prefer_spreads"] = True   # legacy key, kept for compatibility
        # Higher bar, not lower. Chaos produces more signals, not better ones.
        ov["min_signal_score"] = float(base_config.get("min_signal_score", 15.9)) \
            + conf["crash_min_score_delta"]
        ov["reason"] = "CRASH regime — puts only, debit spreads, reduced size"

    elif rep.regime == "bear":
        ov["allow_calls"] = False
        ov["allow_puts"] = True
        ov["max_atr_pct"] = 8.0
        ov["max_contract_price"] = max(
            float(base_config.get("max_contract_price", 3.0)), 6.0)
        ov["size_mult"] = 0.7
        ov["prefer_defined_risk"] = rep.vix >= conf["spread_iv_threshold"]
        ov["prefer_spreads"] = ov["prefer_defined_risk"]
        ov["reason"] = "BEAR regime — puts only"

    elif rep.regime == "bull":
        ov["allow_calls"] = True
        ov["allow_puts"] = False
        ov["size_mult"] = 1.0
        # Deep-ITM routing is a downside structure only; a bullish signal has
        # no inverse-ETF translation, so it takes the ordinary path.
        ov["prefer_defined_risk"] = False
        ov["prefer_spreads"] = False
        ov["reason"] = "BULL regime — calls only"

    else:  # neutral
        ov["allow_calls"] = True
        ov["allow_puts"] = True
        ov["size_mult"] = 0.85
        ov["prefer_defined_risk"] = rep.vix >= conf["spread_iv_threshold"]
        ov["prefer_spreads"] = ov["prefer_defined_risk"]
        ov["reason"] = "NEUTRAL regime — both directions, index only"

    # A degraded read means we could not see the market properly. Shrink.
    if rep.degraded:
        ov["size_mult"] = min(ov.get("size_mult", 1.0), 0.5)
        ov["reason"] = ov.get("reason", "") + " | DEGRADED DATA — size halved"

    return ov


def log_report(rep: RegimeReport) -> None:
    """One compact block in the log so you can reconstruct any decision later."""
    icon = {"crash": "🔻", "bear": "📉", "neutral": "➡️", "bull": "📈"}[rep.regime]
    log.info("═" * 60)
    log.info(f"{icon} REGIME: {rep.regime.upper()}"
             f"{'  (slow 20/50 check says ' + rep.prior_regime.upper() + ')' if rep.prior_regime != rep.regime else ''}")
    log.info(f"   VIX {rep.vix:.1f} | VIX3M {rep.vix3m:.1f} | "
             f"term {rep.term_structure:.3f}"
             f"{'  ⚠️ BACKWARDATION' if rep.term_structure >= 1.0 else ''}")
    log.info(f"   SPY {rep.spy_price:.2f} | -{rep.spy_drawdown_20d:.1f}% from 20d high "
             f"| -{rep.spy_drawdown_52w:.1f}% from 52w high "
             f"| {'above' if rep.above_200d else 'BELOW'} 200d")
    log.info(f"   Crash votes: {rep.crash_votes}")
    for r in rep.reasons:
        log.info(f"     • {r}")
    for m in rep.macro_notes:
        log.info(f"   📎 {m}")
    log.info("═" * 60)


__all__ = ["RegimeReport", "assess", "assess_from_frames", "overrides_for",
           "log_report", "REGIME_TICKERS", "DEFAULTS"]
