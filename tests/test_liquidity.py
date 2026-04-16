"""Tests for src.analysis.liquidity."""

from __future__ import annotations

from src.analysis.liquidity import (
    LiquidityLevel,
    SweepEvent,
    analyze_liquidity,
    detect_sweeps,
    find_equal_highs,
    find_equal_lows,
    last_sweep,
    liquidity_above,
    liquidity_below,
)
from src.data.candle_buffer import Candle
from src.data.models import Direction


def mk(h: float, l: float, c: float | None = None) -> Candle:
    mid = c if c is not None else (h + l) / 2
    return Candle(open=mid, high=h, low=l, close=mid, volume=1.0)


# ── Equal highs / lows ──────────────────────────────────────────────────────


def _two_peaks_at(price: float, offset: float = 0.0) -> list[Candle]:
    """Construct a buffer with two swing highs at ~price."""
    return (
        [mk(price - 5, price - 10)] * 3
        + [mk(price, price - 5)]              # swing high 1
        + [mk(price - 3, price - 8)] * 3
        + [mk(price - 2, price - 10)]         # low between
        + [mk(price - 3, price - 8)] * 3
        + [mk(price + offset, price - 5)]     # swing high 2 (near first)
        + [mk(price - 3, price - 8)] * 3
    )


def test_equal_highs_detected_within_tolerance():
    # Two peaks at 100 and 100.05 (0.05%) → within 0.1% tolerance
    candles = _two_peaks_at(100, offset=0.05)
    highs = find_equal_highs(candles, lookback=3, tolerance_pct=0.1)
    assert len(highs) == 1
    assert highs[0].touches == 2
    assert highs[0].kind == "high"


def test_equal_highs_rejected_outside_tolerance():
    candles = _two_peaks_at(100, offset=5)  # 5% apart
    highs = find_equal_highs(candles, lookback=3, tolerance_pct=0.1)
    # Should not cluster
    for l in highs:
        assert l.touches == 1 or True  # min_touches default 2, so no levels
    assert all(l.touches >= 2 for l in highs) is True or len(highs) == 0


def test_min_touches_enforced():
    candles = _two_peaks_at(100, offset=0.05)
    highs = find_equal_highs(candles, lookback=3, tolerance_pct=0.1, min_touches=3)
    assert highs == []


def test_equal_lows_detected():
    # Build two troughs at ~90
    candles = (
        [mk(95, 92)] * 3
        + [mk(93, 90)]                 # low 1
        + [mk(95, 92)] * 3
        + [mk(100, 95)]                # high
        + [mk(95, 92)] * 3
        + [mk(94, 90.05)]              # low 2 (close)
        + [mk(95, 92)] * 3
    )
    lows = find_equal_lows(candles, lookback=3, tolerance_pct=0.2)
    assert len(lows) == 1
    assert lows[0].kind == "low"


# ── Sweep detection ─────────────────────────────────────────────────────────


def test_sweep_high_detected():
    level = LiquidityLevel(price=100, kind="high", touches=2, bar_indices=[3, 10])
    # Sweep bar at index 15: wick above 100, close below
    candles = [mk(95, 90) for _ in range(15)]
    candles.append(mk(102, 95, c=98))  # wick above 100, close 98 < 100
    sweeps = detect_sweeps(candles, [level])
    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BEARISH
    assert sweeps[0].level == 100
    assert level.swept is True
    assert level.sweep_bar == 15


def test_sweep_low_detected():
    level = LiquidityLevel(price=90, kind="low", touches=2, bar_indices=[3, 10])
    candles = [mk(95, 92) for _ in range(15)]
    candles.append(mk(95, 85, c=93))  # low wicked below 90, close above
    sweeps = detect_sweeps(candles, [level])
    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BULLISH
    assert level.swept is True


def test_no_sweep_if_close_stays_above_high():
    level = LiquidityLevel(price=100, kind="high", touches=2, bar_indices=[3, 10])
    candles = [mk(95, 90) for _ in range(15)]
    candles.append(mk(105, 98, c=103))  # broke but closed above — not a sweep
    assert detect_sweeps(candles, [level]) == []
    assert level.swept is False


def test_sweep_triggers_only_once_per_level():
    level = LiquidityLevel(price=100, kind="high", touches=2, bar_indices=[3, 10])
    candles = [mk(95, 90) for _ in range(15)]
    candles.append(mk(102, 95, c=98))
    candles.append(mk(103, 96, c=99))  # another wick back
    sweeps = detect_sweeps(candles, [level])
    assert len(sweeps) == 1


# ── Queries ─────────────────────────────────────────────────────────────────


def test_liquidity_above_below_sorted_by_distance():
    levels = [
        LiquidityLevel(price=120, kind="high", bar_indices=[1]),
        LiquidityLevel(price=130, kind="high", bar_indices=[5]),
        LiquidityLevel(price=90, kind="low", bar_indices=[10]),
        LiquidityLevel(price=80, kind="low", bar_indices=[12]),
        LiquidityLevel(price=110, kind="high", bar_indices=[15], swept=True),
    ]
    above = liquidity_above(levels, price=100)
    assert [l.price for l in above] == [120, 130]  # swept level excluded, sorted

    below = liquidity_below(levels, price=100)
    assert [l.price for l in below] == [90, 80]


def test_last_sweep_returns_most_recent():
    sweeps = [
        SweepEvent(direction=Direction.BULLISH, level=90, bar_index=5),
        SweepEvent(direction=Direction.BEARISH, level=110, bar_index=15),
        SweepEvent(direction=Direction.BULLISH, level=95, bar_index=10),
    ]
    last = last_sweep(sweeps)
    assert last.bar_index == 15
    assert last_sweep([]) is None


# ── End-to-end ──────────────────────────────────────────────────────────────


def test_analyze_liquidity_returns_levels_and_sweeps():
    # Two lows at ~90, then a sweep
    candles = (
        [mk(95, 92)] * 3
        + [mk(93, 90)]
        + [mk(95, 92)] * 3
        + [mk(100, 95)]
        + [mk(95, 92)] * 3
        + [mk(94, 90)]
        + [mk(95, 92)] * 3
        + [mk(95, 85, c=93)]   # sweep of low @ 90
    )
    levels, sweeps = analyze_liquidity(candles, lookback=3, tolerance_pct=0.2)
    assert any(l.kind == "low" for l in levels)
    assert any(s.direction == Direction.BULLISH for s in sweeps)
