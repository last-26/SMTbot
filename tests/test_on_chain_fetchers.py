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


def _balance_body(stable_total_usd: float, btc_netflow_usd: float,
                  eth_netflow_usd: float = 0.0) -> list:
    """Build a response mirroring Arkham's real v1.1 shape (verified
    via live probe 2026-04-21):
      [{entityId, tokenBalances: [{tokenId, balanceUsd, prevBalanceUsd}]}]
    Balance change = balanceUsd − prevBalanceUsd.
    """
    half_stable = stable_total_usd / 2.0
    # Convert deltas to a (now, prev) pair. Using (delta, 0) keeps
    # tests simple; fetcher subtracts prev from now.
    return [
        {
            "entityId": "binance",
            "entityName": "Binance",
            "entityType": "cex",
            "balanceUsd": 1.0e11,
            "prevBalanceUsd": 0.99e11,
            "tokenBalances": [
                {"tokenId": "tether", "tokenSymbol": "usdt",
                 "balanceUsd": half_stable, "prevBalanceUsd": 0.0},
                {"tokenId": "usd-coin", "tokenSymbol": "usdc",
                 "balanceUsd": half_stable, "prevBalanceUsd": 0.0},
                {"tokenId": "bitcoin", "tokenSymbol": "btc",
                 "balanceUsd": btc_netflow_usd, "prevBalanceUsd": 0.0},
                {"tokenId": "ethereum", "tokenSymbol": "eth",
                 "balanceUsd": eth_netflow_usd, "prevBalanceUsd": 0.0},
            ],
        },
    ]


# ── fetch_daily_snapshot — classification rules ────────────────────────────


@pytest.mark.asyncio
async def test_daily_snapshot_bullish_when_stables_in_and_btc_out():
    body = _balance_body(stable_total_usd=80_000_000.0,
                         btc_netflow_usd=-150_000_000.0)
    client = _build_client_with_response(body)
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
    body = _balance_body(stable_total_usd=-80_000_000.0,
                         btc_netflow_usd=150_000_000.0)
    client = _build_client_with_response(body)
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
    # Mixed signal — stablecoins arriving but BTC also arriving. Not a
    # clean bullish (BTC leaving) nor clean bearish. Neutral.
    body = _balance_body(stable_total_usd=80_000_000.0,
                         btc_netflow_usd=100_000_000.0)
    client = _build_client_with_response(body)
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
    body = _balance_body(stable_total_usd=20_000_000.0,  # below 50M
                         btc_netflow_usd=-150_000_000.0)
    client = _build_client_with_response(body)
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is not None
    assert snap.daily_macro_bias == "neutral"


@pytest.mark.asyncio
async def test_daily_snapshot_returns_none_on_http_failure():
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(status_code=500),
        _FakeResponse(status_code=500),
        _FakeResponse(status_code=500),
    ])
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is None


@pytest.mark.asyncio
async def test_daily_snapshot_returns_none_when_entities_missing():
    # A 200 response but without an `entities` key — defensive against
    # Arkham API shape changes. Degrade rather than crash.
    client = _build_client_with_response({"unexpected": "shape"})
    snap = await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert snap is None


@pytest.mark.asyncio
async def test_daily_snapshot_stale_threshold_propagates():
    body = _balance_body(stable_total_usd=0.0, btc_netflow_usd=0.0)
    client = _build_client_with_response(body)
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


@pytest.mark.asyncio
async def test_hourly_pulse_returns_none_stub():
    """fetch_hourly_stablecoin_pulse is a deliberate stub since the
    real Arkham endpoint only supports 7d+ windows. Returns None so
    Phase E penalty stays inert until a real hourly source lands
    (likely /transfers/histogram or WS aggregation)."""
    body = [
        {"entityId": "binance", "tokenBalances": [
            {"tokenId": "tether",
             "balanceUsd": 40_000_000.0, "prevBalanceUsd": 0.0},
        ]},
    ]
    client = _build_client_with_response(body)
    pulse = await fetch_hourly_stablecoin_pulse(client)
    assert pulse is None


@pytest.mark.asyncio
async def test_hourly_pulse_stub_does_not_hit_api():
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[])
    client._client = fake  # type: ignore
    pulse = await fetch_hourly_stablecoin_pulse(client)
    assert pulse is None
    # Stub must not make any HTTP calls.
    assert fake.calls == []
