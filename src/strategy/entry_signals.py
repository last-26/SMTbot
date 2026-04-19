"""Entry signal generation: MarketState + confluence → EntryIntent → TradePlan.

This module is the per-candle brain of the bot:

  1. Run confluence scoring (Phase 2 capstone) to pick a direction + score.
  2. Reject if score < min_confluence or direction is UNDEFINED.
  3. Pick an SL source by preference: Pine OB → Pine FVG → Python OB →
     Python FVG → swing lookback → ATR fallback.
  4. Build a TradePlan via `calculate_trade_plan` (pure math).
  5. Enforce min_rr_ratio one more time at the end.

The orchestration layer (`src/bot/`) calls `build_trade_plan_from_state`
once per poll. If it returns None, we sit the bar out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.analysis.fvg import FVG
from src.analysis.multi_timeframe import (
    ConfluenceScore,
    calculate_confluence,
)
from src.analysis.order_blocks import OrderBlock as PyOrderBlock
from src.analysis.support_resistance import SRZone
from src.analysis.trend_regime import TrendRegime
from src.data.candle_buffer import Candle
from src.data.models import (
    Direction,
    FVGZone,
    MarketState,
    OrderBlock,
    Session,
)
from src.strategy.position_sizer import (
    recent_swing_price,
    sl_from_atr,
    sl_from_fvg,
    sl_from_order_block,
    sl_from_swing,
)
from src.strategy.rr_system import calculate_trade_plan
from src.strategy.trade_plan import TradePlan


# ── HTF S/R helpers (Madde D) ───────────────────────────────────────────────
#
# These push the raw SL past any HTF S/R zone sitting between entry and SL
# (so price has to break the zone before stopping us out), and cap the TP
# below/above the next HTF zone in the profit direction (so we bank profit
# instead of getting front-run at resistance). Both are pure functions of
# the geometry — no I/O, easy to unit-test.


def _push_sl_past_htf_zone(
    sl: float, entry: float, direction: Direction,
    htf_zones: list[SRZone], buffer_atr: float, atr: float,
) -> float:
    """Snap SL to just past any HTF zone sitting between SL and entry.

    Bullish long: entry=100, sl=96, HTF support at 97-98 → snap sl to
    97 - buffer (just below the zone). Bearish short: symmetric. Only
    ever *tightens* the stop toward entry — never widens risk.
    """
    if not htf_zones or atr <= 0:
        return sl
    buf = buffer_atr * atr
    new_sl = sl
    for z in htf_zones:
        if direction == Direction.BULLISH:
            # Zone fully between SL and entry → SL can tighten up to z.bottom-buf
            if new_sl < z.bottom and z.top < entry:
                new_sl = max(new_sl, z.bottom - buf)
        elif direction == Direction.BEARISH:
            # Zone fully between entry and SL → SL can tighten down to z.top+buf
            if new_sl > z.top and z.bottom > entry:
                new_sl = min(new_sl, z.top + buf)
    return new_sl


def _apply_htf_tp_ceiling(
    tp: float, entry: float, direction: Direction,
    htf_zones: list[SRZone], buffer_atr: float, atr: float,
) -> float:
    """Cap TP so we don't place it past an HTF zone in the profit direction."""
    if not htf_zones or atr <= 0:
        return tp
    buf = buffer_atr * atr
    new_tp = tp
    for z in htf_zones:
        if direction == Direction.BULLISH and z.role in ("RESISTANCE", "MIXED"):
            # HTF resistance between entry and TP → pull TP down
            if entry < z.bottom < new_tp:
                new_tp = min(new_tp, z.bottom - buf)
        elif direction == Direction.BEARISH and z.role in ("SUPPORT", "MIXED"):
            # HTF support between TP and entry → pull TP up
            if new_tp < z.top < entry:
                new_tp = max(new_tp, z.top + buf)
    return new_tp


# ── Intent (pre-sizing) ─────────────────────────────────────────────────────


@dataclass
class EntryIntent:
    """What we want to trade, before position sizing.

    Produced by `generate_entry_intent`. If an SL source is unavailable
    (no structural level AND no ATR), `sl_price` is None and the intent
    is not tradable.
    """
    direction: Direction
    entry_price: float
    sl_price: Optional[float]
    sl_source: str
    atr: float
    confluence: ConfluenceScore
    notes: str = ""

    @property
    def is_tradable(self) -> bool:
        return (
            self.direction in (Direction.BULLISH, Direction.BEARISH)
            and self.sl_price is not None
            and self.entry_price > 0
        )


# ── SL source selection ─────────────────────────────────────────────────────


def _best_ob_for_long(obs, entry: float):
    """Closest active long OB whose top is below entry."""
    below = [o for o in obs
             if o.direction == Direction.BULLISH
             and getattr(o, "status", "ACTIVE") == "ACTIVE"
             and o.top < entry]
    return max(below, key=lambda o: o.top) if below else None


def _best_ob_for_short(obs, entry: float):
    """Closest active short OB whose bottom is above entry."""
    above = [o for o in obs
             if o.direction == Direction.BEARISH
             and getattr(o, "status", "ACTIVE") == "ACTIVE"
             and o.bottom > entry]
    return min(above, key=lambda o: o.bottom) if above else None


def _best_fvg_for_long(fvgs, entry: float):
    below = [f for f in fvgs
             if f.direction == Direction.BULLISH
             and getattr(f, "status", "ACTIVE") == "ACTIVE"
             and f.top < entry]
    return max(below, key=lambda f: f.top) if below else None


def _best_fvg_for_short(fvgs, entry: float):
    above = [f for f in fvgs
             if f.direction == Direction.BEARISH
             and getattr(f, "status", "ACTIVE") == "ACTIVE"
             and f.bottom > entry]
    return min(above, key=lambda f: f.bottom) if above else None


def select_sl_price(
    state: MarketState,
    direction: Direction,
    entry_price: float,
    atr: float,
    candles: Optional[list[Candle]] = None,
    python_order_blocks: Optional[list[PyOrderBlock]] = None,
    python_fvgs: Optional[list[FVG]] = None,
    buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
) -> tuple[Optional[float], str]:
    """Return (sl_price, source_label). Source "" when we can't place an SL."""
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return None, ""
    if atr <= 0 or entry_price <= 0:
        return None, ""

    # 1. Pine-derived OB drawings on the chart
    pine_obs: list[OrderBlock] = state.order_blocks
    pick = (
        _best_ob_for_long(pine_obs, entry_price)
        if direction == Direction.BULLISH
        else _best_ob_for_short(pine_obs, entry_price)
    )
    if pick is not None:
        return sl_from_order_block(pick, atr, direction, buffer_mult), "order_block_pine"

    # 2. Pine-derived FVG drawings
    pine_fvgs: list[FVGZone] = state.fvg_zones
    pick = (
        _best_fvg_for_long(pine_fvgs, entry_price)
        if direction == Direction.BULLISH
        else _best_fvg_for_short(pine_fvgs, entry_price)
    )
    if pick is not None:
        return sl_from_fvg(pick, atr, direction, buffer_mult), "fvg_pine"

    # 3. Python-side OB (used when HTF isn't on the chart)
    if python_order_blocks:
        pick = (
            _best_ob_for_long(python_order_blocks, entry_price)
            if direction == Direction.BULLISH
            else _best_ob_for_short(python_order_blocks, entry_price)
        )
        if pick is not None:
            return sl_from_order_block(pick, atr, direction, buffer_mult), "order_block_py"

    # 4. Python-side FVG
    if python_fvgs:
        pick = (
            _best_fvg_for_long(python_fvgs, entry_price)
            if direction == Direction.BULLISH
            else _best_fvg_for_short(python_fvgs, entry_price)
        )
        if pick is not None:
            return sl_from_fvg(pick, atr, direction, buffer_mult), "fvg_py"

    # 5. Swing lookback from the candle buffer
    swing = recent_swing_price(candles or [], direction, lookback=swing_lookback)
    if swing is not None:
        # Sanity: swing must be on the invalidation side of entry
        if direction == Direction.BULLISH and swing < entry_price:
            return sl_from_swing(swing, atr, direction, buffer_mult), "swing"
        if direction == Direction.BEARISH and swing > entry_price:
            return sl_from_swing(swing, atr, direction, buffer_mult), "swing"

    # 6. ATR fallback
    return sl_from_atr(entry_price, atr, direction, atr_fallback_mult), "atr_fallback"


# ── VWAP hard veto (Phase 6.9 A4) ───────────────────────────────────────────
#
# The confluence scorer already awards 3 small factors (vwap_{1,3,15}m_alignment)
# when price sits on the right side of each session-anchored VWAP. But with
# min_confluence_score=3.0 a trade can still pass after being on the wrong side
# of every VWAP, since structural factors (MSS/OB/FVG) and sweeps are weighed
# independently. On a liquidity-driven futures book, entering into a wall of
# VWAPs is statistically bad — operator can flip this on to hard-reject such
# trades pre-SL math.
#
# Semantics (strict): bullish entry rejected when price < min(available VWAPs);
# bearish when price > max(...). Missing (0.0) VWAPs are skipped. Requires at
# least one VWAP to be present, otherwise no-op (fail-open — can't judge).


def _vwap_hard_veto(state: MarketState, direction: Direction, price: float) -> bool:
    sig = state.signal_table
    vwaps = [v for v in (sig.vwap_1m, sig.vwap_3m, sig.vwap_15m) if v > 0.0]
    if not vwaps or price <= 0:
        return False
    if direction == Direction.BULLISH:
        return price < min(vwaps)
    if direction == Direction.BEARISH:
        return price > max(vwaps)
    return False


# ── EMA 21/55 momentum veto (Phase 7.A5) ────────────────────────────────────
#
# Sprint 3 diagnostic: the bot repeatedly fired entries against the entry-TF
# momentum stack — MSS-driven signals don't read the EMA trend. A clean
# bull stack (price > EMA21 > EMA55) makes a bearish short statistically
# worse; same for the mirror. Gate rejects trades that oppose the local
# momentum regime; neutral / insufficient data fails open (no veto).


def _ema(values: list[float], period: int) -> Optional[float]:
    """EMA of the last `period` samples. Returns None when input is shorter
    than the period (not enough data to seed a stable EMA)."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period        # SMA seed
    for price in values[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def _ema_momentum_veto(
    candles: Optional[list[Candle]],
    direction: Direction,
    price: float,
    fast_period: int = 21,
    slow_period: int = 55,
) -> bool:
    """True → reject. Short stack blocks BULLISH; long stack blocks BEARISH.
    Neutral stack or missing data → False (fail-open)."""
    if not candles or price <= 0:
        return False
    closes = [c.close for c in candles if getattr(c, "close", None) is not None]
    ema_fast = _ema(closes, fast_period)
    ema_slow = _ema(closes, slow_period)
    if ema_fast is None or ema_slow is None:
        return False
    bull_stack = price > ema_fast > ema_slow
    bear_stack = price < ema_fast < ema_slow
    if direction == Direction.BULLISH and bear_stack:
        return True
    if direction == Direction.BEARISH and bull_stack:
        return True
    return False


# ── Cross-asset pillar opposition (Phase 7.A6) ─────────────────────────────
#
# BTC + ETH set the crypto-market tape; altcoin trades that fight *both*
# pillars have a poor edge. Veto fires only when both pillars are fresh and
# opposite to the trade direction. Missing / neutral / stale data fails
# open. Caller supplies the pillar bias dict; planner stays pure.


def _premium_discount_veto(
    candles: Optional[list[Candle]],
    direction: Direction,
    price: float,
    lookback: int = 40,
) -> bool:
    """True → reject: entry is on the wrong side of premium/discount.

    Swing midpoint = (last N-bar high + last N-bar low) / 2. Longs should
    enter at a *discount* (price ≤ midpoint). Shorts should enter at a
    *premium* (price ≥ midpoint). Buying premium / selling discount is the
    "chase the move" pattern the pivot wants to block.

    Missing candles / zero price / degenerate range → fail-open (False).
    """
    if not candles or price <= 0 or lookback < 2:
        return False
    tail = candles[-lookback:] if len(candles) >= lookback else list(candles)
    if len(tail) < 2:
        return False
    hi = max(c.high for c in tail if c.high > 0)
    lo = min(c.low for c in tail if c.low > 0)
    if hi <= lo:
        return False
    midpoint = (hi + lo) / 2.0
    if direction == Direction.BULLISH and price > midpoint:
        return True
    if direction == Direction.BEARISH and price < midpoint:
        return True
    return False


def _cross_asset_opposes(
    pillar_opposition: Optional[Direction],
    direction: Direction,
) -> bool:
    """True when caller supplied an opposition signal matching this direction.

    `pillar_opposition` is Direction.BULLISH when both pillars are BULLISH
    (i.e. blocks a BEARISH entry), Direction.BEARISH when both are BEARISH
    (blocks a BULLISH entry), or None/UNDEFINED when the caller decided
    no veto applies.
    """
    if pillar_opposition is None or pillar_opposition == Direction.UNDEFINED:
        return False
    if direction == Direction.BULLISH and pillar_opposition == Direction.BEARISH:
        return True
    if direction == Direction.BEARISH and pillar_opposition == Direction.BULLISH:
        return True
    return False


# ── Full pipeline ───────────────────────────────────────────────────────────


def generate_entry_intent(
    state: MarketState,
    candles: Optional[list[Candle]] = None,
    python_fvgs: Optional[list[FVG]] = None,
    python_order_blocks: Optional[list[PyOrderBlock]] = None,
    sr_zones: Optional[list[SRZone]] = None,
    weights: Optional[dict[str, float]] = None,
    allowed_sessions: Optional[list[Session]] = None,
    min_confluence_score: float = 2.0,
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
    ltf_state: Optional[object] = None,
    min_rsi_mfi_magnitude: float = 2.0,
    liquidity_pool_max_atr_dist: float = 3.0,
    displacement_atr_mult: float = 1.5,
    displacement_max_bars_ago: int = 5,
    divergence_fresh_bars: int = 3,
    divergence_decay_bars: int = 6,
    divergence_max_bars: int = 9,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
) -> Optional[EntryIntent]:
    """Compute confluence + pick an SL. Returns None when not tradable."""
    if state.current_price <= 0:
        return None

    confluence = calculate_confluence(
        state,
        ltf_candles=candles,
        fvgs=python_fvgs,
        order_blocks=python_order_blocks,
        sr_zones=sr_zones,
        weights=weights,
        allowed_sessions=allowed_sessions,
        ltf_state=ltf_state,
        min_rsi_mfi_magnitude=min_rsi_mfi_magnitude,
        liquidity_pool_max_atr_dist=liquidity_pool_max_atr_dist,
        displacement_atr_mult=displacement_atr_mult,
        displacement_max_bars_ago=displacement_max_bars_ago,
        divergence_fresh_bars=divergence_fresh_bars,
        divergence_decay_bars=divergence_decay_bars,
        divergence_max_bars=divergence_max_bars,
        trend_regime=trend_regime,
        trend_regime_conditional_scoring_enabled=trend_regime_conditional_scoring_enabled,
    )
    if not confluence.is_tradable(min_confluence_score):
        return None

    entry_price = state.current_price
    sl_price, sl_source = select_sl_price(
        state=state,
        direction=confluence.direction,
        entry_price=entry_price,
        atr=state.atr,
        candles=candles,
        python_order_blocks=python_order_blocks,
        python_fvgs=python_fvgs,
        buffer_mult=sl_buffer_mult,
        swing_lookback=swing_lookback,
        atr_fallback_mult=atr_fallback_mult,
    )

    return EntryIntent(
        direction=confluence.direction,
        entry_price=entry_price,
        sl_price=sl_price,
        sl_source=sl_source,
        atr=state.atr,
        confluence=confluence,
    )


def _should_skip_for_derivatives(
    deriv_state,
    direction: Direction,
    crowded_skip_enabled: bool,
    crowded_skip_z_threshold: float,
) -> bool:
    """Crowded-skip gate (Phase 1.5 Madde 6).

    Blocks a BULLISH entry when the market is LONG_CROWDED and funding is
    historically hot (|z| ≥ threshold). Symmetric for shorts in a
    SHORT_CROWDED regime. Returns False when derivatives is absent or the
    gate is disabled — never blocks without data.
    """
    if not crowded_skip_enabled or deriv_state is None:
        return False
    regime = getattr(deriv_state, "regime", "UNKNOWN")
    funding_z = float(getattr(deriv_state, "funding_rate_zscore_30d", 0.0) or 0.0)
    if direction == Direction.BULLISH and regime == "LONG_CROWDED" \
            and funding_z >= crowded_skip_z_threshold:
        return True
    if direction == Direction.BEARISH and regime == "SHORT_CROWDED" \
            and funding_z <= -crowded_skip_z_threshold:
        return True
    return False


def build_trade_plan_from_state(
    state: MarketState,
    account_balance: float,
    *,
    candles: Optional[list[Candle]] = None,
    python_fvgs: Optional[list[FVG]] = None,
    python_order_blocks: Optional[list[PyOrderBlock]] = None,
    sr_zones: Optional[list[SRZone]] = None,
    weights: Optional[dict[str, float]] = None,
    allowed_sessions: Optional[list[Session]] = None,
    min_confluence_score: float = 2.0,
    risk_pct: float = 0.01,
    rr_ratio: float = 3.0,
    min_rr_ratio: float = 2.0,
    max_leverage: int = 20,
    contract_size: float = 0.01,
    margin_balance: Optional[float] = None,
    fee_reserve_pct: float = 0.0,
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
    htf_sr_zones: Optional[list[SRZone]] = None,
    htf_sr_ceiling_enabled: bool = False,
    htf_sr_buffer_atr: float = 0.2,
    crowded_skip_enabled: bool = False,
    crowded_skip_z_threshold: float = 3.0,
    ltf_state: Optional[object] = None,
    min_tp_distance_pct: float = 0.0,
    min_sl_distance_pct: float = 0.0,
    partial_tp_enabled: bool = False,
    partial_tp_ratio: float = 0.5,
    min_rsi_mfi_magnitude: float = 2.0,
    liquidity_pool_max_atr_dist: float = 3.0,
    vwap_hard_veto_enabled: bool = False,
    ema_veto_enabled: bool = False,
    ema_veto_fast_period: int = 21,
    ema_veto_slow_period: int = 55,
    pillar_opposition: Optional[Direction] = None,
    premium_discount_veto_enabled: bool = False,
    premium_discount_lookback: int = 40,
    displacement_atr_mult: float = 1.5,
    displacement_max_bars_ago: int = 5,
    divergence_fresh_bars: int = 3,
    divergence_decay_bars: int = 6,
    divergence_max_bars: int = 9,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
) -> Optional[TradePlan]:
    """End-to-end: MarketState → TradePlan. Returns None when no trade.

    `rr_ratio` is the target. `min_rr_ratio` is a hard floor — this function
    always honors the hard floor by erroring if the caller passed rr_ratio
    below it.

    When `htf_sr_ceiling_enabled`, SL is pushed past any HTF zone between
    entry and SL, and TP is capped in front of the next HTF zone in the
    profit direction. If the ceiling pushes R:R below `min_rr_ratio`, the
    plan is rejected (returns None).
    """
    plan, _reason = build_trade_plan_with_reason(
        state, account_balance,
        candles=candles,
        python_fvgs=python_fvgs,
        python_order_blocks=python_order_blocks,
        sr_zones=sr_zones,
        weights=weights,
        allowed_sessions=allowed_sessions,
        min_confluence_score=min_confluence_score,
        risk_pct=risk_pct,
        rr_ratio=rr_ratio,
        min_rr_ratio=min_rr_ratio,
        max_leverage=max_leverage,
        contract_size=contract_size,
        margin_balance=margin_balance,
        fee_reserve_pct=fee_reserve_pct,
        sl_buffer_mult=sl_buffer_mult,
        swing_lookback=swing_lookback,
        atr_fallback_mult=atr_fallback_mult,
        htf_sr_zones=htf_sr_zones,
        htf_sr_ceiling_enabled=htf_sr_ceiling_enabled,
        htf_sr_buffer_atr=htf_sr_buffer_atr,
        crowded_skip_enabled=crowded_skip_enabled,
        crowded_skip_z_threshold=crowded_skip_z_threshold,
        ltf_state=ltf_state,
        min_tp_distance_pct=min_tp_distance_pct,
        min_sl_distance_pct=min_sl_distance_pct,
        partial_tp_enabled=partial_tp_enabled,
        partial_tp_ratio=partial_tp_ratio,
        min_rsi_mfi_magnitude=min_rsi_mfi_magnitude,
        liquidity_pool_max_atr_dist=liquidity_pool_max_atr_dist,
        vwap_hard_veto_enabled=vwap_hard_veto_enabled,
        ema_veto_enabled=ema_veto_enabled,
        ema_veto_fast_period=ema_veto_fast_period,
        ema_veto_slow_period=ema_veto_slow_period,
        pillar_opposition=pillar_opposition,
        premium_discount_veto_enabled=premium_discount_veto_enabled,
        premium_discount_lookback=premium_discount_lookback,
        displacement_atr_mult=displacement_atr_mult,
        displacement_max_bars_ago=displacement_max_bars_ago,
        divergence_fresh_bars=divergence_fresh_bars,
        divergence_decay_bars=divergence_decay_bars,
        divergence_max_bars=divergence_max_bars,
        trend_regime=trend_regime,
        trend_regime_conditional_scoring_enabled=trend_regime_conditional_scoring_enabled,
    )
    return plan


def build_trade_plan_with_reason(
    state: MarketState,
    account_balance: float,
    *,
    candles: Optional[list[Candle]] = None,
    python_fvgs: Optional[list[FVG]] = None,
    python_order_blocks: Optional[list[PyOrderBlock]] = None,
    sr_zones: Optional[list[SRZone]] = None,
    weights: Optional[dict[str, float]] = None,
    allowed_sessions: Optional[list[Session]] = None,
    min_confluence_score: float = 2.0,
    risk_pct: float = 0.01,
    rr_ratio: float = 3.0,
    min_rr_ratio: float = 2.0,
    max_leverage: int = 20,
    contract_size: float = 0.01,
    margin_balance: Optional[float] = None,
    fee_reserve_pct: float = 0.0,
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
    htf_sr_zones: Optional[list[SRZone]] = None,
    htf_sr_ceiling_enabled: bool = False,
    htf_sr_buffer_atr: float = 0.2,
    crowded_skip_enabled: bool = False,
    crowded_skip_z_threshold: float = 3.0,
    ltf_state: Optional[object] = None,
    min_tp_distance_pct: float = 0.0,
    min_sl_distance_pct: float = 0.0,
    partial_tp_enabled: bool = False,
    partial_tp_ratio: float = 0.5,
    min_rsi_mfi_magnitude: float = 2.0,
    liquidity_pool_max_atr_dist: float = 3.0,
    vwap_hard_veto_enabled: bool = False,
    ema_veto_enabled: bool = False,
    ema_veto_fast_period: int = 21,
    ema_veto_slow_period: int = 55,
    pillar_opposition: Optional[Direction] = None,
    premium_discount_veto_enabled: bool = False,
    premium_discount_lookback: int = 40,
    displacement_atr_mult: float = 1.5,
    displacement_max_bars_ago: int = 5,
    divergence_fresh_bars: int = 3,
    divergence_decay_bars: int = 6,
    divergence_max_bars: int = 9,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
) -> tuple[Optional[TradePlan], str]:
    """Same as `build_trade_plan_from_state` but returns `(plan, reason)`.

    `reason` is `""` on success, otherwise one of:
      - "below_confluence" — confluence score below threshold or direction UNDEFINED
      - "session_filter"   — allowed_sessions excludes current session
      - "no_sl_source"     — intent produced no SL price
      - "vwap_misaligned"  — vwap_hard_veto_enabled and price on the wrong
        side of all available session VWAPs for the proposed direction
      - "ema_momentum_contra" — ema_veto_enabled and EMA stack opposes
        the proposed direction (bull stack + bearish entry, or vice versa)
      - "cross_asset_opposition" — BTC and ETH pillars both oppose the
        proposed altcoin direction (pillar_opposition set by caller)
      - "wrong_side_of_premium_discount" — premium_discount_veto_enabled
        and price sits on the wrong half of the N-bar swing range
        (long above midpoint, or short below midpoint)
      - "crowded_skip"     — derivatives crowded-skip gate blocked
      - "zero_contracts"   — contract rounding wiped position to zero
      - "htf_tp_ceiling"   — HTF S/R ceiling squeezed R:R below min_rr_ratio
      - "tp_too_tight"     — TP distance below min_tp_distance_pct (fee drag)
      - "insufficient_contracts_for_split" — partial-TP enabled but
        int(num_contracts * partial_tp_ratio) would produce a zero leg

    Note: sub-floor SL distances are widened to min_sl_distance_pct rather
    than rejected (fee-noise stops get wicked at high leverage).
    """
    if rr_ratio < min_rr_ratio:
        raise ValueError(
            f"rr_ratio={rr_ratio} is below min_rr_ratio={min_rr_ratio}"
        )

    intent = generate_entry_intent(
        state=state,
        candles=candles,
        python_fvgs=python_fvgs,
        python_order_blocks=python_order_blocks,
        sr_zones=sr_zones,
        weights=weights,
        allowed_sessions=allowed_sessions,
        min_confluence_score=min_confluence_score,
        sl_buffer_mult=sl_buffer_mult,
        swing_lookback=swing_lookback,
        atr_fallback_mult=atr_fallback_mult,
        ltf_state=ltf_state,
        min_rsi_mfi_magnitude=min_rsi_mfi_magnitude,
        liquidity_pool_max_atr_dist=liquidity_pool_max_atr_dist,
        displacement_atr_mult=displacement_atr_mult,
        displacement_max_bars_ago=displacement_max_bars_ago,
        divergence_fresh_bars=divergence_fresh_bars,
        divergence_decay_bars=divergence_decay_bars,
        divergence_max_bars=divergence_max_bars,
        trend_regime=trend_regime,
        trend_regime_conditional_scoring_enabled=trend_regime_conditional_scoring_enabled,
    )
    if intent is None:
        # Distinguish the three upstream `generate_entry_intent` None paths.
        conf = calculate_confluence(
            state, ltf_candles=candles,
            fvgs=python_fvgs, order_blocks=python_order_blocks,
            sr_zones=sr_zones, weights=weights,
            allowed_sessions=allowed_sessions, ltf_state=ltf_state,
            min_rsi_mfi_magnitude=min_rsi_mfi_magnitude,
            liquidity_pool_max_atr_dist=liquidity_pool_max_atr_dist,
            displacement_atr_mult=displacement_atr_mult,
            displacement_max_bars_ago=displacement_max_bars_ago,
            divergence_fresh_bars=divergence_fresh_bars,
            divergence_decay_bars=divergence_decay_bars,
            divergence_max_bars=divergence_max_bars,
            trend_regime=trend_regime,
            trend_regime_conditional_scoring_enabled=trend_regime_conditional_scoring_enabled,
        )
        if not conf.is_tradable(min_confluence_score):
            return None, "below_confluence"
        if allowed_sessions and state.active_session not in allowed_sessions:
            return None, "session_filter"
        return None, "no_sl_source"
    if not intent.is_tradable:
        return None, "no_sl_source"

    if vwap_hard_veto_enabled and _vwap_hard_veto(
        state, intent.direction, intent.entry_price,
    ):
        return None, "vwap_misaligned"

    if ema_veto_enabled and _ema_momentum_veto(
        candles,
        intent.direction,
        intent.entry_price,
        fast_period=ema_veto_fast_period,
        slow_period=ema_veto_slow_period,
    ):
        return None, "ema_momentum_contra"

    if premium_discount_veto_enabled and _premium_discount_veto(
        candles,
        intent.direction,
        intent.entry_price,
        lookback=premium_discount_lookback,
    ):
        return None, "wrong_side_of_premium_discount"

    if _cross_asset_opposes(pillar_opposition, intent.direction):
        return None, "cross_asset_opposition"

    if _should_skip_for_derivatives(
        getattr(state, "derivatives", None),
        intent.direction,
        crowded_skip_enabled,
        crowded_skip_z_threshold,
    ):
        return None, "crowded_skip"

    sl_price = intent.sl_price
    atr = intent.atr

    if htf_sr_ceiling_enabled and htf_sr_zones and sl_price is not None:
        sl_price = _push_sl_past_htf_zone(
            sl_price, intent.entry_price, intent.direction,
            htf_sr_zones, htf_sr_buffer_atr, atr,
        )

    # Min SL distance floor — widen tight stops to at least this distance
    # instead of rejecting. A sub-floor Pine OB/FVG stop at high leverage
    # gets wicked out instantly; widening gives the fill real breathing room
    # while position size auto-shrinks (risk_amount / sl_pct) to keep R flat.
    # Evaluated AFTER the HTF push (which can also widen the SL).
    if (
        min_sl_distance_pct > 0.0
        and intent.entry_price > 0.0
        and sl_price is not None
    ):
        sl_dist_pct = abs(intent.entry_price - sl_price) / intent.entry_price
        if sl_dist_pct < min_sl_distance_pct:
            min_dist = intent.entry_price * min_sl_distance_pct
            if intent.direction == Direction.BULLISH:
                sl_price = intent.entry_price - min_dist
            else:
                sl_price = intent.entry_price + min_dist

    plan = calculate_trade_plan(
        direction=intent.direction,
        entry_price=intent.entry_price,
        sl_price=sl_price,
        account_balance=account_balance,
        risk_pct=risk_pct,
        rr_ratio=rr_ratio,
        max_leverage=max_leverage,
        contract_size=contract_size,
        margin_balance=margin_balance,
        fee_reserve_pct=fee_reserve_pct,
        sl_source=intent.sl_source,
        confluence_score=intent.confluence.score,
        confluence_factors=intent.confluence.factor_names,
        reason=f"{intent.direction.value} via {intent.sl_source}",
    )

    if plan.num_contracts <= 0:
        return None, "zero_contracts"

    # Partial-TP split feasibility — when the router is configured for two-leg
    # TP1/TP2 placement, a plan that floors to a single contract (or any count
    # where int(n * ratio) == 0 or == n) cannot be split. Reject here instead
    # of silently degrading to a single OCO downstream, so the TP1/TP2 policy
    # actually holds on every live trade.
    if partial_tp_enabled:
        size1 = int(plan.num_contracts * partial_tp_ratio)
        size2 = plan.num_contracts - size1
        if size1 <= 0 or size2 <= 0:
            return None, "insufficient_contracts_for_split"

    if htf_sr_ceiling_enabled and htf_sr_zones:
        new_tp = _apply_htf_tp_ceiling(
            plan.tp_price, plan.entry_price, plan.direction,
            htf_sr_zones, htf_sr_buffer_atr, atr,
        )
        if new_tp != plan.tp_price:
            sl_dist = abs(plan.entry_price - plan.sl_price) or 1e-9
            new_rr = abs(new_tp - plan.entry_price) / sl_dist
            if new_rr < min_rr_ratio:
                return None, "htf_tp_ceiling"
            plan = _replace_tp(plan, new_tp=new_tp, new_rr=new_rr)

    # Fee-aware min TP distance — a TP closer than N× round-trip taker fees
    # cannot survive a 3-fill partial-TP lifecycle net of fees even when the
    # price moves the full R, so reject before order placement.
    if min_tp_distance_pct > 0.0 and plan.entry_price > 0.0:
        tp_dist_pct = abs(plan.tp_price - plan.entry_price) / plan.entry_price
        if tp_dist_pct < min_tp_distance_pct:
            return None, "tp_too_tight"

    return plan, ""


def _replace_tp(plan: TradePlan, *, new_tp: float, new_rr: float) -> TradePlan:
    """Return a new TradePlan with tp_price/rr_ratio swapped (dataclass copy)."""
    from dataclasses import replace
    return replace(plan, tp_price=new_tp, rr_ratio=new_rr)
