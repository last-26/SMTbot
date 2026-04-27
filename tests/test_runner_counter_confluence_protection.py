"""2026-04-27 — Counter-confluence open-position protection (Mekanizma 2).

Tests the runner's `_maybe_apply_counter_confluence_protection` directly,
stubbing the helper methods (`_compute_counter_confluence_score`,
`_get_position_mfe_r`) and module-level imports
(`count_failing_invalidation_gates`, `classify_trend_regime`) so each test
isolates ONE behaviour:

  * MFE > 1R trigger → BE+0.5R lock
  * MFE 0..1R trigger → BE+fee lock
  * MFE < 0 trigger → defensive close (EARLY_CLOSE_COUNTER_CONFLUENCE)
  * Below counter-confluence threshold → no trigger, streak resets
  * Insufficient hard-gate count → no trigger, streak resets
  * Aligned STRONG_TREND → exempt, streak resets (pullback noise)
  * Hysteresis: streak < cycles → no fire, counter increments
  * Disabled flag → skip entirely (no score/gate calls)
  * Recovery: trigger met then not met → streak resets to 0
  * Close path clears the streak via _handle_close
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.analysis.trend_regime import TrendRegime
from src.bot.runner import BotRunner
from src.data.models import Direction
from tests.conftest import FakeMonitor, make_config


UTC = timezone.utc


# ── Minimal counter-protection-aware monitor ───────────────────────────────


class CounterFakeMonitor(FakeMonitor):
    """FakeMonitor + `lock_sl_at` recorder + scriptable `get_tracked_runner`."""

    def __init__(self, *, entry_price: float = 100.0, plan_sl: float = 95.0):
        super().__init__()
        self.lock_sl_calls: list[dict] = []
        self.tracked_runner_payload: dict | None = {
            "entry_price": entry_price,
            "sl_price": plan_sl,
            "plan_sl_price": plan_sl,
            "tp2_price": entry_price + (entry_price - plan_sl) * 2.0,
            "runner_size": 1,
            "be_already_moved": False,
            "last_tp_revise_at": None,
            "sl_lock_applied": False,
        }

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        return self.tracked_runner_payload

    def lock_sl_at(
        self, inst_id: str, pos_side: str, new_sl: float,
        *, one_shot: bool = True, tighter_only: bool = True,
    ) -> bool:
        self.lock_sl_calls.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "new_sl": new_sl, "one_shot": one_shot,
            "tighter_only": tighter_only,
        })
        return True


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_runner(make_ctx, *, entry_price: float = 100.0,
                 plan_sl: float = 95.0, htf_bias: Direction = Direction.UNDEFINED):
    """Build a runner with the counter-confluence-aware monitor + a
    fake state (SimpleNamespace) whose `trend_htf` and `current_price`
    are settable. MarketState's `trend_htf` is a property without a setter,
    so the evaluator's `getattr(state, "trend_htf", ...)` and
    `getattr(state, "current_price", ...)` plumbing accepts duck-typed
    objects fine."""
    cfg = make_config()
    cfg.execution.counter_confluence_protection_enabled = True
    cfg.execution.counter_confluence_threshold = 3.75
    cfg.execution.counter_confluence_decay_cycles = 3
    cfg.execution.counter_confluence_min_hard_gates = 2
    cfg.execution.sl_be_offset_pct = 0.001  # 10 bps fee buffer
    monitor = CounterFakeMonitor(entry_price=entry_price, plan_sl=plan_sl)
    ctx, fakes = make_ctx(config=cfg, monitor=monitor)
    state = SimpleNamespace(
        current_price=entry_price,
        trend_htf=htf_bias,
    )
    return BotRunner(ctx), ctx, monitor, state


def _stub_decision(monkeypatch, *, score: float, gates: int,
                   mfe: float | None, regime: TrendRegime = TrendRegime.UNKNOWN):
    """Stub all decision inputs so the evaluator deterministically reaches
    its dispatcher branch.

    score: counter-confluence score returned by `_compute_counter_confluence_score`
    gates: failing hard-gate count returned by `count_failing_invalidation_gates`
    mfe:   running MFE in plan-R multiples (None disables `_get_position_mfe_r`)
    regime: TrendRegime classifier result for the exemption gate
    """
    monkeypatch.setattr(
        BotRunner, "_compute_counter_confluence_score",
        lambda self, *_a, **_kw: score,
    )
    monkeypatch.setattr(
        BotRunner, "_get_position_mfe_r",
        lambda self, *_a, **_kw: mfe,
    )
    monkeypatch.setattr(
        "src.bot.runner.count_failing_invalidation_gates",
        lambda **_kw: gates,
    )
    monkeypatch.setattr(
        "src.bot.runner.classify_trend_regime",
        lambda *_a, **_kw: SimpleNamespace(regime=regime),
    )


# ── Tests ──────────────────────────────────────────────────────────────────


async def test_trigger_mfe_above_1r_locks_at_be_plus_05r(monkeypatch, make_ctx):
    """MFE > 1R + counter-confluence trigger → SL lock at entry + 0.5R."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    _stub_decision(monkeypatch, score=4.5, gates=2, mfe=1.4)
    candles: list = []
    # Drive 3 cycles (default cycles=3) to trip the streak.
    for _ in range(3):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "long", state, candles,
        )
    assert len(monitor.lock_sl_calls) == 1
    call = monitor.lock_sl_calls[0]
    # entry=100, plan_sl=95 → sl_distance=5 → BE+0.5R = 100 + 0.5*5 = 102.5
    assert call["new_sl"] == 102.5
    assert call["one_shot"] is False
    assert call["tighter_only"] is True


async def test_trigger_mfe_in_0_to_1r_locks_at_be_plus_fee(monkeypatch, make_ctx):
    """MFE 0..1R + counter-confluence trigger → SL lock at BE + fee buffer."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    _stub_decision(monkeypatch, score=4.0, gates=3, mfe=0.4)
    candles: list = []
    for _ in range(3):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "long", state, candles,
        )
    assert len(monitor.lock_sl_calls) == 1
    # entry=100, sl_be_offset_pct=0.001 → new_sl = 100 + 100*0.001 = 100.1
    assert abs(monitor.lock_sl_calls[0]["new_sl"] - 100.1) < 1e-9


async def test_trigger_mfe_negative_fires_defensive_close(monkeypatch, make_ctx):
    """MFE < 0 + counter-confluence trigger → defensive close with
    EARLY_CLOSE_COUNTER_CONFLUENCE close_reason."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    _stub_decision(monkeypatch, score=4.5, gates=2, mfe=-0.5)
    # Spy on _defensive_close to inspect arguments without exercising
    # the actual Bybit close path.
    close_calls: list[dict] = []

    async def _spy_close(self, symbol, side, reason, *,
                        close_reason="EARLY_CLOSE_LTF_REVERSAL"):
        close_calls.append({
            "symbol": symbol, "side": side,
            "reason": reason, "close_reason": close_reason,
        })
    monkeypatch.setattr(BotRunner, "_defensive_close", _spy_close)
    candles: list = []
    for _ in range(3):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "long", state, candles,
        )
    assert len(close_calls) == 1
    assert close_calls[0]["close_reason"] == "EARLY_CLOSE_COUNTER_CONFLUENCE"
    assert close_calls[0]["reason"].startswith("counter_confluence:")
    # SL lock not invoked on the close branch.
    assert monitor.lock_sl_calls == []
    # Streak cleared after dispatch.
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_below_threshold_no_trigger_streak_resets(monkeypatch, make_ctx):
    """Counter score below threshold → no trigger; an existing streak is
    reset to 0 (entry popped from the dict)."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    # Pre-seed an in-progress streak so we can verify reset.
    ctx.counter_confluence_streak[("BTC-USDT-SWAP", "long")] = 2
    _stub_decision(monkeypatch, score=3.0, gates=3, mfe=0.5)
    candles: list = []
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_insufficient_gates_no_trigger_streak_resets(monkeypatch, make_ctx):
    """Counter score above threshold but only 1 gate flipped (< min=2) →
    no trigger, streak resets."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    ctx.counter_confluence_streak[("BTC-USDT-SWAP", "long")] = 2
    _stub_decision(monkeypatch, score=5.0, gates=1, mfe=0.5)
    candles: list = []
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_aligned_strong_trend_exempts_long_position(monkeypatch, make_ctx):
    """STRONG_TREND + position direction agrees with HTF bias → exemption,
    no trigger, streak resets. Pullback-noise shield."""
    runner, ctx, monitor, state = _make_runner(
        make_ctx, htf_bias=Direction.BULLISH,
    )
    ctx.counter_confluence_streak[("BTC-USDT-SWAP", "long")] = 2
    _stub_decision(monkeypatch, score=5.0, gates=3, mfe=0.5,
                   regime=TrendRegime.STRONG_TREND)
    candles: list = []
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_strong_trend_against_position_does_NOT_exempt(monkeypatch, make_ctx):
    """STRONG_TREND but the trend is AGAINST the position (long vs HTF
    bearish) → no exemption, trigger fires normally."""
    runner, ctx, monitor, state = _make_runner(
        make_ctx, htf_bias=Direction.BEARISH,  # against the long
    )
    _stub_decision(monkeypatch, score=4.5, gates=2, mfe=0.4,
                   regime=TrendRegime.STRONG_TREND)
    candles: list = []
    for _ in range(3):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "long", state, candles,
        )
    assert len(monitor.lock_sl_calls) == 1


async def test_hysteresis_below_cycles_does_not_fire(monkeypatch, make_ctx):
    """With cycles=3, two consecutive trigger cycles must NOT fire — only
    the third does. Counter increments cleanly."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    _stub_decision(monkeypatch, score=4.0, gates=3, mfe=0.5)
    candles: list = []
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) == 2


async def test_disabled_flag_skips_score_and_gate_calls(monkeypatch, make_ctx):
    """When `counter_confluence_protection_enabled=False` the evaluator
    is a no-op — neither score nor gate count is computed (sentinel
    counter stays 0)."""
    runner, ctx, monitor, state = _make_runner(make_ctx)
    runner.ctx.config.execution.counter_confluence_protection_enabled = False
    score_calls = {"n": 0}

    def _spy_score(self, *_a, **_kw):
        score_calls["n"] += 1
        return 99.0  # arbitrarily high; should not be observed

    monkeypatch.setattr(BotRunner, "_compute_counter_confluence_score", _spy_score)
    candles: list = []
    for _ in range(5):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "long", state, candles,
        )
    assert score_calls["n"] == 0
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_recovery_after_partial_streak_resets_to_zero(monkeypatch, make_ctx):
    """Two trigger cycles → recovery cycle (score below threshold) → no
    fire, streak resets to 0. A subsequent trigger cycle starts fresh."""
    runner, ctx, monitor, state = _make_runner(make_ctx)

    # Cycle 1+2: trigger holds.
    _stub_decision(monkeypatch, score=4.0, gates=3, mfe=0.5)
    candles: list = []
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) == 2

    # Cycle 3: recovery (score drops).
    _stub_decision(monkeypatch, score=3.0, gates=3, mfe=0.5)
    await runner._maybe_apply_counter_confluence_protection(
        "BTC-USDT-SWAP", "long", state, candles,
    )
    assert monitor.lock_sl_calls == []
    assert ctx.counter_confluence_streak.get(("BTC-USDT-SWAP", "long")) is None


async def test_short_position_be_plus_05r_lock_below_entry(monkeypatch, make_ctx):
    """Short pos: MFE > 1R lock should sit BELOW entry by 0.5R (sign flip)."""
    runner, ctx, monitor, state = _make_runner(
        make_ctx, entry_price=100.0, plan_sl=105.0,  # short setup
    )
    _stub_decision(monkeypatch, score=4.5, gates=2, mfe=1.4)
    candles: list = []
    for _ in range(3):
        await runner._maybe_apply_counter_confluence_protection(
            "BTC-USDT-SWAP", "short", state, candles,
        )
    assert len(monitor.lock_sl_calls) == 1
    # entry=100, plan_sl=105 → sl_distance=5 → short BE+0.5R = 100 - 0.5*5 = 97.5
    assert monitor.lock_sl_calls[0]["new_sl"] == 97.5
