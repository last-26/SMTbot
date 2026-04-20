"""Zone-based entry planner (rebalanced 2026-04-19).

Converts a directional signal (confluence + HTF trend-picker) into a
concrete `ZoneSetup`: a price range to limit-order into, a structural SL
beyond the zone, and a TP target (plus optional partial-TP ladder) from
the surrounding liquidity landscape.

Scalp-native source priority (first hit wins):
  1. VWAP retest           — pullback to 1m/3m/15m session VWAP
  2. EMA21 pullback        — price revisits fast EMA with aligned stack
  3. FVG (entry TF)        — 3m unfilled FVG matching direction
  4. Sweep retest          — reclaimed opposite-side sweep
  5. Liq pool (near)       — only when an abnormally-large cluster sits
                             within ``liq_entry_near_max_atr`` × ATR of
                             price AND its notional clears the magnitude
                             gate (``liq_entry_magnitude_mult`` × median).
                             Entry sits AT the cluster (zone mid), not its
                             far edge.
  6. HTF 15m FVG           — opt-in (``htf_fvg_entry_enabled=True``).

Liquidity is primarily a TP instrument (``tp_primary`` + optional
ladder), not an entry instrument. The near-liq entry exists for the
"BTC 75000 / 74800 abnormal cluster support" case the operator
specifically asked for.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from src.analysis.liquidity_heatmap import LiquidityHeatmap
from src.data.models import Direction, MarketState
from src.strategy._indicators import ema
from src.strategy.trade_plan import TradePlan


ZoneSource = Literal[
    "vwap_retest", "ema21_pullback", "fvg_entry",
    "sweep_retest", "liq_pool_near", "fvg_htf",
]
TriggerType = Literal["zone_touch", "sweep_reversal", "displacement_return"]


@dataclass(frozen=True)
class ZoneSetup:
    """Limit-order setup to be placed at a structural zone."""
    direction: Direction
    entry_zone: tuple[float, float]    # (low, high), low <= high
    trigger_type: TriggerType
    sl_beyond_zone: float              # structural stop, not % floor
    tp_primary: float                  # first TP (may be single leg)
    max_wait_bars: int
    zone_source: ZoneSource
    tp_ladder: tuple[tuple[float, float], ...] = field(
        default_factory=lambda: tuple()
    )
    # Each entry: (tp_price, share_fraction). Shares sum to 1.0. An empty
    # tuple means the consumer should treat it as a single-leg ladder of
    # ((tp_primary, 1.0),).


def _is_long(direction: Direction) -> bool:
    return direction == Direction.BULLISH


# ── Zone sources ───────────────────────────────────────────────────────────


def _vwap_zone(
    direction: Direction, price: float, atr: float,
    state: MarketState, zone_atr: float,
) -> Optional[tuple[float, float]]:
    """Directional zone anchored on the nearest session VWAP.

    Long:  zone = (vwap, upper_band) — entry mid = vwap + 0.5σ
    Short: zone = (lower_band, vwap) — entry mid = vwap − 0.5σ

    When the picked VWAP is 3m and Pine emitted a live ±1σ band, the zone
    uses the band (session-realised volatility). Otherwise the zone is a
    single-sided ATR half-band on the directional side of VWAP — still
    above VWAP for long / below for short, just using ATR as a stdev
    proxy. Either way entry lands between VWAP and current price (never
    past VWAP on the far side), which was the point of the 2026-04-19
    rewire: static ATR buffer below VWAP was filling <5% of setups on
    tight-tape sessions.
    """
    sig = state.signal_table
    raw = [
        ("1m", sig.vwap_1m),
        ("3m", sig.vwap_3m),
        ("15m", sig.vwap_15m),
    ]
    candidates = [
        (tf, v) for tf, v in raw if v > 0
        and ((_is_long(direction) and v < price)
             or (not _is_long(direction) and v > price))
    ]
    if not candidates:
        return None
    tf, target = min(candidates, key=lambda tv: abs(price - tv[1]))
    if tf == "3m":
        upper = sig.vwap_3m_upper
        lower = sig.vwap_3m_lower
        if _is_long(direction) and upper > target:
            return (target, upper)
        if not _is_long(direction) and 0.0 < lower < target:
            return (lower, target)
    half = zone_atr * atr
    if _is_long(direction):
        return (target, target + half)
    return (target - half, target)


def _ema21_pullback_zone(
    direction: Direction, price: float, atr: float,
    candles: Optional[list[Any]], zone_atr: float,
    fast_period: int, slow_period: int,
) -> Optional[tuple[float, float]]:
    """Price inside ``zone_atr × ATR`` of EMA_fast, with stack aligned.

    Bull stack (price > EMA21 > EMA55) arms a BULLISH pullback.
    Bear stack (price < EMA21 < EMA55) arms a BEARISH pullback.
    Returns None when candles are missing, stack is contra, or price is
    already outside the band.
    """
    if not candles:
        return None
    closes = [c.close for c in candles if getattr(c, "close", None) is not None]
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
    if ema_fast is None or ema_slow is None:
        return None
    if _is_long(direction):
        stack_ok = price > ema_fast > ema_slow
        if not stack_ok:
            return None
        # Long pullback: EMA21 must sit below price (retrace target).
        if ema_fast >= price:
            return None
    else:
        stack_ok = price < ema_fast < ema_slow
        if not stack_ok:
            return None
        if ema_fast <= price:
            return None
    half = zone_atr * atr
    return (ema_fast - half, ema_fast + half)


def _entry_tf_fvg_zone(
    direction: Direction, price: float, state: MarketState,
) -> Optional[tuple[float, float]]:
    """Nearest unfilled ENTRY-TF FVG on the correct side of price."""
    zones = (state.active_bull_fvgs() if _is_long(direction)
             else state.active_bear_fvgs())
    candidates: list[tuple[float, float]] = []
    for z in zones:
        lo, hi = min(z.bottom, z.top), max(z.bottom, z.top)
        if _is_long(direction) and hi < price:
            candidates.append((lo, hi))
        elif not _is_long(direction) and lo > price:
            candidates.append((lo, hi))
    if not candidates:
        return None
    candidates.sort(key=lambda lh: abs(price - (lh[0] + lh[1]) / 2.0))
    return candidates[0]


def _htf_fvg_zone(
    direction: Direction, price: float,
    htf_state: Optional[MarketState],
) -> Optional[tuple[float, float]]:
    """Nearest unfilled HTF 15m FVG. Off by default post-pivot; entry path
    only when ``htf_fvg_entry_enabled=True``. Otherwise kept for TP-side
    alignment use only."""
    if htf_state is None:
        return None
    zones = (htf_state.active_bull_fvgs() if _is_long(direction)
             else htf_state.active_bear_fvgs())
    candidates: list[tuple[float, float]] = []
    for z in zones:
        lo, hi = min(z.bottom, z.top), max(z.bottom, z.top)
        if _is_long(direction) and hi < price:
            candidates.append((lo, hi))
        elif not _is_long(direction) and lo > price:
            candidates.append((lo, hi))
    if not candidates:
        return None
    candidates.sort(key=lambda lh: abs(price - (lh[0] + lh[1]) / 2.0))
    return candidates[0]


def _sweep_zone(
    direction: Direction, price: float, atr: float,
    state: MarketState, zone_atr: float,
) -> Optional[tuple[float, float]]:
    """Most recent sweep of the opposite side as a retest zone."""
    if not state.sweep_events:
        return None
    wanted = Direction.BEARISH if _is_long(direction) else Direction.BULLISH
    matches = [e for e in state.sweep_events if e.direction == wanted]
    if not matches:
        return None
    level = matches[-1].level
    if _is_long(direction) and level >= price:
        return None
    if not _is_long(direction) and level <= price:
        return None
    half = zone_atr * atr
    return (level - half, level + half)


def _liq_pool_near_zone(
    direction: Direction, price: float, atr: float,
    heatmap: Optional[LiquidityHeatmap],
    zone_atr: float,
    max_dist_atr: float,
    magnitude_mult: float,
) -> Optional[tuple[float, float]]:
    """Abnormally-large liquidity cluster within reach of price.

    Two gates:
      * Distance: ``|cluster - price| <= max_dist_atr × ATR``.
      * Magnitude: ``cluster.notional_usd >= magnitude_mult × median_side``.

    Operator's BTC-75000 / 74800-big-cluster case: cluster sits on the
    correct structural side (support for long, resistance for short) and
    is genuinely abnormal — not "place a limit at the top cluster and
    hope for a sweep-reversal".
    """
    if heatmap is None:
        return None
    cluster = heatmap.nearest_below if _is_long(direction) else heatmap.nearest_above
    if cluster is None or cluster.notional_usd <= 0:
        return None
    if _is_long(direction) and cluster.price >= price:
        return None
    if not _is_long(direction) and cluster.price <= price:
        return None
    if abs(cluster.price - price) > max_dist_atr * atr:
        return None
    side_clusters = (heatmap.clusters_below if _is_long(direction)
                     else heatmap.clusters_above)
    notionals = [c.notional_usd for c in side_clusters if c.notional_usd > 0]
    if not notionals:
        return None
    median = statistics.median(notionals)
    if median <= 0:
        return None
    if cluster.notional_usd < magnitude_mult * median:
        return None
    half = zone_atr * atr
    return (cluster.price - half, cluster.price + half)


# ── SL / TP helpers ────────────────────────────────────────────────────────


def _sl_beyond_zone(
    direction: Direction, zone: tuple[float, float],
    atr: float, sl_buffer_atr: float,
) -> float:
    low, high = zone
    buf = sl_buffer_atr * atr
    return (low - buf) if _is_long(direction) else (high + buf)


def _tp_primary(
    direction: Direction, zone: tuple[float, float],
    heatmap: Optional[LiquidityHeatmap], atr: float, default_rr: float,
) -> float:
    """TP = nearest cluster in direction (on correct side), else RR × width."""
    zone_mid = (zone[0] + zone[1]) / 2.0
    if heatmap is not None:
        cluster = (heatmap.nearest_above if _is_long(direction)
                   else heatmap.nearest_below)
        if cluster is not None and cluster.notional_usd > 0:
            if _is_long(direction) and cluster.price > zone_mid:
                return cluster.price
            if not _is_long(direction) and cluster.price < zone_mid:
                return cluster.price
    width = abs(zone[1] - zone[0]) + atr
    if _is_long(direction):
        return zone_mid + default_rr * width
    return zone_mid - default_rr * width


def _build_tp_ladder(
    direction: Direction,
    zone_mid: float,
    heatmap: Optional[LiquidityHeatmap],
    tp_primary: float,
    shares: tuple[float, ...],
    min_notional_frac: float,
) -> tuple[tuple[float, float], ...]:
    """Build a partial-TP ladder from liquidity clusters on the target side.

    Returns ((price, share_fraction), ...) ordered near→far. Shares are
    renormalised when fewer than ``len(shares)`` clusters pass the notional
    filter. Falls back to ``((tp_primary, 1.0),)`` when there is no
    heatmap, no valid cluster, or ladder is disabled (``shares`` empty /
    single 1.0).
    """
    single = ((tp_primary, 1.0),)
    if heatmap is None or not shares or len(shares) == 1:
        return single
    side_clusters = (heatmap.clusters_above if _is_long(direction)
                     else heatmap.clusters_below)
    if not side_clusters:
        return single
    largest = (heatmap.largest_above_notional if _is_long(direction)
               else heatmap.largest_below_notional)
    if largest <= 0:
        return single
    threshold = largest * min_notional_frac
    valid = []
    for c in side_clusters:
        if c.notional_usd < threshold:
            continue
        if _is_long(direction) and c.price <= zone_mid:
            continue
        if not _is_long(direction) and c.price >= zone_mid:
            continue
        valid.append(c)
    if not valid:
        return single
    taken = valid[:len(shares)]
    used = shares[:len(taken)]
    total = sum(used)
    if total <= 0:
        return single
    norm = tuple(s / total for s in used)
    return tuple((c.price, sh) for c, sh in zip(taken, norm))


# ── Public API ─────────────────────────────────────────────────────────────


def build_zone_setup(
    *,
    direction: Direction,
    state: MarketState,
    htf_state: Optional[MarketState] = None,
    heatmap: Optional[LiquidityHeatmap] = None,
    ltf_candles: Optional[list[Any]] = None,
    zone_buffer_atr: float = 0.25,
    sl_buffer_atr: float = 0.5,
    max_wait_bars: int = 10,
    default_rr: float = 2.0,
    liq_entry_near_max_atr: float = 1.5,
    liq_entry_magnitude_mult: float = 2.5,
    ema21_pullback_enabled: bool = True,
    ema_fast_period: int = 21,
    ema_slow_period: int = 55,
    htf_fvg_entry_enabled: bool = False,
    tp_ladder_enabled: bool = True,
    tp_ladder_shares: tuple[float, ...] = (0.40, 0.35, 0.25),
    tp_ladder_min_notional_frac: float = 0.30,
) -> Optional[ZoneSetup]:
    """Return the best `ZoneSetup` for *direction*, or None if no source fits."""
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return None
    price = state.current_price
    atr = state.atr
    if atr <= 0 or price <= 0:
        return None

    hm = heatmap if heatmap is not None else state.liquidity_heatmap

    sources: list[tuple[ZoneSource, TriggerType, Optional[tuple[float, float]]]] = [
        ("vwap_retest", "zone_touch",
            _vwap_zone(direction, price, atr, state, zone_buffer_atr)),
        ("ema21_pullback", "zone_touch",
            _ema21_pullback_zone(
                direction, price, atr, ltf_candles, zone_buffer_atr,
                ema_fast_period, ema_slow_period,
            ) if ema21_pullback_enabled else None),
        ("fvg_entry", "zone_touch",
            _entry_tf_fvg_zone(direction, price, state)),
        ("sweep_retest", "sweep_reversal",
            _sweep_zone(direction, price, atr, state, zone_buffer_atr)),
        ("liq_pool_near", "zone_touch",
            _liq_pool_near_zone(
                direction, price, atr, hm, zone_buffer_atr,
                liq_entry_near_max_atr, liq_entry_magnitude_mult,
            )),
    ]
    if htf_fvg_entry_enabled:
        sources.append((
            "fvg_htf", "zone_touch",
            _htf_fvg_zone(direction, price, htf_state),
        ))

    shares = tp_ladder_shares if tp_ladder_enabled else (1.0,)
    for source, trigger, zone in sources:
        if zone is None:
            continue
        sl = _sl_beyond_zone(direction, zone, atr, sl_buffer_atr)
        tp = _tp_primary(direction, zone, hm, atr, default_rr)
        zone_mid = (zone[0] + zone[1]) / 2.0
        ladder = _build_tp_ladder(
            direction, zone_mid, hm, tp, shares, tp_ladder_min_notional_frac,
        )
        return ZoneSetup(
            direction=direction,
            entry_zone=zone,
            trigger_type=trigger,
            sl_beyond_zone=sl,
            tp_primary=tp,
            max_wait_bars=max_wait_bars,
            zone_source=source,
            tp_ladder=ladder,
        )
    return None


def zone_limit_price(
    direction: Direction, zone: tuple[float, float],
    zone_source: Optional[str] = None,
) -> float:
    """Limit-entry price for `direction` inside `zone`.

    Near-edge for pullback-style sources (EMA / FVG / sweep):
      * Long: zone low (buy-limit below market).
      * Short: zone high (sell-limit above market).

    Zone mid for ``liq_pool_near`` and ``vwap_retest``:
      * liq_pool_near — the cluster IS the support/resistance target.
      * vwap_retest — zone now spans (vwap, upper_band) for long /
        (lower_band, vwap) for short, so zone-mid = vwap ± 0.5σ, i.e.
        inside the VWAP band on the directional side. Previously used
        zone.low for long which sat past VWAP on the discount side and
        rarely filled.
    """
    low, high = zone
    if zone_source in ("liq_pool_near", "vwap_retest"):
        return (low + high) / 2.0
    return low if _is_long(direction) else high


def apply_zone_to_plan(
    plan: TradePlan, zone: "ZoneSetup", contract_size: float,
    min_sl_distance_pct: float = 0.0,
    target_rr_cap: float = 0.0,
) -> TradePlan:
    """Return a new TradePlan with entry/SL/TP taken from *zone*, re-sized
    so total USDT risk on the structural SL equals `plan.risk_amount_usdt`.

    Why re-size: the original plan was sized against the minimum-% SL
    floor. The zone's structural SL can be wider or narrower, so leaving
    contracts unchanged would drift risk. Leverage + risk budget are
    preserved; primary TP is overridden with the zone's target, and
    `tp_ladder` carries the full partial-TP ladder for downstream
    consumption.

    `min_sl_distance_pct` floor is re-applied here because the zone's
    structural SL (buffer × ATR past zone edge) can land well inside the
    per-symbol floor that entry_signals already widened the original plan
    to. Without this re-check, an SL right on top of entry gets wicked
    out immediately — and on 2026-04-19 caused sCode 51277
    (`place_algo_order: (no message)`) on 3 positions because the mark
    had crossed the trigger between fill and OCO attach. Widen the SL
    here, keep the zone's direction/structural intent, resize contracts
    off the new distance so R stays flat.

    `target_rr_cap` enforces a hard 1:N reward/risk on the final primary
    TP (and clamps every ladder rung to the same bound). Without it, the
    heatmap-cluster TP source drifted to 8-12R on 2026-04-19 (operator
    log showed "$300 SL → $3600 TP"). When `target_rr_cap > 0` the
    primary TP becomes exactly ``entry ± cap × sl_distance`` — clusters
    no longer override the RR contract.
    """
    if plan.direction != zone.direction:
        raise ValueError(
            f"direction mismatch: plan={plan.direction} zone={zone.direction}"
        )
    new_entry = zone_limit_price(zone.direction, zone.entry_zone, zone.zone_source)
    new_sl = zone.sl_beyond_zone
    new_tp = zone.tp_primary
    new_sl_distance = abs(new_entry - new_sl)
    if new_entry <= 0 or new_sl_distance <= 0:
        raise ValueError(
            f"degenerate zone: entry={new_entry} sl={new_sl} dist={new_sl_distance}"
        )
    if min_sl_distance_pct > 0.0:
        sl_dist_pct = new_sl_distance / new_entry
        if sl_dist_pct < min_sl_distance_pct:
            min_dist = new_entry * min_sl_distance_pct
            if zone.direction == Direction.BULLISH:
                new_sl = new_entry - min_dist
            else:
                new_sl = new_entry + min_dist
            new_sl_distance = abs(new_entry - new_sl)
    sign = 1 if zone.direction == Direction.BULLISH else -1
    if target_rr_cap > 0.0:
        new_tp = new_entry + sign * target_rr_cap * new_sl_distance
    new_sl_pct = new_sl_distance / new_entry
    # Mirror rr_system's 2026-04-19 ceil-sizing contract here: when the
    # original plan was ceil-sized (not capped), re-size with ceil so the
    # zone-adjusted realized loss stays ≥ plan.risk_amount_usdt with
    # bounded overshoot (< one per_contract_cost). Capped plans keep
    # floor — respecting the leverage/margin ceiling wins over the
    # equal-risk target. Without this mirror, zone entries floor-rounded
    # while market/legacy entries ceiled, producing the $2-$13 $R spread
    # the operator observed across the 5 open positions on 2026-04-20.
    risk = plan.risk_amount_usdt
    denom = new_sl_pct + plan.fee_reserve_pct
    per_contract_usdt = new_entry * contract_size
    per_contract_cost = denom * per_contract_usdt
    if per_contract_cost <= 0 or per_contract_usdt <= 0:
        num_contracts = 0
    elif plan.capped:
        notional = risk / denom if denom > 0 else risk / new_sl_pct
        num_contracts = int(notional / per_contract_usdt)
    else:
        num_contracts = math.ceil(risk / per_contract_cost)
    actual_notional = num_contracts * per_contract_usdt
    actual_risk = actual_notional * new_sl_pct
    tp_distance = abs(new_tp - new_entry)
    new_rr = tp_distance / new_sl_distance if new_sl_distance > 0 else 0.0
    ladder_src = zone.tp_ladder if zone.tp_ladder else ((new_tp, 1.0),)
    if target_rr_cap > 0.0:
        cap_boundary = new_entry + sign * target_rr_cap * new_sl_distance
        ladder = []
        for p, s in ladder_src:
            pf = float(p)
            if sign > 0 and pf > cap_boundary:
                pf = cap_boundary
            elif sign < 0 and pf < cap_boundary:
                pf = cap_boundary
            ladder.append((pf, float(s)))
    else:
        ladder = [(float(p), float(s)) for p, s in ladder_src]

    return TradePlan(
        direction=plan.direction,
        entry_price=new_entry,
        sl_price=new_sl,
        tp_price=new_tp,
        rr_ratio=new_rr,
        sl_distance=new_sl_distance,
        sl_pct=new_sl_pct,
        position_size_usdt=actual_notional,
        leverage=plan.leverage,
        required_leverage=plan.required_leverage,
        num_contracts=num_contracts,
        risk_amount_usdt=actual_risk,
        max_risk_usdt=plan.max_risk_usdt,
        capped=plan.capped,
        fee_reserve_pct=plan.fee_reserve_pct,
        sl_source=f"zone_{zone.zone_source}",
        confluence_score=plan.confluence_score,
        confluence_factors=list(plan.confluence_factors),
        reason=f"{plan.reason} | zone={zone.zone_source}",
        tp_ladder=ladder,
    )
