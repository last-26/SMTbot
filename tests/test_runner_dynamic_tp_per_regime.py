"""Phase A.3 (2026-05-02) — regime-aware target_rr_ratio in dynamic-TP revise.

`_maybe_revise_tp_dynamic` now reads the regime captured at entry-time on
the tracked position and looks up `target_rr_ratio_per_regime[regime]` (or
falls back to the global `target_rr_ratio`). Same per-regime lookup applies
to `tp_min_rr_floor`. UNKNOWN / None / missing regime → global.

The math itself is unchanged (entry + sign × target_rr × sl_dist with
floor / cooldown / delta gates). These tests just pin the per-regime
selection logic so it can't silently regress.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.bot.runner import BotRunner
from tests.conftest import FakeMonitor, make_config


class _AtrState:
    """Minimal MarketState stand-in: only `atr` is read by the dynamic-TP
    revise gate. Real MarketState has a frozen `atr` property; we need a
    settable equivalent so tests can dial different ATR values."""

    def __init__(self, atr: float):
        self.atr = atr


class _RegimeAwareMonitor(FakeMonitor):
    """FakeMonitor variant that returns a custom runner-view dict so the
    dynamic-TP gate exercises the real lookup path instead of bailing out
    early on `get_tracked_runner is None`."""

    def __init__(self, *, regime: Optional[str], entry: float, plan_sl: float,
                 cur_tp: float):
        super().__init__()
        self._runner_view = {
            "entry_price": entry,
            "sl_price": plan_sl,
            "plan_sl_price": plan_sl,
            "tp2_price": cur_tp,
            "runner_size": 5,
            "be_already_moved": False,
            "last_tp_revise_at": None,
            "sl_lock_applied": False,
            "regime_at_entry": regime,
        }

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        return self._runner_view


def _make_runner(monkeypatch, *, regime: Optional[str], entry: float,
                 plan_sl: float, cur_tp: float, target_rr_global: float = 1.5,
                 target_rr_per_regime: Optional[dict] = None,
                 tp_min_rr_floor_global: float = 0.7,
                 tp_min_rr_floor_per_regime: Optional[dict] = None):
    """Construct a BotRunner with an injected regime-aware monitor and
    config. Bypasses the heavy plan-builder path — these tests poke the
    revise method directly."""
    cfg = make_config()
    cfg.execution.tp_dynamic_enabled = True
    cfg.execution.target_rr_ratio = target_rr_global
    cfg.execution.tp_min_rr_floor = tp_min_rr_floor_global
    cfg.execution.tp_revise_min_delta_atr = 0.0  # always exceed delta in tests
    cfg.execution.tp_revise_cooldown_s = 0.0
    if target_rr_per_regime is not None:
        cfg.execution.target_rr_ratio_per_regime = target_rr_per_regime
    if tp_min_rr_floor_per_regime is not None:
        cfg.execution.tp_min_rr_floor_per_regime = tp_min_rr_floor_per_regime

    mon = _RegimeAwareMonitor(
        regime=regime, entry=entry, plan_sl=plan_sl, cur_tp=cur_tp,
    )

    # Build a minimal context — only the fields _maybe_revise_tp_dynamic
    # touches are needed (config + monitor). Real BotContext requires more,
    # so we build a duck-typed stub.
    class _Ctx:
        config = cfg
        monitor = mon
    runner = object.__new__(BotRunner)
    runner.ctx = _Ctx()
    return runner, mon


@pytest.mark.asyncio
async def test_revise_uses_global_rr_when_per_regime_empty(monkeypatch):
    runner, mon = _make_runner(
        monkeypatch, regime="WEAK_TREND",
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5, target_rr_per_regime={},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    # sl_distance=1.0, target_rr=1.5 → new_tp = 100 + 1*1.5 = 101.5
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 101.5)]


@pytest.mark.asyncio
async def test_revise_picks_per_regime_target_rr_for_strong_trend(monkeypatch):
    runner, mon = _make_runner(
        monkeypatch, regime="STRONG_TREND",
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5,
        target_rr_per_regime={"RANGING": 1.2, "STRONG_TREND": 2.5},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    # sl_distance=1.0, regime override target_rr=2.5 → 100 + 1*2.5 = 102.5
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 102.5)]


@pytest.mark.asyncio
async def test_revise_picks_per_regime_target_rr_for_ranging(monkeypatch):
    runner, mon = _make_runner(
        monkeypatch, regime="RANGING",
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5,
        target_rr_per_regime={"RANGING": 1.2, "STRONG_TREND": 2.5},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    # RANGING override target_rr=1.2 → 100 + 1*1.2 = 101.2
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 101.2)]


@pytest.mark.asyncio
async def test_revise_falls_back_to_global_when_regime_missing_in_dict(monkeypatch):
    """WEAK_TREND not in the override dict → use global 1.5."""
    runner, mon = _make_runner(
        monkeypatch, regime="WEAK_TREND",
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5,
        target_rr_per_regime={"RANGING": 1.2, "STRONG_TREND": 2.5},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 101.5)]


@pytest.mark.asyncio
async def test_revise_falls_back_to_global_for_unknown_regime(monkeypatch):
    """UNKNOWN explicitly falls through (Phase A contract).

    A pre-Phase-A rehydrate row arriving with regime_at_entry=None must
    behave identically to the legacy global-only code path."""
    runner, mon = _make_runner(
        monkeypatch, regime=None,
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5,
        target_rr_per_regime={"RANGING": 1.2, "STRONG_TREND": 2.5},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 101.5)]


@pytest.mark.asyncio
async def test_revise_short_position_per_regime_target_inverts_sign(monkeypatch):
    runner, mon = _make_runner(
        monkeypatch, regime="STRONG_TREND",
        entry=100.0, plan_sl=101.0, cur_tp=98.0,
        target_rr_global=1.5,
        target_rr_per_regime={"STRONG_TREND": 2.5},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "short", state)
    # SHORT: sl_distance=1.0, target_rr=2.5 → 100 - 1*2.5 = 97.5
    assert mon.revise_calls == [("BTC-USDT-SWAP", "short", 97.5)]


@pytest.mark.asyncio
async def test_revise_per_regime_floor_clamps_sub_floor_proposal(monkeypatch):
    """If mark drift made the target_rr proposal slip below the per-regime
    floor, the floor wins."""
    # entry=100, plan_sl=99 → sl_distance=1.0
    # target_rr=0.5 (via per-regime override) → new_tp = 100.5
    # floor=1.0 → min_tp = 101 → must clamp to 101
    runner, mon = _make_runner(
        monkeypatch, regime="STRONG_TREND",
        entry=100.0, plan_sl=99.0, cur_tp=102.0,
        target_rr_global=1.5,
        target_rr_per_regime={"STRONG_TREND": 0.5},
        tp_min_rr_floor_global=0.7,
        tp_min_rr_floor_per_regime={"STRONG_TREND": 1.0},
    )
    state = _AtrState(0.5)
    await runner._maybe_revise_tp_dynamic("BTC-USDT-SWAP", "long", state)
    # Floor 1.0 wins: new_tp = 100 + 1*1.0 = 101.0
    assert mon.revise_calls == [("BTC-USDT-SWAP", "long", 101.0)]
