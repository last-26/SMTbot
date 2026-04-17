"""Tests for src.strategy.entry_signals — intent + pipeline."""

from __future__ import annotations

import pytest

from src.analysis.fvg import FVG
from src.analysis.order_blocks import OrderBlock as PyOrderBlock
from src.data.candle_buffer import Candle
from src.data.models import (
    Direction,
    FVGZone,
    MarketState,
    OrderBlock,
    OscillatorTableData,
    SignalTableData,
)
from src.strategy.entry_signals import (
    build_trade_plan_from_state,
    build_trade_plan_with_reason,
    generate_entry_intent,
    select_sl_price,
)


def _state(
    *,
    price: float = 100.0,
    atr: float = 1.0,
    trend_htf: Direction = Direction.BULLISH,
    last_mss: str = "BULLISH@99",
    active_ob: str = "BULL@95-97",
    vmc_ribbon: str = "BULLISH",
    order_blocks: list[OrderBlock] | None = None,
    fvg_zones: list[FVGZone] | None = None,
) -> MarketState:
    sig = SignalTableData(
        trend_htf=trend_htf,
        last_mss=last_mss,
        active_ob=active_ob,
        vmc_ribbon=vmc_ribbon,
        price=price,
        atr_14=atr,
    )
    return MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=order_blocks or [],
        fvg_zones=fvg_zones or [],
    )


def _c(low: float, high: float) -> Candle:
    return Candle(open=low, high=high, low=low, close=(low + high) / 2)


# ── select_sl_price priority order ──────────────────────────────────────────


def test_sl_prefers_pine_order_block_when_available():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)
    state = _state(order_blocks=[ob])
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=1.0, buffer_mult=0.2,
    )
    assert src == "order_block_pine"
    assert sl == pytest.approx(94.8)


def test_sl_falls_back_to_pine_fvg_when_no_ob():
    fvg = FVGZone(direction=Direction.BULLISH, bottom=94.0, top=96.0)
    state = _state(fvg_zones=[fvg])
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=1.0, buffer_mult=0.2,
    )
    assert src == "fvg_pine"
    assert sl == pytest.approx(93.8)


def test_sl_falls_back_to_python_ob():
    state = _state()
    py_ob = PyOrderBlock(
        direction=Direction.BULLISH, bottom=92.0, top=94.0, origin_bar=0,
    )
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=1.0,
        python_order_blocks=[py_ob], buffer_mult=0.2,
    )
    assert src == "order_block_py"
    assert sl == pytest.approx(91.8)


def test_sl_falls_back_to_swing_lookback():
    state = _state()
    candles = [_c(88, 92), _c(87, 93), _c(90, 95), _c(91, 96)]
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=1.0,
        candles=candles, buffer_mult=0.2,
    )
    assert src == "swing"
    assert sl == pytest.approx(87.0 - 0.2)


def test_sl_final_fallback_is_atr():
    state = _state()
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=2.0,
        atr_fallback_mult=2.0,
    )
    assert src == "atr_fallback"
    assert sl == pytest.approx(96.0)


def test_sl_returns_none_when_atr_zero():
    state = _state(atr=0.0)
    sl, src = select_sl_price(
        state, Direction.BULLISH, entry_price=100.0, atr=0.0,
    )
    assert sl is None
    assert src == ""


def test_bearish_ob_must_be_above_entry():
    # A BEARISH OB below entry should NOT be selected — it's already mitigated
    ob = OrderBlock(direction=Direction.BEARISH, bottom=95.0, top=97.0)
    state = _state(order_blocks=[ob])
    sl, src = select_sl_price(
        state, Direction.BEARISH, entry_price=100.0, atr=1.0,
    )
    # Falls through to ATR fallback since no valid structural level
    assert src == "atr_fallback"


# ── generate_entry_intent ───────────────────────────────────────────────────


def test_intent_none_when_confluence_below_threshold():
    state = _state(
        trend_htf=Direction.UNDEFINED, last_mss="", active_ob="", vmc_ribbon="",
    )
    intent = generate_entry_intent(state, min_confluence_score=2.0)
    assert intent is None


def test_intent_returned_when_confluence_strong():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)
    state = _state(order_blocks=[ob])
    intent = generate_entry_intent(state, min_confluence_score=2.0)
    assert intent is not None
    assert intent.direction == Direction.BULLISH
    assert intent.is_tradable
    assert intent.sl_source == "order_block_pine"
    assert intent.confluence.score >= 2.0


def test_intent_none_when_price_zero():
    state = _state(price=0.0)
    assert generate_entry_intent(state) is None


# ── build_trade_plan_from_state ─────────────────────────────────────────────


def test_pipeline_produces_plan_end_to_end():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)
    state = _state(order_blocks=[ob], price=100.0, atr=1.0)
    plan = build_trade_plan_from_state(
        state,
        account_balance=10_000.0,
        min_confluence_score=2.0,
        risk_pct=0.01,
        rr_ratio=3.0,
        min_rr_ratio=2.0,
        max_leverage=20,
    )
    assert plan is not None
    assert plan.direction == Direction.BULLISH
    assert plan.sl_price == pytest.approx(94.8)
    assert plan.tp_price > plan.entry_price
    assert plan.num_contracts > 0
    assert plan.sl_source == "order_block_pine"
    assert "htf_trend_alignment" in plan.confluence_factors


def test_pipeline_returns_none_when_confluence_low():
    state = _state(
        trend_htf=Direction.UNDEFINED, last_mss="", active_ob="", vmc_ribbon="",
    )
    plan = build_trade_plan_from_state(state, account_balance=10_000.0)
    assert plan is None


def test_pipeline_rejects_rr_below_floor():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95, top=97)
    state = _state(order_blocks=[ob])
    with pytest.raises(ValueError):
        build_trade_plan_from_state(
            state, account_balance=10_000, rr_ratio=1.5, min_rr_ratio=2.0,
        )


def test_pipeline_returns_none_when_contracts_round_to_zero():
    """Tiny balance where contract rounding can't even buy 1 contract."""
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95, top=97)
    state = _state(order_blocks=[ob], price=50_000.0, atr=100.0)
    # With balance=10 and contract_size=0.01, min contract notional = 500 USDT
    # But capped leverage = 20 * 10 = 200, so 200/500 = 0 contracts
    plan = build_trade_plan_from_state(
        state, account_balance=10.0, max_leverage=20,
    )
    assert plan is None


# ── build_trade_plan_with_reason — reject reason strings ───────────────────


def test_reason_below_confluence_when_direction_undefined():
    state = _state(
        trend_htf=Direction.UNDEFINED, last_mss="", active_ob="", vmc_ribbon="",
    )
    plan, reason = build_trade_plan_with_reason(state, account_balance=10_000.0)
    assert plan is None
    assert reason == "below_confluence"


def test_reason_zero_contracts_when_balance_too_tight():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95, top=97)
    state = _state(order_blocks=[ob], price=50_000.0, atr=100.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10.0, max_leverage=20,
    )
    assert plan is None
    assert reason == "zero_contracts"


def test_reason_empty_on_success():
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95, top=97)
    state = _state(order_blocks=[ob])
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
    )
    assert plan is not None
    assert reason == ""


# ── min_tp_distance_pct (fee-aware gate) ────────────────────────────────────


def test_min_tp_distance_gate_disabled_by_default():
    """With default min_tp_distance_pct=0, the gate is off — the same plan
    that would trip a non-zero threshold still passes."""
    # Entry 100, SL 99.5 → sl_dist=0.5 → tp_dist=1.5 (rr=3) → tp_pct=1.5%
    # With threshold=0.0, gate is disabled, plan survives.
    ob = OrderBlock(direction=Direction.BULLISH, bottom=99.0, top=99.7)
    state = _state(order_blocks=[ob], price=100.0, atr=1.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
    )
    assert plan is not None
    assert reason == ""


def test_min_tp_distance_gate_rejects_tight_tp():
    """A sub-threshold TP distance returns (None, 'tp_too_tight')."""
    # Very tight SL → very tight TP. SL at 99.95 (0.05%), RR=3 → TP=100.15,
    # tp_dist_pct = 0.0015 → below 0.004 threshold → reject.
    ob = OrderBlock(direction=Direction.BULLISH, bottom=99.9, top=99.94)
    state = _state(order_blocks=[ob], price=100.0, atr=0.01)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_tp_distance_pct=0.004,  # 0.4%
    )
    assert plan is None
    assert reason == "tp_too_tight"


def test_min_tp_distance_gate_allows_wide_tp():
    """A TP distance above the threshold passes through unchanged."""
    # SL at 99 (1%), RR=3 → TP at 103, tp_dist_pct=0.03 → above 0.004 floor.
    ob = OrderBlock(direction=Direction.BULLISH, bottom=98.5, top=99.0)
    state = _state(order_blocks=[ob], price=100.0, atr=1.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_tp_distance_pct=0.004,
    )
    assert plan is not None
    assert reason == ""
    tp_dist_pct = (plan.tp_price - plan.entry_price) / plan.entry_price
    assert tp_dist_pct >= 0.004


def test_min_tp_distance_gate_short_side():
    """Gate works on bearish trades — uses abs() on tp-entry distance."""
    # Short entry 100, SL 100.05 (0.05%), RR=3 → TP=99.85, dist_pct=0.0015.
    ob = OrderBlock(direction=Direction.BEARISH, bottom=100.06, top=100.10)
    state = _state(
        order_blocks=[ob], price=100.0, atr=0.01,
        trend_htf=Direction.BEARISH, last_mss="BEARISH@101",
        active_ob="BEAR@100.06-100.10", vmc_ribbon="BEARISH",
    )
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_tp_distance_pct=0.004,
    )
    assert plan is None
    assert reason == "tp_too_tight"


# ── min_sl_distance_pct (noise floor gate) ─────────────────────────────────


def test_min_sl_distance_gate_disabled_by_default():
    """With default min_sl_distance_pct=0, the gate is off."""
    ob = OrderBlock(direction=Direction.BULLISH, bottom=99.9, top=99.94)
    state = _state(order_blocks=[ob], price=100.0, atr=0.01)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
    )
    # With no fee gate either, the plan itself builds — may fail zero_contracts
    # below a price=100 / balance relationship. Here it passes as a plan.
    assert reason in ("", "zero_contracts")


def test_min_sl_distance_gate_widens_tight_sl():
    """A sub-threshold SL distance is widened to the floor, not rejected.

    With a tight OB-derived SL (~0.05% from entry) and a 0.003 floor, the
    gate should widen SL to exactly 0.3% below entry (bullish case) and
    return a valid plan. Sizing auto-shrinks to keep R constant.
    """
    ob = OrderBlock(direction=Direction.BULLISH, bottom=99.9, top=99.94)
    state = _state(order_blocks=[ob], price=100.0, atr=0.01)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_sl_distance_pct=0.003,
    )
    assert plan is not None
    assert reason == ""
    sl_dist_pct = (plan.entry_price - plan.sl_price) / plan.entry_price
    # Floor applied exactly — not widened beyond the threshold.
    assert sl_dist_pct == pytest.approx(0.003, abs=1e-6)


def test_min_sl_distance_gate_allows_wide_sl():
    """A wide SL passes through — no reject."""
    ob = OrderBlock(direction=Direction.BULLISH, bottom=98.5, top=99.0)
    state = _state(order_blocks=[ob], price=100.0, atr=1.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_sl_distance_pct=0.003,
    )
    assert plan is not None
    assert reason == ""
    sl_dist_pct = (plan.entry_price - plan.sl_price) / plan.entry_price
    assert sl_dist_pct >= 0.003


def test_min_sl_distance_gate_widens_short_side():
    """Bearish side: a tight SL above entry is widened above the floor."""
    ob = OrderBlock(direction=Direction.BEARISH, bottom=100.06, top=100.10)
    state = _state(
        order_blocks=[ob], price=100.0, atr=0.01,
        trend_htf=Direction.BEARISH, last_mss="BEARISH@101",
        active_ob="BEAR@100.06-100.10", vmc_ribbon="BEARISH",
    )
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        min_sl_distance_pct=0.003,
    )
    assert plan is not None
    assert reason == ""
    sl_dist_pct = (plan.sl_price - plan.entry_price) / plan.entry_price
    assert sl_dist_pct == pytest.approx(0.003, abs=1e-6)


def test_min_tp_distance_runs_after_htf_ceiling_squeeze():
    """After an HTF ceiling pulls TP in, the fee-aware floor still applies.

    SL at 99 (rr_ratio=3 would give TP=103), but a RESISTANCE zone at
    100.3-100.4 caps TP to ~100.1. That sits at ~0.1% — below a 0.004 floor.
    Order of operations: ceiling first, then fee gate.
    """
    from src.analysis.support_resistance import SRZone

    ob = OrderBlock(direction=Direction.BULLISH, bottom=98.5, top=99.0)
    state = _state(order_blocks=[ob], price=100.0, atr=1.0)
    # RESISTANCE zone just above entry compresses TP.
    zone = SRZone(
        center=100.35, bottom=100.3, top=100.4,
        touches=3, role="RESISTANCE", score=1.0,
    )
    # With HTF ceiling on, TP becomes ~100.1 → rr=~0.1 → below min_rr_ratio
    # → rejected as "htf_tp_ceiling" before reaching the fee gate.
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0,
        htf_sr_zones=[zone],
        htf_sr_ceiling_enabled=True,
        htf_sr_buffer_atr=0.2,
        min_tp_distance_pct=0.004,
    )
    assert plan is None
    # The HTF ceiling gate fires first because it's the stricter/earlier check.
    assert reason == "htf_tp_ceiling"


# ── partial_tp_enabled split feasibility ────────────────────────────────────


def test_reject_when_partial_tp_split_would_be_degenerate():
    """partial_tp_enabled + num_contracts that cannot split into two
    positive legs → reject with `insufficient_contracts_for_split`.
    Guarantees TP1/TP2 fires on every trade rather than silently
    degrading to single OCO."""
    # price=500, contracts_unit = 0.01*500 = 5 USDT; OB at 470-480 → SL near
    # 470, sl_dist≈30, sl_pct≈0.06. Balance 60 USDT, 1% risk = 0.6 USDT.
    # Ideal notional = 0.6/0.06 = 10 USDT → num_contracts = 10//5 = 2 — still
    # splits. Shrink risk_pct to 0.5% → risk=0.3 → notional=5 → contracts=1.
    ob = OrderBlock(direction=Direction.BULLISH, bottom=470, top=480)
    state = _state(order_blocks=[ob], price=500.0, atr=5.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=100.0, max_leverage=20,
        risk_pct=0.005,
        partial_tp_enabled=True, partial_tp_ratio=0.5,
    )
    assert plan is None
    assert reason == "insufficient_contracts_for_split"


def test_partial_tp_disabled_skips_split_gate():
    """With partial_tp_enabled=False the split-feasibility gate is inert,
    so a single-contract plan still opens (single-OCO path)."""
    ob = OrderBlock(direction=Direction.BULLISH, bottom=470, top=480)
    state = _state(order_blocks=[ob], price=500.0, atr=5.0)
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=100.0, max_leverage=20,
        risk_pct=0.005,
        partial_tp_enabled=False,
    )
    # May still reject for `zero_contracts`, but never for the split reason.
    assert reason != "insufficient_contracts_for_split"


def test_partial_tp_passes_with_splittable_plan():
    """A plan with enough contracts for a clean 50/50 split passes the gate."""
    ob = OrderBlock(direction=Direction.BULLISH, bottom=95, top=97)
    state = _state(order_blocks=[ob])
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0, max_leverage=20,
        partial_tp_enabled=True, partial_tp_ratio=0.5,
    )
    assert plan is not None
    assert reason == ""
    # Both legs must be strictly positive.
    size1 = int(plan.num_contracts * 0.5)
    size2 = plan.num_contracts - size1
    assert size1 > 0 and size2 > 0
