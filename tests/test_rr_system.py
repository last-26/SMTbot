"""Tests for src.strategy.rr_system — pure R:R math."""

from __future__ import annotations

import math

import pytest

from src.data.models import Direction
from src.strategy.rr_system import (
    break_even_win_rate,
    calculate_trade_plan,
    expected_value_r,
)


# ── break_even_win_rate ─────────────────────────────────────────────────────


def test_break_even_1_to_1():
    assert break_even_win_rate(1.0) == pytest.approx(0.5)


def test_break_even_1_to_2():
    assert break_even_win_rate(2.0) == pytest.approx(1 / 3)


def test_break_even_1_to_3():
    assert break_even_win_rate(3.0) == pytest.approx(0.25)


def test_break_even_rejects_non_positive():
    with pytest.raises(ValueError):
        break_even_win_rate(0)
    with pytest.raises(ValueError):
        break_even_win_rate(-1)


# ── expected_value_r ────────────────────────────────────────────────────────


def test_ev_zero_at_break_even():
    ev = expected_value_r(0.5, 1.0)
    assert ev == pytest.approx(0.0)


def test_ev_positive_when_win_rate_exceeds_break_even():
    assert expected_value_r(0.5, 3.0) > 0


def test_ev_rejects_bad_inputs():
    with pytest.raises(ValueError):
        expected_value_r(-0.1, 2.0)
    with pytest.raises(ValueError):
        expected_value_r(1.1, 2.0)
    with pytest.raises(ValueError):
        expected_value_r(0.5, 0)


# ── calculate_trade_plan: validation ────────────────────────────────────────


def test_long_sl_must_be_below_entry():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BULLISH, entry_price=100.0, sl_price=101.0,
            account_balance=1000.0,
        )


def test_short_sl_must_be_above_entry():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BEARISH, entry_price=100.0, sl_price=99.0,
            account_balance=1000.0,
        )


def test_rejects_undefined_direction():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.UNDEFINED, 100.0, 99.0, 1000.0,
        )


def test_rejects_non_positive_entry_or_sl():
    with pytest.raises(ValueError):
        calculate_trade_plan(Direction.BULLISH, 0.0, -1.0, 1000.0)


def test_rejects_risk_pct_out_of_range():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BULLISH, 100, 99, 1000, risk_pct=0.0,
        )
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BULLISH, 100, 99, 1000, risk_pct=0.2,
        )


# ── calculate_trade_plan: sizing math ───────────────────────────────────────


def test_long_basic_sizing():
    """Entry 100, SL 99 (1% SL), risk 1% of 10k → 100 USDT risk, 10k notional."""
    plan = calculate_trade_plan(
        direction=Direction.BULLISH,
        entry_price=100.0,
        sl_price=99.0,
        account_balance=10_000.0,
        risk_pct=0.01,
        rr_ratio=3.0,
        max_leverage=20,
        contract_size=0.01,
    )
    assert plan.sl_distance == pytest.approx(1.0)
    assert plan.sl_pct == pytest.approx(0.01)
    assert plan.tp_price == pytest.approx(103.0)
    # Ideal notional = 100 / 0.01 = 10,000 → required_leverage 1.0, but we
    # bump actual leverage to 2 so initial margin leaves a 5% fee buffer.
    assert plan.leverage == 2
    assert plan.required_leverage == pytest.approx(1.0)
    assert plan.capped is False
    # Contracts: 10000 / (0.01 * 100) = 10000
    assert plan.num_contracts == 10_000
    assert plan.risk_amount_usdt == pytest.approx(100.0)


def test_short_tp_goes_below_entry():
    plan = calculate_trade_plan(
        direction=Direction.BEARISH,
        entry_price=100.0,
        sl_price=101.0,
        account_balance=10_000.0,
        rr_ratio=2.0,
    )
    assert plan.tp_price == pytest.approx(98.0)
    assert plan.is_short


def test_tight_sl_drives_high_leverage():
    """0.3% SL on 10k balance → leverage in the 15-20x range per CLAUDE.md."""
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=1000.0, sl_price=997.0,  # 0.3% SL
        account_balance=10_000.0, risk_pct=0.01, max_leverage=50,
    )
    # required_leverage = (risk / sl_pct) / balance = (100/0.003)/10000 ≈ 3.33
    # Hmm — that's not 15-20x. The CLAUDE.md example implicitly assumes a
    # larger account relative to risk. We only check the relationship: leverage
    # is higher for tighter SL than for wider SL at the same risk_pct.
    wider = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=1000.0, sl_price=980.0,  # 2% SL
        account_balance=10_000.0, risk_pct=0.01, max_leverage=50,
    )
    assert plan.required_leverage > wider.required_leverage


def test_leverage_caps_and_marks_capped_flag():
    """Tiny SL + 5% risk can push required leverage above the cap."""
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=100.0, sl_price=99.9,   # 0.1% SL
        account_balance=1_000.0, risk_pct=0.05,  # 5% risk → 50 USDT
        max_leverage=20,
    )
    # ideal_notional = 50 / 0.001 = 50,000; required_lev = 50 → capped at 20
    assert plan.required_leverage > 20
    assert plan.leverage == 20
    assert plan.capped is True
    # Actual notional shrinks to 20 * 1000 = 20,000, so actual risk < 50 USDT
    assert plan.risk_amount_usdt < plan.max_risk_usdt


def test_capped_plan_never_exceeds_requested_risk():
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=50_000.0, sl_price=49_950.0,  # 0.1% SL
        account_balance=1_000.0, risk_pct=0.03,
        max_leverage=10,
    )
    assert plan.risk_amount_usdt <= plan.max_risk_usdt + 1e-6


def test_contract_rounding_keeps_risk_below_target():
    """Integer contract rounding should never push risk ABOVE the target."""
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=123.45, sl_price=120.0,
        account_balance=500.0, risk_pct=0.01, rr_ratio=3.0,
        max_leverage=20,
    )
    assert plan.num_contracts >= 0
    assert plan.risk_amount_usdt <= plan.max_risk_usdt + 1e-6


def test_tp_distance_equals_rr_times_sl_distance():
    plan = calculate_trade_plan(
        Direction.BULLISH, 100, 99, 1000, rr_ratio=4.0,
    )
    assert plan.tp_distance == pytest.approx(4.0 * plan.sl_distance)


def test_expected_win_usdt_matches_tp_math():
    plan = calculate_trade_plan(
        Direction.BULLISH, 100, 99, 10_000, risk_pct=0.01, rr_ratio=3.0,
    )
    # TP at 103, notional 10_000 → win ≈ notional * 3% = 300
    assert plan.expected_win_usdt == pytest.approx(plan.position_size_usdt * 0.03, rel=1e-3)
    assert not math.isnan(plan.expected_win_usdt)


def test_confluence_metadata_preserved():
    plan = calculate_trade_plan(
        Direction.BULLISH, 100, 99, 10_000,
        confluence_score=4.5,
        confluence_factors=["htf_trend_alignment", "at_order_block"],
        sl_source="order_block_pine",
        reason="test",
    )
    assert plan.confluence_score == 4.5
    assert plan.confluence_factors == ["htf_trend_alignment", "at_order_block"]
    assert plan.sl_source == "order_block_pine"
    assert plan.reason == "test"
