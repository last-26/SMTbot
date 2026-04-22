"""Tests for the 2026-04-23 extended `_derive_enrichment` + helpers.

Covers:
  * `_timeframe_to_minutes` — TV-string → int conversion + fallback
  * `_price_change_pct` — candle-buffer-derived change + edge cases
  * `_top_n_heatmap_clusters` — top-N extraction + distance_atr math
  * `_derive_enrichment` — new DerivativesState fields + heatmap JSON +
    price_change integration + backward-compat (candles=None)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from src.bot.runner import (
    _derive_enrichment,
    _price_change_pct,
    _timeframe_to_minutes,
    _top_n_heatmap_clusters,
)
from src.data.models import MarketState


# ── _timeframe_to_minutes ──────────────────────────────────────────────────


def test_timeframe_to_minutes_minute_suffix():
    assert _timeframe_to_minutes("3m") == 3
    assert _timeframe_to_minutes("15m") == 15
    assert _timeframe_to_minutes("1M") == 1


def test_timeframe_to_minutes_hour_suffix():
    assert _timeframe_to_minutes("1h") == 60
    assert _timeframe_to_minutes("4H") == 240


def test_timeframe_to_minutes_bare_int():
    assert _timeframe_to_minutes("5") == 5


def test_timeframe_to_minutes_fallback_on_garbage():
    # Unrecognised → falls back to 3 (entry TF default).
    assert _timeframe_to_minutes("weird") == 3
    assert _timeframe_to_minutes("") == 3
    assert _timeframe_to_minutes("3D") == 3  # "D" suffix unsupported


# ── _price_change_pct ──────────────────────────────────────────────────────


@dataclass
class _FakeCandle:
    close: float


def test_price_change_none_on_empty_buffer():
    assert _price_change_pct(None, 20) is None
    assert _price_change_pct([], 20) is None


def test_price_change_none_when_buffer_too_short():
    buf = [_FakeCandle(close=100.0) for _ in range(5)]
    # bars_ago=20 but only 5 candles → None, not IndexError.
    assert _price_change_pct(buf, 20) is None


def test_price_change_positive_rally():
    # 21 candles so bars_ago=20 has a valid past close.
    buf = [_FakeCandle(close=100.0)] * 21
    buf[-1] = _FakeCandle(close=105.0)
    result = _price_change_pct(buf, 20)
    assert result == pytest.approx(5.0)


def test_price_change_negative_drop():
    buf = [_FakeCandle(close=100.0)] * 81  # 4h lookback on 3m (80 bars)
    buf[-1] = _FakeCandle(close=95.0)
    result = _price_change_pct(buf, 80)
    assert result == pytest.approx(-5.0)


def test_price_change_zero_price_returns_none():
    buf = [_FakeCandle(close=0.0)] * 21
    buf[-1] = _FakeCandle(close=100.0)
    # Past close zero → defensive None.
    assert _price_change_pct(buf, 20) is None


def test_price_change_malformed_candle_returns_none():
    buf = [object(), object()]  # no .close attribute
    assert _price_change_pct(buf, 1) is None


# ── _top_n_heatmap_clusters ────────────────────────────────────────────────


@dataclass
class _FakeCluster:
    price: float
    notional_usd: float


@dataclass
class _FakeHeatmap:
    clusters_above: list = field(default_factory=list)
    clusters_below: list = field(default_factory=list)


def test_top_n_returns_empty_on_none_heatmap():
    assert _top_n_heatmap_clusters(None, current_price=100.0, atr=1.0) == {}


def test_top_n_returns_empty_when_no_clusters():
    hm = _FakeHeatmap()
    assert _top_n_heatmap_clusters(hm, current_price=100.0, atr=1.0) == {}


def test_top_n_extracts_both_sides():
    hm = _FakeHeatmap(
        clusters_above=[
            _FakeCluster(price=101.0, notional_usd=5_000_000),
            _FakeCluster(price=103.0, notional_usd=10_000_000),
            _FakeCluster(price=107.0, notional_usd=2_000_000),
        ],
        clusters_below=[
            _FakeCluster(price=98.0, notional_usd=8_000_000),
            _FakeCluster(price=94.0, notional_usd=15_000_000),
        ],
    )
    result = _top_n_heatmap_clusters(
        hm, current_price=100.0, atr=2.0, top_n=5,
    )
    assert set(result.keys()) == {"above", "below"}
    assert len(result["above"]) == 3
    assert len(result["below"]) == 2

    # Check shape + distance_atr math.
    first_above = result["above"][0]
    assert first_above["price"] == pytest.approx(101.0)
    assert first_above["notional_usd"] == pytest.approx(5_000_000)
    assert first_above["distance_atr"] == pytest.approx(0.5)   # (101-100)/2

    first_below = result["below"][0]
    assert first_below["distance_atr"] == pytest.approx(1.0)   # (100-98)/2


def test_top_n_respects_top_n_limit():
    hm = _FakeHeatmap(
        clusters_above=[_FakeCluster(price=100+i, notional_usd=1e6) for i in range(10)],
        clusters_below=[_FakeCluster(price=100-i, notional_usd=1e6) for i in range(1, 11)],
    )
    result = _top_n_heatmap_clusters(hm, current_price=100.0, atr=1.0, top_n=3)
    assert len(result["above"]) == 3
    assert len(result["below"]) == 3


def test_top_n_distance_atr_none_when_atr_zero():
    hm = _FakeHeatmap(
        clusters_above=[_FakeCluster(price=101.0, notional_usd=1e6)],
    )
    result = _top_n_heatmap_clusters(hm, current_price=100.0, atr=0.0, top_n=5)
    assert result["above"][0]["distance_atr"] is None


def test_top_n_distance_atr_none_when_price_zero():
    hm = _FakeHeatmap(
        clusters_above=[_FakeCluster(price=101.0, notional_usd=1e6)],
    )
    result = _top_n_heatmap_clusters(hm, current_price=0.0, atr=1.0, top_n=5)
    assert result["above"][0]["distance_atr"] is None


# ── _derive_enrichment extended fields ─────────────────────────────────────


@dataclass
class _FakeDerivatives:
    regime: str = "BALANCED"
    funding_rate_zscore_30d: float = 0.5
    long_short_ratio: float = 1.2
    oi_change_24h_pct: float = 3.0
    liq_imbalance_1h: float = 0.1
    # New extended fields
    open_interest_usd: float = 1_500_000_000
    oi_change_1h_pct: float = 0.5
    funding_rate_current: float = 0.0001
    funding_rate_predicted: float = 0.00015
    long_liq_notional_1h: float = 2_000_000
    short_liq_notional_1h: float = 1_500_000
    ls_ratio_zscore_14d: float = 1.8


def _make_state(deriv=None, heatmap=None, price=100.0, atr=1.0):
    ms = MarketState()
    # `current_price` + `atr` are read-only properties on MarketState;
    # set the underlying signal_table fields instead.
    ms.signal_table.price = price
    ms.signal_table.atr_14 = atr
    if deriv is not None:
        ms.derivatives = deriv
    if heatmap is not None:
        ms.liquidity_heatmap = heatmap
    return ms


def test_derive_enrichment_pulls_all_extended_derivatives_fields():
    state = _make_state(deriv=_FakeDerivatives())
    result = _derive_enrichment(state)
    assert result["open_interest_usd_at_entry"] == pytest.approx(1_500_000_000)
    assert result["oi_change_1h_pct_at_entry"] == pytest.approx(0.5)
    assert result["funding_rate_current_at_entry"] == pytest.approx(0.0001)
    assert result["funding_rate_predicted_at_entry"] == pytest.approx(0.00015)
    assert result["long_liq_notional_1h_at_entry"] == pytest.approx(2_000_000)
    assert result["short_liq_notional_1h_at_entry"] == pytest.approx(1_500_000)
    assert result["ls_ratio_zscore_14d_at_entry"] == pytest.approx(1.8)


def test_derive_enrichment_none_when_derivatives_missing():
    state = _make_state(deriv=None)
    result = _derive_enrichment(state)
    # All new REAL fields stay None when state.derivatives is absent.
    for k in (
        "open_interest_usd_at_entry",
        "oi_change_1h_pct_at_entry",
        "funding_rate_current_at_entry",
        "funding_rate_predicted_at_entry",
        "long_liq_notional_1h_at_entry",
        "short_liq_notional_1h_at_entry",
        "ls_ratio_zscore_14d_at_entry",
    ):
        assert result[k] is None


def test_derive_enrichment_price_change_computes_when_candles_provided():
    candles = [_FakeCandle(close=100.0)] * 81
    candles[-1] = _FakeCandle(close=102.0)
    state = _make_state()
    result = _derive_enrichment(state, candles=candles, entry_tf_minutes=3)
    # 1h on 3m TF = 20 bars back, price was 100, now 102 → +2%.
    assert result["price_change_1h_pct_at_entry"] == pytest.approx(2.0)
    # 4h on 3m TF = 80 bars back, same → +2%.
    assert result["price_change_4h_pct_at_entry"] == pytest.approx(2.0)


def test_derive_enrichment_price_change_none_without_candles():
    state = _make_state()
    result = _derive_enrichment(state, candles=None)
    assert result["price_change_1h_pct_at_entry"] is None
    assert result["price_change_4h_pct_at_entry"] is None


def test_derive_enrichment_heatmap_top_clusters_populated():
    hm = _FakeHeatmap(
        clusters_above=[_FakeCluster(price=101.0, notional_usd=1e6)],
        clusters_below=[_FakeCluster(price=99.0, notional_usd=2e6)],
    )
    state = _make_state(heatmap=hm, price=100.0, atr=0.5)
    result = _derive_enrichment(state)
    clusters = result["liq_heatmap_top_clusters"]
    assert "above" in clusters and "below" in clusters
    assert clusters["above"][0]["distance_atr"] == pytest.approx(2.0)  # (101-100)/0.5
    assert clusters["below"][0]["distance_atr"] == pytest.approx(2.0)  # (100-99)/0.5


def test_derive_enrichment_backward_compat_no_candles_no_heatmap():
    """Existing call sites pass only `state` — helper must still return
    a complete dict with the new keys defaulted to None / empty dict."""
    state = _make_state()
    result = _derive_enrichment(state)
    # New 2026-04-23 keys present with safe defaults.
    for k in (
        "open_interest_usd_at_entry",
        "oi_change_1h_pct_at_entry",
        "funding_rate_current_at_entry",
        "funding_rate_predicted_at_entry",
        "long_liq_notional_1h_at_entry",
        "short_liq_notional_1h_at_entry",
        "ls_ratio_zscore_14d_at_entry",
        "price_change_1h_pct_at_entry",
        "price_change_4h_pct_at_entry",
    ):
        assert k in result
        assert result[k] is None
    assert result["liq_heatmap_top_clusters"] == {}


def test_derive_enrichment_entry_tf_minutes_zero_skips_price_change():
    """Guard against config glitch passing entry_tf_minutes=0 — should not
    infinite-loop or divide-by-zero; just skip price_change."""
    candles = [_FakeCandle(close=100.0)] * 50
    state = _make_state()
    result = _derive_enrichment(state, candles=candles, entry_tf_minutes=0)
    assert result["price_change_1h_pct_at_entry"] is None
    assert result["price_change_4h_pct_at_entry"] is None
