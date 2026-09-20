"""Runs the engine on a constructed session so you can see the output shape."""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "tests"))
import pandas as pd
from ordb.config import Config
from ordb.rules import evaluate, Context
from ordb.execution import Trade, entry_order, scale_oco_order, runner_stop_order
from test_rules import _scenario, DAY

intraday, daily, _ = _scenario()
cfg = Config(skip_if_market_cap_unknown=False, rvol_min=3.0, min_reward_risk=0.5,
             max_risk_per_share_pct=0.10)
ctx = Context("DEMO", market_cap=820_000_000, equity=100_000)
s = evaluate("DEMO", intraday, daily, pd.Timestamp(DAY), ctx, cfg)

print("=== SETUP " + "=" * 58)
for k in ("symbol","side","trigger_level","entry","stop","target","risk_per_share",
          "reward_risk","qty","qty_scale","qty_runner","rvol","body_run","score"):
    print(f"  {k:<16} {getattr(s, k)}")
print("  notes:")
for n in s.notes: print("    -", n)
print("\n=== ORDERS " + "=" * 57)
print("1. entry   ", json.dumps(entry_order(s), separators=(",", ":")))
print("2. on fill ", json.dumps(scale_oco_order(s), separators=(",", ":")))
print("3. on fill ", json.dumps(runner_stop_order(s), separators=(",", ":")))

print("\n=== SIMULATED FILL " + "=" * 49)
t = Trade(setup=s); t.entry_order_id = "e1"
nxt = t.on_trade_update("fill", {"id": "e1", "filled_avg_price": str(s.entry),
                                 "filled_qty": str(s.qty)}, cfg)
print(f"  state={t.state.value}  next orders={[o['type'] + '/' + o.get('order_class','simple') for o in nxt]}")
t.scale_order_id = "s1"
nxt = t.on_trade_update("fill", {"id": "s1", "filled_avg_price": str(s.target)}, cfg)
print(f"  state={t.state.value}  next orders={[o['type'] + ' @ ' + o.get('stop_price','') for o in nxt]}")
for l in t.log: print("   ", l)
