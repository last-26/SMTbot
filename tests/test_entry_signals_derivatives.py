"""Unit tests for Phase 1.5 Madde 6 — derivatives in the entry pipeline.

Covers:
  * 3 new confluence factors (`derivatives_contrarian`,
    `derivatives_capitulation`, `derivatives_heatmap_target`)
  * crowded-skip gate in `build_trade_plan_from_state`
  * one-slot-per-cycle rule (elif chain)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from src.analysis.multi_timeframe import score_direction
from src.data.models import (
    Direction,
    MarketState,
    OscillatorTableData,
    Session,
    SignalTableData,
)
from src.strategy.entry_signals import (
    _should_skip_for_derivatives,
    build_trade_plan_from_state,
)


@dataclass
class FakeDeriv:
    regime: str = "BALANCED"
    liq_imbalance_1h: float = 0.0
    funding_rate_zscore_30d: float = 0.0


@dataclass
class FakeCluster:
    price: float
    notional_usd: float
    side: str = "SHORT_LIQ"


@dataclass
class FakeHeatmap:
    nearest_above: Optional[FakeCluster] = None
    nearest_below: Optional[FakeCluster] = None
    largest_above_notional: float = 0.0
    largest_below_notional: float = 0.0
    clusters_above: list = field(default_factory=list)
    clusters_below: list = field(default_factory=list)


def _base_state(price: float = 100.0, atr: float = 1.0) -> MarketState:
    st = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="3m",
        signal_table=SignalTableData(price=price, atr_14=atr),
        oscillator=OscillatorTableData(),
    )
    return st


# ── Confluence factors ────────────────────────────────────────────────────


def test_contrarian_factor_fires_for_long_in_short_crowded():
    state = _base_state()
    state.derivatives = FakeDeriv(regime="SHORT_CROWDED")
    score = score_direction(state, Direction.BULLISH)
    names = [f.name for f in score.factors]
    assert "derivatives_contrarian" in names


def test_contrarian_factor_fires_for_short_in_long_crowded():
    state = _base_state()
    state.derivatives = FakeDeriv(regime="LONG_CROWDED")
    score = score_direction(state, Direction.BEARISH)
    names = [f.name for f in score.factors]
    assert "derivatives_contrarian" in names


def test_contrarian_does_not_fire_for_aligned_side():
    """Bullish trade into LONG_CROWDED should NOT get the contrarian boost."""
    state = _base_state()
    state.derivatives = FakeDeriv(regime="LONG_CROWDED")
    score = score_direction(state, Direction.BULLISH)
    names = [f.name for f in score.factors]
    assert "derivatives_contrarian" not in names


def test_capitulation_factor_fires_on_imbalance():
    state = _base_state()
    # Shorts washed (imbalance > 0) → bullish gets the capitulation boost.
    state.derivatives = FakeDeriv(regime="CAPITULATION", liq_imbalance_1h=0.4)
    score = score_direction(state, Direction.BULLISH)
    assert any(f.name == "derivatives_capitulation" for f in score.factors)


def test_only_one_derivatives_slot_fires_per_cycle():
    """SHORT_CROWDED + a strong heatmap cluster → only contrarian fires,
    not heatmap_target (elif chain)."""
    state = _base_state(price=100.0, atr=1.0)
    state.derivatives = FakeDeriv(regime="SHORT_CROWDED")
    state.liquidity_heatmap = FakeHeatmap(
        nearest_above=FakeCluster(price=101.0, notional_usd=1000.0),
        largest_above_notional=1000.0,
    )
    score = score_direction(state, Direction.BULLISH)
    names = [f.name for f in score.factors]
    assert "derivatives_contrarian" in names
    assert "derivatives_heatmap_target" not in names


def test_heatmap_target_fires_when_cluster_close_and_large():
    state = _base_state(price=100.0, atr=1.0)
    state.derivatives = FakeDeriv(regime="BALANCED")
    # nearest_above at $102 (within 3*ATR=3.0) with 80% of largest notional.
    state.liquidity_heatmap = FakeHeatmap(
        nearest_above=FakeCluster(price=102.0, notional_usd=800.0),
        largest_above_notional=1000.0,
    )
    score = score_direction(state, Direction.BULLISH)
    assert any(f.name == "derivatives_heatmap_target" for f in score.factors)


def test_heatmap_target_skipped_when_cluster_too_far():
    state = _base_state(price=100.0, atr=1.0)
    state.derivatives = FakeDeriv(regime="BALANCED")
    state.liquidity_heatmap = FakeHeatmap(
        nearest_above=FakeCluster(price=120.0, notional_usd=800.0),  # >3*ATR
        largest_above_notional=1000.0,
    )
    score = score_direction(state, Direction.BULLISH)
    assert not any(f.name == "derivatives_heatmap_target" for f in score.factors)


# ── Crowded-skip gate ──────────────────────────────────────────────────────


def test_crowded_skip_gate_blocks_long_in_long_crowded_with_hot_funding():
    deriv = FakeDeriv(regime="LONG_CROWDED", funding_rate_zscore_30d=3.5)
    assert _should_skip_for_derivatives(
        deriv, Direction.BULLISH,
        crowded_skip_enabled=True, crowded_skip_z_threshold=3.0,
    ) is True


def test_crowded_skip_gate_does_not_block_contrarian_side():
    """LONG_CROWDED + shorting → gate should allow it (we want to fade)."""
    deriv = FakeDeriv(regime="LONG_CROWDED", funding_rate_zscore_30d=3.5)
    assert _should_skip_for_derivatives(
        deriv, Direction.BEARISH,
        crowded_skip_enabled=True, crowded_skip_z_threshold=3.0,
    ) is False


def test_crowded_skip_gate_disabled_never_blocks():
    deriv = FakeDeriv(regime="LONG_CROWDED", funding_rate_zscore_30d=5.0)
    assert _should_skip_for_derivatives(
        deriv, Direction.BULLISH,
        crowded_skip_enabled=False, crowded_skip_z_threshold=3.0,
    ) is False


def test_crowded_skip_gate_respects_z_threshold():
    """Regime is crowded but funding_z below threshold → don't block."""
    deriv = FakeDeriv(regime="LONG_CROWDED", funding_rate_zscore_30d=1.5)
    assert _should_skip_for_derivatives(
        deriv, Direction.BULLISH,
        crowded_skip_enabled=True, crowded_skip_z_threshold=3.0,
    ) is False


def test_crowded_skip_gate_none_state_never_blocks():
    assert _should_skip_for_derivatives(
        None, Direction.BULLISH,
        crowded_skip_enabled=True, crowded_skip_z_threshold=3.0,
    ) is False
