"""Tests for `scripts/peg_rejected_outcomes.py` (Pass 2.5).

The walk algorithm is the load-bearing piece — Bybit kline fetch is
trivial wrapping. These tests pin the per-direction WIN/LOSS/TIMEOUT
contract plus the same-bar pessimistic SL-first rule that lets the
peg result reflect a realistic worst-case fill (real exchanges trigger
on price irrespective of bar high/low ordering and we can't recover
intra-bar tick order from candles).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.peg_rejected_outcomes import (
    Kline,
    PegInput,
    _expand_symbol_filter,
    _normalize_kline_response,
    _signal_ts_to_bar_start_ms,
    walk_klines,
)


UTC = timezone.utc


def _kline(start_ms: int, *, low: float, high: float, o: float = 0.0,
           c: float = 0.0) -> Kline:
    return Kline(
        bar_start_ms=start_ms, low=low, high=high,
        open=o or low, close=c or high,
    )


# ── walk_klines: LONG ────────────────────────────────────────────────────────


def test_walk_long_sl_hit_first_returns_loss():
    klines = [
        _kline(1, low=99.5, high=100.5),
        _kline(2, low=99.0, high=100.0),  # bar idx 1: low touches sl=99 → LOSS
        _kline(3, low=99.5, high=102.0),  # would have been WIN if not for sl
    ]
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0,
        proposed_tp_price=101.5,
        klines=klines,
    )
    assert res.outcome == "LOSS"
    assert res.bars_to_sl == 1
    assert res.bars_to_tp is None


def test_walk_long_tp_hit_first_returns_win():
    klines = [
        _kline(1, low=99.5, high=100.5),
        _kline(2, low=99.5, high=101.5),  # bar idx 1: high reaches tp=101.5 → WIN
        _kline(3, low=99.0, high=99.5),   # would have been LOSS if walked further
    ]
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0,
        proposed_tp_price=101.5,
        klines=klines,
    )
    assert res.outcome == "WIN"
    assert res.bars_to_tp == 1
    assert res.bars_to_sl is None


def test_walk_long_first_bar_is_index_zero():
    """SL hit on the very first bar = bars_to_sl=0, not 1."""
    klines = [_kline(1, low=99.0, high=100.0)]
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0, proposed_tp_price=102.0,
        klines=klines,
    )
    assert res.outcome == "LOSS"
    assert res.bars_to_sl == 0


# ── walk_klines: SHORT ───────────────────────────────────────────────────────


def test_walk_short_sl_hit_first_returns_loss():
    klines = [
        _kline(1, low=99.5, high=100.5),
        _kline(2, low=99.5, high=101.0),  # bar idx 1: high hits sl=101 → LOSS
    ]
    res = walk_klines(
        direction="BEARISH",
        proposed_sl_price=101.0,
        proposed_tp_price=98.5,
        klines=klines,
    )
    assert res.outcome == "LOSS"
    assert res.bars_to_sl == 1
    assert res.bars_to_tp is None


def test_walk_short_tp_hit_first_returns_win():
    klines = [
        _kline(1, low=99.5, high=100.5),
        _kline(2, low=98.5, high=100.0),  # bar idx 1: low reaches tp=98.5 → WIN
    ]
    res = walk_klines(
        direction="BEARISH",
        proposed_sl_price=101.0,
        proposed_tp_price=98.5,
        klines=klines,
    )
    assert res.outcome == "WIN"
    assert res.bars_to_tp == 1
    assert res.bars_to_sl is None


# ── Same-bar collision: SL pessimistic ──────────────────────────────────────


def test_walk_long_same_bar_sl_and_tp_returns_loss_pessimistic():
    """Wide bar that touches BOTH sl and tp must resolve LOSS (worst-case)."""
    klines = [
        _kline(1, low=98.0, high=102.0),  # touches sl=99 AND tp=101
    ]
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0,
        proposed_tp_price=101.0,
        klines=klines,
    )
    assert res.outcome == "LOSS"
    assert res.bars_to_sl == 0
    assert res.bars_to_tp is None


def test_walk_short_same_bar_sl_and_tp_returns_loss_pessimistic():
    klines = [
        _kline(1, low=98.0, high=102.0),
    ]
    res = walk_klines(
        direction="BEARISH",
        proposed_sl_price=101.0,
        proposed_tp_price=99.0,
        klines=klines,
    )
    assert res.outcome == "LOSS"
    assert res.bars_to_sl == 0
    assert res.bars_to_tp is None


# ── TIMEOUT + boundary ──────────────────────────────────────────────────────


def test_walk_returns_timeout_when_neither_target_hits():
    """Sideways within (sl, tp) for the entire window → TIMEOUT."""
    klines = [_kline(i, low=99.5, high=100.5) for i in range(1, 51)]
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0, proposed_tp_price=101.5,
        klines=klines,
    )
    assert res.outcome == "TIMEOUT"
    assert res.bars_to_sl is None
    assert res.bars_to_tp is None


def test_walk_max_bars_caps_lookforward():
    """Even if more klines provided, only first max_bars are walked."""
    klines = [_kline(i, low=99.5, high=100.5) for i in range(1, 200)]
    # Bar 50 (1-indexed) hits sl. With max_bars=10, walk should not see it.
    klines[49] = _kline(50, low=99.0, high=100.5)
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0, proposed_tp_price=101.5,
        klines=klines,
        max_bars=10,
    )
    assert res.outcome == "TIMEOUT"


def test_walk_empty_klines_returns_skip():
    res = walk_klines(
        direction="BULLISH",
        proposed_sl_price=99.0, proposed_tp_price=101.5,
        klines=[],
    )
    assert res.outcome == "SKIP"
    assert res.skip_reason == "no_klines"


# ── Bybit response normalization ────────────────────────────────────────────


def test_normalize_kline_response_flips_desc_to_asc():
    """Bybit V5 returns DESC (newest first); pegger needs ASC for walk."""
    raw = {"result": {"list": [
        ["3000", "100", "101", "99", "100.5", "1", "100"],
        ["2000", "100", "101", "99", "100.5", "1", "100"],
        ["1000", "100", "101", "99", "100.5", "1", "100"],
    ]}}
    klines = _normalize_kline_response(raw)
    assert [k.bar_start_ms for k in klines] == [1000, 2000, 3000]


def test_normalize_kline_response_skips_malformed_rows():
    raw = {"result": {"list": [
        ["1000", "100", "101", "99", "100.5", "1", "100"],
        ["bad-row"],  # malformed
        ["2000", "100", "101", "99", "100.5", "1", "100"],
    ]}}
    klines = _normalize_kline_response(raw)
    assert len(klines) == 2
    assert [k.bar_start_ms for k in klines] == [1000, 2000]


def test_normalize_kline_response_empty_payload():
    assert _normalize_kline_response({}) == []
    assert _normalize_kline_response({"result": {}}) == []
    assert _normalize_kline_response({"result": {"list": None}}) == []


# ── Bar-start floor + 1-bar offset ──────────────────────────────────────────


def test_signal_ts_floors_to_bar_open_then_adds_one_bar():
    """3m TF: a signal at 12:01:30 (mid-bar) → bar starts 12:00 → next bar 12:03."""
    sig = datetime(2026, 4, 28, 12, 1, 30, tzinfo=UTC)
    next_bar_ms = _signal_ts_to_bar_start_ms(sig, interval_minutes=3)
    expected = datetime(2026, 4, 28, 12, 3, tzinfo=UTC)
    assert next_bar_ms == int(expected.timestamp() * 1000)


def test_signal_ts_on_exact_bar_open_skips_to_next_bar():
    """Signal exactly at 12:00:00 → bar starts 12:00 → next bar 12:03 (NOT 12:00)."""
    sig = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    next_bar_ms = _signal_ts_to_bar_start_ms(sig, interval_minutes=3)
    expected = datetime(2026, 4, 28, 12, 3, tzinfo=UTC)
    assert next_bar_ms == int(expected.timestamp() * 1000)


def test_signal_ts_15m_interval():
    sig = datetime(2026, 4, 28, 12, 7, 0, tzinfo=UTC)  # mid 12:00-12:15 bar
    next_bar_ms = _signal_ts_to_bar_start_ms(sig, interval_minutes=15)
    expected = datetime(2026, 4, 28, 12, 15, tzinfo=UTC)
    assert next_bar_ms == int(expected.timestamp() * 1000)


# ── CLI symbol filter expansion ─────────────────────────────────────────────


def test_expand_symbol_filter_short_form():
    assert _expand_symbol_filter("BTC,ETH") == {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP",
    }


def test_expand_symbol_filter_full_form():
    assert _expand_symbol_filter("BTC-USDT-SWAP,SOL-USDT-SWAP") == {
        "BTC-USDT-SWAP", "SOL-USDT-SWAP",
    }


def test_expand_symbol_filter_mixed_case_and_whitespace():
    assert _expand_symbol_filter(" btc , Eth, doge ") == {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "DOGE-USDT-SWAP",
    }


def test_expand_symbol_filter_none_or_empty():
    assert _expand_symbol_filter(None) is None
    assert _expand_symbol_filter("") is None
    assert _expand_symbol_filter(", ,") is None


# ── PegInput dataclass sanity ───────────────────────────────────────────────


def test_peg_input_is_immutable():
    inp = PegInput(
        rejection_id="x", symbol="BTC-USDT-SWAP", direction="BULLISH",
        signal_timestamp=datetime(2026, 4, 28, 12, tzinfo=UTC),
        proposed_sl_price=99.0, proposed_tp_price=101.5,
    )
    with pytest.raises((AttributeError, TypeError)):
        inp.proposed_sl_price = 50.0  # type: ignore[misc]
