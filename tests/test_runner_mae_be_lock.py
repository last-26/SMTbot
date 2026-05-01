"""Phase A.6 (2026-05-02) — MAE-triggered BE-lock with LIMIT-based exit.

Operator-described mechanic:
  1. MAE crosses `mae_be_lock_threshold_r` (-0.6R) → arm
  2. Mark recovers within `mae_be_lock_recovery_band_r` (0.1R) of entry
  3. Cycle's LTF direction signal still adverse to position
  4. Place reduce-only post-only LIMIT at:
        LONG : entry × (1 + sl_be_offset_pct)  (sell, micro-profit)
        SHORT: entry × (1 - sl_be_offset_pct)  (buy, micro-profit)
  5. Position-attached SL at -1R stays as backup.

One-shot per position via `mae_be_lock_applied`.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.bot.runner import BotRunner
from src.data.models import Direction
from tests.conftest import FakeMonitor, make_config


class _AtrState:
    def __init__(self, current_price: float, atr: float = 1.0):
        self.current_price = current_price
        self.atr = atr


class _LtfStub:
    """Minimal LTFState stand-in: only `trend` is read by the gate."""

    def __init__(self, trend: Optional[Direction]):
        self.trend = trend


class _MaeMonitor(FakeMonitor):
    def __init__(self, *, regime: Optional[str], entry: float, plan_sl: float,
                 mae_r_low: float = 0.0,
                 mae_be_lock_armed: bool = False,
                 mae_be_lock_applied: bool = False,
                 tp2_price: float = 999.0):
        super().__init__()
        self._runner_view = {
            "entry_price": entry,
            "sl_price": plan_sl,
            "plan_sl_price": plan_sl,
            "tp2_price": tp2_price,
            "runner_size": 5,
            "be_already_moved": False,
            "last_tp_revise_at": None,
            "sl_lock_applied": False,
            "regime_at_entry": regime,
            "last_trail_lock_r": 0.0,
            "mfe_r_high": 0.0,
            "mae_r_low": mae_r_low,
            "mae_be_lock_armed": mae_be_lock_armed,
            "mae_be_lock_applied": mae_be_lock_applied,
        }
        self.mae_arm_calls: list[tuple[str, str]] = []
        self.be_recovery_calls: list[tuple[str, str, float, str]] = []

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        return self._runner_view

    def arm_mae_be_lock(self, inst_id: str, pos_side: str) -> bool:
        self.mae_arm_calls.append((inst_id, pos_side))
        # Reflect arm in the cached view so a same-cycle re-read sees it
        # (real monitor mutates _Tracked which is what get_tracked_runner reads).
        self._runner_view["mae_be_lock_armed"] = True
        return True

    def place_be_recovery_limit(self, inst_id: str, pos_side: str,
                                 limit_px: float,
                                 margin_mode: str = "cross") -> Optional[str]:
        self.be_recovery_calls.append((inst_id, pos_side, limit_px, margin_mode))
        self._runner_view["mae_be_lock_applied"] = True
        return f"BE-{inst_id}-{pos_side}"


def _make_runner(*, regime: Optional[str], entry: float, plan_sl: float,
                 mae_r_low: float = 0.0,
                 mae_be_lock_armed: bool = False,
                 mae_be_lock_applied: bool = False,
                 ltf_trend: Optional[Direction] = None,
                 mae_threshold: float = -0.6,
                 recovery_band: float = 0.1,
                 disabled_regimes: Optional[list] = None,
                 sl_be_offset_pct: float = 0.001):
    cfg = make_config()
    cfg.execution.mae_be_lock_enabled = True
    cfg.execution.mae_be_lock_threshold_r = mae_threshold
    cfg.execution.mae_be_lock_recovery_band_r = recovery_band
    cfg.execution.mae_be_lock_disabled_regimes = disabled_regimes or []
    cfg.execution.sl_be_offset_pct = sl_be_offset_pct
    cfg.execution.margin_mode = "cross"

    mon = _MaeMonitor(
        regime=regime, entry=entry, plan_sl=plan_sl,
        mae_r_low=mae_r_low,
        mae_be_lock_armed=mae_be_lock_armed,
        mae_be_lock_applied=mae_be_lock_applied,
    )

    class _Ctx:
        config = cfg
        monitor = mon
        ltf_cache = {"BTC-USDT-SWAP": _LtfStub(ltf_trend)}
    runner = object.__new__(BotRunner)
    runner.ctx = _Ctx()
    return runner, mon


# Stage 1 — arm gate

@pytest.mark.asyncio
async def test_does_not_arm_when_mae_above_threshold():
    """MAE -0.4R, threshold -0.6R → not armed yet (didn't go deep enough)."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_r_low=-0.4,
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.mae_arm_calls == []
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_arms_when_mae_crosses_threshold():
    """MAE -0.7R, threshold -0.6R → arm fires (returns same cycle, no stage 2)."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_r_low=-0.7,
    )
    state = _AtrState(current_price=99.3)  # still adverse
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.mae_arm_calls == [("BTC-USDT-SWAP", "long")]
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_arming_stage_returns_without_firing_stage_2():
    """Operator design: stage 1 + stage 2 in same cycle is forbidden — give
    the system one cycle to react. (Mark could be at recovery already, but
    we force a one-cycle delay before placing the limit.)"""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_r_low=-0.7,  # crosses threshold this cycle
        ltf_trend=Direction.BEARISH,
    )
    state = _AtrState(current_price=100.0)  # already at entry (recovered)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    # Should arm but NOT place limit in same cycle.
    assert len(mon.mae_arm_calls) == 1
    assert mon.be_recovery_calls == []


# Stage 2 — fire gate (already armed)

@pytest.mark.asyncio
async def test_fires_when_armed_and_recovered_and_adverse_long():
    """LONG armed; mark at entry (cur_r=0); LTF says BEARISH → fire.
    limit_px = entry * (1 + sl_be_offset_pct) = 100.0 * 1.001 = 100.1"""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_r_low=-0.7,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BEARISH,
    )
    state = _AtrState(current_price=100.0)  # at entry
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert len(mon.be_recovery_calls) == 1
    inst, side, limit_px, margin = mon.be_recovery_calls[0]
    assert inst == "BTC-USDT-SWAP" and side == "long"
    assert limit_px == pytest.approx(100.1)  # entry + 0.001 * entry
    assert margin == "cross"


@pytest.mark.asyncio
async def test_fires_when_armed_and_recovered_and_adverse_short():
    """SHORT mirror: entry=100, SL=101 (sl_dist=1). limit_px = entry - fee_buffer.
    For short, adverse cycle = LTF BULLISH (mark trying to go up against us)."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=101.0,
        mae_r_low=-0.7,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BULLISH,
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "short", state)
    assert len(mon.be_recovery_calls) == 1
    inst, side, limit_px, _ = mon.be_recovery_calls[0]
    assert side == "short"
    # entry - fee_buffer = 100 - 0.001 * 100 = 99.9
    assert limit_px == pytest.approx(99.9)


@pytest.mark.asyncio
async def test_does_not_fire_when_mark_outside_recovery_band():
    """Armed but mark still 0.3R adverse (band=0.1R) → no fire."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BEARISH,
    )
    state = _AtrState(current_price=99.7)  # cur_r = -0.3R
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_does_not_fire_when_ltf_aligned_with_position():
    """Armed + recovered, but LTF still BULLISH (matches our long) → recovery
    looks legit, don't preempt with BE-lock."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BULLISH,  # aligned with long
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_does_not_fire_when_ltf_neutral():
    """LTF trend is None (neutral / unparsed) → conservative skip."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        ltf_trend=None,
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_does_not_fire_when_already_applied():
    """One-shot guard: applied=True → return immediately."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        mae_be_lock_applied=True,
        ltf_trend=Direction.BEARISH,
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.be_recovery_calls == []
    assert mon.mae_arm_calls == []  # also no re-arm


@pytest.mark.asyncio
async def test_does_not_fire_when_disabled_regime():
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BEARISH,
        disabled_regimes=["STRONG_TREND"],
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_master_disabled_skips_gate():
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        mae_r_low=-0.7,
        ltf_trend=Direction.BEARISH,
    )
    runner.ctx.config.execution.mae_be_lock_enabled = False
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert mon.mae_arm_calls == []
    assert mon.be_recovery_calls == []


@pytest.mark.asyncio
async def test_unknown_regime_treated_as_allowed():
    """regime=None (UNKNOWN / pre-Phase-A rehydrate) → gate runs normally."""
    runner, mon = _make_runner(
        regime=None, entry=100.0, plan_sl=99.0,
        mae_be_lock_armed=True,
        ltf_trend=Direction.BEARISH,
    )
    state = _AtrState(current_price=100.0)
    await runner._maybe_lock_sl_on_mae_recovery("BTC-USDT-SWAP", "long", state)
    assert len(mon.be_recovery_calls) == 1
