"""
spreads.py — Vertical debit spread construction and Tradier multileg orders
===========================================================================

Why spreads instead of naked long puts in a selloff
---------------------------------------------------
Buying a single put after VIX has already spiked means paying peak implied
volatility for the privilege of being right. The position is long vega. One
green day, one Fed headline, one "the worst is priced in" session, and IV
collapses — the underlying can keep drifting your way while the option
loses money. This is the most common way a directionally-correct crash bet
still loses.

A put debit spread (buy the higher strike put, sell a lower strike put) is
short the second leg's volatility. Net vega is small, net theta is small,
and the cost is a fraction of the naked put. What you give up is the tail:
profit is capped at the strike width. In exchange, being right pays reliably
instead of occasionally.

  Bear put spread   — buy K1 put, sell K2 put, K1 > K2   (profits down)
  Bull call spread  — buy K1 call, sell K2 call, K1 < K2 (profits up)

  Max loss   = net debit
  Max profit = (strike width - net debit) x 100 x contracts
  Breakeven  = K1 - debit  (puts)  /  K1 + debit  (calls)

Broker note
-----------
Tradier requires **options approval level 3** for spreads. Level 2 (long
calls/puts only) will reject a multileg order at the API. If a multileg
order comes back rejected on entry, the agent falls back to a single-leg
trade rather than leaving you with a naked short leg. That fallback is the
single most important safety property in this file — an unfilled long leg
paired with a filled short leg is an unlimited-risk position.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("OptionsAgent")

DEFAULTS: dict[str, Any] = {
    "long_delta_target":   0.50,   # long leg near the money
    "short_delta_target":  0.28,   # short leg funds the trade
    "min_width_pct":       1.0,    # strike width as % of spot, floor
    "max_width_pct":       6.0,    # ...and ceiling
    "min_reward_risk":     1.00,   # (width - debit) / debit
    "max_leg_spread_pct":  25.0,   # per-leg bid/ask width tolerance
    "min_open_interest":   50,     # both legs must be tradeable
    "min_debit":           0.10,
}


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _delta(opt: dict) -> float:
    g = opt.get("greeks") or {}
    return abs(_f(g.get("delta"), 0.0))


def _leg_ok(opt: dict, cfg: dict) -> bool:
    bid, ask = _f(opt.get("bid")), _f(opt.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid <= 0 or (ask - bid) / mid * 100 > cfg["max_leg_spread_pct"]:
        return False
    oi = _f(opt.get("open_interest"), 0)
    vol = _f(opt.get("volume"), 0)
    # Open interest OR live volume — a fresh 0DTE strike can have zero OI but
    # heavy volume, and rejecting those throws away the best crash liquidity.
    if oi < cfg["min_open_interest"] and vol < cfg["min_open_interest"]:
        return False
    return True


def build_debit_spread(options: list[dict], spot: float, direction: str,
                       budget: float, atr: float = 0.0,
                       cfg: dict | None = None) -> dict | None:
    """Construct the best vertical debit spread from a Tradier option chain.

    Parameters
    ----------
    options   : raw Tradier chain (list of option dicts, both types mixed)
    spot      : underlying price
    direction : "PUT" (bear put spread) or "CALL" (bull call spread)
    budget    : maximum total debit in dollars for the whole position
    atr       : underlying ATR, used to size the strike width sensibly
    """
    conf = {**DEFAULTS, **(cfg or {})}
    if not options or spot <= 0:
        return None

    want = "put" if direction == "PUT" else "call"
    legs = [o for o in options
            if (o.get("option_type") or "").lower() == want and _leg_ok(o, conf)]
    if len(legs) < 2:
        log.info(f"  ⬜ Spread: only {len(legs)} tradeable {want} legs — falling back")
        return None

    legs.sort(key=lambda o: _f(o.get("strike")))

    # ── target strike width ────────────────────────────────────────────────
    # One ATR is the natural width: it is roughly how far the underlying moves
    # in the option's lifetime, so the short strike sits near the realistic
    # edge of the move rather than at an arbitrary percentage.
    width_floor = spot * conf["min_width_pct"] / 100
    width_cap = spot * conf["max_width_pct"] / 100
    target_width = min(max(atr if atr > 0 else width_floor, width_floor), width_cap)

    # ── long leg: closest to the delta target, else closest to the money ────
    have_greeks = any(_delta(o) > 0.01 for o in legs)
    if have_greeks:
        long_leg = min(legs, key=lambda o: abs(_delta(o) - conf["long_delta_target"]))
    else:
        long_leg = min(legs, key=lambda o: abs(_f(o.get("strike")) - spot))

    long_strike = _f(long_leg.get("strike"))

    # ── short leg: OTM relative to the long leg, ~target_width away ─────────
    if direction == "PUT":
        candidates = [o for o in legs if _f(o.get("strike")) < long_strike]
    else:
        candidates = [o for o in legs if _f(o.get("strike")) > long_strike]
    if not candidates:
        log.info("  ⬜ Spread: no strike available for the short leg — falling back")
        return None

    ideal = (long_strike - target_width if direction == "PUT"
             else long_strike + target_width)

    # If greeks are available, require the short leg to be meaningfully OTM but
    # not worthless. A 0.02-delta short leg collects almost no premium — you
    # have effectively bought a naked put at a small discount and reintroduced
    # all the vega risk the spread was meant to remove.
    if have_greeks:
        lo, hi = 0.10, conf["short_delta_target"] + 0.15
        funded = [o for o in candidates if lo <= _delta(o) <= hi]
        if funded:
            candidates = funded

    candidates.sort(key=lambda o: abs(_f(o.get("strike")) - ideal))

    # Take the candidate CLOSEST TO THE ATR-IMPLIED WIDTH that passes every
    # filter — deliberately not the highest reward:risk. Maximising R:R always
    # picks the widest, most out-of-the-money short leg, which walks the
    # position straight back toward a naked put. ATR is the principled width:
    # it is roughly how far the underlying actually moves in the option's life.
    best: dict | None = None
    for short_leg in candidates[:8]:
        short_strike = _f(short_leg.get("strike"))
        width = abs(long_strike - short_strike)
        if width <= 0:
            continue

        # Pay the ask on what we buy, receive the bid on what we sell. Never
        # model a spread at mid — mid is not a price you can get filled at.
        debit = _f(long_leg.get("ask")) - _f(short_leg.get("bid"))
        if debit < conf["min_debit"] or debit >= width:
            continue

        reward_risk = (width - debit) / debit
        if reward_risk < conf["min_reward_risk"]:
            continue

        contracts = int(budget / (debit * 100))
        if contracts < 1:
            continue

        cand = {
            "structure":      "vertical_debit",
            "direction":      direction,
            "long_symbol":    long_leg.get("symbol"),
            "short_symbol":   short_leg.get("symbol"),
            "long_strike":    long_strike,
            "short_strike":   short_strike,
            "width":          round(width, 2),
            "debit":          round(debit, 2),
            "contracts":      contracts,
            "total_cost":     round(contracts * debit * 100, 2),
            "max_profit":     round(contracts * (width - debit) * 100, 2),
            "max_loss":       round(contracts * debit * 100, 2),
            "reward_risk":    round(reward_risk, 2),
            "breakeven":      round(long_strike - debit if direction == "PUT"
                                    else long_strike + debit, 2),
            "expiry":         long_leg.get("expiration_date"),
            "long_delta":     round(_delta(long_leg), 3),
            "short_delta":    round(_delta(short_leg), 3),
        }
        best = cand
        break  # candidates are sorted by distance from the ideal width

    if best is None:
        log.info("  ⬜ Spread: no candidate met reward:risk / budget — falling back")
        return None

    log.info(
        f"  🧱 Spread built: {direction} {best['long_strike']}/{best['short_strike']} "
        f"x{best['contracts']} | debit ${best['debit']:.2f} "
        f"| max profit ${best['max_profit']:.0f} / max loss ${best['max_loss']:.0f} "
        f"| R:R {best['reward_risk']:.2f} | BE {best['breakeven']}"
    )
    return best


# ── Tradier multileg payloads ──────────────────────────────────────────────────

def open_spread_payload(underlying: str, spread: dict,
                        limit_price: float | None = None) -> dict:
    """Tradier ``class=multileg`` payload that OPENS the spread as a debit.

    Tradier does not accept ``type=market`` for multileg; use debit/credit/even
    with a limit price. We pad the modelled debit by a few cents so a fast tape
    does not leave the order sitting unfilled.
    """
    price = limit_price if limit_price is not None else spread["debit"] * 1.05
    qty = str(spread["contracts"])
    return {
        "class": "multileg",
        "symbol": underlying,
        "type": "debit",
        "duration": "day",
        "price": f"{max(0.01, round(price, 2)):.2f}",
        "option_symbol[0]": spread["long_symbol"],
        "side[0]": "buy_to_open",
        "quantity[0]": qty,
        "option_symbol[1]": spread["short_symbol"],
        "side[1]": "sell_to_open",
        "quantity[1]": qty,
    }


def close_spread_payload(underlying: str, spread: dict,
                         limit_price: float, contracts: int | None = None) -> dict:
    """Tradier payload that CLOSES the spread, collecting a net credit."""
    qty = str(contracts if contracts is not None else spread["contracts"])
    return {
        "class": "multileg",
        "symbol": underlying,
        "type": "credit",
        "duration": "day",
        "price": f"{max(0.01, round(limit_price, 2)):.2f}",
        "option_symbol[0]": spread["long_symbol"],
        "side[0]": "sell_to_close",
        "quantity[0]": qty,
        "option_symbol[1]": spread["short_symbol"],
        "side[1]": "buy_to_close",
        "quantity[1]": qty,
    }


def spread_mark(long_quote: dict, short_quote: dict) -> dict:
    """Current liquidation value of an open spread.

    Exiting means selling the long leg (hit the bid) and buying back the short
    leg (lift the ask). That is the conservative mark and the only one worth
    triggering a stop on.
    """
    long_bid = _f(long_quote.get("bid"))
    short_ask = _f(short_quote.get("ask"))
    exit_value = long_bid - short_ask

    long_ask = _f(long_quote.get("ask"))
    short_bid = _f(short_quote.get("bid"))
    mid = ((long_bid + long_ask) / 2) - ((short_bid + short_ask) / 2)

    return {
        "exit_value": round(exit_value, 4),   # what you would actually receive
        "mid_value":  round(mid, 4),          # what it is theoretically worth
        "long_bid":   long_bid,
        "short_ask":  short_ask,
        "valid":      long_bid > 0 and short_ask >= 0,
    }


def spread_pnl_pct(entry_debit: float, mark: dict) -> float:
    """P&L as a percentage of the debit paid (i.e. of maximum loss)."""
    if entry_debit <= 0:
        return 0.0
    return (mark["exit_value"] - entry_debit) / entry_debit * 100


__all__ = [
    "build_debit_spread", "open_spread_payload", "close_spread_payload",
    "spread_mark", "spread_pnl_pct", "DEFAULTS",
]
