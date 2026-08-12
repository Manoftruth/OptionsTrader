"""
deep_itm.py — Level-2 downside structure: deep in-the-money options
====================================================================

The problem
-----------
A debit spread neutralises volatility risk by selling the expensive leg. That
needs Tradier options approval level 3. At level 2 you can only buy calls and
puts outright — so the question becomes: which single long option is least
damaged when implied volatility mean-reverts?

The answer is the one with the least extrinsic value.

An option's price splits into two parts:

    premium = intrinsic + extrinsic

Intrinsic is arithmetic — how far in the money the strike is. It cannot be
destroyed by a volatility collapse, only by the underlying moving against
you. Extrinsic is time value, and it is *entirely* made of implied volatility
and time to expiry. It is the part that evaporates.

An at-the-money option is ~100% extrinsic. Buy one after VIX has spiked and
you own a pile of pure volatility premium at its most expensive. One calm
session and it is gone, whether or not you were right about direction.

A 0.75-delta option might be 70% intrinsic. The same vol crush takes 30% of
the position instead of all of it. That is not as clean as a spread — you are
still net long vega — but it is the closest level-2 approximation, and it is
the difference between "right and paid" and "right and stopped out".

Why this module filters on extrinsic %, not delta
--------------------------------------------------
Delta is a proxy for moneyness; extrinsic ratio is the thing we actually care
about. Two contracts with identical 0.72 delta can carry very different time
value depending on days to expiry and the vol surface. Filtering on
``extrinsic / premium`` measures the exposure directly instead of inferring
it. Delta is kept as a secondary floor so we don't accidentally buy something
so deep it barely moves.

The cost
--------
Deep ITM contracts cost several times an out-of-the-money lottery ticket, so
the same budget buys far fewer of them. On a small account that means one
position instead of four. That concentration is the trade you are making in
exchange for the position surviving contact with a vol crush.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("OptionsAgent")

DEFAULTS: dict[str, Any] = {
    "target_delta":        0.75,   # what we aim for
    "min_delta":           0.60,   # below this it is not "deep" any more
    "max_delta":           0.92,   # above this you are paying for stock
    "max_extrinsic_pct":   40.0,   # THE gate: time value as % of premium
    "max_spread_pct":      18.0,   # bid/ask width tolerance
    "min_open_interest":   25,
    "min_intrinsic":       0.05,   # must actually be in the money
}


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _delta(opt: dict) -> float:
    return abs(_f((opt.get("greeks") or {}).get("delta"), 0.0))


def analyse(opt: dict, spot: float, direction: str) -> dict | None:
    """Break a contract into intrinsic / extrinsic and score its vol exposure."""
    strike = _f(opt.get("strike"))
    ask = _f(opt.get("ask"))
    bid = _f(opt.get("bid"))
    if strike <= 0 or ask <= 0 or bid <= 0 or spot <= 0:
        return None

    intrinsic = (spot - strike) if direction == "CALL" else (strike - spot)
    if intrinsic <= 0:
        return None                       # out of the money — not our business

    extrinsic = max(0.0, ask - intrinsic)
    mid = (bid + ask) / 2
    return {
        "symbol":        opt.get("symbol"),
        "strike":        strike,
        "ask":           ask,
        "bid":           bid,
        "intrinsic":     round(intrinsic, 3),
        "extrinsic":     round(extrinsic, 3),
        "extrinsic_pct": round(extrinsic / ask * 100, 1) if ask else 100.0,
        "delta":         _delta(opt),
        "spread_pct":    round((ask - bid) / mid * 100, 1) if mid else 100.0,
        "open_interest": _f(opt.get("open_interest")),
        "volume":        _f(opt.get("volume")),
        "expiry":        opt.get("expiration_date"),
    }


def build_deep_itm(options: list[dict], spot: float, direction: str,
                   budget: float, cfg: dict | None = None) -> dict | None:
    """Pick the deepest in-the-money contract that fits ``budget``.

    Returns None rather than silently degrading to an at-the-money contract —
    if the budget cannot reach a genuinely ITM strike, the caller should know
    that and decide, not discover it later in the P&L.
    """
    conf = {**DEFAULTS, **(cfg or {})}
    if not options or spot <= 0 or budget <= 0:
        return None

    want = direction.lower()
    candidates, rejected = [], []

    for opt in options:
        if (opt.get("option_type") or "").lower() != want:
            continue
        a = analyse(opt, spot, direction)
        if a is None:
            continue

        if a["intrinsic"] < conf["min_intrinsic"]:
            continue
        if a["ask"] * 100 > budget:
            rejected.append((a, f"costs ${a['ask'] * 100:.0f} > budget ${budget:.0f}"))
            continue
        if a["extrinsic_pct"] > conf["max_extrinsic_pct"]:
            rejected.append((a, f"extrinsic {a['extrinsic_pct']:.0f}% > "
                                f"{conf['max_extrinsic_pct']:.0f}% — too much vol premium"))
            continue
        if a["spread_pct"] > conf["max_spread_pct"]:
            rejected.append((a, f"bid/ask {a['spread_pct']:.0f}% too wide"))
            continue
        if a["open_interest"] < conf["min_open_interest"] and \
                a["volume"] < conf["min_open_interest"]:
            rejected.append((a, "no open interest or volume"))
            continue
        # Delta floor only when the broker actually supplied greeks.
        if a["delta"] > 0.01:
            if a["delta"] < conf["min_delta"]:
                rejected.append((a, f"delta {a['delta']:.2f} < {conf['min_delta']} "
                                    f"— not deep enough"))
                continue
            if a["delta"] > conf["max_delta"]:
                rejected.append((a, f"delta {a['delta']:.2f} > {conf['max_delta']} "
                                    f"— paying for stock"))
                continue
        candidates.append(a)

    if not candidates:
        log.info(f"  ⬜ Deep ITM: nothing qualified at a ${budget:.0f} budget")
        for a, why in rejected[:4]:
            log.info(f"       {a['strike']:.1f} ${a['ask']:.2f} "
                     f"(intrinsic ${a['intrinsic']:.2f} / extrinsic "
                     f"{a['extrinsic_pct']:.0f}%) — {why}")
        return None

    # Prefer the LOWEST extrinsic ratio — the least volatility premium paid.
    # Delta breaks ties toward the deeper contract.
    candidates.sort(key=lambda a: (a["extrinsic_pct"], -a["delta"]))
    best = candidates[0]

    contracts = max(1, int(budget / (best["ask"] * 100)))
    total = contracts * best["ask"] * 100

    breakeven = (best["strike"] + best["ask"]) if direction == "CALL" \
        else (best["strike"] - best["ask"])

    result = {
        "structure":     "deep_itm",
        "direction":     direction,
        "option_symbol": best["symbol"],
        "strike":        best["strike"],
        "expiry":        best["expiry"],
        "ask":           best["ask"],
        "bid":           best["bid"],
        "delta":         best["delta"],
        "intrinsic":     best["intrinsic"],
        "extrinsic":     best["extrinsic"],
        "extrinsic_pct": best["extrinsic_pct"],
        "contracts":     contracts,
        "total_cost":    round(total, 2),
        "max_loss":      round(total, 2),      # long option — debit is the risk
        "breakeven":     round(breakeven, 2),
        "vol_at_risk":   round(contracts * best["extrinsic"] * 100, 2),
    }

    log.info(
        f"  🧊 Deep ITM: {direction} {best['strike']:.1f} x{contracts} @ "
        f"${best['ask']:.2f} | delta {best['delta']:.2f} | "
        f"intrinsic ${best['intrinsic']:.2f} + extrinsic ${best['extrinsic']:.2f} "
        f"({best['extrinsic_pct']:.0f}%) | cost ${total:.0f} | "
        f"only ${result['vol_at_risk']:.0f} of that is vol premium | BE {result['breakeven']}"
    )
    return result


__all__ = ["build_deep_itm", "analyse", "DEFAULTS"]
