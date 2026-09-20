# ordb — opening-range / daily-bias scanner on Alpaca

A scanner and execution engine for the strategy written up in **[SPEC.md](SPEC.md)**.
The rules engine is a pure function, so it runs identically live and in backtest.

## Layout

```
ordb/config.py       every tunable number, marked MIKE (your rule) or ASSUMED (my default)
ordb/indicators.py   candle anatomy, SMA/EMA/ATR, fair value gaps, session slicing, RVOL
ordb/rules.py        evaluate() -> Setup | Rejection   <- the strategy
ordb/execution.py    order payloads + the fill-driven 75/25 exit state machine
ordb/client.py       Alpaca REST (assets, bars, snapshots, screener, news, orders)
ordb/providers.py    market cap, which Alpaca does not serve
ordb/scanner.py      universe -> prefilter -> bars -> evaluate -> ranked
app.py               FastAPI dashboard + JSON API (this is what Render serves)
render.yaml          Render blueprint - start command, health check, env vars
compare_polarity.py  runs both FVG rules on the same sessions and diffs the targets
tests/               23 tests, including exact entry/stop/target/size math
```

## Run

Python 3.9+. The codebase deliberately avoids `X | Y` type syntax at runtime so it
works on the python3 macOS ships.

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q      # 30 passed
uvicorn app:app --reload         # dashboard at http://127.0.0.1:8000
python3 demo.py                  # end-to-end on a constructed session
```

## Live

```bash
export ALPACA_KEY_ID=...         # paper keys
export ALPACA_SECRET_KEY=...
```

```python
from ordb.client import Alpaca
from ordb.scanner import scan
from ordb.providers import NasdaqScreenerProvider

setups, near_misses, rejects = scan(Alpaca(paper=True), caps=NasdaqScreenerProvider())

for s in setups:
    print(f"{s.symbol:<6} {s.side:<5} entry {s.entry}  stop {s.stop}  "
          f"target {s.target}  {s.reward_risk}R  {s.qty} sh  score {s.score}")

for r in near_misses:                      # passed every rule, under the R:R floor
    print(f"{r.setup.symbol:<6} near miss  {r.setup.reward_risk}R  {r.detail}")
```

Nothing here submits an order on its own. `scan()` returns setups; you pass the ones
you want to `execution.entry_order()` and `client.submit_order()`.

## Before this is useful

Read SPEC.md. The runner stop and the R:R floor are settled; FVG polarity is being
measured rather than guessed — run `compare_polarity.py` against your own symbols and
decide from the rows where the two rules disagree.

**Data plan:** this strategy needs pre-market SIP bars. The free IEX feed doesn't
have them. Algo Trader Plus ($99/mo) is a prerequisite, not an upgrade.


## Deploying to Render

**Start command:**

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

**Build command:** `pip install -r requirements.txt`
**Health check path:** `/health`

`render.yaml` has all of it as a blueprint if you'd rather point Render at the repo
than fill the form.

### Environment variables

| Key | Value | Why |
|---|---|---|
| `ALPACA_KEY_ID` | your paper key | |
| `ALPACA_SECRET_KEY` | your paper secret | |
| `ALPACA_PAPER` | `true` | `false` switches to the live trading host |
| `TRADING_ENABLED` | `false` | must be `true` before `/api/order` does anything |
| `SCAN_TOKEN` | a long random string | required in `X-Scan-Token` to place an order |
| `PYTHON_VERSION` | `3.11.9` | Render defaults to an older interpreter |

### Things to know before you rely on it

**The free plan spins down after ~15 minutes idle,** and a cold start takes 30–60s.
Your whole strategy keys off 09:00–09:45 ET. A sleeping service is the one failure mode
that costs you the trade. Either pay for an always-on instance or accept that you're
waking it manually before the bell.

**`/api/order` is off by default and guarded three ways** — `TRADING_ENABLED`, a
`SCAN_TOKEN` header, and paper mode. A Render URL is public. Anyone who finds it and
knows the path could otherwise submit orders against your account.

**Scans run on a background thread**, not inside the request, so a pass over a few
hundred symbols won't hit Render's request timeout. `POST /api/scan` returns
immediately; the dashboard polls `/api/setups` every 4 seconds.

**A web service does not run anything on a schedule.** Nothing scans at 09:00 unless
something pokes it. Add a Render Cron Job hitting `POST /api/scan`, or run the scanner
as a Background Worker instead — see the open question in SPEC.md.
