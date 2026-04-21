"""Unit tests for `src.data.on_chain.fetch_daily_snapshot` and
`fetch_hourly_stablecoin_pulse` — the Phase B snapshot-derivation layer."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.data import on_chain as on_chain_mod
from src.data.on_chain import (
    ArkhamClient,
    fetch_daily_snapshot,
    fetch_hourly_stablecoin_pulse,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: Any = None,
                 headers: Optional[dict] = None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.content = b"{}" if json_body is not None else b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _FakeClient:
    def __init__(self, queued: Optional[list] = None):
        self.queued: list = queued or []
        self.calls: list[tuple[str, dict]] = []

    def _next(self) -> _FakeResponse:
        if not self.queued:
            return _FakeResponse(status_code=200, json_body={"empty": True})
        return self.queued.pop(0)

    async def get(self, path: str, params: Optional[dict] = None) -> _FakeResponse:
        self.calls.append((path, params or {}))
        return self._next()

    async def post(self, path: str, *, params: Optional[dict] = None,
                   json: Optional[dict] = None) -> _FakeResponse:
        self.calls.append((path, {"params": params or {}, "json": json or {}}))
        return self._next()

    async def delete(self, path: str,
                     params: Optional[dict] = None) -> _FakeResponse:
        return _FakeResponse(status_code=200)

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _silence_sleep(monkeypatch):
    async def _fake_sleep(seconds: float) -> None:
        return None
    monkeypatch.setattr(on_chain_mod.asyncio, "sleep", _fake_sleep)


def _build_client_with_response(body: dict) -> ArkhamClient:
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[_FakeResponse(json_body=body)])  # type: ignore
    return client


def _hist(usd: float) -> list:
    """Single-bucket histogram response shape: `[{time, count, usd}]`."""
    return [{"time": "2026-04-21T00:00:00Z", "count": 1, "usd": usd}]


def _queue_f3_responses(
    stable_net_usd: float,
    btc_net_usd: float,
) -> list:
    """Build the 4-response queue the F3 fetch_daily_snapshot needs:
    stable_in, stable_out, btc_in, btc_out (in that order).

    Net = in_sum - out_sum. Tests pass a single net number per token;
    we express it as in=net, out=0 for simplicity.
    """
    pairs = [
        (max(stable_net_usd, 0.0), max(-stable_net_usd, 0.0)),
        (max(btc_net_usd, 0.0), max(-btc_net_usd, 0.0)),
    ]
    queue = []
    for in_usd, out_usd in pairs:
        queue.append(_FakeResponse(json_body=_hist(in_usd)))
        queue.append(_FakeResponse(json_body=_hist(out_usd)))
    return queue


# ── fetch_daily_snapshot — classification rules ────────────────────────────


@pytest.mark.asyncio
async def test_daily_snapshot_bullish_when_stables_in_and_btc_out():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=_queue_f3_responses(  # type: ignore
        stable_net_usd=80_000_000.0, btc_net_usd=-150_000_000.0,
    ))
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is not None
    assert snap.daily_macro_bias == "bullish"
    assert snap.cex_btc_netflow_24h_usd == -150_000_000.0
    assert snap.snapshot_age_s == 0
    assert snap.fresh is True


@pytest.mark.asyncio
async def test_daily_snapshot_bearish_when_stables_out_and_btc_in():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=_queue_f3_responses(  # type: ignore
        stable_net_usd=-80_000_000.0, btc_net_usd=150_000_000.0,
    ))
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is not None
    assert snap.daily_macro_bias == "bearish"
    assert snap.cex_btc_netflow_24h_usd == 150_000_000.0


@pytest.mark.asyncio
async def test_daily_snapshot_neutral_when_stables_in_but_btc_also_in():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=_queue_f3_responses(  # type: ignore
        stable_net_usd=80_000_000.0, btc_net_usd=100_000_000.0,
    ))
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is not None
    assert snap.daily_macro_bias == "neutral"


@pytest.mark.asyncio
async def test_daily_snapshot_neutral_when_stable_delta_below_threshold():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=_queue_f3_responses(  # type: ignore
        stable_net_usd=20_000_000.0, btc_net_usd=-150_000_000.0,
    ))
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is not None
    assert snap.daily_macro_bias == "neutral"


@pytest.mark.asyncio
async def test_daily_snapshot_returns_none_when_both_legs_fail():
    """All 8 attempts (4 calls × max_retries=2 in test) return 500.
    Both stablecoin + BTC legs report None → whole snapshot is None."""
    client = ArkhamClient(api_key="test-key", max_retries=1)
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(status_code=500) for _ in range(8)
    ])
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is None


@pytest.mark.asyncio
async def test_daily_snapshot_stale_threshold_propagates():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=_queue_f3_responses(  # type: ignore
        stable_net_usd=0.0, btc_net_usd=0.0,
    ))
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=3600,
        snapshot_age_s=1800,
    )
    assert snap is not None
    assert snap.stale_threshold_s == 3600
    assert snap.snapshot_age_s == 1800
    assert snap.fresh is True  # 1800 < 3600


# ── fetch_hourly_stablecoin_pulse ──────────────────────────────────────────


def _histogram_body(buckets: list[tuple[str, float]]) -> list:
    """Build a histogram response matching Arkham v1.1 /transfers/histogram shape:
    `[{"time": ISO, "count": int, "usd": float}]`."""
    return [
        {"time": t, "count": 1, "usd": usd}
        for t, usd in buckets
    ]


@pytest.mark.asyncio
async def test_hourly_pulse_computes_net_inflow_minus_outflow():
    """Two /transfers/histogram calls (flow=in then flow=out) → net
    pulse = inflow total − outflow total."""
    inflow_body = _histogram_body([
        ("2026-04-21T20:00:00Z", 400_000_000.0),
        ("2026-04-21T21:00:00Z", 200_000_000.0),
    ])
    outflow_body = _histogram_body([
        ("2026-04-21T20:00:00Z", 150_000_000.0),
        ("2026-04-21T21:00:00Z", 100_000_000.0),
    ])
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=inflow_body),
        _FakeResponse(json_body=outflow_body),
    ])
    pulse = await fetch_hourly_stablecoin_pulse(client)
    # (400M + 200M) - (150M + 100M) = 350M
    assert pulse == 350_000_000.0


@pytest.mark.asyncio
async def test_hourly_pulse_returns_none_when_either_leg_fails():
    # First call 200, second call 500 → one retry per attempt, eventually None.
    client = ArkhamClient(api_key="test-key", max_retries=1)
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=_histogram_body([("2026-04-21T20:00:00Z", 10.0)])),
        _FakeResponse(status_code=500),
    ])
    pulse = await fetch_hourly_stablecoin_pulse(client)
    assert pulse is None


@pytest.mark.asyncio
async def test_hourly_pulse_empty_buckets_yields_zero():
    """Both legs return empty lists → net pulse is exactly 0.0 (not None).
    Distinguishes "no flow in the window" (0) from "API failed" (None)."""
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=[]),
        _FakeResponse(json_body=[]),
    ])
    pulse = await fetch_hourly_stablecoin_pulse(client)
    assert pulse == 0.0


@pytest.mark.asyncio
async def test_hourly_pulse_hits_histogram_endpoint_with_cex_filter():
    body = _histogram_body([("2026-04-21T20:00:00Z", 50_000_000.0)])
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=body),
        _FakeResponse(json_body=body),
    ])
    client._client = fake  # type: ignore
    await fetch_hourly_stablecoin_pulse(client)
    assert len(fake.calls) == 2
    # Both calls hit /transfers/histogram.
    for path, params in fake.calls:
        assert path == "/transfers/histogram"
        assert params["base"] == "type:cex"
        assert params["granularity"] == "1h"
        assert params["tokens"] == "tether,usd-coin"
    # First call is inflow, second is outflow.
    assert fake.calls[0][1]["flow"] == "in"
    assert fake.calls[1][1]["flow"] == "out"


# ── get_altcoin_index ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_altcoin_index_returns_int():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body={"altcoinIndex": 42}),
    ])
    aci = await client.get_altcoin_index()
    assert aci == 42


@pytest.mark.asyncio
async def test_get_altcoin_index_none_on_failure():
    client = ArkhamClient(api_key="test-key", max_retries=1)
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(status_code=500),
    ])
    aci = await client.get_altcoin_index()
    assert aci is None


@pytest.mark.asyncio
async def test_get_altcoin_index_none_on_unexpected_shape():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=[1, 2, 3]),  # wrong shape
    ])
    aci = await client.get_altcoin_index()
    assert aci is None
