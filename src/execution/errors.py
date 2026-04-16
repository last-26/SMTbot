"""Execution-layer exceptions.

We wrap the heterogenous errors the OKX SDK returns (dict responses with
code/msg, HTTP errors, network errors) into a small, typed hierarchy so
callers — the bot loop and the router itself — can react sensibly.
"""

from __future__ import annotations

from typing import Optional


class ExecutionError(Exception):
    """Base class for anything that goes wrong during order placement."""

    def __init__(self, message: str, code: Optional[str] = None, payload: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class OKXError(ExecutionError):
    """OKX API returned a non-zero `code` in its response envelope."""


class LeverageSetError(ExecutionError):
    """Setting leverage failed — abort placing the entry."""


class OrderRejected(ExecutionError):
    """Entry or algo order was rejected by the exchange."""


class InsufficientMargin(OrderRejected):
    """OKX margin check failed (code 51008 / 51020 etc.)."""


class AlgoOrderError(ExecutionError):
    """OCO SL/TP algo order failed after entry was filled.

    This is the most dangerous failure mode — position is OPEN with no
    protection. The router raises this and the caller must react.
    """
