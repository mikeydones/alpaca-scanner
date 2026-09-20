"""The setup engine.

evaluate() is a pure function: bars in, a Setup or a rejection out. No network
calls live here, which is what makes it backtestable and testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

import pandas as pd

from .config import Config, DEFAULT
from .indicators import (
    FVG, confirming_run, ema, sma, next_target_fvg,
    opening_range, premarket_rvol, session_slice,
)

Side = Literal["long", "short"]


# --------------------------------------------------------------------------
@dataclass
class Context:
    """Everything about the symbol that does not come out of a bar frame."""
    symbol: str
    market_cap: Optional[float] = None      # Alpaca does NOT serve this - see providers.py
    tradable: bool = True
    shortable: bool = True
    easy_to_borrow: bool = True
    sector: Optional[str] = None
    headlines: list[str] = field(default_factory=list)
    equity: float = 25_000.0


@dataclass
class Setup:
    symbol: str
    side: Side
    trigger_level: float      # the line Mike draws, at the wick
    entry: float              # trigger +/- 2c
    stop: float               # the 09:30 open
    target: float             # near edge of the overhead FVG
    risk_per_share: float
    reward_risk: float
    qty: int
    qty_scale: int            # the 75%
    qty_runner: int           # the 25%
    score: float
    rvol: Optional[float]
    body_run: int
    fvg: Optional[FVG]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fvg"] = None if self.fvg is None else {
            "kind": self.fvg.kind, "lower": self.fvg.lower,
            "upper": self.fvg.upper, "ts": str(self.fvg.ts),
        }
        return d


@dataclass
class Rejection:
    symbol: str
    reason: str
    detail: str = ""


Verdict = Setup | Rejection


# --------------------------------------------------------------------------
def htf_bias(daily: pd.DataFrame, cfg: Config) -> tuple[Optional[Side], str]:
    """Mike step 2.2 - daily close vs the 200 SMA and 20 EMA."""
    if len(daily) < cfg.daily_sma_period:
        return None, f"only {len(daily)} daily bars, need {cfg.daily_sma_period}"
    c = float(daily["close"].iloc[-1])
    s200 = float(sma(daily["close"], cfg.daily_sma_period).iloc[-1])
    e20 = float(ema(daily["close"], cfg.daily_ema_period).iloc[-1])
    if c > s200 and c > e20:
        return "long", f"close {c:.2f} > 200SMA {s200:.2f} and 20EMA {e20:.2f}"
    if c < s200 and c < e20:
        return "short", f"close {c:.2f} < 200SMA {s200:.2f} and 20EMA {e20:.2f}"
    return None, f"close {c:.2f} is between 200SMA {s200:.2f} and 20EMA {e20:.2f} - no HTF agreement"


def position_size(entry: float, stop: float, equity: float, cfg: Config) -> int:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0
    by_risk = (equity * cfg.account_risk_pct) / risk
    by_cap = (equity * cfg.max_position_pct) / entry
    return max(0, int(math.floor(min(by_risk, by_cap))))


# --------------------------------------------------------------------------
def evaluate(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame,
    day: pd.Timestamp,
    ctx: Context,
    cfg: Config = DEFAULT,
    now_et: Optional[str] = None,
) -> Verdict:
    """Run Mike's 9 steps in order. First failure short-circuits.

    intraday : 5-minute bars, UTC index, covering at least `rvol_lookback_sessions`
               prior sessions plus today through the opening range.
    daily    : daily bars, enough history to seed a 200 SMA, ending at yesterday.
    day      : the ET session date being evaluated.
    """
    notes: list[str] = []

    # --- Step 3: hard tradability gates -----------------------------------
    if not ctx.tradable:
        return Rejection(symbol, "not_tradable")

    if ctx.market_cap is None:
        if cfg.skip_if_market_cap_unknown:
            return Rejection(symbol, "market_cap_unknown",
                             "Alpaca serves no fundamentals - wire up a provider")
        notes.append("market cap unknown, gate skipped")
    elif ctx.market_cap < cfg.min_market_cap:
        return Rejection(symbol, "market_cap_too_small",
                         f"${ctx.market_cap/1e6:.1f}M < ${cfg.min_market_cap/1e6:.0f}M")

    # --- Step 1: pre-market relative volume -------------------------------
    rvol = premarket_rvol(intraday, day, cfg.premarket_open, cfg.momentum_window_end,
                          cfg.rvol_lookback_sessions)
    if rvol is None:
        return Rejection(symbol, "rvol_unavailable", "not enough prior-session pre-market data")
    if rvol < cfg.rvol_min:
        return Rejection(symbol, "rvol_too_low", f"{rvol:.2f}x < {cfg.rvol_min:.2f}x")

    # --- Step 2: 09:00-09:30 big-body confirmation ------------------------
    pre = session_slice(intraday, day, cfg.momentum_window_start, cfg.momentum_window_end)
    if pre.empty:
        return Rejection(symbol, "no_premarket_bars",
                         f"no bars {cfg.momentum_window_start}-{cfg.momentum_window_end} ET")
    body_dir, body_run = confirming_run(pre, cfg.body_pct_min)
    if body_run < cfg.min_confirming_candles:
        return Rejection(symbol, "no_body_confirmation",
                         f"no 5m candle with body >= {cfg.body_pct_min:.0%}")
    pre_side: Side = "long" if body_dir > 0 else "short"

    # --- Step 2b: higher timeframe must agree -----------------------------
    bias, bias_note = htf_bias(daily, cfg)
    if bias is None:
        return Rejection(symbol, "no_htf_trend", bias_note)
    if bias != pre_side:
        return Rejection(symbol, "htf_conflict",
                         f"pre-market is {pre_side}, daily is {bias}")
    side: Side = bias
    notes.append(bias_note)

    if side == "short":
        if not cfg.allow_short:
            return Rejection(symbol, "shorts_disabled")
        if not (ctx.shortable and ctx.easy_to_borrow):
            return Rejection(symbol, "not_shortable")

    # --- Steps 4-5: the 15-minute opening range, marked at the wicks ------
    orr = opening_range(intraday, day, cfg.opening_range_minutes)
    if orr is None:
        return Rejection(symbol, "no_opening_range")
    or_high, or_low, rth_open = orr

    # --- Step 6: did a 5m candle close outside the range? -----------------
    after = session_slice(intraday, day, "09:45", now_et or "16:00")
    if after.empty:
        return Rejection(symbol, "awaiting_breakout", "opening range set, no post-09:45 bars yet")
    if side == "long":
        broke = after[after["close"] > or_high]
        if broke.empty:
            return Rejection(symbol, "no_breakout", f"no 5m close above OR high {or_high:.2f}")
        trigger, entry = or_high, round(or_high + cfg.entry_offset, 2)
    else:
        broke = after[after["close"] < or_low]
        if broke.empty:
            return Rejection(symbol, "no_breakout", f"no 5m close below OR low {or_low:.2f}")
        trigger, entry = or_low, round(or_low - cfg.entry_offset, 2)
    notes.append(f"breakout confirmed on the {broke.index[0].tz_convert('America/New_York'):%H:%M} bar")

    # --- Step 9: stop at the opening bell price ---------------------------
    stop = round(rth_open, 2)
    risk = abs(entry - stop)
    if risk < cfg.min_risk_per_share:
        return Rejection(symbol, "stop_too_tight", f"${risk:.3f} risk/share")
    if risk / entry > cfg.max_risk_per_share_pct:
        return Rejection(symbol, "stop_too_wide",
                         f"${risk:.2f} is {risk/entry:.1%} of entry")
    if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
        return Rejection(symbol, "invalid_stop", f"open {stop:.2f} on wrong side of entry {entry:.2f}")

    # --- Step 8: target = nearest unfilled daily FVG ----------------------
    gap = next_target_fvg(daily.tail(cfg.fvg_lookback_bars), entry, side,
                          cfg.fvg_polarity, cfg.fvg_min_gap_pct)
    if gap is None:
        if cfg.skip_if_no_fvg:
            return Rejection(symbol, "no_fvg_target", "no unfilled daily gap in trade direction")
        target = round(entry + (risk * cfg.fallback_target_r) * (1 if side == "long" else -1), 2)
        notes.append(f"no daily FVG - falling back to {cfg.fallback_target_r:.1f}R")
    else:
        target = round(gap.edge(cfg.fvg_target_edge, approaching_from_below=(side == "long")), 2)
        notes.append(f"target = {cfg.fvg_target_edge} edge of {gap.kind} FVG "
                     f"{gap.lower:.2f}-{gap.upper:.2f} from {gap.ts:%Y-%m-%d}")

    reward = abs(target - entry)
    rr = reward / risk
    if rr < cfg.min_reward_risk:
        return Rejection(symbol, "reward_risk_too_low", f"{rr:.2f}R < {cfg.min_reward_risk:.2f}R")

    # --- Sizing and the 75/25 split ---------------------------------------
    qty = position_size(entry, stop, ctx.equity, cfg)
    if qty < 1:
        return Rejection(symbol, "size_rounds_to_zero",
                         f"${ctx.equity:,.0f} equity at {cfg.account_risk_pct:.1%} "
                         f"cannot carry ${risk:.2f}/share risk")
    qty_scale = int(round(qty * cfg.scale_out_pct))
    qty_runner = qty - qty_scale
    if qty_runner < 1:
        qty_scale, qty_runner = qty, 0
        notes.append("position too small to split - exiting in one piece at the FVG")

    score = (
        min(rvol / cfg.rvol_min, 3.0) * 2.0
        + min(body_run, 4) * 1.5
        + min(rr, 5.0)
        + (1.0 if gap is not None else 0.0)
    )

    return Setup(
        symbol=symbol, side=side, trigger_level=round(trigger, 2), entry=entry,
        stop=stop, target=target, risk_per_share=round(risk, 4),
        reward_risk=round(rr, 2), qty=qty, qty_scale=qty_scale, qty_runner=qty_runner,
        score=round(score, 2), rvol=round(rvol, 2), body_run=body_run, fvg=gap, notes=notes,
    )
