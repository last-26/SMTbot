"""Integration tests for src.bot.runner.BotRunner.

`build_trade_plan_from_state` is heavy (runs full confluence analysis), so
we monkeypatch it in `src.bot.runner` to return a canned plan / None.
That keeps these tests focused on the RUNNER's orchestration — what gets
called, in what order, with what state mutations — rather than re-testing
the strategy layer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.bot.runner import BotContext, BotRunner
from src.data.models import Direction
from src.execution.errors import AlgoOrderError
from src.execution.models import PositionSnapshot
from src.journal.database import TradeJournal
from src.journal.models import TradeOutcome
from src.strategy.risk_manager import RiskManager
from tests.conftest import (
    FakeMonitor,
    FakeBybitClient,
    FakeReader,
    FakeRouter,
    make_close_fill,
    make_config,
    make_plan,
    make_report,
    make_state,
)


UTC = timezone.utc


def _patch_plan_builder(monkeypatch, plan_or_none):
    """Make build_trade_plan_with_reason return a constant value."""
    reason = "" if plan_or_none is not None else "below_confluence"

    def _stub(*a, **kw):
        return plan_or_none, reason
    monkeypatch.setattr("src.bot.runner.build_trade_plan_with_reason", _stub)


# ── No-signal / signal paths ────────────────────────────────────────────────


async def test_run_once_no_signal_does_nothing(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
    assert fakes.router.calls == []
    assert fakes.risk_mgr.open_positions == 0
    assert ctx.open_trade_ids == {}


async def test_run_once_bullish_signal_places_order_and_journals(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert len(fakes.router.calls) == 1
    placed, inst_id = fakes.router.calls[0]
    assert placed.direction == Direction.BULLISH
    assert inst_id == "BTC-USDT-SWAP"
    assert fakes.risk_mgr.open_positions == 1
    assert len(ctx.open_trade_ids) == 1
    key = ("BTC-USDT-SWAP", "long")
    assert key in ctx.open_trade_ids
    assert fakes.monitor.registered == [
        ("BTC-USDT-SWAP", "long", 5.0, 67_000.0),
    ]


# ── Dedup ────────────────────────────────────────────────────────────────────


async def test_symbol_level_dedup_blocks_second_open_same_tick(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        await runner.run_once()
    assert len(fakes.router.calls) == 1
    assert fakes.risk_mgr.open_positions == 1


async def test_dedup_clears_after_close_is_processed(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    cfg = make_config()
    # Disable the Madde C reentry gate so this dedup test only asserts the
    # open/close-dedup contract, not the post-close quality gate.
    cfg.reentry.min_bars_after_close = 0
    cfg.reentry.min_atr_move = 0.0
    cfg.reentry.require_higher_confluence_after_win = False
    cfg.reentry.require_higher_or_equal_confluence_after_loss = False
    ctx, fakes = make_ctx(config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()                          # open #1
        # Queue the close — enrichment returns a real-looking fill
        fakes.bybit_client.enrich_return = make_close_fill(pnl_usdt=30.0)
        fakes.monitor.queued_fills.append(make_close_fill(pnl_usdt=0.0))
        await runner.run_once()                          # process close → clears dedup
        fakes.bybit_client.enrich_return = None
        await runner.run_once()                          # can open again

    assert len(fakes.router.calls) == 2


# ── Risk manager gate ───────────────────────────────────────────────────────


async def test_circuit_breaker_blocks_order(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    ctx, fakes = make_ctx()
    # Trip consecutive_losses over the threshold
    fakes.risk_mgr.consecutive_losses = 10
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
    assert fakes.router.calls == []


# ── Execution failure ───────────────────────────────────────────────────────


async def test_algo_failure_does_not_record_open(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    router = FakeRouter(raise_exc=AlgoOrderError("algo rejected"))
    ctx, fakes = make_ctx(router=router)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
    # Router was called but raised → nothing should register.
    assert len(router.calls) == 1
    assert fakes.risk_mgr.open_positions == 0
    assert ctx.open_trade_ids == {}
    assert fakes.monitor.registered == []


# ── Close handling ──────────────────────────────────────────────────────────


async def test_process_closes_flows_through_enrichment_and_updates_risk(monkeypatch, make_ctx):
    # Test the close flow without re-opening — signal returns None on tick 2.
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    starting = fakes.risk_mgr.current_balance

    # Tick 1: plan available → open
    _patch_plan_builder(monkeypatch, make_plan())
    async with ctx.journal:
        await runner.run_once()
        trade_id = next(iter(ctx.open_trade_ids.values()))

        # Tick 2: no plan, but monitor has a zeroed fill that enrichment
        # replaces with pnl=+30
        _patch_plan_builder(monkeypatch, None)
        fakes.monitor.queued_fills.append(make_close_fill(pnl_usdt=0.0))
        fakes.bybit_client.enrich_return = make_close_fill(pnl_usdt=30.0)
        await runner.run_once()

        rec = await ctx.journal.get_trade(trade_id)
        assert rec is not None
        assert rec.outcome == TradeOutcome.WIN
        assert rec.pnl_usdt == pytest.approx(30.0)

    assert fakes.risk_mgr.current_balance == pytest.approx(starting + 30.0)
    assert ctx.open_trade_ids == {}


async def test_orphan_close_still_updates_risk_balance(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)   # no open this tick
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    starting = fakes.risk_mgr.current_balance
    # Orphan close: no trade_id in open_trade_ids, but monitor emits a fill
    fakes.monitor.queued_fills.append(make_close_fill(pnl_usdt=0.0))
    fakes.bybit_client.enrich_return = make_close_fill(pnl_usdt=-15.0)
    # Orphan close must still decrement risk_mgr.open_positions doesn't matter
    # (it's 0), but balance must update
    fakes.risk_mgr.open_positions = 1      # simulate: was opened, we forgot
    async with ctx.journal:
        await runner.run_once()
    assert fakes.risk_mgr.current_balance == pytest.approx(starting - 15.0)


# ── Startup primer ─────────────────────────────────────────────────────────


async def test_startup_replay_rebuilds_peak_and_streak(monkeypatch, make_ctx, tmp_path):
    # Seed a real on-disk journal then point a new runner at it.
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        for i, pnl in enumerate([+10.0, +10.0, +10.0, -10.0, -10.0]):
            opened = await seed.record_open(
                make_plan(), make_report(), symbol="BTC-USDT-SWAP",
                signal_timestamp=t0 + timedelta(hours=i),
                entry_timestamp=t0 + timedelta(hours=i),
            )
            await seed.record_close(opened.trade_id, make_close_fill(
                pnl_usdt=pnl,
                closed_at=t0 + timedelta(hours=i, minutes=30),
            ))

    _patch_plan_builder(monkeypatch, None)   # no new trades this tick
    cfg = make_config()
    journal = TradeJournal(str(db))
    risk_mgr = RiskManager(cfg.bot.starting_balance, cfg.breakers(), now=t0)
    ctx, fakes = make_ctx(journal=journal, risk_mgr=risk_mgr, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    # 3 wins (+30) then 2 losses (-20) on starting 1000 → 1010; peak after 3 wins = 1030
    assert risk_mgr.current_balance == pytest.approx(1_010.0)
    assert risk_mgr.peak_balance == pytest.approx(1_030.0)
    assert risk_mgr.consecutive_losses == 2
    assert risk_mgr.open_positions == 0


async def test_startup_rehydrates_open_positions_to_monitor(monkeypatch, tmp_path, make_ctx):
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        await seed.record_open(make_plan(), make_report(),
                               symbol="BTC-USDT-SWAP",
                               signal_timestamp=t0, entry_timestamp=t0)
    cfg = make_config()
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    assert len(fakes.monitor.registered) == 1
    inst, pos_side, size, entry = fakes.monitor.registered[0]
    assert inst == "BTC-USDT-SWAP" and pos_side == "long"
    assert ("BTC-USDT-SWAP", "long") in ctx.open_trade_ids


async def test_rehydrate_places_fresh_tp_limit_for_open_position(
    monkeypatch, tmp_path, make_ctx,
):
    """Resting TP limits don't survive restart (orphan-pending-limit sweep
    wipes them). Rehydrate must re-place one per journal OPEN row that has
    not moved SL to BE, so maker-TP coverage is restored."""
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        await seed.record_open(
            make_plan(), make_report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=t0, entry_timestamp=t0,
        )
    cfg = make_config()
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    assert len(fakes.bybit_client.tp_limits_placed) == 1
    placed = fakes.bybit_client.tp_limits_placed[0]
    assert placed["inst_id"] == "BTC-USDT-SWAP"
    assert placed["pos_side"] == "long"
    assert placed["post_only"] is True
    # Monitor got the fresh TP limit id threaded through register_open.
    extras = fakes.monitor.register_extras[0]
    assert extras["tp_limit_order_id"] == "TP_LIMIT_1"


async def test_rehydrate_skips_tp_limit_when_be_already_moved(
    monkeypatch, tmp_path, make_ctx,
):
    """Post-BE positions lost their plan_sl_price in the journal, so the
    rehydrate path explicitly skips TP-limit re-placement — the runner OCO
    (with BE stop) still protects."""
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        rec = await seed.record_open(
            make_plan(), make_report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=t0, entry_timestamp=t0,
        )
        # update_algo_ids side-effect: sets sl_moved_to_be=1 (TP1-fill path).
        await seed.update_algo_ids(rec.trade_id, ["ALG-1", "BE_NEW"])
    cfg = make_config()
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    # Post-BE → no TP-limit re-placement; monitor still registered normally.
    assert fakes.bybit_client.tp_limits_placed == []
    extras = fakes.monitor.register_extras[0]
    assert extras["be_already_moved"] is True
    assert extras["tp_limit_order_id"] == ""


async def test_rehydrate_skips_tp_limit_when_feature_disabled(
    monkeypatch, tmp_path, make_ctx,
):
    """Config flag `tp_resting_limit_enabled=False` disables the rehydrate
    TP-limit re-placement entirely."""
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        await seed.record_open(
            make_plan(), make_report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=t0, entry_timestamp=t0,
        )
    cfg = make_config()
    cfg.execution.tp_resting_limit_enabled = False
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    assert fakes.bybit_client.tp_limits_placed == []
    extras = fakes.monitor.register_extras[0]
    assert extras["tp_limit_order_id"] == ""


async def test_rehydrate_tolerates_tp_limit_place_failure(
    monkeypatch, tmp_path, make_ctx,
):
    """If the TP-limit place fails on rehydrate (e.g. 51124 post_only
    reject because book has already reached TP), the monitor still gets
    registered — OCO market-TP still protects."""
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        await seed.record_open(
            make_plan(), make_report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=t0, entry_timestamp=t0,
        )
    cfg = make_config()
    client = FakeBybitClient(tp_limit_place_raises=RuntimeError("51124"))
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg, bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    assert len(fakes.monitor.registered) == 1
    extras = fakes.monitor.register_extras[0]
    assert extras["tp_limit_order_id"] == ""


async def test_rehydrate_passes_be_already_moved_from_journal(monkeypatch, tmp_path, make_ctx):
    """If the journal row shows SL-to-BE already completed pre-restart, the
    rehydrate path must forward `be_already_moved=True` to the monitor so it
    doesn't re-cancel the already-replaced TP2 on the next poll."""
    db = tmp_path / "trades.db"
    t0 = datetime(2026, 4, 16, 9, tzinfo=UTC)
    async with TradeJournal(str(db)) as seed:
        rec = await seed.record_open(make_plan(), make_report(),
                                     symbol="BTC-USDT-SWAP",
                                     signal_timestamp=t0, entry_timestamp=t0)
        # Simulate TP1 firing pre-restart: algo_ids rewritten + flag stamped.
        await seed.update_algo_ids(rec.trade_id, ["ALG-1", "NEW_BE"])

    cfg = make_config()
    journal = TradeJournal(str(db))
    ctx, fakes = make_ctx(journal=journal, config=cfg)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._prime()

    assert len(fakes.monitor.register_extras) == 1
    extras = fakes.monitor.register_extras[0]
    assert extras["be_already_moved"] is True
    assert extras["algo_ids"] == ["ALG-1", "NEW_BE"]


async def test_reconcile_logs_orphan_live_without_journal(monkeypatch, caplog, make_ctx):
    # Intercept loguru into pytest's caplog via a one-off sink.
    from loguru import logger
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m), level="ERROR")
    try:
        client = FakeBybitClient(positions=[
            PositionSnapshot(
                inst_id="BTC-USDT-SWAP", pos_side="long",
                size=5.0, entry_price=67_000.0, mark_price=67_100.0,
                unrealized_pnl=5.0, leverage=10,
            ),
        ])
        ctx, fakes = make_ctx(bybit_client=client)
        runner = BotRunner(ctx)
        async with ctx.journal:
            await runner._reconcile_orphans()
        assert any("orphan_live_position_no_journal_row" in m for m in messages)
    finally:
        logger.remove(sink_id)


# ── Startup orphan cancels (pending limits + surplus OCOs) ─────────────────


class _TradeSubStub:
    """Drop-in for `client.trade` — exposes get_order_list only."""
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def get_order_list(self, instType: str = "SWAP") -> dict:
        return {"code": "0", "data": list(self._rows)}


class FakeBybitClientWithPending(FakeBybitClient):
    """FakeBybitClient + the surfaces `_reconcile_orphans` touches:
    `trade.get_order_list`, `cancel_order`, `list_pending_algos`,
    plus call logs so tests can assert."""

    def __init__(
        self, *,
        positions: Optional[list[PositionSnapshot]] = None,
        pending_limits: Optional[list[dict]] = None,
        pending_algos: Optional[list[dict]] = None,
    ):
        super().__init__(positions=positions)
        self.trade = _TradeSubStub(pending_limits or [])
        self._pending_algos = pending_algos or []
        self.cancel_order_calls: list[tuple[str, str]] = []
        self.list_pending_algos_calls: list[str] = []

    def cancel_order(self, inst_id: str, order_id: str) -> dict:
        self.cancel_order_calls.append((inst_id, order_id))
        return {}

    def list_pending_algos(
        self, inst_id: Optional[str] = None, ord_type: str = "oco",
    ) -> list[dict]:
        self.list_pending_algos_calls.append(ord_type)
        return list(self._pending_algos)


async def test_reconcile_cancels_every_resting_pending_limit(make_ctx):
    """The monitor's in-memory _pending is empty at startup, so any resting
    limit on OKX can't be tracked — reconcile must cancel them so they
    can't fill into an untracked (unprotected) position."""
    # Need Optional import for the helper class
    client = FakeBybitClientWithPending(
        pending_limits=[
            {"instId": "ETH-USDT-SWAP", "ordId": "ORPH_ETH",
             "px": "2280.04", "sz": "29"},
            {"instId": "BNB-USDT-SWAP", "ordId": "ORPH_BNB",
             "px": "620.3", "sz": "1182"},
        ],
    )
    ctx, fakes = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._reconcile_orphans()
    assert ("ETH-USDT-SWAP", "ORPH_ETH") in client.cancel_order_calls
    assert ("BNB-USDT-SWAP", "ORPH_BNB") in client.cancel_order_calls


# Note: tests for `_cancel_surplus_ocos` were removed when that method
# itself was deleted in the 2026-04-26 OKX cleanup (Phase 3). On Bybit V5
# TP/SL is a position attribute, not a separate algo order, so there is
# no orphan-OCO sweep to test.


# ── Shutdown ────────────────────────────────────────────────────────────────


async def test_shutdown_event_exits_run_loop_promptly(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    runner.shutdown.set()          # request shutdown before run() begins
    await asyncio.wait_for(runner.run(), timeout=1.0)
    # If we got here, run() exited — the assertion is "didn't hang".
    assert runner.shutdown.is_set()


# ── Dry-run ────────────────────────────────────────────────────────────────


async def test_dry_run_uses_dry_run_report_and_journals_dryrun_order_id(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    # Replace FakeRouter with the real _DryRunRouter from runner
    from src.bot.runner import _DryRunRouter
    from src.execution.order_router import RouterConfig
    dry_router = _DryRunRouter(RouterConfig(inst_id="BTC-USDT-SWAP"))
    ctx, fakes = make_ctx(router=dry_router)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        trade_id = next(iter(ctx.open_trade_ids.values()))
        rec = await ctx.journal.get_trade(trade_id)
    assert rec is not None
    assert rec.order_id == "DRYRUN"
    assert rec.algo_id == "DRYRUN"
