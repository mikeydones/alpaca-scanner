"""FastAPI front end for the scanner. Render start command:

    uvicorn app:app --host 0.0.0.0 --port $PORT

Scans run on a background thread, never inside a request, so the dashboard
stays responsive while a pass is grinding through a few hundred symbols.
"""
from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse

from ordb.client import Alpaca
from ordb.config import Config, DEFAULT
from ordb.execution import entry_order
from ordb.providers import NasdaqScreenerProvider
from ordb.rules import Setup
from ordb.scanner import scan

ET = "America/New_York"
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
TOKEN = os.environ.get("SCAN_TOKEN")          # required for anything that places an order
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "false").lower() == "true"

app = FastAPI(title="ordb scanner")


# --------------------------------------------------------------------------
class ScanState:
    """Last completed scan plus whatever is running now."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.started: Optional[datetime] = None
        self.finished: Optional[datetime] = None
        self.setups: list[Setup] = []
        self.near: list[Any] = []
        self.rejects: list[Any] = []
        self.error: Optional[str] = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "started": self.started.isoformat() if self.started else None,
                "finished": self.finished.isoformat() if self.finished else None,
                "error": self.error,
                "paper": PAPER,
                "trading_enabled": TRADING_ENABLED,
                "setups": [s.to_dict() for s in self.setups],
                "near_misses": [
                    {**r.setup.to_dict(), "why": r.detail} for r in self.near if r.setup
                ],
                "reject_counts": _counts(self.rejects),
            }


STATE = ScanState()
_CAPS = NasdaqScreenerProvider()


def _counts(rejects: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rejects:
        out[r.reason] = out.get(r.reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _run_scan(symbols: Optional[list[str]], cfg: Config) -> None:
    try:
        api = Alpaca(paper=PAPER)
        setups, near, rejects = scan(api, cfg=cfg, caps=_CAPS, universe=symbols)
        with STATE.lock:
            STATE.setups, STATE.near, STATE.rejects, STATE.error = setups, near, rejects, None
    except Exception:
        with STATE.lock:
            STATE.error = traceback.format_exc(limit=3)
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    """Render's health check. Must not touch Alpaca - it runs before keys matter."""
    return {"ok": True, "paper": PAPER, "trading_enabled": TRADING_ENABLED}


@app.post("/api/scan")
def start_scan(symbols: Optional[str] = Query(None, description="comma separated override")) -> dict:
    with STATE.lock:
        if STATE.running:
            raise HTTPException(409, "a scan is already running")
        STATE.running = True
        STATE.started = datetime.now(timezone.utc)
    syms = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    threading.Thread(target=_run_scan, args=(syms, DEFAULT), daemon=True).start()
    return {"started": True}


@app.get("/api/setups")
def get_setups() -> dict:
    return STATE.snapshot()


@app.post("/api/order")
def place_order(symbol: str, x_scan_token: str = Header(default="")) -> JSONResponse:
    """Confirm-then-fire. Guarded three ways, because this endpoint spends money."""
    if not TRADING_ENABLED:
        raise HTTPException(403, "TRADING_ENABLED is not set")
    if not TOKEN or x_scan_token != TOKEN:
        raise HTTPException(401, "bad or missing X-Scan-Token")
    with STATE.lock:
        match = next((s for s in STATE.setups if s.symbol == symbol.upper()), None)
    if match is None:
        raise HTTPException(404, f"{symbol} is not in the current scan")
    resp = Alpaca(paper=PAPER).submit_order(entry_order(match))
    return JSONResponse({"submitted": resp.get("id"), "symbol": match.symbol,
                         "entry": match.entry, "qty": match.qty, "paper": PAPER})


# --------------------------------------------------------------------------
PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Scanner</title><style>
:root{--bg:#fbfbfa;--fg:#1a1a18;--mut:#6b6b66;--line:#e4e4e0;--card:#fff;
--up:#1a7f5a;--dn:#b3402f}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececea;--mut:#9a9a96;
--line:#2c2c32;--card:#1e1e24;--up:#4ec08d;--dn:#e08270}}
*{box-sizing:border-box}body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);
font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
.wrap{max-width:1100px;margin:0 auto}h1{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
button{font:inherit;padding:7px 14px;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:7px;cursor:pointer}button:hover{border-color:var(--mut)}
button[disabled]{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse;margin-top:14px;font-variant-numeric:tabular-nums}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
color:var(--mut);font-weight:600;padding:6px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid var(--line)}
tr.near{opacity:.52}.sym{font-weight:600}
.long{color:var(--up)}.short{color:var(--dn)}
.tag{font-size:11px;padding:1px 7px;border:1px solid var(--line);border-radius:99px;color:var(--mut)}
.note{color:var(--mut);font-size:12px}
.banner{padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);
font-size:13px;margin-bottom:14px}
.err{white-space:pre-wrap;font:12px ui-monospace,monospace;color:var(--dn)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
margin:26px 0 0;font-weight:600}
</style><div class=wrap>
<h1>Opening-range scanner</h1>
<div class=sub id=sub>idle</div>
<div class=banner id=mode></div>
<button id=go>Run scan</button>
<div id=out></div></div><script>
const $=s=>document.querySelector(s);
const f=(n,d=2)=>n==null?'—':Number(n).toFixed(d);
async function tick(){
  const r=await fetch('/api/setups');const d=await r.json();
  $('#mode').textContent=(d.paper?'PAPER account':'LIVE ACCOUNT')+
    ' · ordering '+(d.trading_enabled?'enabled':'disabled');
  $('#go').disabled=d.running;
  $('#sub').textContent=d.running?'scanning…':
    (d.finished?'last scan '+new Date(d.finished).toLocaleTimeString():'idle');
  let h='';
  if(d.error){h+='<div class="banner err">'+d.error+'</div>';}
  const rows=(a,cls)=>a.map(s=>`<tr class="${cls}">
    <td class=sym>${s.symbol}</td>
    <td class="${s.side}">${s.side}</td>
    <td>${f(s.entry)}</td><td>${f(s.stop)}</td><td>${f(s.target)}</td>
    <td>${f(s.reward_risk)}R</td><td>${s.qty}</td>
    <td>${s.qty_scale}/${s.qty_runner}</td>
    <td>${f(s.rvol,1)}x</td><td>${s.body_run}</td>
    <td class=note>${s.why||(s.fvg?s.fvg.kind+' gap':'2R fallback')}</td></tr>`).join('');
  const head=`<tr><th>sym<th>side<th>entry<th>stop<th>target<th>r:r<th>qty
    <th>75/25<th>rvol<th>run<th>target from</tr>`;
  if(d.setups.length)h+='<h2>Setups</h2><table>'+head+rows(d.setups,'')+'</table>';
  else if(!d.running)h+='<h2>Setups</h2><p class=note>Nothing cleared the rules.</p>';
  if(d.near_misses.length)h+='<h2>Near misses — under your R:R floor</h2><table>'
    +head+rows(d.near_misses,'near')+'</table>';
  const rc=Object.entries(d.reject_counts||{});
  if(rc.length)h+='<h2>Filtered out</h2><p>'+rc.map(([k,v])=>
    `<span class=tag>${k} ${v}</span>`).join(' ')+'</p>';
  $('#out').innerHTML=h;
}
$('#go').onclick=async()=>{await fetch('/api/scan',{method:'POST'});tick();};
tick();setInterval(tick,4000);
</script></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE
