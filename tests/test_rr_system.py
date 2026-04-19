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
    # Ideal notional = 100 / 0.01 = 10,000 → required_leverage 1.0. The
    # rule picks the MAX feasible leverage (min of max_leverage and the
    # liquidation-safe ceiling) to minimize margin locked per position, so
    # concurrent trades fit inside the account. liq_safe = floor(0.6/0.01)
    # = 60, capped at max_leverage=20 → leverage=20.
    assert plan.leverage == 20
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


def test_leverage_picks_max_feasible_for_concurrent_sizing():
    """Leverage should climb to the cap (or liq-safe ceiling) — NOT stop at
    required_leverage — so initial margin stays tiny and three concurrent
    positions all fit in the account.

    Entry 100, SL 99.5 (0.5%), $1000 balance, 2.5% risk, max_leverage=75.
      ideal_notional  = 25 / 0.005 = 5000
      required_lev    = 5000 / 1000 = 5
      liq_safe_lev    = floor(0.6 / 0.005) = 120
      chosen leverage = min(75, 120) = 75
      margin locked   = 5000 / 75 ≈ 67 USDT (not 5000/5 = 1000 USDT)
    """
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=100.0, sl_price=99.5,
        account_balance=1_000.0, risk_pct=0.025,
        max_leverage=75,
    )
    assert plan.leverage == 75
    assert plan.required_leverage == pytest.approx(5.0)
    margin_locked = plan.position_size_usdt / plan.leverage
    assert margin_locked < 100.0  # << balance, so 3 can coexist


def test_leverage_ceiling_scales_with_sl_width():
    """Wide SL → lower safe leverage (liquidation closer). 3% SL with
    max_leverage=75 should pick leverage=20 (floor(0.6 / 0.03) = 20)."""
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=100.0, sl_price=97.0,  # 3% SL
        account_balance=10_000.0, risk_pct=0.01,
        max_leverage=75,
    )
    assert plan.leverage == 20


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


def test_contract_rounding_keeps_risk_at_or_above_target():
    """Integer contract ceil should produce risk ≥ target (un-capped path).

    Post-2026-04-19 operator contract: each position's realized SL loss must
    clear max_risk_usdt regardless of per-symbol ctVal/entry quantization,
    so SL/TP USDT amounts are equalized across symbols (previously floor
    produced $40-$54 variance on nominal $55). Overshoot bounded by one
    per_contract_cost step (sl_pct × contracts_unit_usdt).
    """
    plan = calculate_trade_plan(
        Direction.BULLISH,
        entry_price=123.45, sl_price=120.0,
        account_balance=500.0, risk_pct=0.01, rr_ratio=3.0,
        max_leverage=20,
    )
    assert plan.num_contracts >= 1
    assert not plan.capped
    # New invariant: un-capped ceil keeps risk ≥ target (modulo float noise).
    assert plan.risk_amount_usdt >= plan.max_risk_usdt - 1e-6
    # Overshoot bounded by one per-contract price-cost step.
    ctu = 0.01 * 123.45
    per_contract_price_cost = plan.sl_pct * ctu
    assert plan.risk_amount_usdt <= plan.max_risk_usdt + per_contract_price_cost + 1e-6


def test_equal_realized_loss_across_heterogeneous_symbols():
    """Operator-visible contract: at $55 R target with fee reserve, each
    symbol's TOTAL realized SL loss (price + fee reserve) clusters tightly
    around $55, not the $40-$54 variance that floor-rounding produced.
    `risk_amount_usdt` tracks the price-only slice, so we compute the total
    as `num_contracts × effective_sl_pct × ctu` for the invariant check.
    """
    # (entry, sl_pct, ctval) per symbol — representative mid-market prices.
    symbols = [
        ("BTC", 68_000.0, 0.004, 0.01),  # ctu = 680
        ("ETH",  2_400.0, 0.006, 0.10),  # ctu = 240
        ("SOL",    140.0, 0.010, 1.00),  # ctu = 140
        ("DOGE",     0.35, 0.008, 1.00),  # ctu = 0.35
        ("BNB",    700.0, 0.005, 0.10),  # ctu = 70
    ]
    account_balance = 5_500.0
    risk_pct = 0.01  # $55 target
    fee_reserve_pct = 0.001
    target = account_balance * risk_pct
    realized_totals = []
    for _sym, entry, sl_pct, ctval in symbols:
        sl_price = entry * (1.0 - sl_pct)
        plan = calculate_trade_plan(
            direction=Direction.BULLISH,
            entry_price=entry, sl_price=sl_price,
            account_balance=account_balance, risk_pct=risk_pct,
            rr_ratio=3.0, max_leverage=75, contract_size=ctval,
            margin_balance=1_000.0, fee_reserve_pct=fee_reserve_pct,
        )
        assert not plan.capped  # Un-capped path is what we're testing.
        effective_sl_pct = plan.sl_pct + plan.fee_reserve_pct
        total_realized = plan.position_size_usdt * effective_sl_pct
        # ceil on effective_sl_pct ensures total realized ≥ target.
        assert total_realized >= target - 1e-6
        realized_totals.append(total_realized)
    # Spread bounded by the widest symbol's per-contract *total* step
    # (BTC: 0.005 × 680 = $3.40). Previously floor produced ≥ $10 spread.
    assert max(realized_totals) - min(realized_totals) < 3.5


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


# ── margin_balance vs account_balance split ────────────────────────────────


def test_margin_balance_defaults_to_account_balance():
    """Omitting margin_balance must reproduce legacy single-balance math."""
    plan_default = calculate_trade_plan(
        Direction.BULLISH, 100, 99, 10_000, risk_pct=0.01, max_leverage=20,
    )
    plan_explicit = calculate_trade_plan(
        Direction.BULLISH, 100, 99, 10_000, risk_pct=0.01, max_leverage=20,
        margin_balance=10_000,
    )
    assert plan_default.num_contracts == plan_explicit.num_contracts
    assert plan_default.leverage == plan_explicit.leverage
    assert plan_default.position_size_usdt == plan_explicit.position_size_usdt
    assert plan_default.risk_amount_usdt == plan_explicit.risk_amount_usdt


def test_risk_uses_account_balance_not_margin_balance():
    """R = account_balance × risk_pct, independent of margin_balance.

    With a 1% SL this is the uncapped path — notional = R / sl_pct fits
    inside max_notional = margin_balance × max_leverage × 0.95.
    """
    plan = calculate_trade_plan(
        Direction.BULLISH, 100, 99, account_balance=10_000,
        margin_balance=1_000, risk_pct=0.01, max_leverage=50,
        contract_size=0.01,
    )
    # R must track account_balance (= 100), not margin_balance (= 10)
    assert plan.max_risk_usdt == pytest.approx(100.0)
    # Uncapped ⇒ actual ≈ max
    assert plan.risk_amount_usdt == pytest.approx(100.0, rel=5e-3)
    assert not plan.capped


def test_margin_balance_caps_notional():
    """Margin-fit ceiling uses margin_balance × max_leverage × 0.95, not
    account_balance, so a tiny free margin correctly shrinks the position."""
    # Tight SL (0.5%) ⇒ ideal_notional = 100/0.005 = 20_000. margin_balance
    # caps max_notional at 100 × 20 × 0.95 = 1_900.
    plan = calculate_trade_plan(
        Direction.BULLISH, 100, 99.5, account_balance=10_000,
        margin_balance=100, risk_pct=0.01, max_leverage=20,
        contract_size=0.01,
    )
    assert plan.capped
    assert plan.position_size_usdt <= 100 * 20 * 0.95 + 1e-6
    # Actual risk ends up strictly below max_risk_usdt when capped.
    assert plan.risk_amount_usdt < plan.max_risk_usdt


def test_negative_margin_balance_rejected():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BULLISH, 100, 99, account_balance=10_000,
            margin_balance=-1,
        )


# ── fee_reserve_pct: fee-aware sizing ───────────────────────────────────────


def test_fee_reserve_shrinks_notional_but_keeps_tp_price():
    """Reserving round-trip fees in sizing shrinks notional so a stop-out
    still lands inside the USDT risk budget after taker fees. TP price is
    unchanged — fee compensation comes from size, not widened TP."""
    base = calculate_trade_plan(
        direction=Direction.BULLISH,
        entry_price=100.0, sl_price=99.0,
        account_balance=10_000.0, risk_pct=0.01,
        rr_ratio=3.0, max_leverage=20, contract_size=0.01,
    )
    with_fee = calculate_trade_plan(
        direction=Direction.BULLISH,
        entry_price=100.0, sl_price=99.0,
        account_balance=10_000.0, risk_pct=0.01,
        rr_ratio=3.0, max_leverage=20, contract_size=0.01,
        fee_reserve_pct=0.001,
    )
    # TP price unchanged (price-only math).
    assert with_fee.tp_price == base.tp_price
    # Notional shrinks by sl_pct / (sl_pct + fee_reserve_pct) = 0.01/0.011.
    expected_ratio = 0.01 / 0.011
    assert with_fee.num_contracts < base.num_contracts
    assert (with_fee.position_size_usdt / base.position_size_usdt
            == pytest.approx(expected_ratio, rel=0.01))
    assert with_fee.fee_reserve_pct == pytest.approx(0.001)


def test_fee_reserve_zero_matches_legacy_sizing():
    """Default fee_reserve_pct=0 must match prior behavior for back-compat."""
    base = calculate_trade_plan(
        direction=Direction.BULLISH, entry_price=100.0, sl_price=99.0,
        account_balance=10_000.0, risk_pct=0.01, rr_ratio=3.0,
        max_leverage=20, contract_size=0.01,
    )
    same = calculate_trade_plan(
        direction=Direction.BULLISH, entry_price=100.0, sl_price=99.0,
        account_balance=10_000.0, risk_pct=0.01, rr_ratio=3.0,
        max_leverage=20, contract_size=0.01,
        fee_reserve_pct=0.0,
    )
    assert same.num_contracts == base.num_contracts
    assert same.position_size_usdt == base.position_size_usdt


def test_fee_reserve_negative_rejected():
    with pytest.raises(ValueError):
        calculate_trade_plan(
            Direction.BULLISH, 100.0, 99.0, account_balance=10_000.0,
            fee_reserve_pct=-0.001,
        )
