"""Unit tests for ``src/data/kline_cache.py`` (Pass 3 replay tune).

Tests focus on the cache contract: get/put roundtrip, key uniqueness,
miss-with-fetcher falls back, miss-without-fetcher raises clearly,
malformed Bybit response handling, DESC→ASC sort.

Bybit pybit HTTP is replaced with a stub fetcher implementing the
minimal ``get_kline`` Protocol — no network, no pybit dep needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.data.kline_cache import (
    Kline,
    KlineCache,
    _cache_key,
    _normalize_kline_response,
)


# ── Stub fetcher ────────────────────────────────────────────────────────────


class _StubFetcher:
    """Returns a fixed Bybit-shaped response and counts calls."""

    def __init__(self, klines_desc: list[list]):
        self._payload = {"result": {"list": klines_desc}}
        self.call_count = 0

    def get_kline(self, **kwargs) -> dict:
        self.call_count += 1
        return self._payload


def _bybit_row(ts_ms: int, *, low: float, high: float,
               o: float = 0.0, c: float = 0.0) -> list:
    """Build a Bybit V5 kline row [start, o, h, l, c, vol, turnover]."""
    return [
        str(ts_ms),
        str(o or low),
        str(high),
        str(low),
        str(c or high),
        "1",
        "100",
    ]


# ── _cache_key uniqueness ───────────────────────────────────────────────────


def test_cache_key_changes_with_every_dimension():
    base = dict(bybit_symbol="BTCUSDT", interval_minutes=3,
                start_ms=1000, max_bars=100)
    key0 = _cache_key(**base)
    assert _cache_key(**{**base, "bybit_symbol": "ETHUSDT"}) != key0
    assert _cache_key(**{**base, "interval_minutes": 5}) != key0
    assert _cache_key(**{**base, "start_ms": 2000}) != key0
    assert _cache_key(**{**base, "max_bars": 50}) != key0


# ── _normalize_kline_response ───────────────────────────────────────────────


def test_normalize_flips_desc_to_asc():
    raw = {"result": {"list": [
        _bybit_row(3000, low=99, high=101),
        _bybit_row(2000, low=99, high=101),
        _bybit_row(1000, low=99, high=101),
    ]}}
    out = _normalize_kline_response(raw)
    assert [k.bar_start_ms for k in out] == [1000, 2000, 3000]


def test_normalize_skips_malformed_rows():
    raw = {"result": {"list": [
        _bybit_row(1000, low=99, high=101),
        ["bad-row"],
        _bybit_row(2000, low=99, high=101),
    ]}}
    out = _normalize_kline_response(raw)
    assert len(out) == 2
    assert [k.bar_start_ms for k in out] == [1000, 2000]


def test_normalize_empty_payload_variants():
    assert _normalize_kline_response({}) == []
    assert _normalize_kline_response({"result": {}}) == []
    assert _normalize_kline_response({"result": {"list": None}}) == []


# ── put/get roundtrip ──────────────────────────────────────────────────────


def test_put_get_roundtrip_preserves_klines(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    klines = [
        Kline(bar_start_ms=1000, open=100.0, high=101.0, low=99.0, close=100.5),
        Kline(bar_start_ms=2000, open=100.5, high=102.0, low=100.0, close=101.5),
    ]
    cache.put(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=2, klines=klines,
    )
    out = cache.get(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=2,
    )
    assert out == klines


def test_get_returns_none_on_miss(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    out = cache.get(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=9999, max_bars=100,
    )
    assert out is None


def test_put_is_idempotent_on_collision(tmp_path):
    """Same cache_key can be re-put with fresh data — INSERT OR REPLACE."""
    cache = KlineCache(tmp_path / "cache.db")
    k_v1 = [Kline(bar_start_ms=1000, open=1, high=2, low=1, close=1.5)]
    k_v2 = [Kline(bar_start_ms=1000, open=10, high=20, low=10, close=15)]
    common = dict(bybit_symbol="BTCUSDT", interval_minutes=3,
                  start_ms=1000, max_bars=1)
    cache.put(**common, klines=k_v1)
    cache.put(**common, klines=k_v2)
    out = cache.get(**common)
    assert out == k_v2


# ── get_or_fetch behavior ──────────────────────────────────────────────────


def test_get_or_fetch_uses_cache_on_hit_no_fetcher_call(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    klines = [Kline(bar_start_ms=1000, open=1, high=2, low=1, close=1.5)]
    cache.put(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=1, klines=klines,
    )
    fetcher = _StubFetcher([])  # would return empty if called
    out = cache.get_or_fetch(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=1, fetcher=fetcher,
    )
    assert out == klines
    assert fetcher.call_count == 0  # cache hit, fetcher untouched


def test_get_or_fetch_falls_back_to_fetcher_on_miss(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    fetcher = _StubFetcher([
        _bybit_row(1000, low=99, high=101),
        _bybit_row(2000, low=99, high=101),
    ])
    out = cache.get_or_fetch(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=2, fetcher=fetcher,
    )
    assert fetcher.call_count == 1
    assert len(out) == 2
    # And on the next call: cache hit, no second fetch
    out2 = cache.get_or_fetch(
        bybit_symbol="BTCUSDT", interval_minutes=3,
        start_ms=1000, max_bars=2, fetcher=fetcher,
    )
    assert fetcher.call_count == 1
    assert out2 == out


def test_get_or_fetch_raises_on_miss_without_fetcher(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    with pytest.raises(RuntimeError, match="kline cache miss"):
        cache.get_or_fetch(
            bybit_symbol="BTCUSDT", interval_minutes=3,
            start_ms=9999, max_bars=100, fetcher=None,
        )


def test_different_max_bars_get_different_cache_rows(tmp_path):
    """Same start_ms but different max_bars must not alias."""
    cache = KlineCache(tmp_path / "cache.db")
    k_short = [Kline(bar_start_ms=1000, open=1, high=2, low=1, close=1.5)]
    k_long = [
        Kline(bar_start_ms=1000, open=1, high=2, low=1, close=1.5),
        Kline(bar_start_ms=2000, open=2, high=3, low=2, close=2.5),
    ]
    cache.put(bybit_symbol="BTCUSDT", interval_minutes=3,
              start_ms=1000, max_bars=1, klines=k_short)
    cache.put(bybit_symbol="BTCUSDT", interval_minutes=3,
              start_ms=1000, max_bars=2, klines=k_long)
    assert len(cache.get(bybit_symbol="BTCUSDT", interval_minutes=3,
                         start_ms=1000, max_bars=1)) == 1
    assert len(cache.get(bybit_symbol="BTCUSDT", interval_minutes=3,
                         start_ms=1000, max_bars=2)) == 2


# ── stats ──────────────────────────────────────────────────────────────────


def test_stats_reports_row_count_and_timestamps(tmp_path):
    cache = KlineCache(tmp_path / "cache.db")
    assert cache.stats() == {"n_rows": 0, "oldest": None, "newest": None}
    cache.put(bybit_symbol="BTCUSDT", interval_minutes=3,
              start_ms=1000, max_bars=1,
              klines=[Kline(1000, 1, 2, 1, 1.5)])
    s = cache.stats()
    assert s["n_rows"] == 1
    assert s["oldest"] is not None
    assert s["newest"] is not None


# ── On-disk persistence (re-instantiate same path) ─────────────────────────


def test_cache_survives_reopen(tmp_path):
    db = tmp_path / "cache.db"
    klines = [Kline(bar_start_ms=1000, open=1, high=2, low=1, close=1.5)]
    cache_a = KlineCache(db)
    cache_a.put(bybit_symbol="BTCUSDT", interval_minutes=3,
                start_ms=1000, max_bars=1, klines=klines)
    # Reopen with a fresh instance pointing at the same file
    cache_b = KlineCache(db)
    out = cache_b.get(bybit_symbol="BTCUSDT", interval_minutes=3,
                      start_ms=1000, max_bars=1)
    assert out == klines
