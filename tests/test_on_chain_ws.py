"""Unit tests for `src.data.on_chain_ws` — Arkham whale-transfer
WebSocket listener (Phase D).

Protocol references: Arkham Intel API v1.1, docs at
https://intel.arkm.com/api/docs.
"""

from __future__ import annotations

import json

import pytest

from src.data.on_chain_types import WhaleBlackoutState
from src.data.on_chain_ws import (
    ARKHAM_WS_BASE,
    ArkhamWebSocketListener,
    build_subscribe_message,
    build_ws_url,
    parse_transfer_message,
)


# ── build_ws_url ────────────────────────────────────────────────────────────


def test_build_ws_url_default_base():
    url = build_ws_url("abc-123")
    assert url == f"{ARKHAM_WS_BASE}?session_id=abc-123"


def test_build_ws_url_custom_base():
    url = build_ws_url("sid-42", base="wss://example.com/custom")
    assert url == "wss://example.com/custom?session_id=sid-42"


# ── build_subscribe_message ─────────────────────────────────────────────────


def test_build_subscribe_message_v1_shape():
    msg = build_subscribe_message(
        tokens=["BTC", "USDT"],
        usd_gte=100_000_000.0,
    )
    parsed = json.loads(msg)
    assert parsed["type"] == "subscribe"
    assert "id" in parsed
    assert parsed["payload"]["filters"]["tokens"] == ["BTC", "USDT"]
    assert parsed["payload"]["filters"]["usdGte"] == 100_000_000
    # `usdGte` must be an int per the docs example.
    assert isinstance(parsed["payload"]["filters"]["usdGte"], int)


def test_build_subscribe_message_empty_tokens():
    msg = build_subscribe_message(tokens=[], usd_gte=100_000_000.0)
    parsed = json.loads(msg)
    # Empty tokens list is valid — usdGte alone filters the stream.
    assert parsed["payload"]["filters"]["tokens"] == []


def test_build_subscribe_message_custom_id():
    msg = build_subscribe_message(
        tokens=["BTC"], usd_gte=10_000, message_id="custom-id-7",
    )
    parsed = json.loads(msg)
    assert parsed["id"] == "custom-id-7"


# ── parse_transfer_message ─────────────────────────────────────────────────


def _wrap_transfer(**fields) -> str:
    """Shortcut for the v1 `{"type":"transfer","payload":{"transfer":{...}}}`
    shape."""
    return json.dumps({
        "type": "transfer",
        "payload": {"transfer": fields},
    })


def test_parse_transfer_happy_path():
    raw = _wrap_transfer(
        tokenSymbol="BTC",
        historicalUSD=150_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    token, usd, ts = result
    assert token == "BTC"
    assert usd == 150_000_000.0
    # Timestamp within ±5s of the expected epoch (ISO 2026-04-21T11:01:35Z).
    # Computed once: 2026-04-21T11:01:35Z → epoch ms.
    from datetime import datetime, timezone
    expected_ms = int(
        datetime(2026, 4, 21, 11, 1, 35, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert ts == expected_ms


def test_parse_transfer_below_threshold():
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=50_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is None


def test_parse_transfer_at_exact_threshold():
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=100_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is not None


def test_parse_transfer_ack_returns_none():
    raw = json.dumps({"type": "ack", "id": "1"})
    assert parse_transfer_message(raw, threshold_usd=1.0) is None


def test_parse_transfer_error_returns_none():
    raw = json.dumps({"type": "error", "payload": {"code": "INVALID_FILTER"}})
    assert parse_transfer_message(raw, threshold_usd=1.0) is None


def test_parse_transfer_invalid_json():
    assert parse_transfer_message("not-json", threshold_usd=1.0) is None


def test_parse_transfer_non_dict_returns_none():
    assert parse_transfer_message(json.dumps([1, 2, 3]), threshold_usd=1.0) is None


def test_parse_transfer_missing_payload():
    raw = json.dumps({"type": "transfer"})
    assert parse_transfer_message(raw, threshold_usd=1.0) is None


def test_parse_transfer_missing_inner_transfer():
    raw = json.dumps({"type": "transfer", "payload": {"other": "field"}})
    assert parse_transfer_message(raw, threshold_usd=1.0) is None


def test_parse_transfer_missing_token_symbol():
    raw = _wrap_transfer(historicalUSD=200_000_000.0,
                         blockTimestamp="2026-04-21T11:01:35Z")
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is None


def test_parse_transfer_missing_usd():
    raw = _wrap_transfer(tokenSymbol="BTC",
                         blockTimestamp="2026-04-21T11:01:35Z")
    # historicalUSD absent → usd_value=0 → below any positive threshold.
    assert parse_transfer_message(raw, threshold_usd=100_000.0) is None


def test_parse_transfer_timestamp_fallback_to_now():
    import time as _t
    raw = _wrap_transfer(tokenSymbol="BTC", historicalUSD=200_000_000.0)
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    _, _, ts = result
    now_ms = int(_t.time() * 1000)
    assert abs(ts - now_ms) < 5_000


def test_parse_transfer_alternate_token_key():
    # Arkham might ship alternate keys (tokenId / token / asset).
    raw = json.dumps({
        "type": "transfer",
        "payload": {"transfer": {
            "tokenId": "ETH",
            "historicalUSD": 200_000_000.0,
            "blockTimestamp": "2026-04-21T11:01:35Z",
        }},
    })
    result = parse_transfer_message(raw, threshold_usd=100_000_000.0)
    assert result is not None
    assert result[0] == "ETH"


# ── ArkhamWebSocketListener._handle ────────────────────────────────────────


class _FakeArkham:
    api_key = "sid-test-key"

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
    raw = _wrap_transfer(
        tokenSymbol="USDT", historicalUSD=200_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    from datetime import datetime, timezone
    ts_ms = int(
        datetime(2026, 4, 21, 11, 1, 35, tzinfo=timezone.utc).timestamp() * 1000
    )
    expected_until = ts_ms + 600_000
    for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                "DOGE-USDT-SWAP", "BNB-USDT-SWAP"):
        assert state.blackouts.get(sym) == expected_until


def test_handle_btc_transfer_blacks_out_only_btc():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=200_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts.get("BTC-USDT-SWAP") is not None
    assert state.blackouts.get("ETH-USDT-SWAP") is None
    assert state.blackouts.get("SOL-USDT-SWAP") is None


def test_handle_eth_transfer_blacks_out_only_eth():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = _wrap_transfer(
        tokenSymbol="ETH", historicalUSD=150_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts.get("ETH-USDT-SWAP") is not None
    assert state.blackouts.get("BTC-USDT-SWAP") is None


def test_handle_below_threshold_does_not_blackout():
    state = WhaleBlackoutState()
    listener = _make_listener(state, threshold=200_000_000.0)
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=100_000_000.0,  # < 200M
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts == {}


def test_handle_unknown_token_returns_without_error():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    raw = _wrap_transfer(
        tokenSymbol="XRP",  # not in affected_symbols_for map
        historicalUSD=200_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
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
    raw1 = _wrap_transfer(tokenSymbol="BTC", historicalUSD=200_000_000.0,
                          blockTimestamp="2026-04-21T11:00:00Z")
    raw2 = _wrap_transfer(tokenSymbol="BTC", historicalUSD=200_000_000.0,
                          blockTimestamp="2026-04-21T11:15:00Z")
    listener._handle(raw1)
    first = state.blackouts["BTC-USDT-SWAP"]
    listener._handle(raw2)
    second = state.blackouts["BTC-USDT-SWAP"]
    assert second > first


def test_listener_disabled_flag_initially_false():
    state = WhaleBlackoutState()
    listener = _make_listener(state)
    assert listener.disabled is False
