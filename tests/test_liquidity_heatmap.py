"""Unit tests for src/analysis/liquidity_heatmap.py (Phase 1.5 Madde 4).

Pure-function module, so no mocks beyond a tiny FakeLiqStream for the
historical-events branch in `build_heatmap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.analysis.liquidity_heatmap import (
    FEE_BUFFER,
    LEVERAGE_BUCKETS,
    Cluster,
    EstimatedLiqLevel,
    build_heatmap,
    cluster_levels,
    estimate_liquidation_levels,
    historical_liq_levels,
)


# ── Fakes ─────────────────────────────────────────────────────────────────


@dataclass
class FakeDerivState:
    open_interest_usd: float = 0.0
    long_short_ratio: float = 1.0


@dataclass
class FakeLiqEvent:
    price: float
    notional_usd: float
    side: str


class FakeLiqStream:
    def __init__(self, events_map: dict[str, list[FakeLiqEvent]]):
        self._events_map = events_map

    def recent(self, symbol: str, lookback_ms: int) -> list[FakeLiqEvent]:
        return list(self._events_map.get(symbol, []))


# ── estimate_liquidation_levels ───────────────────────────────────────────


def test_estimate_balanced_ls_splits_oi_half_half():
    """LS=1.0 means long_oi == short_oi; long/short notionals match 1:1."""
    levels = estimate_liquidation_levels(
        current_price=100.0, long_short_ratio=1.0, total_oi_usd=100_000.0,
    )
    longs = [l for l in levels if l.side == "LONG_LIQ"]
    shorts = [l for l in levels if l.side == "SHORT_LIQ"]
    assert sum(l.notional_usd for l in longs) == pytest.approx(50_000.0)
    assert sum(l.notional_usd for l in shorts) == pytest.approx(50_000.0)


def test_estimate_long_liq_price_formula():
    """10x long at $100 → liq ≈ $100 * (1 - 0.1 + 0.005) = $90.5."""
    levels = estimate_liquidation_levels(
        current_price=100.0, long_short_ratio=1.0, total_oi_usd=100_000.0,
        leverage_buckets=[(10, 1.0)],
    )
    long_lvl = next(l for l in levels if l.side == "LONG_LIQ")
    short_lvl = next(l for l in levels if l.side == "SHORT_LIQ")
    assert long_lvl.price == pytest.approx(100.0 * (1.0 - 1.0 / 10 + FEE_BUFFER))
    assert short_lvl.price == pytest.approx(100.0 * (1.0 + 1.0 / 10 - FEE_BUFFER))


def test_estimate_zero_oi_returns_empty():
    assert estimate_liquidation_levels(100.0, 1.0, 0.0) == []


def test_estimate_asymmetric_ls_favors_long_side_notional():
    """LS=2.0 → 2/3 of OI is longs; longs get more notional than shorts."""
    levels = estimate_liquidation_levels(
        current_price=100.0, long_short_ratio=2.0, total_oi_usd=90_000.0,
    )
    longs_total = sum(l.notional_usd for l in levels if l.side == "LONG_LIQ")
    shorts_total = sum(l.notional_usd for l in levels if l.side == "SHORT_LIQ")
    assert longs_total == pytest.approx(60_000.0)
    assert shorts_total == pytest.approx(30_000.0)


# ── cluster_levels ────────────────────────────────────────────────────────


def test_cluster_merges_same_side_nearby_levels():
    """Two LONG_LIQ levels within bucket_pct collapse into one cluster."""
    levels = [
        EstimatedLiqLevel(price=100.0, notional_usd=1000.0, side="LONG_LIQ", leverage=10),
        EstimatedLiqLevel(price=100.1, notional_usd=500.0, side="LONG_LIQ", leverage=25),
    ]
    clusters = cluster_levels(levels, bucket_pct=0.01)  # generous 1% bucket
    long_clusters = [c for c in clusters if c.side == "LONG_LIQ"]
    assert len(long_clusters) == 1
    assert long_clusters[0].notional_usd == pytest.approx(1500.0)
    # Weighted price pulled toward the larger notional (100.0).
    assert long_clusters[0].price < 100.1


def test_cluster_keeps_opposite_sides_separate():
    levels = [
        EstimatedLiqLevel(price=100.0, notional_usd=1000.0, side="LONG_LIQ", leverage=10),
        EstimatedLiqLevel(price=100.0, notional_usd=2000.0, side="SHORT_LIQ", leverage=10),
    ]
    clusters = cluster_levels(levels, bucket_pct=0.002)
    sides = {c.side for c in clusters}
    assert sides == {"LONG_LIQ", "SHORT_LIQ"}


# ── historical_liq_levels ─────────────────────────────────────────────────


def test_historical_liq_levels_none_stream_returns_empty():
    assert historical_liq_levels(None, "BTC-USDT-SWAP", 10_000) == []


def test_historical_liq_levels_maps_events_to_level_dataclass():
    stream = FakeLiqStream({
        "BTC-USDT-SWAP": [
            FakeLiqEvent(price=70_000.0, notional_usd=50_000.0, side="LONG_LIQ"),
        ],
    })
    out = historical_liq_levels(stream, "BTC-USDT-SWAP", 60_000)
    assert len(out) == 1
    assert out[0].price == 70_000.0
    assert out[0].kind == "historical"


# ── build_heatmap ─────────────────────────────────────────────────────────


def test_build_heatmap_empty_oi_returns_empty_clusters():
    hm = build_heatmap(
        symbol="BTC-USDT-SWAP",
        current_price=100.0,
        deriv_state=FakeDerivState(open_interest_usd=0.0),
        liq_stream=None,
    )
    assert hm.clusters_above == []
    assert hm.clusters_below == []
    assert hm.nearest_above is None
    assert hm.nearest_below is None


def test_build_heatmap_splits_above_and_below_current_price():
    """With balanced LS=1 and positive OI, we expect clusters on BOTH sides."""
    hm = build_heatmap(
        symbol="BTC-USDT-SWAP",
        current_price=100.0,
        deriv_state=FakeDerivState(
            open_interest_usd=1_000_000.0, long_short_ratio=1.0,
        ),
        liq_stream=None,
    )
    # Long liqs sit below current_price, short liqs sit above.
    assert len(hm.clusters_below) >= 1
    assert len(hm.clusters_above) >= 1
    assert all(c.price < 100.0 for c in hm.clusters_below)
    assert all(c.price > 100.0 for c in hm.clusters_above)
    # Nearest-above / nearest-below are populated.
    assert hm.nearest_above is not None
    assert hm.nearest_below is not None
    # largest_*_notional matches the max in each side.
    assert hm.largest_above_notional == max(
        c.notional_usd for c in hm.clusters_above
    )


def test_build_heatmap_merges_historical_with_estimated():
    """A historical event near an estimated level should collapse into one
    cluster whose sources contain both 'estimated' and 'historical'."""
    # 10x long at $100 → liq price $90.5. Plant a historical event nearby.
    stream = FakeLiqStream({
        "BTC-USDT-SWAP": [
            FakeLiqEvent(price=90.5, notional_usd=20_000.0, side="LONG_LIQ"),
        ],
    })
    hm = build_heatmap(
        symbol="BTC-USDT-SWAP",
        current_price=100.0,
        deriv_state=FakeDerivState(
            open_interest_usd=100_000.0, long_short_ratio=1.0,
        ),
        liq_stream=stream,
        leverage_buckets=[(10, 1.0)],
        bucket_pct=0.01,   # 1% — guarantees the merge
    )
    merged = [c for c in hm.clusters_below if "historical" in c.sources]
    assert merged, "expected at least one cluster carrying historical source"
    assert "estimated" in merged[0].sources
