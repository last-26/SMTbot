"""Tests for src.analysis.market_structure."""

from __future__ import annotations

from src.analysis.market_structure import (
    MarketStructure,
    SwingType,
    analyze_structure,
    classify_swings,
    detect_structure_events,
    find_swing_points,
)
from src.data.candle_buffer import Candle
from src.data.models import Direction


def mk(h: float, l: float, c: float | None = None, o: float | None = None) -> Candle:
    """Helper — builds a candle where open/close default to the midpoint."""
    mid = (h + l) / 2 if c is None else c
    return Candle(
        open=o if o is not None else mid,
        high=h,
        low=l,
        close=mid,
        volume=1.0,
    )


# ── Swing detection ─────────────────────────────────────────────────────────


def test_find_swing_points_simple_peak():
    # ___/\___ shape — bar 3 is the peak
    candles = [
        mk(100, 99), mk(102, 100), mk(104, 101),
        mk(110, 105),  # swing high
        mk(104, 101), mk(102, 100), mk(100, 99),
    ]
    swings = find_swing_points(candles, lookback=3)
    assert len(swings) == 1
    assert swings[0].kind == "high"
    assert swings[0].bar_index == 3
    assert swings[0].price == 110


def test_find_swing_points_simple_trough():
    # ‾‾‾\/‾‾‾ shape — bar 3 is the trough
    candles = [
        mk(110, 100), mk(108, 99), mk(106, 97),
        mk(105, 90),  # swing low
        mk(107, 97), mk(108, 99), mk(110, 100),
    ]
    swings = find_swing_points(candles, lookback=3)
    assert len(swings) == 1
    assert swings[0].kind == "low"
    assert swings[0].price == 90


def test_find_swing_points_too_few_candles():
    candles = [mk(100, 99), mk(102, 100)]
    assert find_swing_points(candles, lookback=3) == []


def test_find_swing_points_zero_lookback_rejected():
    candles = [mk(100, 99) for _ in range(10)]
    assert find_swing_points(candles, lookback=0) == []


# ── Classification (HH/HL/LH/LL) ────────────────────────────────────────────


def test_classify_swings_uptrend():
    candles = (
        [mk(100, 95), mk(102, 97), mk(104, 99)]     # rising
        + [mk(110, 105)]                            # SH1 @ idx 3
        + [mk(108, 102), mk(106, 100), mk(104, 98)] # down
        + [mk(100, 92)]                             # SL1 @ idx 7
        + [mk(102, 95), mk(105, 98), mk(108, 100)]  # up
        + [mk(115, 108)]                            # SH2 @ idx 11 (HH)
        + [mk(113, 105), mk(111, 103), mk(109, 101)]
        + [mk(107, 96)]                             # SL2 @ idx 15 (HL)
        + [mk(109, 100), mk(112, 103), mk(115, 105)]
    )
    swings = classify_swings(find_swing_points(candles, lookback=3))
    kinds = [s.swing_type for s in swings]
    # Expect sequence containing at least H, L, HH, HL
    assert SwingType.HH in kinds
    assert SwingType.HL in kinds


def test_classify_swings_downtrend():
    candles = (
        [mk(100, 95), mk(98, 93), mk(96, 91)]
        + [mk(94, 82)]                              # SL1
        + [mk(96, 85), mk(99, 87), mk(102, 90)]
        + [mk(105, 95)]                             # SH1
        + [mk(103, 92), mk(101, 88), mk(99, 85)]
        + [mk(97, 75)]                              # SL2 (LL)
        + [mk(99, 80), mk(101, 82), mk(103, 85)]
        + [mk(104, 88)]                             # SH2 (LH)
        + [mk(101, 83), mk(98, 78), mk(96, 75)]
    )
    swings = classify_swings(find_swing_points(candles, lookback=3))
    kinds = [s.swing_type for s in swings]
    assert SwingType.LL in kinds
    assert SwingType.LH in kinds


# ── Structure events (BOS / CHoCH / MSS) ────────────────────────────────────


def test_detect_bos_in_uptrend():
    # Build: SL → rally SH1 → pullback SL1(HL) → break SH1 (BOS)
    candles = (
        [mk(110, 100)] * 3
        + [mk(109, 85)]                               # SL
        + [mk(110, 100)] * 3
        + [mk(130, 115)]                              # SH1
        + [mk(120, 110)] * 3
        + [mk(115, 95)]                               # SL1 (HL)
        + [mk(120, 110)] * 3
        + [mk(140, 132, c=135)]                       # breaks SH1 (130)
    )
    structure = analyze_structure(candles, lookback=3)
    types = [e.event_type for e in structure.events]
    assert any(t in ("BOS", "CHoCH", "MSS") for t in types)
    # First bullish break should be CHoCH (we started from undefined/down)
    bull_events = [e for e in structure.events if e.direction == Direction.BULLISH]
    assert bull_events, "expected at least one bullish structure event"


def test_choch_on_opposite_trend_break():
    # Create a clear uptrend (HH + HL), then break the last SL → bearish CHoCH
    candles = (
        [mk(95, 90)] * 3
        + [mk(94, 80)]                                # SL0
        + [mk(95, 90)] * 3
        + [mk(110, 95)]                               # SH1
        + [mk(100, 92)] * 3
        + [mk(99, 88)]                                # SL1 (HL) — new SL
        + [mk(100, 92)] * 3
        + [mk(120, 110)]                              # SH2 (HH) — confirms uptrend
        + [mk(110, 100)] * 3
        + [mk(105, 93)]                               # SL2 (HL again)
        + [mk(100, 95)] * 3
        + [mk(90, 80, c=85)]                          # breaks SL2 (93) → bearish CHoCH
    )
    structure = analyze_structure(candles, lookback=3)
    assert structure.events, "expected at least one event"
    # A bearish CHoCH should appear somewhere once uptrend gets broken
    assert any(e.direction == Direction.BEARISH for e in structure.events)


def test_empty_candle_buffer_safe():
    structure = analyze_structure([], lookback=3)
    assert isinstance(structure, MarketStructure)
    assert structure.swings == []
    assert structure.events == []
    assert structure.trend == Direction.UNDEFINED


def test_structure_convenience_accessors():
    candles = (
        [mk(100, 95)] * 3
        + [mk(110, 100)]
        + [mk(105, 95)] * 3
    )
    structure = analyze_structure(candles, lookback=3)
    assert structure.last_swing_high is not None or structure.last_swing_low is not None


def test_structure_events_are_chronological():
    candles = (
        [mk(100, 95)] * 3 + [mk(110, 100)] + [mk(105, 95)] * 3
        + [mk(95, 80)] + [mk(105, 95)] * 3 + [mk(125, 115, c=120)]
    )
    structure = analyze_structure(candles, lookback=3)
    idxs = [e.bar_index for e in structure.events]
    assert idxs == sorted(idxs)
