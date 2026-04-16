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
