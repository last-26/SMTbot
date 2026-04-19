"""Tests for the Binance public futures cross-check layer (Katman 2).

Covers three slices:
  1. `okx_swap_to_binance_futures` symbol mapping.
  2. `price_inside_candle` band + tolerance math.
  3. `BinancePublicClient.get_kline_around` happy path + failure isolation
     (network error, non-200, malformed JSON, empty list) using a stubbed
     httpx client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.data.public_market_feed import (
    BINANCE_FUTURES_BASE,
    BinancePublicClient,
    RealCandle,
    okx_swap_to_binance_futures,
    price_inside_candle,
)


# ── Symbol mapping ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "okx_symbol,expected",
    [
        ("BTC-USDT-SWAP", "BTCUSDT"),
        ("ETH-USDT-SWAP", "ETHUSDT"),
        ("DOGE-USDT-SWAP", "DOGEUSDT"),
    ],
)
def test_symbol_mapping_happy(okx_symbol: str, expected: str) -> None:
    assert okx_swap_to_binance_futures(okx_symbol) == expected


@pytest.mark.parametrize(
    "okx_symbol",
    ["", "BTC-USDT", "BTC-USDT-PERP", "SPOT-ONLY", "-USDT-SWAP", "BTC--SWAP"],
)
def test_symbol_mapping_rejects_bad_shapes(okx_symbol: str) -> None:
    assert okx_swap_to_binance_futures(okx_symbol) is None


# ── price_inside_candle ─────────────────────────────────────────────────────


def _candle(low: float, high: float) -> RealCandle:
    return RealCandle(open_time_ms=0, open=low, high=high, low=low, close=high)


def test_price_inside_band_no_tolerance() -> None:
    c = _candle(100.0, 101.0)
    assert price_inside_candle(100.5, c) is True
    assert price_inside_candle(100.0, c) is True
    assert price_inside_candle(101.0, c) is True


def test_price_outside_band_no_tolerance() -> None:
    c = _candle(100.0, 101.0)
    assert price_inside_candle(99.99, c) is False
    assert price_inside_candle(101.01, c) is False


def test_price_tolerance_widens_both_sides() -> None:
    # 5bps tolerance on a $1 band → $0.0005 buffer on each side.
    c = _candle(100.0, 101.0)
    # 99.9996 is inside widened band (lo = 99.9995).
    assert price_inside_candle(99.9996, c, tolerance_pct=0.0005) is True
    # 101.0004 is inside widened band (hi = 101.0005).
    assert price_inside_candle(101.0004, c, tolerance_pct=0.0005) is True
    # Without tolerance, 99.9996 would be outside.
    assert price_inside_candle(99.9996, c, tolerance_pct=0.0) is False
    # Tolerance doesn't widen arbitrarily far.
    assert price_inside_candle(99.0, c, tolerance_pct=0.0005) is False


def test_price_lenient_on_degenerate_candle() -> None:
    # Missing data (low=high=0) → we can't tell, so lenient = True.
    c = _candle(0.0, 0.0)
    assert price_inside_candle(50.0, c) is True


# ── BinancePublicClient.get_kline_around ────────────────────────────────────


def _mock_resp(status_code: int, json_payload=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload)
    resp.text = text
    return resp


def _kline_row(open_ms: int, low: float, high: float) -> list:
    # Binance row: [openTime, open, high, low, close, volume, closeTime, ...].
    return [open_ms, str(low), str(high), str(low), str(high), "0", open_ms + 60_000]


def test_get_kline_around_picks_containing_minute() -> None:
    client = BinancePublicClient()
    ts_ms = 1_700_000_100_000  # 100s past the 1_700_000_040_000 minute boundary
    # Produce two candles: 1_700_000_040_000 and 1_700_000_100_000. The first
    # contains ts_ms (open ≤ ts_ms < open+60s), the second does not yet.
    rows = [
        _kline_row(1_700_000_040_000, 100.0, 101.0),
        _kline_row(1_700_000_100_000, 101.0, 102.0),
    ]
    client._client = MagicMock()
    client._client.get = MagicMock(return_value=_mock_resp(200, rows))

    candle = client.get_kline_around("BTCUSDT", ts_ms)

    assert candle is not None
    # The minute containing ts_ms is 1_700_000_040_000 (spans [040, 100)).
    # Since ts_ms = 100_000, it lands exactly on the next boundary. Assert
    # the one we actually receive contains ts_ms.
    assert candle.open_time_ms <= ts_ms < candle.open_time_ms + 60_000


def test_get_kline_around_returns_none_on_non_200() -> None:
    client = BinancePublicClient()
    client._client = MagicMock()
    client._client.get = MagicMock(return_value=_mock_resp(429, text="rate-limited"))
    assert client.get_kline_around("BTCUSDT", 1_700_000_000_000) is None


def test_get_kline_around_returns_none_on_network_error() -> None:
    client = BinancePublicClient()
    client._client = MagicMock()
    client._client.get = MagicMock(side_effect=httpx.ConnectError("DNS fail"))
    assert client.get_kline_around("BTCUSDT", 1_700_000_000_000) is None


def test_get_kline_around_returns_none_on_empty_list() -> None:
    client = BinancePublicClient()
    client._client = MagicMock()
    client._client.get = MagicMock(return_value=_mock_resp(200, []))
    assert client.get_kline_around("BTCUSDT", 1_700_000_000_000) is None


def test_get_kline_around_falls_back_to_last_row_when_no_exact_match() -> None:
    """If none of the rows strictly contain ts_ms, grab the latest one — we
    still want *something* to cross-check against rather than bailing."""
    client = BinancePublicClient()
    # All rows far before ts_ms.
    rows = [
        _kline_row(1_000_000, 10.0, 11.0),
        _kline_row(1_060_000, 11.0, 12.0),
    ]
    client._client = MagicMock()
    client._client.get = MagicMock(return_value=_mock_resp(200, rows))

    candle = client.get_kline_around("BTCUSDT", 9_999_999_999_999)

    assert candle is not None
    assert candle.open_time_ms == 1_060_000  # last row


def test_get_kline_around_rejects_empty_symbol() -> None:
    client = BinancePublicClient()
    client._client = MagicMock()
    client._client.get = MagicMock()
    assert client.get_kline_around("", 1_700_000_000_000) is None
    client._client.get.assert_not_called()


def test_get_kline_around_uses_futures_base_url() -> None:
    """Sanity: the URL must be the USD-M futures endpoint, not spot."""
    client = BinancePublicClient()
    client._client = MagicMock()
    client._client.get = MagicMock(return_value=_mock_resp(200, []))
    client.get_kline_around("BTCUSDT", 1_700_000_000_000)
    called_url = client._client.get.call_args.args[0]
    assert called_url.startswith(BINANCE_FUTURES_BASE)
    assert "/fapi/v1/klines" in called_url
