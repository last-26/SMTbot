"""Tests for `TradeJournal.update_artifact_flags` (Katman 2).

Covers:
  - Flags persist through a full close round-trip.
  - `demo_artifact=None` when neither side could be checked (feed down).
  - Tri-state booleans: None, True, False.
  - Unknown trade_id raises KeyError (caller notices stale state).
"""

from __future__ import annotations

from datetime import datetime, timezone

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
from src.strategy.trade_plan import TradePlan


UTC = timezone.utc


def _plan() -> TradePlan:
    return TradePlan(
        direction=Direction.BULLISH,
        entry_price=67_000.0, sl_price=66_500.0, tp_price=68_500.0,
        rr_ratio=3.0, sl_distance=500.0, sl_pct=500 / 67_000.0,
        position_size_usdt=1_000.0, leverage=10, required_leverage=10.0,
        num_contracts=5, risk_amount_usdt=10.0, max_risk_usdt=10.0,
        capped=False, sl_source="order_block",
        confluence_score=5.0,
        confluence_factors=["OB_test"], reason="test",
    )


def _report() -> ExecutionReport:
    return ExecutionReport(
        entry=OrderResult(order_id="O1", client_order_id="c1",
                          status=OrderStatus.PENDING),
        algo=AlgoResult(algo_id="A1", client_algo_id="ca1",
                        sl_trigger_px=66_500.0, tp_trigger_px=68_500.0),
        state=PositionState.OPEN, leverage_set=True,
    )


def _close() -> CloseFill:
    return CloseFill(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        entry_price=67_000.0, exit_price=68_500.0, size=5.0,
        pnl_usdt=15.0,
        closed_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
    )


async def _open_journal() -> TradeJournal:
    j = TradeJournal(":memory:")
    await j.connect()
    return j


@pytest.mark.asyncio
async def test_artifact_flags_round_trip() -> None:
    j = await _open_journal()
    try:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 19, 11, 59, tzinfo=UTC),
        )
        await j.record_close(rec.trade_id, _close(), fees_usdt=0.1)

        await j.update_artifact_flags(
            rec.trade_id,
            real_market_entry_valid=True,
            real_market_exit_valid=False,
            demo_artifact=True,
            artifact_reason="exit_above_binance_high",
        )

        got = await j.get_trade(rec.trade_id)
        assert got is not None
        assert got.real_market_entry_valid is True
        assert got.real_market_exit_valid is False
        assert got.demo_artifact is True
        assert got.artifact_reason == "exit_above_binance_high"
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_artifact_flags_all_none_when_feed_down() -> None:
    j = await _open_journal()
    try:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 19, 11, 59, tzinfo=UTC),
        )
        await j.record_close(rec.trade_id, _close(), fees_usdt=0.1)

        await j.update_artifact_flags(
            rec.trade_id,
            real_market_entry_valid=None,
            real_market_exit_valid=None,
            demo_artifact=None,
            artifact_reason=None,
        )

        got = await j.get_trade(rec.trade_id)
        assert got is not None
        assert got.real_market_entry_valid is None
        assert got.real_market_exit_valid is None
        assert got.demo_artifact is None
        assert got.artifact_reason is None
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_artifact_flags_unknown_trade_raises() -> None:
    j = await _open_journal()
    try:
        with pytest.raises(KeyError):
            await j.update_artifact_flags(
                "no-such-id",
                real_market_entry_valid=True,
                real_market_exit_valid=True,
                demo_artifact=False,
                artifact_reason=None,
            )
    finally:
        await j.close()


@pytest.mark.asyncio
async def test_artifact_flags_mixed_validity_tri_state() -> None:
    """One side checked + valid, the other couldn't be checked (None).
    `demo_artifact` should be False: at least one side passed, nothing
    flagged as invalid."""
    j = await _open_journal()
    try:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime(2026, 4, 19, 11, 59, tzinfo=UTC),
        )
        await j.record_close(rec.trade_id, _close(), fees_usdt=0.1)

        await j.update_artifact_flags(
            rec.trade_id,
            real_market_entry_valid=True,
            real_market_exit_valid=None,
            demo_artifact=False,
            artifact_reason=None,
        )

        got = await j.get_trade(rec.trade_id)
        assert got is not None
        assert got.real_market_entry_valid is True
        assert got.real_market_exit_valid is None
        assert got.demo_artifact is False
    finally:
        await j.close()
