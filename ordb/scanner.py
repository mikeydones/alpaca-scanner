"""Orchestration: universe -> cheap prefilter -> bars -> evaluate -> ranked list."""
from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from .client import Alpaca
from .config import Config, DEFAULT
from .providers import MarketCapProvider
from .rules import Context, Rejection, Setup, Verdict, evaluate

ET = "America/New_York"


def scan(
    api: Alpaca,
    day: Optional[pd.Timestamp] = None,
    cfg: Config = DEFAULT,
    caps: Optional[MarketCapProvider] = None,
    universe: Optional[list[str]] = None,
) -> tuple[list[Setup], list[Rejection]]:
    day = day or pd.Timestamp.now(tz=ET).normalize()
    equity = float(api.account()["equity"])

    # --- pass 1: who is even in play today? -------------------------------
    if universe is None:
        actives = [r["symbol"] for r in api.most_actives(by="volume", top=200)]
        assets = {a["symbol"]: a for a in api.tradable_universe()}
        universe = [s for s in actives if s in assets]
    else:
        assets = {a["symbol"]: a for a in api.tradable_universe()}

    snaps = api.snapshots(universe, feed=cfg.feed)
    shortlist = []
    for sym, snap in snaps.items():
        daily = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        px = (snap.get("latestTrade") or {}).get("p") or daily.get("c")
        if not px or not (cfg.min_price <= px <= cfg.max_price):
            continue
        if prev.get("v") and daily.get("v", 0) / max(prev["v"], 1) < 0.10:
            continue                       # nowhere near yesterday's pace
        shortlist.append(sym)
    shortlist = shortlist[: max(cfg.watchlist_size * 4, 100)]

    # --- pass 2: real bars for the survivors ------------------------------
    start_i = (day - pd.Timedelta(days=cfg.rvol_lookback_sessions * 2)).strftime("%Y-%m-%d")
    start_d = (day - pd.Timedelta(days=cfg.htf_lookback_days * 2)).strftime("%Y-%m-%d")
    intra = api.bars(shortlist, f"{cfg.bar_minutes}Min", start_i, feed=cfg.feed)
    dailies = api.bars(shortlist, "1Day", start_d, feed=cfg.feed)

    setups: list[Setup] = []
    rejects: list[Rejection] = []
    for sym in shortlist:
        if sym not in intra or sym not in dailies:
            continue
        a = assets.get(sym, {})
        ctx = Context(
            symbol=sym,
            market_cap=caps.market_cap(sym) if caps else None,
            tradable=a.get("tradable", True),
            shortable=a.get("shortable", False),
            easy_to_borrow=a.get("easy_to_borrow", False),
            equity=equity,
        )
        v: Verdict = evaluate(sym, intra[sym], dailies[sym], day, ctx, cfg)
        (setups if isinstance(v, Setup) else rejects).append(v)

    setups.sort(key=lambda s: s.score, reverse=True)
    return setups[: cfg.watchlist_size], rejects
