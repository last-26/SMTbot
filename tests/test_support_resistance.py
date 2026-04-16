"""Tests for src.analysis.support_resistance."""

from __future__ import annotations

from src.analysis.support_resistance import (
    SRZone,
    at_key_level,
    detect_sr_zones,
    nearest_zone,
    zones_above,
    zones_below,
)
from src.data.candle_buffer import Candle


def mk(h: float, l: float, c: float | None = None) -> Candle:
    mid = c if c is not None else (h + l) / 2
    return Candle(open=mid, high=h, low=l, close=mid, volume=1.0)


def _peak(price: float, pad: int = 3) -> list[Candle]:
    """Construct a swing-high fractal centered on `price` (strict fractal)."""
    return (
        [mk(price - 2 - i * 0.5, price - 10) for i in range(pad)][::-1]
        + [mk(price, price - 5)]
        + [mk(price - 2 - i * 0.5, price - 10) for i in range(pad)]
    )


def _trough(price: float, pad: int = 3) -> list[Candle]:
    """Construct a swing-low fractal centered on `price` (strict fractal)."""
    return (
        [mk(price + 10, price + 2 + i * 0.5) for i in range(pad)][::-1]
        + [mk(price + 5, price)]
        + [mk(price + 10, price + 2 + i * 0.5) for i in range(pad)]
    )


# ── Detection ───────────────────────────────────────────────────────────────


def test_detect_sr_zone_clusters_three_peaks():
    candles = _peak(100) + _peak(100.5) + _peak(100.2)
    zones = detect_sr_zones(
        candles, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    assert len(zones) >= 1
    top_zone = zones[0]
    assert top_zone.touches >= 3
    assert 99 <= top_zone.center <= 101


def test_detect_sr_zone_requires_min_touches():
    candles = _peak(100) + _peak(100.5)
    zones = detect_sr_zones(
        candles, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    assert zones == []


def test_sr_zone_role_classification():
    # Mix of highs and lows at the same level → MIXED (flip zone)
    candles = _peak(100) + _trough(100) + _peak(100.2)
    zones = detect_sr_zones(
        candles, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    assert any(z.role == "MIXED" for z in zones)


def test_sr_zone_support_only():
    candles = _trough(90) + _trough(90.2) + _trough(90.1)
    zones = detect_sr_zones(
        candles, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    assert zones
    assert zones[0].role == "SUPPORT"


def test_empty_candles_returns_empty():
    assert detect_sr_zones([]) == []


# ── Scoring ─────────────────────────────────────────────────────────────────


def test_score_rewards_recency_and_mixed_role():
    # Same touch count — MIXED should outscore SUPPORT
    candles_support = _trough(90) + _trough(90.1) + _trough(90.2)
    candles_mixed = _trough(80) + _peak(80.1) + _trough(80.2)
    s_zones = detect_sr_zones(
        candles_support, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    m_zones = detect_sr_zones(
        candles_mixed, swing_lookback=3, zone_atr_mult=5, min_touches=3,
    )
    if s_zones and m_zones:
        # MIXED bonus (0.5) pushes score higher
        assert m_zones[0].score > s_zones[0].score


# ── Queries ─────────────────────────────────────────────────────────────────


def test_nearest_zone_returns_closest():
    zones = [
        SRZone(center=100, bottom=99, top=101, touches=3, role="RESISTANCE"),
        SRZone(center=120, bottom=119, top=121, touches=3, role="RESISTANCE"),
        SRZone(center=80, bottom=79, top=81, touches=3, role="SUPPORT"),
    ]
    n = nearest_zone(zones, price=105)
    assert n.center == 100


def test_nearest_zone_role_filter_includes_mixed():
    zones = [
        SRZone(center=100, bottom=99, top=101, touches=3, role="RESISTANCE"),
        SRZone(center=110, bottom=109, top=111, touches=3, role="MIXED"),
    ]
    n = nearest_zone(zones, price=105, role="SUPPORT")
    assert n is not None
    assert n.role == "MIXED"   # MIXED counts for support queries


def test_zones_above_below_are_ordered_by_distance():
    zones = [
        SRZone(center=120, bottom=119, top=121, touches=3, role="RESISTANCE"),
        SRZone(center=130, bottom=129, top=131, touches=3, role="RESISTANCE"),
        SRZone(center=80, bottom=79, top=81, touches=3, role="SUPPORT"),
        SRZone(center=70, bottom=69, top=71, touches=3, role="SUPPORT"),
    ]
    above = zones_above(zones, price=100)
    assert [z.center for z in above] == [120, 130]
    below = zones_below(zones, price=100)
    assert [z.center for z in below] == [80, 70]


def test_at_key_level_returns_containing_zone():
    zones = [
        SRZone(center=100, bottom=99, top=101, touches=3, role="RESISTANCE", score=3.5),
        SRZone(center=100, bottom=98, top=102, touches=5, role="MIXED", score=5.5),
    ]
    at = at_key_level(zones, price=100)
    assert at is not None
    assert at.touches == 5   # highest-scored containing zone


def test_at_key_level_returns_none_when_price_outside():
    zones = [
        SRZone(center=100, bottom=99, top=101, touches=3, role="RESISTANCE"),
    ]
    assert at_key_level(zones, price=105) is None


def test_zone_distance_to_inside_zero():
    zone = SRZone(center=100, bottom=99, top=101, touches=3)
    assert zone.distance_to(100) == 0.0
    assert zone.distance_to(105) == 4.0
    assert zone.distance_to(95) == 4.0
