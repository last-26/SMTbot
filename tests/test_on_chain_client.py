"""Unit tests for `src.data.on_chain.ArkhamClient`.

Mirrors the `tests/test_derivatives_api.py` style — custom `_FakeResponse`
+ queued `_FakeClient` replacing the internal httpx AsyncClient. No
external mock library; keeps Phase A zero-new-dep.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.data import on_chain as on_chain_mod
from src.data.on_chain import ArkhamClient


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_body: Any = None,
        headers: Optional[dict] = None,
    ):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        # httpx.Response-like: `content` is the raw body bytes. The
        # client uses `bool(resp.content)` to distinguish 204 / empty
        # payloads from real bodies.
        self.content = b"{}" if json_body is not None else b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeClient:
    """Queue-backed fake for httpx.AsyncClient.

    `get` / `post` / `delete` each pop the next response from `queued`.
    Raise by enqueueing an Exception instance instead of a response.
    """

    def __init__(self, queued: Optional[list] = None):
        self.queued: list = queued or []
        self.calls: list[tuple[str, str, dict]] = []  # (method, path, params)

    def _next(self) -> _FakeResponse:
        if not self.queued:
            return _FakeResponse(status_code=200, json_body={"empty": True})
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, path: str, params: Optional[dict] = None) -> _FakeResponse:
        self.calls.append(("GET", path, params or {}))
        return self._next()

    async def post(self, path: str, *, params: Optional[dict] = None,
                   json: Optional[dict] = None) -> _FakeResponse:
        self.calls.append(("POST", path, {"params": params or {},
                                           "json": json or {}}))
        return self._next()

    async def delete(self, path: str,
                     params: Optional[dict] = None) -> _FakeResponse:
        self.calls.append(("DELETE", path, params or {}))
        return self._next()

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _silence_sleep(monkeypatch):
    """Make exponential-retry sleeps no-ops so tests stay fast."""
    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(on_chain_mod.asyncio, "sleep", _fake_sleep)


def _make_client(queued: list, *, api_key: str = "test-key") -> ArkhamClient:
    client = ArkhamClient(api_key=api_key, max_retries=2)
    client._client = _FakeClient(queued=queued)  # type: ignore[assignment]
    return client


# ── Core request path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_api_key_returns_none_without_http_call():
    # Explicit empty string beats env var fallback inside __init__.
    client = ArkhamClient(api_key="")
    client.api_key = None
    fake = _FakeClient(queued=[_FakeResponse(json_body={"ok": True})])
    client._client = fake  # type: ignore[assignment]
    result = await client.get_entity_balance_changes(["a"], ["tether"])
    assert result is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_happy_path_returns_parsed_json_and_absorbs_headers():
    body = {"entities": {"binance": {"balance_change_usd": 42_000_000}}}
    resp = _FakeResponse(
        status_code=200, json_body=body,
        headers={
            "X-Intel-Datapoints-Usage": "500",
            "X-Intel-Datapoints-Limit": "10000",
            "X-Intel-Datapoints-Remaining": "9500",
        },
    )
    client = _make_client([resp])
    result = await client.get_entity_balance_changes(["binance"], ["tether"], "24h")
    assert result == body
    snap = client.last_usage_snapshot
    assert snap["usage"] == 500.0
    assert snap["limit"] == 10_000.0
    assert snap["remaining"] == 9_500.0


@pytest.mark.asyncio
async def test_429_populates_rate_pause_until_and_short_circuits_next_call():
    r1 = _FakeResponse(status_code=429, headers={"Retry-After": "30"})
    r2 = _FakeResponse(status_code=200, json_body={"should_not_be_seen": True})
    client = _make_client([r1, r2])
    first = await client.get_entity_balance_changes(["x"], ["tether"])
    assert first is None
    # `_rate_pause_until` was set; the next call must return None without
    # consuming the queued r2 response.
    assert client._rate_pause_until > 0
    before_calls = len(client._client.calls)  # type: ignore[attr-defined]
    second = await client.get_entity_balance_changes(["x"], ["tether"])
    assert second is None
    after_calls = len(client._client.calls)  # type: ignore[attr-defined]
    assert after_calls == before_calls


@pytest.mark.asyncio
async def test_401_and_403_return_none_without_retry():
    for code in (401, 403):
        resp = _FakeResponse(status_code=code)
        client = _make_client([resp])
        result = await client.get_entity_balance_changes(["x"], ["tether"])
        assert result is None
        # Exactly one call — the auth error short-circuits before the retry loop.
        assert len(client._client.calls) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_5xx_retries_then_returns_none():
    # max_retries=2 → 2 attempts, both fail, final return None.
    r1 = _FakeResponse(status_code=503)
    r2 = _FakeResponse(status_code=503)
    client = _make_client([r1, r2])
    result = await client.get_entity_balance_changes(["x"], ["tether"])
    assert result is None
    assert len(client._client.calls) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_generic_exception_is_swallowed_and_retries():
    client = _make_client([
        RuntimeError("boom"),
        _FakeResponse(status_code=200, json_body={"ok": True}),
    ])
    result = await client.get_entity_balance_changes(["x"], ["tether"])
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_auto_disable_fires_at_threshold_and_blocks_future_calls():
    resp = _FakeResponse(
        status_code=200, json_body={"ok": True},
        headers={
            "X-Intel-Datapoints-Usage": "9600",
            "X-Intel-Datapoints-Limit": "10000",
            "X-Intel-Datapoints-Remaining": "400",
        },
    )
    client = _make_client([resp], api_key="k")
    # Auto-disable threshold defaults to 95%. 96% > 95% → fires.
    result = await client.get_entity_balance_changes(["x"], ["tether"])
    assert result == {"ok": True}
    assert client.hard_disabled is True
    # All subsequent calls short-circuit, no matter the queued response.
    client._client.queued.append(  # type: ignore[attr-defined]
        _FakeResponse(status_code=200, json_body={"never": "seen"})
    )
    before = len(client._client.calls)  # type: ignore[attr-defined]
    out = await client.get_entity_balance_changes(["x"], ["tether"])
    assert out is None
    after = len(client._client.calls)  # type: ignore[attr-defined]
    assert after == before


@pytest.mark.asyncio
async def test_auto_disable_does_not_fire_below_threshold():
    resp = _FakeResponse(
        status_code=200, json_body={"ok": True},
        headers={
            "X-Intel-Datapoints-Usage": "9000",
            "X-Intel-Datapoints-Limit": "10000",
            "X-Intel-Datapoints-Remaining": "1000",
        },
    )
    client = _make_client([resp])
    await client.get_entity_balance_changes(["x"], ["tether"])
    assert client.hard_disabled is False


@pytest.mark.asyncio
async def test_header_parse_failure_does_not_break_response():
    # Malformed numbers — we swallow and keep returning the JSON body.
    resp = _FakeResponse(
        status_code=200, json_body={"ok": True},
        headers={"X-Intel-Datapoints-Usage": "not-a-number"},
    )
    client = _make_client([resp])
    result = await client.get_entity_balance_changes(["x"], ["tether"])
    assert result == {"ok": True}
    assert client.hard_disabled is False


# ── create_ws_session / delete_ws_session / usage ──────────────────────────


@pytest.mark.asyncio
async def test_create_ws_session_returns_id_on_success():
    resp = _FakeResponse(status_code=200, json_body={"sessionId": "abc-123"})
    client = _make_client([resp])
    sid = await client.create_ws_session()
    assert sid == "abc-123"


@pytest.mark.asyncio
async def test_create_ws_session_returns_none_on_missing_key_in_body():
    resp = _FakeResponse(status_code=200, json_body={"other": "field"})
    client = _make_client([resp])
    sid = await client.create_ws_session()
    assert sid is None


@pytest.mark.asyncio
async def test_create_ws_session_returns_none_on_http_error():
    resp = _FakeResponse(status_code=500)
    client = _make_client([resp, _FakeResponse(status_code=500)])
    sid = await client.create_ws_session()
    assert sid is None


@pytest.mark.asyncio
async def test_delete_ws_session_reports_success_on_2xx():
    resp = _FakeResponse(status_code=204)
    client = _make_client([resp])
    ok = await client.delete_ws_session("abc-123")
    assert ok is True


@pytest.mark.asyncio
async def test_delete_ws_session_failure_returns_false_not_raise():
    client = _make_client([RuntimeError("net down")])
    ok = await client.delete_ws_session("abc-123")
    assert ok is False


@pytest.mark.asyncio
async def test_delete_ws_session_no_key_short_circuits():
    client = ArkhamClient(api_key="")
    client.api_key = None
    fake = _FakeClient()
    client._client = fake  # type: ignore[assignment]
    ok = await client.delete_ws_session("abc")
    assert ok is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_close_is_idempotent_and_noraise():
    client = _make_client([])
    await client.close()
    await client.close()
