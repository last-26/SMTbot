"""Unit tests for the ``ltf_momentum_alignment`` confluence factor.

LTF reversal moves often lead the entry TF. The factor gives full weight
when the LTF trend agrees with the candidate direction, and a partial boost
(0.6x) when a fresh LTF signal (≤3 bars old) agrees even if the trend
still reads RANGING.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.analysis.multi_timeframe import DEFAULT_WEIGHTS, score_direction
from src.data.models import (
    Direction,
    MarketState,
    OscillatorTableData,
    SignalTableData,
)


@dataclass
class FakeLTF:
    trend: Direction = Direction.RANGING
    last_signal: str = ""
    last_signal_bars_ago: int = 99


def _state(price: float = 100.0, atr: float = 1.0) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP", timeframe="3m",
        signal_table=SignalTableData(price=price, atr_14=atr),
        oscillator=OscillatorTableData(),
    )


def _has(score, name: str) -> bool:
    return any(f.name == name for f in score.factors)


def _weight_of(score, name: str) -> float:
    return next(f.weight for f in score.factors if f.name == name)


def test_ltf_trend_matches_direction_full_weight():
    """LTF trend == direction → factor fires at full weight."""
    ltf = FakeLTF(trend=Direction.BULLISH)
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert _has(score, "ltf_momentum_alignment")
    assert _weight_of(score, "ltf_momentum_alignment") == DEFAULT_WEIGHTS[
        "ltf_momentum_alignment"
    ]


def test_ltf_trend_opposes_direction_no_factor():
    """LTF trend opposes direction → factor does not fire."""
    ltf = FakeLTF(trend=Direction.BEARISH)
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert not _has(score, "ltf_momentum_alignment")


def test_fresh_buy_signal_gives_partial_weight_for_bullish():
    """Trend RANGING but fresh BUY ≤3 bars ago → 0.6x weight for bullish."""
    ltf = FakeLTF(
        trend=Direction.RANGING,
        last_signal="BUY",
        last_signal_bars_ago=2,
    )
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert _has(score, "ltf_momentum_alignment")
    expected = DEFAULT_WEIGHTS["ltf_momentum_alignment"] * 0.6
    assert abs(_weight_of(score, "ltf_momentum_alignment") - expected) < 1e-9


def test_fresh_sell_signal_gives_partial_weight_for_bearish():
    ltf = FakeLTF(
        trend=Direction.RANGING,
        last_signal="SELL",
        last_signal_bars_ago=1,
    )
    score = score_direction(_state(), Direction.BEARISH, ltf_state=ltf)
    assert _has(score, "ltf_momentum_alignment")


def test_stale_signal_does_not_fire():
    """bars_ago > 3 disqualifies the partial-weight path."""
    ltf = FakeLTF(
        trend=Direction.RANGING,
        last_signal="BUY",
        last_signal_bars_ago=10,
    )
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert not _has(score, "ltf_momentum_alignment")


def test_signal_side_mismatch_does_not_fire():
    """Fresh SELL on a BULLISH candidate → no factor."""
    ltf = FakeLTF(
        trend=Direction.RANGING,
        last_signal="SELL",
        last_signal_bars_ago=1,
    )
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert not _has(score, "ltf_momentum_alignment")


def test_no_ltf_state_is_noop():
    """ltf_state=None never adds the factor and never raises."""
    score = score_direction(_state(), Direction.BULLISH, ltf_state=None)
    assert not _has(score, "ltf_momentum_alignment")


def test_gold_buy_signal_also_counts():
    """Full Pine signal strings like GOLD_BUY still contain BUY and count."""
    ltf = FakeLTF(
        trend=Direction.RANGING,
        last_signal="GOLD_BUY",
        last_signal_bars_ago=0,
    )
    score = score_direction(_state(), Direction.BULLISH, ltf_state=ltf)
    assert _has(score, "ltf_momentum_alignment")
