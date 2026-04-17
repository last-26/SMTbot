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
    # LTF momentum confirmation: full weight when the LTF trend agrees with
    # the candidate direction, partial weight when the last LTF signal is
    # fresh and agrees. Keeps it a single principled slot, not stacked.
    "ltf_momentum_alignment": 0.5,
    # Derivatives (Phase 1.5 Madde 6) — at most one of these three fires per
    # cycle; the elif chain in score_direction enforces that.
    "derivatives_contrarian": 0.7,
    "derivatives_capitulation": 0.6,
    "derivatives_heatmap_target": 0.5,
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


def _heatmap_supports_direction(state: MarketState, direction: Direction) -> bool:
    """True when a meaningful liquidity cluster sits in the trade's path.

    Bullish → nearest_above cluster within ATR*3 AND its notional ≥ 70% of the
    largest above cluster. Bearish: symmetric below. Returns False if the
    heatmap or ATR is missing.
    """
    hm = getattr(state, "liquidity_heatmap", None)
    if hm is None:
        return False
    atr = state.atr
    price = state.current_price
    if atr <= 0 or price <= 0:
        return False
    reach = atr * 3.0
    if direction == Direction.BULLISH:
        na = getattr(hm, "nearest_above", None)
        if na is None:
            return False
        if (na.price - price) > reach:
            return False
        largest = float(getattr(hm, "largest_above_notional", 0.0) or 0.0)
        return largest > 0 and na.notional_usd >= largest * 0.7
    if direction == Direction.BEARISH:
        nb = getattr(hm, "nearest_below", None)
        if nb is None:
            return False
        if (price - nb.price) > reach:
            return False
        largest = float(getattr(hm, "largest_below_notional", 0.0) or 0.0)
        return largest > 0 and nb.notional_usd >= largest * 0.7
    return False


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
    ltf_state: Optional[object] = None,
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

    # 12. LTF momentum alignment — reversal moves often kick off on the LTF
    # before the entry TF catches them. Full weight when LTF trend matches,
    # partial when the most recent LTF signal (≤3 bars old) points that way.
    if ltf_state is not None:
        ltf_trend = getattr(ltf_state, "trend", None)
        if ltf_trend == direction:
            factors.append(ConfluenceFactor(
                name="ltf_momentum_alignment",
                weight=w["ltf_momentum_alignment"],
                direction=direction,
                detail=f"ltf_trend={getattr(ltf_trend, 'value', ltf_trend)}",
            ))
        else:
            sig_raw = (getattr(ltf_state, "last_signal", "") or "").upper()
            raw_bars = getattr(ltf_state, "last_signal_bars_ago", None)
            bars_ago = int(raw_bars) if raw_bars is not None else 99
            fresh = bars_ago <= 3
            if fresh and (
                (direction == Direction.BULLISH and "BUY" in sig_raw) or
                (direction == Direction.BEARISH and "SELL" in sig_raw)
            ):
                factors.append(ConfluenceFactor(
                    name="ltf_momentum_alignment",
                    weight=w["ltf_momentum_alignment"] * 0.6,
                    direction=direction,
                    detail=f"ltf_signal={sig_raw} bars_ago={bars_ago}",
                ))

    # 13. Derivatives (Phase 1.5 Madde 6) — one slot max per cycle.
    deriv_state = getattr(state, "derivatives", None)
    if deriv_state is not None:
        regime = getattr(deriv_state, "regime", "UNKNOWN")
        added_derivatives = False
        # a) Contrarian: fade a crowded side.
        if direction == Direction.BULLISH and regime == "SHORT_CROWDED":
            factors.append(ConfluenceFactor(
                name="derivatives_contrarian",
                weight=w["derivatives_contrarian"],
                direction=direction,
                detail=f"regime={regime}",
            ))
            added_derivatives = True
        elif direction == Direction.BEARISH and regime == "LONG_CROWDED":
            factors.append(ConfluenceFactor(
                name="derivatives_contrarian",
                weight=w["derivatives_contrarian"],
                direction=direction,
                detail=f"regime={regime}",
            ))
            added_derivatives = True
        # b) Capitulation favors the contrarian side by imbalance.
        elif regime == "CAPITULATION":
            imbalance = float(getattr(deriv_state, "liq_imbalance_1h", 0.0) or 0.0)
            # imbalance = (short - long) / total: >0 means shorts got washed
            # → bullish bias. <0 means longs washed → bearish bias.
            if (direction == Direction.BULLISH and imbalance > 0.1) or \
               (direction == Direction.BEARISH and imbalance < -0.1):
                factors.append(ConfluenceFactor(
                    name="derivatives_capitulation",
                    weight=w["derivatives_capitulation"],
                    direction=direction,
                    detail=f"imbalance={imbalance:+.2f}",
                ))
                added_derivatives = True
        # c) Heatmap magnet in the trade direction.
        if not added_derivatives and _heatmap_supports_direction(state, direction):
            factors.append(ConfluenceFactor(
                name="derivatives_heatmap_target",
                weight=w["derivatives_heatmap_target"],
                direction=direction,
                detail="nearest_cluster_matches_target",
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
    ltf_state: Optional[object] = None,
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
        ltf_state=ltf_state,
    )
    bear = score_direction(
        state, Direction.BEARISH,
        ltf_candles=ltf_candles, fvgs=fvgs,
        order_blocks=order_blocks, sr_zones=sr_zones,
        weights=weights, allowed_sessions=allowed_sessions,
        ltf_state=ltf_state,
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
