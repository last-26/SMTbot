"""Tests for src.journal.database — TradeJournal CRUD + replay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data.models import Direction
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.journal.database import TradeJournal
from src.journal.models import TradeOutcome
from src.strategy.risk_manager import RiskManager
from src.strategy.trade_plan import TradePlan


UTC = timezone.utc


def _plan(direction: Direction = Direction.BULLISH) -> TradePlan:
    return TradePlan(
        direction=direction,
        entry_price=67_000.0,
        sl_price=66_500.0,
        tp_price=68_500.0,
        rr_ratio=3.0,
        sl_distance=500.0,
        sl_pct=500 / 67_000.0,
        position_size_usdt=1_000.0,
        leverage=10,
        required_leverage=10.0,
        num_contracts=5,
        risk_amount_usdt=10.0,
        max_risk_usdt=10.0,
        capped=False,
        sl_source="order_block",
        confluence_score=5.0,
        confluence_factors=["OB_test", "FVG_active", "VMC_bullish"],
        reason="OB tested with FVG overlap",
    )


def _report() -> ExecutionReport:
    return ExecutionReport(
        entry=OrderResult(
            order_id="ORD-123", client_order_id="cliORD-123",
            status=OrderStatus.PENDING,
        ),
        algo=AlgoResult(
            algo_id="ALGO-9", client_algo_id="cliALGO-9",
            sl_trigger_px=66_500.0, tp_trigger_px=68_500.0,
        ),
        state=PositionState.OPEN,
        leverage_set=True,
    )


def _close(pnl: float = 15.0, exit_px: float = 68_500.0,
           closed_at: datetime = None) -> CloseFill:
    return CloseFill(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        entry_price=67_000.0, exit_price=exit_px, size=5.0,
        pnl_usdt=pnl,
        closed_at=closed_at or datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
    )


async def _open_journal() -> TradeJournal:
    j = TradeJournal(":memory:")
    await j.connect()
    return j


# ── Schema ──────────────────────────────────────────────────────────────────


async def test_connect_creates_schema():
    async with TradeJournal(":memory:") as j:
        conn = j._require_conn()
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            rows = await cur.fetchall()
        names = {r[0] for r in rows}
    assert "trades" in names


# ── record_open ─────────────────────────────────────────────────────────────


async def test_record_open_returns_open_record():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 16, 11, 55, tzinfo=UTC),
        )
    assert rec.outcome == TradeOutcome.OPEN
    assert rec.symbol == "BTC-USDT-SWAP"
    assert rec.direction == Direction.BULLISH
    assert rec.trade_id  # non-empty
    assert rec.order_id == "ORD-123"
    assert rec.algo_id == "ALGO-9"
    assert rec.entry_price == 67_000.0


async def test_record_open_generates_unique_trade_ids():
    async with TradeJournal(":memory:") as j:
        a = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        b = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
    assert a.trade_id != b.trade_id


# ── record_close ────────────────────────────────────────────────────────────


async def test_record_close_win_computes_pnl_r():
    async with TradeJournal(":memory:") as j:
        opened = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        updated = await j.record_close(opened.trade_id, _close(pnl=30.0))
    assert updated.outcome == TradeOutcome.WIN
    assert updated.pnl_usdt == 30.0
    # risk_amount_usdt=10 → pnl_r = 3.0
    assert updated.pnl_r == pytest.approx(3.0)
    assert updated.exit_price == 68_500.0


async def test_record_close_loss_sets_outcome_loss():
    async with TradeJournal(":memory:") as j:
        opened = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        updated = await j.record_close(opened.trade_id, _close(pnl=-10.0, exit_px=66_500.0))
    assert updated.outcome == TradeOutcome.LOSS
    assert updated.pnl_r == pytest.approx(-1.0)


async def test_record_close_breakeven():
    async with TradeJournal(":memory:") as j:
        opened = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        updated = await j.record_close(opened.trade_id, _close(pnl=0.0, exit_px=67_000.0))
    assert updated.outcome == TradeOutcome.BREAKEVEN
    assert updated.pnl_r == 0.0


async def test_record_close_unknown_trade_raises():
    async with TradeJournal(":memory:") as j:
        with pytest.raises(KeyError):
            await j.record_close("nonexistent", _close())


# ── Round-trip fields ───────────────────────────────────────────────────────


async def test_get_trade_roundtrips_confluence_factors():
    async with TradeJournal(":memory:") as j:
        plan = _plan()
        plan.confluence_factors = ["OB_test", "FVG_active", "VMC_bullish", "sweep_below"]
        opened = await j.record_open(
            plan, _report(), symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC),
            session="LONDON", htf_bias="BULLISH",
        )
        fetched = await j.get_trade(opened.trade_id)
    assert fetched is not None
    assert fetched.confluence_factors == ["OB_test", "FVG_active", "VMC_bullish", "sweep_below"]
    assert fetched.session == "LONDON"
    assert fetched.htf_bias == "BULLISH"


async def test_get_trade_returns_none_for_unknown():
    async with TradeJournal(":memory:") as j:
        assert await j.get_trade("nonexistent") is None


# ── Listing ─────────────────────────────────────────────────────────────────


async def test_list_open_vs_closed():
    async with TradeJournal(":memory:") as j:
        t_open = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        t_closed = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                       signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        await j.record_close(t_closed.trade_id, _close(pnl=15.0))

        open_list = await j.list_open_trades()
        closed_list = await j.list_closed_trades()

    assert [t.trade_id for t in open_list] == [t_open.trade_id]
    assert [t.trade_id for t in closed_list] == [t_closed.trade_id]


async def test_list_closed_since_filters_by_exit_timestamp():
    async with TradeJournal(":memory:") as j:
        a = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                signal_timestamp=datetime(2026, 4, 10, tzinfo=UTC))
        b = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                signal_timestamp=datetime(2026, 4, 15, tzinfo=UTC))
        await j.record_close(a.trade_id, _close(pnl=5.0,
                                                closed_at=datetime(2026, 4, 10, 12, tzinfo=UTC)))
        await j.record_close(b.trade_id, _close(pnl=5.0,
                                                closed_at=datetime(2026, 4, 15, 12, tzinfo=UTC)))

        since = datetime(2026, 4, 14, tzinfo=UTC)
        filtered = await j.list_closed_trades(since=since)
    assert [t.trade_id for t in filtered] == [b.trade_id]


# ── Cancel ──────────────────────────────────────────────────────────────────


async def test_mark_canceled_excludes_from_closed_list():
    async with TradeJournal(":memory:") as j:
        opened = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        await j.mark_canceled(opened.trade_id, reason="signal invalidated")

        row = await j.get_trade(opened.trade_id)
        open_list = await j.list_open_trades()
        closed_list = await j.list_closed_trades()

    assert row.outcome == TradeOutcome.CANCELED
    assert row.notes == "signal invalidated"
    assert open_list == []
    assert closed_list == []


async def test_mark_canceled_unknown_raises():
    async with TradeJournal(":memory:") as j:
        with pytest.raises(KeyError):
            await j.mark_canceled("nope")


# ── Replay ──────────────────────────────────────────────────────────────────


async def test_persists_to_disk_and_reopens(tmp_path):
    """Write a full lifecycle, close the DB, reopen — row must survive."""
    db = tmp_path / "trades.db"
    async with TradeJournal(str(db)) as j:
        opened = await j.record_open(_plan(), _report(), symbol="BTC-USDT-SWAP",
                                     signal_timestamp=datetime(2026, 4, 16, tzinfo=UTC))
        await j.record_close(opened.trade_id, _close(pnl=15.0))
        saved_id = opened.trade_id

    async with TradeJournal(str(db)) as j2:
        fetched = await j2.get_trade(saved_id)
    assert fetched is not None
    assert fetched.outcome == TradeOutcome.WIN
    assert fetched.pnl_r == pytest.approx(1.5)
    assert fetched.confluence_factors == ["OB_test", "FVG_active", "VMC_bullish"]


async def test_replay_for_risk_manager_rebuilds_streaks_and_peak():
    """3 wins then 2 losses → peak after the wins, consecutive_losses=2 at end."""
    async with TradeJournal(":memory:") as j:
        t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
        deltas = [0, 1, 2, 3, 4]
        pnls = [+10.0, +10.0, +10.0, -10.0, -10.0]
        for i, (d, p) in enumerate(zip(deltas, pnls)):
            opened = await j.record_open(
                _plan(), _report(), symbol="BTC-USDT-SWAP",
                signal_timestamp=t0 + timedelta(hours=d),
                entry_timestamp=t0 + timedelta(hours=d),
            )
            await j.record_close(opened.trade_id,
                                 _close(pnl=p, closed_at=t0 + timedelta(hours=d, minutes=30)))

        mgr = RiskManager(starting_balance=1_000.0, now=t0)
        await j.replay_for_risk_manager(mgr)

    assert mgr.current_balance == pytest.approx(1_010.0)     # +30 -20
    assert mgr.peak_balance == pytest.approx(1_030.0)        # after 3 wins
    assert mgr.consecutive_losses == 2
    assert mgr.open_positions == 0                           # every open paired with a close
