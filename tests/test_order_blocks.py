"""Tests for src.analysis.order_blocks."""

from __future__ import annotations

from src.analysis.order_blocks import (
    OrderBlock,
    active_order_blocks,
    detect_order_blocks,
    nearest_order_block,
    price_in_order_block,
)
from src.data.candle_buffer import Candle
from src.data.models import Direction


def c(o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(open=o, high=h, low=l, close=cl, volume=1.0)


def bull(body: float, low_offset: float = 0.2) -> Candle:
    base = 100.0
    return c(o=base, h=base + body + 0.1, l=base - low_offset, cl=base + body)


def bear(body: float, high_offset: float = 0.2) -> Candle:
    base = 100.0
    return c(o=base, h=base + high_offset, l=base - body - 0.1, cl=base - body)


# ── Detection ───────────────────────────────────────────────────────────────


def test_detect_bullish_order_block():
    # Build small-body candles, then a bearish candle, then an impulsive bull
    candles = [
        c(100, 101, 99, 100.5),   # small body
        c(100.5, 101.5, 99.5, 101),
        c(101, 102, 100, 101.5),
        c(101.5, 102, 100.5, 100.6),  # small bearish — candidate OB
        c(100.6, 115, 100.5, 114),    # big impulsive bull (impulse)
        c(114, 116, 113, 115),
    ]
    obs = detect_order_blocks(candles, impulse_multiplier=2.0, lookback=10)
    assert any(o.direction == Direction.BULLISH for o in obs)
    ob = next(o for o in obs if o.direction == Direction.BULLISH)
    assert ob.origin_bar == 3   # the small bearish candle
    assert ob.bottom == 100.5
    assert ob.top == 102


def test_detect_bearish_order_block():
    candles = [
        c(100, 101, 99, 100.5),
        c(100.5, 101, 99.5, 100),
        c(100, 101, 99, 100.5),
        c(100.5, 102, 100, 101.5),    # small bullish — candidate OB
        c(101.5, 102, 88, 89),        # impulsive bear
        c(89, 91, 87, 88),
    ]
    obs = detect_order_blocks(candles, impulse_multiplier=2.0, lookback=10)
    assert any(o.direction == Direction.BEARISH for o in obs)
    ob = next(o for o in obs if o.direction == Direction.BEARISH)
    assert ob.origin_bar == 3
    assert ob.direction == Direction.BEARISH


def test_detect_uses_body_only_when_configured():
    candles = [
        c(100, 101, 99, 100.5),
        c(100.5, 101.5, 99.5, 101),
        c(101, 102, 100, 101.5),
        c(101.5, 103, 99, 100.6),     # bearish — full range 99-103, body 100.6-101.5
        c(100.6, 115, 100.5, 114),    # impulsive bull
        c(114, 116, 113, 115),
    ]
    obs = detect_order_blocks(candles, impulse_multiplier=2.0, use_body_only=True)
    ob = next(o for o in obs if o.direction == Direction.BULLISH)
    assert ob.bottom == 100.6
    assert ob.top == 101.5


def test_detect_no_obs_on_ranging_price():
    candles = [c(100, 101, 99, 100.5) for _ in range(20)]
    assert detect_order_blocks(candles) == []


def test_detect_requires_minimum_candles():
    assert detect_order_blocks([c(1, 2, 0, 1)] * 3) == []


# ── Broken + tests tracking ─────────────────────────────────────────────────


def test_bullish_ob_marked_broken_when_close_below():
    candles = [
        c(100, 101, 99, 100.5),
        c(100.5, 101.5, 99.5, 101),
        c(101, 102, 100, 101.5),
        c(101.5, 102, 100.5, 100.6),  # bearish OB at [100.5, 102]
        c(100.6, 115, 100.5, 114),    # impulse
        c(114, 116, 113, 115),
        c(115, 116, 114, 114.5),
        c(114, 116, 99, 100),         # close well below OB bottom
    ]
    obs = detect_order_blocks(candles, impulse_multiplier=2.0)
    bull_ob = next(o for o in obs if o.direction == Direction.BULLISH)
    assert bull_ob.status == "BROKEN"


def test_bullish_ob_counts_tests():
    candles = [
        c(100, 101, 99, 100.5),
        c(100.5, 101.5, 99.5, 101),
        c(101, 102, 100, 101.5),
        c(101.5, 102, 100.5, 100.6),  # OB bar 3 @ [100.5, 102]
        c(100.6, 115, 100.5, 114),    # impulse bar 4
        c(114, 116, 101, 115),        # test bar 5 (low=101 in zone)
        c(115, 117, 101.5, 116),      # test bar 6
        c(116, 117, 115, 115.5),      # no test
    ]
    obs = detect_order_blocks(candles, impulse_multiplier=2.0)
    bull_ob = next(o for o in obs if o.direction == Direction.BULLISH)
    assert bull_ob.status == "ACTIVE"
    assert bull_ob.tests >= 2


# ── Queries ─────────────────────────────────────────────────────────────────


def test_active_order_blocks_filter():
    obs = [
        OrderBlock(direction=Direction.BULLISH, bottom=100, top=102, origin_bar=1),
        OrderBlock(direction=Direction.BULLISH, bottom=90, top=92,
                   origin_bar=5, status="BROKEN"),
    ]
    active = active_order_blocks(obs)
    assert len(active) == 1
    assert active[0].top == 102


def test_nearest_order_block_direction_filter():
    obs = [
        OrderBlock(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=1),
        OrderBlock(direction=Direction.BEARISH, bottom=120, top=125, origin_bar=5),
    ]
    n = nearest_order_block(obs, price=110, direction=Direction.BULLISH)
    assert n is not None
    assert n.top == 105


def test_price_in_order_block():
    obs = [
        OrderBlock(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=1),
    ]
    assert price_in_order_block(obs, 103) is not None
    assert price_in_order_block(obs, 110) is None


def test_order_block_contains_and_midpoint():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=100, top=110, origin_bar=1)
    assert ob.midpoint == 105
    assert ob.contains(105)
    assert not ob.contains(111)
