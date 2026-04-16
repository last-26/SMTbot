"""Journal-side support for partial TP (Madde E).

Two new columns (`algo_ids`, `close_reason`) + migration. Tests confirm
round-trip, idempotent ALTER, and the `update_algo_ids` helper.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from src.execution.models import AlgoResult, CloseFill, ExecutionReport, OrderResult, OrderStatus, PositionState
from src.journal.database import TradeJournal
from src.strategy.trade_plan import TradePlan
from src.data.models import Direction


UTC = timezone.utc


def _plan() -> TradePlan:
    return TradePlan(
        direction=Direction.BULLISH,
        entry_price=100.0, sl_price=99.0, tp_price=103.0,
        rr_ratio=3.0, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=1_000.0,
        leverage=10, required_leverage=10.0,
        num_contracts=5,
        risk_amount_usdt=10.0, max_risk_usdt=10.0, capped=False,
    )


def _report_two_algos() -> ExecutionReport:
    return ExecutionReport(
        entry=OrderResult(
            order_id="ORD-1", client_order_id="cli-ord-1",
            status=OrderStatus.PENDING,
        ),
        algos=[
            AlgoResult(algo_id="ALG1", client_algo_id="cli1",
                       sl_trigger_px=99.0, tp_trigger_px=101.5),
            AlgoResult(algo_id="ALG2", client_algo_id="cli2",
                       sl_trigger_px=99.0, tp_trigger_px=103.0),
        ],
        state=PositionState.OPEN, leverage_set=True,
    )


# ── Round-trip ──────────────────────────────────────────────────────────────


async def test_algo_ids_round_trip():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report_two_algos(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 17, tzinfo=UTC),
        )
        fetched = await j.get_trade(rec.trade_id)
        assert fetched is not None
        assert fetched.algo_ids == ["ALG1", "ALG2"]
        assert fetched.close_reason is None


async def test_update_algo_ids_rewrites_column():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report_two_algos(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 17, tzinfo=UTC),
        )
        await j.update_algo_ids(rec.trade_id, ["ALG1", "NEW_BE"])
        fetched = await j.get_trade(rec.trade_id)
        assert fetched.algo_ids == ["ALG1", "NEW_BE"]


async def test_close_reason_persists_on_record_close():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report_two_algos(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 17, tzinfo=UTC),
        )
        fill = CloseFill(
            inst_id="BTC-USDT-SWAP", pos_side="long",
            entry_price=100.0, exit_price=102.0, size=5.0,
            pnl_usdt=10.0,
            closed_at=datetime(2026, 4, 17, 1, tzinfo=UTC),
        )
        await j.record_close(rec.trade_id, fill, close_reason="EARLY_CLOSE_LTF_REVERSAL")
        fetched = await j.get_trade(rec.trade_id)
        assert fetched.close_reason == "EARLY_CLOSE_LTF_REVERSAL"


# ── Idempotent migration ────────────────────────────────────────────────────


async def test_schema_migration_idempotent(tmp_path):
    db_path = str(tmp_path / "legacy.db")

    # Seed a pre-Madde-E DB that lacks algo_ids / close_reason.
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                outcome TEXT NOT NULL,
                signal_timestamp TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT,
                entry_price REAL NOT NULL,
                sl_price REAL NOT NULL,
                tp_price REAL NOT NULL,
                rr_ratio REAL NOT NULL,
                leverage INTEGER NOT NULL,
                num_contracts INTEGER NOT NULL,
                position_size_usdt REAL NOT NULL,
                risk_amount_usdt REAL NOT NULL,
                sl_source TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                confluence_score REAL NOT NULL DEFAULT 0,
                confluence_factors TEXT NOT NULL DEFAULT '[]',
                order_id TEXT, algo_id TEXT, client_order_id TEXT, client_algo_id TEXT,
                entry_timeframe TEXT, htf_timeframe TEXT, htf_bias TEXT,
                session TEXT, market_structure TEXT,
                exit_price REAL, pnl_usdt REAL, pnl_r REAL,
                fees_usdt REAL NOT NULL DEFAULT 0,
                notes TEXT, screenshot_entry TEXT, screenshot_exit TEXT
            );
        """)
        await db.commit()

    # First connect: migration runs and adds the columns.
    async with TradeJournal(db_path) as j:
        rec = await j.record_open(
            _plan(), _report_two_algos(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 17, tzinfo=UTC),
        )

    # Second connect: migration re-runs without error, existing row is readable.
    async with TradeJournal(db_path) as j:
        fetched = await j.get_trade(rec.trade_id)
        assert fetched is not None
        assert fetched.algo_ids == ["ALG1", "ALG2"]
