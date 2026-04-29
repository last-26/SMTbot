"""Unit tests for `src/strategy/what_if_sltp.py` (Pass 2.5).

The pure helper is shared by the live reject path
(`BotRunner._compute_what_if_proposed_sltp`) and the backfill script
(`scripts/backfill_proposed_sl_tp.py`). Tests here pin the math
contract directly — full path-coverage (NO_PROPOSED_SLTP_REASONS,
UNDEFINED direction, missing inputs) without standing up a runner /
journal.
"""
from __future__ import annotations

import math

import pytest

from src.data.models import Direction
from src.strategy.what_if_sltp import (
    NO_PROPOSED_SLTP_REASONS,
    compute_what_if_proposed_sltp,
)


# ── ATR-dominant case (atr × 1.5 > price × floor_pct) ───────────────────────


def test_long_atr_dominant_returns_atr_based_sltp():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0,
        atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.001,  # 67 → atr×1.5=750 wins
        target_rr=1.5,
    )
    # sl_distance = max(750, 67) = 750
    assert sl == pytest.approx(67_000.0 - 750.0)
    assert tp == pytest.approx(67_000.0 + 750.0 * 1.5)
    assert rr == pytest.approx(1.5)


def test_short_atr_dominant_mirrors_long():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BEARISH,
        price=67_000.0, atr=500.0,
        reject_reason="ema_momentum_contra",
        floor_pct=0.001, target_rr=1.5,
    )
    assert sl == pytest.approx(67_000.0 + 750.0)
    assert tp == pytest.approx(67_000.0 - 750.0 * 1.5)
    assert rr == pytest.approx(1.5)


# ── Floor-dominant case ─────────────────────────────────────────────────────


def test_long_floor_dominant_returns_floor_based_sltp():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0,
        atr=10.0,  # atr×1.5=15
        reject_reason="below_confluence",
        floor_pct=0.005,  # 67000*0.005=335 wins
        target_rr=1.5,
    )
    assert sl == pytest.approx(67_000.0 - 335.0)
    assert tp == pytest.approx(67_000.0 + 335.0 * 1.5)


def test_target_rr_zero_falls_back_to_1_5():
    """Live config can have target_rr_ratio=0.0 (legacy heatmap behavior).
    Helper falls back to 1.5 so peg targets are always non-trivial."""
    _sl, _tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.001, target_rr=0.0,
    )
    assert rr == pytest.approx(1.5)


def test_target_rr_none_falls_back_to_1_5():
    _sl, _tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.001, target_rr=None,
    )
    assert rr == pytest.approx(1.5)


def test_target_rr_custom_value_used():
    _sl, _tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.001, target_rr=2.0,
    )
    assert rr == pytest.approx(2.0)


# ── NULL-returning short-circuit cases ──────────────────────────────────────


@pytest.mark.parametrize("reason", sorted(NO_PROPOSED_SLTP_REASONS))
def test_no_proposed_for_short_circuit_reasons(reason: str):
    """Every reason in the skip set must yield (None, None, None)."""
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=500.0,
        reject_reason=reason,
        floor_pct=0.005, target_rr=1.5,
    )
    assert sl is None and tp is None and rr is None


def test_undefined_direction_returns_none():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.UNDEFINED,
        price=67_000.0, atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.005, target_rr=1.5,
    )
    assert sl is None and tp is None and rr is None


def test_missing_price_returns_none():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=None, atr=500.0,
        reject_reason="below_confluence",
        floor_pct=0.005, target_rr=1.5,
    )
    assert sl is None and tp is None and rr is None


def test_missing_atr_returns_none():
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=None,
        reject_reason="below_confluence",
        floor_pct=0.005, target_rr=1.5,
    )
    assert sl is None and tp is None and rr is None


def test_zero_atr_returns_none():
    """ATR exactly 0 short-circuits (no scale to derive distance from)."""
    sl, tp, rr = compute_what_if_proposed_sltp(
        symbol="BTC-USDT-SWAP",
        direction=Direction.BULLISH,
        price=67_000.0, atr=0.0,
        reject_reason="below_confluence",
        floor_pct=0.005, target_rr=1.5,
    )
    assert sl is None and tp is None and rr is None


# ── Pegger-side invariant: rr is always finite & positive when set ──────────


def test_returned_rr_always_positive_when_set():
    for direction in (Direction.BULLISH, Direction.BEARISH):
        for atr in (500.0, 10.0):
            for floor_pct in (0.001, 0.005):
                _sl, _tp, rr = compute_what_if_proposed_sltp(
                    symbol="BTC-USDT-SWAP",
                    direction=direction,
                    price=67_000.0, atr=atr,
                    reject_reason="below_confluence",
                    floor_pct=floor_pct, target_rr=1.5,
                )
                assert rr is not None
                assert rr > 0
                assert math.isfinite(rr)
