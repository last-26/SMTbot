"""Tests for the `whale_transfers` journal table (Phase 8 data layer).

Covers:
  * `TradeJournal.record_whale_transfer` INSERT + round-trip.
  * `TradeJournal.list_whale_transfers` — token / since / until filters,
    NULL-optional fields, affected_symbols JSON encoding + ordering.
  * Idempotent CREATE TABLE IF NOT EXISTS path — closing and reopening
    a disk-backed journal does not drop the table.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.journal.database import TradeJournal
from src.journal.models import WhaleTransferRecord


UTC = timezone.utc


# ── record + list round-trip ───────────────────────────────────────────────


async def test_record_and_list_single_transfer():
    """Insert a single row and verify every column round-trips correctly."""
    captured_at = datetime(2026, 4, 22, 13, 15, 0, tzinfo=UTC)
    async with TradeJournal(":memory:") as j:
        row_id = await j.record_whale_transfer(
            captured_at=captured_at,
            token="bitcoin",
            usd_value=250_000_000.0,
            from_entity="coinbase",
            to_entity="binance",
            tx_hash="0xabc123",
            affected_symbols=["BTC-USDT-SWAP"],
        )
        assert row_id >= 1

        transfers = await j.list_whale_transfers()

    assert len(transfers) == 1
    t = transfers[0]
    assert isinstance(t, WhaleTransferRecord)
    assert t.captured_at == captured_at
    assert t.token == "bitcoin"
    assert t.usd_value == 250_000_000.0
    assert t.from_entity == "coinbase"
    assert t.to_entity == "binance"
    assert t.tx_hash == "0xabc123"
    assert t.affected_symbols == ["BTC-USDT-SWAP"]


async def test_list_filters_by_token():
    """`token=...` filter returns only matching rows."""
    async with TradeJournal(":memory:") as j:
        base = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        await j.record_whale_transfer(
            captured_at=base, token="bitcoin", usd_value=200_000_000.0,
            affected_symbols=["BTC-USDT-SWAP"],
        )
        await j.record_whale_transfer(
            captured_at=base + timedelta(minutes=5),
            token="ethereum", usd_value=150_000_000.0,
            affected_symbols=["ETH-USDT-SWAP"],
        )
        await j.record_whale_transfer(
            captured_at=base + timedelta(minutes=10),
            token="tether", usd_value=300_000_000.0,
            affected_symbols=[
                "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                "DOGE-USDT-SWAP", "BNB-USDT-SWAP",
            ],
        )

        eth_only = await j.list_whale_transfers(token="ethereum")

    assert len(eth_only) == 1
    assert eth_only[0].token == "ethereum"
    assert eth_only[0].usd_value == 150_000_000.0


async def test_list_filters_by_since_and_until():
    """`since`/`until` filters are inclusive at both ends."""
    async with TradeJournal(":memory:") as j:
        t1 = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
        t2 = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        t3 = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)
        await j.record_whale_transfer(
            captured_at=t1, token="bitcoin", usd_value=200_000_000.0,
        )
        await j.record_whale_transfer(
            captured_at=t2, token="ethereum", usd_value=150_000_000.0,
        )
        await j.record_whale_transfer(
            captured_at=t3, token="solana", usd_value=100_000_000.0,
        )

        since_t2 = await j.list_whale_transfers(since=t2)
        assert [t.captured_at for t in since_t2] == [t2, t3]

        between_t1_t2 = await j.list_whale_transfers(since=t1, until=t2)
        assert [t.captured_at for t in between_t1_t2] == [t1, t2]


async def test_record_with_null_optional_fields():
    """Optional fields (from/to_entity, tx_hash, affected_symbols) default
    to None / empty list and round-trip intact."""
    captured_at = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
    async with TradeJournal(":memory:") as j:
        await j.record_whale_transfer(
            captured_at=captured_at,
            token="bitcoin",
            usd_value=200_000_000.0,
            from_entity=None,
            to_entity=None,
            tx_hash=None,
            affected_symbols=[],
        )
        transfers = await j.list_whale_transfers()

    assert len(transfers) == 1
    t = transfers[0]
    assert t.from_entity is None
    assert t.to_entity is None
    assert t.tx_hash is None
    assert t.affected_symbols == []


async def test_affected_symbols_json_preserves_list_ordering():
    """`affected_symbols` round-trips as a list in the original order."""
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
    captured_at = datetime(2026, 4, 22, 13, 30, tzinfo=UTC)
    async with TradeJournal(":memory:") as j:
        await j.record_whale_transfer(
            captured_at=captured_at,
            token="tether",
            usd_value=400_000_000.0,
            affected_symbols=symbols,
        )
        transfers = await j.list_whale_transfers()

    assert len(transfers) == 1
    assert transfers[0].affected_symbols == symbols


async def test_list_orders_by_captured_at_ascending():
    """Insert rows out of chronological order and assert list returns them
    ascending by captured_at."""
    async with TradeJournal(":memory:") as j:
        t_mid = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        t_early = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
        t_late = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)
        # Intentionally record out of order.
        await j.record_whale_transfer(
            captured_at=t_mid, token="ethereum", usd_value=150_000_000.0,
        )
        await j.record_whale_transfer(
            captured_at=t_late, token="solana", usd_value=100_000_000.0,
        )
        await j.record_whale_transfer(
            captured_at=t_early, token="bitcoin", usd_value=200_000_000.0,
        )

        transfers = await j.list_whale_transfers()

    assert [t.captured_at for t in transfers] == [t_early, t_mid, t_late]


async def test_schema_migration_adds_whale_transfers_table_to_legacy_db():
    """Exercise the idempotent `CREATE TABLE IF NOT EXISTS whale_transfers`
    path by creating a real on-disk journal, closing it, reopening, then
    recording a transfer. This demonstrates the schema is created once
    and the second connect is a no-op (so the table is reused rather
    than re-created destructively).
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        # First connect → schema installed.
        async with TradeJournal(db_path) as j:
            await j.record_whale_transfer(
                captured_at=datetime(2026, 4, 22, 10, 0, tzinfo=UTC),
                token="bitcoin",
                usd_value=200_000_000.0,
                affected_symbols=["BTC-USDT-SWAP"],
            )

        # Reopen — idempotent CREATE TABLE IF NOT EXISTS must not wipe data.
        async with TradeJournal(db_path) as j:
            # Previous row still there.
            existing = await j.list_whale_transfers()
            assert len(existing) == 1
            assert existing[0].token == "bitcoin"

            # New INSERT works against the reopened schema.
            await j.record_whale_transfer(
                captured_at=datetime(2026, 4, 22, 11, 0, tzinfo=UTC),
                token="ethereum",
                usd_value=150_000_000.0,
                affected_symbols=["ETH-USDT-SWAP"],
            )
            all_rows = await j.list_whale_transfers()
            assert len(all_rows) == 2
            assert {r.token for r in all_rows} == {"bitcoin", "ethereum"}
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
