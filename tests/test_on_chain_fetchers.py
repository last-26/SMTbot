"""Unit tests for `src.data.on_chain.fetch_daily_snapshot` and
`fetch_hourly_stablecoin_pulse` — the Phase B snapshot-derivation layer."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.data import on_chain as on_chain_mod
from src.data.on_chain import (
    ArkhamClient,
    fetch_daily_snapshot,
    fetch_entity_netflow_24h,
    fetch_hourly_stablecoin_pulse,
    fetch_token_volume_last_hour,
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
async def test_daily_snapshot_uses_hourly_granularity():
    """2026-04-23 fix: daily snapshot's histogram calls must use
    granularity=1h (not 1d) so the rolling 24h window updates every
    hour instead of pinning to the last-closed UTC day.

    Call sequence: stable_in, stable_out, btc_in, btc_out, eth_in,
    eth_out (6 calls via 3 token sets × 2 legs).
    """
    client = ArkhamClient(api_key="test-key")
    # 6 responses — one per (token, flow) pair
    queue = [_FakeResponse(json_body=_hist(1.0)) for _ in range(6)]
    fake = _FakeClient(queued=queue)  # type: ignore
    client._client = fake  # type: ignore
    await fetch_daily_snapshot(
        client,
        stablecoin_threshold_usd=50_000_000.0,
        btc_netflow_threshold_usd=50_000_000.0,
        stale_threshold_s=7200,
    )
    assert len(fake.calls) == 6
    for path, params in fake.calls:
        assert path == "/transfers/histogram"
        assert params.get("granularity") == "1h", (
            f"regression: {path} using {params.get('granularity')!r} — "
            "must be 1h so the rolling 24h window doesn't freeze at UTC day close"
        )
        assert params.get("timeLast") == "24h"


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


# ── 2026-04-23: fetch_entity_netflow_24h (rolling 1h-granularity histogram) ─


def _hist_multi(buckets: list[tuple[str, float]]) -> list:
    """Multi-bucket histogram response shape: `[{time, count, usd}, ...]`."""
    return [{"time": ts, "count": 1, "usd": usd} for ts, usd in buckets]


@pytest.mark.asyncio
async def test_fetch_entity_netflow_sums_hourly_buckets():
    """Sum of in − sum of out across all 1h buckets in the 24h window."""
    inflow = _hist_multi([
        (f"2026-04-23T{h:02d}:00:00Z", 10_000_000.0) for h in range(24)
    ])  # 24 × 10M = 240M in
    outflow = _hist_multi([
        (f"2026-04-23T{h:02d}:00:00Z", 7_000_000.0) for h in range(24)
    ])  # 24 × 7M = 168M out
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=inflow),
        _FakeResponse(json_body=outflow),
    ])
    result = await fetch_entity_netflow_24h(client, "binance")
    assert result == 72_000_000.0  # 240M - 168M


@pytest.mark.asyncio
async def test_fetch_entity_netflow_negative_when_outflow_dominates():
    inflow = _hist_multi([("2026-04-23T00:00:00Z", 1_000_000.0)])
    outflow = _hist_multi([("2026-04-23T00:00:00Z", 5_000_000.0)])
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=inflow),
        _FakeResponse(json_body=outflow),
    ])
    result = await fetch_entity_netflow_24h(client, "bybit")
    assert result == -4_000_000.0


@pytest.mark.asyncio
async def test_fetch_entity_netflow_none_when_outflow_leg_fails():
    """One leg failing → None (caller treats as no-signal)."""
    client = ArkhamClient(api_key="test-key", max_retries=1)
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=_hist_multi([("2026-04-23T00:00:00Z", 1.0)])),
        _FakeResponse(status_code=500),
    ])
    result = await fetch_entity_netflow_24h(client, "coinbase")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_entity_netflow_zero_on_empty_buckets():
    """Both legs empty list → 0.0 (valid signal: no flow this window)."""
    client = ArkhamClient(api_key="test-key")
    client._client = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=[]),
        _FakeResponse(json_body=[]),
    ])
    result = await fetch_entity_netflow_24h(client, "binance")
    assert result == 0.0


@pytest.mark.asyncio
async def test_fetch_entity_netflow_hits_histogram_with_entity_base():
    """Verify the new endpoint + params (histogram, base=<entity>, 1h/24h)."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[  # type: ignore
        _FakeResponse(json_body=[]),
        _FakeResponse(json_body=[]),
    ])
    client._client = fake  # type: ignore
    await fetch_entity_netflow_24h(client, "bybit")
    assert len(fake.calls) == 2
    for path, params in fake.calls:
        assert path == "/transfers/histogram"
        assert params.get("base") == "bybit"
        assert params.get("granularity") == "1h"
        assert params.get("timeLast") == "24h"
    # first call is inflow, second is outflow
    assert fake.calls[0][1].get("flow") == "in"
    assert fake.calls[1][1].get("flow") == "out"


# ── 2026-04-22: fetch_token_volume_last_hour (FAZ 3) ─────────────────────


def _token_volume_body(buckets: list[tuple[str, float, float]]) -> list:
    """Mirror Arkham's `/token/volume/{id}` shape (verified via probe).
    `buckets`: list of (iso_ts, in_usd, out_usd)."""
    return [
        {
            "time": ts,
            "inUSD": in_usd,
            "outUSD": out_usd,
            "inValue": in_usd / 100_000.0,  # placeholder native amount
            "outValue": out_usd / 100_000.0,
        }
        for ts, in_usd, out_usd in buckets
    ]


@pytest.mark.asyncio
async def test_fetch_token_volume_last_hour_uses_last_bucket():
    body = _token_volume_body([
        ("2026-04-22T05:00:00Z", 5_000_000.0, 7_000_000.0),
        ("2026-04-22T06:00:00Z", 12_000_000.0, 4_000_000.0),
        ("2026-04-22T07:00:00Z", 20_800_110.91, 3_395_308.11),  # real probe sample
    ])
    client = _build_client_with_response(body)
    result = await fetch_token_volume_last_hour(client, "bitcoin")
    assert result is not None
    # 20.8M in - 3.4M out = ~17.4M net (positive = deposit pressure)
    assert abs(result - 17_404_802.80) < 0.01


@pytest.mark.asyncio
async def test_fetch_token_volume_returns_none_on_empty_list():
    client = _build_client_with_response([])
    result = await fetch_token_volume_last_hour(client, "ethereum")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_token_volume_negative_when_withdrawals_dominate():
    body = _token_volume_body([
        ("2026-04-22T07:00:00Z", 1_000_000.0, 8_500_000.0),
    ])
    client = _build_client_with_response(body)
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result == -7_500_000.0


@pytest.mark.asyncio
async def test_fetch_token_volume_hits_correct_endpoint_with_hourly_granularity():
    body = _token_volume_body([("2026-04-22T07:00:00Z", 1.0, 2.0)])
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[_FakeResponse(json_body=body)])  # type: ignore
    client._client = fake  # type: ignore
    await fetch_token_volume_last_hour(client, "dogecoin")
    assert len(fake.calls) == 1
    path, params = fake.calls[0]
    assert path == "/token/volume/dogecoin"
    # Probe confirmed sub-hourly returns 500; only 1h works.
    assert params.get("granularity") == "1h"
    assert params.get("timeLast") == "24h"


# ── 2026-04-23: histogram fallback for Arkham gap tokens (solana) ─────────


@pytest.mark.asyncio
async def test_fetch_token_volume_falls_back_on_null_primary():
    """Arkham returns JSON `null` (parsed as Python None) for tokens it
    recognises but hasn't aggregated into /token/volume. When this
    happens, the fallback hits /transfers/histogram and returns the
    last bucket's inflow − outflow."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        # Primary: /token/volume/solana → Arkham body is JSON null.
        _FakeResponse(json_body=None),
        # Fallback leg 1: /transfers/histogram flow=in → most-recent bucket $6.38M
        _FakeResponse(json_body=_hist(6_385_883.42)),
        # Fallback leg 2: /transfers/histogram flow=out → most-recent bucket $1.75M
        _FakeResponse(json_body=_hist(1_751_776.13)),
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result is not None
    # 6.385883M - 1.751776M ≈ 4.634107M (matches the live probe reading)
    assert abs(result - 4_634_107.29) < 0.01
    # Confirm both endpoints were hit in sequence.
    assert len(fake.calls) == 3
    assert fake.calls[0][0] == "/token/volume/solana"
    assert fake.calls[1][0] == "/transfers/histogram"
    assert fake.calls[2][0] == "/transfers/histogram"
    assert fake.calls[1][1].get("flow") == "in"
    assert fake.calls[2][1].get("flow") == "out"
    # Fallback legs must use 1h granularity to match primary semantic.
    assert fake.calls[1][1].get("granularity") == "1h"
    # Token passed comma-joined into histogram (Arkham shape).
    assert fake.calls[1][1].get("tokens") == "solana"


@pytest.mark.asyncio
async def test_fetch_token_volume_falls_back_on_empty_primary_list():
    """Primary returning `[]` (not null — but still no data) should also
    trigger the histogram fallback."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        _FakeResponse(json_body=[]),  # primary empty
        _FakeResponse(json_body=_hist(1_000_000.0)),
        _FakeResponse(json_body=_hist(250_000.0)),
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result == pytest.approx(750_000.0)


@pytest.mark.asyncio
async def test_fetch_token_volume_primary_non_dict_last_falls_back():
    """Primary list where the last bucket isn't a dict (defensive) should
    not crash — just fall back to histogram."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        _FakeResponse(json_body=["not-a-dict"]),  # malformed
        _FakeResponse(json_body=_hist(500_000.0)),
        _FakeResponse(json_body=_hist(100_000.0)),
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result == pytest.approx(400_000.0)


@pytest.mark.asyncio
async def test_fetch_token_volume_fallback_returns_none_when_in_leg_fails():
    """If the fallback's inflow leg returns None, give up — don't
    fabricate a zero-in value."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        _FakeResponse(json_body=None),          # primary null
        _FakeResponse(json_body=None),          # inflow leg fails
        _FakeResponse(json_body=_hist(100.0)),  # outflow (unreached ideally)
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_token_volume_fallback_handles_empty_histogram_buckets():
    """Histogram legs return empty lists → None (no bucket to take from)."""
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        _FakeResponse(json_body=None),
        _FakeResponse(json_body=[]),
        _FakeResponse(json_body=[]),
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "solana")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_token_volume_primary_success_skips_fallback():
    """Don't waste 2 extra calls when the primary path succeeds."""
    body = _token_volume_body([("2026-04-22T07:00:00Z", 20_800_110.91, 3_395_308.11)])
    client = ArkhamClient(api_key="test-key")
    fake = _FakeClient(queued=[
        _FakeResponse(json_body=body),
        # Queue extra responses that must NOT be consumed.
        _FakeResponse(json_body=_hist(999_999.0)),
        _FakeResponse(json_body=_hist(999_999.0)),
    ])
    client._client = fake  # type: ignore
    result = await fetch_token_volume_last_hour(client, "bitcoin")
    assert result == pytest.approx(17_404_802.80, abs=0.01)
    # Only one call — primary returned a usable bucket.
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "/token/volume/bitcoin"
