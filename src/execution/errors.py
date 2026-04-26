"""Execution-layer exceptions.

We wrap the heterogenous errors the Bybit SDK returns (dict responses
with retCode/retMsg, HTTP errors, network errors) into a small, typed
hierarchy so callers — the bot loop and the router itself — can react
sensibly.
"""

from __future__ import annotations

from typing import Optional


class ExecutionError(Exception):
    """Base class for anything that goes wrong during order placement."""

    def __init__(self, message: str, code: Optional[str] = None, payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class BybitError(ExecutionError):
    """Bybit API returned a non-zero `retCode` in its response envelope."""


class LeverageSetError(ExecutionError):
    """Setting leverage failed — abort placing the entry."""


class OrderRejected(ExecutionError):
    """Entry or position-attached TP/SL order was rejected by the exchange."""


class InsufficientMargin(OrderRejected):
    """Bybit margin check failed (retCode 110004 / 110007 / 110012)."""


class AlgoOrderError(ExecutionError):
    """TP/SL attachment failed after entry was filled.

    On Bybit V5 TP/SL is part of the position itself (set via
    /v5/position/trading-stop after a limit fills, or attached at
    create-order time for market entries). When this attachment fails
    the position is OPEN with no protection — the most dangerous
    failure mode. The router raises this and the caller must react.
    """
