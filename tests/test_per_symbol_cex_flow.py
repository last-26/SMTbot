"""Tests for the per-symbol 1h CEX volume penalty (2026-04-22 gece, late).

Promoted from journal-only to runtime as part of the Arkham data-layer
integration push preceding the Pass 1 clean restart. Unlike stablecoins
— where INFLOW to CEX is bullish (cash arriving) — individual tokens
flowing INTO an exchange are BEARISH for that symbol (selling setup)
and OUT is BULLISH (cold/DEX accumulation). This test file locks that
inverted-for-tokens semantic.

Covers `src.strategy.entry_signals._per_symbol_cex_flow_penalty`.
"""

from __future__ import annotations

import pytest

from src.data.models import Direction
from src.strategy.entry_signals import _per_symbol_cex_flow_penalty


def test_none_input_returns_zero():
    """Missing snapshot → fail-open, zero penalty."""
    assert _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=None,
        noise_floor_usd=5_000_000.0,
        penalty=0.5,
    ) == 0.0


def test_zero_penalty_returns_zero_even_when_misaligned():
    """`penalty=0` short-circuits regardless of direction / magnitude."""
    assert _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+50_000_000.0,  # strong bearish-for-token
        noise_floor_usd=5_000_000.0,
        penalty=0.0,
    ) == 0.0


def test_below_noise_floor_returns_zero():
    """|netflow| < floor → treated as noise, no penalty regardless of sign."""
    # +$3M is BELOW $5M floor — even though signed bearish, no penalty.
    assert _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+3_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    ) == 0.0
    # -$3M same deal.
    assert _per_symbol_cex_flow_penalty(
        direction=Direction.BEARISH,
        symbol_netflow_1h_usd=-3_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    ) == 0.0


def test_long_with_token_inflow_is_misaligned():
    """Long trade + token flowing INTO exchange (bearish signal) → +penalty."""
    # +$10M inflow → bearish for token → misaligned against long.
    result = _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+10_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    assert result == pytest.approx(0.25)


def test_long_with_token_outflow_is_aligned():
    """Long trade + token flowing OUT (bullish signal) → aligned, no penalty."""
    result = _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=-10_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    assert result == 0.0


def test_short_with_token_outflow_is_misaligned():
    """Short trade + token flowing OUT (bullish signal) → +penalty."""
    result = _per_symbol_cex_flow_penalty(
        direction=Direction.BEARISH,
        symbol_netflow_1h_usd=-10_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    assert result == pytest.approx(0.25)


def test_short_with_token_inflow_is_aligned():
    """Short trade + token flowing INTO exchange (bearish signal) → aligned."""
    result = _per_symbol_cex_flow_penalty(
        direction=Direction.BEARISH,
        symbol_netflow_1h_usd=+10_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    assert result == 0.0


def test_penalty_is_constant_not_magnitude_scaled():
    """Unlike flow_alignment's |score|-scaled bump, per-symbol is a BINARY
    penalty — above-floor misalignment pays the full `penalty` regardless
    of signal strength (matches `_stablecoin_pulse_penalty` semantics)."""
    # Both $10M and $100M misalignment → same penalty.
    small = _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+10_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    large = _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+500_000_000.0,
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    assert small == large == pytest.approx(0.25)


def test_noise_floor_boundary_exact():
    """Exactly-at-floor values qualify as "above" (>= semantics)."""
    # |netflow| == floor → should be treated as signal (>=), not noise (<).
    result = _per_symbol_cex_flow_penalty(
        direction=Direction.BULLISH,
        symbol_netflow_1h_usd=+5_000_000.0,  # exactly at floor
        noise_floor_usd=5_000_000.0,
        penalty=0.25,
    )
    # The helper uses `abs(x) < floor` to filter → at-floor value passes through.
    assert result == pytest.approx(0.25)
