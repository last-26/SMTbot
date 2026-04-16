"""Candlestick pattern detection.

Python-side candlestick pattern detection on OHLCV candle buffers.
Supplements the Pine Script overlay (smt_overlay.pine) with per-candle
pattern recognition used by the strategy engine for entry confluence.

All detectors operate on `Candle` objects from `src.data.candle_buffer`.
Detectors return `CandlePattern` records describing direction, strength
(0.0-1.0), and the source bars. None is returned when no pattern is found.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.candle_buffer import Candle
from src.data.models import Direction


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class CandlePattern:
    """A detected candlestick pattern.

    Attributes:
        name: Human readable pattern name (e.g. "bullish_engulfing").
        direction: BULLISH / BEARISH. Ranging patterns (doji, inside_bar)
            use UNDEFINED.
        strength: 0.0-1.0 confidence score derived from body/wick geometry.
        bar_offset: Offset from the latest candle (0 = latest, 1 = prior).
        price_level: Reference price for SL placement (swing low/high of pattern).
        notes: Free-form detail describing what triggered the pattern.
    """
    name: str
    direction: Direction
    strength: float
    bar_offset: int = 0
    price_level: float = 0.0
    notes: str = ""
    meta: dict = field(default_factory=dict)


# ── Single-candle helpers ───────────────────────────────────────────────────


def _body_ratio(c: Candle) -> float:
    """Body size as a fraction of total range. 0 when range is zero."""
    return c.body_size / c.total_range if c.total_range > 0 else 0.0


def _upper_wick_ratio(c: Candle) -> float:
    return c.upper_wick / c.total_range if c.total_range > 0 else 0.0


def _lower_wick_ratio(c: Candle) -> float:
    return c.lower_wick / c.total_range if c.total_range > 0 else 0.0


# ── Pattern detectors ───────────────────────────────────────────────────────


def detect_doji(candle: Candle, max_body_ratio: float = 0.1) -> Optional[CandlePattern]:
    """Doji: very small body relative to total range (indecision)."""
    if candle.total_range <= 0:
        return None
    body_r = _body_ratio(candle)
    if body_r > max_body_ratio:
        return None
    return CandlePattern(
        name="doji",
        direction=Direction.UNDEFINED,
        strength=1.0 - (body_r / max_body_ratio),
        price_level=candle.close,
        notes=f"body_ratio={body_r:.3f}",
    )


def detect_hammer(candle: Candle) -> Optional[CandlePattern]:
    """Hammer: long lower wick, small body, little upper wick. Bullish reversal."""
    if candle.total_range <= 0:
        return None
    body_r = _body_ratio(candle)
    low_r = _lower_wick_ratio(candle)
    up_r = _upper_wick_ratio(candle)
    if low_r < 0.55 or up_r > 0.2 or body_r > 0.4:
        return None
    strength = min(1.0, (low_r - 0.5) * 2)
    return CandlePattern(
        name="hammer",
        direction=Direction.BULLISH,
        strength=max(0.1, strength),
        price_level=candle.low,
        notes=f"lower_wick_ratio={low_r:.3f}",
    )


def detect_shooting_star(candle: Candle) -> Optional[CandlePattern]:
    """Shooting star: long upper wick, small body, little lower wick. Bearish reversal."""
    if candle.total_range <= 0:
        return None
    body_r = _body_ratio(candle)
    up_r = _upper_wick_ratio(candle)
    low_r = _lower_wick_ratio(candle)
    if up_r < 0.55 or low_r > 0.2 or body_r > 0.4:
        return None
    strength = min(1.0, (up_r - 0.5) * 2)
    return CandlePattern(
        name="shooting_star",
        direction=Direction.BEARISH,
        strength=max(0.1, strength),
        price_level=candle.high,
        notes=f"upper_wick_ratio={up_r:.3f}",
    )


def detect_pin_bar(candle: Candle) -> Optional[CandlePattern]:
    """Pin bar: either a hammer or shooting star. Convenience wrapper."""
    h = detect_hammer(candle)
    if h:
        h.name = "pin_bar_bull"
        return h
    s = detect_shooting_star(candle)
    if s:
        s.name = "pin_bar_bear"
        return s
    return None


def detect_engulfing(prev: Candle, curr: Candle) -> Optional[CandlePattern]:
    """Engulfing: current candle body fully engulfs the prior body.

    Bullish: prev bearish, curr bullish, curr body spans prev body.
    Bearish: prev bullish, curr bearish, curr body spans prev body.
    """
    if curr.total_range <= 0 or prev.total_range <= 0:
        return None

    prev_body_top = max(prev.open, prev.close)
    prev_body_bot = min(prev.open, prev.close)
    curr_body_top = max(curr.open, curr.close)
    curr_body_bot = min(curr.open, curr.close)

    # Must fully engulf the prior body
    engulfs = curr_body_top >= prev_body_top and curr_body_bot <= prev_body_bot
    if not engulfs:
        return None

    if prev.is_bearish and curr.is_bullish:
        direction = Direction.BULLISH
        name = "bullish_engulfing"
        price_level = curr.low
    elif prev.is_bullish and curr.is_bearish:
        direction = Direction.BEARISH
        name = "bearish_engulfing"
        price_level = curr.high
    else:
        return None

    # Strength: size of current body relative to prev body
    if prev.body_size > 0:
        ratio = curr.body_size / prev.body_size
        strength = min(1.0, (ratio - 1.0) * 0.5 + 0.5)
    else:
        strength = 0.6
    return CandlePattern(
        name=name,
        direction=direction,
        strength=max(0.3, strength),
        price_level=price_level,
        notes=f"body_ratio={curr.body_size / max(prev.body_size, 1e-9):.2f}",
    )


def detect_inside_bar(prev: Candle, curr: Candle) -> Optional[CandlePattern]:
    """Inside bar: current candle fully contained within prior candle's range."""
    if curr.high < prev.high and curr.low > prev.low:
        return CandlePattern(
            name="inside_bar",
            direction=Direction.UNDEFINED,
            strength=0.5,
            price_level=curr.close,
            notes="consolidation/compression",
        )
    return None


def detect_morning_star(c1: Candle, c2: Candle, c3: Candle) -> Optional[CandlePattern]:
    """Morning star: bearish → small body (star) → bullish close into c1 body.

    Bullish 3-candle reversal pattern at lows.
    """
    if not c1.is_bearish:
        return None
    # Star body should be small
    if _body_ratio(c2) > 0.35:
        return None
    if not c3.is_bullish:
        return None
    # c3 should close into the upper half of c1's body
    c1_mid = (c1.open + c1.close) / 2
    if c3.close < c1_mid:
        return None
    return CandlePattern(
        name="morning_star",
        direction=Direction.BULLISH,
        strength=0.8,
        price_level=min(c1.low, c2.low, c3.low),
        notes="3-bar reversal at lows",
    )


def detect_evening_star(c1: Candle, c2: Candle, c3: Candle) -> Optional[CandlePattern]:
    """Evening star: bullish → small body (star) → bearish close into c1 body.

    Bearish 3-candle reversal pattern at highs.
    """
    if not c1.is_bullish:
        return None
    if _body_ratio(c2) > 0.35:
        return None
    if not c3.is_bearish:
        return None
    c1_mid = (c1.open + c1.close) / 2
    if c3.close > c1_mid:
        return None
    return CandlePattern(
        name="evening_star",
        direction=Direction.BEARISH,
        strength=0.8,
        price_level=max(c1.high, c2.high, c3.high),
        notes="3-bar reversal at highs",
    )


# ── Aggregator ──────────────────────────────────────────────────────────────


def detect_all_patterns(candles: list[Candle]) -> list[CandlePattern]:
    """Run all pattern detectors against the tail of a candle buffer.

    Inspects the last 3 candles and returns every pattern hit found across
    single-candle, 2-candle, and 3-candle detectors. The most recent candle
    (offset=0) is the "current" bar.
    """
    patterns: list[CandlePattern] = []
    n = len(candles)
    if n == 0:
        return patterns

    curr = candles[-1]

    # Single-candle patterns on the current bar
    for detector in (detect_doji, detect_hammer, detect_shooting_star):
        hit = detector(curr)
        if hit:
            hit.bar_offset = 0
            patterns.append(hit)

    # Two-candle patterns
    if n >= 2:
        prev = candles[-2]
        eng = detect_engulfing(prev, curr)
        if eng:
            eng.bar_offset = 0
            patterns.append(eng)
        inside = detect_inside_bar(prev, curr)
        if inside:
            inside.bar_offset = 0
            patterns.append(inside)

    # Three-candle patterns
    if n >= 3:
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        for detector in (detect_morning_star, detect_evening_star):
            hit = detector(c1, c2, c3)
            if hit:
                hit.bar_offset = 0
                patterns.append(hit)

    return patterns


def has_entry_pattern(candles: list[Candle], direction: Direction) -> bool:
    """Convenience: does the tail of the buffer contain a pattern in `direction`?"""
    if direction == Direction.UNDEFINED:
        return False
    for p in detect_all_patterns(candles):
        if p.direction == direction and p.strength >= 0.4:
            return True
    return False
