"""Phase A.8 (2026-05-02) — weakening-momentum defensive exit.

Closes a profitable position when the cycle-on-cycle directional confluence
score is monotonically falling. Operator: "longdaysak aynı yönlü daha düşük
veriler gelmeye devam ediyorsa cyclelarda hareketin yavaşladığını düşünüp
pozisyonda kalıcılığı istemeyeceğiz."

Conditions:
  1. weakening_exit_enabled
  2. recent_confluence_history has >= weakening_min_cycles entries
  3. Every step-to-step delta within the last `min_cycles` entries is
     >= weakening_min_score_drop (monotonic decline)
  4. mfe_r_high >= weakening_min_mfe_r (only close from profit)

Fires `_defensive_close(symbol, side, "momentum_fade")` which writes
`EARLY_CLOSE_MOMENTUM_FADE` into pending_close_reasons.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.bot.runner import BotRunner
from src.data.models import Direction
from tests.conftest import FakeMonitor, make_config


class _AtrState:
    """Minimal MarketState stand-in: passed by reference to _defensive_close
    chain (which doesn't actually use these fields when bybit_client is
    a stub). Just needs to be a non-None object."""

    def __init__(self):
        self.atr = 1.0
        self.current_price = 100.0
        self.signal_table = type("ST", (), {"vwap_3m": 0.0})()
        self.on_chain = None


class _StubBybit:
    def __init__(self):
        self.close_calls: list[tuple[str, str]] = []

    def close_position(self, symbol: str, side: str) -> None:
        self.close_calls.append((symbol, side))


class _WeakMonitor(FakeMonitor):
    """FakeMonitor that returns a custom runner-view + records score appends."""

    def __init__(self, *, history: list[float], mfe_r_high: float = 0.0,
                 entry: float = 100.0, plan_sl: float = 99.0):
        super().__init__()
        self._runner_view = {
            "entry_price": entry,
            "sl_price": plan_sl,
            "plan_sl_price": plan_sl,
            "tp2_price": 105.0,
            "runner_size": 5,
            "be_already_moved": False,
            "last_tp_revise_at": None,
            "sl_lock_applied": False,
            "regime_at_entry": "WEAK_TREND",
            "last_trail_lock_r": 0.0,
            "mfe_r_high": mfe_r_high,
            "mae_r_low": 0.0,
            "mae_be_lock_armed": False,
            "mae_be_lock_applied": False,
            "recent_confluence_history": tuple(history),
        }
        self._history = list(history)
        self.appended: list[float] = []

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        # Return a fresh tuple each call so the gate's re-fetch sees the
        # latest history (mirrors real PositionMonitor behavior).
        view = dict(self._runner_view)
        view["recent_confluence_history"] = tuple(self._history)
        return view

    def append_confluence_score(self, inst_id: str, pos_side: str,
                                score: float, max_history: int) -> bool:
        self.appended.append(score)
        self._history.append(score)
        overflow = len(self._history) - max_history
        if overflow > 0:
            del self._history[:overflow]
        return True


def _make_runner(*, history: list[float], mfe_r_high: float,
                 enabled: bool = True, min_cycles: int = 3,
                 min_drop: float = 0.5, min_mfe: float = 0.5,
                 score_now: float = 0.0):
    cfg = make_config()
    cfg.execution.weakening_exit_enabled = enabled
    cfg.execution.weakening_min_cycles = min_cycles
    cfg.execution.weakening_min_score_drop = min_drop
    cfg.execution.weakening_min_mfe_r = min_mfe
    cfg.execution.weakening_max_history = 8

    mon = _WeakMonitor(history=history, mfe_r_high=mfe_r_high)
    bybit = _StubBybit()

    class _Ctx:
        config = cfg
        monitor = mon
        bybit_client = bybit
        ltf_cache: dict = {}
        htf_state_cache: dict = {}
        pending_close_reasons: dict = {}
        defensive_close_in_flight: set = set()
    runner = object.__new__(BotRunner)
    runner.ctx = _Ctx()

    # Stub `score_direction` import in runner module to return our chosen
    # score_now without dragging in the full multi_timeframe stack
    # (heavy state.signal_table requirements that the _AtrState stub
    # doesn't satisfy).
    import src.bot.runner as runner_module
    from src.analysis.multi_timeframe import ConfluenceScore

    def fake_score_direction(state, direction, **kwargs):
        return ConfluenceScore(
            direction=direction, score=score_now, factors=[],
        )
    runner_module.score_direction = fake_score_direction
    return runner, mon, bybit


@pytest.mark.asyncio
async def test_does_not_fire_with_short_history():
    """history=[3.0] + append → length 2; min_cycles=3 → no fire even with
    a steep drop. Need min_cycles entries before the gate triggers."""
    runner, mon, bybit = _make_runner(
        history=[3.0], mfe_r_high=1.0, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False
    assert bybit.close_calls == []
    # Score WAS appended; history is now length 2 (< 3 min_cycles).
    assert len(mon.appended) == 1


@pytest.mark.asyncio
async def test_fires_on_three_cycle_monotonic_decline_in_profit():
    """history grows to [4.0, 3.0, 2.0, 1.0] (last 3 = monotonic decline
    with steps 1.0 each, drop_threshold=0.5). MFE 1.2R clears profit
    floor → close fires."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.0, 2.0], mfe_r_high=1.2, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is True
    assert bybit.close_calls == [("BTC-USDT-SWAP", "long")]
    # close_reason stamped via _defensive_close → pending_close_reasons.
    assert (
        runner.ctx.pending_close_reasons[("BTC-USDT-SWAP", "long")]
        == "EARLY_CLOSE_MOMENTUM_FADE"
    )


@pytest.mark.asyncio
async def test_does_not_fire_when_decline_below_drop_threshold():
    """history step-deltas are 0.3 each → below 0.5 threshold. Not a
    confirmed weakening pattern."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.7, 3.4], mfe_r_high=1.0, score_now=3.1,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False
    assert bybit.close_calls == []


@pytest.mark.asyncio
async def test_does_not_fire_when_one_step_increases():
    """history shows decline-recovery-decline pattern → not monotonic.
    last 3 = [3.0, 4.0, 1.0] → step 1 INCREASES (3.0→4.0) → bail."""
    runner, mon, bybit = _make_runner(
        history=[5.0, 3.0, 4.0], mfe_r_high=1.0, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False


@pytest.mark.asyncio
async def test_does_not_fire_when_position_in_mae():
    """All conditions met EXCEPT mfe_r_high=0.2 < min_mfe_r=0.5. Operator's
    max-profit doctrine: only close from profit, not from drawdown."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.0, 2.0], mfe_r_high=0.2, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False


@pytest.mark.asyncio
async def test_master_disabled_skips_gate_entirely():
    """`weakening_exit_enabled=False` → no score compute, no append."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.0, 2.0], mfe_r_high=1.5, enabled=False,
        score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False
    assert bybit.close_calls == []
    assert mon.appended == []  # not even appended


@pytest.mark.asyncio
async def test_min_cycles_2_fires_faster():
    """Operator can tighten min_cycles=2 once demo validates noise level.
    history=[3.0, 1.0] last-2 step delta = 2.0 → fire if mfe ok."""
    runner, mon, bybit = _make_runner(
        history=[3.0], mfe_r_high=1.0, min_cycles=2, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    # After append history=[3.0, 1.0], delta=2.0 > 0.5 threshold, mfe ok.
    assert closed is True


@pytest.mark.asyncio
async def test_short_position_fires_with_same_logic():
    """SHORT position: same scoring pattern, just `pos_side='short'`.
    score_direction is called with Direction.BEARISH internally; scores
    are direction-aligned numbers in our stub."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.0, 2.0], mfe_r_high=1.5, score_now=1.0,
    )
    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "short", state, candles=[],
    )
    assert closed is True
    assert bybit.close_calls == [("BTC-USDT-SWAP", "short")]


@pytest.mark.asyncio
async def test_score_compute_failure_does_not_close():
    """If `score_direction` raises, the gate logs and bails — never
    triggers close on broken telemetry."""
    runner, mon, bybit = _make_runner(
        history=[4.0, 3.0, 2.0], mfe_r_high=1.5, score_now=1.0,
    )
    import src.bot.runner as runner_module

    def boom(*a, **kw):
        raise RuntimeError("synthetic")
    runner_module.score_direction = boom

    state = _AtrState()
    closed = await runner._maybe_close_on_momentum_fade(
        "BTC-USDT-SWAP", "long", state, candles=[],
    )
    assert closed is False
    assert bybit.close_calls == []
