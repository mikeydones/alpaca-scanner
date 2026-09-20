"""
Every tunable number in the strategy lives here.

Values marked  # MIKE  came directly from your written rules.
Values marked  # ASSUMED  are my defaults for things your rules did not pin down.
Change them, don't hunt through the logic.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Config:
    # ---------- Session clock (all US/Eastern) ----------
    premarket_open: str = "04:00"          # ASSUMED  - start of RVOL accumulation window
    momentum_window_start: str = "09:00"   # MIKE     - "9:00 AM EST to 9:30 AM EST"
    momentum_window_end: str = "09:30"     # MIKE
    rth_open: str = "09:30"                # MIKE     - opening bell
    opening_range_minutes: int = 15        # MIKE     - "wait at least 15 minutes"
    force_flat_at: str = "15:55"           # MIKE     - "if it doesn't trigger by 3:55 PM EST"
    bar_minutes: int = 5                   # MIKE     - "I will look at a 5 minute chart"

    # ---------- Step 1: relative volume ----------
    rvol_lookback_sessions: int = 20       # ASSUMED  - baseline = mean of prior N same-window volumes
    rvol_min: float = 3.0                  # ASSUMED  - you said "highest", not a threshold. This cuts the tail.
    watchlist_size: int = 25               # ASSUMED  - how many survive the RVOL sort

    # ---------- Step 2: candle body confirmation ----------
    body_pct_min: float = 0.85             # MIKE     - "85% body and no more than 15% wick"
    min_confirming_candles: int = 1        # MIKE     - "At least one of 5 minute candles"
    # Consecutive same-direction big-body candles raise the score, they are not a gate.

    # ---------- Step 2b: higher timeframe trend ----------
    daily_sma_period: int = 200            # MIKE     - "stay above the 200 SMA"
    daily_ema_period: int = 20             # MIKE     - "and the 20 EMA"
    require_4h_agreement: bool = False     # ASSUMED  - see SPEC.md note on 4H bar alignment
    htf_lookback_days: int = 320           # ASSUMED  - enough history to seed a 200 SMA

    # ---------- Step 3: tradability gates ----------
    min_market_cap: float = 50_000_000     # MIKE     - "under $50 million ... I will not trade it"
    skip_if_market_cap_unknown: bool = True  # ASSUMED - fail closed. Alpaca does not serve market cap.
    min_price: float = 1.00                # ASSUMED
    max_price: float = 500.00              # ASSUMED
    require_news_catalyst: bool = False    # MIKE researches a catalyst but does not require one to trade.

    # ---------- Steps 5-6: entry ----------
    entry_offset: float = 0.02             # MIKE     - "2 cents above the price level"
    # MIKE: levels are drawn at the WICK, not the body.
    level_from_wick: bool = True           # MIKE

    # ---------- Step 9: stop ----------
    stop_basis: str = "rth_open"           # MIKE     - "right at the opening bell price point"
    max_risk_per_share_pct: float = 0.04   # ASSUMED  - reject if stop distance > 4% of entry
    min_risk_per_share: float = 0.05       # ASSUMED  - reject knife-edge stops that are really noise

    # ---------- Step 8: targets ----------
    fvg_timeframe: str = "1Day"            # MIKE     - "fair value gap that I draw out on the daily"
    fvg_lookback_bars: int = 120           # ASSUMED
    fvg_min_gap_pct: float = 0.005         # ASSUMED  - ignore sub-0.5% gaps, they are noise
    fvg_polarity: str = "opposite"         # ASSUMED  - see SPEC.md: for a long, target the unfilled
                                           #            BEARISH gap overhead. "any" to relax.
    fvg_target_edge: str = "near"          # ASSUMED  - near | mid | far edge of the gap zone
    fallback_target_r: float = 2.0         # ASSUMED  - used when no qualifying FVG exists
    skip_if_no_fvg: bool = False           # ASSUMED  - True = only trade when a real gap is overhead
    min_reward_risk: float = 1.5           # ASSUMED  - R:R floor. Not in your rules; see SPEC.md.
    surface_near_misses: bool = True       # MIKE (clarified 09/20) - keep the floor, but show
                                           # what it threw away instead of silently dropping it
    near_miss_floor: float = 0.75          # ASSUMED  - below this it is not a near miss, it is a no

    # ---------- Step 8: scale out ----------
    scale_out_pct: float = 0.75            # MIKE     - "take 75% profit at the first value gap"
    runner_stop_mode: str = "breakeven_then_trail"   # MIKE (clarified 09/20)
    # Three-phase runner:
    #   1. entry fills            -> runner carries the 09:30 stop
    #   2. the 75% fills at target-> runner stop moves to the actual average fill (breakeven)
    #   3. price runs another `runner_trail_trigger_r` R past the target
    #                             -> breakeven stop is replaced by a true trailing stop
    runner_trail_trigger_r: float = 0.5    # ASSUMED  - extra R beyond target before converting
    runner_trail_pct: float = 1.5          # ASSUMED  - trail distance once converted, percent

    # ---------- Sizing ----------
    account_risk_pct: float = 0.01         # ASSUMED  - 1% of equity per trade
    max_position_pct: float = 0.25         # ASSUMED  - no single position over 25% of equity
    max_concurrent_positions: int = 3      # ASSUMED

    # ---------- Data ----------
    feed: str = "sip"                      # IEX pre-market is too thin for any of this. See SPEC.md.
    allow_short: bool = True               # MIKE     - "If it's bearish, it's inverse"

    def __post_init__(self) -> None:
        assert 0 < self.body_pct_min <= 1
        assert 0 < self.scale_out_pct < 1
        assert self.fvg_polarity in ("opposite", "same", "any")
        assert self.fvg_target_edge in ("near", "mid", "far")
        assert self.runner_stop_mode in ("breakeven", "trailing", "breakeven_then_trail")
        # A near-miss band only makes sense below the floor. Clamp rather than
        # assert: lowering min_reward_risk should not blow up construction.
        self.near_miss_floor = min(self.near_miss_floor, self.min_reward_risk)


DEFAULT = Config()
