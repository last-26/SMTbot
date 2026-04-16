"""Phase 4 — OKX execution layer.

Public API:
  - OKXClient, OKXCredentials — typed wrapper over python-okx
  - OrderRouter, RouterConfig — TradePlan → live orders
  - PositionMonitor — poll positions, emit CloseFill events
  - dry_run_report — build a fake ExecutionReport for paper trading
  - ExecutionReport, OrderResult, AlgoResult, PositionSnapshot, CloseFill
  - OrderStatus, PositionState
  - ExecutionError, OKXError, OrderRejected, InsufficientMargin,
    LeverageSetError, AlgoOrderError
"""

from src.execution.errors import (
    AlgoOrderError,
    ExecutionError,
    InsufficientMargin,
    LeverageSetError,
    OKXError,
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
from src.execution.okx_client import OKXClient, OKXCredentials
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.execution.position_monitor import PositionMonitor

__all__ = [
    "AlgoOrderError",
    "AlgoResult",
    "CloseFill",
    "ExecutionError",
    "ExecutionReport",
    "InsufficientMargin",
    "LeverageSetError",
    "OKXClient",
    "OKXCredentials",
    "OKXError",
    "OrderRejected",
    "OrderResult",
    "OrderRouter",
    "OrderStatus",
    "PositionMonitor",
    "PositionSnapshot",
    "PositionState",
    "RouterConfig",
    "dry_run_report",
]
