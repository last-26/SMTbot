"""Tests for src.analysis.price_action candlestick detectors."""

from __future__ import annotations

from src.analysis.price_action import (
    CandlePattern,
    detect_all_patterns,
    detect_doji,
    detect_engulfing,
    detect_evening_star,
    detect_hammer,
    detect_inside_bar,
    detect_morning_star,
    detect_pin_bar,
    detect_shooting_star,
    has_entry_pattern,
)
from src.data.candle_buffer import Candle
from src.data.models import Direction


def mk(o: float, h: float, l: float, c: float, v: float = 1.0) -> Candle:
    return Candle(open=o, high=h, low=l, close=c, volume=v)


# ── Doji ────────────────────────────────────────────────────────────────────


def test_doji_detects_small_body():
    # Body = 1, range = 20 → ratio = 0.05 (well under 0.1)
    candle = mk(100, 110, 90, 100.5)
    pat = detect_doji(candle)
    assert pat is not None
    assert pat.name == "doji"
    assert pat.direction == Direction.UNDEFINED


def test_doji_ignores_large_body():
    candle = mk(100, 110, 95, 108)  # large body
    assert detect_doji(candle) is None


def test_doji_handles_zero_range():
    candle = mk(100, 100, 100, 100)
    assert detect_doji(candle) is None


# ── Hammer / Shooting Star ──────────────────────────────────────────────────


def test_hammer_detects_long_lower_wick():
    # Long lower wick, small upper wick, small body at top
    candle = mk(o=105, h=106, l=90, c=106)  # range 16, lower wick 15
    pat = detect_hammer(candle)
    assert pat is not None
    assert pat.direction == Direction.BULLISH
    assert pat.price_level == 90


def test_hammer_rejects_long_upper_wick():
    candle = mk(o=100, h=120, l=99, c=101)
    assert detect_hammer(candle) is None


def test_shooting_star_detects_long_upper_wick():
    candle = mk(o=95, h=110, l=94, c=94)  # long upper wick
    pat = detect_shooting_star(candle)
    assert pat is not None
    assert pat.direction == Direction.BEARISH
    assert pat.price_level == 110


def test_pin_bar_returns_hammer_or_star():
    hammer = mk(o=105, h=106, l=90, c=106)
    star = mk(o=95, h=110, l=94, c=94)
    assert detect_pin_bar(hammer).name.startswith("pin_bar")
    assert detect_pin_bar(star).name.startswith("pin_bar")


# ── Engulfing ───────────────────────────────────────────────────────────────


def test_bullish_engulfing():
    prev = mk(o=105, h=106, l=100, c=101)      # bearish body 101-105
    curr = mk(o=100, h=108, l=99, c=107)       # bullish body 100-107 (engulfs)
    pat = detect_engulfing(prev, curr)
    assert pat is not None
    assert pat.name == "bullish_engulfing"
    assert pat.direction == Direction.BULLISH


def test_bearish_engulfing():
    prev = mk(o=100, h=103, l=99, c=102)       # bullish body 100-102
    curr = mk(o=103, h=104, l=98, c=99)        # bearish body 99-103 (engulfs)
    pat = detect_engulfing(prev, curr)
    assert pat is not None
    assert pat.name == "bearish_engulfing"
    assert pat.direction == Direction.BEARISH


def test_engulfing_requires_opposite_colors():
    prev = mk(o=100, h=103, l=99, c=102)       # bullish
    curr = mk(o=99, h=105, l=98, c=103)        # bullish too
    assert detect_engulfing(prev, curr) is None


def test_engulfing_requires_full_body_overlap():
    prev = mk(o=105, h=106, l=100, c=101)
    curr = mk(o=102, h=106, l=101, c=104)      # does NOT fully engulf
    assert detect_engulfing(prev, curr) is None


# ── Inside bar ──────────────────────────────────────────────────────────────


def test_inside_bar_detected():
    prev = mk(o=100, h=110, l=90, c=105)
    curr = mk(o=102, h=108, l=95, c=104)       # inside prev range
    pat = detect_inside_bar(prev, curr)
    assert pat is not None
    assert pat.name == "inside_bar"


def test_inside_bar_rejects_breakout():
    prev = mk(o=100, h=110, l=90, c=105)
    curr = mk(o=105, h=115, l=95, c=113)       # breaks above
    assert detect_inside_bar(prev, curr) is None


# ── Morning / Evening star ──────────────────────────────────────────────────


def test_morning_star():
    c1 = mk(o=110, h=111, l=100, c=101)        # bearish
    c2 = mk(o=101, h=102, l=99, c=100.5)       # small body
    c3 = mk(o=101, h=109, l=100, c=108)        # bullish into c1 body top
    pat = detect_morning_star(c1, c2, c3)
    assert pat is not None
    assert pat.direction == Direction.BULLISH


def test_evening_star():
    c1 = mk(o=100, h=111, l=99, c=110)         # bullish
    c2 = mk(o=110, h=112, l=109, c=110.5)      # small body
    c3 = mk(o=110, h=111, l=101, c=102)        # bearish into c1 body
    pat = detect_evening_star(c1, c2, c3)
    assert pat is not None
    assert pat.direction == Direction.BEARISH


def test_morning_star_rejected_if_c3_weak():
    c1 = mk(o=110, h=111, l=100, c=101)
    c2 = mk(o=101, h=102, l=99, c=100.5)
    c3 = mk(o=101, h=103, l=100, c=102)        # weak close, below c1 midpoint
    assert detect_morning_star(c1, c2, c3) is None


# ── Aggregator / has_entry_pattern ──────────────────────────────────────────


def test_detect_all_patterns_finds_multiple():
    c1 = mk(o=110, h=111, l=100, c=101)
    c2 = mk(o=101, h=102, l=99, c=100.5)
    c3 = mk(o=101, h=109, l=100, c=108)
    patterns = detect_all_patterns([c1, c2, c3])
    names = {p.name for p in patterns}
    # Morning star should appear from 3-candle scan
    assert "morning_star" in names


def test_has_entry_pattern_bullish():
    c1 = mk(o=110, h=111, l=100, c=101)
    c2 = mk(o=101, h=102, l=99, c=100.5)
    c3 = mk(o=101, h=109, l=100, c=108)
    assert has_entry_pattern([c1, c2, c3], Direction.BULLISH) is True
    assert has_entry_pattern([c1, c2, c3], Direction.BEARISH) is False


def test_empty_buffer_returns_no_patterns():
    assert detect_all_patterns([]) == []
