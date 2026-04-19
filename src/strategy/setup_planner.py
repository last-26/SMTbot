"""Zone-based entry planner (Phase 7.C1).

Converts a directional signal (confluence + HTF trend-picker) into a
concrete `ZoneSetup`: a price range to limit-order into, a structural SL
beyond the zone, and a TP target from the surrounding liquidity
landscape. The entry orchestrator (Phase 7.C4) consumes the ZoneSetup
and places a maker-preferred limit order.

Four zone sources, priority order — first hit wins:
  1. Coinalyze liquidity pool (heatmap cluster on the correct side)
  2. HTF 15m unfilled FVG (Pine-sourced via htf_state_cache)
  3. Session VWAP retest (pullback to 1m/3m/15m session-anchored VWAP)
  4. Recent-swing sweep retest (last sweep reclaimed, now a retest zone)

Pure function — no I/O, no state. Tests drive it with synthesised
MarketState and heatmap payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from src.analysis.liquidity_heatmap import LiquidityHeatmap
from src.data.models import Direction, MarketState
from src.strategy.trade_plan import TradePlan


ZoneSource = Literal["liq_pool", "fvg_htf", "vwap_retest", "sweep_retest"]
TriggerType = Literal["zone_touch", "sweep_reversal", "displacement_return"]


@dataclass(frozen=True)
class ZoneSetup:
    """Limit-order setup to be placed at a structural zone."""
    direction: Direction
    entry_zone: tuple[float, float]    # (low, high), low <= high
    trigger_type: TriggerType
    sl_beyond_zone: float              # structural stop, not % floor
    tp_primary: float                  # next liquidity or HTF target
    max_wait_bars: int
    zone_source: ZoneSource


def _is_long(direction: Direction) -> bool:
    return direction == Direction.BULLISH


def _liq_pool_zone(
    direction: Direction, price: float, atr: float,
    heatmap: Optional[LiquidityHeatmap], zone_atr: float,
) -> Optional[tuple[float, float]]:
    """Nearest heatmap cluster on the correct side of price, ± zone_atr × ATR.

    - Long: cluster below price (long-liquidation pool = support bid)
    - Short: cluster above price
    Skip clusters with zero notional (missing data sentinel).
    """
    if heatmap is None:
        return None
    cluster = heatmap.nearest_below if _is_long(direction) else heatmap.nearest_above
    if cluster is None or cluster.notional_usd <= 0:
        return None
    # Guard: cluster must be on the correct side (heatmap should enforce but
    # demo data has shown stale above/below splits after price crosses a level).
    if _is_long(direction) and cluster.price >= price:
        return None
    if not _is_long(direction) and cluster.price <= price:
        return None
    half = zone_atr * atr
    return (cluster.price - half, cluster.price + half)


def _htf_fvg_zone(
    direction: Direction, price: float,
    htf_state: Optional[MarketState],
) -> Optional[tuple[float, float]]:
    """Nearest unfilled HTF 15m FVG on the correct side of price."""
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


def _vwap_zone(
    direction: Direction, price: float, atr: float,
    state: MarketState, zone_atr: float,
) -> Optional[tuple[float, float]]:
    """Nearest parsed session VWAP on the correct side of price."""
    sig = state.signal_table
    raw = [sig.vwap_1m, sig.vwap_3m, sig.vwap_15m]
    candidates = [
        v for v in raw if v and v > 0
        and ((_is_long(direction) and v < price)
             or (not _is_long(direction) and v > price))
    ]
    if not candidates:
        return None
    target = min(candidates, key=lambda v: abs(price - v))
    half = zone_atr * atr
    return (target - half, target + half)


def _sweep_zone(
    direction: Direction, price: float, atr: float,
    state: MarketState, zone_atr: float,
) -> Optional[tuple[float, float]]:
    """Most recent sweep of the opposite side as a retest zone.

    A bearish sweep (flushed upside liquidity) leaves a bullish setup at
    the reclaimed level — and vice versa. Sweep must still sit on the
    correct side of current price.
    """
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


def _sl_beyond_zone(
    direction: Direction, zone: tuple[float, float],
    atr: float, sl_buffer_atr: float,
) -> float:
    """SL sits `sl_buffer_atr × ATR` beyond the far edge against direction."""
    low, high = zone
    buf = sl_buffer_atr * atr
    return (low - buf) if _is_long(direction) else (high + buf)


def _tp_primary(
    direction: Direction, zone: tuple[float, float],
    heatmap: Optional[LiquidityHeatmap], atr: float, default_rr: float,
) -> float:
    """TP = nearest cluster in direction (when on correct side), else RR × zone-width."""
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


def build_zone_setup(
    *,
    direction: Direction,
    state: MarketState,
    htf_state: Optional[MarketState] = None,
    heatmap: Optional[LiquidityHeatmap] = None,
    zone_buffer_atr: float = 0.25,
    sl_buffer_atr: float = 0.5,
    max_wait_bars: int = 10,
    default_rr: float = 2.0,
) -> Optional[ZoneSetup]:
    """Return the best `ZoneSetup` for *direction*, or None if no source fits.

    - `state`: entry-TF MarketState (sweeps, VWAPs, current price, ATR)
    - `htf_state`: HTF 15m MarketState (cached via Phase 7.B4) for HTF FVGs
    - `heatmap`: LiquidityHeatmap from derivatives layer; falls back to
       `state.liquidity_heatmap` when not explicitly passed
    """
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return None
    price = state.current_price
    atr = state.atr
    if atr <= 0 or price <= 0:
        return None

    hm = heatmap if heatmap is not None else state.liquidity_heatmap

    sources: list[tuple[ZoneSource, TriggerType, Optional[tuple[float, float]]]] = [
        ("liq_pool", "zone_touch",
            _liq_pool_zone(direction, price, atr, hm, zone_buffer_atr)),
        ("fvg_htf", "zone_touch",
            _htf_fvg_zone(direction, price, htf_state)),
        ("vwap_retest", "zone_touch",
            _vwap_zone(direction, price, atr, state, zone_buffer_atr)),
        ("sweep_retest", "sweep_reversal",
            _sweep_zone(direction, price, atr, state, zone_buffer_atr)),
    ]

    for source, trigger, zone in sources:
        if zone is None:
            continue
        sl = _sl_beyond_zone(direction, zone, atr, sl_buffer_atr)
        tp = _tp_primary(direction, zone, hm, atr, default_rr)
        return ZoneSetup(
            direction=direction,
            entry_zone=zone,
            trigger_type=trigger,
            sl_beyond_zone=sl,
            tp_primary=tp,
            max_wait_bars=max_wait_bars,
            zone_source=source,
        )
    return None


def zone_limit_price(direction: Direction, zone: tuple[float, float]) -> float:
    """Limit-entry price for `direction` inside `zone`.

    Long: near edge (low) — buy-limit must rest below market.
    Short: near edge (high) — sell-limit must rest above market.
    """
    low, high = zone
    return low if _is_long(direction) else high


def apply_zone_to_plan(
    plan: TradePlan, zone: "ZoneSetup", contract_size: float,
) -> TradePlan:
    """Return a new TradePlan with entry/SL/TP taken from *zone*, re-sized
    so total USDT risk on the structural SL equals `plan.risk_amount_usdt`.

    Why re-size: the original plan was sized against the minimum-% SL floor.
    The zone's structural SL can be wider or narrower, so leaving contracts
    unchanged would drift risk. Leverage + risk budget are preserved; TP is
    overridden with the zone's primary target (usually an HTF liquidity
    cluster), which shifts realized RR — logged via `plan.rr_ratio`.
    """
    if plan.direction != zone.direction:
        raise ValueError(
            f"direction mismatch: plan={plan.direction} zone={zone.direction}"
        )
    new_entry = zone_limit_price(zone.direction, zone.entry_zone)
    new_sl = zone.sl_beyond_zone
    new_tp = zone.tp_primary
    new_sl_distance = abs(new_entry - new_sl)
    if new_entry <= 0 or new_sl_distance <= 0:
        raise ValueError(
            f"degenerate zone: entry={new_entry} sl={new_sl} dist={new_sl_distance}"
        )
    new_sl_pct = new_sl_distance / new_entry
    risk = plan.risk_amount_usdt
    denom = new_sl_pct + plan.fee_reserve_pct
    notional = risk / denom if denom > 0 else risk / new_sl_pct
    num_contracts = max(1, int(notional / (new_entry * contract_size)))
    actual_notional = num_contracts * new_entry * contract_size
    actual_risk = actual_notional * new_sl_pct
    tp_distance = abs(new_tp - new_entry)
    new_rr = tp_distance / new_sl_distance if new_sl_distance > 0 else 0.0

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
    )
