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
from src.analysis.trend_regime import TrendRegime
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
    # Rebalanced 2026-04-19: scalp-native emphasis — oscillator + VWAP +
    # money-flow + divergence dominate; structural pillars demoted so the
    # bot stops waiting for clean HTF structure on every cycle.
    "htf_trend_alignment": 0.5,
    "mss_alignment": 0.75,
    "at_order_block": 0.6,
    "at_fvg": 0.75,
    "at_sr_zone": 0.75,
    "recent_sweep": 1.0,
    "ltf_pattern": 0.75,
    "oscillator_momentum": 0.75,
    "oscillator_signal": 0.75,
    "vmc_ribbon": 0.5,
    "session_filter": 0.25,
    "ltf_momentum_alignment": 0.75,
    # 2026-04-28 — scalp-confirmation soft factors using existing 1m
    # SignalTable data already fetched by LTFReader. Both fire when the
    # 1m signal aligns with the proposed trade direction — small bonuses
    # designed to add scalp-vote without disturbing the main pillar
    # weights or the min_confluence_score threshold.
    "ltf_ribbon_alignment": 0.25,   # 1m EMA21-55 ribbon bias (vmc_ribbon)
    "ltf_mss_alignment": 0.25,      # 1m last MSS direction prefix
    # 2026-04-28 — 15m MSS journal-only by default (weight=0.0). Operator
    # asked for the 15m last_mss to be CAPTURED in the journal so Pass 3
    # GBT can train on it; active scoring weight is opt-in. The factor
    # still appears in `factors` list with weight 0 → name lands in
    # `confluence_factors` JSON column → Pass 3 sees it as a binary
    # feature. Flip to 0.25 in YAML if Pass 3 importance shows lift.
    "htf_mss_alignment": 0.0,
    # Derivatives (Phase 1.5 Madde 6) — at most one of these three fires per
    # cycle; the elif chain in score_direction enforces that.
    "derivatives_contrarian": 0.7,
    "derivatives_capitulation": 0.6,
    "derivatives_heatmap_target": 0.5,
    # Multi-TF VWAP — legacy per-TF slots zeroed in YAML; composite carries.
    "vwap_1m_alignment": 0.3,
    "vwap_3m_alignment": 0.3,
    "vwap_15m_alignment": 0.4,
    "vwap_composite_alignment": 1.25,
    "money_flow_alignment": 1.0,
    "liquidity_pool_target": 0.5,
    "oscillator_high_conviction_signal": 1.5,
    "displacement_candle": 0.6,
    "divergence_signal": 1.25,
}


# ── Tunable thresholds (non-weight parameters) ──────────────────────────────


# A1 — minimum |rsi_mfi| magnitude before money_flow_alignment fires.
# Below this the bias tag is noise. Overridable via score_direction kwarg.
DEFAULT_MIN_RSI_MFI_MAGNITUDE: float = 2.0

# A2 — maximum distance (in ATR multiples) to the nearest Pine liquidity pool
# for liquidity_pool_target to fire. 3.0 ≈ reachable-within-a-few-bars on
# 3m TF without being so far away the pool is irrelevant.
DEFAULT_LIQUIDITY_POOL_MAX_ATR_DIST: float = 3.0

# D1 — displacement_candle tunables. Body must be at least this multiple of
# ATR, and must sit within the last `DISPLACEMENT_MAX_BARS_AGO` closed bars.
DEFAULT_DISPLACEMENT_ATR_MULT: float = 1.5
DEFAULT_DISPLACEMENT_MAX_BARS_AGO: int = 5

# D2 — divergence_signal bar-ago decay. Divergences lose edge fast as price
# moves past the divergence pivot; we scale the factor weight monotonically.
# bars_ago ≤ DIVERGENCE_FRESH_BARS: full weight
# bars_ago ≤ DIVERGENCE_DECAY_BARS: 50% weight
# bars_ago > DIVERGENCE_MAX_BARS:   skip entirely
DEFAULT_DIVERGENCE_FRESH_BARS: int = 3
DEFAULT_DIVERGENCE_DECAY_BARS: int = 6
DEFAULT_DIVERGENCE_MAX_BARS: int = 9


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


def _apply_trend_regime_conditional(
    weights: dict[str, float],
    regime: Optional[TrendRegime],
    enabled: bool,
) -> dict[str, float]:
    """Return a copy of `weights` adjusted for the trend-regime gate.

    Policy (opt-in, off by default for back-compat):
      * STRONG_TREND → `htf_trend_alignment` × 1.5, `recent_sweep` × 0.5.
        Rewards trend-continuation, penalises reversal setups that
        consistently lose in trending tape.
      * RANGING      → `htf_trend_alignment` × 0.5, `recent_sweep` × 1.5.
        Mean-reversion / sweep-reversal setups earn their keep here.
      * WEAK_TREND / UNKNOWN / None → unchanged (fail-open).
    """
    if not enabled or regime is None or regime == TrendRegime.UNKNOWN:
        return weights
    if regime == TrendRegime.WEAK_TREND:
        return weights
    adjusted = dict(weights)
    if regime == TrendRegime.STRONG_TREND:
        adjusted["htf_trend_alignment"] = adjusted.get("htf_trend_alignment", 0.0) * 1.5
        adjusted["recent_sweep"] = adjusted.get("recent_sweep", 0.0) * 0.5
    elif regime == TrendRegime.RANGING:
        adjusted["htf_trend_alignment"] = adjusted.get("htf_trend_alignment", 0.0) * 0.5
        adjusted["recent_sweep"] = adjusted.get("recent_sweep", 0.0) * 1.5
    return adjusted


def _divergence_direction(div_raw: Optional[str]) -> Direction:
    """Map the Pine `last_wt_div` token to the direction it signals.

    BULL_REG / BULL_HIDDEN → bullish entry edge.
    BEAR_REG / BEAR_HIDDEN → bearish entry edge.
    Unknown / empty tokens → UNDEFINED (no contribution).
    """
    if not div_raw:
        return Direction.UNDEFINED
    token = div_raw.strip().upper()
    if token.startswith("BULL"):
        return Direction.BULLISH
    if token.startswith("BEAR"):
        return Direction.BEARISH
    return Direction.UNDEFINED


def _divergence_decay_weight(
    bars_ago: int,
    fresh_bars: int,
    decay_bars: int,
    max_bars: int,
) -> float:
    """Monotonic bar-ago decay multiplier for divergence_signal.

    bars_ago ≤ fresh_bars  → 1.0  (full weight)
    bars_ago ≤ decay_bars  → 0.5  (half weight)
    bars_ago ≤ max_bars    → 0.25 (faded tail)
    bars_ago >  max_bars   → 0.0  (skip)

    Returns the multiplier (0.0 = skip). Negative bars_ago normalized to 0.
    """
    if bars_ago < 0:
        bars_ago = 0
    if bars_ago > max_bars:
        return 0.0
    if bars_ago <= fresh_bars:
        return 1.0
    if bars_ago <= decay_bars:
        return 0.5
    return 0.25


def _displacement_in_direction(
    candles: Optional[list[Candle]],
    direction: Direction,
    atr: float,
    atr_mult: float,
    max_bars_ago: int,
) -> Optional[tuple[int, float]]:
    """Find the freshest directional displacement candle in the last N bars.

    Returns (bars_ago, body_atr_mult) when a qualifying candle exists, else
    None. A displacement candle has (a) body in the trade direction and
    (b) body size ≥ atr_mult × ATR. Skips bars where ATR or body is
    degenerate. `bars_ago=0` is the most recent closed bar in `candles[-1]`.
    """
    if not candles or atr <= 0 or max_bars_ago <= 0:
        return None
    threshold = atr * atr_mult
    tail = candles[-max_bars_ago:] if len(candles) >= max_bars_ago else list(candles)
    # Iterate most-recent first so the freshest qualifying bar wins.
    for offset, candle in enumerate(reversed(tail)):
        if candle.body_size < threshold:
            continue
        if direction == Direction.BULLISH and not candle.is_bullish:
            continue
        if direction == Direction.BEARISH and not candle.is_bearish:
            continue
        return offset, candle.body_size / atr
    return None


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
    htf_state: Optional[object] = None,
    min_rsi_mfi_magnitude: float = DEFAULT_MIN_RSI_MFI_MAGNITUDE,
    liquidity_pool_max_atr_dist: float = DEFAULT_LIQUIDITY_POOL_MAX_ATR_DIST,
    displacement_atr_mult: float = DEFAULT_DISPLACEMENT_ATR_MULT,
    displacement_max_bars_ago: int = DEFAULT_DISPLACEMENT_MAX_BARS_AGO,
    divergence_fresh_bars: int = DEFAULT_DIVERGENCE_FRESH_BARS,
    divergence_decay_bars: int = DEFAULT_DIVERGENCE_DECAY_BARS,
    divergence_max_bars: int = DEFAULT_DIVERGENCE_MAX_BARS,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
) -> ConfluenceScore:
    """Compute a confluence score for `direction` from the current market state.

    Only non-zero contributions show up in `factors`. The total `score` is
    the sum of the contributing weights.
    """
    if direction == Direction.UNDEFINED:
        return ConfluenceScore(direction=Direction.UNDEFINED, score=0.0)
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    w = _apply_trend_regime_conditional(
        w, trend_regime, trend_regime_conditional_scoring_enabled,
    )
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

    # 6b. Standing liquidity pool in trade direction (Phase 6.9 A2).
    # Pine emits equal-highs/lows as pools in `liquidity_above/below`; price
    # heading into one is a classic "liquidity hunt" setup. Complements
    # `derivatives_heatmap_target` (OI-derived) — they fire independently.
    atr = state.atr
    if price > 0 and atr > 0:
        reach = atr * liquidity_pool_max_atr_dist
        sig_tbl = state.signal_table
        if direction == Direction.BULLISH and sig_tbl.liquidity_above:
            nearest = min(sig_tbl.liquidity_above, key=lambda lvl: abs(lvl - price))
            if nearest > price and (nearest - price) <= reach:
                factors.append(ConfluenceFactor(
                    name="liquidity_pool_target",
                    weight=w["liquidity_pool_target"],
                    direction=direction,
                    detail=f"pool@{nearest:.4f} dist={(nearest - price) / atr:.2f}×ATR",
                ))
        elif direction == Direction.BEARISH and sig_tbl.liquidity_below:
            nearest = min(sig_tbl.liquidity_below, key=lambda lvl: abs(lvl - price))
            if nearest < price and (price - nearest) <= reach:
                factors.append(ConfluenceFactor(
                    name="liquidity_pool_target",
                    weight=w["liquidity_pool_target"],
                    direction=direction,
                    detail=f"pool@{nearest:.4f} dist={(price - nearest) / atr:.2f}×ATR",
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

    # 9. Last oscillator signal aligned with direction. VMC Cipher B's
    # high-conviction signals (GOLD_BUY / BUY_DIV / SELL_DIV) score under a
    # separate name with heavier weight so observability distinguishes them
    # from plain BUY/SELL. Mutually exclusive — one slot per cycle.
    sig = osc.last_signal.upper() if osc.last_signal else ""
    fresh = osc.last_signal_bars_ago <= 3
    high_conviction_tokens_bull = ("GOLD_BUY", "BUY_DIV")
    high_conviction_tokens_bear = ("SELL_DIV",)
    is_bull_high = direction == Direction.BULLISH and fresh and any(
        t in sig for t in high_conviction_tokens_bull
    )
    is_bear_high = direction == Direction.BEARISH and fresh and any(
        t in sig for t in high_conviction_tokens_bear
    )
    if is_bull_high or is_bear_high:
        factors.append(ConfluenceFactor(
            name="oscillator_high_conviction_signal",
            weight=w["oscillator_high_conviction_signal"],
            direction=direction,
            detail=f"{osc.last_signal} {osc.last_signal_bars_ago} bars ago",
        ))
    elif direction == Direction.BULLISH and ("BUY" in sig and fresh):
        factors.append(ConfluenceFactor(
            name="oscillator_signal",
            weight=w["oscillator_signal"],
            direction=direction,
            detail=f"{osc.last_signal} {osc.last_signal_bars_ago} bars ago",
        ))
    elif direction == Direction.BEARISH and ("SELL" in sig and fresh):
        factors.append(ConfluenceFactor(
            name="oscillator_signal",
            weight=w["oscillator_signal"],
            direction=direction,
            detail=f"{osc.last_signal} {osc.last_signal_bars_ago} bars ago",
        ))

    # 9b. Money flow alignment (Phase 6.9 A1). RSI+MFI bias from the
    # oscillator table agrees with direction AND its magnitude clears the
    # noise floor. Futures are liquidity-driven, so this is a primary
    # momentum teyidi alongside WT.
    mfi_bias = _parse_direction_prefix(osc.rsi_mfi_bias)
    if mfi_bias == direction and abs(osc.rsi_mfi) >= min_rsi_mfi_magnitude:
        factors.append(ConfluenceFactor(
            name="money_flow_alignment",
            weight=w["money_flow_alignment"],
            direction=direction,
            detail=f"bias={osc.rsi_mfi_bias} val={osc.rsi_mfi:+.2f}",
        ))

    # 9c. Displacement candle (Phase 7.D1). Large-body, fast-move candle in
    # direction within the last N bars = "real imbalance" confirmation. A
    # pivot insight from sprint 3: FVGs / OBs formed *without* displacement
    # are low quality (price just drifted through). Weight 0.6 — below core
    # structural pillars but above pure oscillator slots.
    disp = _displacement_in_direction(
        ltf_candles, direction, state.atr,
        atr_mult=displacement_atr_mult,
        max_bars_ago=displacement_max_bars_ago,
    )
    if disp is not None:
        bars_ago, body_atr = disp
        factors.append(ConfluenceFactor(
            name="displacement_candle",
            weight=w["displacement_candle"],
            direction=direction,
            detail=f"body={body_atr:.2f}×ATR bars_ago={bars_ago}",
        ))

    # 9d. Divergence signal (Phase 7.D2). Pine's native `last_wt_div` stream
    # (BULL_REG / BEAR_REG / BULL_HIDDEN / BEAR_HIDDEN). Monotonic bar-ago
    # decay: fresh divergence 1.0× weight, aging into 0.5× / 0.25×, dropped
    # past `divergence_max_bars`. Orthogonal to `oscillator_high_conviction_
    # signal` (which fires on the summary `last_signal` string). Either can
    # contribute independently when both streams agree with the direction.
    div_dir = _divergence_direction(osc.last_wt_div)
    if div_dir == direction:
        div_bars_ago = int(getattr(osc, "last_wt_div_bars_ago", 99))
        decay = _divergence_decay_weight(
            div_bars_ago,
            fresh_bars=divergence_fresh_bars,
            decay_bars=divergence_decay_bars,
            max_bars=divergence_max_bars,
        )
        if decay > 0.0:
            factors.append(ConfluenceFactor(
                name="divergence_signal",
                weight=w["divergence_signal"] * decay,
                direction=direction,
                detail=(
                    f"{osc.last_wt_div} bars_ago={div_bars_ago} decay={decay:.2f}"
                ),
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

        # 12.B 1m EMA ribbon alignment (vmc_ribbon = EMA21 vs EMA55 bias).
        # Operator-driven scalp factor (2026-04-28). Fires when 1m EMA
        # ribbon direction matches the proposed trade direction. Small
        # bonus only — won't break the min_confluence_score threshold
        # alone, but adds scalp-vote weight when ribbon + MSS + main
        # pillars line up.
        ribbon_dir = _parse_direction_prefix(getattr(ltf_state, "vmc_ribbon", ""))
        if ribbon_dir == direction:
            factors.append(ConfluenceFactor(
                name="ltf_ribbon_alignment",
                weight=w.get("ltf_ribbon_alignment", 0.0),
                direction=direction,
                detail=f"1m_ribbon={ltf_state.vmc_ribbon}",
            ))

        # 12.C 1m MSS alignment (last market structure shift on 1m).
        # Pairs with `mss_alignment` (entry-TF MSS) — when both fire, the
        # short-window structural shift confirms the entry-TF picture.
        # Operator-driven scalp factor (2026-04-28).
        mss_dir = _parse_direction_prefix(getattr(ltf_state, "last_mss", None))
        if mss_dir == direction:
            factors.append(ConfluenceFactor(
                name="ltf_mss_alignment",
                weight=w.get("ltf_mss_alignment", 0.0),
                direction=direction,
                detail=f"1m_mss={ltf_state.last_mss}",
            ))

    # 12.D 15m MSS alignment (HTF last MSS direction prefix).
    # Operator-driven journal capture (2026-04-28). DEFAULT WEIGHT = 0.0
    # — the factor lands in `confluence_factors` JSON for Pass 3 GBT to
    # train on, but doesn't tilt the live confluence score. Flip the
    # YAML weight to 0.25 if a future Pass 3 importance pass shows lift.
    # Trio with `mss_alignment` (3m, entry-TF) + `ltf_mss_alignment` (1m)
    # gives a complete multi-TF MSS picture for the model.
    if htf_state is not None:
        htf_sig = getattr(htf_state, "signal_table", None)
        htf_mss_raw = getattr(htf_sig, "last_mss", None) if htf_sig else None
        htf_mss_dir = _parse_direction_prefix(htf_mss_raw)
        if htf_mss_dir == direction:
            factors.append(ConfluenceFactor(
                name="htf_mss_alignment",
                weight=w.get("htf_mss_alignment", 0.0),
                direction=direction,
                detail=f"15m_mss={htf_mss_raw}",
            ))

    # 12.5 Multi-TF VWAP alignment.
    #
    # Two factor families fire here:
    #   * vwap_{1m,3m,15m}_alignment — legacy per-TF factors. YAML zeroes
    #     their weight by default (Phase 7.A4); they remain for RL feature
    #     visibility and existing test coverage.
    #   * vwap_composite_alignment — single factor scaling with the fraction
    #     of PRESENT TFs that align. Missing VWAPs (0.0) are excluded from
    #     both numerator and denominator so a run with only 1m+3m VWAPs
    #     still scores 3/3 if both agree.
    sig = state.signal_table
    vwap_entries = (
        ("vwap_1m_alignment",  sig.vwap_1m),
        ("vwap_3m_alignment",  sig.vwap_3m),
        ("vwap_15m_alignment", sig.vwap_15m),
    )
    present = 0
    aligned = 0
    if price > 0:
        for fname, vwap_val in vwap_entries:
            if vwap_val <= 0:
                continue
            present += 1
            side_ok = (
                (direction == Direction.BULLISH and price > vwap_val) or
                (direction == Direction.BEARISH and price < vwap_val)
            )
            if side_ok:
                aligned += 1
                factors.append(ConfluenceFactor(
                    name=fname,
                    weight=w[fname],
                    direction=direction,
                    detail=f"price {'>' if direction == Direction.BULLISH else '<'} "
                           f"{vwap_val:.4f}",
                ))
    if present > 0 and aligned > 0:
        composite_weight = w["vwap_composite_alignment"] * (aligned / present)
        factors.append(ConfluenceFactor(
            name="vwap_composite_alignment",
            weight=composite_weight,
            direction=direction,
            detail=f"{aligned}/{present} VWAPs aligned",
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
    htf_state: Optional[object] = None,
    min_rsi_mfi_magnitude: float = DEFAULT_MIN_RSI_MFI_MAGNITUDE,
    liquidity_pool_max_atr_dist: float = DEFAULT_LIQUIDITY_POOL_MAX_ATR_DIST,
    displacement_atr_mult: float = DEFAULT_DISPLACEMENT_ATR_MULT,
    displacement_max_bars_ago: int = DEFAULT_DISPLACEMENT_MAX_BARS_AGO,
    divergence_fresh_bars: int = DEFAULT_DIVERGENCE_FRESH_BARS,
    divergence_decay_bars: int = DEFAULT_DIVERGENCE_DECAY_BARS,
    divergence_max_bars: int = DEFAULT_DIVERGENCE_MAX_BARS,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
    daily_bias_enabled: bool = False,
    daily_bias_delta: float = 0.0,
) -> ConfluenceScore:
    """Compute confluence for BOTH directions and return the winning side.

    If both scores tie, the HTF trend breaks the tie. If HTF trend is
    undefined, the bearish side wins (no-trade bias).

    When neither side has contributing factors, returns score 0.0 with
    direction UNDEFINED (strategy engine skips the bar).

    **2026-04-21 — Arkham daily macro-bias modifier (Phase C).** When
    `daily_bias_enabled=True` AND `state.on_chain` carries a fresh
    snapshot AND `daily_bias_delta > 0`, the two directional scores are
    multiplied by ±(1 + delta):
      * bullish day → bull score × (1 + delta), bear × (1 − delta)
      * bearish day → mirror
      * neutral / stale / absent → both × 1.0 (no-op)
    The modifier is applied BEFORE the tie-break + threshold compare
    downstream so `below_confluence` rejections reflect the adjusted
    score.
    """
    bull = score_direction(
        state, Direction.BULLISH,
        ltf_candles=ltf_candles, fvgs=fvgs,
        order_blocks=order_blocks, sr_zones=sr_zones,
        weights=weights, allowed_sessions=allowed_sessions,
        ltf_state=ltf_state,
        htf_state=htf_state,
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
    bear = score_direction(
        state, Direction.BEARISH,
        ltf_candles=ltf_candles, fvgs=fvgs,
        order_blocks=order_blocks, sr_zones=sr_zones,
        weights=weights, allowed_sessions=allowed_sessions,
        ltf_state=ltf_state,
        htf_state=htf_state,
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

    # 2026-04-21 — Arkham daily macro-bias modifier. Kept out of
    # `score_direction` so per-pillar weights stay pure and the modifier
    # is introspectable as a single scalar multiply. Stale snapshots +
    # neutral bias + master-off all fall through via `mult_long=mult_short=1.0`.
    mult_long, mult_short = _daily_bias_multipliers(
        state.on_chain if daily_bias_enabled else None,
        delta=daily_bias_delta,
    )
    if mult_long != 1.0:
        bull = ConfluenceScore(
            direction=bull.direction,
            score=bull.score * mult_long,
            factors=bull.factors,
        )
    if mult_short != 1.0:
        bear = ConfluenceScore(
            direction=bear.direction,
            score=bear.score * mult_short,
            factors=bear.factors,
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


def _daily_bias_multipliers(
    on_chain: Optional[Any],
    *,
    delta: float,
) -> tuple[float, float]:
    """Return (mult_long, mult_short) scalars for the daily macro-bias
    modifier. Both 1.0 when the snapshot is absent, stale, neutral, or
    delta is 0.0 — the caller can short-circuit on `mult_long == 1.0
    and mult_short == 1.0` to skip the wrapping ConfluenceScore rebuild.

    Rule:
      * bullish   → (1 + delta, 1 - delta)
      * bearish   → (1 - delta, 1 + delta)
      * neutral / absent / stale / delta=0 → (1.0, 1.0)
    """
    if delta <= 0.0:
        return 1.0, 1.0
    if on_chain is None:
        return 1.0, 1.0
    # `fresh` is a property on OnChainSnapshot — absent → falsy on
    # getattr fallback.
    if not getattr(on_chain, "fresh", False):
        return 1.0, 1.0
    bias = getattr(on_chain, "daily_macro_bias", "neutral")
    if bias == "bullish":
        return 1.0 + delta, 1.0 - delta
    if bias == "bearish":
        return 1.0 - delta, 1.0 + delta
    return 1.0, 1.0
