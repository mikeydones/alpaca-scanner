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
                with a breakeven stop at the ACTUAL average fill
  4. TRAILING   price runs a further `runner_trail_trigger_r` R beyond the
                target -> breakeven stop is replaced by a trailing stop.
                Driven by on_price() off the bars websocket, not trade_updates.
  5. CLOSED     runner stopped out, or force-flat at 15:55 ET
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import Config, DEFAULT
from .rules import Setup


class State(str, Enum):
    ARMED = "armed"        # entry limit working
    FILLED = "filled"      # in position, 09:30 stop on both pieces
    SCALED = "scaled"      # 75% booked at the FVG, runner stop at breakeven
    TRAILING = "trailing"  # runner converted to a true trailing stop
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
    """Phase 3: fired once the 75% has filled at the gap.

    A static stop at the ACTUAL average fill - not at the planned entry. If you
    got filled at 112.81 on a 112.78 limit, breakeven is 112.81, and using the
    planned price would leave three cents of loss on the table.
    """
    if setup.qty_runner < 1:
        return None
    if cfg.runner_stop_mode == "trailing" and cfg.runner_trail_pct > 0:
        return runner_trail_order(setup, cfg)
    return {
        "symbol": setup.symbol, "qty": str(setup.qty_runner),
        "side": "sell" if setup.side == "long" else "buy",
        "type": "stop", "stop_price": f"{avg_entry:.2f}",
        "time_in_force": "day", "client_order_id": _tag(setup, "be"),
    }


def runner_trail_order(setup: Setup, cfg: Config = DEFAULT) -> Optional[dict]:
    """Phase 4: a true trailing stop, once the runner has proven itself."""
    if setup.qty_runner < 1:
        return None
    return {
        "symbol": setup.symbol, "qty": str(setup.qty_runner),
        "side": "sell" if setup.side == "long" else "buy",
        "type": "trailing_stop", "trail_percent": f"{cfg.runner_trail_pct:.2f}",
        "time_in_force": "day", "client_order_id": _tag(setup, "trail"),
    }


def trail_trigger_price(setup: Setup, cfg: Config = DEFAULT) -> float:
    """The price at which the breakeven stop converts to a trailing stop.

    `runner_trail_trigger_r` R beyond the target, measured in the same R units
    as the original risk. For a long: target + (0.5 x risk_per_share).
    """
    extra = setup.risk_per_share * cfg.runner_trail_trigger_r
    return setup.target + extra if setup.side == "long" else setup.target - extra


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
    runner_trailing: bool = False
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
                be_px = self.avg_entry or self.setup.entry
                self.log.append(f"scaled {self.setup.qty_scale} @ {fill_px:.2f} - "
                                f"runner stop to breakeven {be_px:.2f}")
                be = runner_breakeven_order(self.setup, be_px, cfg)
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

    def on_price(self, last: float, cfg: Config = DEFAULT) -> list[dict]:
        """Phase 4 trigger. Feed this the last trade or 1-min bar close.

        Only fires once, only after the scale-out, and only in
        breakeven_then_trail mode. Returns the trailing stop to submit (cancel
        the breakeven stop first).
        """
        if (self.state is not State.SCALED
                or self.runner_trailing
                or cfg.runner_stop_mode != "breakeven_then_trail"
                or self.setup.qty_runner < 1):
            return []
        trigger = trail_trigger_price(self.setup, cfg)
        reached = last >= trigger if self.setup.side == "long" else last <= trigger
        if not reached:
            return []
        self.runner_trailing = True
        self.state = State.TRAILING
        self.log.append(f"{last:.2f} cleared trail trigger {trigger:.2f} - "
                        f"converting runner to {cfg.runner_trail_pct:.2f}% trailing stop")
        o = runner_trail_order(self.setup, cfg)
        return [o] if o else []
