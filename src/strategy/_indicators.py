"""Shared numeric helpers for strategy modules.

Extracted from entry_signals and setup_planner — both carried identical
`_ema` (SMA-seeded) implementations. One canonical home prevents drift if
the formula ever needs tightening (e.g. Wilder smoothing variant).
"""

from __future__ import annotations

from typing import Optional


def ema(values: list[float], period: int) -> Optional[float]:
    """EMA of `values` with SMA seed. Returns None when series is shorter
    than `period` (not enough samples to seed a stable EMA)."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1.0 - k)
    return result
