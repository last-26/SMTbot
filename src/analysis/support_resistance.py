"""Support / Resistance level detection and scoring.

Takes the swing points from market_structure and clusters them into price
zones. Each zone is scored by:
  - touch count (how many swings occur there)
  - age weighting (recent touches count more)
  - range band (ATR multiplier)

S/R zones are used by the strategy engine to:
  - validate entry confluence (price at S/R = extra score)
  - place stops (just beyond a strong S/R)
  - project targets (next S/R level on the opposite side)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.analysis.market_structure import SwingPoint, find_swing_points
from src.data.candle_buffer import Candle


@dataclass
class SRZone:
    """A support or resistance zone derived from clustered swings."""
    center: float                     # centerline price
    bottom: float                     # zone lower bound
    top: float                        # zone upper bound
    touches: int                      # number of swings in the cluster
    role: str = "MIXED"               # "SUPPORT", "RESISTANCE", or "MIXED"
    score: float = 0.0                # weighted score
    bar_indices: list[int] = field(default_factory=list)
    last_touch_bar: int = 0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def distance_to(self, price: float) -> float:
        """Absolute distance from `price` to the zone edge (0 if inside)."""
        if self.contains(price):
            return 0.0
        if price < self.bottom:
            return self.bottom - price
        return price - self.top


# ── ATR helper ──────────────────────────────────────────────────────────────


def _atr(candles: list[Candle], period: int = 14) -> float:
    """Classic ATR over the last `period` candles. Zero if insufficient data."""
    if len(candles) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - p.close),
            abs(c.low - p.close),
        )
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


# ── Detection ───────────────────────────────────────────────────────────────


def detect_sr_zones(
    candles: list[Candle],
    swing_lookback: int = 3,
    zone_atr_mult: float = 0.5,
    min_touches: int = 3,
) -> list[SRZone]:
    """Cluster swing points into S/R zones and score them.

    Args:
        candles: OHLCV buffer.
        swing_lookback: fractal window for swing detection.
        zone_atr_mult: zone half-width as a multiple of ATR(14).
            (e.g. 0.5 → zone spans ATR around centerline).
        min_touches: minimum swing touches required to form a zone.

    Returns:
        Sorted list (by score desc) of SRZone objects.
    """
    swings = find_swing_points(candles, lookback=swing_lookback)
    if not swings:
        return []

    atr = _atr(candles, period=14)
    if atr <= 0:
        # Fallback: use 0.2% of last close as zone width
        last_close = candles[-1].close if candles else 1.0
        atr = last_close * 0.002
    zone_width = atr * zone_atr_mult

    # Greedy cluster — same algo as liquidity, but using absolute distance
    pts = sorted(swings, key=lambda s: s.price)
    clusters: list[list[SwingPoint]] = []
    if not pts:
        return []

    current: list[SwingPoint] = [pts[0]]
    for s in pts[1:]:
        if abs(s.price - current[-1].price) <= zone_width:
            current.append(s)
        else:
            clusters.append(current)
            current = [s]
    clusters.append(current)

    zones: list[SRZone] = []
    for cl in clusters:
        if len(cl) < min_touches:
            continue
        prices = [s.price for s in cl]
        center = sum(prices) / len(prices)
        bars = [s.bar_index for s in cl]
        role = _classify_role(cl)
        zone = SRZone(
            center=center,
            bottom=center - zone_width / 2,
            top=center + zone_width / 2,
            touches=len(cl),
            role=role,
            bar_indices=bars,
            last_touch_bar=max(bars),
        )
        zone.score = _score_zone(zone, total_bars=len(candles))
        zones.append(zone)

    zones.sort(key=lambda z: z.score, reverse=True)
    return zones


def _classify_role(cluster: list[SwingPoint]) -> str:
    """Assign SUPPORT / RESISTANCE / MIXED based on swing kinds in the cluster."""
    n_high = sum(1 for s in cluster if s.kind == "high")
    n_low = sum(1 for s in cluster if s.kind == "low")
    if n_high > 0 and n_low > 0:
        return "MIXED"
    return "RESISTANCE" if n_high > 0 else "SUPPORT"


def _score_zone(zone: SRZone, total_bars: int) -> float:
    """Combine touch count + recency into a single score."""
    if total_bars <= 0:
        return float(zone.touches)
    # Touch weight
    base = float(zone.touches)
    # Recency (0..1) — 1 when last touch is the most recent bar
    recency = zone.last_touch_bar / max(total_bars - 1, 1)
    # MIXED zones (role-flip zones) are strongest
    role_bonus = 0.5 if zone.role == "MIXED" else 0.0
    return base + recency + role_bonus


# ── Queries ─────────────────────────────────────────────────────────────────


def nearest_zone(
    zones: list[SRZone],
    price: float,
    role: Optional[str] = None,
) -> Optional[SRZone]:
    """Return the S/R zone closest to `price`, optionally filtered by role."""
    candidates = zones
    if role is not None:
        candidates = [z for z in zones if z.role == role or z.role == "MIXED"]
    if not candidates:
        return None
    return min(candidates, key=lambda z: z.distance_to(price))


def zones_above(zones: list[SRZone], price: float) -> list[SRZone]:
    """Zones whose bottom is above `price`, sorted nearest-first."""
    above = [z for z in zones if z.bottom > price]
    above.sort(key=lambda z: z.bottom - price)
    return above


def zones_below(zones: list[SRZone], price: float) -> list[SRZone]:
    """Zones whose top is below `price`, sorted nearest-first."""
    below = [z for z in zones if z.top < price]
    below.sort(key=lambda z: price - z.top)
    return below


def at_key_level(
    zones: list[SRZone],
    price: float,
) -> Optional[SRZone]:
    """If `price` sits inside any S/R zone, return the highest-scored one."""
    inside = [z for z in zones if z.contains(price)]
    if not inside:
        return None
    return max(inside, key=lambda z: z.score)
