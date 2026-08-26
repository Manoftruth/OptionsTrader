#!/usr/bin/env python3
"""
crash_rehearsal.py — what would actually happen if the market plunged today?

The problem with a crash strategy
---------------------------------
It is untestable by waiting. The regime that triggers it might arrive next
month or not this year, and when it does, the first time anyone finds out
whether the plumbing works is with real money in a fast market. Every other
part of this system got measured against 296 trades of history. The crash path
has never fired once.

So this rehearses it against LIVE chains: it forces the crash overrides on,
runs the real selection code, and prints what would be bought, for how much,
and what it would be worth across a range of market drops.

    ./venv/bin/python crash_rehearsal.py
    ./venv/bin/python crash_rehearsal.py --budget 400
    ./venv/bin/python crash_rehearsal.py --fallback   # test single-leg too

What it CANNOT tell you
-----------------------
Today's chains are priced at today's implied volatility. In an actual plunge
IV expands violently — the same strike costs materially more, and the deep-ITM
filter (extrinsic <= 40% of premium) gets harder to satisfy, not easier. So
"reachable today at VIX 15" is an OPTIMISTIC read on "reachable in a crash at
VIX 38". Treat a comfortable pass here as the minimum bar, not a guarantee.

The payoff columns are at-expiry intrinsic value. Before expiry a deep ITM
option tracks close to that because its extrinsic component is small — which
is the entire reason this structure was chosen over a near-the-money option.

Read-only. Quotes and chains only. Places no orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from config import CONFIG
from agent import TradierClient
import deep_itm as di

# Inverse ETFs are -3x their index on a DAILY basis. Over one or two sessions
# that holds closely enough; over weeks, path-dependent decay erodes it.
LEVERAGE = {"SQQQ": (-3.0, "QQQ"), "SPXS": (-3.0, "SPY"),
            "SDS": (-2.0, "SPY"), "SOXS": (-3.0, "SMH"),
            "SQQQ.": (-3.0, "QQQ")}

DROPS = [-2.0, -3.0, -5.0, -8.0, -12.0]


def quote(client, sym) -> float:
    q = client.get_quote(sym) or {}
    d = (q.get("quotes") or {}).get("quote") or {}
    return float(d.get("last") or d.get("close") or 0)


def nearest_expiry(client, sym, dte):
    exps = (client.get_options_expirations(sym) or {}) \
        .get("expirations", {}).get("date", [])
    if isinstance(exps, str):
        exps = [exps]
    today = datetime.now().date()
    for e in sorted(exps):
        d = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if d >= dte:
            return e, d
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=None,
                    help="override crash_max_trade_size")
    ap.add_argument("--dte", type=int, default=None)
    ap.add_argument("--fallback", action="store_true",
                    help="also evaluate the single-leg fallback on the index")
    a = ap.parse_args()

    client = TradierClient(CONFIG["tradier_token"], sandbox=CONFIG["sandbox"])
    budget = a.budget if a.budget is not None else float(
        CONFIG.get("crash_max_trade_size", 250.0))
    dte = a.dte if a.dte is not None else int(CONFIG.get("min_days_to_expiry", 0))
    unders = list(CONFIG.get("crash_underlyings", ["SQQQ", "SPXS"]))
    cfg = CONFIG.get("deep_itm_cfg")

    print("=" * 76)
    print("  CRASH REHEARSAL — what fires if the market plunges right now")
    print("=" * 76)
    print(f"  crash_structure          {CONFIG.get('crash_structure')}")
    print(f"  crash_max_trade_size     ${budget:.0f}   (final budget, not scaled)")
    print(f"  crash_underlyings        {unders}")
    print(f"  single-leg fallback      {CONFIG.get('crash_allow_single_leg_fallback')}")
    print(f"  crash_mode_enabled       {CONFIG.get('crash_mode_enabled')}")
    if not CONFIG.get("crash_mode_enabled"):
        print("\n  ⚠️  crash_mode_enabled is False — in a real plunge the agent")
        print("     would fall back to legacy rules and go FLAT above max_vix,")
        print("     not trade the downside at all.")

    any_reachable = False

    for etf in unders:
        spot = quote(client, etf)
        if spot <= 0:
            print(f"\n  {etf}: no quote.")
            continue
        exp, days = nearest_expiry(client, etf, dte)
        if not exp:
            print(f"\n  {etf}: no expiry at {dte}+ DTE.")
            continue

        chain = client.get_options_chain(etf, exp) or {}
        opts = (chain.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]

        print(f"\n  {'─'*72}")
        print(f"  {etf}  spot ${spot:.2f}   expiry {exp} ({days} DTE)   "
              f"budget ${budget:.0f}")

        pick = di.build_deep_itm(opts, spot, "CALL", budget, cfg)
        if not pick:
            print(f"    ✗ NO deep ITM structure reachable at ${budget:.0f}.")
            print(f"      In a real crash the agent STANDS DOWN here"
                  f"{' (or falls through to a single leg)' if CONFIG.get('crash_allow_single_leg_fallback') else ''}.")
            cheapest = None
            for o in opts:
                if (o.get("option_type") or "").lower() != "call":
                    continue
                k, ask = float(o.get("strike", 0)), float(o.get("ask") or 0)
                d = abs(float((o.get("greeks") or {}).get("delta", 0) or 0))
                if ask > 0 and d >= float((cfg or di.DEFAULTS).get("min_delta", 0.60)):
                    if cheapest is None or ask < cheapest[1]:
                        cheapest = (k, ask, d)
            if cheapest:
                k, ask, d = cheapest
                print(f"      Cheapest contract meeting the delta floor: "
                      f"strike {k:.1f} @ ${ask:.2f} = ${ask*100:.0f}/contract "
                      f"(delta {d:.2f})")
                print(f"      → crash_max_trade_size would need to be "
                      f"≥ ${ask*100:.0f}")
            continue

        any_reachable = True
        n = max(1, int(budget // (pick["ask"] * 100)))
        cost = n * pick["ask"] * 100
        mult, index = LEVERAGE.get(etf, (-3.0, "the index"))

        print(f"    ✓ strike {pick['strike']:.1f} @ ${pick['ask']:.2f}  "
              f"× {n} = ${cost:.0f}")
        print(f"      delta {pick['delta']:.2f} | intrinsic ${pick['intrinsic']:.2f}"
              f" + extrinsic ${pick['extrinsic']:.2f} "
              f"({pick['extrinsic_pct']:.0f}% of premium)")
        print(f"      max loss ${cost:.0f} (it is a long option)")

        print(f"\n      {index} falls →  {etf} moves ({mult:+.0f}x daily)")
        print(f"      {'index':>8}{etf:>10}{'intrinsic':>12}{'position':>11}"
              f"{'P&L':>11}{'return':>9}")
        print("      " + "-" * 61)
        for drop in DROPS:
            etf_move = drop * mult / 100.0
            new_spot = spot * (1 + etf_move)
            intrinsic = max(0.0, new_spot - pick["strike"])
            value = intrinsic * 100 * n
            pnl = value - cost
            print(f"      {drop:>+7.1f}%{etf_move*100:>+9.1f}%"
                  f"{intrinsic:>12.2f}{value:>11.0f}{pnl:>+11.0f}"
                  f"{100*pnl/cost:>+8.0f}%")

        be_etf = (pick["strike"] + pick["ask"] - spot) / spot * 100
        print(f"\n      breakeven at expiry: {etf} {be_etf:+.1f}%  "
              f"→ {index} {be_etf/mult:+.1f}%")

    # ── the single-leg fallback, evaluated honestly ────────────────────────
    if a.fallback or CONFIG.get("crash_allow_single_leg_fallback"):
        print(f"\n  {'─'*72}")
        print(f"  SINGLE-LEG FALLBACK — a plain PUT on the index")
        print(f"  This is what fires when no deep ITM structure is reachable")
        print(f"  AND crash_allow_single_leg_fallback is True.")
        idx = "SPY"
        spot = quote(client, idx)
        exp, days = nearest_expiry(client, idx, dte)
        if spot > 0 and exp:
            chain = client.get_options_chain(idx, exp) or {}
            opts = (chain.get("options") or {}).get("option") or []
            if isinstance(opts, dict):
                opts = [opts]
            puts = [o for o in opts if (o.get("option_type") or "").lower() == "put"]
            ceiling = budget / 100.0
            min_px = float(CONFIG.get("min_contract_price", 0.20))
            ok = []
            for o in puts:
                ask = float(o.get("ask") or 0)
                d = abs(float((o.get("greeks") or {}).get("delta", 0) or 0))
                if min_px <= ask <= ceiling and 0.20 <= d <= 0.70:
                    ok.append((float(o.get("strike", 0)), ask, d))
            print(f"\n    {idx} ${spot:.2f}, expiry {exp} ({days} DTE)")
            print(f"    budget ${budget:.0f} → ceiling ${ceiling:.2f}/contract, "
                  f"delta 0.20-0.70, min ${min_px:.2f}")
            if not ok:
                print(f"    ✗ NOTHING QUALIFIES. {len(puts)} puts checked.")
                print(f"      The fallback would find no contract, so setting")
                print(f"      crash_allow_single_leg_fallback=True changes")
                print(f"      nothing here — it cannot rescue an unreachable")
                print(f"      crash trade at this budget.")
            else:
                ok.sort(key=lambda t: abs(t[2] - 0.45))
                k, ask, d = ok[0]
                print(f"    → would buy {idx} {k:.0f}P @ ${ask:.2f} (delta {d:.2f})")
                print(f"      ⚠️  Bought AFTER the vol spike. If IV mean-reverts")
                print(f"         while you hold it, vega works against you even")
                print(f"         when direction is right. That is the failure")
                print(f"         mode the deep-ITM structure exists to avoid.")
                print(f"      Note this is priced at TODAY's calm IV. In a real")
                print(f"      plunge the same strike costs materially more and")
                print(f"      may exceed the ${ceiling:.2f} ceiling entirely.")

    print("\n" + "=" * 76)
    if not any_reachable:
        print("  BOTTOM LINE: no downside structure is reachable at "
              f"${budget:.0f} today.")
        print("  In a plunge this agent would stand down. If the recession")
        print("  thesis is the point, that is the number to change.")
    else:
        print("  Deep ITM reachable today at calm IV. Re-run when VIX is")
        print("  elevated — that is the case that actually matters, and it is")
        print("  strictly harder than this one.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
