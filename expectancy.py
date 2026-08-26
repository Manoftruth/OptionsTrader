#!/usr/bin/env python3
"""
expectancy.py — does this system have positive expectancy, and could we tell?

The question nobody has asked yet
---------------------------------
Every analysis so far has asked "which component predicts profit". The answer
kept coming back "none, significantly". But the prior question was never put
directly: across all closed trades, does the average trade make or lose money,
and how confident can anyone be either way?

That matters more than any gate, because a system with negative expectancy
gets worse the more accurately it executes. Six versions of bug fixes this
week removed real defects — a stop loss that silently skipped on a zero bid, a
budget that discarded valid signals, a kill switch that reset on restart. Not
one of them created an edge. They made a system trade more faithfully toward
whatever expectancy it already had.

What this reports
-----------------
  * expectancy per trade in dollars and in percent, with a bootstrap interval
  * the same, split before and after a cutoff date, so the v5.6 stop-loss fix
    on 2026-08-18 can be checked for an actual change in the distribution
  * HOW MANY TRADES would be needed to distinguish the observed edge from
    zero, given the observed variance

That last number is usually the sobering one. Option returns are wildly
dispersed — a 25% stop against a 45% target with a fat left tail — and
detecting a small edge through that noise takes a sample far larger than most
people expect. If the required n is 900 and you have 229, then "is it
profitable" is not yet answerable, and no amount of re-reading the same 229
trades will make it answerable.

    ./venv/bin/python expectancy.py
    ./venv/bin/python expectancy.py --cutoff 2026-08-18
    ./venv/bin/python expectancy.py --log agent.log

Read-only. Parses agent.log and prints. Touches nothing else.
"""

from __future__ import annotations

import argparse
import math
import random
import re
from collections import defaultdict
from pathlib import Path

TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
PNL = re.compile(
    r"REALIZED P&L:\s+([A-Z0-9]+)\s+\|\s+Entry \$([\d.]+)\s*→\s*Exit \$([\d.]+)"
    r"\s*\|\s*([+-]?[\d.]+)%\s*\|\s*\$([+-]?[\d.]+)")

SEED = 20260825
BOOT = 20_000


def log_files(path: Path) -> list[Path]:
    """agent.log plus everything logrotate moved aside, oldest first.

    v2 — the first run of this tool reported "nothing has closed yet" against a
    box that had been trading for months. logrotate had moved the history to
    agent.log.1 / agent.log.2.gz and the tool only ever opened agent.log. A
    analysis tool that silently reads a fraction of the data is worse than one
    that refuses to run, because the output looks legitimate.
    """
    found = []
    for p in path.parent.glob(path.name + "*"):
        if p.is_file():
            m = re.search(r"\.(\d+)(?:\.gz)?$", p.name)
            found.append((int(m.group(1)) if m else 0, p))
    # logrotate numbers ascending with age, so reverse for chronological order.
    return [p for _, p in sorted(found, key=lambda t: -t[0])]


def _open(p: Path):
    if p.suffix == ".gz":
        import gzip
        return gzip.open(p, "rt", errors="ignore")
    return open(p, errors="ignore")


def load(path: Path, verbose: bool = True) -> list[dict]:
    files = log_files(path)
    if verbose:
        print(f"  reading {len(files)} log file(s): "
              f"{', '.join(f.name for f in files)}")
    out, loose = [], []
    for f in files:
        date = None
        try:
            with _open(f) as fh:
                for line in fh:
                    t = TS.match(line)
                    if t:
                        date = t.group(1)
                    if "REALIZED" not in line:
                        continue
                    m = PNL.search(line)
                    if m:
                        sym, entry, exit_, pct, dollars = m.groups()
                        out.append({
                            "date": date, "symbol": sym,
                            "entry": float(entry), "exit": float(exit_),
                            "pct": float(pct), "dollars": float(dollars),
                        })
                    elif len(loose) < 5:
                        loose.append(line.rstrip())
        except Exception as e:
            print(f"  ⚠️  could not read {f.name}: {e}")

    # Format drift is a real risk with regex log mining. If lines mention
    # REALIZED but none parse, say so loudly rather than reporting "0 trades",
    # which reads as "no trading happened".
    if loose and not out:
        print(f"\n  ⚠️  Found {len(loose)}+ lines containing 'REALIZED' that the")
        print(f"     parser could not read. The log format has probably")
        print(f"     changed. Samples:")
        for l in loose:
            print(f"       {l[:120]}")
    elif loose:
        print(f"  ⚠️  {len(loose)} REALIZED line(s) did not parse "
              f"(parsed {len(out)} fine)")
    return out


def boot_mean(vals: list[float], reps: int = BOOT) -> tuple[float, float, float, float]:
    """Bootstrap mean with a 95% interval and a two-sided p against zero."""
    n = len(vals)
    if n < 2:
        return (float("nan"),) * 4
    rng = random.Random(SEED)
    point = sum(vals) / n
    means = []
    for _ in range(reps):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * reps)]
    hi = means[int(0.975 * reps)]
    side = sum(1 for m in means if m <= 0) / reps
    return point, lo, hi, min(1.0, 2 * min(side, 1 - side))


def required_n(vals: list[float]) -> float:
    """Trades needed for 80% power to distinguish this mean from zero at 5%."""
    n = len(vals)
    if n < 2:
        return float("nan")
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    sd = math.sqrt(var)
    if mean == 0 or sd == 0:
        return float("inf")
    # (z_{a/2} + z_beta)^2 * sd^2 / effect^2  →  (1.96 + 0.84)^2 = 7.849
    return 7.849 * (sd / mean) ** 2


def block(name: str, rows: list[dict]) -> None:
    if len(rows) < 5:
        print(f"\n  {name}: only {len(rows)} trades — nothing to say.")
        return
    pcts = [r["pct"] for r in rows]
    dols = [r["dollars"] for r in rows]
    wins = sum(1 for p in pcts if p > 0)

    mp, plo, phi, pp = boot_mean(pcts)
    md, dlo, dhi, dp = boot_mean(dols)
    need = required_n(pcts)

    print(f"\n  ── {name} ──")
    print(f"    trades              {len(rows)}")
    print(f"    win rate            {100*wins/len(rows):.0f}%  ({wins}W / {len(rows)-wins}L)")
    print(f"    total P&L           ${sum(dols):+,.2f}")
    print(f"    mean per trade      {mp:+.1f}%   95% CI [{plo:+.1f}, {phi:+.1f}]  p={pp:.3f}")
    print(f"    mean per trade      ${md:+.2f}   95% CI [${dlo:+.2f}, ${dhi:+.2f}]  p={dp:.3f}")
    print(f"    median              {sorted(pcts)[len(pcts)//2]:+.1f}%")
    print(f"    best / worst        {max(pcts):+.0f}% / {min(pcts):+.0f}%")

    losers = [p for p in pcts if p <= 0]
    if losers:
        deep = sum(1 for p in losers if p < -50)
        print(f"    losers beyond -50%  {deep} of {len(losers)} "
              f"({100*deep/len(losers):.0f}% of losing trades)")

    verdict = ("POSITIVE, and distinguishable from zero" if pp < 0.05 and mp > 0
               else "NEGATIVE, and distinguishable from zero" if pp < 0.05
               else "indistinguishable from zero")
    print(f"    →  expectancy is {verdict}")
    if not math.isinf(need) and not math.isnan(need):
        print(f"    →  to call this edge real at 80% power you would need "
              f"~{need:,.0f} trades")
        if need > len(rows):
            print(f"       you have {len(rows)}. The honest answer at this "
                  f"sample size is 'not yet known'.")


def payoff_math(rows: list[dict]) -> None:
    """The arithmetic that decides whether a take-profit/stop pair can work.

    A +45% target against a -25% stop needs a win rate of 25/(45+25) = 35.7%
    just to break even BEFORE costs. Every option round trip also pays the
    bid-ask spread in full — buy at the ask, sell at the bid — so the real
    hurdle is higher. If the observed win rate sits a few points above the
    breakeven line and the spread is a few percent, the system is a coin flip
    with a house edge against it, which is exactly what a mean of +1.0% at
    p=0.858 across 296 trades looks like.
    """
    wins = [r["pct"] for r in rows if r["pct"] > 0]
    losses = [r["pct"] for r in rows if r["pct"] <= 0]
    if not wins or not losses:
        return
    aw = sum(wins) / len(wins)
    al = abs(sum(losses) / len(losses))
    wr = len(wins) / len(rows) * 100
    be = al / (aw + al) * 100

    print(f"\n  ── payoff arithmetic ──")
    print(f"    average win            {aw:+.1f}%")
    print(f"    average loss           {-al:+.1f}%")
    print(f"    payoff ratio           {aw/al:.2f} : 1")
    print(f"    breakeven win rate     {be:.1f}%   (before costs)")
    print(f"    actual win rate        {wr:.1f}%")
    margin = wr - be
    print(f"    margin                 {margin:+.1f}pp")
    edge = (wr/100) * aw - (1 - wr/100) * al
    print(f"    implied edge per trade {edge:+.2f}%  before spread")
    print(f"\n    Every round trip pays the full bid-ask spread — you buy at")
    print(f"    the ask and sell at the bid. Observed spreads on these")
    print(f"    contracts run 3-8%. Subtract that from the line above.")
    if edge < 8:
        print(f"    →  a {edge:+.2f}% edge does not survive a 3-8% spread.")


def outlier_sensitivity(rows: list[dict]) -> None:
    """How much of the result rests on the largest few trades?

    A strategy whose entire profit comes from one or two positions has not
    demonstrated an edge; it has demonstrated that it was holding a lottery
    ticket when one paid. The question is what the other 294 trades did.
    """
    dols = sorted((r["dollars"] for r in rows), reverse=True)
    total = sum(dols)
    print(f"\n  ── how much rides on the best few trades? ──")
    print(f"    all {len(dols)} trades           ${total:>+10,.2f}")
    for k in (1, 2, 3, 5, 10):
        if k < len(dols):
            rest = total - sum(dols[:k])
            print(f"    excluding best {k:<2}        ${rest:>+10,.2f}"
                  f"   (dropped ${sum(dols[:k]):+,.0f})")
    if len(dols) > 2 and total > 0 and total - sum(dols[:2]) < 0:
        print(f"\n    →  Remove the two best trades and six months is NEGATIVE.")
        print(f"       That is not an edge, it is one lottery ticket that paid.")


def by_entry_price(rows: list[dict]) -> None:
    """Expectancy split by what the contract cost.

    This directly tests a recommendation I made earlier in the week — move to
    cheaper underlyings so the budget can reach real delta. It cuts both ways:
    a $0.35 contract with a $0.02 spread pays 6% in costs per round trip,
    while a $7.00 contract with a $0.10 spread pays 1.4%. Cheap contracts are
    reachable AND carry the worst proportional cost drag. Which effect wins is
    an empirical question, and this is the data that answers it.
    """
    buckets = [(0, 0.50), (0.50, 1.00), (1.00, 2.00), (2.00, 5.00), (5.00, 1e9)]
    print(f"\n  ── expectancy by contract price ──")
    print(f"    {'entry price':<16}{'n':>5}{'win%':>7}{'mean':>9}"
          f"{'median':>9}{'total $':>12}")
    print("    " + "-" * 58)
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r["entry"] < hi]
        if len(sel) < 8:
            continue
        pcts = [r["pct"] for r in sel]
        w = sum(1 for p in pcts if p > 0)
        label = f"${lo:.2f}-${hi:.2f}" if hi < 1e9 else f"${lo:.2f}+"
        print(f"    {label:<16}{len(sel):>5}{100*w/len(sel):>6.0f}%"
              f"{sum(pcts)/len(sel):>+9.1f}{sorted(pcts)[len(pcts)//2]:>+9.1f}"
              f"{sum(r['dollars'] for r in sel):>+12,.0f}")
    print(f"\n    A cheap contract is not a cheap trade. The bid-ask spread is")
    print(f"    roughly constant in cents and therefore largest in PERCENT on")
    print(f"    the cheapest contracts — which is where a small budget is")
    print(f"    forced to shop.")


OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _dte(rec: dict) -> int | None:
    """Days to expiry at CLOSE, decoded from the OCC symbol.

    AAPL260819C00205000  →  root AAPL, expiry 2026-08-19, call, strike 205.
    Holds average well under a day, so this is close to DTE at entry.
    """
    m = OCC.match(rec["symbol"])
    if not m or not rec.get("date"):
        return None
    try:
        from datetime import date
        y, mo, d = 2000 + int(m.group(2)[:2]), int(m.group(2)[2:4]), int(m.group(2)[4:6])
        cy, cm, cd = (int(x) for x in rec["date"].split("-"))
        return (date(y, mo, d) - date(cy, cm, cd)).days
    except Exception:
        return None


def by_dte(rows: list[dict]) -> None:
    """Expectancy by days to expiry — the confound sitting behind price.

    A cheap contract is usually cheap for one of two reasons: it is far out of
    the money, or it is nearly expired. Both destroy option value, but they
    call for different fixes — one is a strike choice, the other is
    min_days_to_expiry. Splitting on price alone cannot tell them apart, and
    acting on the wrong one wastes weeks.
    """
    tagged = [(d, r) for r in rows if (d := _dte(r)) is not None]
    if len(tagged) < 30:
        print(f"\n  ── expectancy by days to expiry ──")
        print(f"    only {len(tagged)} symbols decoded — skipping.")
        return
    print(f"\n  ── expectancy by days to expiry (at close) ──")
    print(f"    {'DTE':<12}{'n':>5}{'win%':>7}{'mean':>9}{'median':>9}"
          f"{'avg entry':>11}{'total $':>11}")
    print("    " + "-" * 64)
    for lo, hi, label in [(0, 1, "0 (same day)"), (1, 2, "1"), (2, 4, "2-3"),
                          (4, 8, "4-7"), (8, 10_000, "8+")]:
        sel = [r for d, r in tagged if lo <= d < hi]
        if len(sel) < 8:
            continue
        pcts = [r["pct"] for r in sel]
        w = sum(1 for p in pcts if p > 0)
        print(f"    {label:<12}{len(sel):>5}{100*w/len(sel):>6.0f}%"
              f"{sum(pcts)/len(sel):>+9.1f}"
              f"{sorted(pcts)[len(pcts)//2]:>+9.1f}"
              f"{sum(r['entry'] for r in sel)/len(sel):>11.2f}"
              f"{sum(r['dollars'] for r in sel):>+11,.0f}")

    # The decisive cross-tab: does price still matter once DTE is held fixed?
    print(f"\n    ── price effect WITHIN each DTE band ──")
    print(f"    {'DTE':<12}{'<$1 n':>7}{'<$1 win%':>10}"
          f"{'$1+ n':>7}{'$1+ win%':>10}{'gap':>8}")
    print("    " + "-" * 56)
    for lo, hi, label in [(0, 2, "0-1"), (2, 8, "2-7"), (8, 10_000, "8+")]:
        band = [r for d, r in tagged if lo <= d < hi]
        cheap = [r for r in band if r["entry"] < 1.00]
        rich = [r for r in band if r["entry"] >= 1.00]
        if len(cheap) < 8 or len(rich) < 8:
            continue
        cw = 100 * sum(1 for r in cheap if r["pct"] > 0) / len(cheap)
        rw = 100 * sum(1 for r in rich if r["pct"] > 0) / len(rich)
        print(f"    {label:<12}{len(cheap):>7}{cw:>9.0f}%"
              f"{len(rich):>7}{rw:>9.0f}%{rw-cw:>+7.0f}pp")
    print(f"\n    If the gap persists inside every DTE band, price is doing")
    print(f"    the work and min_contract_price is the lever. If it collapses,")
    print(f"    price was a proxy for expiry and min_days_to_expiry is.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="agent.log")
    ap.add_argument("--cutoff", default=None,
                    help="split before/after this date, e.g. 2026-08-18")
    a = ap.parse_args()

    path = Path(a.log)
    if not path.is_absolute():
        path = Path(__file__).parent / a.log
    if not path.exists():
        print(f"No log at {path}")
        return 1

    rows = load(path)
    if not rows:
        print("\n  No parseable 'REALIZED P&L' lines found.")
        print("  Check what history actually exists:")
        print("      ls -la agent.log*")
        print("      zgrep -c REALIZED agent.log*  2>/dev/null || "
              "grep -c REALIZED agent.log*")
        print("  If the history is elsewhere, point at it: "
              "expectancy.py --log /path/to/agent.log")
        return 1

    dates = sorted({r["date"] for r in rows if r["date"]})
    print("=" * 74)
    print(f"  EXPECTANCY — {len(rows)} closed trades, "
          f"{dates[0]} → {dates[-1]}")
    print("=" * 74)
    print("  Expectancy is the average outcome of one trade. It is the only")
    print("  number that decides whether trading more is good or bad. A gate")
    print("  that improves win rate while lowering expectancy makes things")
    print("  worse, and vice versa.")

    block("ALL TRADES", rows)
    payoff_math(rows)
    outlier_sensitivity(rows)
    by_entry_price(rows)
    by_dte(rows)

    if a.cutoff:
        before = [r for r in rows if r["date"] and r["date"] < a.cutoff]
        after = [r for r in rows if r["date"] and r["date"] >= a.cutoff]
        block(f"BEFORE {a.cutoff}", before)
        block(f"ON/AFTER {a.cutoff}", after)
        if len(before) >= 5 and len(after) >= 5:
            bp = [r["pct"] for r in before]
            apct = [r["pct"] for r in after]
            rng = random.Random(SEED + 5)
            diffs = []
            for _ in range(BOOT):
                x = sum(bp[rng.randrange(len(bp))] for _ in range(len(bp))) / len(bp)
                y = sum(apct[rng.randrange(len(apct))] for _ in range(len(apct))) / len(apct)
                diffs.append(y - x)
            diffs.sort()
            d = sum(apct)/len(apct) - sum(bp)/len(bp)
            lo, hi = diffs[int(0.025*BOOT)], diffs[int(0.975*BOOT)]
            side = sum(1 for v in diffs if v <= 0) / BOOT
            p = min(1.0, 2 * min(side, 1 - side))
            print(f"\n  ── did anything change at {a.cutoff}? ──")
            print(f"    difference in mean return  {d:+.1f}pp  "
                  f"95% CI [{lo:+.1f}, {hi:+.1f}]  p={p:.3f}")
            print(f"    → {'a real change' if p < 0.05 else 'no detectable change'}")
            if p < 0.05:
                print(f"\n    ⚠️  Only trust this if you chose {a.cutoff} BEFORE")
                print(f"       looking at the P&L. Picking the split point")
                print(f"       after noticing a drawdown there guarantees a")
                print(f"       small p-value — the date was selected BY the")
                print(f"       effect it is now being used to test. With "
                      f"{min(len(before), len(after))} trades")
                print(f"       on the smaller side, treat it as a hypothesis.")

    # Per-day P&L, so a single catastrophic session is visible rather than
    # averaged away.
    byday: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["date"]:
            byday[r["date"]] += r["dollars"]
    if len(byday) > 1:
        print(f"\n  ── daily realised P&L ──")
        for d in sorted(byday):
            bar = "█" * min(40, int(abs(byday[d]) / 10))
            print(f"    {d}  ${byday[d]:>+9.2f}  {bar}")
        up = sum(1 for v in byday.values() if v > 0)
        print(f"\n    {up}/{len(byday)} days positive")

    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
