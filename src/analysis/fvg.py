"""Fair Value Gap (FVG) detection.

A Fair Value Gap is a 3-candle imbalance where candle 2 moves so aggressively
that the wicks of candle 1 and candle 3 do not overlap. These zones often act
as magnets — price tends to revisit them before continuing.

- Bullish FVG: candle 1's high < candle 3's low → gap is [c1.high, c3.low]
- Bearish FVG: candle 1's low > candle 3's high → gap is [c3.high, c1.low]

Supplements the Pine Script (fvg_mapper.pine / smt_overlay.pine) by letting
the Python bot recompute FVGs on any timeframe directly from the candle
buffer — useful for HTF analysis without switching the chart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data.candle_buffer import Candle
from src.data.models import Direction


@dataclass
class FVG:
    """A Fair Value Gap zone."""
    direction: Direction      # BULLISH or BEARISH
    bottom: float             # lower bound of the gap
    top: float                # upper bound of the gap
    origin_bar: int           # bar index of the middle (impulse) candle
    status: str = "ACTIVE"    # ACTIVE or MITIGATED
    size_pct: float = 0.0     # gap size as percent of origin price
    mitigation_bar: Optional[int] = None  # bar that mitigated it (if any)

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


# ── Detection ───────────────────────────────────────────────────────────────


def detect_fvgs(
    candles: list[Candle],
    min_size_pct: float = 0.0,
) -> list[FVG]:
    """Scan candle buffer and return all FVGs (active + mitigated).

    Args:
        candles: OHLCV candles in chronological order.
        min_size_pct: minimum gap size as percent of c2.close. Tiny gaps
            (< 0.05%) are usually noise.

    Returns:
        List of FVGs in bar order. Each FVG is marked ACTIVE unless a
        later candle's wick has filled the gap, in which case status =
        MITIGATED and mitigation_bar is set.
    """
    fvgs: list[FVG] = []
    if len(candles) < 3:
        return fvgs

    for i in range(1, len(candles) - 1):
        c1, c2, c3 = candles[i - 1], candles[i], candles[i + 1]

        # Bullish FVG: c1.high < c3.low
        if c1.high < c3.low:
            bottom, top = c1.high, c3.low
            size_pct = ((top - bottom) / c2.close * 100) if c2.close > 0 else 0.0
            if size_pct >= min_size_pct:
                fvgs.append(FVG(
                    direction=Direction.BULLISH,
                    bottom=bottom,
                    top=top,
                    origin_bar=i,
                    size_pct=size_pct,
                ))
        # Bearish FVG: c1.low > c3.high
        elif c1.low > c3.high:
            bottom, top = c3.high, c1.low
            size_pct = ((top - bottom) / c2.close * 100) if c2.close > 0 else 0.0
            if size_pct >= min_size_pct:
                fvgs.append(FVG(
                    direction=Direction.BEARISH,
                    bottom=bottom,
                    top=top,
                    origin_bar=i,
                    size_pct=size_pct,
                ))

    # Mark mitigation: any subsequent candle whose range enters the gap
    for fvg in fvgs:
        for j in range(fvg.origin_bar + 2, len(candles)):
            cand = candles[j]
            if fvg.direction == Direction.BULLISH:
                # Bullish FVG mitigated when price trades down into it
                if cand.low <= fvg.top:
                    fvg.status = "MITIGATED"
                    fvg.mitigation_bar = j
                    break
            else:
                # Bearish FVG mitigated when price trades up into it
                if cand.high >= fvg.bottom:
                    fvg.status = "MITIGATED"
                    fvg.mitigation_bar = j
                    break

    return fvgs


def active_fvgs(fvgs: list[FVG]) -> list[FVG]:
    """Filter to only ACTIVE FVGs."""
    return [f for f in fvgs if f.status == "ACTIVE"]


def nearest_fvg(
    fvgs: list[FVG],
    price: float,
    direction: Optional[Direction] = None,
    side: Optional[str] = None,
) -> Optional[FVG]:
    """Return the FVG closest to `price`.

    Args:
        fvgs: FVGs to search through.
        price: reference price (usually current price).
        direction: if set, only consider FVGs of this direction.
        side: "above" → only FVGs whose bottom >= price.
              "below" → only FVGs whose top <= price.
              None    → no side filter.
    """
    candidates = [f for f in fvgs if f.status == "ACTIVE"]
    if direction is not None:
        candidates = [f for f in candidates if f.direction == direction]
    if side == "above":
        candidates = [f for f in candidates if f.bottom >= price]
    elif side == "below":
        candidates = [f for f in candidates if f.top <= price]

    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.midpoint - price))


def price_in_fvg(
    fvgs: list[FVG],
    price: float,
    direction: Optional[Direction] = None,
) -> Optional[FVG]:
    """If `price` is inside any (active) FVG, return it. Direction filter optional."""
    for fvg in fvgs:
        if fvg.status != "ACTIVE":
            continue
        if direction is not None and fvg.direction != direction:
            continue
        if fvg.contains(price):
            return fvg
    return None
