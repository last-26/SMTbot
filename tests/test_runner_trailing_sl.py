"""Phase A.5 (2026-05-02) — multi-step trailing SL after MFE-lock.

Pulls SL forward in `trail_step_r` increments once MFE crosses
`trail_arm_at_mfe_r`. Monotonic-only: a mark dip never releases locked
profit. Disabled regimes (default RANGING) skip the gate entirely.

Math contract (with arm=1.5R, step=0.5R, dist=0.5R):
  MFE  1.4R → no fire (below arm)
  MFE  1.5R → SL = entry+1.0R   (target = floor((1.5-0.5)/0.5)*0.5 = 1.0R)
  MFE  1.9R → no advance (target still 1.0R; monotonic guard)
  MFE  2.0R → SL = entry+1.5R
  MFE  2.5R → SL = entry+2.0R   (or TP fires for STRONG_TREND)
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.bot.runner import BotRunner
from tests.conftest import FakeMonitor, make_config


class _AtrState:
    """Minimal MarketState stand-in: only `atr` and `current_price` are
    read by the trailing-SL gate. Real MarketState's `atr` property is
    frozen, so we use a duck-type for tests."""

    def __init__(self, current_price: float, atr: float = 1.0):
        self.current_price = current_price
        self.atr = atr


class _TrailMonitor(FakeMonitor):
    """FakeMonitor that returns a custom runner-view + records trail calls.

    Simulates the contract `_maybe_trail_sl_after_mfe` exercises:
      get_tracked_runner -> {entry, plan_sl, regime, last_trail_lock_r}
      trail_sl_to(inst, side, new_sl, lock_r) -> records the call
    """

    def __init__(self, *, regime: Optional[str], entry: float, plan_sl: float,
                 last_trail_lock_r: float = 0.0, tp2_price: float = 999.0):
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
            "last_trail_lock_r": last_trail_lock_r,
        }
        self.trail_calls: list[tuple[str, str, float, float]] = []

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        return self._runner_view

    def trail_sl_to(self, inst_id: str, pos_side: str, new_sl: float,
                    lock_r: float) -> bool:
        self.trail_calls.append((inst_id, pos_side, new_sl, lock_r))
        return True


def _make_runner(*, regime: Optional[str], entry: float, plan_sl: float,
                 last_trail_lock_r: float = 0.0,
                 trail_arm: float = 1.5, trail_step: float = 0.5,
                 trail_dist: float = 0.5,
                 disabled_regimes: Optional[list] = None):
    cfg = make_config()
    cfg.execution.trail_sl_enabled = True
    cfg.execution.trail_arm_at_mfe_r = trail_arm
    cfg.execution.trail_step_r = trail_step
    cfg.execution.trail_distance_r = trail_dist
    cfg.execution.trail_disabled_regimes = disabled_regimes or []

    mon = _TrailMonitor(
        regime=regime, entry=entry, plan_sl=plan_sl,
        last_trail_lock_r=last_trail_lock_r,
    )

    class _Ctx:
        config = cfg
        monitor = mon
    runner = object.__new__(BotRunner)
    runner.ctx = _Ctx()
    return runner, mon


@pytest.mark.asyncio
async def test_trail_does_not_fire_below_arm_threshold():
    """MFE 1.4R, arm=1.5R → no trail call."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,  # sl_distance=1.0
    )
    # current_price=101.4 → mfe_r = 1*(101.4-100)/1.0 = 1.4
    state = _AtrState(current_price=101.4)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert mon.trail_calls == []


@pytest.mark.asyncio
async def test_trail_fires_at_arm_threshold_with_step_aligned_lock():
    """MFE 1.5R → target = floor((1.5-0.5)/0.5)*0.5 = 1.0R → SL=entry+1.0R=101.0"""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,
    )
    state = _AtrState(current_price=101.5)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert len(mon.trail_calls) == 1
    inst, side, new_sl, lock_r = mon.trail_calls[0]
    assert inst == "BTC-USDT-SWAP" and side == "long"
    assert new_sl == pytest.approx(101.0)
    assert lock_r == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_trail_no_advance_when_target_lock_unchanged():
    """MFE 1.9R, last_lock=1.0R: target = floor((1.9-0.5)/0.5)*0.5 = 1.0R.
    Same as last_lock → no fire (monotonic + step-aligned guard)."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,
        last_trail_lock_r=1.0,
    )
    state = _AtrState(current_price=101.9)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert mon.trail_calls == []


@pytest.mark.asyncio
async def test_trail_advances_to_next_step_at_2r():
    """MFE 2.0R, last_lock=1.0R: target = floor((2.0-0.5)/0.5)*0.5 = 1.5R.
    Above last_lock → fire SL=entry+1.5R=101.5."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,
        last_trail_lock_r=1.0,
    )
    state = _AtrState(current_price=102.0)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert len(mon.trail_calls) == 1
    _, _, new_sl, lock_r = mon.trail_calls[0]
    assert new_sl == pytest.approx(101.5)
    assert lock_r == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_trail_short_position_inverts_sign():
    """SHORT entry=100, plan_sl=101 (sl_dist=1). MFE 1.5R = mark moved
    to 98.5. Target_lock_r=1.0, new_sl = 100 - 1*1.0 = 99.0."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=101.0,
    )
    state = _AtrState(current_price=98.5)  # 1*(100-98.5)/1 = 1.5R MFE for short
    # Above we used long; here adjust the entry/plan_sl for short
    # Actually our _make_runner doesn't differentiate side; mon._runner_view
    # has entry=100, plan_sl=101 → sl_distance=1. For a SHORT, mark moving
    # DOWN is favorable. mark=98.5, sign=-1, mfe_r=-1*(98.5-100)/1=1.5. Correct.
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "short", state)
    assert len(mon.trail_calls) == 1
    _, side, new_sl, lock_r = mon.trail_calls[0]
    assert side == "short"
    # new_sl = 100 + (-1) * 1.0 * 1.0 = 99.0
    assert new_sl == pytest.approx(99.0)
    assert lock_r == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_trail_skipped_in_disabled_regime():
    """RANGING is in disabled_regimes → no fire even at MFE 2.0R."""
    runner, mon = _make_runner(
        regime="RANGING", entry=100.0, plan_sl=99.0,
        disabled_regimes=["RANGING"],
    )
    state = _AtrState(current_price=102.0)  # MFE 2R
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert mon.trail_calls == []


@pytest.mark.asyncio
async def test_trail_runs_for_weak_trend_when_not_disabled():
    """WEAK_TREND not in disabled_regimes → trailing fires normally."""
    runner, mon = _make_runner(
        regime="WEAK_TREND", entry=100.0, plan_sl=99.0,
        disabled_regimes=["RANGING"],  # WEAK is allowed
    )
    state = _AtrState(current_price=101.5)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert len(mon.trail_calls) == 1


@pytest.mark.asyncio
async def test_trail_master_disabled_skips_gate():
    """`trail_sl_enabled=False` → no fire even at high MFE."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=99.0,
    )
    runner.ctx.config.execution.trail_sl_enabled = False
    state = _AtrState(current_price=102.5)  # MFE 2.5R
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert mon.trail_calls == []


@pytest.mark.asyncio
async def test_trail_unknown_regime_treated_as_allowed():
    """regime=None (UNKNOWN / pre-Phase-A rehydrate) → trailing allowed
    when not in disabled_regimes. Conservative: only RANGING is excluded
    by default; UNKNOWN should still benefit from trailing."""
    runner, mon = _make_runner(
        regime=None, entry=100.0, plan_sl=99.0,
        disabled_regimes=["RANGING"],
    )
    state = _AtrState(current_price=101.5)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert len(mon.trail_calls) == 1


@pytest.mark.asyncio
async def test_trail_skips_when_plan_sl_unknown():
    """plan_sl=0 (post-BE rehydrate sentinel) → trailing skips, can't
    compute R-based math without the immutable plan_sl."""
    runner, mon = _make_runner(
        regime="STRONG_TREND", entry=100.0, plan_sl=0.0,
    )
    state = _AtrState(current_price=102.0)
    await runner._maybe_trail_sl_after_mfe("BTC-USDT-SWAP", "long", state)
    assert mon.trail_calls == []
