"""Tests for the Arkham daily macro-bias modifier applied inside
`calculate_confluence` (Phase C).

These tests mock `score_direction` so we can reason about the modifier
in isolation — no need to build a full MarketState that happens to
produce a specific pillar score.
"""

from __future__ import annotations

import pytest

from src.analysis import multi_timeframe as mtf_mod
from src.analysis.multi_timeframe import (
    ConfluenceFactor,
    ConfluenceScore,
    _daily_bias_multipliers,
    calculate_confluence,
)
from src.data.models import Direction, MarketState
from src.data.on_chain_types import OnChainSnapshot


# ── _daily_bias_multipliers — unit coverage ─────────────────────────────────


def test_multipliers_both_one_when_delta_zero():
    snap = OnChainSnapshot(daily_macro_bias="bullish", snapshot_age_s=0,
                           stale_threshold_s=7200)
    assert _daily_bias_multipliers(snap, delta=0.0) == (1.0, 1.0)


def test_multipliers_both_one_when_snapshot_absent():
    assert _daily_bias_multipliers(None, delta=0.10) == (1.0, 1.0)


def test_multipliers_both_one_when_snapshot_stale():
    snap = OnChainSnapshot(
        daily_macro_bias="bullish",
        snapshot_age_s=10_000,   # > stale_threshold_s → fresh=False
        stale_threshold_s=7200,
    )
    assert snap.fresh is False
    assert _daily_bias_multipliers(snap, delta=0.10) == (1.0, 1.0)


def test_multipliers_bullish_favors_long_penalizes_short():
    snap = OnChainSnapshot(
        daily_macro_bias="bullish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    ml, ms = _daily_bias_multipliers(snap, delta=0.10)
    assert ml == pytest.approx(1.10)
    assert ms == pytest.approx(0.90)


def test_multipliers_bearish_mirrors_bullish():
    snap = OnChainSnapshot(
        daily_macro_bias="bearish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    ml, ms = _daily_bias_multipliers(snap, delta=0.10)
    assert ml == pytest.approx(0.90)
    assert ms == pytest.approx(1.10)


def test_multipliers_neutral_is_noop():
    snap = OnChainSnapshot(
        daily_macro_bias="neutral", snapshot_age_s=0, stale_threshold_s=7200,
    )
    assert _daily_bias_multipliers(snap, delta=0.10) == (1.0, 1.0)


# ── calculate_confluence with mocked score_direction ───────────────────────


def _patch_score_direction(monkeypatch, bull_score: float, bear_score: float):
    """Make `score_direction` deterministic by returning fixed scores per
    direction. Keeps these tests focused on the modifier math.
    """

    def _fake(state, direction, **kwargs):
        # Preserve a dummy factor so factor_names list is non-empty.
        if direction == Direction.BULLISH:
            return ConfluenceScore(
                direction=Direction.BULLISH,
                score=bull_score,
                factors=[ConfluenceFactor(
                    name="dummy_bull", weight=bull_score,
                    direction=Direction.BULLISH,
                )],
            )
        return ConfluenceScore(
            direction=Direction.BEARISH,
            score=bear_score,
            factors=[ConfluenceFactor(
                name="dummy_bear", weight=bear_score,
                direction=Direction.BEARISH,
            )],
        )

    monkeypatch.setattr(mtf_mod, "score_direction", _fake)


def _state() -> MarketState:
    return MarketState(symbol="BTC-USDT-SWAP", timeframe="3")


def test_confluence_modifier_off_returns_untouched_scores(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    conf_default = calculate_confluence(state)
    conf_flag_off = calculate_confluence(
        state, daily_bias_enabled=False, daily_bias_delta=0.10,
    )
    assert conf_default.direction == Direction.BULLISH
    assert conf_default.score == pytest.approx(3.0)
    assert conf_flag_off.score == pytest.approx(3.0)


def test_confluence_modifier_on_without_snapshot_is_noop(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    # No state.on_chain attached — modifier must no-op.
    conf = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    assert conf.score == pytest.approx(3.0)


def test_confluence_modifier_bullish_bias_boosts_long_score(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bullish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    boosted = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    # Bull score 3.0 × 1.10 = 3.30.
    assert boosted.direction == Direction.BULLISH
    assert boosted.score == pytest.approx(3.30)


def test_confluence_modifier_bearish_bias_dampens_long_score(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bearish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    dampened = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    # Bull score 3.0 × 0.90 = 2.70. Bear 1.0 × 1.10 = 1.10.
    # Bull still wins at 2.70 > 1.10.
    assert dampened.direction == Direction.BULLISH
    assert dampened.score == pytest.approx(2.70)


def test_confluence_modifier_can_flip_winner_when_bias_strong_enough(monkeypatch):
    """If bear score gets boosted enough under a bearish bias, the short
    side wins even when the base bull > base bear."""
    # Delta = 0.20 on a bear score of 2.5 → 3.00; bull 2.9 × 0.80 = 2.32.
    _patch_score_direction(monkeypatch, bull_score=2.9, bear_score=2.5)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bearish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    baseline = calculate_confluence(state)
    flipped = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.20,
    )
    # Without modifier, bull 2.9 > bear 2.5 → bullish.
    assert baseline.direction == Direction.BULLISH
    # With bearish modifier, bear 3.00 > bull 2.32 → bearish.
    assert flipped.direction == Direction.BEARISH


def test_confluence_modifier_stale_snapshot_skipped(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bullish",
        snapshot_age_s=10_000,   # > stale_threshold_s
        stale_threshold_s=7200,
    )
    assert state.on_chain.fresh is False
    conf = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    assert conf.score == pytest.approx(3.0)  # no modifier applied


def test_confluence_modifier_preserves_factors_list(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=3.0, bear_score=1.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bullish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    conf = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    assert len(conf.factors) == 1
    assert conf.factors[0].name == "dummy_bull"


def test_confluence_modifier_borderline_setup_lifts_to_tradable(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=2.85, bear_score=1.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bullish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    baseline = calculate_confluence(state)
    boosted = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    # Below threshold without modifier:
    assert baseline.is_tradable(3.0) is False
    # Above threshold with modifier: 2.85 × 1.10 = 3.135 > 3.0.
    assert boosted.score == pytest.approx(3.135)
    assert boosted.is_tradable(3.0) is True


def test_confluence_modifier_symmetric_for_bear_setup(monkeypatch):
    _patch_score_direction(monkeypatch, bull_score=1.0, bear_score=3.0)
    state = _state()
    state.on_chain = OnChainSnapshot(
        daily_macro_bias="bearish", snapshot_age_s=0, stale_threshold_s=7200,
    )
    boosted = calculate_confluence(
        state, daily_bias_enabled=True, daily_bias_delta=0.10,
    )
    # Bear 3.0 × 1.10 = 3.30. Bull 1.0 × 0.90 = 0.90.
    assert boosted.direction == Direction.BEARISH
    assert boosted.score == pytest.approx(3.30)
