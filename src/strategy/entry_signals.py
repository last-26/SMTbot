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

from dataclasses import dataclass
from datetime import datetime, timezone
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
from src.strategy._indicators import ema
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


def in_vwap_reset_blackout(
    now: datetime,
    *,
    pre_minutes: int,
    post_minutes: int,
) -> bool:
    """True when ``now`` falls inside the daily VWAP-reset blackout window.

    Pine-side VWAPs (1m / 3m / 15m, all `ta.vwap()` derived) anchor on the
    daily session change → reset at UTC 00:00. For the first ~10-30 min
    after reset the ±1σ band is collapsed (stdev ≈ 0) and the
    `vwap_composite_alignment` soft pillar reads near-noise.

    Window is symmetric around UTC midnight:
      [00:00 - pre_minutes, 00:00 + post_minutes)

    Both ends are clamped to a single 60-min half so the window cannot
    span a full day; the config validator enforces [0, 60] inputs.

    A non-UTC datetime is converted to UTC before testing. Naive
    datetimes are treated as already-UTC (matches `_utc_now()` helper
    in the runner which always emits tz-aware UTC).
    """
    if pre_minutes <= 0 and post_minutes <= 0:
        return False
    if now.tzinfo is None:
        utc_now = now.replace(tzinfo=timezone.utc)
    else:
        utc_now = now.astimezone(timezone.utc)
    minutes_into_day = utc_now.hour * 60 + utc_now.minute
    if minutes_into_day < post_minutes:
        return True
    if minutes_into_day >= (24 * 60 - pre_minutes):
        return True
    return False


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
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
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


def _stablecoin_pulse_penalty(
    *,
    direction: Direction,
    pulse_usd: Optional[float],
    threshold_usd: float,
    penalty: float,
) -> float:
    """Return the additive confluence-threshold penalty when the hourly
    stablecoin pulse opposes the proposed direction.

    Rule (mirrors plan §2):
      * Long + pulse <= -threshold → misaligned, +penalty.
      * Short + pulse >= +threshold → misaligned, +penalty.
      * otherwise (aligned / below threshold / None) → 0.
    """
    if penalty <= 0.0:
        return 0.0
    if pulse_usd is None:
        return 0.0
    is_long = direction == Direction.BULLISH
    misaligned = (
        (is_long and pulse_usd <= -float(threshold_usd))
        or (not is_long and pulse_usd >= float(threshold_usd))
    )
    return penalty if misaligned else 0.0


def _flow_alignment_score(
    stablecoin_pulse_1h_usd: Optional[float],
    btc_netflow_24h_usd: Optional[float],
    eth_netflow_24h_usd: Optional[float],
    noise_floor_usd: float = 1_000_000.0,
    coinbase_netflow_24h_usd: Optional[float] = None,
    binance_netflow_24h_usd: Optional[float] = None,
    bybit_netflow_24h_usd: Optional[float] = None,
) -> float:
    """Return a directional alignment score in [-1, +1].

    Combines up to six Arkham signals into a single directional bias:
      * +1 → strong bullish alignment (stablecoin inflow + BTC/ETH/entity
        outflow from CEX; cash arriving, supply leaving)
      * -1 → strong bearish alignment (stables leaving + BTC/ETH/entity
        arriving; profit taking pattern)
      *  0 → neutral / conflicting / insufficient data

    Weights (sum = 1.0):
      * stablecoin_pulse    0.25  — hourly, timeliest signal
      * BTC netflow         0.25  — market leader, daily cadence
      * ETH netflow         0.15  — secondary leader
      * Coinbase netflow    0.15  — "Coinbase premium" institutional pattern
      * Binance netflow     0.10  — primarily Asia retail, noisier
      * Bybit netflow       0.10  — leverage/derivatives flow

    BTC/ETH and all per-entity netflows use INVERTED sign semantics: a
    negative (OUT of CEX) value is bullish, because supply is being
    withdrawn. Stablecoin pulse uses natural sign: positive (IN to CEX)
    is bullish, because buying power is arriving.

    Each input below `noise_floor_usd` contributes 0 (not ±1), so low-
    magnitude flow ticks don't drag the score around. None inputs also
    contribute 0 — fail-open when Arkham data is missing.

    2026-04-22 (gece): initial 3-input version (stable + BTC + ETH) shipped
    with whale hard-gate removal. 2026-04-22 (gece, late): extended with
    Coinbase + Binance + Bybit per-entity netflow when those journal-only
    columns were promoted to runtime ahead of the Pass 1 clean-restart.
    Pass 2 re-tunes all weights on uniform-coverage data.
    """
    def _sign(x: Optional[float]) -> int:
        if x is None:
            return 0
        if abs(x) < float(noise_floor_usd):
            return 0
        return 1 if x > 0 else -1

    stable_dir = _sign(stablecoin_pulse_1h_usd)
    # BTC/ETH + per-entity netflows: OUT of CEX (negative) is bullish → invert.
    btc_dir = -_sign(btc_netflow_24h_usd)
    eth_dir = -_sign(eth_netflow_24h_usd)
    coinbase_dir = -_sign(coinbase_netflow_24h_usd)
    binance_dir = -_sign(binance_netflow_24h_usd)
    bybit_dir = -_sign(bybit_netflow_24h_usd)
    score = (
        0.25 * stable_dir
        + 0.25 * btc_dir
        + 0.15 * eth_dir
        + 0.15 * coinbase_dir
        + 0.10 * binance_dir
        + 0.10 * bybit_dir
    )
    return max(-1.0, min(1.0, score))


def _flow_alignment_penalty(
    *,
    direction: Direction,
    score: float,
    penalty: float,
) -> float:
    """Additive confluence-threshold bump when the flow score opposes direction.

    Rule (mirrors `_stablecoin_pulse_penalty` / `_altcoin_index_penalty`):
      * long + score < 0   → misaligned (bearish flow regime), +penalty*|score|
      * short + score > 0  → misaligned (bullish flow regime), +penalty*|score|
      * aligned or |score|=0 → 0 (no bump)

    Scales linearly with |score| so weak signals pay a soft penalty and
    strong aligned moves pay the full magnitude. 2026-04-22 default
    `penalty=0.25` in YAML (Pass 2 will tune).
    """
    if penalty <= 0.0:
        return 0.0
    if score == 0.0:
        return 0.0
    dir_sign = 1 if direction == Direction.BULLISH else -1
    if dir_sign * score >= 0.0:
        return 0.0
    return float(penalty) * abs(float(score))


def _per_symbol_cex_flow_penalty(
    *,
    direction: Direction,
    symbol_netflow_1h_usd: Optional[float],
    noise_floor_usd: float,
    penalty: float,
) -> float:
    """Additive confluence-threshold bump when the traded symbol's own
    1h CEX volume opposes the proposed direction.

    Semantics (tokens behave OPPOSITE to stablecoins here):
      * Token flowing INTO exchange (positive netflow) → preparing to
        sell → BEARISH for that symbol.
      * Token flowing OUT (negative netflow) → moving to cold / DEX →
        BULLISH for that symbol.
    So:
      * Long + netflow ≥ +floor  → misaligned, +penalty
      * Short + netflow ≤ -floor → misaligned, +penalty
      * Aligned / below floor / None → 0

    Data source: `on_chain_snapshots.token_volume_1h_net_usd_json[symbol]`
    from Arkham `/token/volume/{id}?granularity=1h`. Journal-only until
    2026-04-22 (gece, late) when promoted to runtime ahead of the
    Pass 1 clean restart.

    `noise_floor_usd` defaults to $5M in YAML (token volume is noisier
    than stablecoin pulse — token prints in $1M range are routine).
    """
    if penalty <= 0.0:
        return 0.0
    if symbol_netflow_1h_usd is None:
        return 0.0
    magnitude = abs(symbol_netflow_1h_usd)
    if magnitude < float(noise_floor_usd):
        return 0.0
    is_long = direction == Direction.BULLISH
    misaligned = (
        (is_long and symbol_netflow_1h_usd >= float(noise_floor_usd))
        or (not is_long and symbol_netflow_1h_usd <= -float(noise_floor_usd))
    )
    return float(penalty) if misaligned else 0.0


def _altcoin_index_penalty(
    *,
    direction: Direction,
    index_value: Optional[int],
    is_altcoin: bool,
    bearish_threshold: int,
    bullish_threshold: int,
    penalty: float,
) -> float:
    """Return the additive confluence-threshold penalty for altcoin
    trades that oppose the prevailing Arkham altcoin index regime.

    The index is a scalar 0–100 (low → altcoins underperforming BTC,
    high → altcoins outperforming). This penalty only bites on altcoin
    symbols — majors (BTC / ETH) never pay it.

    Rule:
      * altcoin LONG + index <= bearish_threshold → misaligned
        (trading an alt long in BTC-dominance season), +penalty.
      * altcoin SHORT + index >= bullish_threshold → misaligned
        (fading alts during altseason), +penalty.
      * `is_altcoin=False` → never applies.
      * Index in the neutral band (bearish < v < bullish) → no penalty.
      * `index_value=None` → no signal, no penalty.
    """
    if penalty <= 0.0:
        return 0.0
    if not is_altcoin:
        return 0.0
    if index_value is None:
        return 0.0
    is_long = direction == Direction.BULLISH
    if is_long and index_value <= int(bearish_threshold):
        return penalty
    if (not is_long) and index_value >= int(bullish_threshold):
        return penalty
    return 0.0


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
    htf_state: Optional[object] = None,
    min_rsi_mfi_magnitude: float = 2.0,
    liquidity_pool_max_atr_dist: float = 3.0,
    displacement_atr_mult: float = 1.5,
    displacement_max_bars_ago: int = 5,
    divergence_fresh_bars: int = 3,
    divergence_decay_bars: int = 6,
    divergence_max_bars: int = 9,
    trend_regime: Optional[TrendRegime] = None,
    trend_regime_conditional_scoring_enabled: bool = False,
    daily_bias_enabled: bool = False,
    daily_bias_delta: float = 0.0,
    stablecoin_pulse_enabled: bool = False,
    stablecoin_pulse_usd: Optional[float] = None,
    stablecoin_pulse_threshold_usd: float = 50_000_000.0,
    stablecoin_pulse_penalty: float = 0.5,
    altcoin_index_enabled: bool = False,
    altcoin_index_value: Optional[int] = None,
    altcoin_index_is_altcoin: bool = False,
    altcoin_index_bearish_threshold: int = 25,
    altcoin_index_bullish_threshold: int = 75,
    altcoin_index_penalty: float = 0.5,
    flow_alignment_enabled: bool = False,
    flow_alignment_penalty: float = 0.0,
    flow_alignment_noise_floor_usd: float = 1_000_000.0,
    flow_alignment_btc_netflow_24h_usd: Optional[float] = None,
    flow_alignment_eth_netflow_24h_usd: Optional[float] = None,
    flow_alignment_coinbase_netflow_24h_usd: Optional[float] = None,
    flow_alignment_binance_netflow_24h_usd: Optional[float] = None,
    flow_alignment_bybit_netflow_24h_usd: Optional[float] = None,
    per_symbol_cex_flow_enabled: bool = False,
    per_symbol_cex_flow_usd: Optional[float] = None,
    per_symbol_cex_flow_noise_floor_usd: float = 5_000_000.0,
    per_symbol_cex_flow_penalty: float = 0.0,
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
        daily_bias_enabled=daily_bias_enabled,
        daily_bias_delta=daily_bias_delta,
    )
    # 2026-04-21 — Arkham confluence-threshold penalties (Phase E + F2).
    # Both are additive bumps to the effective `min_confluence_score`
    # applied AFTER calculate_confluence so direction is resolved.
    # Below-threshold setups reject under `below_confluence` (no new
    # reject string) but via an adjusted bar. Aligned / missing-signal
    # → no penalty.
    effective_min_conf = float(min_confluence_score)
    if stablecoin_pulse_enabled:
        effective_min_conf += _stablecoin_pulse_penalty(
            direction=confluence.direction,
            pulse_usd=stablecoin_pulse_usd,
            threshold_usd=stablecoin_pulse_threshold_usd,
            penalty=stablecoin_pulse_penalty,
        )
    if altcoin_index_enabled:
        effective_min_conf += _altcoin_index_penalty(
            direction=confluence.direction,
            index_value=altcoin_index_value,
            is_altcoin=altcoin_index_is_altcoin,
            bearish_threshold=altcoin_index_bearish_threshold,
            bullish_threshold=altcoin_index_bullish_threshold,
            penalty=altcoin_index_penalty,
        )
    if flow_alignment_enabled:
        alignment = _flow_alignment_score(
            stablecoin_pulse_1h_usd=stablecoin_pulse_usd,
            btc_netflow_24h_usd=flow_alignment_btc_netflow_24h_usd,
            eth_netflow_24h_usd=flow_alignment_eth_netflow_24h_usd,
            noise_floor_usd=flow_alignment_noise_floor_usd,
            coinbase_netflow_24h_usd=flow_alignment_coinbase_netflow_24h_usd,
            binance_netflow_24h_usd=flow_alignment_binance_netflow_24h_usd,
            bybit_netflow_24h_usd=flow_alignment_bybit_netflow_24h_usd,
        )
        effective_min_conf += _flow_alignment_penalty(
            direction=confluence.direction,
            score=alignment,
            penalty=flow_alignment_penalty,
        )
    if per_symbol_cex_flow_enabled:
        effective_min_conf += _per_symbol_cex_flow_penalty(
            direction=confluence.direction,
            symbol_netflow_1h_usd=per_symbol_cex_flow_usd,
            noise_floor_usd=per_symbol_cex_flow_noise_floor_usd,
            penalty=per_symbol_cex_flow_penalty,
        )
    if not confluence.is_tradable(effective_min_conf):
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
    risk_amount_usdt_override: Optional[float] = None,
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
    htf_sr_zones: Optional[list[SRZone]] = None,
    htf_sr_ceiling_enabled: bool = False,
    htf_sr_buffer_atr: float = 0.2,
    crowded_skip_enabled: bool = False,
    crowded_skip_z_threshold: float = 3.0,
    ltf_state: Optional[object] = None,
    htf_state: Optional[object] = None,
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
        risk_amount_usdt_override=risk_amount_usdt_override,
        sl_buffer_mult=sl_buffer_mult,
        swing_lookback=swing_lookback,
        atr_fallback_mult=atr_fallback_mult,
        htf_sr_zones=htf_sr_zones,
        htf_sr_ceiling_enabled=htf_sr_ceiling_enabled,
        htf_sr_buffer_atr=htf_sr_buffer_atr,
        crowded_skip_enabled=crowded_skip_enabled,
        crowded_skip_z_threshold=crowded_skip_z_threshold,
        ltf_state=ltf_state,
        htf_state=htf_state,
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
    risk_amount_usdt_override: Optional[float] = None,
    sl_buffer_mult: float = 0.2,
    swing_lookback: int = 20,
    atr_fallback_mult: float = 2.0,
    htf_sr_zones: Optional[list[SRZone]] = None,
    htf_sr_ceiling_enabled: bool = False,
    htf_sr_buffer_atr: float = 0.2,
    crowded_skip_enabled: bool = False,
    crowded_skip_z_threshold: float = 3.0,
    ltf_state: Optional[object] = None,
    htf_state: Optional[object] = None,
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
    daily_bias_enabled: bool = False,
    daily_bias_delta: float = 0.0,
    stablecoin_pulse_enabled: bool = False,
    stablecoin_pulse_usd: Optional[float] = None,
    stablecoin_pulse_threshold_usd: float = 50_000_000.0,
    stablecoin_pulse_penalty: float = 0.5,
    altcoin_index_enabled: bool = False,
    altcoin_index_value: Optional[int] = None,
    altcoin_index_is_altcoin: bool = False,
    altcoin_index_bearish_threshold: int = 25,
    altcoin_index_bullish_threshold: int = 75,
    altcoin_index_penalty: float = 0.5,
    # 2026-04-22 — flow_alignment soft signal. Replaces whale-blackout
    # directional intuition. Misaligned direction pays additive threshold
    # penalty scaled by |score|. Stablecoin input reuses
    # `stablecoin_pulse_usd`; BTC/ETH netflow + per-entity (Coinbase/
    # Binance/Bybit) netflow come from the Arkham daily snapshot
    # (UTC-day completed series). Per-entity inputs added 2026-04-22
    # (gece, late) when those journal-only columns were promoted to
    # runtime ahead of the Pass 1 clean restart.
    flow_alignment_enabled: bool = False,
    flow_alignment_penalty: float = 0.0,
    flow_alignment_noise_floor_usd: float = 1_000_000.0,
    flow_alignment_btc_netflow_24h_usd: Optional[float] = None,
    flow_alignment_eth_netflow_24h_usd: Optional[float] = None,
    flow_alignment_coinbase_netflow_24h_usd: Optional[float] = None,
    flow_alignment_binance_netflow_24h_usd: Optional[float] = None,
    flow_alignment_bybit_netflow_24h_usd: Optional[float] = None,
    # 2026-04-22 (gece, late) — per-symbol 1h CEX volume penalty.
    # Symbol's own token flow: INTO CEX = bearish for that symbol, OUT = bullish.
    # Misaligned direction pays additive threshold bump. Source: Arkham
    # `/token/volume/{id}?granularity=1h` most-recent-bucket `inUSD - outUSD`.
    per_symbol_cex_flow_enabled: bool = False,
    per_symbol_cex_flow_usd: Optional[float] = None,
    per_symbol_cex_flow_noise_floor_usd: float = 5_000_000.0,
    per_symbol_cex_flow_penalty: float = 0.0,
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
        daily_bias_enabled=daily_bias_enabled,
        daily_bias_delta=daily_bias_delta,
        stablecoin_pulse_enabled=stablecoin_pulse_enabled,
        stablecoin_pulse_usd=stablecoin_pulse_usd,
        stablecoin_pulse_threshold_usd=stablecoin_pulse_threshold_usd,
        stablecoin_pulse_penalty=stablecoin_pulse_penalty,
        altcoin_index_enabled=altcoin_index_enabled,
        altcoin_index_value=altcoin_index_value,
        altcoin_index_is_altcoin=altcoin_index_is_altcoin,
        altcoin_index_bearish_threshold=altcoin_index_bearish_threshold,
        altcoin_index_bullish_threshold=altcoin_index_bullish_threshold,
        altcoin_index_penalty=altcoin_index_penalty,
        flow_alignment_enabled=flow_alignment_enabled,
        flow_alignment_penalty=flow_alignment_penalty,
        flow_alignment_noise_floor_usd=flow_alignment_noise_floor_usd,
        flow_alignment_btc_netflow_24h_usd=flow_alignment_btc_netflow_24h_usd,
        flow_alignment_eth_netflow_24h_usd=flow_alignment_eth_netflow_24h_usd,
        flow_alignment_coinbase_netflow_24h_usd=flow_alignment_coinbase_netflow_24h_usd,
        flow_alignment_binance_netflow_24h_usd=flow_alignment_binance_netflow_24h_usd,
        flow_alignment_bybit_netflow_24h_usd=flow_alignment_bybit_netflow_24h_usd,
        per_symbol_cex_flow_enabled=per_symbol_cex_flow_enabled,
        per_symbol_cex_flow_usd=per_symbol_cex_flow_usd,
        per_symbol_cex_flow_noise_floor_usd=per_symbol_cex_flow_noise_floor_usd,
        per_symbol_cex_flow_penalty=per_symbol_cex_flow_penalty,
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
            daily_bias_enabled=daily_bias_enabled,
            daily_bias_delta=daily_bias_delta,
        )
        # Apply the same penalties (Phase E + F2) to the diagnostic
        # check so reject reasons stay consistent with the primary path.
        effective_min_conf = float(min_confluence_score)
        if stablecoin_pulse_enabled:
            effective_min_conf += _stablecoin_pulse_penalty(
                direction=conf.direction,
                pulse_usd=stablecoin_pulse_usd,
                threshold_usd=stablecoin_pulse_threshold_usd,
                penalty=stablecoin_pulse_penalty,
            )
        if altcoin_index_enabled:
            effective_min_conf += _altcoin_index_penalty(
                direction=conf.direction,
                index_value=altcoin_index_value,
                is_altcoin=altcoin_index_is_altcoin,
                bearish_threshold=altcoin_index_bearish_threshold,
                bullish_threshold=altcoin_index_bullish_threshold,
                penalty=altcoin_index_penalty,
            )
        if flow_alignment_enabled:
            alignment = _flow_alignment_score(
                stablecoin_pulse_1h_usd=stablecoin_pulse_usd,
                btc_netflow_24h_usd=flow_alignment_btc_netflow_24h_usd,
                eth_netflow_24h_usd=flow_alignment_eth_netflow_24h_usd,
                noise_floor_usd=flow_alignment_noise_floor_usd,
                coinbase_netflow_24h_usd=flow_alignment_coinbase_netflow_24h_usd,
                binance_netflow_24h_usd=flow_alignment_binance_netflow_24h_usd,
                bybit_netflow_24h_usd=flow_alignment_bybit_netflow_24h_usd,
            )
            effective_min_conf += _flow_alignment_penalty(
                direction=conf.direction,
                score=alignment,
                penalty=flow_alignment_penalty,
            )
        if per_symbol_cex_flow_enabled:
            effective_min_conf += _per_symbol_cex_flow_penalty(
                direction=conf.direction,
                symbol_netflow_1h_usd=per_symbol_cex_flow_usd,
                noise_floor_usd=per_symbol_cex_flow_noise_floor_usd,
                penalty=per_symbol_cex_flow_penalty,
            )
        if not conf.is_tradable(effective_min_conf):
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

    # 2026-04-22 (gece) — Arkham whale-transfer HARD GATE REMOVED.
    # Previously (Phase D, 2026-04-21): a $150M+ CEX↔CEX transfer rejected
    # new entries for the affected symbol(s) for 10 min. Operator feedback:
    # whale events are directionally ambiguous, not neutral — gating kills
    # both winners and losers with roughly equal probability. New model:
    # the WS listener streams events into `whale_transfers` journal (for
    # Phase 9 GBT) and the `flow_alignment_penalty` soft signal replaces
    # the directional intuition. See CLAUDE.md changelog 2026-04-22 (gece).

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

    pillar_scores = {
        f.name: float(f.weight) for f in intent.confluence.factors
    }
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
        risk_amount_usdt_override=risk_amount_usdt_override,
        sl_source=intent.sl_source,
        confluence_score=intent.confluence.score,
        confluence_factors=intent.confluence.factor_names,
        confluence_pillar_scores=pillar_scores,
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


def evaluate_pending_invalidation_gates(
    *,
    state: MarketState,
    candles: list[Candle],
    direction: Direction,
    entry_price: float,
    pillar_opposition: bool = False,
    vwap_hard_veto_enabled: bool = True,
    ema_veto_enabled: bool = True,
    ema_veto_fast_period: int = 9,
    ema_veto_slow_period: int = 21,
    now: Optional[datetime] = None,
    vwap_reset_blackout_enabled: bool = False,
    vwap_reset_blackout_pre_minutes: int = 0,
    vwap_reset_blackout_post_minutes: int = 0,
) -> Optional[str]:
    """Re-evaluate the HARD veto gates against current state for a pending limit.

    Used by the runner's pending-poll cycle (2026-04-22) to detect mid-pending
    invalidation: a sharp market turn between limit placement and the 7-bar
    (21-min) timeout. If any gate would now reject a NEW entry of the same
    direction, the pending limit is canceled before a fill at a no-longer-
    favorable level.

    Returns the first failing gate's reject_reason string, or None if all
    pass (pending should remain active).

    Deliberately limited to HARD vetoes — confluence rescore is NOT done
    because the strategy is pullback-based and confluence naturally
    fluctuates during the wait window. Only "would now reject a NEW entry
    of the same direction" cases trigger cancel; price drifting away from
    the limit (which IS the pullback by design) does not.

    Order matches the live entry path in `build_trade_plan_with_reason` so
    rejected_signals reasons are consistent with the new-entry vocabulary:
      vwap_misaligned → ema_momentum_contra → cross_asset_opposition

    2026-04-22 (gece): whale_transfer_blackout gate removed (paired with
    the new-entry path). flow_alignment is a SOFT signal, not evaluated
    here (it shifts confluence threshold; rescore disabled by design).

    2026-04-26: vwap_reset_blackout added. Re-evaluates the time-based
    UTC-midnight blackout on every poll so a pending placed at e.g.
    23:50 with a 7-bar timeout window doesn't fill at 00:05 when the
    fresh-session VWAP is unreliable. Pure time check; uses ``now`` if
    supplied (test seam) else ``datetime.now(tz=utc)``.
    """
    if vwap_reset_blackout_enabled and (
        vwap_reset_blackout_pre_minutes > 0
        or vwap_reset_blackout_post_minutes > 0
    ):
        ref = now if now is not None else datetime.now(tz=timezone.utc)
        if in_vwap_reset_blackout(
            ref,
            pre_minutes=vwap_reset_blackout_pre_minutes,
            post_minutes=vwap_reset_blackout_post_minutes,
        ):
            return "vwap_reset_blackout"

    if vwap_hard_veto_enabled and _vwap_hard_veto(state, direction, entry_price):
        return "vwap_misaligned"

    if ema_veto_enabled and _ema_momentum_veto(
        candles, direction, entry_price,
        fast_period=ema_veto_fast_period,
        slow_period=ema_veto_slow_period,
    ):
        return "ema_momentum_contra"

    if _cross_asset_opposes(pillar_opposition, direction):
        return "cross_asset_opposition"

    # 2026-04-22 (gece) — whale_transfer_blackout gate removed here too,
    # paired with the new-entry path. Pending-limit invalidation now fires
    # only on vwap/ema/cross-asset flips. See changelog.

    return None


def _replace_tp(plan: TradePlan, *, new_tp: float, new_rr: float) -> TradePlan:
    """Return a new TradePlan with tp_price/rr_ratio swapped (dataclass copy)."""
    from dataclasses import replace
    return replace(plan, tp_price=new_tp, rr_ratio=new_rr)
