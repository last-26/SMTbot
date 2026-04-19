"""Phase 7.C1 — zone-based setup planner.

Direction is an input (decided upstream by confluence + HTF trend
picker); the planner's job is to find the best zone to limit-order
into. Sources are tried in priority order: liq pool → HTF FVG →
VWAP retest → sweep retest. First hit wins.
"""

from __future__ import annotations

import pytest

from src.analysis.liquidity_heatmap import Cluster, LiquidityHeatmap
from src.data.models import (
    Direction,
    FVGZone,
    MarketState,
    SignalTableData,
    SweepEvent,
)
from src.strategy.setup_planner import ZoneSetup, build_zone_setup


def _state(price: float, atr: float, **signal_overrides) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP", timeframe="3m",
        signal_table=SignalTableData(price=price, atr_14=atr, **signal_overrides),
    )


def _heatmap(*, price: float, below: list[Cluster] | None = None,
             above: list[Cluster] | None = None) -> LiquidityHeatmap:
    below = below or []
    above = above or []
    return LiquidityHeatmap(
        symbol="BTC-USDT-SWAP", current_price=price,
        clusters_above=above, clusters_below=below,
        nearest_above=above[0] if above else None,
        nearest_below=below[0] if below else None,
        largest_above_notional=max((c.notional_usd for c in above), default=0.0),
        largest_below_notional=max((c.notional_usd for c in below), default=0.0),
    )


def _cluster(price: float, notional: float = 5_000_000.0,
             side: str = "LONG_LIQ") -> Cluster:
    return Cluster(price=price, notional_usd=notional, side=side)


# ── Rejection paths ─────────────────────────────────────────────────────────


def test_undefined_direction_returns_none():
    state = _state(100.0, 1.0)
    assert build_zone_setup(direction=Direction.UNDEFINED, state=state) is None


def test_zero_atr_returns_none():
    state = _state(100.0, 0.0)
    assert build_zone_setup(direction=Direction.BULLISH, state=state) is None


def test_no_sources_returns_none():
    """Bare state with no heatmap, no HTF FVGs, no VWAPs, no sweeps — None."""
    state = _state(100.0, 1.0)
    assert build_zone_setup(direction=Direction.BULLISH, state=state) is None


# ── Source 1 — liquidity pool ───────────────────────────────────────────────


def test_liq_pool_beats_other_sources_when_available():
    """Priority order: a liq pool should win even if a VWAP retest is also
    available. This pins the documented priority chain."""
    state = _state(100.0, 1.0, vwap_3m=99.5)       # VWAP retest option
    hm = _heatmap(price=100.0,
                  below=[_cluster(price=98.0)])     # long liq pool below
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "liq_pool"
    low, high = setup.entry_zone
    assert low < 98.0 < high


def test_liq_pool_respects_direction_side():
    """Bearish setup takes the nearest_above cluster, not nearest_below."""
    state = _state(100.0, 1.0)
    hm = _heatmap(price=100.0,
                  below=[_cluster(price=98.0)],
                  above=[_cluster(price=102.0, side="SHORT_LIQ")])
    setup = build_zone_setup(
        direction=Direction.BEARISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "liq_pool"
    low, high = setup.entry_zone
    assert low < 102.0 < high


def test_liq_pool_zero_notional_skipped():
    """A cluster with zero notional (missing-data sentinel) is ignored,
    falling through to the next source."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    hm = _heatmap(price=100.0,
                  below=[_cluster(price=98.0, notional=0.0)])
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "vwap_retest"


def test_liq_pool_on_wrong_side_of_price_skipped():
    """nearest_below for a long, but with price=98 — cluster is now above,
    so the source guards and falls through."""
    state = _state(97.0, 1.0, vwap_3m=96.5)
    hm = _heatmap(price=97.0,
                  below=[_cluster(price=98.0)])   # stale: actually above now
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "vwap_retest"


# ── Source 2 — HTF FVG ──────────────────────────────────────────────────────


def test_htf_fvg_long_picks_bull_fvg_below_price():
    """Bull FVG below price → long entry zone."""
    state = _state(100.0, 1.0)
    htf = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="15",
        fvg_zones=[FVGZone(direction=Direction.BULLISH, bottom=97.5, top=98.5)],
    )
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, htf_state=htf,
    )
    assert setup is not None
    assert setup.zone_source == "fvg_htf"
    assert setup.entry_zone == (97.5, 98.5)


def test_htf_fvg_ignores_wrong_side_or_wrong_direction():
    """A BEARISH FVG for a BULLISH setup is skipped; a BULLISH FVG above
    current price is also skipped (can't limit-buy above market)."""
    state = _state(100.0, 1.0)
    htf = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="15",
        fvg_zones=[
            FVGZone(direction=Direction.BEARISH, bottom=97.0, top=97.5),
            FVGZone(direction=Direction.BULLISH, bottom=101.0, top=102.0),
        ],
    )
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, htf_state=htf,
    )
    assert setup is None


def test_htf_fvg_picks_nearest_when_multiple():
    state = _state(100.0, 1.0)
    htf = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="15",
        fvg_zones=[
            FVGZone(direction=Direction.BULLISH, bottom=90.0, top=91.0),   # far
            FVGZone(direction=Direction.BULLISH, bottom=97.5, top=98.5),   # near
        ],
    )
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, htf_state=htf,
    )
    assert setup is not None
    assert setup.entry_zone == (97.5, 98.5)


# ── Source 3 — VWAP retest ──────────────────────────────────────────────────


def test_vwap_retest_picks_nearest_below_for_long():
    state = _state(100.0, 1.0, vwap_1m=95.0, vwap_3m=99.5, vwap_15m=97.0)
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    low, high = setup.entry_zone
    assert low < 99.5 < high


def test_vwap_retest_ignores_zero_and_wrong_side():
    """vwap=0 is unparsed; a VWAP *above* price for a long is wrong side."""
    state = _state(100.0, 1.0, vwap_1m=0.0, vwap_3m=101.0, vwap_15m=0.0)
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is None


# ── Source 4 — sweep retest ─────────────────────────────────────────────────


def test_sweep_retest_bearish_sweep_gives_bullish_zone():
    """Bearish sweep (swept highs) → reversal long at the reclaimed level."""
    state = _state(100.0, 1.0)
    state.sweep_events.append(
        SweepEvent(direction=Direction.BEARISH, level=98.0)
    )
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "sweep_retest"
    assert setup.trigger_type == "sweep_reversal"
    low, high = setup.entry_zone
    assert low < 98.0 < high


def test_sweep_retest_opposite_direction_skipped():
    """A bullish sweep does not create a BULLISH (long) setup."""
    state = _state(100.0, 1.0)
    state.sweep_events.append(
        SweepEvent(direction=Direction.BULLISH, level=98.0)
    )
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is None


# ── SL + TP integration ─────────────────────────────────────────────────────


def test_sl_sits_beyond_zone_on_structural_side():
    """SL is `sl_buffer_atr × ATR` beyond the far edge."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, sl_buffer_atr=0.5,
        zone_buffer_atr=0.25,
    )
    assert setup is not None
    low, _ = setup.entry_zone
    assert setup.sl_beyond_zone == pytest.approx(low - 0.5, abs=1e-6)


def test_tp_primary_uses_nearest_cluster_in_direction():
    """Long: TP pulls from nearest_above cluster (beyond the zone mid)."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    hm = _heatmap(
        price=100.0,
        above=[_cluster(price=105.0, side="SHORT_LIQ")],
    )
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.tp_primary == 105.0


def test_tp_falls_back_to_rr_when_heatmap_missing():
    """Without a heatmap, TP is projected as RR × (zone width + ATR)."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, default_rr=2.0,
    )
    assert setup is not None
    assert setup.tp_primary > 100.0        # above entry zone mid
