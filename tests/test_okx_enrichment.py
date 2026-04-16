"""Tests for OKXClient.enrich_close_fill — positions-history → CloseFill.

PositionMonitor emits a CloseFill with `exit_price=0, pnl_usdt=0` on close;
without enrichment the journal records every trade as break-even and the
risk manager's streaks / drawdown never trip. These tests pin that the
enrichment path parses OKX's positions-history envelope correctly and
degrades to the raw fill when no matching row is returned.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.execution.errors import OKXError
from src.execution.models import CloseFill
from src.execution.okx_client import OKXClient, OKXCredentials


UTC = timezone.utc


class FakeAccountWithHistory:
    def __init__(self, history_resp: dict):
        self.history_resp = history_resp
        self.calls: list[tuple[str, dict]] = []

    def get_positions_history(self, **kw):
        self.calls.append(("get_positions_history", kw))
        return self.history_resp


def _client(history_resp: dict) -> tuple[OKXClient, FakeAccountWithHistory]:
    acct = FakeAccountWithHistory(history_resp)
    sdk = SimpleNamespace(
        trade=SimpleNamespace(),
        account=acct,
        market=SimpleNamespace(),
    )
    creds = OKXCredentials(api_key="k", api_secret="s", passphrase="p", demo_flag="1")
    return OKXClient(creds, sdk=sdk), acct


def _raw_fill() -> CloseFill:
    """Shape of what PositionMonitor._close_fill_from emits today."""
    return CloseFill(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        entry_price=67_000.0, exit_price=0.0, size=5.0, pnl_usdt=0.0,
    )


def test_enrich_returns_populated_fill_when_history_matches():
    history = {"code": "0", "data": [{
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "avgPx": "67000",
        "closeAvgPx": "68500",
        "realizedPnl": "32.5",
        "pnl": "35.0",
        "uTime": "1713268800000",   # 2024-04-16 12:00:00 UTC
        "cTime": "1713265200000",
    }]}
    client, _ = _client(history)
    enriched = client.enrich_close_fill(_raw_fill())
    assert enriched.exit_price == pytest.approx(68_500.0)
    assert enriched.pnl_usdt == pytest.approx(32.5)
    assert enriched.closed_at == datetime(2024, 4, 16, 12, 0, tzinfo=UTC)
    # Immutable fields pass through
    assert enriched.inst_id == "BTC-USDT-SWAP"
    assert enriched.pos_side == "long"
    assert enriched.entry_price == 67_000.0
    assert enriched.size == 5.0


def test_enrich_returns_original_when_no_match():
    # API returns rows but none match pos_side=long for this instrument
    history = {"code": "0", "data": [
        {"instId": "ETH-USDT-SWAP", "posSide": "long", "closeAvgPx": "3000"},
        {"instId": "BTC-USDT-SWAP", "posSide": "short", "closeAvgPx": "67000"},
    ]}
    client, _ = _client(history)
    original = _raw_fill()
    result = client.enrich_close_fill(original)
    assert result is original  # no mutation, no substitution


def test_enrich_handles_envelope_error_gracefully():
    history = {"code": "50001", "msg": "service unavailable", "data": []}
    client, _ = _client(history)
    with pytest.raises(OKXError):
        client.enrich_close_fill(_raw_fill())


def test_enrich_picks_most_recent_when_multiple():
    history = {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "posSide": "long",
         "closeAvgPx": "68000", "realizedPnl": "10.0",
         "uTime": "1713182400000"},     # older
        {"instId": "BTC-USDT-SWAP", "posSide": "long",
         "closeAvgPx": "69000", "realizedPnl": "50.0",
         "uTime": "1713268800000"},     # newer
        {"instId": "BTC-USDT-SWAP", "posSide": "long",
         "closeAvgPx": "67500", "realizedPnl": "-5.0",
         "uTime": "1713100000000"},     # oldest
    ]}
    client, _ = _client(history)
    enriched = client.enrich_close_fill(_raw_fill())
    # Must pick the uTime=1713268800000 row
    assert enriched.exit_price == pytest.approx(69_000.0)
    assert enriched.pnl_usdt == pytest.approx(50.0)
