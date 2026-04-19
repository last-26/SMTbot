"""Phase 7.D3 — ADX trend-regime classifier + conditional scoring tests.

Covers:
  * `_wilder_smooth` seed + running-sum behavior.
  * `compute_adx` on synthetic strong-uptrend / ranging / flat series.
  * `classify_trend_regime` threshold boundaries + UNKNOWN branches.
  * `_apply_trend_regime_conditional` weight adjustments per regime.
  * `score_direction` end-to-end weight shift under STRONG_TREND and
    RANGING when the opt-in flag is set.
"""

from __future__ import annotations

from src.analysis.multi_timeframe import (
    DEFAULT_WEIGHTS,
    _apply_trend_regime_conditional,
    score_direction,
)
from src.analysis.trend_regime import (
    DEFAULT_ADX_PERIOD,
    DEFAULT_RANGING_THRESHOLD,
    DEFAULT_STRONG_THRESHOLD,
    TrendRegime,
    _wilder_smooth,
    classify_trend_regime,
    compute_adx,
)
from src.data.candle_buffer import Candle
from src.data.models import (
    Direction,
    MarketState,
    OrderBlock,
    OscillatorTableData,
    SignalTableData,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_candle(o: float, h: float, l: float, c: float) -> Candle:
    return Candle(open=o, high=h, low=l, close=c, volume=1.0)


def _strong_uptrend(n: int = 40, start: float = 100.0, step: float = 1.5) -> list[Candle]:
    """Strictly higher highs + higher lows → ADX rockets above 30."""
    out: list[Candle] = []
    prev_close = start
    for i in range(n):
        o = prev_close
        c = prev_close + step
        h = c + 0.1
        l = o - 0.05
        out.append(_make_candle(o, h, l, c))
        prev_close = c
    return out


def _ranging(n: int = 60, base: float = 100.0, amp: float = 0.5) -> list[Candle]:
    """Oscillating bars with no sustained drift → balanced +DI/-DI → ADX < 20.

    Longer pseudo-random zig-zag (deterministic via index) so TRs / DMs
    both fire every bar but the DI ratio stays balanced. A naïve strict
    2-bar flip yields zero DMs on both sides which collapses to UNKNOWN.
    """
    out: list[Candle] = []
    pattern = [0.4, -0.3, 0.2, -0.4, 0.3, -0.2, 0.35, -0.35]
    for i in range(n):
        drift = pattern[i % len(pattern)]
        o = base
        c = base + drift
        h = max(o, c) + 0.05 + (0.02 if i % 3 == 0 else 0.0)
        l = min(o, c) - 0.05 - (0.02 if i % 4 == 0 else 0.0)
        out.append(_make_candle(o, h, l, c))
    return out


def _flat(n: int = 40, price: float = 100.0) -> list[Candle]:
    """Zero range bars — degenerate input, ADX math collapses."""
    return [_make_candle(price, price, price, price) for _ in range(n)]


# ── _wilder_smooth ──────────────────────────────────────────────────────────


def test_wilder_smooth_empty_when_buffer_short():
    assert _wilder_smooth([1.0, 2.0], period=5) == []


def test_wilder_smooth_seed_is_sum_of_first_period():
    out = _wilder_smooth([1.0, 2.0, 3.0, 4.0, 5.0], period=3)
    assert out[0] == 6.0   # 1+2+3
    # next = prev - prev/3 + 4  = 6 - 2 + 4 = 8
    assert out[1] == 8.0
    # = 8 - 8/3 + 5 ≈ 7.6667
    assert abs(out[2] - (8 - 8 / 3 + 5)) < 1e-9


# ── compute_adx ─────────────────────────────────────────────────────────────


def test_compute_adx_returns_none_on_short_buffer():
    # Needs at least 2*period+1 bars.
    assert compute_adx([], period=14) is None
    assert compute_adx(_strong_uptrend(10), period=14) is None


def test_compute_adx_strong_uptrend_pushes_adx_above_strong_threshold():
    result = compute_adx(_strong_uptrend(50), period=14)
    assert result is not None
    adx, plus_di, minus_di = result
    assert adx >= DEFAULT_STRONG_THRESHOLD
    # +DI must dominate when every bar is up.
    assert plus_di > minus_di


def test_compute_adx_flat_series_returns_none():
    # Zero TR / zero DM series → DX undefined → None by contract.
    assert compute_adx(_flat(50), period=14) is None


# ── classify_trend_regime ───────────────────────────────────────────────────


def test_classify_unknown_on_empty_candles():
    res = classify_trend_regime([])
    assert res.regime == TrendRegime.UNKNOWN
    assert res.bars_used == 0


def test_classify_unknown_on_short_buffer():
    res = classify_trend_regime(_strong_uptrend(10))
    assert res.regime == TrendRegime.UNKNOWN


def test_classify_strong_trend_on_trending_tape():
    res = classify_trend_regime(_strong_uptrend(50))
    assert res.regime == TrendRegime.STRONG_TREND
    assert res.adx >= DEFAULT_STRONG_THRESHOLD


def test_classify_ranging_on_sideways_tape():
    res = classify_trend_regime(_ranging(60))
    assert res.regime == TrendRegime.RANGING
    assert res.adx < DEFAULT_RANGING_THRESHOLD


def test_classify_unknown_on_degenerate_flat_input():
    res = classify_trend_regime(_flat(50))
    assert res.regime == TrendRegime.UNKNOWN


def test_classify_rejects_inverted_thresholds():
    import pytest
    with pytest.raises(ValueError):
        classify_trend_regime(
            _strong_uptrend(50),
            ranging_threshold=30.0,
            strong_threshold=20.0,
        )


def test_classify_default_thresholds_match_constants():
    # Regression — we rely on these for YAML parity.
    assert DEFAULT_ADX_PERIOD == 14
    assert DEFAULT_RANGING_THRESHOLD == 20.0
    assert DEFAULT_STRONG_THRESHOLD == 30.0


# ── _apply_trend_regime_conditional ────────────────────────────────────────


def _base_weights() -> dict[str, float]:
    return dict(DEFAULT_WEIGHTS)


def test_conditional_flag_off_returns_weights_unchanged():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, TrendRegime.STRONG_TREND, enabled=False)
    assert out is w  # no copy when flag off


def test_conditional_unknown_regime_returns_unchanged():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, TrendRegime.UNKNOWN, enabled=True)
    assert out is w


def test_conditional_weak_trend_returns_unchanged():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, TrendRegime.WEAK_TREND, enabled=True)
    assert out is w


def test_conditional_none_regime_returns_unchanged():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, None, enabled=True)
    assert out is w


def test_conditional_strong_trend_boosts_htf_penalises_sweep():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, TrendRegime.STRONG_TREND, enabled=True)
    assert out is not w  # new dict
    assert out["htf_trend_alignment"] == w["htf_trend_alignment"] * 1.5
    assert out["recent_sweep"] == w["recent_sweep"] * 0.5


def test_conditional_ranging_penalises_htf_boosts_sweep():
    w = _base_weights()
    out = _apply_trend_regime_conditional(w, TrendRegime.RANGING, enabled=True)
    assert out is not w
    assert out["htf_trend_alignment"] == w["htf_trend_alignment"] * 0.5
    assert out["recent_sweep"] == w["recent_sweep"] * 1.5


# ── score_direction integration ────────────────────────────────────────────


def _state_with_htf_and_sweep(direction: Direction) -> MarketState:
    """State that would trigger both htf_trend_alignment AND recent_sweep.

    Sweep direction is inverted (bearish sweep = bullish entry signal).
    """
    sweep_token = "BEAR@99" if direction == Direction.BULLISH else "BULL@101"
    sig = SignalTableData(
        trend_htf=direction,
        last_mss="—",
        last_sweep=sweep_token,
        active_ob="—",
        active_fvg="—",
        vmc_ribbon="—",
        price=100.0,
        atr_14=1.0,
    )
    return MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=[OrderBlock(direction=direction, bottom=95.0, top=97.0)],
        fvg_zones=[],
    )


def test_score_direction_strong_trend_shifts_weights():
    state = _state_with_htf_and_sweep(Direction.BULLISH)
    baseline = score_direction(state, Direction.BULLISH)
    shifted = score_direction(
        state, Direction.BULLISH,
        trend_regime=TrendRegime.STRONG_TREND,
        trend_regime_conditional_scoring_enabled=True,
    )
    base_htf = next(f.weight for f in baseline.factors if f.name == "htf_trend_alignment")
    base_sweep = next(f.weight for f in baseline.factors if f.name == "recent_sweep")
    shift_htf = next(f.weight for f in shifted.factors if f.name == "htf_trend_alignment")
    shift_sweep = next(f.weight for f in shifted.factors if f.name == "recent_sweep")
    assert shift_htf == base_htf * 1.5
    assert shift_sweep == base_sweep * 0.5


def test_score_direction_ranging_shifts_weights():
    state = _state_with_htf_and_sweep(Direction.BULLISH)
    baseline = score_direction(state, Direction.BULLISH)
    shifted = score_direction(
        state, Direction.BULLISH,
        trend_regime=TrendRegime.RANGING,
        trend_regime_conditional_scoring_enabled=True,
    )
    base_htf = next(f.weight for f in baseline.factors if f.name == "htf_trend_alignment")
    base_sweep = next(f.weight for f in baseline.factors if f.name == "recent_sweep")
    shift_htf = next(f.weight for f in shifted.factors if f.name == "htf_trend_alignment")
    shift_sweep = next(f.weight for f in shifted.factors if f.name == "recent_sweep")
    assert shift_htf == base_htf * 0.5
    assert shift_sweep == base_sweep * 1.5


def test_score_direction_flag_off_ignores_regime():
    state = _state_with_htf_and_sweep(Direction.BULLISH)
    baseline = score_direction(state, Direction.BULLISH)
    forced = score_direction(
        state, Direction.BULLISH,
        trend_regime=TrendRegime.STRONG_TREND,
        trend_regime_conditional_scoring_enabled=False,
    )
    # Same weights regardless of regime when the flag is off.
    assert baseline.score == forced.score


def test_score_direction_weak_trend_is_neutral():
    state = _state_with_htf_and_sweep(Direction.BULLISH)
    baseline = score_direction(state, Direction.BULLISH)
    shifted = score_direction(
        state, Direction.BULLISH,
        trend_regime=TrendRegime.WEAK_TREND,
        trend_regime_conditional_scoring_enabled=True,
    )
    assert baseline.score == shifted.score
