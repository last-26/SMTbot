"""Position polling → close-fill events.

The OrderRouter places orders and walks away. The PositionMonitor watches
OKX for the effect: entry fills, SL/TP hits, liquidations. It emits:

  - on open→close transition: a CloseFill record, which the caller feeds
    into RiskManager.register_trade_closed().
  - on partial TP1 fill (size shrinks but not to zero), it cancels the
    remaining TP2 algo and places a new SL-at-breakeven OCO for the
    surviving contracts (Madde E).

No websocket — we REST-poll because the bot loop is already polling the
TV MCP every N seconds. Adding a second concurrent connection isn't worth
the complexity for MVP demo flow.

Stateful: the monitor remembers the last snapshot of each (inst_id,
pos_side) key so it can detect the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from loguru import logger

from src.execution.models import CloseFill, PositionSnapshot, PositionState
from src.execution.okx_client import OKXClient


@dataclass
class _Tracked:
    inst_id: str
    pos_side: str
    size: float
    entry_price: float
    initial_size: float = 0.0
    algo_ids: list[str] = field(default_factory=list)
    tp2_price: Optional[float] = None
    be_already_moved: bool = False


class PositionMonitor:
    """Tracks open positions and emits CloseFill events on closure."""

    def __init__(
        self,
        client: OKXClient,
        *,
        margin_mode: str = "isolated",
        move_sl_to_be_enabled: bool = False,
        on_sl_moved: Optional[Callable[[str, str, list[str]], None]] = None,
    ):
        self.client = client
        self.margin_mode = margin_mode
        self.move_sl_to_be_enabled = move_sl_to_be_enabled
        self._on_sl_moved = on_sl_moved
        self._tracked: dict[tuple[str, str], _Tracked] = {}

    # Called by the router after it places an order, so the monitor
    # "knows" to expect this position on the next poll.
    def register_open(
        self,
        inst_id: str,
        pos_side: str,
        size: float,
        entry_price: float,
        *,
        algo_ids: Optional[list[str]] = None,
        tp2_price: Optional[float] = None,
    ) -> None:
        self._tracked[(inst_id, pos_side)] = _Tracked(
            inst_id=inst_id, pos_side=pos_side, size=size, entry_price=entry_price,
            initial_size=size, algo_ids=list(algo_ids or []), tp2_price=tp2_price,
        )

    def poll(self, inst_id: Optional[str] = None) -> list[CloseFill]:
        """Pull current positions from OKX, emit fills for anything that closed.

        As a side-effect on the way, if a partial TP1 fill is detected and
        `move_sl_to_be_enabled`, cancel the remaining TP2 algo and place a
        new SL-at-breakeven OCO for the surviving contracts.
        """
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
                snap = live_snaps[key]
                self._detect_tp1_and_move_sl(tracked, snap)
                # Refresh cached entry_price/size from the live row
                tracked.size = snap.size
                if snap.entry_price > 0:
                    tracked.entry_price = snap.entry_price

        for key in to_remove:
            self._tracked.pop(key, None)

        return fills

    def _detect_tp1_and_move_sl(
        self, t: _Tracked, snap: PositionSnapshot,
    ) -> None:
        """If the live size shrank (TP1 fill) and we haven't moved SL yet,
        cancel TP2 and replace it with SL=entry on the remaining size.
        Failures are logged and the `be_already_moved` flag stays False so
        the next poll retries."""
        if not self.move_sl_to_be_enabled:
            return
        if t.be_already_moved:
            return
        if t.initial_size <= 0 or len(t.algo_ids) < 2:
            return
        # Only react when size strictly shrank AND something is still open.
        if snap.size >= t.initial_size or snap.size <= 0:
            return
        if t.tp2_price is None:
            return

        tp2_algo_id = t.algo_ids[1]
        try:
            self.client.cancel_algo(t.inst_id, tp2_algo_id)
            new_algo = self.client.place_oco_algo(
                inst_id=t.inst_id, pos_side=t.pos_side,
                size_contracts=int(snap.size),
                sl_trigger_px=t.entry_price,
                tp_trigger_px=t.tp2_price,
                td_mode=self.margin_mode,
            )
        except Exception:
            logger.exception(
                "sl_to_be_retry_next_poll inst={} side={}",
                t.inst_id, t.pos_side,
            )
            return

        t.algo_ids = [t.algo_ids[0], new_algo.algo_id]
        t.be_already_moved = True
        logger.info(
            "sl_moved_to_be_via_replace inst={} side={} remaining_size={} new_algo={}",
            t.inst_id, t.pos_side, snap.size, new_algo.algo_id,
        )
        if self._on_sl_moved is not None:
            try:
                self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
            except Exception:
                logger.exception("on_sl_moved_callback_failed")

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
