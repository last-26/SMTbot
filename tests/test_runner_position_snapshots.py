"""Tests for BotRunner._maybe_write_position_snapshots — cadence gate +
disabled-config no-op + journal write contract."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import pytest

from datetime import datetime, timezone

from src.bot.runner import BotRunner
from src.data.models import (
    Direction, MarketState, OscillatorTableData, Session, SignalTableData,
)
from src.execution.models import PositionSnapshot


@dataclass
class _StubTracked:
    """Minimal shape `_maybe_write_position_snapshots` reads from
    `monitor.get_tracked()`. Mirrors the live `_Tracked` fields the
    writer references; defaults match the rehydrate sentinel."""
    entry_price: float = 67_000.0
    plan_sl_price: float = 66_800.0
    sl_price: float = 66_800.0
    tp2_price: Optional[float] = 67_400.0
    be_already_moved: bool = False
    sl_lock_applied: bool = False
    mfe_r_high: float = 0.0
    mae_r_low: float = 0.0


def _snap(inst="BTC-USDT-SWAP", side="long",
          entry=67_000.0, mark=67_300.0) -> PositionSnapshot:
    return PositionSnapshot(
        inst_id=inst, pos_side=side, size=3.0,
        entry_price=entry, mark_price=mark,
        unrealized_pnl=42.0, leverage=10,
    )


def _arm(ctx, fakes, *, mark=67_300.0):
    """Wire one OPEN BTC long with a tracked record + open_trade_ids
    + a queued live snap on the FakeMonitor for next poll()."""
    snap = _snap(mark=mark)
    fakes.monitor.queued_live_snaps = [snap]
    fakes.monitor.tracked_overrides[("BTC-USDT-SWAP", "long")] = _StubTracked()
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-abc"


async def _make_runner(make_ctx, **overrides):
    ctx, fakes = make_ctx(**overrides)
    await ctx.journal.connect()
    return BotRunner(ctx), ctx, fakes


# ── Cadence gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_calls_within_cadence_window_writes_once(make_ctx):
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    _arm(ctx, fakes)

    await runner._maybe_write_position_snapshots(list(fakes.monitor.queued_live_snaps))
    rows_after_first = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows_after_first) == 1

    # Second call within cadence window — should be a no-op.
    fakes.monitor.queued_live_snaps = [_snap(mark=67_400.0)]
    await runner._maybe_write_position_snapshots(list(fakes.monitor.queued_live_snaps))
    rows_after_second = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows_after_second) == 1, "cadence gate should suppress 2nd write"


@pytest.mark.asyncio
async def test_call_after_cadence_elapsed_writes_again(make_ctx):
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    cfg = ctx.config.model_copy(update={
        "journal": ctx.config.journal.model_copy(update={
            "position_snapshot_cadence_s": 60,
        }),
    })
    ctx.config = cfg
    runner = BotRunner(ctx)
    _arm(ctx, fakes)

    await runner._maybe_write_position_snapshots(list(fakes.monitor.queued_live_snaps))
    # Force the cadence clock backward — simulates 61s elapsed.
    ctx.last_position_snapshot_ts = time.monotonic() - 61.0

    fakes.monitor.queued_live_snaps = [_snap(mark=67_400.0)]
    await runner._maybe_write_position_snapshots(list(fakes.monitor.queued_live_snaps))

    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows) == 2


# ── Disabled config ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_config_writes_nothing(make_ctx):
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    cfg = ctx.config.model_copy(update={
        "journal": ctx.config.journal.model_copy(update={
            "position_snapshot_enabled": False,
        }),
    })
    ctx.config = cfg
    runner = BotRunner(ctx)
    _arm(ctx, fakes)

    await runner._maybe_write_position_snapshots(list(fakes.monitor.queued_live_snaps))
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert rows == []


# ── Skip conditions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_live_snaps_writes_nothing(make_ctx):
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    await runner._maybe_write_position_snapshots([])
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert rows == []


@pytest.mark.asyncio
async def test_skips_when_no_tracked_record(make_ctx):
    """live_snap present but get_tracked returns None (e.g. monitor lost
    the row between poll batches) — writer skips silently."""
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    snap = _snap()
    fakes.monitor.queued_live_snaps = [snap]
    # No tracked_overrides, no open_trade_ids — nothing to write.
    await runner._maybe_write_position_snapshots([snap])
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert rows == []


@pytest.mark.asyncio
async def test_skips_when_plan_sl_price_unset(make_ctx):
    """Rehydrated positions have plan_sl_price=0.0 — the running R math
    is undefined, so we skip rather than stamp a meaningless 0R row."""
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    snap = _snap()
    fakes.monitor.queued_live_snaps = [snap]
    fakes.monitor.tracked_overrides[("BTC-USDT-SWAP", "long")] = _StubTracked(
        plan_sl_price=0.0,
    )
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-abc"
    await runner._maybe_write_position_snapshots([snap])
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert rows == []


@pytest.mark.asyncio
async def test_writes_vwap_3m_distance_atr_from_centerline(make_ctx):
    """2026-04-27 (F4) — vwap_3m_distance_atr_now must be computed from
    `signal_table.vwap_3m` (centerline) when present, since the ±1σ band
    fields go NULL right after Pine's UTC daily VWAP reset and were
    leaving 713/713 NULL pre-fix. Lock the formula:
    `(mark - vwap_3m) / atr` and assert non-NULL on a populated cache.
    """
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    snap = _snap(mark=67_300.0)
    fakes.monitor.queued_live_snaps = [snap]
    fakes.monitor.tracked_overrides[("BTC-USDT-SWAP", "long")] = _StubTracked()
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-abc"

    # Populate the per-symbol cache with a state that has VWAP centerline
    # but NO band (band collapse — primary failure mode this fix targets).
    ctx.last_market_state_per_symbol["BTC-USDT-SWAP"] = MarketState(
        symbol="BTC-USDT-SWAP",
        timeframe="3m",
        timestamp=datetime(2026, 4, 27, 1, tzinfo=timezone.utc),
        signal_table=SignalTableData(
            price=67_300.0,
            atr_14=120.0,
            session=Session.LONDON,
            trend_htf=Direction.BULLISH,
            vwap_3m=66_900.0,
            vwap_3m_upper=0.0,  # band fields collapsed (post-reset)
            vwap_3m_lower=0.0,
        ),
        oscillator=OscillatorTableData(),
    )

    await runner._maybe_write_position_snapshots([snap])
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows) == 1
    # (67_300 - 66_900) / 120 = 3.333...
    assert rows[0].vwap_3m_distance_atr_now == pytest.approx(400.0 / 120.0)


@pytest.mark.asyncio
async def test_vwap_distance_falls_back_to_band_mid_when_centerline_zero(make_ctx):
    """Edge case: vwap_3m centerline missing but ±1σ bands populated —
    derive band_mid as fallback so we still emit a non-NULL distance."""
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    snap = _snap(mark=67_300.0)
    fakes.monitor.queued_live_snaps = [snap]
    fakes.monitor.tracked_overrides[("BTC-USDT-SWAP", "long")] = _StubTracked()
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-abc"

    ctx.last_market_state_per_symbol["BTC-USDT-SWAP"] = MarketState(
        symbol="BTC-USDT-SWAP",
        timeframe="3m",
        timestamp=datetime(2026, 4, 27, 1, tzinfo=timezone.utc),
        signal_table=SignalTableData(
            price=67_300.0,
            atr_14=120.0,
            session=Session.LONDON,
            trend_htf=Direction.BULLISH,
            vwap_3m=0.0,           # centerline missing
            vwap_3m_upper=67_000.0,
            vwap_3m_lower=66_800.0,
        ),
        oscillator=OscillatorTableData(),
    )

    await runner._maybe_write_position_snapshots([snap])
    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows) == 1
    # band_mid = (67_000 + 66_800) / 2 = 66_900
    # (67_300 - 66_900) / 120 = 3.333...
    assert rows[0].vwap_3m_distance_atr_now == pytest.approx(400.0 / 120.0)


@pytest.mark.asyncio
async def test_writes_capture_mfe_mae_and_unrealized_r(make_ctx):
    """End-to-end contract: a snap with mark=67_300 (favorable +1.5R for
    SL-distance 200) + tracked.mfe_r_high=1.5 → row stores the values."""
    ctx, fakes = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    snap = _snap(mark=67_300.0)
    fakes.monitor.queued_live_snaps = [snap]
    fakes.monitor.tracked_overrides[("BTC-USDT-SWAP", "long")] = _StubTracked(
        entry_price=67_000.0,
        plan_sl_price=66_800.0,  # SL distance 200
        mfe_r_high=1.5,
        mae_r_low=-0.3,
        be_already_moved=True,
    )
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-abc"
    await runner._maybe_write_position_snapshots([snap])

    rows = await ctx.journal.get_position_snapshots("trade-abc")
    assert len(rows) == 1
    row = rows[0]
    assert row.mark_price == 67_300.0
    assert row.unrealized_pnl_usdt == 42.0
    assert row.unrealized_pnl_r == pytest.approx(1.5)
    assert row.mfe_r_so_far == 1.5
    assert row.mae_r_so_far == -0.3
    assert row.sl_to_be_moved is True
    assert row.mfe_lock_applied is False
