"""Phase 7.D1 — displacement_candle factor + premium_discount_zone veto.

Scope:
  * `_displacement_in_direction` helper — direction+magnitude filter over
    a candle tail, used as section 9c in `score_direction`.
  * `_premium_discount_veto` helper — midpoint-of-swing gate used before
    plan construction in `build_trade_plan_with_reason`.
  * Integration round-trip: displacement factor shows up in confluence
    output, and the premium/discount veto surfaces the pivot-era reject
    reason `wrong_side_of_premium_discount`.
"""

from __future__ import annotations

from src.analysis.multi_timeframe import (
    _displacement_in_direction,
    calculate_confluence,
    score_direction,
)
from src.data.candle_buffer import Candle
from src.data.models import (
    Direction,
    MarketState,
    OrderBlock,
    OscillatorTableData,
    SignalTableData,
)
from src.strategy.entry_signals import (
    _premium_discount_veto,
    build_trade_plan_with_reason,
)


# ── _displacement_in_direction ──────────────────────────────────────────────


def _body_candle(direction: Direction, body: float, base: float = 100.0) -> Candle:
    """Make a Candle whose body equals `body` and orientation matches `direction`."""
    if direction == Direction.BULLISH:
        return Candle(open=base, high=base + body + 0.5, low=base - 0.1,
                      close=base + body)
    return Candle(open=base, high=base + 0.1, low=base - body - 0.5,
                  close=base - body)


def test_displacement_fresh_bullish_body_detected():
    # Tail: [noise, noise, big-bull]. Threshold = 1.0 * 1.5 = 1.5 ATR.
    small = Candle(open=100.0, high=100.3, low=99.9, close=100.1)   # body 0.1
    big = _body_candle(Direction.BULLISH, body=2.5)                 # 2.5 ATR
    result = _displacement_in_direction(
        [small, small, big], Direction.BULLISH,
        atr=1.0, atr_mult=1.5, max_bars_ago=5,
    )
    assert result is not None
    bars_ago, body_atr = result
    assert bars_ago == 0                         # most recent
    assert body_atr >= 1.5


def test_displacement_counter_direction_body_rejected():
    # Big bearish body should NOT qualify a bullish displacement search.
    big_bear = _body_candle(Direction.BEARISH, body=3.0)
    result = _displacement_in_direction(
        [big_bear], Direction.BULLISH,
        atr=1.0, atr_mult=1.5, max_bars_ago=5,
    )
    assert result is None


def test_displacement_body_below_threshold_rejected():
    # Bullish body of 1.0 ATR < 1.5 ATR threshold.
    weak = _body_candle(Direction.BULLISH, body=1.0)
    result = _displacement_in_direction(
        [weak], Direction.BULLISH,
        atr=1.0, atr_mult=1.5, max_bars_ago=5,
    )
    assert result is None


def test_displacement_old_bar_outside_window_rejected():
    # Big bullish body 10 bars back — max_bars_ago=3 → not found.
    noise = Candle(open=100.0, high=100.2, low=99.8, close=100.05)
    big = _body_candle(Direction.BULLISH, body=3.0)
    tail = [big] + [noise] * 10
    result = _displacement_in_direction(
        tail, Direction.BULLISH,
        atr=1.0, atr_mult=1.5, max_bars_ago=3,
    )
    assert result is None


def test_displacement_missing_atr_fails_open():
    big = _body_candle(Direction.BULLISH, body=3.0)
    assert _displacement_in_direction(
        [big], Direction.BULLISH, atr=0.0, atr_mult=1.5, max_bars_ago=5,
    ) is None


def test_displacement_empty_candles_fails_open():
    assert _displacement_in_direction(
        None, Direction.BULLISH, atr=1.0, atr_mult=1.5, max_bars_ago=5,
    ) is None
    assert _displacement_in_direction(
        [], Direction.BULLISH, atr=1.0, atr_mult=1.5, max_bars_ago=5,
    ) is None


# ── displacement surfaces through score_direction ───────────────────────────


def _bullish_state(price: float = 100.0, atr: float = 1.0) -> MarketState:
    sig = SignalTableData(
        trend_htf=Direction.BULLISH,
        last_mss="BULLISH@99",
        active_ob="BULL@95-97",
        vmc_ribbon="BULLISH",
        price=price,
        atr_14=atr,
    )
    return MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=[OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)],
        fvg_zones=[],
    )


def test_score_direction_emits_displacement_factor():
    state = _bullish_state()
    big = _body_candle(Direction.BULLISH, body=3.0)       # 3.0 ATR body
    score = score_direction(
        state, Direction.BULLISH, ltf_candles=[big],
        displacement_atr_mult=1.5, displacement_max_bars_ago=5,
    )
    names = [f.name for f in score.factors]
    assert "displacement_candle" in names


def test_score_direction_omits_displacement_when_counter():
    state = _bullish_state()
    big_bear = _body_candle(Direction.BEARISH, body=3.0)
    score = score_direction(
        state, Direction.BULLISH, ltf_candles=[big_bear],
        displacement_atr_mult=1.5, displacement_max_bars_ago=5,
    )
    assert "displacement_candle" not in [f.name for f in score.factors]


def test_calculate_confluence_plumbs_displacement_kwargs():
    # Same round-trip via calculate_confluence (the pipeline entry point).
    state = _bullish_state()
    big = _body_candle(Direction.BULLISH, body=3.0)
    conf = calculate_confluence(
        state, ltf_candles=[big],
        displacement_atr_mult=1.5, displacement_max_bars_ago=5,
    )
    assert "displacement_candle" in conf.factor_names


# ── _premium_discount_veto ──────────────────────────────────────────────────


def _range_candles(low: float, high: float, count: int = 40) -> list[Candle]:
    """Fake swing range: alternating low/high candles so hi/lo = (high, low)."""
    out = []
    for i in range(count):
        if i % 2 == 0:
            out.append(Candle(open=low + 0.1, high=high, low=low, close=low + 0.2))
        else:
            out.append(Candle(open=high - 0.2, high=high, low=low, close=high - 0.1))
    return out


def test_premium_discount_veto_blocks_long_above_midpoint():
    # Range 90-110, midpoint 100. Long at 105 = premium side → veto.
    candles = _range_candles(low=90.0, high=110.0)
    assert _premium_discount_veto(candles, Direction.BULLISH, price=105.0) is True


def test_premium_discount_veto_allows_long_below_midpoint():
    candles = _range_candles(low=90.0, high=110.0)
    assert _premium_discount_veto(candles, Direction.BULLISH, price=95.0) is False


def test_premium_discount_veto_blocks_short_below_midpoint():
    # Range 90-110, midpoint 100. Short at 95 = discount side → veto.
    candles = _range_candles(low=90.0, high=110.0)
    assert _premium_discount_veto(candles, Direction.BEARISH, price=95.0) is True


def test_premium_discount_veto_allows_short_above_midpoint():
    candles = _range_candles(low=90.0, high=110.0)
    assert _premium_discount_veto(candles, Direction.BEARISH, price=105.0) is False


def test_premium_discount_veto_exact_midpoint_passes():
    # Price == midpoint → not on wrong side → fail open (False).
    candles = _range_candles(low=90.0, high=110.0)
    assert _premium_discount_veto(candles, Direction.BULLISH, price=100.0) is False
    assert _premium_discount_veto(candles, Direction.BEARISH, price=100.0) is False


def test_premium_discount_veto_fails_open_on_missing_data():
    assert _premium_discount_veto(None, Direction.BULLISH, price=100.0) is False
    assert _premium_discount_veto([], Direction.BULLISH, price=100.0) is False
    # Single candle → degenerate (lookback < 2 requires at least 2 bars)
    one = [Candle(open=100.0, high=101.0, low=99.0, close=100.5)]
    assert _premium_discount_veto(one, Direction.BULLISH, price=100.0) is False


def test_premium_discount_veto_fails_open_on_degenerate_range():
    # All candles at identical prices → hi == lo → fail open.
    flat = [Candle(open=100.0, high=100.0, low=100.0, close=100.0)] * 10
    assert _premium_discount_veto(flat, Direction.BULLISH, price=100.0) is False


def test_premium_discount_veto_disabled_by_default_in_pipeline():
    # When the flag is off, a premium long still goes through.
    candles = _range_candles(low=90.0, high=110.0)
    sig = SignalTableData(
        trend_htf=Direction.BULLISH,
        last_mss="BULLISH@99",
        active_ob="BULL@95-97",
        vmc_ribbon="BULLISH",
        price=105.0,
        atr_14=1.0,
    )
    state = MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=[OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)],
        fvg_zones=[],
    )
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0, candles=candles,
        # flag omitted → default False
    )
    assert plan is not None
    assert reason == ""


def test_premium_discount_veto_enabled_rejects_in_pipeline():
    # Same setup, flag on → rejected.
    candles = _range_candles(low=90.0, high=110.0)
    sig = SignalTableData(
        trend_htf=Direction.BULLISH,
        last_mss="BULLISH@99",
        active_ob="BULL@95-97",
        vmc_ribbon="BULLISH",
        price=105.0,
        atr_14=1.0,
    )
    state = MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=[OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0)],
        fvg_zones=[],
    )
    plan, reason = build_trade_plan_with_reason(
        state, account_balance=10_000.0, candles=candles,
        premium_discount_veto_enabled=True, premium_discount_lookback=40,
    )
    assert plan is None
    assert reason == "wrong_side_of_premium_discount"
