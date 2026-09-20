"""Order construction and the fill-driven exit state machine.

THE CONSTRAINT: an Alpaca bracket order's take_profit and stop_loss cover the
ENTIRE order quantity. There is no native "sell 75% here, trail the rest".
So the 75/25 scale-out cannot be one submit - it has to be driven off the
trade_updates websocket. This module builds the payloads; runner.py drives them.

Lifecycle
---------
  1. ARMED      entry limit order working              POST /v2/orders (simple limit)
  2. FILLED     entry fills -> immediately submit:
                  a. OCO on 75%: limit @ FVG target / stop @ 09:30 open
                  b. plain stop on 25% @ 09:30 open
  3. SCALED     the 75% take-profit fills -> replace the runner's stop
                with a breakeven stop (or a trailing stop if configured)
  4. CLOSED     runner stopped out, or force-flat at 15:55 ET
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import Config, DEFAULT
from .rules import Setup


class State(str, Enum):
    ARMED = "armed"
    FILLED = "filled"
    SCALED = "scaled"
    CLOSED = "closed"
    CANCELED = "canceled"


def _tag(setup: Setup, leg: str) -> str:
    return f"ordb-{setup.symbol}-{leg}-{uuid.uuid4().hex[:8]}"[:128]


def entry_order(setup: Setup, cfg: Config = DEFAULT) -> dict:
    """Step 6: limit 2c beyond the level, day order, full size.

    Deliberately NOT order_class=bracket - see the module docstring.
    """
    return {
        "symbol": setup.symbol,
        "qty": str(setup.qty),
        "side": "buy" if setup.side == "long" else "sell",
        "type": "limit",
        "limit_price": f"{setup.entry:.2f}",
        "time_in_force": "day",
        "extended_hours": False,
        "client_order_id": _tag(setup, "entry"),
    }


def scale_oco_order(setup: Setup, cfg: Config = DEFAULT) -> Optional[dict]:
    """Step 8.1: the 75% exits at the fair value gap, protected by the 09:30 stop."""
    if setup.qty_scale < 1:
        return None
    return {
        "symbol": setup.symbol,
        "qty": str(setup.qty_scale),
        "side": "sell" if setup.side == "long" else "buy",
        "type": "limit",
        "time_in_force": "day",
        "order_class": "oco",
        "take_profit": {"limit_price": f"{setup.target:.2f}"},
        "stop_loss": {"stop_price": f"{setup.stop:.2f}"},
        "client_order_id": _tag(setup, "scale"),
    }


def runner_stop_order(setup: Setup, cfg: Config = DEFAULT) -> Optional[dict]:
    """The 25% carries only a stop until the scale-out fills."""
    if setup.qty_runner < 1:
        return None
    return {
        "symbol": setup.symbol,
        "qty": str(setup.qty_runner),
        "side": "sell" if setup.side == "long" else "buy",
        "type": "stop",
        "stop_price": f"{setup.stop:.2f}",
        "time_in_force": "day",
        "client_order_id": _tag(setup, "runner"),
    }


def runner_breakeven_order(setup: Setup, avg_entry: float, cfg: Config = DEFAULT) -> Optional[dict]:
    """Step 8.2, fired once the 75% has filled.

    Mike wrote "trailing stop loss to break even price", which is two different
    instruments. runner_stop_mode picks one:
      breakeven  - a static stop at the actual average fill price (the literal
                   reading, and what actually guarantees a scratch)
      trailing   - a real trailing stop runner_trail_pct behind the high
    """
    if setup.qty_runner < 1:
        return None
    side = "sell" if setup.side == "long" else "buy"
    if cfg.runner_stop_mode == "trailing" and cfg.runner_trail_pct > 0:
        return {
            "symbol": setup.symbol, "qty": str(setup.qty_runner), "side": side,
            "type": "trailing_stop", "trail_percent": f"{cfg.runner_trail_pct:.2f}",
            "time_in_force": "day", "client_order_id": _tag(setup, "trail"),
        }
    return {
        "symbol": setup.symbol, "qty": str(setup.qty_runner), "side": side,
        "type": "stop", "stop_price": f"{avg_entry:.2f}",
        "time_in_force": "day", "client_order_id": _tag(setup, "be"),
    }


def force_flat_order(symbol: str, qty: int, side: str) -> dict:
    """Step 8.2 tail: 'you must manually close out the remaining 25%' at 15:55 ET."""
    return {
        "symbol": symbol, "qty": str(qty),
        "side": "sell" if side == "long" else "buy",
        "type": "market", "time_in_force": "day",
        "client_order_id": f"ordb-{symbol}-flat-{uuid.uuid4().hex[:8]}",
    }


# --------------------------------------------------------------------------
@dataclass
class Trade:
    """One live setup and the orders attached to it."""
    setup: Setup
    state: State = State.ARMED
    entry_order_id: Optional[str] = None
    scale_order_id: Optional[str] = None
    runner_order_id: Optional[str] = None
    avg_entry: Optional[float] = None
    filled_qty: int = 0
    log: list[str] = field(default_factory=list)

    def on_trade_update(self, event: str, order: dict, cfg: Config = DEFAULT) -> list[dict]:
        """Consume one trade_updates message. Returns orders to submit next.

        Wire this to  wss://paper-api.alpaca.markets/stream  ->
        {"action":"listen","data":{"streams":["trade_updates"]}}
        """
        oid = order.get("id")
        to_submit: list[dict] = []

        if event == "fill" and oid == self.entry_order_id:
            self.avg_entry = float(order.get("filled_avg_price") or self.setup.entry)
            self.filled_qty = int(float(order.get("filled_qty") or self.setup.qty))
            self.state = State.FILLED
            self.log.append(f"entry filled {self.filled_qty} @ {self.avg_entry:.2f}")
            for o in (scale_oco_order(self.setup, cfg), runner_stop_order(self.setup, cfg)):
                if o:
                    to_submit.append(o)

        elif event == "fill" and oid == self.scale_order_id:
            fill_px = float(order.get("filled_avg_price") or self.setup.target)
            hit_target = (
                fill_px >= self.setup.target - 0.01 if self.setup.side == "long"
                else fill_px <= self.setup.target + 0.01
            )
            if hit_target and self.setup.qty_runner >= 1:
                self.state = State.SCALED
                self.log.append(f"scaled {self.setup.qty_scale} @ {fill_px:.2f} - moving runner to breakeven")
                be = runner_breakeven_order(self.setup, self.avg_entry or self.setup.entry, cfg)
                if be:
                    to_submit.append(be)   # cancel runner_order_id first
            else:
                self.state = State.CLOSED
                self.log.append(f"stopped out @ {fill_px:.2f}")

        elif event == "fill" and oid == self.runner_order_id:
            self.state = State.CLOSED
            self.log.append(f"runner closed @ {order.get('filled_avg_price')}")

        elif event in ("canceled", "expired", "rejected") and oid == self.entry_order_id:
            self.state = State.CANCELED
            self.log.append(f"entry {event}")

        return to_submit
