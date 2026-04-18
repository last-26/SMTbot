"""Tests for Phase 1.5 Madde 7 — journal derivatives enrichment.

Covers:
  * migration idempotency (ALTER TABLE runs twice without blowing up)
  * record_open accepts the 9 new derivatives kwargs and persists them
  * record_close flips outcome, new fields survive the update
  * regime_breakdown reporter aggregates per-regime stats
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.data.models import Direction
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
)
from src.journal.database import TradeJournal
from src.journal.models import TradeOutcome
from src.journal.reporter import regime_breakdown, summary
from src.strategy.trade_plan import TradePlan


def _plan(direction: Direction = Direction.BULLISH) -> TradePlan:
    return TradePlan(
        direction=direction,
        entry_price=100.0, sl_price=98.0, tp_price=106.0,
        rr_ratio=3.0, sl_distance=2.0, sl_pct=0.02,
        position_size_usdt=1000.0, leverage=10, required_leverage=2.5,
        num_contracts=10,
        risk_amount_usdt=50.0, max_risk_usdt=50.0, capped=False,
        sl_source="swing", reason="test",
        confluence_score=3.5, confluence_factors=["at_fvg", "mss_alignment"],
    )


def _report() -> ExecutionReport:
    entry = OrderResult(
        order_id="O1", client_order_id="C1",
        avg_price=100.0, filled_sz=10,
        status=OrderStatus.FILLED, submitted_at=datetime.now(tz=timezone.utc),
    )
    algo = AlgoResult(
        algo_id="A1", client_algo_id="CA1",
        sl_trigger_px=98.0, tp_trigger_px=106.0,
    )
    return ExecutionReport(entry=entry, algos=[algo])


@pytest.mark.asyncio
async def test_migrations_idempotent_on_repeat_connect(tmp_path: Path):
    """Connecting twice to the same on-disk DB should not raise."""
    db = tmp_path / "trades.db"
    async with TradeJournal(str(db)) as j:
        pass
    # Second open re-runs migrations — they must all except cleanly.
    async with TradeJournal(str(db)) as j:
        rows = await j.list_closed_trades()
        assert rows == []


@pytest.mark.asyncio
async def test_record_open_persists_derivatives_enrichment():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime.now(tz=timezone.utc),
            regime_at_entry="LONG_CROWDED",
            funding_z_at_entry=2.7,
            ls_ratio_at_entry=1.9,
            oi_change_24h_at_entry=12.5,
            liq_imbalance_1h_at_entry=-0.35,
            nearest_liq_cluster_above_price=102.5,
            nearest_liq_cluster_below_price=97.0,
            nearest_liq_cluster_above_notional=5_000_000.0,
            nearest_liq_cluster_below_notional=3_200_000.0,
        )
        assert rec.regime_at_entry == "LONG_CROWDED"
        assert rec.funding_z_at_entry == pytest.approx(2.7)
        # Round-trip via SQL.
        back = await j.get_trade(rec.trade_id)
        assert back is not None
        assert back.ls_ratio_at_entry == pytest.approx(1.9)
        assert back.nearest_liq_cluster_above_notional == pytest.approx(5_000_000.0)


@pytest.mark.asyncio
async def test_record_close_preserves_derivatives_fields():
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime.now(tz=timezone.utc),
            regime_at_entry="CAPITULATION",
            funding_z_at_entry=-3.1,
        )
        fill = CloseFill(
            inst_id="BTC-USDT-SWAP", pos_side="long",
            entry_price=100.0, exit_price=106.0, size=10,
            pnl_usdt=60.0, closed_at=datetime.now(tz=timezone.utc),
        )
        closed = await j.record_close(rec.trade_id, fill)
        assert closed.outcome == TradeOutcome.WIN
        # Enrichment survives the close update.
        assert closed.regime_at_entry == "CAPITULATION"
        assert closed.funding_z_at_entry == pytest.approx(-3.1)


@pytest.mark.asyncio
async def test_regime_breakdown_aggregates_per_regime(monkeypatch):
    """Two wins in LONG_CROWDED + one loss in BALANCED → regime stats split."""
    async with TradeJournal(":memory:") as j:
        # Seed 3 trades with different regimes.
        specs = [
            ("LONG_CROWDED", 80.0),
            ("LONG_CROWDED", 40.0),
            ("BALANCED", -55.0),
        ]
        for regime, pnl in specs:
            rec = await j.record_open(
                _plan(), _report(),
                symbol="BTC-USDT-SWAP",
                signal_timestamp=datetime.now(tz=timezone.utc),
                regime_at_entry=regime,
            )
            await j.record_close(
                rec.trade_id,
                CloseFill(
                    inst_id="BTC-USDT-SWAP", pos_side="long",
                    entry_price=100.0, exit_price=100.0, size=10,
                    pnl_usdt=pnl,
                    closed_at=datetime.now(tz=timezone.utc),
                ),
            )
        closed = await j.list_closed_trades()
    assert len(closed) == 3

    stats = regime_breakdown(closed)
    assert stats["LONG_CROWDED"]["num_trades"] == 2
    assert stats["LONG_CROWDED"]["win_rate"] == pytest.approx(1.0)
    assert stats["BALANCED"]["num_trades"] == 1
    assert stats["BALANCED"]["win_rate"] == pytest.approx(0.0)

    # Full summary includes the breakdown key.
    s = summary(closed, starting_balance=10_000.0)
    assert "regime_breakdown" in s
    assert "LONG_CROWDED" in s["regime_breakdown"]


@pytest.mark.asyncio
async def test_record_open_persists_cluster_distance_atr():
    """BLOK D-7 — cluster distance_atr round-trips through SQL."""
    async with TradeJournal(":memory:") as j:
        rec = await j.record_open(
            _plan(), _report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime.now(tz=timezone.utc),
            nearest_liq_cluster_above_price=102.5,
            nearest_liq_cluster_above_distance_atr=1.25,
            nearest_liq_cluster_below_price=97.0,
            nearest_liq_cluster_below_distance_atr=1.50,
        )
        assert rec.nearest_liq_cluster_above_distance_atr == pytest.approx(1.25)
        back = await j.get_trade(rec.trade_id)
        assert back is not None
        assert back.nearest_liq_cluster_above_distance_atr == pytest.approx(1.25)
        assert back.nearest_liq_cluster_below_distance_atr == pytest.approx(1.50)


def test_regime_breakdown_buckets_none_as_unknown():
    from src.journal.models import TradeRecord

    t1 = TradeRecord(
        trade_id="x1", symbol="BTC-USDT-SWAP", direction=Direction.BULLISH,
        outcome=TradeOutcome.WIN,
        signal_timestamp=datetime.now(tz=timezone.utc),
        entry_timestamp=datetime.now(tz=timezone.utc),
        exit_timestamp=datetime.now(tz=timezone.utc),
        entry_price=100, sl_price=98, tp_price=106, rr_ratio=3.0,
        leverage=10, num_contracts=10, position_size_usdt=1000,
        risk_amount_usdt=50, pnl_usdt=100.0, pnl_r=2.0,
        regime_at_entry=None,
    )
    stats = regime_breakdown([t1])
    assert "UNKNOWN" in stats
    assert stats["UNKNOWN"]["num_trades"] == 1
