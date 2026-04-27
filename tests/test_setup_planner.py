"""Zone-based setup planner — 2026-04-19 scalp rebalance.

Source priority:
    vwap_retest > ema21_pullback > fvg_entry > sweep_retest > liq_pool_near
HTF 15m FVG is available as an opt-in entry source
(`htf_fvg_entry_enabled=True`); by default it is TP-only.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.analysis.liquidity_heatmap import Cluster, LiquidityHeatmap
from src.data.models import (
    Direction,
    FVGZone,
    MarketState,
    SignalTableData,
    SweepEvent,
)
from src.strategy.setup_planner import (
    ZoneSetup,
    apply_zone_to_plan,
    build_zone_setup,
)
from src.strategy.trade_plan import TradePlan


# ── Fixtures ────────────────────────────────────────────────────────────────


def _state(price: float, atr: float, *,
           fvg_zones: list[FVGZone] | None = None,
           **signal_overrides) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP", timeframe="3m",
        signal_table=SignalTableData(price=price, atr_14=atr, **signal_overrides),
        fvg_zones=fvg_zones or [],
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


@dataclass
class _FakeCandle:
    close: float


def _candles_for_stack(n: int = 100, *, bull: bool = True,
                       current_price: float = 100.0) -> list[_FakeCandle]:
    """Build a monotonic candle series that yields a clean EMA stack at
    ``current_price``. Linear ramp keeps EMA21 above EMA55 for bull and
    below for bear.
    """
    if bull:
        start = current_price - 8.0
        step = (current_price - start) / (n - 1)
        return [_FakeCandle(close=start + step * i) for i in range(n)]
    start = current_price + 8.0
    step = (start - current_price) / (n - 1)
    return [_FakeCandle(close=start - step * i) for i in range(n)]


# ── Rejection paths ─────────────────────────────────────────────────────────


def test_undefined_direction_returns_none():
    state = _state(100.0, 1.0)
    assert build_zone_setup(direction=Direction.UNDEFINED, state=state) is None


def test_zero_atr_returns_none():
    state = _state(100.0, 0.0)
    assert build_zone_setup(direction=Direction.BULLISH, state=state) is None


def test_no_sources_returns_none():
    """Bare state with no heatmap, no FVGs, no VWAPs, no sweeps — None."""
    state = _state(100.0, 1.0)
    assert build_zone_setup(direction=Direction.BULLISH, state=state) is None


# ── Priority: VWAP retest wins ──────────────────────────────────────────────


def test_vwap_retest_beats_liq_pool_when_both_available():
    """Post-pivot priority: VWAP retest wins over liq_pool_near even when
    both fire. Liquidity is a TP instrument, not the primary entry."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    # Abnormal-looking cluster: 5M notional with no peers → would clear
    # the magnitude gate on its own, but VWAP still wins by priority.
    hm = _heatmap(price=100.0, below=[_cluster(price=99.2)])
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "vwap_retest"


def test_vwap_retest_picks_nearest_below_for_long():
    state = _state(100.0, 1.0, vwap_1m=95.0, vwap_3m=99.5, vwap_15m=97.0)
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    low, high = setup.entry_zone
    # Post-2026-04-19 rewire: zone sits on the directional side of VWAP
    # (long → above). No bands in this fixture → ATR half-band above VWAP.
    assert low == pytest.approx(99.5)
    assert high > 99.5


def test_vwap_retest_uses_3m_bands_when_available():
    """3m is the nearest VWAP and Pine has emitted ±1σ bands → zone spans
    (vwap, upper_band) for long. Realised session volatility, not ATR."""
    state = _state(
        100.0, 1.0,
        vwap_3m=99.5, vwap_3m_upper=99.9, vwap_3m_lower=99.1,
    )
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    assert setup.entry_zone == (pytest.approx(99.5), pytest.approx(99.9))


def test_vwap_retest_short_uses_lower_band():
    """Short mirror: zone = (lower_band, vwap). Price must be above VWAP."""
    state = _state(
        100.0, 1.0,
        vwap_3m=100.5, vwap_3m_upper=100.9, vwap_3m_lower=100.1,
    )
    setup = build_zone_setup(direction=Direction.BEARISH, state=state)
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    assert setup.entry_zone == (pytest.approx(100.1), pytest.approx(100.5))


def test_vwap_retest_falls_back_to_atr_when_bands_missing():
    """3m is nearest but bands are 0 (session too young or old Pine) →
    directional ATR half-band above VWAP for long."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, zone_buffer_atr=0.25,
    )
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    assert setup.entry_zone == (pytest.approx(99.5), pytest.approx(99.75))


def test_vwap_retest_ignores_3m_bands_when_1m_is_nearer():
    """Bands live on 3m only. If 1m VWAP is the nearest, the 3m bands must
    not leak into the zone — falls back to ATR around 1m VWAP."""
    state = _state(
        100.0, 1.0,
        vwap_1m=99.8, vwap_3m=99.5,
        vwap_3m_upper=99.9, vwap_3m_lower=99.1,
    )
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "vwap_retest"
    # Zone anchored on 99.8 (1m), NOT (99.5, 99.9) from 3m bands.
    low, high = setup.entry_zone
    assert low == pytest.approx(99.8)
    assert high > 99.8


def test_vwap_retest_ignores_zero_and_wrong_side():
    """vwap=0 is unparsed; a VWAP *above* price for a long is wrong side.
    Falls through to later sources — None here (none configured)."""
    state = _state(100.0, 1.0, vwap_1m=0.0, vwap_3m=101.0, vwap_15m=0.0)
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is None


# ── EMA21 pullback ──────────────────────────────────────────────────────────


def test_ema21_pullback_fires_when_price_near_ema_in_stack():
    """Bull stack (price > EMA21 > EMA55) with price inside the EMA21 band."""
    candles = _candles_for_stack(bull=True, current_price=100.0)
    # No VWAP, no FVG — EMA21 pullback must be the first hit.
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, ltf_candles=candles,
    )
    assert setup is not None
    assert setup.zone_source == "ema21_pullback"
    low, high = setup.entry_zone
    # Zone centres on EMA21 (below price); ATR buffer widens ±0.25.
    assert high < 100.0


def test_ema21_pullback_rejects_contra_stack():
    """Bear stack + LONG direction → EMA21 pullback returns None; falls
    through to later sources (also empty here)."""
    candles = _candles_for_stack(bull=False, current_price=100.0)
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, ltf_candles=candles,
    )
    assert setup is None


def test_ema21_pullback_disabled_skips_source():
    """When flag is off the EMA21 source returns None by construction."""
    candles = _candles_for_stack(bull=True, current_price=100.0)
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, ltf_candles=candles,
        ema21_pullback_enabled=False,
    )
    assert setup is None


# ── Entry-TF FVG ────────────────────────────────────────────────────────────


def test_entry_tf_fvg_fires_without_htf_state():
    """Entry-TF FVGs live on MarketState.fvg_zones — no htf_state needed."""
    fvg = FVGZone(direction=Direction.BULLISH, bottom=97.5, top=98.5)
    state = _state(100.0, 1.0, fvg_zones=[fvg])
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "fvg_entry"
    assert setup.entry_zone == (97.5, 98.5)


def test_entry_tf_fvg_ignores_wrong_side_or_wrong_direction():
    state = _state(100.0, 1.0, fvg_zones=[
        FVGZone(direction=Direction.BEARISH, bottom=97.0, top=97.5),
        FVGZone(direction=Direction.BULLISH, bottom=101.0, top=102.0),
    ])
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is None


# ── Sweep retest ────────────────────────────────────────────────────────────


def test_sweep_retest_bearish_sweep_gives_bullish_zone():
    state = _state(100.0, 1.0)
    state.sweep_events.append(SweepEvent(direction=Direction.BEARISH, level=98.0))
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.zone_source == "sweep_retest"
    assert setup.trigger_type == "sweep_reversal"


def test_sweep_retest_opposite_direction_skipped():
    state = _state(100.0, 1.0)
    state.sweep_events.append(SweepEvent(direction=Direction.BULLISH, level=98.0))
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is None


# ── Liq pool (near + abnormal gates) ────────────────────────────────────────


def test_liq_pool_near_accepts_abnormal_cluster_near_price():
    """BTC-75000 / 74800 case: near + 5× bigger than peers → entry zone at
    the cluster. All higher-priority sources empty."""
    big = _cluster(price=99.3, notional=5_000_000.0)          # ~0.7×ATR away
    peers = [_cluster(price=99.0, notional=1_000_000.0),
             _cluster(price=98.8, notional=900_000.0)]
    hm = _heatmap(price=100.0, below=[big, *peers])
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is not None
    assert setup.zone_source == "liq_pool_near"


def test_liq_pool_near_rejects_far_cluster():
    """Cluster outside `liq_entry_near_max_atr × ATR` is not an entry."""
    big = _cluster(price=95.0, notional=5_000_000.0)          # ~5×ATR away
    peers = [_cluster(price=94.5, notional=1_000_000.0)]
    hm = _heatmap(price=100.0, below=[big, *peers])
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
        liq_entry_near_max_atr=1.5,
    )
    assert setup is None


def test_liq_pool_near_rejects_small_magnitude():
    """Nearest cluster has no special notional advantage → not abnormal."""
    c1 = _cluster(price=99.3, notional=1_000_000.0)
    c2 = _cluster(price=99.0, notional=1_000_000.0)
    hm = _heatmap(price=100.0, below=[c1, c2])
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
        liq_entry_magnitude_mult=2.5,
    )
    assert setup is None


def test_liq_pool_near_wrong_side_skipped():
    """Long with a cluster above price fails the side guard."""
    hm = _heatmap(price=97.0, below=[_cluster(price=98.0)])   # stale above
    state = _state(97.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
    )
    assert setup is None


def test_liq_pool_near_entry_price_is_zone_mid():
    """Zone-mid sources (liq_pool_near, vwap_retest) both centre entry in
    the middle of the zone; pullback-edge sources still hit the far edge."""
    from src.strategy.setup_planner import zone_limit_price
    zone = (99.0, 99.5)
    assert zone_limit_price(
        Direction.BULLISH, zone, zone_source="liq_pool_near",
    ) == pytest.approx(99.25)
    # vwap_retest with default anchors (0.75/0.25) collapses to the old
    # 0.5σ midpoint — backwards-compat contract for callers that don't
    # thread YAML config through (tests, legacy fixtures).
    assert zone_limit_price(
        Direction.BULLISH, zone, zone_source="vwap_retest",
    ) == pytest.approx(99.25)
    # Sanity: pullback-edge sources still hit the far edge for long.
    assert zone_limit_price(
        Direction.BULLISH, zone, zone_source="ema21_pullback",
    ) == 99.0


def test_vwap_retest_long_anchor_pulls_entry_toward_vwap():
    """long_anchor=0.7 → entry at 40% above VWAP (0.4σ) using the user's
    2026-04-21 example: VWAP=100, upper=150, long limit at 120."""
    from src.strategy.setup_planner import zone_limit_price
    # Long zone is (VWAP, upper_band).
    zone = (100.0, 150.0)
    # anchor=0.7 on full [lower=50, upper=150] axis → 50 + 0.7×100 = 120.
    limit = zone_limit_price(
        Direction.BULLISH, zone, zone_source="vwap_retest",
        vwap_long_anchor=0.7, vwap_short_anchor=0.3,
    )
    assert limit == pytest.approx(120.0)


def test_vwap_retest_short_anchor_pulls_entry_toward_vwap():
    """short_anchor=0.3 → entry at 40% below VWAP. VWAP=100, lower=50 →
    short limit at 80 (symmetric with the long=120 case)."""
    from src.strategy.setup_planner import zone_limit_price
    # Short zone is (lower_band, VWAP).
    zone = (50.0, 100.0)
    # anchor=0.3 on full axis → 50 + 0.3×100 = 80.
    limit = zone_limit_price(
        Direction.BEARISH, zone, zone_source="vwap_retest",
        vwap_long_anchor=0.7, vwap_short_anchor=0.3,
    )
    assert limit == pytest.approx(80.0)


def test_vwap_retest_anchor_half_collapses_to_vwap():
    """anchor=0.5 for either direction lands the limit exactly at VWAP
    (zone.low for long, zone.high for short) — boundary of the valid
    anchor range."""
    from src.strategy.setup_planner import zone_limit_price
    long_zone = (100.0, 150.0)
    assert zone_limit_price(
        Direction.BULLISH, long_zone, zone_source="vwap_retest",
        vwap_long_anchor=0.5, vwap_short_anchor=0.5,
    ) == pytest.approx(100.0)
    short_zone = (50.0, 100.0)
    assert zone_limit_price(
        Direction.BEARISH, short_zone, zone_source="vwap_retest",
        vwap_long_anchor=0.5, vwap_short_anchor=0.5,
    ) == pytest.approx(100.0)


def test_vwap_retest_anchor_extremes_hit_outer_bands():
    """anchor=1.0 for long → upper band (zone.high). anchor=0.0 for short
    → lower band (zone.low). These are the outer edges of the valid range
    (maximal distance from VWAP, riskiest fill targets)."""
    from src.strategy.setup_planner import zone_limit_price
    long_zone = (100.0, 150.0)
    assert zone_limit_price(
        Direction.BULLISH, long_zone, zone_source="vwap_retest",
        vwap_long_anchor=1.0,
    ) == pytest.approx(150.0)
    short_zone = (50.0, 100.0)
    assert zone_limit_price(
        Direction.BEARISH, short_zone, zone_source="vwap_retest",
        vwap_short_anchor=0.0,
    ) == pytest.approx(50.0)


def test_vwap_retest_default_anchors_preserve_midpoint_contract():
    """Convention X sanity: 0.75 (long) / 0.25 (short) on the full band
    axis both equal the 0.5σ midpoint — the pre-2026-04-21 behaviour. Any
    caller that does not pass anchors (tests, legacy) gets the old mid."""
    from src.strategy.setup_planner import zone_limit_price
    long_zone = (100.0, 150.0)
    # Long default 0.75 → 50 + 0.75×100 = 125 = midpoint of (100, 150).
    assert zone_limit_price(
        Direction.BULLISH, long_zone, zone_source="vwap_retest",
    ) == pytest.approx(125.0)
    short_zone = (50.0, 100.0)
    # Short default 0.25 → 50 + 0.25×100 = 75 = midpoint of (50, 100).
    assert zone_limit_price(
        Direction.BEARISH, short_zone, zone_source="vwap_retest",
    ) == pytest.approx(75.0)


def test_vwap_retest_anchor_applies_in_atr_fallback_path():
    """When Pine emits no 3m band, `_vwap_zone` falls back to a single-
    sided ATR half-band ``(vwap, vwap + atr·mult)`` for long — same
    geometry, different σ. The anchor formula uses (high − low) regardless
    of whether σ came from the Pine band or the ATR fallback, so 0.7
    still lands 40% above VWAP inside the synthetic band."""
    from src.strategy.setup_planner import zone_limit_price
    # Synthetic long zone: VWAP=100, zone_buffer_atr × ATR = 0.5.
    zone = (100.0, 100.5)
    limit = zone_limit_price(
        Direction.BULLISH, zone, zone_source="vwap_retest",
        vwap_long_anchor=0.7,
    )
    # 100 + (2·0.7 − 1) × 0.5 = 100 + 0.4 × 0.5 = 100.2
    assert limit == pytest.approx(100.2)


# ── HTF FVG (opt-in entry source) ───────────────────────────────────────────


def test_htf_fvg_entry_off_by_default():
    """By default HTF FVG is TP-only; no entry fires even with a valid FVG."""
    htf = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="15",
        fvg_zones=[FVGZone(direction=Direction.BULLISH, bottom=97.5, top=98.5)],
    )
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, htf_state=htf,
    )
    assert setup is None


def test_htf_fvg_entry_enabled_fires_last():
    """With the opt-in flag, HTF FVG sits after liq_pool_near in priority."""
    htf = MarketState(
        symbol="BTC-USDT-SWAP", timeframe="15",
        fvg_zones=[FVGZone(direction=Direction.BULLISH, bottom=97.5, top=98.5)],
    )
    state = _state(100.0, 1.0)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, htf_state=htf,
        htf_fvg_entry_enabled=True,
    )
    assert setup is not None
    assert setup.zone_source == "fvg_htf"


# ── SL / TP + ladder ────────────────────────────────────────────────────────


def test_sl_sits_beyond_zone_on_structural_side():
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, sl_buffer_atr=0.5,
        zone_buffer_atr=0.25,
    )
    assert setup is not None
    low, _ = setup.entry_zone
    assert setup.sl_beyond_zone == pytest.approx(low - 0.5, abs=1e-6)


def test_tp_primary_uses_nearest_cluster_in_direction():
    """Long: TP pulls from nearest_above cluster beyond the zone mid."""
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
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, default_rr=2.0,
    )
    assert setup is not None
    assert setup.tp_primary > 100.0


def test_tp_ladder_builds_from_multiple_clusters():
    """Three above-side clusters all pass the min-notional filter → ladder
    returns three (price, share) pairs in nearest→far order."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    above = [
        _cluster(price=102.0, notional=5_000_000.0, side="SHORT_LIQ"),
        _cluster(price=104.0, notional=4_000_000.0, side="SHORT_LIQ"),
        _cluster(price=108.0, notional=3_000_000.0, side="SHORT_LIQ"),
    ]
    hm = _heatmap(price=100.0, above=above)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
        tp_ladder_shares=(0.40, 0.35, 0.25),
        tp_ladder_min_notional_frac=0.30,
    )
    assert setup is not None
    assert len(setup.tp_ladder) == 3
    prices = [p for p, _ in setup.tp_ladder]
    assert prices == [102.0, 104.0, 108.0]
    shares = [s for _, s in setup.tp_ladder]
    assert sum(shares) == pytest.approx(1.0)


def test_tp_ladder_falls_back_to_single_leg_when_no_heatmap():
    state = _state(100.0, 1.0, vwap_3m=99.5)
    setup = build_zone_setup(direction=Direction.BULLISH, state=state)
    assert setup is not None
    assert setup.tp_ladder == ((setup.tp_primary, 1.0),)


def test_tp_ladder_disabled_returns_single_leg():
    state = _state(100.0, 1.0, vwap_3m=99.5)
    above = [_cluster(price=102.0, notional=5_000_000.0, side="SHORT_LIQ"),
             _cluster(price=105.0, notional=4_000_000.0, side="SHORT_LIQ")]
    hm = _heatmap(price=100.0, above=above)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
        tp_ladder_enabled=False,
    )
    assert setup is not None
    assert setup.tp_ladder == ((setup.tp_primary, 1.0),)


def test_tp_ladder_renormalises_when_fewer_clusters_pass():
    """Only one cluster clears the min_notional_frac × largest filter →
    ladder shrinks to 1 leg, share renormalises to 1.0."""
    state = _state(100.0, 1.0, vwap_3m=99.5)
    above = [
        _cluster(price=102.0, notional=10_000_000.0, side="SHORT_LIQ"),
        _cluster(price=104.0, notional=500_000.0, side="SHORT_LIQ"),   # filtered
    ]
    hm = _heatmap(price=100.0, above=above)
    setup = build_zone_setup(
        direction=Direction.BULLISH, state=state, heatmap=hm,
        tp_ladder_min_notional_frac=0.30,
    )
    assert setup is not None
    assert len(setup.tp_ladder) == 1
    assert setup.tp_ladder[0][0] == 102.0
    assert setup.tp_ladder[0][1] == pytest.approx(1.0)


# ── apply_zone_to_plan ──────────────────────────────────────────────────────


def _plan(direction: Direction = Direction.BULLISH) -> TradePlan:
    return TradePlan(
        direction=direction, entry_price=100.0, sl_price=99.0, tp_price=102.0,
        rr_ratio=2.0, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=5000.0, leverage=10, required_leverage=10.0,
        num_contracts=50, risk_amount_usdt=50.0, max_risk_usdt=50.0,
        capped=False, fee_reserve_pct=0.001,
    )


def test_apply_zone_to_plan_copies_ladder_from_zone():
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=105.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
        tp_ladder=((105.0, 0.6), (108.0, 0.4)),
    )
    new_plan = apply_zone_to_plan(_plan(), zone, contract_size=0.01)
    assert new_plan.tp_ladder == [(105.0, 0.6), (108.0, 0.4)]


def test_apply_zone_to_plan_preserves_confluence_pillar_scores():
    """Pass 2 instrumentation: zone-wrapped plan must keep the per-pillar
    weight dict so journal.record_open / record_rejected_signal stamps it.

    Pre-fix: setup_planner re-built the TradePlan and forwarded
    `confluence_factors` only — `confluence_pillar_scores` defaulted to {}
    on every zone-based entry, leaving Pass 2/3 GBT/Optuna without the
    per-pillar magnitude feature. All 5 OPEN positions at 2026-04-26 had
    empty `pillar_scores` despite populated `factors`.
    """
    plan = _plan()
    object.__setattr__(plan, "confluence_pillar_scores", {
        "mss_alignment": 1.0,
        "money_flow_alignment": 0.85,
        "vwap_composite_alignment": 1.25,
    })
    object.__setattr__(plan, "confluence_factors", [
        "mss_alignment", "money_flow_alignment", "vwap_composite_alignment",
    ])
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=105.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
        tp_ladder=((105.0, 1.0),),
    )
    new_plan = apply_zone_to_plan(plan, zone, contract_size=0.01)
    assert new_plan.confluence_pillar_scores == {
        "mss_alignment": 1.0,
        "money_flow_alignment": 0.85,
        "vwap_composite_alignment": 1.25,
    }
    # Defensive copy — mutating the new plan must not bleed into the source.
    new_plan.confluence_pillar_scores["new_key"] = 0.5
    assert "new_key" not in plan.confluence_pillar_scores


def test_apply_zone_to_plan_defaults_ladder_to_primary_when_empty():
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=105.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
        tp_ladder=(),
    )
    new_plan = apply_zone_to_plan(_plan(), zone, contract_size=0.01)
    assert new_plan.tp_ladder == [(105.0, 1.0)]


def test_apply_zone_to_plan_uses_zone_mid_for_liq_pool_near():
    """Liq-pool near entry lands at zone mid, not low edge."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=105.0,
        max_wait_bars=10,
        zone_source="liq_pool_near",
    )
    new_plan = apply_zone_to_plan(_plan(), zone, contract_size=0.01)
    assert new_plan.entry_price == pytest.approx(99.25)


def test_apply_zone_to_plan_target_rr_cap_clamps_long_tp():
    """When target_rr_cap > 0 the primary TP is forced to entry + cap × sl_dist
    on a long, regardless of how far the heatmap-driven zone.tp_primary sits."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=120.0,                 # 41R away from zone-low entry
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(
        _plan(), zone, contract_size=0.01, target_rr_cap=3.0,
    )
    # vwap_retest uses zone mid for entry → 99.25. sl=98.5 → sl_dist=0.75.
    # 1:3 → tp = 99.25 + 3*0.75 = 101.5.
    assert new_plan.entry_price == pytest.approx(99.25)
    assert new_plan.sl_price == pytest.approx(98.5)
    assert new_plan.tp_price == pytest.approx(101.5)
    assert new_plan.rr_ratio == pytest.approx(3.0)


def test_apply_zone_to_plan_target_rr_cap_clamps_short_tp():
    """Same enforcement on shorts — TP = entry - cap × sl_dist."""
    zone = ZoneSetup(
        direction=Direction.BEARISH,
        entry_zone=(101.0, 101.5),
        trigger_type="zone_touch",
        sl_beyond_zone=102.0,
        tp_primary=80.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(
        _plan(direction=Direction.BEARISH), zone, contract_size=0.01,
        target_rr_cap=3.0,
    )
    # vwap_retest uses zone mid for entry → 101.25. sl=102.0 → sl_dist=0.75.
    # tp = 101.25 - 3*0.75 = 99.0.
    assert new_plan.entry_price == pytest.approx(101.25)
    assert new_plan.sl_price == pytest.approx(102.0)
    assert new_plan.tp_price == pytest.approx(99.0)
    assert new_plan.rr_ratio == pytest.approx(3.0)


def test_apply_zone_to_plan_target_rr_cap_clamps_ladder_rungs():
    """Every ladder rung beyond the cap collapses to the boundary so the
    downstream ladder consumer never sees a 12R rung."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=120.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
        tp_ladder=((101.0, 0.4), (108.0, 0.35), (120.0, 0.25)),
    )
    new_plan = apply_zone_to_plan(
        _plan(), zone, contract_size=0.01, target_rr_cap=3.0,
    )
    # entry=99.25 (vwap_retest mid), sl=98.5 → sl_dist=0.75. boundary=101.5.
    # Rung 1 (101.0) is inside the cap → untouched. Rungs 2 and 3 clamp.
    assert new_plan.tp_ladder == [
        (pytest.approx(101.0), 0.4),
        (pytest.approx(101.5), 0.35),
        (pytest.approx(101.5), 0.25),
    ]


def test_apply_zone_to_plan_target_rr_cap_off_when_zero():
    """Cap=0 keeps the legacy heatmap-cluster TP behavior intact."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=120.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(
        _plan(), zone, contract_size=0.01, target_rr_cap=0.0,
    )
    assert new_plan.tp_price == pytest.approx(120.0)


def test_apply_zone_to_plan_target_rr_cap_after_sl_widening():
    """TP cap is re-derived from the *widened* sl_distance, not the
    structural one — preserves the 1:N contract when min_sl_distance_pct
    moves the SL outward."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.95,             # tiny structural stop, well inside floor
        tp_primary=200.0,
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(
        _plan(), zone, contract_size=0.01,
        min_sl_distance_pct=0.01,         # forces SL ≥ 1 % below entry
        target_rr_cap=3.0,
    )
    # vwap_retest → entry = mid(99.0, 99.5) = 99.25.
    # sl widened to 99.25*(1-0.01) = 98.2575 (sl_dist = 0.9925).
    # TP forced to 99.25 + 3*0.9925 = 102.2275.
    assert new_plan.entry_price == pytest.approx(99.25)
    assert new_plan.sl_price == pytest.approx(98.2575)
    assert new_plan.tp_price == pytest.approx(102.2275)
    assert new_plan.rr_ratio == pytest.approx(3.0)


def test_apply_zone_to_plan_ceil_keeps_risk_at_or_above_target_uncapped():
    """Zone re-sizing mirrors rr_system's 2026-04-19 ceil contract — un-capped
    plans round contracts UP so realized risk ≥ plan.risk_amount_usdt,
    overshoot bounded by one per_contract_cost step. Before this fix, the
    zone path floored contracts, undoing the ceil elsewhere and producing
    the $2-$13 spread the operator flagged on 2026-04-20 across 5 open
    positions."""
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),            # vwap_retest mid = 99.25
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,                # sl_dist at entry = 0.75 → sl_pct ≈ 0.756%
        tp_primary=102.25,                  # cap at 3R landing
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    plan = _plan()                          # risk_amount_usdt=50, fee_reserve_pct=0.001, not capped
    new_plan = apply_zone_to_plan(plan, zone, contract_size=0.01)
    effective_sl_pct = new_plan.sl_pct + new_plan.fee_reserve_pct
    total_realized = new_plan.position_size_usdt * effective_sl_pct
    assert total_realized >= plan.risk_amount_usdt - 1e-6
    # Overshoot bounded by one per_contract_cost step.
    ctu = new_plan.entry_price * 0.01
    per_contract_cost = effective_sl_pct * ctu
    assert total_realized <= plan.risk_amount_usdt + per_contract_cost + 1e-6


def test_apply_zone_to_plan_resizes_off_max_risk_not_realized_risk():
    """2026-04-28 DOGE 110007 regression. When the original plan's
    structural SL is far from entry (e.g. major support 8.7% below), ceil
    sizing overshoots `max_risk_usdt`: a $10 target with 8.7% SL sizes to
    2 contracts realising $17.36. Pre-fix, `apply_zone_to_plan` then
    re-targeted that inflated $17.36 against the floor-widened zone SL
    (0.6%), multiplying contracts ~14×. The fix grounds zone re-sizing
    on `plan.max_risk_usdt` (operator's invariant target) so a tighter
    zone SL re-sizes to a realistic count.
    """
    plan = TradePlan(
        direction=Direction.BULLISH,
        entry_price=0.0987, sl_price=0.0900, tp_price=0.1117,
        rr_ratio=1.5, sl_distance=0.0087, sl_pct=0.0881,
        position_size_usdt=197.4, leverage=6, required_leverage=1.17,
        num_contracts=2,
        risk_amount_usdt=17.36,         # post-ceil REALIZED, NOT the target
        max_risk_usdt=10.0,             # operator's intended target
        capped=False, fee_reserve_pct=0.001,
    )
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(0.0984, 0.0986),
        trigger_type="zone_touch",
        sl_beyond_zone=0.0982,          # ~0.4% sl_pct, below 0.6% floor
        tp_primary=0.1010,
        max_wait_bars=2,
        zone_source="ema21_pullback",
    )
    new_plan = apply_zone_to_plan(
        plan, zone, contract_size=1000.0,
        min_sl_distance_pct=0.006,      # DOGE per-symbol floor
        target_rr_cap=1.5,
    )
    # Realized risk on zone SL must track max_risk_usdt (10), not the
    # inflated risk_amount_usdt (17.36). Pre-fix: ceil(17.36/0.689)=26
    # contracts → realized ~$15.36. Post-fix: ceil(10/0.689)=15.
    assert new_plan.num_contracts == 15
    effective_sl_pct = new_plan.sl_pct + new_plan.fee_reserve_pct
    realized = new_plan.position_size_usdt * effective_sl_pct
    assert realized >= plan.max_risk_usdt - 1e-6
    ctu = new_plan.entry_price * 1000.0
    per_contract_cost = effective_sl_pct * ctu
    assert realized <= plan.max_risk_usdt + per_contract_cost + 1e-6


def test_apply_zone_to_plan_recomputes_leverage_when_margin_threaded():
    """Zone tightens SL → notional grows for fixed risk → leverage must
    grow to fit inside margin slot. Pre-fix, `apply_zone_to_plan` kept
    `plan.leverage` (sized against the wide structural SL, e.g. 6x), so
    the inflated zone notional left no room inside the per-slot margin
    budget and Bybit returned 110007 every cycle for DOGE on a $97 slot.
    """
    plan = TradePlan(
        direction=Direction.BULLISH,
        entry_price=0.0987, sl_price=0.0900, tp_price=0.1117,
        rr_ratio=1.5, sl_distance=0.0087, sl_pct=0.0881,
        position_size_usdt=197.4, leverage=6, required_leverage=1.17,
        num_contracts=2,
        risk_amount_usdt=17.36,
        max_risk_usdt=10.0,
        capped=False, fee_reserve_pct=0.001,
    )
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(0.0984, 0.0986),
        trigger_type="zone_touch",
        sl_beyond_zone=0.0982,
        tp_primary=0.1010,
        max_wait_bars=2,
        zone_source="ema21_pullback",
    )
    new_plan = apply_zone_to_plan(
        plan, zone, contract_size=1000.0,
        min_sl_distance_pct=0.006,
        target_rr_cap=1.5,
        margin_balance=97.0,
        max_leverage=30,
    )
    # Notional fits inside margin × leverage × safety buffer.
    margin_required = new_plan.position_size_usdt / new_plan.leverage
    assert margin_required <= 97.0 * 0.95 + 1e-6
    # Leverage scaled up from the original 6x.
    assert new_plan.leverage > plan.leverage


def test_apply_zone_to_plan_back_compat_keeps_plan_leverage_when_margin_zero():
    """When `margin_balance=0` (default) or `max_leverage=0`, leverage
    stays pinned to `plan.leverage` — preserves the pre-2026-04-28 contract
    for test fixtures that don't thread margin/leverage caps.
    """
    plan = _plan()  # leverage=10, max_risk_usdt=50, fee_reserve_pct=0.001
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=102.25,
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(plan, zone, contract_size=0.01)
    assert new_plan.leverage == plan.leverage  # back-compat


def test_apply_zone_to_plan_capped_plan_still_floors():
    """Capped plans (leverage/margin ceiling bound) keep the floor so the
    zone re-size never silently breaches the original leverage cap."""
    plan = TradePlan(
        direction=Direction.BULLISH, entry_price=100.0, sl_price=99.0,
        tp_price=102.0, rr_ratio=2.0, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=5000.0, leverage=10, required_leverage=10.0,
        num_contracts=50, risk_amount_usdt=50.0, max_risk_usdt=50.0,
        capped=True, fee_reserve_pct=0.001,
    )
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.0, 99.5),
        trigger_type="zone_touch",
        sl_beyond_zone=98.5,
        tp_primary=102.25,
        max_wait_bars=10,
        zone_source="vwap_retest",
    )
    new_plan = apply_zone_to_plan(plan, zone, contract_size=0.01)
    effective_sl_pct = new_plan.sl_pct + new_plan.fee_reserve_pct
    total_realized = new_plan.position_size_usdt * effective_sl_pct
    # Floor ⇒ realized ≤ target (strictly < when ceil would have crossed).
    assert total_realized <= plan.risk_amount_usdt + 1e-6
