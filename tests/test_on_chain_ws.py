"""Unit tests for `src.data.on_chain_ws` — Arkham whale-transfer
WebSocket listener (Phase D, v2 streams rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.data.on_chain_types import WhaleBlackoutState
from src.data.on_chain_ws import (
    ARKHAM_WS_BASE,
    ArkhamWebSocketListener,
    _clear_cached_stream_id,
    _read_cached_filter_fingerprint,
    _read_cached_stream_id,
    _write_cached_filter_fingerprint,
    _write_cached_stream_id,
    build_stream_filters,
    build_ws_url,
    compute_filter_fingerprint,
    parse_transfer_message,
)


def _seed_cache(path: Path, stream_id: str, *, tokens=None, usd_gte=100_000_000.0):
    """Helper: write both the stream_id cache AND a matching filter
    fingerprint sidecar so `_obtain_stream_id` treats the cache as
    fingerprint-match (the new 2026-04-22 behavior)."""
    _write_cached_stream_id(path, stream_id)
    fp = compute_filter_fingerprint(
        build_stream_filters(tokens or [], usd_gte)
    )
    _write_cached_filter_fingerprint(path, fp)


# ── build_ws_url ────────────────────────────────────────────────────────────


def test_build_ws_url_uses_stream_id_query_param():
    url = build_ws_url("abc-123")
    assert url == f"{ARKHAM_WS_BASE}?stream_id=abc-123"
    assert "/ws/v2/transfers" in url


def test_build_ws_url_custom_base():
    url = build_ws_url("sid-42", base="wss://example.com/custom")
    assert url == "wss://example.com/custom?stream_id=sid-42"


# ── build_stream_filters ────────────────────────────────────────────────────


def test_build_stream_filters_shape():
    filters = build_stream_filters(tokens=["BTC", "USDT"], usd_gte=100_000_000.0)
    # `usdGte` is a STRING per Arkham's v2 spec (verified via probe).
    assert filters["usdGte"] == "100000000"
    assert isinstance(filters["usdGte"], str)
    assert filters["tokens"] == ["BTC", "USDT"]
    assert filters["from"] == ["type:cex"]
    assert filters["to"] == ["type:cex"]


def test_build_stream_filters_omits_empty_tokens():
    filters = build_stream_filters(tokens=[], usd_gte=100_000_000.0)
    assert "tokens" not in filters
    # Core filter still present.
    assert filters["usdGte"] == "100000000"


# ── parse_transfer_message ─────────────────────────────────────────────────


def _wrap_transfer(**fields) -> str:
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


def test_parse_transfer_missing_token_symbol():
    raw = _wrap_transfer(historicalUSD=200_000_000.0,
                         blockTimestamp="2026-04-21T11:01:35Z")
    assert parse_transfer_message(raw, threshold_usd=100_000_000.0) is None


def test_parse_transfer_alternate_token_key():
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


# ── Stream-id disk persistence helpers ─────────────────────────────────────


def test_read_cached_stream_id_returns_none_for_missing_file(tmp_path):
    path = tmp_path / "nonexistent.txt"
    assert _read_cached_stream_id(path) is None


def test_read_cached_stream_id_returns_none_for_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert _read_cached_stream_id(path) is None


def test_write_then_read_roundtrips(tmp_path):
    path = tmp_path / "sub" / "stream.txt"
    _write_cached_stream_id(path, "abc-123")
    assert _read_cached_stream_id(path) == "abc-123"


def test_clear_cached_stream_id_removes_file(tmp_path):
    path = tmp_path / "stream.txt"
    _write_cached_stream_id(path, "abc-123")
    assert path.exists()
    _clear_cached_stream_id(path)
    assert not path.exists()


def test_clear_cached_stream_id_noop_for_missing_file(tmp_path):
    path = tmp_path / "nonexistent.txt"
    # Must not raise.
    _clear_cached_stream_id(path)


# ── ArkhamWebSocketListener._obtain_stream_id ──────────────────────────────


class _FakeArkham:
    api_key = "test-key"

    def __init__(self, *, list_result=None, create_result=None,
                 create_raises=None):
        self._list_result = list_result
        self._create_result = create_result
        self._create_raises = create_raises
        self.list_calls = 0
        self.create_calls = 0
        self.delete_calls = 0

    async def list_ws_streams(self):
        self.list_calls += 1
        return self._list_result

    async def create_ws_stream(self, filters: dict):
        self.create_calls += 1
        if self._create_raises is not None:
            raise self._create_raises
        return self._create_result

    async def delete_ws_stream(self, sid: str) -> bool:
        self.delete_calls += 1
        return True


def _make_listener(arkham, state: WhaleBlackoutState, *,
                   stream_id_path: Path) -> ArkhamWebSocketListener:
    return ArkhamWebSocketListener(
        arkham, state,
        usd_gte=100_000_000.0,
        blackout_duration_s=600,
        stream_id_path=stream_id_path,
    )


@pytest.mark.asyncio
async def test_obtain_stream_id_reuses_cached_when_still_live(tmp_path):
    path = tmp_path / "stream.txt"
    _seed_cache(path, "cached-sid")  # sidecar fingerprint matches default filter
    # list_ws_streams returns a list containing the cached id.
    arkham = _FakeArkham(list_result=[
        {"streamId": "cached-sid", "isConnected": False},
        {"streamId": "other-sid", "isConnected": False},
    ])
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid == "cached-sid"
    # No creation call = no credit burn.
    assert arkham.create_calls == 0
    assert arkham.list_calls == 1
    assert arkham.delete_calls == 0


@pytest.mark.asyncio
async def test_obtain_stream_id_creates_new_when_cache_stale(tmp_path):
    path = tmp_path / "stream.txt"
    _seed_cache(path, "stale-sid")  # fingerprint matches but stream gone server-side
    # list returns DIFFERENT ids than cache → server-side stale.
    arkham = _FakeArkham(
        list_result=[{"streamId": "other-sid", "isConnected": False}],
        create_result={"streamId": "fresh-sid", "id": 99,
                       "createdAt": "2026-04-21T00:00:00Z"},
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid == "fresh-sid"
    assert arkham.create_calls == 1
    # Cache rewritten with fresh id + fingerprint.
    assert _read_cached_stream_id(path) == "fresh-sid"
    assert _read_cached_filter_fingerprint(path) == compute_filter_fingerprint(
        build_stream_filters([], 100_000_000.0)
    )


@pytest.mark.asyncio
async def test_obtain_stream_id_creates_when_no_cache(tmp_path):
    path = tmp_path / "stream.txt"  # doesn't exist
    arkham = _FakeArkham(
        list_result=[],  # doesn't matter, cache is empty
        create_result={"streamId": "new-sid"},
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid == "new-sid"
    assert _read_cached_stream_id(path) == "new-sid"


@pytest.mark.asyncio
async def test_obtain_stream_id_none_when_creation_fails(tmp_path):
    path = tmp_path / "stream.txt"
    arkham = _FakeArkham(
        list_result=None,
        create_result=None,  # fetcher returns None
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid is None


@pytest.mark.asyncio
async def test_obtain_stream_id_none_on_create_exception(tmp_path):
    path = tmp_path / "stream.txt"
    arkham = _FakeArkham(
        list_result=None,
        create_raises=RuntimeError("network down"),
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid is None


# ── filter-fingerprint cache (2026-04-22 token-filter feature) ─────────────


@pytest.mark.asyncio
async def test_obtain_stream_id_recreates_on_filter_mismatch(tmp_path):
    """If operator changed `whale_tokens` (or threshold) since the cached
    stream was created, the old stream delivers wrong data + wastes label
    quota — must delete the old stream and create a fresh one."""
    path = tmp_path / "stream.txt"
    # Seed cache with a fingerprint computed from a DIFFERENT filter
    # (different tokens list) than what the listener will request.
    _write_cached_stream_id(path, "old-sid")
    _write_cached_filter_fingerprint(
        path,
        compute_filter_fingerprint(build_stream_filters(["xrp", "ada"], 100_000_000.0)),
    )
    arkham = _FakeArkham(
        list_result=[{"streamId": "old-sid", "isConnected": False}],
        create_result={"streamId": "new-sid", "id": 100,
                       "createdAt": "2026-04-22T00:00:00Z"},
    )
    listener = ArkhamWebSocketListener(
        arkham, WhaleBlackoutState(),
        usd_gte=100_000_000.0,
        blackout_duration_s=600,
        tokens=["bitcoin", "ethereum"],  # different from cached
        stream_id_path=path,
    )
    sid = await listener._obtain_stream_id()
    assert sid == "new-sid"
    # Old stream must be deleted to free Arkham server-side resource.
    assert arkham.delete_calls == 1
    assert arkham.create_calls == 1
    # Sidecar updated with NEW fingerprint matching new filter.
    assert _read_cached_filter_fingerprint(path) == compute_filter_fingerprint(
        build_stream_filters(["bitcoin", "ethereum"], 100_000_000.0)
    )


@pytest.mark.asyncio
async def test_obtain_stream_id_legacy_cache_without_fingerprint_recreates(tmp_path):
    """Legacy installs (pre 2026-04-22) have a stream_id but no
    fingerprint sidecar — treat as mismatch and recreate (one-time pay)
    so we don't keep silently reusing a stream with unknown filter."""
    path = tmp_path / "stream.txt"
    _write_cached_stream_id(path, "legacy-sid")  # NO sidecar written
    arkham = _FakeArkham(
        list_result=[{"streamId": "legacy-sid", "isConnected": False}],
        create_result={"streamId": "post-migration-sid"},
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid == "post-migration-sid"
    assert arkham.delete_calls == 1
    assert arkham.create_calls == 1


@pytest.mark.asyncio
async def test_obtain_stream_id_filter_mismatch_survives_delete_failure(tmp_path):
    """If `delete_ws_stream` raises (e.g. network blip), we still proceed
    with the create — Arkham's server-side timeout will reclaim the
    orphan. Recreate is more important than clean shutdown of the old."""
    path = tmp_path / "stream.txt"
    _write_cached_stream_id(path, "old-sid")
    _write_cached_filter_fingerprint(path, "deadbeef0000")  # garbage fp → mismatch

    class _DeleteFails(_FakeArkham):
        async def delete_ws_stream(self, sid):
            self.delete_calls += 1
            raise RuntimeError("transient network err")

    arkham = _DeleteFails(
        create_result={"streamId": "new-sid"},
    )
    listener = _make_listener(arkham, WhaleBlackoutState(),
                              stream_id_path=path)
    sid = await listener._obtain_stream_id()
    assert sid == "new-sid"
    assert arkham.delete_calls == 1
    assert arkham.create_calls == 1


def test_compute_filter_fingerprint_is_order_insensitive():
    a = build_stream_filters(["bitcoin", "ethereum"], 100_000_000.0)
    b = build_stream_filters(["ethereum", "bitcoin"], 100_000_000.0)
    # Same set of tokens, different order → fingerprint depends on the
    # serialized filter dict. Since `build_stream_filters` preserves
    # tokens order, fingerprints differ. Document the contract: any
    # change in token order COUNTS as a filter change. Operator-facing
    # configs should use a stable order (alphabetical or business-prio).
    if a == b:
        assert compute_filter_fingerprint(a) == compute_filter_fingerprint(b)
    # Hash must be deterministic across calls for the same input.
    assert compute_filter_fingerprint(a) == compute_filter_fingerprint(a)
    # Hash must differ when usd_gte changes.
    c = build_stream_filters(["bitcoin"], 200_000_000.0)
    d = build_stream_filters(["bitcoin"], 100_000_000.0)
    assert compute_filter_fingerprint(c) != compute_filter_fingerprint(d)


# ── ArkhamWebSocketListener._handle ────────────────────────────────────────


def test_handle_stablecoin_transfer_blacks_out_all_symbols(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
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
                "DOGE-USDT-SWAP", "XRP-USDT-SWAP"):
        assert state.blackouts.get(sym) == expected_until


def test_handle_btc_transfer_blacks_out_only_btc(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=200_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts.get("BTC-USDT-SWAP") is not None
    assert state.blackouts.get("ETH-USDT-SWAP") is None


def test_handle_eth_transfer_blacks_out_only_eth(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
    raw = _wrap_transfer(
        tokenSymbol="ETH", historicalUSD=150_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts.get("ETH-USDT-SWAP") is not None
    assert state.blackouts.get("BTC-USDT-SWAP") is None


def test_handle_below_threshold_does_not_blackout(tmp_path):
    state = WhaleBlackoutState()
    listener = ArkhamWebSocketListener(
        _FakeArkham(), state,
        usd_gte=200_000_000.0,
        blackout_duration_s=600,
        stream_id_path=tmp_path / "s.txt",
    )
    raw = _wrap_transfer(
        tokenSymbol="BTC", historicalUSD=100_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts == {}


def test_handle_unknown_token_returns_without_error(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
    raw = _wrap_transfer(
        tokenSymbol="XRP", historicalUSD=200_000_000.0,
        blockTimestamp="2026-04-21T11:01:35Z",
    )
    listener._handle(raw)
    assert state.blackouts == {}


def test_handle_malformed_payload_does_not_raise(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
    listener._handle("garbage-not-json")
    assert state.blackouts == {}


def test_listener_disabled_flag_initially_false(tmp_path):
    state = WhaleBlackoutState()
    listener = _make_listener(_FakeArkham(), state,
                              stream_id_path=tmp_path / "s.txt")
    assert listener.disabled is False
    assert listener.stream_id is None
