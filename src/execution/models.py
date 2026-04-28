"""Execution-layer records.

These records cross the boundary from the Bybit API (untyped dicts) into
the bot's typed world. They're intentionally minimal — only what the
router, monitor, and journal need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # submitted, not yet filled
    FILLED = "FILLED"        # fully filled
    PARTIAL = "PARTIAL"      # partially filled
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class PositionState(str, Enum):
    PENDING = "PENDING"      # limit entry placed, not yet filled (Phase 7.C3)
    OPEN = "OPEN"            # entry filled, algo live
    CLOSED = "CLOSED"        # SL or TP hit, position flat
    UNPROTECTED = "UNPROTECTED"  # entry filled but algo failed — dangerous


@dataclass
class OrderResult:
    """Outcome of a single Bybit order placement call."""
    order_id: str
    client_order_id: str
    status: OrderStatus
    filled_sz: float = 0.0
    avg_price: float = 0.0
    raw: dict = field(default_factory=dict)
    submitted_at: datetime = field(default_factory=_utc_now)


@dataclass
class AlgoResult:
    """Outcome of a TP/SL placement. On Bybit V5, TP/SL is attached
    directly to the position rather than placed as a separate algo order;
    this record's `algo_id` is therefore an empty string for Bybit-era
    rows. Field name kept for journal back-compat with pre-migration rows.
    """
    algo_id: str
    client_algo_id: str
    sl_trigger_px: float
    tp_trigger_px: float
    raw: dict = field(default_factory=dict)


@dataclass
class ExecutionReport:
    """Everything a TradePlan produced on the exchange.

    `algos` is the canonical list of algo orders attached to the position —
    1 entry in single-TP mode, 2 entries in partial-TP mode (TP1/TP2).
    `algo` is kept as a convenience property that returns the first algo
    (back-compat for every caller written before Madde E).
    """
    entry: OrderResult
    algo: Optional[AlgoResult] = None
    state: PositionState = PositionState.OPEN
    leverage_set: bool = True
    plan_reason: str = ""
    algos: list[AlgoResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalize so both `algos` and `algo` are populated coherently.
        if not self.algos and self.algo is not None:
            self.algos = [self.algo]
        elif self.algos and self.algo is None:
            self.algo = self.algos[0]

    @property
    def is_protected(self) -> bool:
        return self.state == PositionState.OPEN and bool(self.algos)


@dataclass
class PositionSnapshot:
    """A single poll of an open position from Bybit."""
    inst_id: str
    pos_side: str                 # "long" / "short"
    size: float                   # contracts; 0 when closed
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    # 2026-04-28 — position-attached TP/SL (Bybit V5 returns these on the
    # `/v5/position/list` row). Used by the startup orphan-position
    # reconciliation pass to decide whether a synthetic-inserted DB row's
    # TP/SL must be re-attached or already covers the live position. 0.0
    # means "no leg attached" on the live position. Hot-poll consumers
    # (PositionMonitor.poll, runner snapshot writer) can ignore both.
    take_profit: float = 0.0
    stop_loss: float = 0.0
    sampled_at: datetime = field(default_factory=_utc_now)

    @property
    def is_closed(self) -> bool:
        return self.size == 0.0


@dataclass
class CloseFill:
    """Emitted by the monitor when a position transitions OPEN → CLOSED."""
    inst_id: str
    pos_side: str
    entry_price: float
    exit_price: float
    size: float
    pnl_usdt: float
    fee_usdt: float = 0.0
    closed_at: datetime = field(default_factory=_utc_now)
