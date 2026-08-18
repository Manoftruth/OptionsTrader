"""
scoring.py — turn the signal score into position size instead of a cliff edge

The evidence
------------
229 trades mined from agent.log, joined to realised P&L:

    quartile   score        n   win%   median%    net $
      Q1     14.0-14.7     57    37%    -21.2%   -$208
      Q2     14.7-15.9     57    42%    -21.1%   +$526
      Q3     16.0-18.8     57    44%    -10.6%   +$296
      Q4     18.8-21.8     58    50%     +5.9%   +$913

Win rate, median return and net dollars all rise monotonically with score
across four independent bins. But no single test clears significance —
Pearson r=+0.078 (p=0.265), Spearman r=+0.061 (p=0.358), point-biserial
against win/lose r=+0.049 (p=0.461). The strongest comparison, Q4 vs Q1 win
rate (50% vs 37%), lands at p=0.106.

So the honest reading is: **the score carries a little information, far less
than a hard threshold implies, and more than zero.**

A cliff-edge threshold is the wrong instrument for a signal that weak. It
treats 15.8 and 16.0 as categorically different when the data cannot tell them
apart, and it treats 16.0 and 21.8 as identical when those are the two ends of
the only gradient we can see. Sizing expresses the uncertainty properly:
lean into the top of the distribution, take small positions in the middle,
and skip the bottom.

Q1 is the one clear call. It is the only quartile that loses money — 37% win
rate, median -21.2%, net -$208 — so it keeps a hard floor rather than a small
size.

Caveats worth keeping in view
-----------------------------
* 229 trades is not a lot, and the gradient is not significant on its own.
  Four aligned metrics is suggestive, not proof.
* The breakpoints are quartiles of THIS sample. They will drift. Re-run
  mine_log.py periodically and recheck.
* This measures only signals that passed every gate. Whether the gates
  themselves help is unmeasured — that is what shadow.py exists to answer.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("OptionsAgent")

# (score_below, size_multiplier) — first matching row wins, walking upward.
# Derived from the quartile table above. 0.0 means do not trade.
DEFAULT_CURVE: list[list[float]] = [
    [14.7,  0.00],   # Q1 — the only quartile that lost money
    [15.9,  0.40],   # Q2 — positive net, but median still -21%
    [18.8,  0.70],   # Q3
    [999.0, 1.00],   # Q4 — 50% win rate, median +5.9%, and the +913% outlier
]


def size_for_score(score: float, curve: list | None = None) -> float:
    """Position-size multiplier for a signal score. 0.0 means skip."""
    rows = curve or DEFAULT_CURVE
    try:
        for threshold, mult in rows:
            if score < float(threshold):
                return max(0.0, min(1.0, float(mult)))
    except (TypeError, ValueError) as e:
        log.warning(f"scoring: malformed score_size_curve ({e}) — using 1.0x")
        return 1.0
    return 1.0


def explain(score: float, curve: list | None = None) -> str:
    rows = curve or DEFAULT_CURVE
    mult = size_for_score(score, rows)
    band = next((f"<{t}" for t, _ in rows if score < float(t)), "top")
    if mult <= 0:
        return (f"score {score:.1f} in band {band} → SKIP "
                f"(this band was net negative across 229 trades)")
    return f"score {score:.1f} in band {band} → {mult:.2f}x size"


def floor_score(curve: list | None = None) -> float:
    """Lowest score that gets a non-zero size — the effective threshold.

    The curve is (score_below, multiplier) rows read upward, so a row of
    [14.7, 0.00] means "below 14.7, do not trade". The floor is therefore the
    LARGEST threshold among the zero-size rows, not the smallest threshold
    with a non-zero size.
    """
    rows = curve or DEFAULT_CURVE
    try:
        zeros = [float(t) for t, m in rows if float(m) <= 0]
        return max(zeros) if zeros else 0.0
    except (TypeError, ValueError):
        return 0.0


__all__ = ["size_for_score", "explain", "floor_score", "DEFAULT_CURVE"]
