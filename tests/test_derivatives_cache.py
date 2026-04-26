"""Unit tests for src/data/derivatives_cache.py + derivatives_journal.py
(Phase 1.5 Madde 3).

Both are I/O-heavy by design — we mock LiquidationStream / CoinalyzeClient /
DerivativesJournal so the refresh path is exercised without network or disk.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass

import pytest

from src.data.derivatives_api import DerivativesSnapshot
from src.data.derivatives_cache import DerivativesCache, DerivativesState


# ── Fakes ─────────────────────────────────────────────────────────────────


class FakeLiqStream:
    def __init__(self, stats_map: dict[tuple[str, int], dict]):
        # stats_map key: (symbol, lookback_ms) -> stats dict
        self.stats_map = stats_map

    def stats(self, symbol: str, lookback_ms: int) -> dict:
        return self.stats_map.get(
            (symbol, lookback_ms),
            {"long_liq_notional": 0.0, "short_liq_notional": 0.0,
             "long_liq_count": 0, "short_liq_count": 0,
             "max_liq_notional": 0.0},
        )


class FakeCoinalyze:
    def __init__(self, *, snapshot=None, funding_hist=None, ls_hist=None,
                 oi_change_map=None, symbol_map=None):
        self._snapshot = snapshot
        self._funding_hist = funding_hist or []
        self._ls_hist = ls_hist or []
        self._oi_change_map = oi_change_map or {}
        self._symbol_map = symbol_map or {}
        self.ensure_called = 0
        self.snapshot_calls = 0

    async def ensure_symbol_map(self, watched: list[str]) -> None:
        self.ensure_called += 1

    def coinalyze_symbol(self, internal_symbol: str) -> str | None:
        return self._symbol_map.get(internal_symbol, f"{internal_symbol}.FAKE")

    async def fetch_funding_history_series(self, cn_sym, interval, lookback_hours):
        return list(self._funding_hist)

    async def fetch_ls_ratio_history_series(self, cn_sym, interval, lookback_hours):
        return list(self._ls_hist)

    async def fetch_snapshot(self, internal_symbol: str):
        self.snapshot_calls += 1
        return self._snapshot

    async def fetch_oi_change_pct(self, cn_sym, lookback_hours):
        return self._oi_change_map.get(lookback_hours)


class FakeJournal:
    def __init__(self):
        self.inserted: list = []

    async def insert_snapshot(self, snap) -> None:
        self.inserted.append(snap)


# ── _zscore ───────────────────────────────────────────────────────────────


def test_zscore_needs_at_least_10_samples():
    assert DerivativesCache._zscore(1.0, [0.5] * 5) == 0.0


def test_zscore_zero_stdev_returns_zero():
    assert DerivativesCache._zscore(1.0, [1.0] * 20) == 0.0


def test_zscore_handles_high_value():
    hist = [0.0] * 99 + [1.0]
    mean = statistics.mean(hist)
    stdev = statistics.stdev(hist)
    expected = (1.0 - mean) / stdev
    assert DerivativesCache._zscore(1.0, hist) == pytest.approx(expected)


# ── _refresh_one ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_one_uses_liq_stream_and_coinalyze():
    snap = DerivativesSnapshot(
        symbol="BTC-USDT-SWAP", ts_ms=123,
        funding_rate_current=0.02, funding_rate_predicted=0.015,
        open_interest_usd=1_000_000.0,
        long_short_ratio=1.4, long_share=0.58, short_share=0.42,
        aggregated_long_liq_1h_usd=50_000.0,
        aggregated_short_liq_1h_usd=25_000.0,
    )
    stats_map = {
        ("BTC-USDT-SWAP", 60 * 60 * 1000): {
            "long_liq_notional": 10_000.0, "short_liq_notional": 30_000.0,
            "long_liq_count": 1, "short_liq_count": 2, "max_liq_notional": 20_000.0,
        },
        ("BTC-USDT-SWAP", 4 * 60 * 60 * 1000): {
            "long_liq_notional": 80_000.0, "short_liq_notional": 90_000.0,
            "long_liq_count": 3, "short_liq_count": 4, "max_liq_notional": 40_000.0,
        },
    }
    liq = FakeLiqStream(stats_map)
    coin = FakeCoinalyze(snapshot=snap)
    journal = FakeJournal()
    cache = DerivativesCache(
        watched=["BTC-USDT-SWAP"], liq_stream=liq, coinalyze=coin, journal=journal,
        refresh_interval_s=60.0, oi_refresh_every_n_cycles=1000,   # skip OI branch
    )
    await cache._refresh_one("BTC-USDT-SWAP")
    state = cache.get("BTC-USDT-SWAP")

    # Coinalyze aggregated > WS sum, so the fallback kicks in on the long side.
    assert state.long_liq_notional_1h == 50_000.0
    # WS short sum (30k) > Coinalyze short (25k) → keep WS value.
    assert state.short_liq_notional_1h == 30_000.0
    # 4h stats flow through unchanged from WS.
    assert state.long_liq_notional_4h == 80_000.0
    assert state.funding_rate_current == pytest.approx(0.02)
    assert state.open_interest_usd == pytest.approx(1_000_000.0)
    assert state.coinalyze_snapshot_age_s == 0.0
    # One persisted row.
    assert len(journal.inserted) == 1


@pytest.mark.asyncio
async def test_refresh_one_handles_none_snapshot():
    liq = FakeLiqStream({
        ("BTC-USDT-SWAP", 60 * 60 * 1000): {
            "long_liq_notional": 10_000.0, "short_liq_notional": 10_000.0,
            "long_liq_count": 0, "short_liq_count": 0, "max_liq_notional": 0.0,
        },
        ("BTC-USDT-SWAP", 4 * 60 * 60 * 1000): {
            "long_liq_notional": 0.0, "short_liq_notional": 0.0,
            "long_liq_count": 0, "short_liq_count": 0, "max_liq_notional": 0.0,
        },
    })
    coin = FakeCoinalyze(snapshot=None)
    journal = FakeJournal()
    cache = DerivativesCache(
        watched=["BTC-USDT-SWAP"], liq_stream=liq, coinalyze=coin, journal=journal,
        refresh_interval_s=60.0, oi_refresh_every_n_cycles=1000,
    )
    cache._states["BTC-USDT-SWAP"].coinalyze_snapshot_age_s = 0.0
    await cache._refresh_one("BTC-USDT-SWAP")
    state = cache.get("BTC-USDT-SWAP")
    # Snapshot was None → age bumped by refresh interval, no journal insert.
    assert state.coinalyze_snapshot_age_s == pytest.approx(60.0)
    assert journal.inserted == []


@pytest.mark.asyncio
async def test_refresh_one_oi_refresh_every_n_cycles():
    snap = DerivativesSnapshot(
        symbol="BTC-USDT-SWAP", ts_ms=123,
        funding_rate_current=0.01,
        open_interest_usd=500_000.0,
        long_short_ratio=1.2,
    )
    liq = FakeLiqStream({})
    coin = FakeCoinalyze(
        snapshot=snap,
        oi_change_map={24: 12.5, 1: 0.8},
    )
    cache = DerivativesCache(
        watched=["BTC-USDT-SWAP"], liq_stream=liq, coinalyze=coin,
        journal=FakeJournal(), refresh_interval_s=60.0,
        oi_refresh_every_n_cycles=3,
    )
    # Two refreshes → counter at 2, OI untouched.
    await cache._refresh_one("BTC-USDT-SWAP")
    await cache._refresh_one("BTC-USDT-SWAP")
    assert cache.get("BTC-USDT-SWAP").oi_change_24h_pct == 0.0
    # Third refresh triggers OI fetch.
    await cache._refresh_one("BTC-USDT-SWAP")
    state = cache.get("BTC-USDT-SWAP")
    assert state.oi_change_24h_pct == 12.5
    assert state.oi_change_1h_pct == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_zscore_updates_from_history():
    """Once the funding buffer has ≥10 samples the state z-score is non-zero."""
    snap = DerivativesSnapshot(
        symbol="BTC-USDT-SWAP", ts_ms=0,
        funding_rate_current=0.1,
        open_interest_usd=0.0,
        long_short_ratio=1.0,
    )
    liq = FakeLiqStream({})
    # Seed the history via the start() path.
    coin = FakeCoinalyze(
        snapshot=snap,
        funding_hist=[0.0] * 30,
        ls_hist=[1.0] * 30,
    )
    cache = DerivativesCache(
        watched=["BTC-USDT-SWAP"], liq_stream=liq, coinalyze=coin,
        journal=FakeJournal(), refresh_interval_s=60.0,
        oi_refresh_every_n_cycles=1000,
    )
    # Load history manually (skip asyncio.create_task in start()).
    cache._funding_history["BTC-USDT-SWAP"] = [0.0] * 30
    cache._ls_history["BTC-USDT-SWAP"] = [1.0] * 30

    await cache._refresh_one("BTC-USDT-SWAP")
    state = cache.get("BTC-USDT-SWAP")
    # funding_current=0.1 vs history of 30 zeros → high positive z-score.
    assert state.funding_rate_zscore_30d > 3.0


# ── Journal ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_derivatives_journal_round_trip(tmp_path):
    from src.data.liquidation_stream import LiquidationEvent
    from src.journal.derivatives_journal import DerivativesJournal

    j = DerivativesJournal(str(tmp_path / "trades.db"))
    await j.ensure_schema()

    ev = LiquidationEvent(
        symbol="BTC-USDT-SWAP", side="LONG_LIQ",
        price=70_000.0, quantity=0.5, notional_usd=35_000.0,
        ts_ms=123_456,
    )
    await j.insert_liquidation(ev)

    snap = DerivativesSnapshot(
        symbol="BTC-USDT-SWAP", ts_ms=234_567,
        funding_rate_current=0.01,
        open_interest_usd=1_000_000.0,
        long_short_ratio=1.3,
    )
    await j.insert_snapshot(snap)

    # Reads should succeed even with no data window restriction issues.
    hist = await j.fetch_funding_history("BTC-USDT-SWAP",
                                         lookback_ms=10**15)
    assert any(ts == 234_567 for ts, _ in hist)
