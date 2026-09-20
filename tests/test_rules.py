import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from ordb.config import Config
from ordb.rules import evaluate, Context, Setup, Rejection, position_size
from ordb.indicators import (
    Candle, body_pct_series, confirming_run, find_fvgs, unfilled_fvgs,
    next_target_fvg, opening_range, premarket_rvol,
)
from fixtures import (
    bar, big_body, frame_from_pairs, prior_premarket,
    daily_uptrend, inject_bearish_fvg,
)

DAY = "2026-01-15"
PRIORS = ["2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14"]


# ---------------------------------------------------------------- candles
def test_body_pct_marubozu_vs_doji():
    c = Candle(pd.Timestamp("2026-01-15", tz="UTC"), 10.0, 10.55, 9.95, 10.50, 1)
    assert c.range == pytest.approx(0.60)
    assert c.body == pytest.approx(0.50)
    assert c.body_pct == pytest.approx(0.8333, abs=1e-4)   # 83% -> FAILS Mike's 85%
    assert c.upper_wick == pytest.approx(0.05)
    assert c.lower_wick == pytest.approx(0.05)
    assert c.direction == 1

    doji = Candle(pd.Timestamp("2026-01-15", tz="UTC"), 10.0, 10.5, 9.5, 10.01, 1)
    assert doji.body_pct < 0.05


def test_85_percent_threshold_is_exact():
    """A candle at exactly 85% body must pass; 84.9% must not."""
    # range 1.00, body 0.85
    pass_df = pd.DataFrame([bar(10.0, 10.925, 9.925, 10.85)], index=pd.DatetimeIndex([pd.Timestamp("2026-01-15", tz="UTC")]))
    fail_df = pd.DataFrame([bar(10.0, 10.93, 9.93, 10.84)], index=pd.DatetimeIndex([pd.Timestamp("2026-01-15", tz="UTC")]))
    assert body_pct_series(pass_df).iloc[0] >= 0.85 - 1e-9
    assert body_pct_series(fail_df).iloc[0] < 0.85


def test_consecutive_run_counts_only_same_direction():
    rows = [
        ("2026-01-15 09:00", big_body(10.00, 0.01, 0.95, up=True)),
        ("2026-01-15 09:05", big_body(10.10, 0.01, 0.95, up=True)),
        ("2026-01-15 09:10", big_body(10.20, 0.01, 0.95, up=True)),
        ("2026-01-15 09:15", big_body(10.30, 0.01, 0.95, up=False)),   # flips
        ("2026-01-15 09:20", bar(10.20, 10.60, 9.80, 10.22)),          # 2.5% body, ignored
    ]
    d, run = confirming_run(frame_from_pairs(rows), 0.85)
    assert (d, run) == (1, 3)


# ---------------------------------------------------------------- FVG
def test_bullish_and_bearish_fvg_detection():
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-01-{d:02d}", tz="UTC") for d in (5, 6, 7)])
    bull = pd.DataFrame([bar(10, 10.5, 9.8, 10.4), bar(10.4, 11.5, 10.3, 11.4),
                         bar(11.4, 12.0, 10.8, 11.9)], index=idx)
    g = find_fvgs(bull)
    assert len(g) == 1 and g[0].kind == "bullish"
    assert (g[0].lower, g[0].upper) == pytest.approx((10.5, 10.8))   # high[0] -> low[2]

    bear = pd.DataFrame([bar(12, 12.2, 11.5, 11.6), bar(11.5, 11.6, 10.5, 10.6),
                         bar(10.6, 11.0, 10.2, 10.4)], index=idx)
    g = find_fvgs(bear)
    assert len(g) == 1 and g[0].kind == "bearish"
    assert (g[0].lower, g[0].upper) == pytest.approx((11.0, 11.5))   # high[2] -> low[0]


def test_filled_gap_is_dropped():
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-01-{d:02d}", tz="UTC") for d in (5, 6, 7, 8)])
    df = pd.DataFrame([bar(10, 10.5, 9.8, 10.4), bar(10.4, 11.5, 10.3, 11.4),
                       bar(11.4, 12.0, 10.8, 11.9),
                       bar(11.9, 12.0, 10.2, 10.4)], index=idx)   # trades back below 10.5
    assert len(find_fvgs(df)) == 1
    assert unfilled_fvgs(df) == []


def test_long_target_picks_nearest_overhead_bearish_gap():
    daily = inject_bearish_fvg(daily_uptrend(n=210, seed=3), above=100.0)
    g = next_target_fvg(daily, price=100.0, side="long", polarity="opposite")
    assert g is not None and g.kind == "bearish" and g.lower > 100.0


# ---------------------------------------------------------------- session math
def test_opening_range_uses_wicks_not_bodies():
    rows = prior_premarket(PRIORS) + [
        (f"{DAY} 09:30", bar(50.00, 50.80, 49.90, 50.40)),   # wick high 50.80
        (f"{DAY} 09:35", bar(50.40, 50.60, 49.70, 50.10)),   # wick low  49.70
        (f"{DAY} 09:40", bar(50.10, 50.50, 50.00, 50.45)),
        (f"{DAY} 09:45", bar(50.45, 51.00, 50.40, 50.95)),   # outside the range, excluded
    ]
    hi, lo, op = opening_range(frame_from_pairs(rows), pd.Timestamp(DAY), 15)
    assert (hi, lo, op) == pytest.approx((50.80, 49.70, 50.00))
    # body extremes would have been 50.45 / 50.10 - confirms we are on the wick


def test_premarket_rvol_compares_same_clock_window():
    rows = prior_premarket(PRIORS, per_bar_vol=10_000)     # 10 bars x 10k = 100k/session
    rows += [(f"{DAY} {h:02d}:{m:02d}", bar(10, 10.1, 9.9, 10.05, 50_000))
             for h in range(4, 9) for m in (0, 30)]        # 10 bars x 50k = 500k today
    r = premarket_rvol(frame_from_pairs(rows), pd.Timestamp(DAY), "04:00", "09:30", 20)
    assert r == pytest.approx(5.0)


def test_position_size_respects_risk_and_concentration():
    cfg = Config(account_risk_pct=0.01, max_position_pct=0.25)
    # $100k equity, 1% = $1,000 risk, $0.50/share -> 2,000 shares by risk
    # but 25% cap at $50/share -> 500 shares. Concentration wins.
    assert position_size(50.00, 49.50, 100_000, cfg) == 500
    # wider stop -> risk binds first
    assert position_size(50.00, 45.00, 100_000, cfg) == 200


# ---------------------------------------------------------------- end to end
def _scenario(day=DAY, up=True, catalyst_vol=50_000, or_rows=None, break_close=51.20):
    daily = daily_uptrend(n=210, seed=5)
    last = float(daily["close"].iloc[-1])
    daily = inject_bearish_fvg(daily, above=last)
    base = float(daily["close"].iloc[-1])

    rows = prior_premarket(PRIORS, per_bar_vol=10_000)
    rows += [(f"{day} {h:02d}:{m:02d}", bar(base, base * 1.001, base * 0.999, base, catalyst_vol))
             for h in range(4, 9) for m in (0, 30) if not (h == 9)]
    # 09:00-09:30 big-body confirmation
    px = base
    for t in ("09:00", "09:05", "09:10", "09:15", "09:20", "09:25"):
        b = big_body(px, 0.004, 0.95, up=up, vol=catalyst_vol)
        rows.append((f"{day} {t}", b)); px = b["close"]
    o = or_rows or [
        ("09:30", bar(px, px * 1.006, px * 0.996, px * 1.003)),
        ("09:35", bar(px * 1.003, px * 1.008, px * 0.998, px * 1.005)),
        ("09:40", bar(px * 1.005, px * 1.007, px * 1.001, px * 1.006)),
        ("09:45", bar(px * 1.006, px * 1.020, px * 1.005, px * 1.018)),  # breakout close
    ]
    rows += [(f"{day} {t}", b) for t, b in o]
    return frame_from_pairs(rows), daily, base


def test_full_pipeline_produces_a_valid_long_setup():
    intraday, daily, base = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
                 max_risk_per_share_pct=0.10)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Setup), getattr(v, "reason", None)
    assert v.side == "long"
    # entry is exactly 2 cents above the OR high wick
    assert v.entry == pytest.approx(round(v.trigger_level + 0.02, 2))
    # stop is the 09:30 open, below entry
    assert v.stop < v.entry
    assert v.risk_per_share == pytest.approx(round(v.entry - v.stop, 4))
    # 75/25 split reconstitutes
    assert v.qty_scale + v.qty_runner == v.qty
    assert v.qty_scale == round(v.qty * 0.75)
    assert v.reward_risk >= 0.5


@pytest.mark.parametrize("mutate,expected", [
    (dict(rvol_min=99.0),              "rvol_too_low"),
    (dict(body_pct_min=0.999),         "no_body_confirmation"),
    (dict(min_reward_risk=99.0),       "reward_risk_too_low"),
    (dict(max_risk_per_share_pct=0.0001), "stop_too_wide"),
    (dict(skip_if_market_cap_unknown=True), "market_cap_unknown"),
])
def test_each_gate_rejects_independently(mutate, expected):
    intraday, daily, _ = _scenario()
    base = dict(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
                max_risk_per_share_pct=0.10)
    cfg = Config(**{**base, **mutate})
    ctx = Context("TEST", market_cap=None if "skip_if_market_cap_unknown" in mutate else 800e6,
                  equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and v.reason == expected, v


def test_small_cap_is_rejected():
    intraday, daily, _ = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5)
    ctx = Context("TEST", market_cap=32_000_000, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and v.reason == "market_cap_too_small"


def test_premarket_down_against_daily_uptrend_is_rejected():
    intraday, daily, _ = _scenario(up=False)
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
                 max_risk_per_share_pct=0.10)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and v.reason == "htf_conflict"


def test_no_breakout_close_means_no_trade():
    intraday, daily, base = _scenario(or_rows=[
        ("09:30", bar(100, 100.6, 99.6, 100.3)),
        ("09:35", bar(100.3, 100.8, 99.9, 100.5)),
        ("09:40", bar(100.5, 100.7, 100.1, 100.6)),
        ("09:45", bar(100.6, 100.79, 100.2, 100.4)),   # pokes but never CLOSES above 100.80
    ])
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and v.reason == "no_breakout"


# ---------------------------------------------------------------- 09/20 clarifications
def test_near_miss_carries_its_setup_card():
    """R:R floor rejects the trade but must still hand back the full card."""
    intraday, daily, _ = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0,
                 max_risk_per_share_pct=0.10, min_reward_risk=99.0,
                 surface_near_misses=True, near_miss_floor=0.1)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and v.reason == "reward_risk_too_low"
    assert v.is_near_miss and v.setup is not None
    assert v.setup.entry > v.setup.stop and v.setup.qty >= 1


def test_deep_reject_carries_no_card():
    """Below near_miss_floor it is a plain no, not a greyed-out card."""
    intraday, daily, _ = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0,
                 max_risk_per_share_pct=0.10, min_reward_risk=99.0,
                 surface_near_misses=True, near_miss_floor=98.0)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    v = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(v, Rejection) and not v.is_near_miss and v.setup is None


def test_near_miss_floor_clamps_below_the_rr_floor():
    cfg = Config(min_reward_risk=0.5, near_miss_floor=0.75)
    assert cfg.near_miss_floor == 0.5


def test_runner_three_phases():
    """entry fill -> scale fill -> price trigger -> trailing stop."""
    from ordb.execution import Trade, State, trail_trigger_price
    intraday, daily, _ = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
                 max_risk_per_share_pct=0.10, runner_stop_mode="breakeven_then_trail",
                 runner_trail_trigger_r=0.5, runner_trail_pct=1.5)
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    s = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    assert isinstance(s, Setup) and s.qty_runner >= 1

    t = Trade(setup=s); t.entry_order_id = "e1"

    # phase 2: entry fills three cents worse than the limit
    slipped = s.entry + 0.03
    out = t.on_trade_update("fill", {"id": "e1", "filled_avg_price": str(slipped),
                                     "filled_qty": str(s.qty)}, cfg)
    assert t.state is State.FILLED and len(out) == 2
    oco = next(o for o in out if o.get("order_class") == "oco")
    assert oco["qty"] == str(s.qty_scale)
    assert oco["stop_loss"]["stop_price"] == f"{s.stop:.2f}"

    # phase 3: the 75% fills at the gap -> breakeven at the ACTUAL fill, not the limit
    t.scale_order_id = "s1"
    out = t.on_trade_update("fill", {"id": "s1", "filled_avg_price": str(s.target)}, cfg)
    assert t.state is State.SCALED
    assert out[0]["type"] == "stop"
    assert out[0]["stop_price"] == f"{slipped:.2f}"
    assert out[0]["qty"] == str(s.qty_runner)

    # phase 4: nothing happens until price clears the trigger
    trig = trail_trigger_price(s, cfg)
    assert trig == pytest.approx(s.target + s.risk_per_share * 0.5)
    assert t.on_price(trig - 0.01, cfg) == []
    assert t.state is State.SCALED

    out = t.on_price(trig + 0.01, cfg)
    assert t.state is State.TRAILING
    assert out[0]["type"] == "trailing_stop" and out[0]["trail_percent"] == "1.50"

    # and it only converts once
    assert t.on_price(trig + 5.0, cfg) == []


def test_runner_does_not_trail_in_plain_breakeven_mode():
    from ordb.execution import Trade, State
    intraday, daily, _ = _scenario()
    cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
                 max_risk_per_share_pct=0.10, runner_stop_mode="breakeven")
    ctx = Context("TEST", market_cap=800e6, equity=100_000)
    s = evaluate("TEST", intraday, daily, pd.Timestamp(DAY), ctx, cfg)
    t = Trade(setup=s, state=State.SCALED)
    assert t.on_price(s.target * 2, cfg) == []
