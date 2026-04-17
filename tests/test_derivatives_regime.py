"""Unit tests for src/analysis/derivatives_regime.py (Phase 1.5 Madde 5)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.analysis.derivatives_regime import (
    DEFAULT_THRESHOLDS,
    Regime,
    classify_regime,
    resolve_thresholds,
)


@dataclass
class FakeState:
    coinalyze_snapshot_age_s: float = 0.0
    long_liq_notional_1h: float = 0.0
    short_liq_notional_1h: float = 0.0
    liq_imbalance_1h: float = 0.0
    funding_rate_zscore_30d: float = 0.0
    ls_ratio_zscore_14d: float = 0.0
    oi_change_24h_pct: float = 0.0


def test_stale_snapshot_returns_unknown():
    state = FakeState(coinalyze_snapshot_age_s=600.0)
    a = classify_regime(state, stale_snapshot_s=180.0)
    assert a.regime == Regime.UNKNOWN
    assert a.confidence == 0.0


def test_capitulation_wins_over_crowded_when_both_conditions_met():
    """Large 1h liq + hot funding/LS → CAPITULATION, not LONG_CROWDED."""
    state = FakeState(
        long_liq_notional_1h=12_000_000.0, short_liq_notional_1h=2_000_000.0,
        funding_rate_zscore_30d=3.0, ls_ratio_zscore_14d=3.0,
    )
    a = classify_regime(
        state,
        capitulation_liq_notional=10_000_000.0,
        funding_crowded_z=2.0, ls_crowded_z=2.0,
    )
    assert a.regime == Regime.CAPITULATION


def test_long_crowded_when_funding_and_ls_both_hot():
    state = FakeState(
        funding_rate_zscore_30d=2.5, ls_ratio_zscore_14d=2.5,
        oi_change_24h_pct=9.0,
    )
    a = classify_regime(state, funding_crowded_z=2.0, ls_crowded_z=2.0,
                        oi_surge_pct=8.0)
    assert a.regime == Regime.LONG_CROWDED
    assert a.confidence > 0.0
    assert any("oi_24h" in r and "surging" in r for r in a.reasoning)


def test_short_crowded_when_funding_and_ls_both_cold():
    state = FakeState(
        funding_rate_zscore_30d=-2.5, ls_ratio_zscore_14d=-2.5,
        oi_change_24h_pct=-15.0,
    )
    a = classify_regime(state, funding_crowded_z=2.0, ls_crowded_z=2.0,
                        oi_crash_pct=-10.0)
    assert a.regime == Regime.SHORT_CROWDED
    assert any("crashing" in r for r in a.reasoning)


def test_balanced_when_nothing_extreme():
    state = FakeState(
        funding_rate_zscore_30d=0.5, ls_ratio_zscore_14d=-0.3,
    )
    a = classify_regime(state)
    assert a.regime == Regime.BALANCED


def test_boundary_one_side_hot_other_neutral_is_balanced():
    """High funding alone isn't enough — LS must also be elevated."""
    state = FakeState(
        funding_rate_zscore_30d=3.0, ls_ratio_zscore_14d=0.5,
    )
    a = classify_regime(state, funding_crowded_z=2.0, ls_crowded_z=2.0)
    assert a.regime == Regime.BALANCED


def test_resolve_thresholds_layers_overrides():
    base = DEFAULT_THRESHOLDS
    overrides = {"SOL-USDT-SWAP": {"capitulation_liq_notional": 8_000_000.0}}
    merged = resolve_thresholds("SOL-USDT-SWAP", base, overrides)
    assert merged["capitulation_liq_notional"] == 8_000_000.0
    # Non-overridden keys retain base values.
    assert merged["funding_crowded_z"] == base["funding_crowded_z"]
    # Unrelated symbol gets the full base set.
    assert resolve_thresholds("BTC-USDT-SWAP", base, overrides) == base


def test_cache_refresh_sets_state_regime(monkeypatch):
    """Spot-check: DerivativesCache._refresh_one should stamp state.regime."""
    import asyncio

    from src.data.derivatives_api import DerivativesSnapshot
    from src.data.derivatives_cache import DerivativesCache

    class FakeStream:
        def stats(self, symbol, lookback_ms):
            # Large liq wash → CAPITULATION.
            if lookback_ms == 60 * 60 * 1000:
                return {
                    "long_liq_notional": 6_000_000.0,
                    "short_liq_notional": 6_000_000.0,
                    "long_liq_count": 10, "short_liq_count": 10,
                    "max_liq_notional": 1_000_000.0,
                }
            return {
                "long_liq_notional": 0.0, "short_liq_notional": 0.0,
                "long_liq_count": 0, "short_liq_count": 0,
                "max_liq_notional": 0.0,
            }

    class FakeCoinalyze:
        async def fetch_snapshot(self, okx_symbol):
            return DerivativesSnapshot(
                symbol=okx_symbol, ts_ms=0,
                funding_rate_current=0.01,
                open_interest_usd=500_000.0,
                long_short_ratio=1.1,
            )
        async def ensure_symbol_map(self, w): return None
        def coinalyze_symbol(self, s): return f"{s}.FAKE"
        async def fetch_oi_change_pct(self, c, lookback_hours): return None

    class FakeJournal:
        async def insert_snapshot(self, s): pass

    cache = DerivativesCache(
        watched=["BTC-USDT-SWAP"], liq_stream=FakeStream(),
        coinalyze=FakeCoinalyze(), journal=FakeJournal(),
        refresh_interval_s=60.0, oi_refresh_every_n_cycles=1000,
    )
    asyncio.run(cache._refresh_one("BTC-USDT-SWAP"))
    # 12M total 1h liq ≥ 10M default → CAPITULATION.
    assert cache.get("BTC-USDT-SWAP").regime == "CAPITULATION"
