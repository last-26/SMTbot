"""Phase 7.B4 — HTF 15m MarketState cache lifecycle.

The zone-entry planner (coming in 7.C1) sources HTF FVGs / OBs without
paying for a second TF switch. The runner populates `ctx.htf_state_cache`
during the existing HTF pass while the chart is on the HTF timeframe,
clears it on already-open skips (stale state must never feed a later
planner run), and clears it on read failures.
"""

from __future__ import annotations

import pytest

from src.bot.runner import BotRunner
from src.data.models import Direction, MarketState, SignalTableData
from tests.conftest import FakeReader, make_config


pytestmark = pytest.mark.anyio


class _RecordingBridge:
    async def set_symbol(self, sym: str):
        return {"success": True}

    async def set_timeframe(self, tf: str):
        return {"success": True}


def _patch_plan_builder_reject(monkeypatch):
    """Short-circuit the planner — these tests only care about the HTF pass."""
    def _stub(*a, **kw):
        return None, "below_confluence"
    monkeypatch.setattr("src.bot.runner.build_trade_plan_with_reason", _stub)


def _make_htf_state() -> MarketState:
    """A MarketState distinguishable as the HTF snapshot via price."""
    return MarketState(
        symbol="BTC-USDT-SWAP",
        timeframe="15",
        signal_table=SignalTableData(
            price=67_500.0,       # sentinel — picks this read apart from others
            atr_14=180.0,
            trend_htf=Direction.BULLISH,
        ),
    )


async def test_htf_state_cache_populated_after_htf_pass(monkeypatch, make_ctx):
    _patch_plan_builder_reject(monkeypatch)
    cfg = make_config(
        symbols=["BTC-USDT-SWAP"], symbol_settle_seconds=0.0,
        tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
        pine_settle_poll_interval_s=0.01,
    )
    ctx, _ = make_ctx(config=cfg, reader=FakeReader(_make_htf_state()))
    ctx.bridge = _RecordingBridge()
    runner = BotRunner(ctx)

    async with ctx.journal:
        await runner.run_once()

    cached = ctx.htf_state_cache.get("BTC-USDT-SWAP")
    assert cached is not None
    assert cached.current_price == 67_500.0
    assert cached.trend_htf == Direction.BULLISH


async def test_htf_state_cache_cleared_for_already_open_symbol(
    monkeypatch, make_ctx
):
    """Symbols with an open position skip the HTF pass entirely. Any prior
    cache entry must be evicted — else a later planner run (after the
    position closes) would read stale HTF data from the wrong bar."""
    _patch_plan_builder_reject(monkeypatch)
    cfg = make_config(
        symbols=["BTC-USDT-SWAP"], symbol_settle_seconds=0.0,
        tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
        pine_settle_poll_interval_s=0.01,
    )
    ctx, _ = make_ctx(config=cfg, reader=FakeReader(_make_htf_state()))
    ctx.bridge = _RecordingBridge()

    # Seed a stale cache entry + mark the symbol as already-open.
    ctx.htf_state_cache["BTC-USDT-SWAP"] = _make_htf_state()
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = "trade-id-xyz"

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert "BTC-USDT-SWAP" not in ctx.htf_state_cache


async def test_htf_state_cache_cleared_on_read_failure(monkeypatch, make_ctx):
    """Reader raises during the HTF state snapshot — cache entry is popped
    so downstream consumers see None instead of a stale prior snapshot."""
    _patch_plan_builder_reject(monkeypatch)
    cfg = make_config(
        symbols=["BTC-USDT-SWAP"], symbol_settle_seconds=0.0,
        tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
        pine_settle_poll_interval_s=0.01,
    )

    class _FlakyReader(FakeReader):
        """First two reads (_switch_timeframe baseline peeks) succeed; the
        HTF-state snapshot after SR detection raises."""
        def __init__(self):
            super().__init__(_make_htf_state())
            self.reads = 0

        async def read_market_state(self):
            self.reads += 1
            if self.reads >= 2:
                raise RuntimeError("pine read blew up")
            return self.state

    ctx, _ = make_ctx(config=cfg, reader=_FlakyReader())
    ctx.bridge = _RecordingBridge()
    ctx.htf_state_cache["BTC-USDT-SWAP"] = _make_htf_state()

    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert "BTC-USDT-SWAP" not in ctx.htf_state_cache
