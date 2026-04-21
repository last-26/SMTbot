"""Unit tests for `src.data.on_chain_ws` — Arkham whale-transfer
WebSocket listener (Phase D)."""

from __future__ import annotations

import json
import time

import pytest

from src.data.on_chain_types import WhaleBlackoutState
from src.data.on_chain_ws import (
    ArkhamWebSocketListener,
    build_subscribe_message,
    parse_transfer_message,
)


# ── build_subscribe_message ─────────────────────────────────────────────────


def test_build_subscribe_message_shape():
    msg = build_subscribe_message(
        session_id="abc123",
        tokens=["bitcoin", "tether"],
        usd_gte=100_000_000.0,
    )
    parsed = json.loads(msg)
    assert parsed["op"] == "subscribe"
    assert parsed["sessionId"] == "abc123"
    assert parsed["filter"]["tokens"] == ["bitcoin", "tether"]
    assert parsed["filter"]["usdGte"] == 100_000_000.0


def test_build_subscribe_message_serialises_without_throwing():
    # Any list[str] + float combination should be serialisable.
    msg = build_subscribe_message(
        session_id="sid",
        tokens=["x", "y", "z"],
        usd_gte=1.5e8,
    )
    json.loads(msg)  # round-trip check


# ── parse_transfer_message ─────────────────────────────────────────────────


def test_parse_transfer_message_happy_path():
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "bitcoin",
            "usdValue": 150_000_000.0,
            "timestamp": 1_700_000_000_000,
        },
    })
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    token, usd, ts = result
    assert token == "bitcoin"
    assert usd == 150_000_000.0
    assert ts == 1_700_000_000_000


def test_parse_transfer_message_below_threshold_returns_none():
    raw = json.dumps({
        "type": "transfer",
        "data": {"token": "bitcoin", "usdValue": 50_000_000.0},
    })
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is None


def test_parse_transfer_message_at_threshold_accepted():
    raw = json.dumps({
        "type": "transfer",
        "data": {"token": "bitcoin", "usdValue": 100_000_000.0},
    })
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None


def test_parse_transfer_message_invalid_json_returns_none():
    assert parse_transfer_message("not json", threshold_usd=1.0) is None


def test_parse_transfer_message_non_dict_returns_none():
    assert parse_transfer_message(json.dumps([1, 2, 3]), threshold_usd=1.0) is None


def test_parse_transfer_message_heartbeat_returns_none():
    raw = json.dumps({"type": "heartbeat"})
    assert parse_transfer_message(raw, threshold_usd=1.0) is None


def test_parse_transfer_message_missing_token_returns_none():
    raw = json.dumps({
        "type": "transfer",
        "data": {"usdValue": 200_000_000.0},
    })
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is None


def test_parse_transfer_message_alternate_key_names():
    # Arkham's field naming may drift — accept both camel and snake.
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "tokenId": "ethereum",
            "usd_value": 200_000_000.0,
            "ts_ms": 1_700_000_000_000,
        },
    })
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    token, _, _ = result
    assert token == "ethereum"


def test_parse_transfer_message_fills_timestamp_when_absent():
    raw = json.dumps({
        "type": "transfer",
        "data": {"token": "bitcoin", "usdValue": 200_000_000.0},
    })
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    _, _, ts = result
    now_ms = int(time.time() * 1000)
    # Timestamp should be populated with a close-to-now fallback.
    assert abs(ts - now_ms) < 5_000


# ── ArkhamWebSocketListener._handle ────────────────────────────────────────


class _FakeArkham:
    async def create_ws_session(self) -> str:
        return "sid-1"

    async def delete_ws_session(self, sid: str) -> bool:
        return True


def _make_listener(state: WhaleBlackoutState,
                   threshold: float = 100_000_000.0) -> ArkhamWebSocketListener:
    return ArkhamWebSocketListener(
        _FakeArkham(), state,
        usd_gte=threshold,
        blackout_duration_s=600,
    )


def test_handle_stablecoin_transfer_blacks_out_all_symbols():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "tether",
            "usdValue": 200_000_000.0,
            "timestamp": 1_700_000_000_000,
        },
    })
    listener._handle(raw)
    for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                "DOGE-USDT-SWAP", "BNB-USDT-SWAP"):
        assert state.blackouts.get(sym) == 1_700_000_000_000 + 600_000


def test_handle_bitcoin_transfer_blacks_out_only_btc():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "bitcoin",
            "usdValue": 200_000_000.0,
            "timestamp": 1_700_000_000_000,
        },
    })
    listener._handle(raw)
    assert state.blackouts.get("BTC-USDT-SWAP") == 1_700_000_000_000 + 600_000
    assert state.blackouts.get("ETH-USDT-SWAP") is None
    assert state.blackouts.get("SOL-USDT-SWAP") is None


def test_handle_ethereum_transfer_blacks_out_only_eth():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "ethereum",
            "usdValue": 150_000_000.0,
            "timestamp": 1_700_000_000_000,
        },
    })
    listener._handle(raw)
    assert state.blackouts.get("ETH-USDT-SWAP") == 1_700_000_000_000 + 600_000
    assert state.blackouts.get("BTC-USDT-SWAP") is None


def test_handle_below_threshold_does_not_blackout():
    state = WhaleBlackoutState()
    listener = _make_listener(state, threshold=200_000_000.0)
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "bitcoin",
            "usdValue": 100_000_000.0,  # below 200M
            "timestamp": 1_700_000_000_000,
        },
    })
    listener._handle(raw)
    assert state.blackouts == {}


def test_handle_unknown_token_returns_without_error():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = json.dumps({
        "type": "transfer",
        "data": {
            "token": "ripple",  # not in affected_symbols_for map
            "usdValue": 200_000_000.0,
            "timestamp": 1_700_000_000_000,
        },
    })
    listener._handle(raw)
    assert state.blackouts == {}


def test_handle_malformed_payload_does_not_raise():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    listener._handle("garbage-not-json")
    assert state.blackouts == {}


def test_handle_repeated_transfers_extend_blackout():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw1 = json.dumps({
        "type": "transfer",
        "data": {"token": "bitcoin", "usdValue": 200_000_000.0,
                 "timestamp": 1_700_000_000_000},
    })
    raw2 = json.dumps({
        "type": "transfer",
        "data": {"token": "bitcoin", "usdValue": 200_000_000.0,
                 "timestamp": 1_700_000_900_000},
    })
    listener._handle(raw1)
    first = state.blackouts["BTC-USDT-SWAP"]
    listener._handle(raw2)
    second = state.blackouts["BTC-USDT-SWAP"]
    # Second event has later timestamp → new until_ms is later → extends.
    assert second > first
    assert second == 1_700_000_900_000 + 600_000


def test_listener_disabled_flag_initially_false():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    assert listener.disabled is False
