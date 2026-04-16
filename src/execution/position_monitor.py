"""Position polling → close-fill events.

The OrderRouter places orders and walks away. The PositionMonitor watches
OKX for the effect: entry fills, SL/TP hits, liquidations. It emits:

  - on open→close transition: a CloseFill record, which the caller feeds
    into RiskManager.register_trade_closed().

No websocket — we REST-poll because the bot loop is already polling the
TV MCP every N seconds. Adding a second concurrent connection isn't worth
the complexity for MVP demo flow.

Stateful: the monitor remembers the last snapshot of each (inst_id,
pos_side) key so it can detect the edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.execution.models import CloseFill, PositionSnapshot, PositionState
from src.execution.okx_client import OKXClient


@dataclass
class _Tracked:
    inst_id: str
    pos_side: str
    size: float
    entry_price: float


class PositionMonitor:
    """Tracks open positions and emits CloseFill events on closure."""

    def __init__(self, client: OKXClient):
        self.client = client
        self._tracked: dict[tuple[str, str], _Tracked] = {}

    # Called by the router after it places an order, so the monitor
    # "knows" to expect this position on the next poll.
    def register_open(self, inst_id: str, pos_side: str, size: float, entry_price: float) -> None:
        self._tracked[(inst_id, pos_side)] = _Tracked(
            inst_id=inst_id, pos_side=pos_side, size=size, entry_price=entry_price,
        )

    def poll(self, inst_id: Optional[str] = None) -> list[CloseFill]:
        """Pull current positions from OKX, emit fills for anything that closed."""
        live_snaps: dict[tuple[str, str], PositionSnapshot] = {}
        for snap in self.client.get_positions(inst_id=inst_id):
            if snap.size != 0.0:
                live_snaps[(snap.inst_id, snap.pos_side)] = snap

        fills: list[CloseFill] = []
        to_remove: list[tuple[str, str]] = []
        for key, tracked in self._tracked.items():
            if inst_id is not None and key[0] != inst_id:
                continue  # skip tracked positions on other instruments this poll
            if key not in live_snaps:
                # Tracked position no longer live → it closed.
                fills.append(self._close_fill_from(tracked))
                to_remove.append(key)
            else:
                # Still open — refresh our cached entry_price/size from the live row
                snap = live_snaps[key]
                tracked.size = snap.size
                if snap.entry_price > 0:
                    tracked.entry_price = snap.entry_price

        for key in to_remove:
            self._tracked.pop(key, None)

        return fills

    def _close_fill_from(self, t: _Tracked) -> CloseFill:
        # At close time, OKX has already removed the position row. Best we
        # can do without a trade-history lookup is mark exit_price=0 and
        # pnl_usdt=0 and let the caller enrich via `get_fills` or the
        # journal (Phase 5) when it comes online.
        return CloseFill(
            inst_id=t.inst_id,
            pos_side=t.pos_side,
            entry_price=t.entry_price,
            exit_price=0.0,
            size=t.size,
            pnl_usdt=0.0,
        )

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def state(self, inst_id: str, pos_side: str) -> PositionState:
        key = (inst_id, pos_side)
        return PositionState.OPEN if key in self._tracked else PositionState.CLOSED
