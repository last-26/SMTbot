"""position_snapshots table — schema, CRUD, JSON round-trip, migrations.

2026-04-26 — intra-trade time-series joined to `trades.trade_id` for
post-hoc RL/GBT trajectory analysis. Writer is `record_position_snapshot`
called from the runner's cadence-gated batch loop; reader is
`get_position_snapshots(trade_id)` for tests + future analytics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from src.journal.database import TradeJournal
from src.journal.models import PositionSnapshotRecord


UTC = timezone.utc


# ── Schema ──────────────────────────────────────────────────────────────────


async def test_connect_creates_position_snapshots_table_and_indexes():
    """Schema bootstrap creates table + both indexes (trade_id + captured_at)."""
    async with TradeJournal(":memory:") as j:
        conn = j._require_conn()
        # Table exists
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='position_snapshots'"
        ) as cur:
            tables = await cur.fetchall()
        assert len(tables) == 1
        # Indexes exist
        async with conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='position_snapshots'"
        ) as cur:
            idx_rows = await cur.fetchall()
        idx_names = {r[0] for r in idx_rows}
        assert "idx_position_snapshots_trade_id" in idx_names
        assert "idx_position_snapshots_captured_at" in idx_names


async def test_idempotent_migration_on_reconnect(tmp_path):
    """Re-opening an existing DB must not raise — CREATE/INDEX use IF NOT EXISTS
    in _SCHEMA, and the explicit migration entries are wrapped in
    OperationalError swallow."""
    db_path = tmp_path / "trades.db"
    j1 = TradeJournal(str(db_path))
    await j1.connect()
    await j1.close()
    # Second connect on same file path — would raise if migrations weren't
    # idempotent.
    j2 = TradeJournal(str(db_path))
    await j2.connect()
    await j2.close()


# ── Round-trip: writer → reader ─────────────────────────────────────────────


async def test_record_and_read_full_snapshot():
    """Every column round-trips with bit-exact values."""
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
        row_id = await j.record_position_snapshot(
            trade_id="trade-abc-1",
            captured_at=ts,
            mark_price=78_000.0,
            unrealized_pnl_usdt=42.5,
            unrealized_pnl_r=0.85,
            mfe_r_so_far=1.10,
            mae_r_so_far=-0.30,
            current_sl_price=77_500.0,
            current_tp_price=79_000.0,
            sl_to_be_moved=True,
            mfe_lock_applied=False,
            derivatives_funding_now=0.00012,
            derivatives_oi_now_usd=7.5e9,
            derivatives_ls_ratio_now=1.34,
            derivatives_long_liq_1h_now=1_250_000.0,
            derivatives_short_liq_1h_now=890_000.0,
            on_chain_btc_netflow_now_usd=-50_000_000.0,
            on_chain_stablecoin_pulse_now=12_000_000.0,
            on_chain_flow_alignment_now=0.42,
            oscillator_3m_now_json={"wt1": -45.2, "rsi": 38.5},
            vwap_3m_distance_atr_now=-0.65,
        )
        assert row_id >= 1
        out = await j.get_position_snapshots("trade-abc-1")
        assert len(out) == 1
        rec = out[0]
        assert rec.trade_id == "trade-abc-1"
        assert rec.captured_at == ts
        assert rec.mark_price == 78_000.0
        assert rec.unrealized_pnl_usdt == 42.5
        assert rec.unrealized_pnl_r == 0.85
        assert rec.mfe_r_so_far == 1.10
        assert rec.mae_r_so_far == -0.30
        assert rec.current_sl_price == 77_500.0
        assert rec.current_tp_price == 79_000.0
        assert rec.sl_to_be_moved is True
        assert rec.mfe_lock_applied is False
        assert rec.derivatives_funding_now == 0.00012
        assert rec.derivatives_oi_now_usd == 7.5e9
        assert rec.derivatives_ls_ratio_now == 1.34
        assert rec.derivatives_long_liq_1h_now == 1_250_000.0
        assert rec.derivatives_short_liq_1h_now == 890_000.0
        assert rec.on_chain_btc_netflow_now_usd == -50_000_000.0
        assert rec.on_chain_stablecoin_pulse_now == 12_000_000.0
        assert rec.on_chain_flow_alignment_now == 0.42
        assert rec.oscillator_3m_now_json == {"wt1": -45.2, "rsi": 38.5}
        assert rec.vwap_3m_distance_atr_now == -0.65


async def test_record_with_only_required_fields_uses_none_defaults():
    """All drift fields default to None / empty dict when caller omits them."""
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
        await j.record_position_snapshot(
            trade_id="trade-min-1",
            captured_at=ts,
            mark_price=78_500.0,
            unrealized_pnl_usdt=10.0,
            unrealized_pnl_r=0.20,
            mfe_r_so_far=0.20,
            mae_r_so_far=0.0,
            current_sl_price=78_000.0,
        )
        out = await j.get_position_snapshots("trade-min-1")
        assert len(out) == 1
        rec = out[0]
        assert rec.current_tp_price is None
        assert rec.sl_to_be_moved is False
        assert rec.mfe_lock_applied is False
        assert rec.derivatives_funding_now is None
        assert rec.derivatives_oi_now_usd is None
        assert rec.derivatives_ls_ratio_now is None
        assert rec.derivatives_long_liq_1h_now is None
        assert rec.derivatives_short_liq_1h_now is None
        assert rec.on_chain_btc_netflow_now_usd is None
        assert rec.on_chain_stablecoin_pulse_now is None
        assert rec.on_chain_flow_alignment_now is None
        assert rec.oscillator_3m_now_json == {}
        assert rec.vwap_3m_distance_atr_now is None


# ── Multiple snapshots / ordering / cross-trade isolation ───────────────────


async def test_multiple_snapshots_per_trade_ordered_by_captured_at():
    """Reader returns snapshots in chronological order regardless of
    insert order."""
    async with TradeJournal(":memory:") as j:
        t0 = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
        # Insert OUT of chronological order to prove the ORDER BY clause.
        for offset_min in (10, 0, 5):
            await j.record_position_snapshot(
                trade_id="trade-multi-1",
                captured_at=t0 + timedelta(minutes=offset_min),
                mark_price=78_000.0 + offset_min,
                unrealized_pnl_usdt=0.0,
                unrealized_pnl_r=0.0,
                mfe_r_so_far=0.1 * offset_min,
                mae_r_so_far=0.0,
                current_sl_price=77_500.0,
            )
        out = await j.get_position_snapshots("trade-multi-1")
        assert len(out) == 3
        offsets = [(r.captured_at - t0).total_seconds() / 60 for r in out]
        assert offsets == [0, 5, 10]


async def test_cross_trade_isolation():
    """get_position_snapshots(X) returns only X's rows."""
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 15, 0, tzinfo=UTC)
        for i in range(3):
            await j.record_position_snapshot(
                trade_id="trade-A",
                captured_at=ts + timedelta(minutes=i),
                mark_price=78_000.0, unrealized_pnl_usdt=1.0,
                unrealized_pnl_r=0.02, mfe_r_so_far=0.05, mae_r_so_far=0.0,
                current_sl_price=77_500.0,
            )
        for i in range(2):
            await j.record_position_snapshot(
                trade_id="trade-B",
                captured_at=ts + timedelta(minutes=i),
                mark_price=2300.0, unrealized_pnl_usdt=2.0,
                unrealized_pnl_r=0.04, mfe_r_so_far=0.10, mae_r_so_far=0.0,
                current_sl_price=2270.0,
            )
        a_rows = await j.get_position_snapshots("trade-A")
        b_rows = await j.get_position_snapshots("trade-B")
        assert len(a_rows) == 3
        assert len(b_rows) == 2
        assert all(r.trade_id == "trade-A" for r in a_rows)
        assert all(r.trade_id == "trade-B" for r in b_rows)


async def test_read_unknown_trade_id_returns_empty_list():
    async with TradeJournal(":memory:") as j:
        out = await j.get_position_snapshots("nope")
        assert out == []


# ── JSON / type-coercion edge cases ─────────────────────────────────────────


async def test_oscillator_json_roundtrip_with_nested_structure():
    """`oscillator_3m_now_json` accepts arbitrary dict shapes (nested
    floats, ints, bools, strings) and round-trips them."""
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 16, 0, tzinfo=UTC)
        payload = {
            "wt1": -45.2, "wt2": -38.1,
            "rsi": 38.5, "rsi_mfi": 42.1,
            "stoch_k": 28.3, "stoch_d": 31.7,
            "momentum": 0.45,
            "div_bull_regular": True, "div_bear_regular": False,
            "last_signal": "bullish_cross",
        }
        await j.record_position_snapshot(
            trade_id="trade-osc",
            captured_at=ts,
            mark_price=78_000.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=77_500.0,
            oscillator_3m_now_json=payload,
        )
        out = await j.get_position_snapshots("trade-osc")
        assert out[0].oscillator_3m_now_json == payload


async def test_oscillator_json_null_in_db_reads_as_empty_dict():
    """If the column is NULL in SQLite (caller passed None), reader
    surfaces empty dict — not a crash, not None."""
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 17, 0, tzinfo=UTC)
        await j.record_position_snapshot(
            trade_id="trade-osc-null",
            captured_at=ts,
            mark_price=78_000.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=77_500.0,
            oscillator_3m_now_json=None,
        )
        out = await j.get_position_snapshots("trade-osc-null")
        assert out[0].oscillator_3m_now_json == {}


async def test_bool_flags_persist_as_int_and_read_back_as_bool():
    async with TradeJournal(":memory:") as j:
        ts = datetime(2026, 4, 26, 18, 0, tzinfo=UTC)
        await j.record_position_snapshot(
            trade_id="trade-flags",
            captured_at=ts,
            mark_price=78_000.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=77_500.0,
            sl_to_be_moved=True, mfe_lock_applied=True,
        )
        # Raw column inspection
        conn = j._require_conn()
        async with conn.execute(
            "SELECT sl_to_be_moved, mfe_lock_applied FROM position_snapshots "
            "WHERE trade_id = ?", ("trade-flags",),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1
        assert row[1] == 1
        # Pydantic re-coerces back to bool
        out = await j.get_position_snapshots("trade-flags")
        assert out[0].sl_to_be_moved is True
        assert out[0].mfe_lock_applied is True


async def test_returned_record_is_pydantic_model():
    async with TradeJournal(":memory:") as j:
        await j.record_position_snapshot(
            trade_id="trade-pydantic",
            captured_at=datetime(2026, 4, 26, 19, tzinfo=UTC),
            mark_price=78_000.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=77_500.0,
        )
        out = await j.get_position_snapshots("trade-pydantic")
        assert isinstance(out[0], PositionSnapshotRecord)


# Phase A.7 (2026-05-02) — confluence_score_now column

async def test_confluence_score_now_round_trip():
    """Writer accepts the new column; reader returns the value."""
    async with TradeJournal(":memory:") as j:
        await j.record_position_snapshot(
            trade_id="trade-conf",
            captured_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
            mark_price=100.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.5, mae_r_so_far=-0.3,
            current_sl_price=99.0,
            confluence_score_now=3.25,
        )
        out = await j.get_position_snapshots("trade-conf")
        assert len(out) == 1
        assert out[0].confluence_score_now == pytest.approx(3.25)


async def test_confluence_score_now_defaults_to_null():
    """Callers that don't pass `confluence_score_now` get NULL → reader
    surfaces None. Pre-Phase-A.7 rows on existing DBs read this way too."""
    async with TradeJournal(":memory:") as j:
        await j.record_position_snapshot(
            trade_id="trade-no-conf",
            captured_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
            mark_price=100.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=99.0,
        )
        out = await j.get_position_snapshots("trade-no-conf")
        assert out[0].confluence_score_now is None


async def test_confluence_score_now_supports_signed_values():
    """Sign carries direction: positive = aligned with position, negative
    = opposing. Stored as REAL so both signs round-trip cleanly."""
    async with TradeJournal(":memory:") as j:
        # Aligned (positive)
        await j.record_position_snapshot(
            trade_id="trade-aligned",
            captured_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
            mark_price=100.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=99.0,
            confluence_score_now=4.5,
        )
        # Opposing (negative)
        await j.record_position_snapshot(
            trade_id="trade-opposed",
            captured_at=datetime(2026, 5, 2, 12, tzinfo=UTC),
            mark_price=100.0, unrealized_pnl_usdt=0.0,
            unrealized_pnl_r=0.0, mfe_r_so_far=0.0, mae_r_so_far=0.0,
            current_sl_price=99.0,
            confluence_score_now=-2.75,
        )
        aligned = await j.get_position_snapshots("trade-aligned")
        opposed = await j.get_position_snapshots("trade-opposed")
        assert aligned[0].confluence_score_now == pytest.approx(4.5)
        assert opposed[0].confluence_score_now == pytest.approx(-2.75)
