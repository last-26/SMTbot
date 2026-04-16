"""Market structure analysis: swing points, BOS, CHoCH, MSS.

Python-side swing high/low detection and trend structure classification.
Supplements the Pine Script (smt_overlay.pine) master table — the Pine
Script is the primary source of structure, this module is used when the
bot needs to recompute structure from a candle buffer directly (e.g.
for multi-timeframe analysis on a timeframe other than the active chart).

Key concepts:
- Swing High (SH): a candle whose high is higher than N bars left and right
- Swing Low (SL): a candle whose low is lower than N bars left and right
- HH/HL/LH/LL: higher-high / higher-low / lower-high / lower-low
- BOS (Break of Structure): price breaks the most recent SH in uptrend,
  or the most recent SL in downtrend (trend continuation).
- CHoCH (Change of Character): first break against the current trend
  (early reversal signal).
- MSS (Market Structure Shift): a CHoCH that is confirmed by subsequent
  new swing formation in the new direction (full reversal).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.data.candle_buffer import Candle
from src.data.models import Direction


class SwingType(str, Enum):
    HH = "HH"  # higher high
    HL = "HL"  # higher low
    LH = "LH"  # lower high
    LL = "LL"  # lower low
    H = "H"    # first recorded swing high
    L = "L"    # first recorded swing low


@dataclass
class SwingPoint:
    """A detected swing high or low."""
    bar_index: int            # index into the candle buffer
    price: float
    kind: str                 # "high" or "low"
    swing_type: SwingType = SwingType.H  # HH, HL, LH, LL (when classified)


@dataclass
class StructureEvent:
    """A structure break event (BOS / CHoCH / MSS)."""
    event_type: str           # "BOS", "CHoCH", or "MSS"
    direction: Direction
    price: float              # the level that was broken
    bar_index: int            # bar that broke it


@dataclass
class MarketStructure:
    """Summary of a candle buffer's structure."""
    swings: list[SwingPoint]
    events: list[StructureEvent]
    trend: Direction          # current trend after last events

    @property
    def last_event(self) -> Optional[StructureEvent]:
        return self.events[-1] if self.events else None

    @property
    def last_swing_high(self) -> Optional[SwingPoint]:
        for s in reversed(self.swings):
            if s.kind == "high":
                return s
        return None

    @property
    def last_swing_low(self) -> Optional[SwingPoint]:
        for s in reversed(self.swings):
            if s.kind == "low":
                return s
        return None


# ── Swing detection ─────────────────────────────────────────────────────────


def find_swing_points(candles: list[Candle], lookback: int = 3) -> list[SwingPoint]:
    """Detect swing highs and swing lows using a fractal-style filter.

    A candle at index i is a swing high if its `high` is >= all candles in
    [i-lookback, i+lookback] (excluding i). Swing lows are symmetric.

    The last `lookback` candles can never be confirmed (not enough right-side
    data), so they are excluded from the results.
    """
    swings: list[SwingPoint] = []
    if lookback < 1 or len(candles) < (2 * lookback + 1):
        return swings

    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback:i + lookback + 1]
        center = candles[i]
        max_h = max(c.high for c in window)
        min_l = min(c.low for c in window)
        if center.high >= max_h and center.high > max(
            c.high for j, c in enumerate(window) if j != lookback
        ):
            swings.append(SwingPoint(bar_index=i, price=center.high, kind="high"))
        elif center.low <= min_l and center.low < min(
            c.low for j, c in enumerate(window) if j != lookback
        ):
            swings.append(SwingPoint(bar_index=i, price=center.low, kind="low"))

    return swings


def classify_swings(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Annotate each swing with HH/HL/LH/LL based on the previous same-kind swing.

    Mutates the `swing_type` field in place and returns the same list.
    """
    last_high: Optional[SwingPoint] = None
    last_low: Optional[SwingPoint] = None
    for s in swings:
        if s.kind == "high":
            if last_high is None:
                s.swing_type = SwingType.H
            elif s.price > last_high.price:
                s.swing_type = SwingType.HH
            else:
                s.swing_type = SwingType.LH
            last_high = s
        else:  # low
            if last_low is None:
                s.swing_type = SwingType.L
            elif s.price > last_low.price:
                s.swing_type = SwingType.HL
            else:
                s.swing_type = SwingType.LL
            last_low = s
    return swings


# ── Trend & structure events ────────────────────────────────────────────────


def _infer_trend_from_swings(swings: list[SwingPoint]) -> Direction:
    """Return a coarse trend direction from the swing pattern.

    Bullish when most recent same-kind swings show HH+HL; bearish when LH+LL.
    RANGING otherwise.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return Direction.UNDEFINED

    last_high = highs[-1]
    last_low = lows[-1]

    bull = last_high.swing_type == SwingType.HH and last_low.swing_type == SwingType.HL
    bear = last_high.swing_type == SwingType.LH and last_low.swing_type == SwingType.LL
    if bull and not bear:
        return Direction.BULLISH
    if bear and not bull:
        return Direction.BEARISH
    return Direction.RANGING


def detect_structure_events(
    candles: list[Candle],
    swings: list[SwingPoint],
) -> list[StructureEvent]:
    """Walk the candle buffer forward and emit BOS / CHoCH / MSS events.

    Algorithm:
    - Maintain a "current trend" (starts UNDEFINED).
    - Track the most recent confirmed swing high (SH) and swing low (SL).
    - When close breaks above SH in uptrend → BOS bull.
    - When close breaks above SH in downtrend/undefined → CHoCH bull (reversal signal).
    - Symmetric for bearish side.
    - An MSS is a CHoCH that is followed by a new opposite-direction swing
      formation (handled by promoting the CHoCH to MSS in a second pass).

    Returns events in chronological order.
    """
    events: list[StructureEvent] = []
    if not candles or not swings:
        return events

    # Pre-sort swings by bar_index just in case
    swings_sorted = sorted(swings, key=lambda s: s.bar_index)

    current_trend: Direction = Direction.UNDEFINED
    last_sh: Optional[SwingPoint] = None
    last_sl: Optional[SwingPoint] = None

    # Index pointer into swings list that have been "confirmed" by the bar we're on
    swing_ptr = 0

    for bar_i, candle in enumerate(candles):
        # Confirm swings whose bar_index has now been seen
        while swing_ptr < len(swings_sorted) and swings_sorted[swing_ptr].bar_index <= bar_i:
            s = swings_sorted[swing_ptr]
            if s.kind == "high":
                last_sh = s
            else:
                last_sl = s
            swing_ptr += 1

        # Detect break of last_sh (bullish break)
        if last_sh is not None and candle.close > last_sh.price and bar_i > last_sh.bar_index:
            if current_trend == Direction.BULLISH:
                evt_type = "BOS"
            else:
                evt_type = "CHoCH"
                current_trend = Direction.BULLISH
            events.append(StructureEvent(
                event_type=evt_type,
                direction=Direction.BULLISH,
                price=last_sh.price,
                bar_index=bar_i,
            ))
            # Invalidate this SH so we don't retrigger on the same level
            last_sh = None

        # Detect break of last_sl (bearish break)
        if last_sl is not None and candle.close < last_sl.price and bar_i > last_sl.bar_index:
            if current_trend == Direction.BEARISH:
                evt_type = "BOS"
            else:
                evt_type = "CHoCH"
                current_trend = Direction.BEARISH
            events.append(StructureEvent(
                event_type=evt_type,
                direction=Direction.BEARISH,
                price=last_sl.price,
                bar_index=bar_i,
            ))
            last_sl = None

    # Second pass: promote CHoCH → MSS when a new opposite-direction swing
    # has been confirmed after the CHoCH (i.e., structure has shifted).
    for i, evt in enumerate(events):
        if evt.event_type != "CHoCH":
            continue
        # Is there a swing in `evt.direction` sense after this bar?
        # Bullish CHoCH → need a new HL after (higher low forming the new trend)
        # Bearish CHoCH → need a new LH after
        needed_kind = "low" if evt.direction == Direction.BULLISH else "high"
        for s in swings_sorted:
            if s.bar_index <= evt.bar_index:
                continue
            if s.kind == needed_kind:
                evt.event_type = "MSS"
                break

    return events


def analyze_structure(
    candles: list[Candle],
    lookback: int = 3,
) -> MarketStructure:
    """Full structure analysis: swings + classification + events + trend."""
    swings = classify_swings(find_swing_points(candles, lookback=lookback))
    events = detect_structure_events(candles, swings)
    trend = _infer_trend_from_swings(swings)

    # If the most recent event is a confirmed MSS, prefer its direction
    if events:
        last = events[-1]
        if last.event_type in ("MSS", "BOS"):
            trend = last.direction

    return MarketStructure(swings=swings, events=events, trend=trend)
