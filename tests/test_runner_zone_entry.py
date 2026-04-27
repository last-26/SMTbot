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
        self.cancel_pending_calls: list[tuple] = []

    def register_pending(self, *, inst_id, pos_side, order_id,
                         num_contracts, entry_px, max_wait_s, placed_at=None):
        self.pending_registered.append(
            (inst_id, pos_side, order_id, num_contracts, entry_px, max_wait_s)
        )

    def poll_pending(self) -> list[PendingEvent]:
        out = self.pending_events
        self.pending_events = []
        return out

    def cancel_pending(self, inst_id, pos_side, *, reason="manual"):
        """Mirror PositionMonitor.cancel_pending: returns CANCELED PendingEvent."""
        self.cancel_pending_calls.append((inst_id, pos_side, reason))
        return PendingEvent(
            inst_id=inst_id, pos_side=pos_side, order_id="LIM-X",
            event_type="CANCELED", reason=reason,
        )


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


# ── 2026-04-22: pending hard-gate invalidation (early-cancel) ──────────────


async def test_pending_hard_gate_invalidation_cancels_and_journals(
    monkeypatch, make_ctx,
):
    """When the runner's per-symbol cycle detects a pending limit AND a
    hard gate would now reject a NEW entry of the same direction, the
    pending must be cancelled + the journal must record a
    `pending_hard_gate_invalidated` rejected_signals row."""
    # Stub plan_builder to None so the cancel path is observed in isolation
    # (otherwise the symbol would immediately try to PLACE a new pending
    # after the cancel, occupying the same slot key).
    _patch_plan_builder(monkeypatch, None)
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    # Seed a pending long limit.
    pending_plan = make_plan()
    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=pending_plan, zone=_ZONE, order_id="LIM-OLD",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )

    # Force the helper to flag invalidation regardless of state details.
    def _stub_eval(**_kw):
        return "vwap_misaligned"
    monkeypatch.setattr(
        "src.bot.runner.evaluate_pending_invalidation_gates", _stub_eval,
    )

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        rows = await ctx.journal.list_rejected_signals()

    # Cancel call dispatched to monitor with our prefixed reason.
    assert len(monitor.cancel_pending_calls) == 1
    inst_id, pos_side, reason = monitor.cancel_pending_calls[0]
    assert inst_id == "BTC-USDT-SWAP"
    assert pos_side == "long"
    assert reason.startswith("hard_gate:")
    assert "vwap_misaligned" in reason
    # Pending slot cleared (handler ran).
    assert ("BTC-USDT-SWAP", "long") not in ctx.pending_setups
    # Journal row written with the new specific reject reason.
    reasons = [r.reject_reason for r in rows]
    assert "pending_hard_gate_invalidated" in reasons


async def test_pending_kept_alive_when_no_hard_gate_fires(monkeypatch, make_ctx):
    """When the helper returns None (all gates pass), the pending must be
    left untouched and the runner short-circuits as before — no cancel,
    no rejected_signals row from this path."""
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    ctx.pending_setups[("BTC-USDT-SWAP", "long")] = PendingSetupMeta(
        plan=make_plan(), zone=_ZONE, order_id="LIM-OLD",
        signal_state=fakes.reader.state, placed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "src.bot.runner.evaluate_pending_invalidation_gates",
        lambda **_kw: None,
    )

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
        rows = await ctx.journal.list_rejected_signals()

    # No cancel attempted.
    assert monitor.cancel_pending_calls == []
    # Pending slot still occupied — short-circuited normally.
    assert ("BTC-USDT-SWAP", "long") in ctx.pending_setups
    # No `pending_hard_gate_invalidated` row written.
    assert "pending_hard_gate_invalidated" not in [r.reject_reason for r in rows]


# ── Multi-cycle integration (7.C5) ─────────────────────────────────────────


async def test_integration_full_lifecycle_place_fill_close(monkeypatch, make_ctx):
    """Three cycles: place limit → fill → close.

    Cycle 1: zone builder returns a ZoneSetup → runner places a limit,
    stashes meta, registers pending.
    Cycle 2: zone builder now returns None (irrelevant — dedup blocks
    any new placement); monitor emits a FILLED event → runner attaches
    OCO, registers the open position, records the trade.
    Cycle 3: monitor emits a close fill → runner enriches, records the
    close, the trade is CLOSED in the journal.
    """
    plan = make_plan()
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    # Per-cycle zone-builder return value (controlled by monkeypatch).
    zone_returns = [_ZONE, None, None]

    def _zone_stub(*a, **kw):
        return zone_returns.pop(0) if zone_returns else None
    monkeypatch.setattr("src.bot.runner.build_zone_setup", _zone_stub)
    _patch_plan_builder(monkeypatch, plan)

    runner = BotRunner(ctx)

    async with ctx.journal:
        # Cycle 1: places a limit.
        await runner.run_once()
        assert len(router.limit_calls) == 1
        assert ("BTC-USDT-SWAP", "long") in ctx.pending_setups
        assert router.calls == []  # no market path
        opens_after_c1 = await ctx.journal.list_open_trades()
        assert opens_after_c1 == []  # pending, not yet open

        # Cycle 2: FILLED → promote to OPEN.
        monitor.pending_events = [PendingEvent(
            inst_id="BTC-USDT-SWAP", pos_side="long", order_id=router.limit_order_id,
            event_type="FILLED", reason="fill",
            filled_size=plan.num_contracts, avg_price=plan.entry_price,
        )]
        await runner.run_once()
        assert len(router.attach_calls) == 1
        assert ("BTC-USDT-SWAP", "long") in ctx.open_trade_ids
        assert ("BTC-USDT-SWAP", "long") not in ctx.pending_setups
        opens_after_c2 = await ctx.journal.list_open_trades()
        assert len(opens_after_c2) == 1

        # Cycle 3: close fill → CLOSED.
        from tests.conftest import make_close_fill
        monitor.queued_fills = [make_close_fill()]
        await runner.run_once()
        opens_after_c3 = await ctx.journal.list_open_trades()
        assert opens_after_c3 == []  # trade now closed


async def test_integration_pending_persists_across_cycles(monkeypatch, make_ctx):
    """Cycle 1 places; cycle 2 sees no events → nothing new placed;
    cycle 3 receives a timeout event → rejected_signal written.

    Zone builder returns a zone on cycle 1 only — cycles 2 & 3 return
    None so that after the cycle-3 cancellation clears `pending_setups`,
    the symbol loop does NOT immediately stash a fresh pending.
    """
    plan = make_plan()
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    _patch_plan_builder(monkeypatch, plan)
    zone_returns = [_ZONE, None, None]

    def _zone_stub(*a, **kw):
        return zone_returns.pop(0) if zone_returns else None
    monkeypatch.setattr("src.bot.runner.build_zone_setup", _zone_stub)

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()                    # cycle 1: place
        assert len(router.limit_calls) == 1

        await runner.run_once()                    # cycle 2: dedup blocks
        assert len(router.limit_calls) == 1, "dedup should prevent a 2nd limit"

        monitor.pending_events = [PendingEvent(   # cycle 3: timeout
            inst_id="BTC-USDT-SWAP", pos_side="long",
            order_id=router.limit_order_id,
            event_type="CANCELED", reason="timeout",
        )]
        await runner.run_once()
        rows = await ctx.journal.list_rejected_signals()

    reasons = [r.reject_reason for r in rows]
    assert "zone_timeout_cancel" in reasons
    assert ("BTC-USDT-SWAP", "long") not in ctx.pending_setups


async def test_integration_post_only_rejection_at_router_is_logged(
    monkeypatch, make_ctx,
):
    """Router raises OrderRejected (after its own post-only fallback also
    failed); the runner must swallow the error, NOT register a pending,
    and not fall through to a market order when `zone_require_setup=True`."""
    from src.execution.errors import OrderRejected

    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg, require_setup=True)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    router.limit_raises = OrderRejected(
        "sCode=51124 post-only would cross", code="51124",
    )
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    # Limit was attempted, but no pending stashed, no open trade opened,
    # no fallback market order.
    assert len(router.limit_calls) == 1
    assert router.calls == []
    assert ctx.pending_setups == {}
    assert ctx.open_trade_ids == {}
    assert monitor.pending_registered == []


async def test_integration_risk_gate_blocks_zoned_plan(monkeypatch, make_ctx):
    """The zone re-sizes the plan to match the structural SL, which
    widens R slightly. Risk manager is re-consulted on that zoned plan;
    if it blocks, no limit is placed and no pending is stashed.

    Set `zone_require_setup=True` so the runner doesn't silently fall
    through to a market order after the zone path refuses the plan.
    """
    _patch_plan_builder(monkeypatch, make_plan())
    _patch_zone_builder(monkeypatch, _ZONE)
    cfg = make_config(contract_size=0.01)
    _enable_zone(cfg, require_setup=True)
    monitor = ZoneFakeMonitor()
    router = ZoneFakeRouter()
    ctx, fakes = make_ctx(config=cfg, monitor=monitor, router=router)
    ctx.contract_sizes = {"BTC-USDT-SWAP": 0.01}

    # Force the can_trade call inside _try_place_zone_entry (on the
    # zoned plan) to return False while the pre-zone gate still passes.
    call_count = {"n": 0}
    original = ctx.risk_mgr.can_trade

    def _blocker(plan_arg):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(plan_arg)   # pre-zone gate passes
        return (False, "test_blocks_zoned_plan")  # zone's re-check blocks
    ctx.risk_mgr.can_trade = _blocker  # type: ignore[assignment]

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert router.limit_calls == []
    assert monitor.pending_registered == []
    assert ctx.pending_setups == {}
    # With `zone_require_setup=True`, the market fallback is suppressed.
    assert router.calls == []
