"""Unit tests for src/data/derivatives_api.py (Phase 1.5 Madde 2).

We never hit the real Coinalyze network — `httpx.AsyncClient` inside the
client is replaced with a fake whose `get()` is scripted per test.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from src.data.derivatives_api import CoinalyzeClient


# ── Helpers ───────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200,
                 headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient — queued responses per path."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, list[_FakeResponse]] = {}
        self.default: _FakeResponse | None = None

    def queue(self, path: str, resp: _FakeResponse) -> None:
        self.responses.setdefault(path, []).append(resp)

    async def get(self, path: str, params: dict | None = None) -> _FakeResponse:
        self.calls.append((path, params or {}))
        queue = self.responses.get(path)
        if queue:
            return queue.pop(0)
        if self.default is not None:
            return self.default
        return _FakeResponse([], status_code=200)

    async def aclose(self) -> None:
        pass


def _make_client(api_key: str = "test-key") -> CoinalyzeClient:
    c = CoinalyzeClient(api_key=api_key)
    c._client = _FakeClient()      # swap in the fake
    return c


# ── Construction / missing API key ────────────────────────────────────────


def test_missing_api_key_logs_warning_but_constructs():
    # Explicit None — do NOT inherit from env.
    client = CoinalyzeClient(api_key="")
    assert client.api_key is None or client.api_key == ""


@pytest.mark.asyncio
async def test_missing_api_key_requests_return_none():
    client = CoinalyzeClient(api_key="")
    client.api_key = None       # force the None branch
    assert await client.fetch_current_funding("BTCUSDT_PERP.A") is None
    assert await client.fetch_current_oi_usd("BTCUSDT_PERP.A") is None


# ── Token bucket ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_token_refills_over_time():
    client = _make_client()
    client._rate_tokens = 0.0
    client._rate_last_refill = time.monotonic() - 60.0  # fake elapsed
    # Should refill fully without sleeping because `elapsed * 40/60` > cost.
    slept = 0.0
    orig_sleep = asyncio.sleep

    async def record_sleep(sec: float):
        nonlocal slept
        slept += sec
        # don't actually sleep during the test

    import src.data.derivatives_api as mod
    mod.asyncio.sleep = record_sleep  # type: ignore
    try:
        await client._consume_token(cost=1)
    finally:
        mod.asyncio.sleep = orig_sleep  # type: ignore
    assert slept == 0.0


@pytest.mark.asyncio
async def test_consume_token_blocks_when_empty(monkeypatch):
    client = _make_client()
    client._rate_tokens = 0.0
    client._rate_last_refill = time.monotonic()   # no refill
    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    await client._consume_token(cost=1)
    assert slept and slept[0] == pytest.approx(1.5, abs=0.01)


@pytest.mark.asyncio
async def test_consume_token_multi_cost_deducts_N(monkeypatch):
    client = _make_client()
    client._rate_tokens = 10.0
    client._rate_last_refill = time.monotonic()

    async def no_sleep(_):
        pass

    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    await client._consume_token(cost=3)
    assert client._rate_tokens == pytest.approx(7.0, abs=0.1)


# ── _request — status code handling ───────────────────────────────────────


@pytest.mark.asyncio
async def test_request_429_short_circuits_without_blocking(monkeypatch):
    """2026-04-19: 429 no longer awaits Retry-After inline. It sets
    _rate_pause_until and returns None so callers fall back to stale
    snapshots instead of stalling the event loop for every coroutine."""
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/foo", _FakeResponse({}, status_code=429,
                                    headers={"Retry-After": "1"}))
    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    result = await client._request("/foo", {}, cost=1)
    assert result is None
    assert client._rate_pause_until > time.monotonic()
    # No blocking sleep on the Retry-After duration.
    assert 1.0 not in slept

    # Subsequent calls short-circuit while the pause is active.
    fc.queue("/foo", _FakeResponse([{"value": 1.0}], status_code=200))
    again = await client._request("/foo", {}, cost=1)
    assert again is None


@pytest.mark.asyncio
async def test_request_401_returns_none_without_retry(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/foo", _FakeResponse({}, status_code=401))

    async def no_sleep(_):
        pass

    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    result = await client._request("/foo", {}, cost=1)
    assert result is None
    # Exactly 1 call — no retries after 401.
    assert len(fc.calls) == 1


# ── Symbol mapping ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_symbol_map_prefers_binance_over_okx(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/future-markets", _FakeResponse([
        {"symbol": "BTCUSDT_PERP.3", "base_asset": "BTC", "quote_asset": "USDT",
         "is_perpetual": True, "margined": "STABLE", "exchange": "OKX"},
        {"symbol": "BTCUSDT_PERP.A", "base_asset": "BTC", "quote_asset": "USDT",
         "is_perpetual": True, "margined": "STABLE", "exchange": "Binance"},
    ]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    await client.ensure_symbol_map(["BTC-USDT-SWAP"])
    assert client._symbol_map["BTC-USDT-SWAP"] == "BTCUSDT_PERP.A"


@pytest.mark.asyncio
async def test_ensure_symbol_map_idempotent_on_empty(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/future-markets", _FakeResponse([]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    await client.ensure_symbol_map(["BTC-USDT-SWAP"])
    assert client._symbol_map_loaded is True
    await client.ensure_symbol_map(["BTC-USDT-SWAP"])   # no second call
    assert len([p for p, _ in fc.calls if p == "/future-markets"]) == 1


# ── Schema parsing ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_current_funding_parses_value(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/funding-rate", _FakeResponse(
        [{"symbol": "BTCUSDT_PERP.A", "value": 0.0123, "update": 0}]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    val = await client.fetch_current_funding("BTCUSDT_PERP.A")
    assert val == pytest.approx(0.0123)


@pytest.mark.asyncio
async def test_fetch_liquidation_history_sums_window(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/liquidation-history", _FakeResponse([{
        "symbol": "BTCUSDT_PERP.A",
        "history": [
            {"t": 0, "l": 100.0, "s": 50.0},
            {"t": 1, "l": 200.0, "s": 150.0},
        ],
    }]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    result = await client.fetch_liquidation_history("BTCUSDT_PERP.A")
    assert result == {"long_usd": 300.0, "short_usd": 200.0, "bucket_count": 2}


@pytest.mark.asyncio
async def test_fetch_long_short_ratio_picks_latest_bar(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/long-short-ratio-history", _FakeResponse([{
        "symbol": "BTCUSDT_PERP.A",
        "history": [
            {"t": 0, "r": 1.0, "l": 0.5, "s": 0.5},
            {"t": 1, "r": 1.5, "l": 0.6, "s": 0.4},
        ],
    }]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    r = await client.fetch_long_short_ratio("BTCUSDT_PERP.A")
    assert r == {"ratio": 1.5, "long_share": 0.6, "short_share": 0.4}


@pytest.mark.asyncio
async def test_fetch_history_empty_returns_none(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/liquidation-history", _FakeResponse([{
        "symbol": "BTCUSDT_PERP.A", "history": [],
    }]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    assert await client.fetch_liquidation_history("BTCUSDT_PERP.A") is None


@pytest.mark.asyncio
async def test_fetch_oi_change_pct_math(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/open-interest-history", _FakeResponse([{
        "symbol": "BTCUSDT_PERP.A",
        "history": [
            {"t": 0, "c": 1000.0},
            {"t": 1, "c": 1200.0},
        ],
    }]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    pct = await client.fetch_oi_change_pct("BTCUSDT_PERP.A", lookback_hours=24)
    assert pct == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_fetch_oi_change_pct_guards_zero_start(monkeypatch):
    client = _make_client()
    fc: _FakeClient = client._client            # type: ignore
    fc.queue("/open-interest-history", _FakeResponse([{
        "symbol": "BTCUSDT_PERP.A",
        "history": [
            {"t": 0, "c": 0.0},
            {"t": 1, "c": 500.0},
        ],
    }]))

    async def no_sleep(_): pass
    import src.data.derivatives_api as mod
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    assert await client.fetch_oi_change_pct("BTCUSDT_PERP.A") is None
