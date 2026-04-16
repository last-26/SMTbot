"""Tests for src.strategy.risk_manager — circuit breakers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data.models import Direction
from src.strategy.risk_manager import (
    CircuitBreakerConfig,
    RiskManager,
    TradeResult,
)
from src.strategy.trade_plan import TradePlan


def _dt(h: int = 12, day: int = 1) -> datetime:
    return datetime(2026, 4, day, h, 0, tzinfo=timezone.utc)


def _cfg(**kwargs) -> CircuitBreakerConfig:
    defaults = dict(
        max_daily_loss_pct=3.0,
        max_consecutive_losses=5,
        max_drawdown_pct=10.0,
        max_concurrent_positions=2,
        max_leverage=20,
        min_rr_ratio=2.0,
        cooldown_hours=24,
    )
    defaults.update(kwargs)
    return CircuitBreakerConfig(**defaults)


def _plan(
    leverage: int = 5, rr: float = 3.0, contracts: int = 10,
) -> TradePlan:
    return TradePlan(
        direction=Direction.BULLISH,
        entry_price=100.0, sl_price=99.0, tp_price=103.0,
        rr_ratio=rr, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=1000.0,
        leverage=leverage,
        required_leverage=float(leverage),
        num_contracts=contracts,
        risk_amount_usdt=10.0,
        max_risk_usdt=10.0,
        capped=False,
    )


# ── Initial state ───────────────────────────────────────────────────────────


def test_fresh_manager_allows_trading():
    rm = RiskManager(starting_balance=1000.0, now=_dt())
    ok, reason = rm.can_trade(_plan(), now=_dt())
    assert ok is True
    assert reason == ""


def test_rejects_invalid_starting_balance():
    with pytest.raises(ValueError):
        RiskManager(starting_balance=0.0)


# ── Plan-level checks ───────────────────────────────────────────────────────


def test_rejects_plan_over_max_leverage():
    rm = RiskManager(1000.0, config=_cfg(max_leverage=10), now=_dt())
    ok, reason = rm.can_trade(_plan(leverage=15), now=_dt())
    assert ok is False
    assert "leverage" in reason


def test_rejects_plan_below_min_rr():
    rm = RiskManager(1000.0, config=_cfg(min_rr_ratio=2.0), now=_dt())
    ok, reason = rm.can_trade(_plan(rr=1.5), now=_dt())
    assert ok is False
    assert "rr_ratio" in reason


def test_rejects_plan_with_zero_contracts():
    rm = RiskManager(1000.0, now=_dt())
    ok, reason = rm.can_trade(_plan(contracts=0), now=_dt())
    assert ok is False
    assert "num_contracts" in reason


# ── Concurrent positions ────────────────────────────────────────────────────


def test_concurrent_position_cap_blocks_new_trade():
    rm = RiskManager(1000.0, config=_cfg(max_concurrent_positions=1), now=_dt())
    rm.register_trade_opened()
    ok, reason = rm.can_trade(_plan(), now=_dt())
    assert ok is False
    assert "open_positions" in reason


def test_trade_closed_decrements_open_positions():
    rm = RiskManager(1000.0, config=_cfg(max_concurrent_positions=1), now=_dt())
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=10.0, pnl_r=1.0, timestamp=_dt()))
    ok, _ = rm.can_trade(_plan(), now=_dt())
    assert ok is True


# ── Daily loss halt ─────────────────────────────────────────────────────────


def test_daily_loss_halts_trading():
    rm = RiskManager(1000.0, config=_cfg(max_daily_loss_pct=3.0), now=_dt())
    # Lose 4% of day_start in one trade
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=-40.0, pnl_r=-1.0, timestamp=_dt()))
    ok, reason = rm.can_trade(_plan(), now=_dt(13))
    assert ok is False
    assert "halted" in reason or "daily_loss" in reason


def test_daily_loss_resets_on_new_day():
    rm = RiskManager(1000.0, config=_cfg(max_daily_loss_pct=3.0, cooldown_hours=1),
                     now=_dt(23, day=1))
    rm.register_trade_opened()
    rm.register_trade_closed(
        TradeResult(pnl_usdt=-40.0, pnl_r=-1.0, timestamp=_dt(23, day=1))
    )
    # Next day, past cooldown
    ok, _ = rm.can_trade(_plan(), now=_dt(12, day=2))
    assert ok is True


# ── Consecutive loss halt ───────────────────────────────────────────────────


def test_consecutive_losses_halt_trading():
    rm = RiskManager(10_000.0, config=_cfg(max_consecutive_losses=3), now=_dt())
    for _ in range(3):
        rm.register_trade_opened()
        rm.register_trade_closed(TradeResult(pnl_usdt=-5.0, pnl_r=-1.0, timestamp=_dt()))
    ok, reason = rm.can_trade(_plan(), now=_dt())
    assert ok is False
    assert "consecutive_losses" in reason or "halted" in reason


def test_win_resets_consecutive_losses():
    rm = RiskManager(10_000.0, config=_cfg(max_consecutive_losses=3), now=_dt())
    for _ in range(2):
        rm.register_trade_opened()
        rm.register_trade_closed(TradeResult(pnl_usdt=-5.0, pnl_r=-1.0, timestamp=_dt()))
    assert rm.consecutive_losses == 2
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=10.0, pnl_r=2.0, timestamp=_dt()))
    assert rm.consecutive_losses == 0


def test_breakeven_leaves_consecutive_unchanged():
    rm = RiskManager(10_000.0, now=_dt())
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=-5.0, pnl_r=-1.0, timestamp=_dt()))
    assert rm.consecutive_losses == 1
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=0.0, pnl_r=0.0, timestamp=_dt()))
    assert rm.consecutive_losses == 1


# ── Drawdown halt ───────────────────────────────────────────────────────────


def test_drawdown_halts_permanently():
    rm = RiskManager(10_000.0, config=_cfg(max_drawdown_pct=10.0), now=_dt())
    # First win raises peak
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=1_000.0, pnl_r=2.0, timestamp=_dt()))
    assert rm.peak_balance == 11_000.0
    # Big loss drops balance ≥ 10% below peak
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=-2_000.0, pnl_r=-4.0, timestamp=_dt()))
    # 9_000 / 11_000 = 18.2% drawdown
    assert rm.drawdown_pct > 10
    ok, reason = rm.can_trade(_plan(), now=_dt())
    assert ok is False
    assert "drawdown" in reason


# ── Halt lifecycle ──────────────────────────────────────────────────────────


def test_force_halt_blocks_until_cleared():
    rm = RiskManager(10_000.0, now=_dt())
    rm.force_halt("news blackout", hours=1, now=_dt())
    ok, reason = rm.can_trade(now=_dt())
    assert ok is False
    assert "news" in reason


def test_halt_clears_after_cooldown():
    rm = RiskManager(10_000.0, now=_dt())
    rm.force_halt("test", hours=1, now=_dt())
    assert rm.can_trade(now=_dt())[0] is False
    # 2 hours later
    ok, _ = rm.can_trade(now=_dt() + timedelta(hours=2))
    assert ok is True


def test_clear_halt_manual_override():
    rm = RiskManager(10_000.0, now=_dt())
    rm.force_halt("manual", hours=24, now=_dt())
    rm.clear_halt()
    ok, _ = rm.can_trade(_plan(), now=_dt())
    assert ok is True


# ── Balance bookkeeping ─────────────────────────────────────────────────────


def test_peak_tracks_max_balance():
    rm = RiskManager(10_000.0, now=_dt())
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=500.0, pnl_r=1.0, timestamp=_dt()))
    rm.register_trade_opened()
    rm.register_trade_closed(TradeResult(pnl_usdt=-200.0, pnl_r=-0.5, timestamp=_dt()))
    assert rm.peak_balance == 10_500.0
    assert rm.current_balance == 10_300.0
