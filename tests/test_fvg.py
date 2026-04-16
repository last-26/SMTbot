"""Tests for src.analysis.fvg."""

from __future__ import annotations

from src.analysis.fvg import (
    FVG,
    active_fvgs,
    detect_fvgs,
    nearest_fvg,
    price_in_fvg,
)
from src.data.candle_buffer import Candle
from src.data.models import Direction


def c(o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(open=o, high=h, low=l, close=cl, volume=1.0)


# ── Detection ───────────────────────────────────────────────────────────────


def test_detect_bullish_fvg():
    candles = [
        c(100, 102, 99, 101),    # c1: high=102
        c(102, 110, 101, 109),   # c2: impulse
        c(108, 112, 105, 111),   # c3: low=105 > c1.high → gap [102, 105]
    ]
    fvgs = detect_fvgs(candles)
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction == Direction.BULLISH
    assert fvg.bottom == 102
    assert fvg.top == 105
    assert fvg.origin_bar == 1


def test_detect_bearish_fvg():
    candles = [
        c(110, 112, 108, 109),   # c1: low=108
        c(108, 109, 100, 101),   # c2: impulse down
        c(100, 102, 95, 97),     # c3: high=102 < c1.low → gap [102, 108]
    ]
    fvgs = detect_fvgs(candles)
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction == Direction.BEARISH
    assert fvg.bottom == 102
    assert fvg.top == 108


def test_detect_no_fvg_when_wicks_overlap():
    candles = [
        c(100, 105, 99, 104),    # high=105
        c(104, 108, 103, 107),
        c(107, 110, 104, 109),   # low=104 < c1.high → no gap
    ]
    assert detect_fvgs(candles) == []


def test_detect_requires_three_candles():
    assert detect_fvgs([]) == []
    assert detect_fvgs([c(1, 2, 0, 1)]) == []
    assert detect_fvgs([c(1, 2, 0, 1), c(1, 2, 0, 1)]) == []


def test_fvg_size_pct_filter():
    # Tiny gap
    candles = [
        c(100, 100.05, 99, 100),
        c(100, 101, 99, 100.9),
        c(101, 102, 100.1, 101.5),  # gap [100.05, 100.1] = 0.05 wide
    ]
    assert detect_fvgs(candles, min_size_pct=0.0) != []
    assert detect_fvgs(candles, min_size_pct=1.0) == []  # filter blocks tiny gap


# ── Mitigation ──────────────────────────────────────────────────────────────


def test_bullish_fvg_mitigation():
    candles = [
        c(100, 102, 99, 101),
        c(102, 110, 101, 109),
        c(108, 112, 105, 111),    # creates FVG [102, 105]
        c(110, 112, 108, 110),    # stays above
        c(108, 110, 104, 106),    # low=104, enters FVG → mitigated
    ]
    fvgs = detect_fvgs(candles)
    assert len(fvgs) == 1
    assert fvgs[0].status == "MITIGATED"
    assert fvgs[0].mitigation_bar == 4


def test_bullish_fvg_stays_active_if_never_tagged():
    candles = [
        c(100, 102, 99, 101),
        c(102, 110, 101, 109),
        c(108, 112, 105, 111),    # FVG [102, 105]
        c(112, 116, 110, 115),
        c(116, 120, 113, 119),    # never revisits 105
    ]
    fvgs = detect_fvgs(candles)
    # Find the FVG we care about (may be one of several)
    target = next(f for f in fvgs if f.bottom == 102 and f.top == 105)
    assert target.status == "ACTIVE"


def test_active_fvgs_filter():
    candles = [
        c(100, 102, 99, 101), c(102, 110, 101, 109), c(108, 112, 105, 111),
        c(112, 116, 110, 115), c(116, 120, 113, 119),
    ]
    fvgs = detect_fvgs(candles)
    # All detected FVGs in this dataset stay active
    assert all(f.status == "ACTIVE" for f in fvgs)
    assert active_fvgs(fvgs) == fvgs


# ── Queries ─────────────────────────────────────────────────────────────────


def test_nearest_fvg_filters_by_side_and_direction():
    fvgs = [
        FVG(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=1),
        FVG(direction=Direction.BULLISH, bottom=120, top=125, origin_bar=5),
        FVG(direction=Direction.BEARISH, bottom=95, top=98, origin_bar=10),
    ]
    # Below price 110 → first bullish
    n = nearest_fvg(fvgs, price=110, direction=Direction.BULLISH, side="below")
    assert n is not None
    assert n.bottom == 100
    # Above price 110 → second bullish
    n = nearest_fvg(fvgs, price=110, direction=Direction.BULLISH, side="above")
    assert n is not None
    assert n.bottom == 120
    # Bearish FVG below price
    n = nearest_fvg(fvgs, price=110, direction=Direction.BEARISH)
    assert n is not None
    assert n.direction == Direction.BEARISH


def test_nearest_fvg_ignores_mitigated():
    fvgs = [
        FVG(direction=Direction.BULLISH, bottom=100, top=105,
            origin_bar=1, status="MITIGATED"),
        FVG(direction=Direction.BULLISH, bottom=120, top=125, origin_bar=5),
    ]
    n = nearest_fvg(fvgs, price=110)
    assert n is not None
    assert n.bottom == 120  # mitigated one skipped


def test_price_in_fvg():
    fvgs = [
        FVG(direction=Direction.BULLISH, bottom=100, top=105, origin_bar=1),
    ]
    assert price_in_fvg(fvgs, 103) is not None
    assert price_in_fvg(fvgs, 110) is None
    # direction filter
    assert price_in_fvg(fvgs, 103, direction=Direction.BEARISH) is None


def test_fvg_contains_and_midpoint():
    fvg = FVG(direction=Direction.BULLISH, bottom=100, top=110, origin_bar=1)
    assert fvg.contains(105)
    assert not fvg.contains(99)
    assert fvg.midpoint == 105
    assert fvg.size == 10
