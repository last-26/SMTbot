"""Liquidity analysis: equal highs/lows and sweep detection.

Liquidity pools form at price levels where many stop orders cluster — typically
equal highs (buy-stops resting above) and equal lows (sell-stops resting below).
Price often reaches for these levels (a "sweep") and then reverses, because
smart money uses the clustered liquidity as fuel for a move in the opposite
direction.

- LiquidityLevel: a price level with >= N touches within a tolerance band.
- SweepEvent: a wick that punctured a liquidity level but the candle closed
  back on the original side (liquidity grab without continuation).

Supplements smt_overlay.pine / liquidity_sweep.pine for HTF analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.candle_buffer import Candle
from src.data.models import Direction


@dataclass
class LiquidityLevel:
    """A price level where multiple swing highs/lows cluster."""
    price: float
    kind: str                         # "high" or "low"
    touches: int = 2
    bar_indices: list[int] = field(default_factory=list)
    swept: bool = False
    sweep_bar: Optional[int] = None

    @property
    def side(self) -> str:
        """Used by external callers — "above" for highs, "below" for lows."""
        return "above" if self.kind == "high" else "below"


@dataclass
class SweepEvent:
    """A liquidity sweep (wick punctures level then closes back)."""
    direction: Direction    # BULLISH sweep = swept lows (reversal up);
                            # BEARISH sweep = swept highs (reversal down)
    level: float
    bar_index: int
    touches: int = 0


# ── Equal H/L detection ─────────────────────────────────────────────────────


def _cluster_levels(
    points: list[tuple[int, float]],
    tolerance_pct: float,
) -> list[tuple[float, list[int]]]:
    """Greedy-cluster a list of (bar_index, price) points by relative tolerance.

    Returns list of (avg_price, bar_indices) for each cluster.
    """
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda p: p[1])
    clusters: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = [sorted_pts[0]]
    for bar_i, price in sorted_pts[1:]:
        ref = current[-1][1]
        if ref > 0 and abs(price - ref) / ref * 100 <= tolerance_pct:
            current.append((bar_i, price))
        else:
            clusters.append(current)
            current = [(bar_i, price)]
    clusters.append(current)

    out: list[tuple[float, list[int]]] = []
    for cl in clusters:
        avg = sum(p for _, p in cl) / len(cl)
        out.append((avg, [b for b, _ in cl]))
    return out


def find_equal_highs(
    candles: list[Candle],
    lookback: int = 3,
    tolerance_pct: float = 0.1,
    min_touches: int = 2,
) -> list[LiquidityLevel]:
    """Detect clusters of swing highs at roughly the same price."""
    from src.analysis.market_structure import find_swing_points
    swings = find_swing_points(candles, lookback=lookback)
    highs = [(s.bar_index, s.price) for s in swings if s.kind == "high"]

    clusters = _cluster_levels(highs, tolerance_pct)
    return [
        LiquidityLevel(price=avg, kind="high", touches=len(bars), bar_indices=bars)
        for avg, bars in clusters
        if len(bars) >= min_touches
    ]


def find_equal_lows(
    candles: list[Candle],
    lookback: int = 3,
    tolerance_pct: float = 0.1,
    min_touches: int = 2,
) -> list[LiquidityLevel]:
    """Detect clusters of swing lows at roughly the same price."""
    from src.analysis.market_structure import find_swing_points
    swings = find_swing_points(candles, lookback=lookback)
    lows = [(s.bar_index, s.price) for s in swings if s.kind == "low"]

    clusters = _cluster_levels(lows, tolerance_pct)
    return [
        LiquidityLevel(price=avg, kind="low", touches=len(bars), bar_indices=bars)
        for avg, bars in clusters
        if len(bars) >= min_touches
    ]


# ── Sweep detection ─────────────────────────────────────────────────────────


def detect_sweeps(
    candles: list[Candle],
    levels: list[LiquidityLevel],
) -> list[SweepEvent]:
    """Return sweep events where a candle wicks past a level but closes back.

    Bullish sweep = swept lows (wick below the level, close above).
    Bearish sweep = swept highs (wick above the level, close below).
    Mutates levels in-place to mark .swept / .sweep_bar on the first sweep.
    """
    events: list[SweepEvent] = []
    for level in levels:
        last_bar = max(level.bar_indices) if level.bar_indices else -1
        for i in range(last_bar + 1, len(candles)):
            cand = candles[i]
            if level.kind == "high":
                # Need wick above, close below
                if cand.high > level.price and cand.close < level.price:
                    events.append(SweepEvent(
                        direction=Direction.BEARISH,
                        level=level.price,
                        bar_index=i,
                        touches=level.touches,
                    ))
                    level.swept = True
                    level.sweep_bar = i
                    break
            else:  # low
                if cand.low < level.price and cand.close > level.price:
                    events.append(SweepEvent(
                        direction=Direction.BULLISH,
                        level=level.price,
                        bar_index=i,
                        touches=level.touches,
                    ))
                    level.swept = True
                    level.sweep_bar = i
                    break
    return events


# ── Query helpers ───────────────────────────────────────────────────────────


def liquidity_above(levels: list[LiquidityLevel], price: float) -> list[LiquidityLevel]:
    """Unswept high-side levels above `price`, sorted nearest-first."""
    out = [l for l in levels if l.kind == "high" and not l.swept and l.price > price]
    out.sort(key=lambda l: l.price - price)
    return out


def liquidity_below(levels: list[LiquidityLevel], price: float) -> list[LiquidityLevel]:
    """Unswept low-side levels below `price`, sorted nearest-first."""
    out = [l for l in levels if l.kind == "low" and not l.swept and l.price < price]
    out.sort(key=lambda l: price - l.price)
    return out


def last_sweep(sweeps: list[SweepEvent]) -> Optional[SweepEvent]:
    """Return the most recent sweep event, if any."""
    if not sweeps:
        return None
    return max(sweeps, key=lambda s: s.bar_index)


def analyze_liquidity(
    candles: list[Candle],
    lookback: int = 3,
    tolerance_pct: float = 0.1,
    min_touches: int = 2,
) -> tuple[list[LiquidityLevel], list[SweepEvent]]:
    """Convenience: detect EQH/EQL and their sweeps in one call."""
    highs = find_equal_highs(candles, lookback, tolerance_pct, min_touches)
    lows = find_equal_lows(candles, lookback, tolerance_pct, min_touches)
    levels = highs + lows
    sweeps = detect_sweeps(candles, levels)
    return levels, sweeps
