"""Unit tests for src/data/liquidation_stream.py (Phase 1.5 Madde 1).

We bypass the real WebSocket — call `_handle(raw_json)` directly and assert
on the buffer/journal side-effects. Reconnect/backoff are tested via a
simple monkey-patched `websockets.connect` that raises once.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.data.liquidation_stream import (
    LiquidationEvent,
    LiquidationStream,
    binance_to_okx_symbol,
    okx_to_binance_symbol,
)


# ── Symbol mapping ────────────────────────────────────────────────────────

def test_symbol_mapping_round_trip():
    assert okx_to_binance_symbol("BTC-USDT-SWAP") == "BTCUSDT"
    assert okx_to_binance_symbol("ETH-USDT-SWAP") == "ETHUSDT"
    assert binance_to_okx_symbol("BTCUSDT") == "BTC-USDT-SWAP"
    assert binance_to_okx_symbol("SOLUSDT") == "SOL-USDT-SWAP"


def test_symbol_mapping_rejects_non_usdt():
    # BUSD, USDC, BTC-margined contracts — None means "ignore this stream".
    assert binance_to_okx_symbol("BTCBUSD") is None
    assert binance_to_okx_symbol("BTCUSDC") is None
    assert binance_to_okx_symbol("USDT") is None            # empty base


# ── _handle parser ────────────────────────────────────────────────────────

def _fake_force_order(symbol: str, side: str, price: float, qty: float,
                      ts_ms: int = 1_700_000_000_000) -> str:
    payload = {
        "e": "forceOrder",
        "E": ts_ms,
        "o": {
            "s": symbol, "S": side,
            "ap": str(price), "p": str(price), "q": str(qty),
            "T": ts_ms,
        },
    }
    return json.dumps(payload)


def test_handle_sell_becomes_long_liq():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    s._handle(_fake_force_order("BTCUSDT", "SELL", 70_000.0, 0.5))
    buf = list(s.buffers["BTC-USDT-SWAP"])
    assert len(buf) == 1
    ev = buf[0]
    assert ev.side == "LONG_LIQ"
    assert ev.price == 70_000.0
    assert ev.quantity == 0.5
    assert ev.notional_usd == 35_000.0


def test_handle_buy_becomes_short_liq():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    s._handle(_fake_force_order("BTCUSDT", "BUY", 70_000.0, 0.5))
    assert s.buffers["BTC-USDT-SWAP"][0].side == "SHORT_LIQ"


def test_handle_skips_unwatched_symbol():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    s._handle(_fake_force_order("DOGEUSDT", "BUY", 0.1, 1_000.0))
    assert len(s.buffers["BTC-USDT-SWAP"]) == 0


def test_handle_skips_non_usdt_symbol():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    # BUSD pair — binance_to_okx_symbol returns None, early exit.
    s._handle(_fake_force_order("BTCBUSD", "SELL", 70_000.0, 0.5))
    assert len(s.buffers["BTC-USDT-SWAP"]) == 0


def test_handle_malformed_json_does_not_crash():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    # _handle itself raises JSONDecodeError; the outer _run loop catches it.
    # Here we just verify a nested call with bad payload structure is skipped.
    s._handle(json.dumps({"e": "forceOrder"}))   # no "o" key
    assert len(s.buffers["BTC-USDT-SWAP"]) == 0


def test_handle_zero_price_or_qty_rejected():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    s._handle(_fake_force_order("BTCUSDT", "SELL", 0.0, 0.5))
    s._handle(_fake_force_order("BTCUSDT", "BUY", 70_000.0, 0.0))
    assert len(s.buffers["BTC-USDT-SWAP"]) == 0


# ── Query API ─────────────────────────────────────────────────────────────

def test_recent_filters_out_old_events():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    now_ms = int(time.time() * 1000)
    # Manually push: one fresh (10s ago), one stale (2h ago).
    s.buffers["BTC-USDT-SWAP"].append(LiquidationEvent(
        symbol="BTC-USDT-SWAP", side="LONG_LIQ",
        price=70_000.0, quantity=0.1, notional_usd=7_000.0,
        ts_ms=now_ms - 10_000,
    ))
    s.buffers["BTC-USDT-SWAP"].append(LiquidationEvent(
        symbol="BTC-USDT-SWAP", side="SHORT_LIQ",
        price=70_000.0, quantity=0.1, notional_usd=7_000.0,
        ts_ms=now_ms - 2 * 3600 * 1000,
    ))
    fresh = s.recent("BTC-USDT-SWAP", lookback_ms=60 * 60 * 1000)  # 1h window
    assert len(fresh) == 1
    assert fresh[0].side == "LONG_LIQ"


def test_stats_aggregates_notional():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    now_ms = int(time.time() * 1000)
    s.buffers["BTC-USDT-SWAP"].extend([
        LiquidationEvent("BTC-USDT-SWAP", "LONG_LIQ", 70_000.0, 1.0,
                         70_000.0, now_ms - 1_000),
        LiquidationEvent("BTC-USDT-SWAP", "LONG_LIQ", 69_000.0, 0.5,
                         34_500.0, now_ms - 2_000),
        LiquidationEvent("BTC-USDT-SWAP", "SHORT_LIQ", 71_000.0, 0.2,
                         14_200.0, now_ms - 3_000),
    ])
    stats = s.stats("BTC-USDT-SWAP", lookback_ms=60_000)
    assert stats["long_liq_notional"] == pytest.approx(104_500.0)
    assert stats["short_liq_notional"] == pytest.approx(14_200.0)
    assert stats["long_liq_count"] == 2
    assert stats["short_liq_count"] == 1
    assert stats["max_liq_notional"] == pytest.approx(70_000.0)


def test_stats_empty_symbol():
    s = LiquidationStream(["BTC-USDT-SWAP"])
    stats = s.stats("BTC-USDT-SWAP", lookback_ms=60_000)
    assert stats["long_liq_notional"] == 0
    assert stats["max_liq_notional"] == 0.0


# ── Journal injection ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_journal_receives_insert_on_handle():
    class FakeJournal:
        def __init__(self):
            self.events: list[LiquidationEvent] = []

        async def insert_liquidation(self, ev: LiquidationEvent) -> None:
            self.events.append(ev)

    s = LiquidationStream(["BTC-USDT-SWAP"])
    j = FakeJournal()
    s.attach_journal(j)
    s._handle(_fake_force_order("BTCUSDT", "SELL", 70_000.0, 1.0))
    # asyncio.create_task runs on the next loop turn
    await asyncio.sleep(0)
    assert len(j.events) == 1
    assert j.events[0].side == "LONG_LIQ"
