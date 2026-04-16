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
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
) -> Optional[TradePlan]:
    """End-to-end: MarketState → TradePlan. Returns None when no trade.

    `rr_ratio` is the target. `min_rr_ratio` is a hard floor — this function
    always honors the hard floor by erroring if the caller passed rr_ratio
    below it.
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
    )
    if intent is None or not intent.is_tradable:
        return None

    plan = calculate_trade_plan(
        direction=intent.direction,
        entry_price=intent.entry_price,
        sl_price=intent.sl_price,
        account_balance=account_balance,
        risk_pct=risk_pct,
        rr_ratio=rr_ratio,
        max_leverage=max_leverage,
        contract_size=contract_size,
        sl_source=intent.sl_source,
        confluence_score=intent.confluence.score,
        confluence_factors=intent.confluence.factor_names,
        reason=f"{intent.direction.value} via {intent.sl_source}",
    )

    # Safety: if contract rounding wiped the position to zero, no trade.
    if plan.num_contracts <= 0:
        return None

    return plan
