"""Phase 7.D2 — divergence_signal factor tests.

Covers:
  * `_divergence_direction` token parsing (BULL_REG, BEAR_HIDDEN, junk).
  * `_divergence_decay_weight` bar-ago decay bands.
  * `score_direction` emits the factor with decayed weight when Pine's
    `last_wt_div` matches the candidate direction.
  * Opposite-direction / missing / stale divergences do not contribute.
  * `calculate_confluence` plumbs the kwargs through end-to-end.
"""

from __future__ import annotations

from src.analysis.multi_timeframe import (
    DEFAULT_WEIGHTS,
    _divergence_decay_weight,
    _divergence_direction,
    calculate_confluence,
    score_direction,
)
from src.data.models import (
    Direction,
    MarketState,
    OrderBlock,
    OscillatorTableData,
    SignalTableData,
)


# ── token parsing ──────────────────────────────────────────────────────────


def test_divergence_direction_parses_bullish_tokens():
    assert _divergence_direction("BULL_REG") == Direction.BULLISH
    assert _divergence_direction("BULL_HIDDEN") == Direction.BULLISH
    assert _divergence_direction("bull_reg") == Direction.BULLISH   # case


def test_divergence_direction_parses_bearish_tokens():
    assert _divergence_direction("BEAR_REG") == Direction.BEARISH
    assert _divergence_direction("BEAR_HIDDEN") == Direction.BEARISH


def test_divergence_direction_returns_undefined_on_junk():
    assert _divergence_direction("") == Direction.UNDEFINED
    assert _divergence_direction(None) == Direction.UNDEFINED
    assert _divergence_direction("—") == Direction.UNDEFINED
    assert _divergence_direction("NEUTRAL") == Direction.UNDEFINED


# ── decay bands ────────────────────────────────────────────────────────────


def test_decay_full_weight_within_fresh_window():
    for ba in (0, 1, 2, 3):
        assert _divergence_decay_weight(ba, fresh_bars=3, decay_bars=6, max_bars=9) == 1.0


def test_decay_half_weight_in_decay_window():
    for ba in (4, 5, 6):
        assert _divergence_decay_weight(ba, fresh_bars=3, decay_bars=6, max_bars=9) == 0.5


def test_decay_quarter_weight_in_tail_window():
    for ba in (7, 8, 9):
        assert _divergence_decay_weight(ba, fresh_bars=3, decay_bars=6, max_bars=9) == 0.25


def test_decay_zero_past_max_bars():
    assert _divergence_decay_weight(10, fresh_bars=3, decay_bars=6, max_bars=9) == 0.0
    assert _divergence_decay_weight(99, fresh_bars=3, decay_bars=6, max_bars=9) == 0.0


def test_decay_normalizes_negative_bars_ago():
    # bars_ago can never be negative in practice, but normalize to 0 → full.
    assert _divergence_decay_weight(-5, fresh_bars=3, decay_bars=6, max_bars=9) == 1.0


# ── score_direction integration ────────────────────────────────────────────


def _state_with_divergence(
    *,
    direction: Direction,
    div_token: str,
    bars_ago: int,
) -> MarketState:
    """Minimal bullish-friendly structure + divergence on the oscillator."""
    sig = SignalTableData(
        trend_htf=direction,
        last_mss=f"{direction.value}@99" if direction == Direction.BULLISH else f"{direction.value}@101",
        active_ob="BULL@95-97" if direction == Direction.BULLISH else "BEAR@103-105",
        vmc_ribbon=direction.value,
        price=100.0,
        atr_14=1.0,
    )
    osc = OscillatorTableData(
        last_wt_div=div_token,
        last_wt_div_bars_ago=bars_ago,
    )
    ob = (
        OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)
        if direction == Direction.BULLISH
        else OrderBlock(direction=Direction.BEARISH, bottom=103.0, top=105.0)
    )
    return MarketState(
        signal_table=sig,
        oscillator=osc,
        order_blocks=[ob],
        fvg_zones=[],
    )


def test_score_direction_emits_fresh_bullish_divergence():
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_REG", bars_ago=1,
    )
    score = score_direction(state, Direction.BULLISH)
    match = [f for f in score.factors if f.name == "divergence_signal"]
    assert len(match) == 1
    # Fresh window → full weight.
    assert match[0].weight == DEFAULT_WEIGHTS["divergence_signal"]


def test_score_direction_decays_aging_divergence():
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_HIDDEN", bars_ago=5,
    )
    score = score_direction(state, Direction.BULLISH)
    match = [f for f in score.factors if f.name == "divergence_signal"]
    assert len(match) == 1
    assert match[0].weight == DEFAULT_WEIGHTS["divergence_signal"] * 0.5


def test_score_direction_tail_window_quarter_weight():
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_REG", bars_ago=8,
    )
    score = score_direction(state, Direction.BULLISH)
    match = [f for f in score.factors if f.name == "divergence_signal"]
    assert len(match) == 1
    assert match[0].weight == DEFAULT_WEIGHTS["divergence_signal"] * 0.25


def test_score_direction_drops_stale_divergence():
    # Bars_ago beyond max_bars (default 9) → factor not emitted.
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_REG", bars_ago=15,
    )
    score = score_direction(state, Direction.BULLISH)
    assert "divergence_signal" not in [f.name for f in score.factors]


def test_score_direction_ignores_opposite_direction():
    # BEAR_REG on a BULLISH candidate direction — must NOT fire.
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BEAR_REG", bars_ago=1,
    )
    score = score_direction(state, Direction.BULLISH)
    assert "divergence_signal" not in [f.name for f in score.factors]


def test_score_direction_ignores_missing_divergence_token():
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="", bars_ago=0,
    )
    score = score_direction(state, Direction.BULLISH)
    assert "divergence_signal" not in [f.name for f in score.factors]


def test_score_direction_emits_fresh_bearish_divergence():
    state = _state_with_divergence(
        direction=Direction.BEARISH, div_token="BEAR_HIDDEN", bars_ago=2,
    )
    score = score_direction(state, Direction.BEARISH)
    match = [f for f in score.factors if f.name == "divergence_signal"]
    assert len(match) == 1
    assert match[0].weight == DEFAULT_WEIGHTS["divergence_signal"]


# ── calculate_confluence round-trip ────────────────────────────────────────


def test_calculate_confluence_plumbs_divergence_kwargs():
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_REG", bars_ago=1,
    )
    # Override defaults — tighter fresh window so bars_ago=1 still full weight.
    conf = calculate_confluence(
        state,
        divergence_fresh_bars=2, divergence_decay_bars=4, divergence_max_bars=6,
    )
    assert "divergence_signal" in conf.factor_names


def test_calculate_confluence_respects_custom_decay_band():
    # bars_ago=5 with max_bars=4 → factor should NOT fire (exceeded).
    state = _state_with_divergence(
        direction=Direction.BULLISH, div_token="BULL_REG", bars_ago=5,
    )
    conf = calculate_confluence(
        state,
        divergence_fresh_bars=1, divergence_decay_bars=2, divergence_max_bars=4,
    )
    assert "divergence_signal" not in conf.factor_names
