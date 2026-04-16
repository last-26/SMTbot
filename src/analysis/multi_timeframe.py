"""Multi-timeframe confluence scoring.

Combines every analysis signal the bot can see into a single numeric score
that the strategy engine uses to filter trade ideas. Higher score = more
independent reasons to take the trade.

Inputs:
  - `MarketState` (from Phase 1.6 structured_reader) — current snapshot
    from the Pine Scripts (SMT Signals + SMT Oscillator).
  - Optional candle buffers for entry TF and HTF — used for Python-side
    pattern detection, S/R zones, and FVG/OB lookups when the bot wants
    to cross-check the Pine Script outputs.

Output:
  - `ConfluenceScore`: a breakdown with direction, numeric score, and the
    list of factors that contributed to it.

Pattern weights can be tuned by the RL agent later (Phase 6) — for now
they default to uniform 1.0 so every contributor counts equally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.analysis.fvg import FVG, active_fvgs, price_in_fvg
from src.analysis.liquidity import last_sweep
from src.analysis.order_blocks import (
    OrderBlock,
    active_order_blocks,
    price_in_order_block,
)
from src.analysis.price_action import detect_all_patterns, CandlePattern
from src.analysis.support_resistance import SRZone, at_key_level
from src.data.candle_buffer import Candle
from src.data.models import Direction, MarketState, Session


# ── Result model ────────────────────────────────────────────────────────────


@dataclass
class ConfluenceFactor:
    """One contributor to the confluence score."""
    name: str
    weight: float
    direction: Direction
    detail: str = ""


@dataclass
class ConfluenceScore:
    """Multi-factor confluence aggregated for a candidate direction."""
    direction: Direction
    score: float
    factors: list[ConfluenceFactor] = field(default_factory=list)

    @property
    def factor_names(self) -> list[str]:
        return [f.name for f in self.factors]

    def is_tradable(self, min_score: float) -> bool:
        return self.direction != Direction.UNDEFINED and self.score >= min_score


# ── Weights (tunable by RL later) ───────────────────────────────────────────


DEFAULT_WEIGHTS: dict[str, float] = {
    "htf_trend_alignment": 1.0,
    "mss_alignment": 1.0,
    "at_order_block": 1.0,
    "at_fvg": 1.0,
    "at_sr_zone": 0.75,
    "recent_sweep": 1.0,
    "ltf_pattern": 0.75,
    "oscillator_momentum": 0.5,
    "oscillator_signal": 0.5,
    "vmc_ribbon": 0.5,
    "session_filter": 0.25,
}


# ── Helpers to read MarketState ─────────────────────────────────────────────


def _parse_direction_prefix(value: Optional[str]) -> Direction:
    """Parse "BULLISH@..." / "BULL@..." etc. to Direction."""
    if not value:
        return Direction.UNDEFINED
    v = value.upper()
    if v.startswith("BULL"):
        return Direction.BULLISH
    if v.startswith("BEAR"):
        return Direction.BEARISH
    return Direction.UNDEFINED


def _sweep_direction(sweep_str: Optional[str]) -> Direction:
    """Sweep direction mapped to the reversal direction.

    "BEAR@..." means bearish sweep (swept highs) → bullish reversal.
    "BULL@..." means bullish sweep (swept lows) → bearish reversal.
    We return the reversal direction, which is what confluence cares about.
    """
    d = _parse_direction_prefix(sweep_str)
    if d == Direction.BEARISH:
        return Direction.BULLISH
    if d == Direction.BULLISH:
        return Direction.BEARISH
    return Direction.UNDEFINED


# ── Scoring ─────────────────────────────────────────────────────────────────


def score_direction(
    state: MarketState,
    direction: Direction,
    ltf_candles: Optional[list[Candle]] = None,
    fvgs: Optional[list[FVG]] = None,
    order_blocks: Optional[list[OrderBlock]] = None,
    sr_zones: Optional[list[SRZone]] = None,
    weights: Optional[dict[str, float]] = None,
    allowed_sessions: Optional[list[Session]] = None,
) -> ConfluenceScore:
    """Compute a confluence score for `direction` from the current market state.

    Only non-zero contributions show up in `factors`. The total `score` is
    the sum of the contributing weights.
    """
    if direction == Direction.UNDEFINED:
        return ConfluenceScore(direction=Direction.UNDEFINED, score=0.0)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    factors: list[ConfluenceFactor] = []

    # 1. HTF trend alignment
    if state.trend_htf == direction:
        factors.append(ConfluenceFactor(
            name="htf_trend_alignment",
            weight=w["htf_trend_alignment"],
            direction=direction,
            detail=f"HTF trend={state.trend_htf.value}",
        ))

    # 2. Most recent MSS aligned with direction
    last_mss_dir = _parse_direction_prefix(state.signal_table.last_mss)
    if last_mss_dir == direction:
        factors.append(ConfluenceFactor(
            name="mss_alignment",
            weight=w["mss_alignment"],
            direction=direction,
            detail=state.signal_table.last_mss or "",
        ))

    # 3. Price at an order block matching direction
    price = state.current_price
    at_ob = _parse_direction_prefix(state.signal_table.active_ob)
    if at_ob == direction:
        factors.append(ConfluenceFactor(
            name="at_order_block",
            weight=w["at_order_block"],
            direction=direction,
            detail=state.signal_table.active_ob or "",
        ))
    elif order_blocks and price > 0:
        active = active_order_blocks(order_blocks)
        hit = price_in_order_block(active, price, direction=direction)
        if hit is not None:
            factors.append(ConfluenceFactor(
                name="at_order_block",
                weight=w["at_order_block"],
                direction=direction,
                detail=f"py_ob@{hit.bottom:.2f}-{hit.top:.2f}",
            ))

    # 4. Price at a fair value gap matching direction
    at_fvg_state = _parse_direction_prefix(state.signal_table.active_fvg)
    if at_fvg_state == direction:
        factors.append(ConfluenceFactor(
            name="at_fvg",
            weight=w["at_fvg"],
            direction=direction,
            detail=state.signal_table.active_fvg or "",
        ))
    elif fvgs and price > 0:
        active = active_fvgs(fvgs)
        hit = price_in_fvg(active, price, direction=direction)
        if hit is not None:
            factors.append(ConfluenceFactor(
                name="at_fvg",
                weight=w["at_fvg"],
                direction=direction,
                detail=f"py_fvg@{hit.bottom:.2f}-{hit.top:.2f}",
            ))

    # 5. Price at a Python-computed S/R zone
    if sr_zones and price > 0:
        role_needed = "SUPPORT" if direction == Direction.BULLISH else "RESISTANCE"
        zone = at_key_level(sr_zones, price)
        if zone is not None and zone.role in (role_needed, "MIXED"):
            factors.append(ConfluenceFactor(
                name="at_sr_zone",
                weight=w["at_sr_zone"],
                direction=direction,
                detail=f"{zone.role}@{zone.center:.2f} (touches={zone.touches})",
            ))

    # 6. Recent liquidity sweep suggesting reversal in direction
    sweep_rev = _sweep_direction(state.signal_table.last_sweep)
    if sweep_rev == direction:
        factors.append(ConfluenceFactor(
            name="recent_sweep",
            weight=w["recent_sweep"],
            direction=direction,
            detail=state.signal_table.last_sweep or "",
        ))

    # 7. LTF candlestick pattern in direction
    if ltf_candles:
        patterns = detect_all_patterns(ltf_candles)
        strong = [p for p in patterns if p.direction == direction and p.strength >= 0.4]
        if strong:
            best = max(strong, key=lambda p: p.strength)
            factors.append(ConfluenceFactor(
                name="ltf_pattern",
                weight=w["ltf_pattern"] * best.strength,
                direction=direction,
                detail=f"{best.name} strength={best.strength:.2f}",
            ))

    # 8. Oscillator momentum leaning toward direction
    osc = state.oscillator
    # WT cross in direction
    if direction == Direction.BULLISH and osc.wt_cross == "UP":
        factors.append(ConfluenceFactor(
            name="oscillator_momentum",
            weight=w["oscillator_momentum"],
            direction=direction,
            detail="WT cross UP",
        ))
    elif direction == Direction.BEARISH and osc.wt_cross == "DOWN":
        factors.append(ConfluenceFactor(
            name="oscillator_momentum",
            weight=w["oscillator_momentum"],
            direction=direction,
            detail="WT cross DOWN",
        ))

    # 9. Last oscillator signal ("BUY"/"SELL") aligned with direction
    sig = osc.last_signal.upper() if osc.last_signal else ""
    if direction == Direction.BULLISH and ("BUY" in sig and osc.last_signal_bars_ago <= 3):
        factors.append(ConfluenceFactor(
            name="oscillator_signal",
            weight=w["oscillator_signal"],
            direction=direction,
            detail=f"{osc.last_signal} {osc.last_signal_bars_ago} bars ago",
        ))
    elif direction == Direction.BEARISH and ("SELL" in sig and osc.last_signal_bars_ago <= 3):
        factors.append(ConfluenceFactor(
            name="oscillator_signal",
            weight=w["oscillator_signal"],
            direction=direction,
            detail=f"{osc.last_signal} {osc.last_signal_bars_ago} bars ago",
        ))

    # 10. VMC ribbon alignment (EMA trend bias)
    ribbon_dir = _parse_direction_prefix(state.signal_table.vmc_ribbon)
    if ribbon_dir == direction:
        factors.append(ConfluenceFactor(
            name="vmc_ribbon",
            weight=w["vmc_ribbon"],
            direction=direction,
            detail=f"ribbon={state.signal_table.vmc_ribbon}",
        ))

    # 11. Session filter: small bonus when in an allowed session
    if allowed_sessions and state.active_session in allowed_sessions:
        factors.append(ConfluenceFactor(
            name="session_filter",
            weight=w["session_filter"],
            direction=direction,
            detail=state.active_session.value,
        ))

    total = sum(f.weight for f in factors)
    return ConfluenceScore(direction=direction, score=total, factors=factors)


def calculate_confluence(
    state: MarketState,
    ltf_candles: Optional[list[Candle]] = None,
    fvgs: Optional[list[FVG]] = None,
    order_blocks: Optional[list[OrderBlock]] = None,
    sr_zones: Optional[list[SRZone]] = None,
    weights: Optional[dict[str, float]] = None,
    allowed_sessions: Optional[list[Session]] = None,
) -> ConfluenceScore:
    """Compute confluence for BOTH directions and return the winning side.

    If both scores tie, the HTF trend breaks the tie. If HTF trend is
    undefined, the bearish side wins (no-trade bias).

    When neither side has contributing factors, returns score 0.0 with
    direction UNDEFINED (strategy engine skips the bar).
    """
    bull = score_direction(
        state, Direction.BULLISH,
        ltf_candles=ltf_candles, fvgs=fvgs,
        order_blocks=order_blocks, sr_zones=sr_zones,
        weights=weights, allowed_sessions=allowed_sessions,
    )
    bear = score_direction(
        state, Direction.BEARISH,
        ltf_candles=ltf_candles, fvgs=fvgs,
        order_blocks=order_blocks, sr_zones=sr_zones,
        weights=weights, allowed_sessions=allowed_sessions,
    )

    if bull.score == 0 and bear.score == 0:
        return ConfluenceScore(direction=Direction.UNDEFINED, score=0.0)

    if bull.score > bear.score:
        return bull
    if bear.score > bull.score:
        return bear

    # Tie
    if state.trend_htf == Direction.BULLISH:
        return bull
    return bear
