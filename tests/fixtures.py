"""Synthetic bar builders so every rule can be tested in isolation."""
from __future__ import annotations
import numpy as np, pandas as pd

ET = "America/New_York"


def bar(o, h, l, c, v=100_000):
    return dict(open=o, high=h, low=l, close=c, volume=v)


def big_body(open_, pct_move, body_pct=0.95, up=True, vol=500_000):
    """A candle whose body is `body_pct` of its range - Mike's confirmation bar."""
    body = open_ * pct_move
    close = open_ + body if up else open_ - body
    rng = body / body_pct
    wick = rng - body
    hi = max(open_, close) + wick / 2
    lo = min(open_, close) - wick / 2
    return bar(open_, hi, lo, close, vol)


def intraday_frame(day: str, rows: list[tuple[str, dict]]) -> pd.DataFrame:
    idx = [pd.Timestamp(f"{day} {t}", tz=ET).tz_convert("UTC") for t, _ in rows]
    return pd.DataFrame([r for _, r in rows], index=pd.DatetimeIndex(idx))


def prior_premarket(days: list[str], per_bar_vol: float = 10_000) -> list[tuple]:
    """Quiet pre-market sessions to form the RVOL baseline."""
    out = []
    for d in days:
        for hh in range(4, 9):
            for mm in (0, 30):
                out.append((f"{d} {hh:02d}:{mm:02d}", bar(10, 10.05, 9.95, 10.0, per_bar_vol)))
    return out


def frame_from_pairs(pairs) -> pd.DataFrame:
    idx = [pd.Timestamp(t, tz=ET).tz_convert("UTC") for t, _ in pairs]
    return pd.DataFrame([r for _, r in pairs], index=pd.DatetimeIndex(idx)).sort_index()


def daily_uptrend(n=260, start=50.0, drift=0.004, seed=7) -> pd.DataFrame:
    """A clean uptrend so close > 200SMA and > 20EMA."""
    rng = np.random.default_rng(seed)
    px, rows, idx = start, [], []
    d = pd.Timestamp("2025-01-02", tz=ET)
    for i in range(n):
        while d.dayofweek >= 5:
            d += pd.Timedelta(days=1)
        o = px
        c = px * (1 + drift + rng.normal(0, 0.006))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        rows.append(bar(o, h, l, c, 2_000_000)); idx.append(d)
        px = c; d += pd.Timedelta(days=1)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx).tz_convert("UTC"))


def inject_bearish_fvg(daily: pd.DataFrame, above: float, gap_pct=0.02) -> pd.DataFrame:
    """Force an unfilled bearish gap (untraded supply) above `above`.

    Bearish FVG at i:  high[i] < low[i-2].  Zone = (high[i], low[i-2]).
    We build it well above current price and never trade back into it.
    """
    df = daily.copy()
    lo_edge = above * 1.03
    hi_edge = lo_edge * (1 + gap_pct)
    d0 = df.index[-1] + pd.Timedelta(days=1)
    rows = [
        # i-2: trades down to hi_edge (its low is the TOP of the gap)
        bar(hi_edge * 1.02, hi_edge * 1.03, hi_edge, hi_edge * 1.005, 3_000_000),
        bar(hi_edge * 1.00, hi_edge * 1.01, lo_edge * 0.999, lo_edge * 1.0, 3_000_000),
        # i: its high is the BOTTOM of the gap -> gap = (lo_edge, hi_edge)
        bar(lo_edge * 0.99, lo_edge, lo_edge * 0.95, lo_edge * 0.96, 3_000_000),
    ]
    idx = [d0, d0 + pd.Timedelta(days=1), d0 + pd.Timedelta(days=2)]
    df = pd.concat([df, pd.DataFrame(rows, index=pd.DatetimeIndex(idx).tz_convert("UTC"))])
    # then walk price back DOWN to the original level so the gap stays unfilled overhead
    px = lo_edge * 0.96
    tail, tidx = [], []
    d = idx[-1] + pd.Timedelta(days=1)
    target = above
    for i in range(25):
        o = px
        c = px + (target - px) * 0.25
        tail.append(bar(o, max(o, c) * 1.002, min(o, c) * 0.998, c, 2_000_000))
        tidx.append(d); px = c; d += pd.Timedelta(days=1)
    df = pd.concat([df, pd.DataFrame(tail, index=pd.DatetimeIndex(tidx).tz_convert("UTC"))])
    return df
