# Opening Range / Daily-Bias Strategy — Formal Spec

Your nine steps, rewritten so a machine can execute them without guessing.
Everything below maps to a named parameter in `ordb/config.py`.

---

## Step 1 — Pre-market relative volume

**Your rule:** highest RVOL tickers in pre-market, watchlist sorted high→low.

**Formalized:** `RVOL = today's cumulative volume 04:00–09:30 ET ÷ mean of the same
clock window over the prior 20 sessions.`

Comparing the same *clock window* matters. If you scan at 09:10, today's partial
pre-market is measured against 04:00–09:10 on prior days, not against a full
session — otherwise every symbol looks like 0.05x all morning.

- `rvol_lookback_sessions = 20`
- `rvol_min = 3.0` ← **you didn't give a number.** You said "highest." A rank-only
  scan hands you the top 25 even on a dead day. The floor kills that.
- `watchlist_size = 25`

**Data:** `GET /v2/stocks/bars?timeframe=1Min&feed=sip`. Pre-market bars only exist
on SIP. On the free IEX feed, pre-market is nearly empty — this step cannot run.

---

## Step 2 — 09:00–09:30 body/wick confirmation

**Your rule:** 5-min candles, ≥85% body / ≤15% wick, at least one confirms;
consecutive ones confirm harder.

**Formalized:**
```
range    = high − low
body     = |close − open|
body_pct = body / range        # ≥ 0.85 qualifies
direction = sign(close − open)
```
`confirming_run()` returns the longest run of *consecutive same-direction*
qualifying candles. One passes the gate; the run length feeds the score.

> ⚠️ **Float gotcha, already fixed.** A candle that is arithmetically exactly 85%
> body evaluates to `0.8499999999999996` in float64 and would have been silently
> rejected. The comparison carries a `1e-9` epsilon.

- `body_pct_min = 0.85`, `min_confirming_candles = 1`

---

## Step 2b — Higher timeframe bias

**Your rule:** daily price above the 200 SMA and 20 EMA → bullish; inverse → bearish.

**Formalized:** `bias = long if close > SMA200 and close > EMA20; short if below
both; otherwise NO TRADE.` Between the two averages is a rejection, not a coin flip.

The pre-market direction from Step 2 must **match** the daily bias or the symbol is
dropped (`htf_conflict`). That's the "bigger confirmation" you described, made binding.

- `daily_sma_period = 200`, `daily_ema_period = 20`, `htf_lookback_days = 320`

> **4H note.** Alpaca serves `4Hour` bars, but they're aggregated from midnight UTC,
> so a "4H candle" straddles the RTH open and doesn't line up with what your chart
> platform draws. `require_4h_agreement` defaults **off** until we decide how to
> anchor it. Daily is doing the real work anyway.

---

## Step 3 — Catalyst and market cap

**Market cap > $50M** is a hard gate and **Alpaca cannot answer it.** No fundamentals
endpoint exists. `ordb/providers.py` has two drop-ins:

| Provider | Cost | Notes |
|---|---|---|
| `NasdaqScreenerProvider` | free | Daily CSV of every listed symbol's cap. Snapshot, not live. |
| `FMPProvider` | paid | Live cap **plus sector/industry**, which also covers your "what sector is this" step. |

`skip_if_market_cap_unknown = True` fails closed — an unknown cap is a skip, not a trade.

**Catalyst:** `GET /v1beta1/news?symbols=...` (Benzinga) gives headlines per symbol.
Surfaced on the setup card for you to read. Not a gate — `require_news_catalyst = False`,
because your rules say you *research* the catalyst, not that you require one.

**Twitter sympathy-play check is not automated.** That stays a human field on the card.

---

## Steps 4–5 — The 15-minute opening range

**Your rule:** wait 15 minutes after the open, then mark the level — **at the wick,
not the body.**

**Formalized:** over the three 5-min bars 09:30–09:45 ET:
```
OR_high    = max(high)     # wicks
OR_low     = min(low)      # wicks
RTH_open   = open of the 09:30 bar
```
There's a unit test that proves body extremes would have given different numbers.

---

## Step 6 — Entry

**Your rule:** if price closes above the 15-min opening range, limit order 2 cents
above the drawn level.

**Formalized:** a 5-min bar must **close** beyond the range (not just wick through it):
```
long:   any close > OR_high   →  entry = OR_high + 0.02
short:  any close < OR_low    →  entry = OR_low  − 0.02
```
`entry_offset = 0.02`. Order type `limit`, TIF `day`, `extended_hours: false`.

---

## Step 9 — Stop

**Your rule:** stop at the opening bell price point. `stop = RTH_open`.

> ⚠️ **This is the riskiest part of the spec.** Risk per share is whatever the
> distance from the OR breakout back to the 09:30 print happens to be — on a wide
> opening range that's several percent, and it varies enormously trade to trade.
> Two guards were added that you did not specify:
> - `max_risk_per_share_pct = 0.04` — reject if the stop is more than 4% away
> - `min_risk_per_share = 0.05` — reject stops so tight they're inside the spread
>
> Without these, position sizing produces either 12 shares or 9,000.

---

## Step 8 — Targets and the scale-out

**Your rule:** take 75% at the first fair value gap on the daily, trail the last 25%
to breakeven, close manually by 15:55 ET.

**FVG, formalized** (3-candle imbalance):
```
bullish FVG at i:  low[i]  > high[i−2]   →  zone (high[i−2], low[i])    # support below
bearish FVG at i:  high[i] < low[i−2]    →  zone (high[i],  low[i−2])   # supply above
```
A gap is "filled" once a later bar trades fully back through the zone; those are dropped.

> ❓ **Question for you.** For a **long**, the gap sitting *overhead* as a magnet is an
> unfilled **bearish** gap — untraded supply left on the way down. Unfilled *bullish*
> gaps sit *below* price and act as support, not as a target. I defaulted
> `fvg_polarity = "opposite"` (long → nearest bearish gap above). If you actually mean
> "nearest gap of any kind overhead," set it to `"any"`. **Which do you draw?**

- `fvg_target_edge = "near"` — first touch of the zone, not the midpoint or far side
- `fvg_min_gap_pct = 0.005` — ignore sub-0.5% gaps
- `fallback_target_r = 2.0` — when no qualifying gap exists (set `skip_if_no_fvg = True`
  to stand down instead)
- `min_reward_risk = 1.5` ← **not your rule**, kept by your call on 09/20, but it no
  longer hides anything. The floor is applied **last**, after the full setup is built,
  so a rejected trade still carries its complete card. `scan()` returns
  `(setups, near_misses, rejects)` and anything between `near_miss_floor` (0.75R) and
  the floor comes back as a near miss for the UI to grey out. Below 0.75R it's a plain no.

---

## The execution problem worth knowing about

**An Alpaca bracket order's take-profit and stop-loss cover the entire order quantity.**
There is no native "sell 75% here, trail the other 25%." Your Step 8 cannot be one submit.

The working design (`ordb/execution.py`):

```
1. ARMED    limit entry, full size, plain order — NOT order_class=bracket
2. FILLED   on the trade_updates fill event, submit two children:
              a. OCO on 75%  → limit @ FVG target / stop @ 09:30 open
              b. stop on 25% → stop @ 09:30 open
3. SCALED   when the 75% take-profit fills → cancel (b), resubmit the runner's
            stop at the actual average fill price
4. CLOSED   runner stops out, or market-on-close at 15:55 ET
```

Driven off `wss://paper-api.alpaca.markets/stream` →
`{"action":"listen","data":{"streams":["trade_updates"]}}`.

> ✅ **Resolved 09/20 — the runner gets both, in sequence.**
> `runner_stop_mode = "breakeven_then_trail"`:
>
> 1. On the scale-out, a **static stop at the actual average fill** — not the planned
>    entry. Filled at 112.81 on a 112.78 limit? Breakeven is 112.81. Using the planned
>    price would leave three cents of loss sitting there.
> 2. Once price runs a further `runner_trail_trigger_r` (0.5R) **past the target**, that
>    stop is replaced by a `runner_trail_pct` (1.5%) trailing stop.
>
> Phase 2 is driven by `Trade.on_price()` off the bars websocket, **not** `trade_updates` —
> no order event fires when price simply moves. It converts once and only once.

Also: the 75/25 split needs ≥4 shares to produce a runner. Below that the engine
exits in one piece and says so in the notes.

---

## What's still open

| # | Question | Blocks |
|---|---|---|
| 1 | RVOL floor — is 3.0x right, or do you want pure ranking? | Watchlist size |
| 2 | FVG polarity — bearish gaps overhead, or any gap? | Every target |
| 3 | Runner: static breakeven stop or true trailing stop? | Exit leg |
| 4 | Is a sub-1.5R setup still A+ to you? | Whether `min_reward_risk` stays |
| 5 | Short side: same rules inverted, or do you size/target shorts differently? | Short path |
| 6 | Max risk per share — is 4% of entry the right ceiling? | Rejection rate |
