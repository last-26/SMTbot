"""--clear-halt CLI flag.

Locks in two behaviors:
  * Without the flag, _prime() leaves a replayed halt state intact so the
    next can_trade() returns False with the original reason.
  * With the flag, _prime() wipes halt + daily_realized_pnl + consecutive_losses
    so can_trade() returns True immediately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.bot.runner import BotRunner
from src.strategy.risk_manager import RiskManager


def _seed_halt(rm: RiskManager) -> None:
    """Simulate post-replay state: cooldown halt + deeply red day + streak +
    peak above current (max_drawdown over the limit)."""
    rm.halted_until = datetime.now(tz=timezone.utc) + timedelta(hours=12)
    rm.halt_reason = "daily_loss=17.90%"
    rm.daily_realized_pnl = -750.0
    rm.day_start_balance = 4000.0      # → daily_loss_pct = 18.75%
    rm.consecutive_losses = 6
    rm.peak_balance = 4525.0           # current ≈ 4255 → drawdown ≈ 5.97%
    rm.current_balance = 3349.0        # against peak 4525 → drawdown ≈ 26.0%


async def test_prime_without_clear_halt_keeps_halt(make_ctx, monkeypatch):
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx, clear_halt=False)
    monkeypatch.setattr(runner, "_rehydrate_open_positions",
                        lambda: _noop())
    monkeypatch.setattr(runner, "_reconcile_orphans", lambda: _noop())
    monkeypatch.setattr(runner, "_load_contract_sizes", lambda: _noop())

    async with ctx.journal:
        _seed_halt(ctx.risk_mgr)
        await runner._prime()

    allowed, reason = ctx.risk_mgr.can_trade()
    assert allowed is False
    # Drawdown OR halt OR daily-loss/streak gate fires; the bot is blocked.
    assert (
        "max_drawdown" in reason
        or ctx.risk_mgr.halted_until is not None
        or "daily_loss" in reason
    )
    assert ctx.risk_mgr.consecutive_losses == 6
    assert ctx.risk_mgr.peak_balance > ctx.risk_mgr.current_balance


async def test_prime_with_clear_halt_resumes_trading(make_ctx, monkeypatch):
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx, clear_halt=True)
    monkeypatch.setattr(runner, "_rehydrate_open_positions",
                        lambda: _noop())
    monkeypatch.setattr(runner, "_reconcile_orphans", lambda: _noop())
    monkeypatch.setattr(runner, "_load_contract_sizes", lambda: _noop())

    async with ctx.journal:
        _seed_halt(ctx.risk_mgr)
        await runner._prime()

    rm = ctx.risk_mgr
    assert rm.halted_until is None
    assert rm.halt_reason == ""
    assert rm.daily_realized_pnl == 0.0
    assert rm.consecutive_losses == 0
    # day_start_balance + peak_balance both re-anchor to current_balance so
    # daily_loss_pct=0 AND drawdown_pct=0 going forward.
    assert rm.day_start_balance == pytest.approx(rm.current_balance)
    assert rm.peak_balance == pytest.approx(rm.current_balance)
    assert rm.drawdown_pct == 0.0
    allowed, reason = rm.can_trade()
    assert allowed is True
    assert reason == ""


async def _noop():
    return None
