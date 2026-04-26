"""Phase 4 — Bybit execution layer.

Public API:
  - BybitClient, BybitCredentials — typed wrapper over pybit
  - OrderRouter, RouterConfig — TradePlan → live orders
  - PositionMonitor — poll positions, emit CloseFill events
  - dry_run_report — build a fake ExecutionReport for paper trading
  - ExecutionReport, OrderResult, AlgoResult, PositionSnapshot, CloseFill
  - OrderStatus, PositionState
  - ExecutionError, BybitError, OrderRejected, InsufficientMargin,
    LeverageSetError, AlgoOrderError
"""

from src.execution.bybit_client import BybitClient, BybitCredentials
from src.execution.errors import (
    AlgoOrderError,
    BybitError,
    ExecutionError,
    InsufficientMargin,
    LeverageSetError,
    OrderRejected,
)
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    PositionState,
)
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.execution.position_monitor import PendingEvent, PositionMonitor

__all__ = [
    "AlgoOrderError",
    "AlgoResult",
    "BybitClient",
    "BybitCredentials",
    "BybitError",
    "CloseFill",
    "ExecutionError",
    "ExecutionReport",
    "InsufficientMargin",
    "LeverageSetError",
    "OrderRejected",
    "OrderResult",
    "OrderRouter",
    "OrderStatus",
    "PendingEvent",
    "PositionMonitor",
    "PositionSnapshot",
    "PositionState",
    "RouterConfig",
    "dry_run_report",
]
