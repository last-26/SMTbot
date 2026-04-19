"""Phase 7.C4 — BotRunner zone-based entry lifecycle.

Exercises the runner's integration with:
  * `setup_planner.build_zone_setup` / `apply_zone_to_plan`
  * `OrderRouter.place_limit_entry` / `attach_algos` / `cancel_pending_entry`
  * `PositionMonitor.register_pending` / `poll_pending`

The runner's job is the orchestration: build a zone, adjust the plan,
place a limit, stash metadata, and on the next cycle's `poll_pending`
event, transition the fill into a live position (OCO attached) or log
a rejected_signal on cancellation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from src.bot.runner import BotRunner, PendingSetupMeta
from src.data.models import Direction
from src.execution.models import (
    AlgoResult,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.execution.position_monitor import PendingEvent
from src.strategy.setup_planner import ZoneSetup
from tests.conftest import FakeMonitor, FakeRouter, make_config, make_plan


UTC = timezone.utc


# ── Extended fakes (zone-entry specific) ────────────────────────────────────


class ZoneFakeRouter(FakeRouter):
    """FakeRouter + `place_limit_entry` + `attach_algos` + `cancel_pending_entry`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limit_calls: list[tuple] = []
        self.attach_calls: list[tuple] = []
        self.cancel_calls: list[tuple] = []
        self.limit_raises: Optional[Exception] = None
        self.attach_raises: Optional[Exception] = None
        self.limit_order_id = "LIM-1"

    def place_limit_entry(self, plan, entry_px, inst_id=None,
                          ord_type="post_only", fallback_to_limit=True):
        self.limit_calls.append((plan, entry_px, inst_id, ord_type))
        if self.limit_raises is not None:
            raise self.limit_raises
        return OrderResult(
            order_id=self.limit_order_id,
            client_order_id="cli-lim",
            status=OrderStatus.PENDING,
        )

    def attach_algos(self, plan, inst_id=None):
        self.attach_calls.append((plan, inst_id))
        if self.attach_raises is not None:
            raise self.attach_raises
        return [AlgoResult(
            algo_id="ALG-ATTACHED", client_algo_id="cli-alg",
            sl_trigger_px=plan.sl_price, tp_trigger_px=plan.tp_price,
        )]

    def cancel_pending_entry(self, order_id, inst_id=None):
        self.cancel_calls.append((order_id, inst_id))
        return {}


class ZoneFakeMonitor(FakeMonitor):
    """FakeMonitor + `register_pending` + `poll_pending` + pending event queue."""

    def __init__(self):
        super().__init__()
        self.pending_registered: list[tuple] = []
        self.pending_events: list[PendingEvent] = []

    def register_pending(self, *, inst_id, pos_side, order_id,
                         num_contracts, entry_px, max_wait_s, placed_at=None):
        self.pending_registered.append(
            (inst_id, pos_side, order_id, num_contracts, entry_px, max_wait_s)
        )

    def poll_pending(self) -> list[PendingEvent]:
        out = self.pending_events
        self.pending_events = []
        return out


_ZONE = ZoneSetup(
    direction=Direction.BULLISH,
    entry_zone=(66_800.0, 66_900.0),
    trigger_type="zone_touch",
    sl_beyond_zone=66_400.0,
    tp_primary=68_200.0,
    max_wait_bars=10,
    zone_source="liq_pool",
)


def _enable_zone(cfg, *, require_setup: bool = False):
    """Mutate an ExecutionConfig in place to turn zone-entry on."""
    cfg.execution.zone_entry_enabled = True
    cfg.execution.zone_require_setup = require_setup
    cfg.execution.zone_max_wait_bars = 10


def _patch_plan_builder(monkeypatch, plan):
    reason = "" if plan is not None else "below_confluence"

    def _stub(*a, **kw):
        return plan, reason
    monkeypatch.setattr("src.bot.runner.build_trade_plan_with_reason", _stub)


def _patch_zone_builder(monkeypatch, zone: Optional[ZoneSetup]):
    def _stub(*a, **kw):
        return zone
    monkeypatch.setattr("src.bot.runner.build_zone_setup", _stub)


# ── _try_place_zone_entry ──────────────────────────────────────────────────


async def test_zone_entry_places_limit_and_stashes_meta(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}
    runner = BotRunner(ctx)

    async with ctx.journal:
        await runner.run_once()

    # Limit order was placed — market router.place was NOT called.
    assert len(router.limit_calls) == 1
    assert router.calls == []                       # legacy .place() not called
    # Monitor has a pending row.
    assert len(monitor.pending_registered) == 1
    inst, side, ord_id, _, _, _ = monitor.pending_registered[0]
    assert inst == "BTC-USDT-SWAP"
    assert side == "long"
    assert ord_id == "LIM-1"
    # Runner stashed the metadata for the fill event.
    assert ("BTC-USDT-SWAP", "long") in ctx.pending_setups
    meta = ctx.pending_setups[("BTC-USDT-SWAP", "long")]
    assert meta.zone.zone_source == "liq_pool"


async def test_no_zone_falls_back_to_market_when_not_required(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg, require_setup=False)
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}
    runner = BotRunner(ctx)

    async with ctx.journal:
        await runner.run_once()

    # No limit placed, but market fallback fired.
    assert router.limit_calls == []
    assert len(router.calls) == 1


async def test_no_zone_is_rejected_when_required(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg, require_setup=True)
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}
    runner = BotRunner(ctx)

    async with ctx.journal:
        await runner.run_once()
        assert router.limit_calls == []
        assert router.calls == []     # NO market fallback either
        rows = await ctx.journal.list_rejected_signals()
    reasons = [r.reject_reason for r in rows]
    assert "no_setup_zone" in reasons


async def test_zone_entry_dedup_skips_symbol_with_pending(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}
    # Seed a pending entry — the cycle should short-circuit before zone build.
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=make_plan(), zone=_ZONE, order_id="LIM-0",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    runner = BotRunner(ctx)

    async with ctx.journal:
        await runner.run_once()

    # No new limit placed because dedup blocked us.
    assert router.limit_calls == []


# ── _process_pending: FILLED ───────────────────────────────────────────────


async def test_process_pending_fill_promotes_to_open(monkeypatch, make_ctx):
    # We skip the entry-scan path by stubbing the plan builder to None;
    # poll_pending still fires ahead of that.
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)

    plan = make_plan()
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=plan, zone=_ZONE, order_id="LIM-1",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    monitor.pending_events = [PendingEvent(
        inst_id="BTC-USDT-SWAP", pos_side="long", order_id="LIM-1",
        event_type="FILLED", reason="fill",
        filled_size=plan.num_contracts, avg_price=plan.entry_price,
    )]

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        opens = await ctx.journal.list_open_trades()

    # OCO was attached.
    assert len(router.attach_calls) == 1
    # Monitor's tracked list has a new open row.
    assert len(monitor.registered) == 1
    inst, side, size, entry_px = monitor.registered[0]
    assert inst == "BTC-USDT-SWAP"
    assert side == "long"
    assert size == plan.num_contracts
    # Journal has the OPEN trade.
    assert len(opens) == 1
    # Pending slot cleared.
    assert ("BTC-USDT-SWAP", "long") not in ctx.pending_setups
    # Risk manager registered the open trade.
    assert fakes.risk_mgr.open_positions == 1


async def test_process_pending_partial_fill_resizes_plan(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)

    plan = make_plan(num_contracts=5)
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=plan, zone=_ZONE, order_id="LIM-1",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    monitor.pending_events = [PendingEvent(
        inst_id="BTC-USDT-SWAP", pos_side="long", order_id="LIM-1",
        event_type="FILLED", reason="timeout_partial_fill",
        filled_size=2.0, avg_price=plan.entry_price,
    )]

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    # Attach was called with the resized plan (num_contracts=2).
    attached_plan = router.attach_calls[0][0]
    assert attached_plan.num_contracts == 2
    _, _, size, _ = monitor.registered[0]
    assert size == 2.0


async def test_process_pending_fill_without_meta_is_tolerated(monkeypatch, make_ctx):
    """Defensive: a FILLED event arrives but pending_setups was already popped
    (e.g. handler raced with a manual cancel). The runner logs and moves on."""
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)

    monitor.pending_events = [PendingEvent(
        inst_id="BTC-USDT-SWAP", pos_side="long", order_id="GHOST",
        event_type="FILLED", reason="fill", filled_size=5.0, avg_price=100.0,
    )]
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    # No crash; no attach; no register_open.
    assert router.attach_calls == []
    assert monitor.registered == []


# ── _process_pending: CANCELED ─────────────────────────────────────────────


async def test_process_pending_timeout_clears_slot(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)

    plan = make_plan()
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=plan, zone=_ZONE, order_id="LIM-1",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    monitor.pending_events = [PendingEvent(
        inst_id="BTC-USDT-SWAP", pos_side="long", order_id="LIM-1",
        event_type="CANCELED", reason="timeout",
    )]

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        rows = await ctx.journal.list_rejected_signals()

    assert ("BTC-USDT-SWAP", "long") not in ctx.pending_setups
    # Rejected-signal row persisted with reason "zone_timeout_cancel".
    reasons = [r.reject_reason for r in rows]
    assert "zone_timeout_cancel" in reasons
    # No OPEN trade was recorded, no OCO attached.
    assert router.attach_calls == []
    assert monitor.registered == []


async def test_process_pending_external_cancel_marks_invalidated(
    monkeypatch, make_ctx,
):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor)

    plan = make_plan()
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=plan, zone=_ZONE, order_id="LIM-1",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    monitor.pending_events = [PendingEvent(
        inst_id="BTC-USDT-SWAP", pos_side="long", order_id="LIM-1",
        event_type="CANCELED", reason="external",
    )]

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        rows = await ctx.journal.list_rejected_signals()

    reasons = [r.reject_reason for r in rows]
    assert "pending_invalidated" in reasons
