"""Phase 1.5 Commit 8 — `--derivatives-only` + `--duration` runtime modes.

Covers:
  * derivatives-only bypasses `_run_one_symbol` but still drains closes.
  * `duration_seconds` stops the loop after the deadline without a tick limit.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.bot.runner import BotRunner
from tests.conftest import make_config


async def test_derivatives_only_skips_symbol_loop(monkeypatch, make_ctx):
    """With derivatives_only=True, `_run_one_symbol` must not be called.
    Close-poll should still fire so live positions resolve."""
    cfg = make_config(symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    ctx, fakes = make_ctx(config=cfg)

    calls: list[str] = []

    async def _trap(self, symbol: str) -> None:
        calls.append(symbol)

    monkeypatch.setattr(BotRunner, "_run_one_symbol", _trap)
    runner = BotRunner(ctx, derivatives_only=True)
    async with ctx.journal:
        await runner.run_once()

    assert calls == []                           # entry pipeline bypassed
    assert fakes.monitor.poll_count == 1         # close drain still ran


async def test_duration_stops_loop(monkeypatch, make_ctx):
    """`duration_seconds=0` should cause the outer loop to exit on the first
    iteration after the initial tick (deadline has passed)."""
    cfg = make_config(symbols=["BTC-USDT-SWAP"],
                      symbol_settle_seconds=0.0,
                      tf_settle_seconds=0.0,
                      pine_settle_max_wait_s=0.05,
                      pine_settle_poll_interval_s=0.01)
    ctx, fakes = make_ctx(config=cfg)

    # Stub out _run_one_symbol so we don't care about bridges.
    async def _noop(self, symbol: str) -> None:
        return None
    monkeypatch.setattr(BotRunner, "_run_one_symbol", _noop)

    runner = BotRunner(ctx, derivatives_only=True, duration_seconds=0)
    t0 = time.monotonic()
    await runner.run()
    elapsed = time.monotonic() - t0

    # duration_seconds=0 → second iteration's remaining is <=0 → stop.
    assert elapsed < 2.0
    assert runner.shutdown.is_set()
