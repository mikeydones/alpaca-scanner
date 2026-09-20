"""Run the engine both ways on the same sessions and diff the targets.

Answers the open SPEC question: for a long, is the target the unfilled BEARISH
gap overhead (untraded supply), or simply the nearest gap of any kind?

Usage:
    export ALPACA_KEY_ID=...  ALPACA_SECRET_KEY=...
    python3 compare_polarity.py AAPL NVDA AMD --days 30
"""
from __future__ import annotations

import argparse, sys
from dataclasses import replace

import pandas as pd

from ordb.client import Alpaca
from ordb.config import Config
from ordb.rules import Context, Setup, evaluate
from ordb.providers import NasdaqScreenerProvider

ET = "America/New_York"


def run(symbols: list[str], days: int, equity: float = 100_000.0) -> pd.DataFrame:
    api = Alpaca(paper=True)
    caps = NasdaqScreenerProvider()
    end = pd.Timestamp.now(tz=ET).normalize()
    start_i = (end - pd.Timedelta(days=days + 40)).strftime("%Y-%m-%d")
    start_d = (end - pd.Timedelta(days=700)).strftime("%Y-%m-%d")

    base = Config(min_reward_risk=0.0, skip_if_market_cap_unknown=False)
    opp = replace(base, fvg_polarity="opposite")
    any_ = replace(base, fvg_polarity="any")

    intra = api.bars(symbols, "5Min", start_i)
    daily = api.bars(symbols, "1Day", start_d)

    sessions = pd.bdate_range(end - pd.Timedelta(days=days), end, tz=ET)
    rows = []
    for sym in symbols:
        if sym not in intra or sym not in daily:
            continue
        cap = caps.market_cap(sym)
        for day in sessions:
            d_hist = daily[sym][daily[sym].index < day]
            if len(d_hist) < base.daily_sma_period:
                continue
            ctx = Context(sym, market_cap=cap, equity=equity)
            a = evaluate(sym, intra[sym], d_hist, day, ctx, opp)
            b = evaluate(sym, intra[sym], d_hist, day, ctx, any_)
            if not isinstance(a, Setup) and not isinstance(b, Setup):
                continue
            rows.append({
                "symbol": sym,
                "date": day.date(),
                "side": (a if isinstance(a, Setup) else b).side,
                "entry": (a if isinstance(a, Setup) else b).entry,
                "stop": (a if isinstance(a, Setup) else b).stop,
                "target_bearish": a.target if isinstance(a, Setup) else None,
                "R_bearish": a.reward_risk if isinstance(a, Setup) else None,
                "target_any": b.target if isinstance(b, Setup) else None,
                "R_any": b.reward_risk if isinstance(b, Setup) else None,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["same"] = df["target_bearish"] == df["target_any"]
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--csv", default="polarity_comparison.csv")
    a = ap.parse_args()

    df = run([s.upper() for s in a.symbols], a.days)
    if df.empty:
        print("No setups triggered in that window. Widen --days or add symbols.")
        return 1

    df.to_csv(a.csv, index=False)
    diff = df[~df["same"]]
    print(f"\n{len(df)} setups, {len(diff)} where the two rules disagree "
          f"({len(diff)/len(df):.0%})\n")
    with pd.option_context("display.width", 140, "display.max_rows", 60):
        print(df.to_string(index=False))
    if not diff.empty:
        print(f"\nMedian R, bearish-only: {diff['R_bearish'].median():.2f}")
        print(f"Median R, any gap:      {diff['R_any'].median():.2f}")
    print(f"\nwrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
