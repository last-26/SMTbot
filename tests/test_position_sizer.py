"""Tests for src.strategy.position_sizer — SL placement helpers."""

from __future__ import annotations

import pytest

from src.analysis.fvg import FVG
from src.analysis.order_blocks import OrderBlock as PyOrderBlock
from src.data.candle_buffer import Candle
from src.data.models import Direction
from src.strategy.position_sizer import (
    recent_swing_price,
    sl_from_atr,
    sl_from_fvg,
    sl_from_order_block,
    sl_from_swing,
)


# ── ATR buffer math ─────────────────────────────────────────────────────────


def test_sl_from_order_block_bullish_pushes_below():
    ob = PyOrderBlock(
        direction=Direction.BULLISH, bottom=100.0, top=105.0, origin_bar=0,
    )
    sl = sl_from_order_block(ob, atr=10.0, direction=Direction.BULLISH, buffer_mult=0.2)
    assert sl == pytest.approx(100.0 - 2.0)
    assert sl < ob.bottom


def test_sl_from_order_block_bearish_pushes_above():
    ob = PyOrderBlock(
        direction=Direction.BEARISH, bottom=95.0, top=100.0, origin_bar=0,
    )
    sl = sl_from_order_block(ob, atr=10.0, direction=Direction.BEARISH, buffer_mult=0.2)
    assert sl == pytest.approx(100.0 + 2.0)
    assert sl > ob.top


def test_sl_from_fvg_bullish_below_bottom():
    fvg = FVG(direction=Direction.BULLISH, bottom=90.0, top=95.0, origin_bar=0)
    sl = sl_from_fvg(fvg, atr=5.0, direction=Direction.BULLISH, buffer_mult=0.2)
    assert sl == pytest.approx(89.0)


def test_sl_from_fvg_bearish_above_top():
    fvg = FVG(direction=Direction.BEARISH, bottom=105.0, top=110.0, origin_bar=0)
    sl = sl_from_fvg(fvg, atr=5.0, direction=Direction.BEARISH, buffer_mult=0.2)
    assert sl == pytest.approx(111.0)


def test_sl_from_swing_bullish():
    assert sl_from_swing(95.0, atr=10.0, direction=Direction.BULLISH, buffer_mult=0.2) \
        == pytest.approx(93.0)


def test_sl_from_swing_bearish():
    assert sl_from_swing(105.0, atr=10.0, direction=Direction.BEARISH, buffer_mult=0.2) \
        == pytest.approx(107.0)


def test_sl_from_atr_fallback_bullish():
    sl = sl_from_atr(entry_price=100.0, atr=5.0, direction=Direction.BULLISH, atr_multiple=2.0)
    assert sl == pytest.approx(90.0)


def test_sl_from_atr_fallback_bearish():
    sl = sl_from_atr(entry_price=100.0, atr=5.0, direction=Direction.BEARISH, atr_multiple=2.0)
    assert sl == pytest.approx(110.0)


def test_zero_buffer_puts_sl_exactly_at_level():
    ob = PyOrderBlock(direction=Direction.BULLISH, bottom=100.0, top=105.0, origin_bar=0)
    sl = sl_from_order_block(ob, atr=0.0, direction=Direction.BULLISH, buffer_mult=0.0)
    assert sl == pytest.approx(100.0)


def test_rejects_undefined_direction():
    ob = PyOrderBlock(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=0)
    with pytest.raises(ValueError):
        sl_from_order_block(ob, atr=1.0, direction=Direction.UNDEFINED)


def test_rejects_negative_buffer_mult():
    ob = PyOrderBlock(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=0)
    with pytest.raises(ValueError):
        sl_from_order_block(ob, atr=1.0, direction=Direction.BULLISH, buffer_mult=-0.1)


# ── Swing lookback from candle buffer ───────────────────────────────────────


def _c(low: float, high: float) -> Candle:
    return Candle(open=low, high=high, low=low, close=(low + high) / 2)


def test_recent_swing_bullish_picks_min_low():
    candles = [_c(90, 95), _c(85, 92), _c(88, 94), _c(91, 96)]
    assert recent_swing_price(candles, Direction.BULLISH, lookback=10) == 85.0


def test_recent_swing_bearish_picks_max_high():
    candles = [_c(90, 95), _c(85, 99), _c(88, 94), _c(91, 96)]
    assert recent_swing_price(candles, Direction.BEARISH, lookback=10) == 99.0


def test_recent_swing_respects_lookback_window():
    # First candle has the min low (80) but it's outside the window of 2
    candles = [_c(80, 85), _c(90, 95), _c(91, 96)]
    assert recent_swing_price(candles, Direction.BULLISH, lookback=2) == 90.0


def test_recent_swing_empty_returns_none():
    assert recent_swing_price([], Direction.BULLISH) is None
