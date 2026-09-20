"""Pure functions over OHLCV frames. No network, no Alpaca, no state.

Every frame is expected to be a pandas DataFrame indexed by tz-aware
UTC timestamps with columns: open, high, low, close, volume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

ET = "America/New_York"


# --------------------------------------------------------------------------
# Candle anatomy  (Mike step 2.1: "85% body and no more than 15% wick")
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Candle:
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def body_pct(self) -> float:
        """Body as a fraction of the full range. 1.0 = a perfect marubozu."""
        return self.body / self.range if self.range > 0 else 0.0

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def direction(self) -> int:
        """+1 green, -1 red, 0 doji."""
        if self.close > self.open:
            return 1
        if self.close < self.open:
            return -1
        return 0


def candles(df: pd.DataFrame) -> list[Candle]:
    return [
        Candle(ts, r.open, r.high, r.low, r.close, r.volume)
        for ts, r in df.iterrows()
    ]


def body_pct_series(df: pd.DataFrame) -> pd.Series:
    rng = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()
    return (body / rng.where(rng > 0)).fillna(0.0)


def confirming_run(df: pd.DataFrame, body_pct_min: float) -> tuple[int, int]:
    """Longest run of consecutive same-direction candles whose body >= body_pct_min.

    Returns (direction, run_length). direction is +1, -1 or 0.
    This is Mike's "the more big body candles you see consecutively the
    stronger the confirmation".
    """
    bp = body_pct_series(df)
    dirn = np.sign(df["close"] - df["open"]).astype(int)
    # epsilon: a candle that is arithmetically exactly 85% body lands on
    # 0.8499999999999996 in float64. Without this, a rule Mike would call a
    # pass gets silently rejected.
    qualifies = bp >= body_pct_min - 1e-9

    best_dir, best_len = 0, 0
    cur_dir, cur_len = 0, 0
    for q, d in zip(qualifies.to_numpy(), dirn.to_numpy()):
        if q and d != 0 and d == cur_dir:
            cur_len += 1
        elif q and d != 0:
            cur_dir, cur_len = int(d), 1
        else:
            cur_dir, cur_len = 0, 0
        if cur_len > best_len:
            best_dir, best_len = cur_dir, cur_len
    return best_dir, best_len


# --------------------------------------------------------------------------
# Moving averages  (Mike step 2.2: daily 200 SMA and 20 EMA)
# --------------------------------------------------------------------------
def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# --------------------------------------------------------------------------
# Fair value gaps  (Mike step 8: "the fair value gap I draw on the daily")
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FVG:
    """A 3-candle imbalance.

    bullish: candle[i].low  > candle[i-2].high  -> unfilled demand below
    bearish: candle[i].high < candle[i-2].low   -> unfilled supply above

    `lower`/`upper` bound the untraded price zone.
    """
    kind: Literal["bullish", "bearish"]
    ts: pd.Timestamp
    lower: float
    upper: float

    @property
    def size(self) -> float:
        return self.upper - self.lower

    @property
    def mid(self) -> float:
        return (self.upper + self.lower) / 2

    def edge(self, which: str, approaching_from_below: bool) -> float:
        if which == "mid":
            return self.mid
        near, far = (self.lower, self.upper) if approaching_from_below else (self.upper, self.lower)
        return near if which == "near" else far


def find_fvgs(df: pd.DataFrame, min_gap_pct: float = 0.0) -> list[FVG]:
    """All 3-candle FVGs in the frame, oldest first."""
    out: list[FVG] = []
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    idx = df.index
    for i in range(2, len(df)):
        ref = (h[i] + l[i]) / 2
        if l[i] > h[i - 2]:
            lo, hi = h[i - 2], l[i]
            if ref > 0 and (hi - lo) / ref >= min_gap_pct:
                out.append(FVG("bullish", idx[i], lo, hi))
        elif h[i] < l[i - 2]:
            lo, hi = h[i], l[i - 2]
            if ref > 0 and (hi - lo) / ref >= min_gap_pct:
                out.append(FVG("bearish", idx[i], lo, hi))
    return out


def unfilled_fvgs(df: pd.DataFrame, min_gap_pct: float = 0.0) -> list[FVG]:
    """FVGs that price has not since traded fully through.

    A gap is considered filled once a later candle closes the zone: for a
    bullish gap, a later low <= gap.lower; for a bearish gap, a later high
    >= gap.upper.
    """
    gaps = find_fvgs(df, min_gap_pct)
    alive: list[FVG] = []
    for g in gaps:
        after = df.loc[df.index > g.ts]
        if after.empty:
            alive.append(g)
            continue
        if g.kind == "bullish" and after["low"].min() > g.lower:
            alive.append(g)
        elif g.kind == "bearish" and after["high"].max() < g.upper:
            alive.append(g)
    return alive


def next_target_fvg(
    df: pd.DataFrame,
    price: float,
    side: Literal["long", "short"],
    polarity: str = "opposite",
    min_gap_pct: float = 0.0,
) -> Optional[FVG]:
    """The nearest unfilled gap in the direction of the trade.

    Why 'opposite' is the default: an unfilled BEARISH gap sits above price as
    untraded supply left behind on the way down - that is what price rallies
    into and what makes a natural long target. Unfilled BULLISH gaps sit below
    price and act as support, not as an overhead objective. Set polarity="any"
    to target whichever gap is nearest regardless of kind.
    """
    gaps = unfilled_fvgs(df, min_gap_pct)
    if side == "long":
        want = "bearish" if polarity == "opposite" else ("bullish" if polarity == "same" else None)
        pool = [g for g in gaps if g.lower > price and (want is None or g.kind == want)]
        return min(pool, key=lambda g: g.lower) if pool else None
    want = "bullish" if polarity == "opposite" else ("bearish" if polarity == "same" else None)
    pool = [g for g in gaps if g.upper < price and (want is None or g.kind == want)]
    return max(pool, key=lambda g: g.upper) if pool else None


# --------------------------------------------------------------------------
# Session slicing
# --------------------------------------------------------------------------
def session_slice(df: pd.DataFrame, day: pd.Timestamp, start_hhmm: str, end_hhmm: str) -> pd.DataFrame:
    """Bars whose ET timestamp falls in [start, end) on `day` (an ET date)."""
    et = df.tz_convert(ET)
    same_day = et[et.index.normalize() == pd.Timestamp(day).tz_localize(None).normalize().tz_localize(ET)]
    return same_day.between_time(start_hhmm, end_hhmm, inclusive="left")


def opening_range(df: pd.DataFrame, day: pd.Timestamp, minutes: int = 15) -> Optional[tuple[float, float, float]]:
    """(high, low, open_price) of the first `minutes` of regular trading.

    High and low are taken from the WICKS, per Mike step 5.1.
    open_price is the 09:30 print - the stop basis in step 9.
    """
    end_min = 30 + minutes
    end = f"{9 + end_min // 60:02d}:{end_min % 60:02d}"
    win = session_slice(df, day, "09:30", end)
    if win.empty:
        return None
    return float(win["high"].max()), float(win["low"].min()), float(win["open"].iloc[0])


def premarket_rvol(
    intraday: pd.DataFrame,
    day: pd.Timestamp,
    start_hhmm: str = "04:00",
    end_hhmm: str = "09:30",
    lookback: int = 20,
) -> Optional[float]:
    """Today's cumulative pre-market volume / mean of the prior N sessions'.

    Compares like with like: the same clock window on each prior session, so a
    09:10 scan is measured against 04:00-09:10 on previous days, not against a
    full day's volume.
    """
    et = intraday.tz_convert(ET)
    win = et.between_time(start_hhmm, end_hhmm, inclusive="left")
    by_day = win.groupby(win.index.normalize())["volume"].sum()
    target = pd.Timestamp(day).tz_localize(None).normalize().tz_localize(ET)
    if target not in by_day.index:
        return None
    prior = by_day[by_day.index < target].tail(lookback)
    if prior.empty or prior.mean() <= 0:
        return None
    return float(by_day[target] / prior.mean())
