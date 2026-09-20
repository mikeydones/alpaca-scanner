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
tests/               18 tests, including exact entry/stop/target/size math
```

## Run

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q      # 18 passed
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

setups, rejects = scan(Alpaca(paper=True), caps=NasdaqScreenerProvider())
for s in setups:
    print(f"{s.symbol:<6} {s.side:<5} entry {s.entry}  stop {s.stop}  "
          f"target {s.target}  {s.reward_risk}R  {s.qty} sh  score {s.score}")
```

Nothing here submits an order on its own. `scan()` returns setups; you pass the ones
you want to `execution.entry_order()` and `client.submit_order()`.

## Before this is useful

Read SPEC.md — six open questions are listed at the bottom, and two of them
(FVG polarity, runner stop type) change what the engine actually does.

**Data plan:** this strategy needs pre-market SIP bars. The free IEX feed doesn't
have them. Algo Trader Plus ($99/mo) is a prerequisite, not an upgrade.
