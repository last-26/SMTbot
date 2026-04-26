"""BotRunner — the async outer loop that wires every subsystem.

Shape of one tick (`run_once`):
  1. Fetch MarketState + recent candles from the TV bridge.
  2. Drain closed-position fills from PositionMonitor → enrich PnL via Bybit
     closed-pnl → record_close in journal → update RiskManager.
  3. If any position is already open on our symbol, skip open-attempts
     this tick (symbol-level dedup — `SignalTableData.last_bar` isn't a
     parsed field, so we can't do bar-level dedup without a data-layer
     change).
  4. Build a TradePlan; run it through RiskManager.can_trade(); if it
     passes, place via OrderRouter (or dry_run_report when --dry-run).
  5. Register in-memory state FIRST (monitor + risk_mgr) then journal —
     if the DB write fails we still track the live position so the next
     close is handled; startup reconciliation flags the orphan on restart.

`from_config` wires production components; tests construct `BotContext`
directly with fakes — the runner itself only depends on duck-typed
interfaces (reader.read_market_state, router.place, monitor.poll,
monitor.register_open, bybit_client.enrich_close_fill / get_positions).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from src.analysis.liquidity_heatmap import build_heatmap
from src.analysis.multi_timeframe import calculate_confluence
from src.analysis.support_resistance import detect_sr_zones
from src.analysis.trend_regime import TrendRegime, classify_trend_regime
from src.bot.config import BotConfig
from src.bot.lifecycle import install_shutdown_handlers
from src.data.candle_buffer import MultiTFBuffer
from src.data.derivatives_api import CoinalyzeClient
from src.data.derivatives_cache import DerivativesCache
from src.data.economic_calendar import (
    EconomicCalendarService,
    FairEconomyClient,
    FinnhubClient,
)
from src.data.liquidation_stream import LiquidationStream
from src.data.ltf_reader import LTFReader, LTFState
from src.data.models import Direction, MarketState, Session
from src.data.on_chain import (
    ArkhamClient,
    fetch_daily_snapshot,
    fetch_entity_netflow_24h,
    fetch_entity_per_asset_netflow_24h,
    fetch_hourly_stablecoin_pulse,
    fetch_token_volume_last_hour,
)
from src.data.on_chain_types import (
    OnChainSnapshot,
    WATCHED_SYMBOL_TO_TOKEN_ID,
    WhaleBlackoutState,
)
from src.data.on_chain_ws import ArkhamWebSocketListener
from src.data.public_market_feed import (
    BinancePublicClient,
    RealCandle,
    internal_to_binance_futures,
    price_inside_candle,
)
from src.data.structured_reader import StructuredReader
from src.data.tv_bridge import TVBridge, internal_to_tv_symbol
from src.execution.errors import (
    AlgoOrderError,
    InsufficientMargin,
    LeverageSetError,
    OrderRejected,
)
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.execution.bybit_client import BybitClient
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.execution.position_monitor import PendingEvent, PositionMonitor
from src.journal.database import TradeJournal
from src.journal.derivatives_journal import DerivativesJournal
from src.strategy.entry_signals import (
    _flow_alignment_score,
    build_trade_plan_with_reason,
    evaluate_pending_invalidation_gates,
    in_vwap_reset_blackout,
)
from src.strategy.risk_manager import RiskManager, TradeResult
from src.strategy.setup_planner import (
    ZoneSetup,
    apply_zone_to_plan,
    build_zone_setup,
)
from src.strategy.trade_plan import TradePlan


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _dump_per_venue_dict(d: dict[str, Optional[float]]) -> Optional[str]:
    """Serialise a per-venue netflow dict to a JSON TEXT column value.

    Empty dict → None so the journal column stays NULL until the
    background fetcher actually populates a venue. None values inside the
    dict are preserved (Pass 3 GBT can distinguish "fetch failed" from
    "no flow") via JSON null.
    """
    if not d:
        return None
    return json.dumps(d)


def _timeframe_key(tf: str) -> str:
    """Normalize TV timeframe strings to MultiTFBuffer keys.

    '15m' → '15', '4H' → '240', '1H' → '60'. Defaults to the raw string
    for MultiTFBuffer to handle (buffers are created on first refresh).
    """
    raw = tf.strip()
    if raw.endswith(("m", "M")):
        return raw[:-1]
    if raw.endswith(("h", "H")):
        try:
            return str(int(raw[:-1]) * 60)
        except ValueError:
            return raw
    return raw


def _timeframe_to_minutes(tf: str) -> int:
    """Normalize TV timeframe strings to integer minutes.

    '3m' → 3, '15m' → 15, '1H' → 60, '4H' → 240. Returns 3 (entry TF
    default) on any parse failure — keeps `_derive_enrichment`'s
    price_change computation working even if the YAML has an exotic
    format we don't recognise.
    """
    raw = tf.strip()
    try:
        if raw.endswith(("m", "M")):
            return int(raw[:-1])
        if raw.endswith(("h", "H")):
            return int(raw[:-1]) * 60
        return int(raw)
    except (TypeError, ValueError):
        return 3


def _zone_fill_latency_bars(
    *,
    placed_at: datetime,
    fill_at: datetime,
    entry_tf_minutes: int,
    max_wait_bars: int,
) -> int:
    """Bars between pending placement and fill, clamped to [0, max_wait_bars].

    Computed from wall-clock minutes / entry-TF minutes — a bar-aligned
    counter would need timeline state per pending which the runner does
    not track. Result rounds to nearest int. Bounded above because the
    pending-cancel timer fires AT max_wait_bars, so a fill cannot be
    later by construction; clamp guards against clock skew or
    timeframe-config mismatch.
    """
    if entry_tf_minutes <= 0:
        return 0
    delta_s = max(0.0, (fill_at - placed_at).total_seconds())
    bars = int(round(delta_s / 60.0 / entry_tf_minutes))
    return max(0, min(bars, max_wait_bars))


def _infer_close_reason(pnl_usdt: Optional[float]) -> Optional[str]:
    """Approximate close reason from realized PnL when no explicit reason
    was set by a defensive-close path. Bot's natural close paths are
    SL hit (negative PnL) and TP hit (positive PnL); breakeven is rare
    but possible (SL pulled to BE then triggered). Returns None when
    pnl_usdt is None so `record_close`'s COALESCE preserves any
    pre-existing close_reason on the row.
    """
    if pnl_usdt is None:
        return None
    if pnl_usdt > 0:
        return "tp_hit"
    if pnl_usdt < 0:
        return "sl_hit"
    return "breakeven"


def _tf_seconds(tf: str) -> int:
    """Convert a TV timeframe string to seconds (e.g. '3m' → 180, '4H' → 14400)."""
    raw = tf.strip()
    if not raw:
        return 60
    suffix = raw[-1]
    try:
        val = int(raw[:-1])
    except ValueError:
        return 60
    if suffix in ("m", "M"):
        return val * 60
    if suffix in ("h", "H"):
        return val * 3600
    if suffix in ("d", "D"):
        return val * 86400
    return 60


def _direction_to_pos_side(direction: Direction) -> str:
    return "long" if direction == Direction.BULLISH else "short"


def _runner_size(num_contracts: int, cfg) -> int:
    """Size of the runner (TP2) OCO leg, mirroring router._place_algos().

    Partial mode splits ``num_contracts`` into ``size1 = floor(N × ratio)``
    and ``size2 = N - size1``. The runner OCO is the ``size2`` leg. In
    non-partial mode the single OCO covers the full ``num_contracts``.
    Used by the dynamic-TP gate so cancel+place revisions cover the right
    slice of the position.
    """
    if not getattr(cfg.execution, "partial_tp_enabled", False):
        return int(num_contracts)
    size1 = int(num_contracts * cfg.execution.partial_tp_ratio)
    return max(1, num_contracts - size1)


def _bias_str(state: MarketState) -> Optional[str]:
    try:
        return state.signal_table.trend_htf.value if state.signal_table else None
    except AttributeError:
        return None


def _session_str(state: MarketState) -> Optional[str]:
    try:
        sess = state.signal_table.session if state.signal_table else None
        return sess.value if isinstance(sess, Session) else sess
    except AttributeError:
        return None


def _structure_str(state: MarketState) -> Optional[str]:
    try:
        return state.signal_table.structure if state.signal_table else None
    except AttributeError:
        return None


# ── Cross-asset pillar bias (Phase 7.A6) ────────────────────────────────────
#
# BTC and ETH move the rest of the crypto book; an altcoin entry that
# opposes BOTH pillars is fighting the market-wide tape. We snapshot the
# per-pillar EMA stack each cycle and consult it before altcoin entries.
#
# Veto rule (both pillars must concur against the trade):
#   * BULLISH alt blocked only when BTC and ETH are both BEARISH stacks.
#   * BEARISH alt blocked only when BTC and ETH are both BULLISH stacks.
# Single-pillar dissent, missing data, neutral stacks, or stale snapshots
# → fail-open (no veto). The veto is strict by design: alts diverging
# *with* one pillar is a normal regime and should pass.

_PILLAR_SYMBOLS: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
_PILLAR_BIAS_MAX_AGE_S: float = 300.0      # 5 min — roughly one full cycle


def _ema_pillar(values: list[float], period: int) -> Optional[float]:
    """EMA over `values`; returns None if the series is shorter than period."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _pillar_bias_from(
    state: MarketState,
    candles: list,
    fast_period: int,
    slow_period: int,
) -> Direction:
    """EMA-stack bias for BTC/ETH snapshot. UNDEFINED on neutral / missing."""
    price = float(getattr(state, "current_price", 0.0) or 0.0)
    if price <= 0 or not candles:
        return Direction.UNDEFINED
    closes = [c.close for c in candles if getattr(c, "close", None) is not None]
    ema_fast = _ema_pillar(closes, fast_period)
    ema_slow = _ema_pillar(closes, slow_period)
    if ema_fast is None or ema_slow is None:
        return Direction.UNDEFINED
    if price > ema_fast > ema_slow:
        return Direction.BULLISH
    if price < ema_fast < ema_slow:
        return Direction.BEARISH
    return Direction.UNDEFINED


def _cross_asset_opposition(
    pillar_bias: dict[str, tuple[Direction, datetime]],
    direction: Direction,
    now: datetime,
    max_age_s: float = _PILLAR_BIAS_MAX_AGE_S,
) -> bool:
    """True when BOTH pillars are fresh and oppose the trade direction."""
    if direction == Direction.UNDEFINED:
        return False
    fresh_opposing: list[Direction] = []
    for sym in _PILLAR_SYMBOLS:
        item = pillar_bias.get(sym)
        if item is None:
            return False      # missing pillar → fail open
        bias, updated = item
        if bias == Direction.UNDEFINED:
            return False      # neutral pillar → fail open
        if (now - updated).total_seconds() > max_age_s:
            return False      # stale → fail open
        fresh_opposing.append(bias)
    if direction == Direction.BULLISH:
        return all(b == Direction.BEARISH for b in fresh_opposing)
    if direction == Direction.BEARISH:
        return all(b == Direction.BULLISH for b in fresh_opposing)
    return False


def _price_change_pct(
    candles: Optional[list],
    bars_ago: int,
) -> Optional[float]:
    """Percent price change from N bars ago to the latest close.

    Positive = price up since then; negative = down. Used to give Pass 3
    GBT the OI × price combinatorial features (long pile-in vs short
    covering vs capitulation patterns). Returns None when the buffer is
    too short or any candle's close is <= 0 — defensive, never raises.

    `bars_ago` is expressed on the entry-TF cadence — caller converts
    `hours × 60 / entry_tf_minutes` before passing in.
    """
    if not candles:
        return None
    if len(candles) <= bars_ago:
        return None
    try:
        past_close = float(candles[-1 - bars_ago].close)
        latest_close = float(candles[-1].close)
    except (AttributeError, ValueError, TypeError):
        return None
    if past_close <= 0.0 or latest_close <= 0.0:
        return None
    return (latest_close - past_close) / past_close * 100.0


def _top_n_heatmap_clusters(
    heatmap: Any,
    current_price: float,
    atr: float,
    top_n: int = 5,
) -> dict:
    """Extract top-N above + top-N below clusters for journaling.

    Arkham-independent — reads from `LiquidityHeatmap.clusters_above/below`
    which the heatmap builder already sorts by notional (proximity-ranked,
    see `build_heatmap`). Returns
      `{"above": [{price, notional_usd, distance_atr}, ...],
        "below": [...]}`
    Top-N defaults to 5 per side (rich enough for magnet modelling
    without bloating journal rows). Empty dict when heatmap is None or
    neither side has clusters. `distance_atr` is signed toward-price:
    positive values for above, positive for below (absolute distance in
    ATR units — sign is implicit in the side key).
    """
    if heatmap is None:
        return {}
    above = list(getattr(heatmap, "clusters_above", None) or [])
    below = list(getattr(heatmap, "clusters_below", None) or [])
    if not above and not below:
        return {}

    def _encode(cluster, toward_above: bool) -> dict:
        price = float(getattr(cluster, "price", 0.0) or 0.0)
        dist_atr: Optional[float] = None
        if atr > 0 and current_price > 0 and price > 0:
            dist_atr = (price - current_price) / atr if toward_above else (current_price - price) / atr
        return {
            "price": price,
            "notional_usd": float(getattr(cluster, "notional_usd", 0.0) or 0.0),
            "distance_atr": dist_atr,
        }

    out: dict[str, list] = {}
    if above:
        out["above"] = [_encode(c, True) for c in above[:top_n]]
    if below:
        out["below"] = [_encode(c, False) for c in below[:top_n]]
    return out


def _derive_enrichment(
    state: MarketState,
    candles: Optional[list] = None,
    entry_tf_minutes: int = 3,
) -> dict:
    """Pull derivatives + heatmap snapshot fields out of MarketState for
    journal persistence. All keys are None when a source is missing — the
    journal's ALTER TABLE columns default to NULL so that's safe.

    2026-04-23 extension: pulls every `DerivativesState` field that feeds
    Pass 3 continuous-feature GBT (absolute OI, 1h OI change, absolute
    funding + predicted, per-side 1h liquidation notionals, LS
    z-score-14d) plus price-change windows derived from the entry-TF
    candle buffer (1h / 4h via `_price_change_pct`) plus top-N heatmap
    clusters via `_top_n_heatmap_clusters`. `candles` and
    `entry_tf_minutes` default to None/3 to preserve backward-compat
    with callers (tests, rehydrate path) that don't thread the buffer.
    """
    out: dict = {
        "regime_at_entry": None,
        "funding_z_at_entry": None,
        "ls_ratio_at_entry": None,
        "oi_change_24h_at_entry": None,
        "liq_imbalance_1h_at_entry": None,
        "nearest_liq_cluster_above_price": None,
        "nearest_liq_cluster_below_price": None,
        "nearest_liq_cluster_above_notional": None,
        "nearest_liq_cluster_below_notional": None,
        "nearest_liq_cluster_above_distance_atr": None,
        "nearest_liq_cluster_below_distance_atr": None,
        # 2026-04-23 extension ↓
        "open_interest_usd_at_entry": None,
        "oi_change_1h_pct_at_entry": None,
        "funding_rate_current_at_entry": None,
        "funding_rate_predicted_at_entry": None,
        "long_liq_notional_1h_at_entry": None,
        "short_liq_notional_1h_at_entry": None,
        "ls_ratio_zscore_14d_at_entry": None,
        "price_change_1h_pct_at_entry": None,
        "price_change_4h_pct_at_entry": None,
        "liq_heatmap_top_clusters": {},
    }
    deriv = getattr(state, "derivatives", None)
    if deriv is not None:
        out["regime_at_entry"] = getattr(deriv, "regime", None)
        out["funding_z_at_entry"] = getattr(deriv, "funding_rate_zscore_30d", None)
        out["ls_ratio_at_entry"] = getattr(deriv, "long_short_ratio", None)
        out["oi_change_24h_at_entry"] = getattr(deriv, "oi_change_24h_pct", None)
        out["liq_imbalance_1h_at_entry"] = getattr(deriv, "liq_imbalance_1h", None)
        # 2026-04-23 extension: additional DerivativesState fields that
        # previously lived only in runtime.
        out["open_interest_usd_at_entry"] = getattr(deriv, "open_interest_usd", None)
        out["oi_change_1h_pct_at_entry"] = getattr(deriv, "oi_change_1h_pct", None)
        out["funding_rate_current_at_entry"] = getattr(deriv, "funding_rate_current", None)
        out["funding_rate_predicted_at_entry"] = getattr(deriv, "funding_rate_predicted", None)
        out["long_liq_notional_1h_at_entry"] = getattr(deriv, "long_liq_notional_1h", None)
        out["short_liq_notional_1h_at_entry"] = getattr(deriv, "short_liq_notional_1h", None)
        out["ls_ratio_zscore_14d_at_entry"] = getattr(deriv, "ls_ratio_zscore_14d", None)
    hm = getattr(state, "liquidity_heatmap", None)
    price = float(getattr(state, "current_price", 0.0) or 0.0)
    atr = float(getattr(state, "atr", 0.0) or 0.0)
    if hm is not None:
        na = getattr(hm, "nearest_above", None)
        nb = getattr(hm, "nearest_below", None)
        if na is not None:
            out["nearest_liq_cluster_above_price"] = getattr(na, "price", None)
            out["nearest_liq_cluster_above_notional"] = getattr(na, "notional_usd", None)
            if atr > 0 and price > 0 and getattr(na, "price", None):
                out["nearest_liq_cluster_above_distance_atr"] = (na.price - price) / atr
        if nb is not None:
            out["nearest_liq_cluster_below_price"] = getattr(nb, "price", None)
            out["nearest_liq_cluster_below_notional"] = getattr(nb, "notional_usd", None)
            if atr > 0 and price > 0 and getattr(nb, "price", None):
                out["nearest_liq_cluster_below_distance_atr"] = (price - nb.price) / atr
        # 2026-04-23 extension: top-N clusters for richer magnet modelling.
        out["liq_heatmap_top_clusters"] = _top_n_heatmap_clusters(
            hm, current_price=price, atr=atr, top_n=5,
        )
    # 2026-04-23 extension: price change over 1h / 4h windows from the
    # entry-TF candle buffer. 1h = 60/entry_tf_minutes bars back; 4h =
    # 240/entry_tf_minutes. On 3m TF → 20 / 80 bars; buffer's default
    # size of 100 covers 4h comfortably.
    if candles and entry_tf_minutes > 0:
        bars_1h = max(1, int(60 / entry_tf_minutes))
        bars_4h = max(1, int(240 / entry_tf_minutes))
        out["price_change_1h_pct_at_entry"] = _price_change_pct(candles, bars_1h)
        out["price_change_4h_pct_at_entry"] = _price_change_pct(candles, bars_4h)
    return out


# ── Context ─────────────────────────────────────────────────────────────────


@dataclass
class LastCloseInfo:
    """Snapshot of the most recent close for (symbol, side) — reentry gate."""
    price: float
    time: datetime
    confluence: int
    outcome: str            # "WIN" | "LOSS" | "BREAKEVEN"


@dataclass
class PendingSetupMeta:
    """Phase 7.C4 — state a limit-entry pending needs at fill time.

    The PositionMonitor only tracks order_id → state; the runner stashes
    the plan (for OCO placement + journal record_open) and the MarketState
    snapshot (for journal enrichment) here so the FILLED event path can
    reconstruct everything without re-reading.

    `trend_regime_at_entry` (Phase 7.D3) is the ADX regime classification
    captured at placement time. Persisted to the journal on fill so regime
    at *decision* is recorded, not at fill — the tape can shift between
    limit placement and a fill minutes later.
    """
    plan: TradePlan
    zone: ZoneSetup
    order_id: str
    signal_state: MarketState
    placed_at: datetime
    trend_regime_at_entry: Optional[str] = None
    # 2026-04-22 (gece, late) — per-TF oscillator numerics captured at
    # PLACEMENT TIME. Carried through to fill so the journal row reflects
    # the decision moment (not the later fill moment; charts may have
    # drifted by the time the limit hits). Empty dict when upstream
    # caches were unavailable at placement (bridge=None, LTF timeout,
    # already-open HTF skip).
    oscillator_raw_values_at_placement: dict[str, dict] = field(default_factory=dict)


@dataclass
class BotContext:
    """Everything the runner needs, wired together.

    Tests pass fakes; production builds via `BotRunner.from_config`.
    Duck-typed so fakes don't have to inherit from the real classes.
    """
    reader: Any                # `.read_market_state() -> MarketState` (async)
    multi_tf: Any              # `.refresh(tf, count=)` / `.get_buffer(tf)`
    journal: TradeJournal
    router: Any                # `.place(plan, inst_id=None) -> ExecutionReport` (sync)
    monitor: Any               # `.register_open`, `.poll` (sync)
    risk_mgr: RiskManager
    bybit_client: Any          # `.enrich_close_fill`, `.get_positions`
    config: BotConfig
    bridge: Any = None         # `.set_symbol`, `.set_timeframe` (async) — optional in tests
    ltf_reader: Any = None     # LTFReader — optional (fakes skip it)
    open_trade_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    # HTF S/R zones cached per-symbol after the HTF pass (Madde B → D)
    htf_sr_cache: dict[str, list] = field(default_factory=dict)
    # Full HTF MarketState (Pine tables on 15m) cached per-symbol — Phase 7.B4.
    # Populated alongside htf_sr_cache while the chart is on the HTF timeframe,
    # so the zone-entry planner (Phase 7.C1) can source HTF FVGs / OBs / trend
    # without another TF switch. Cleared on already-open skip or refresh error.
    htf_state_cache: dict[str, MarketState] = field(default_factory=dict)
    # Latest LTF snapshot per-symbol (Madde B → F)
    ltf_cache: dict[str, LTFState] = field(default_factory=dict)
    # Last close per (symbol, side) — reentry gate (Madde C)
    last_close: dict[tuple[str, str], LastCloseInfo] = field(default_factory=dict)
    # Phase 7.C4 — pending limit-entry metadata keyed by (symbol, pos_side).
    # Populated when `place_limit_entry` succeeds, cleared on FILLED
    # (after OCO attach) or CANCELED (timeout/invalidation). Runner uses
    # the stashed plan to attach OCO algos once the fill event arrives.
    pending_setups: dict[tuple[str, str], PendingSetupMeta] = field(default_factory=dict)
    # Madde F — LTF reversal defensive close bookkeeping
    defensive_close_in_flight: set = field(default_factory=set)
    pending_close_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    open_trade_opened_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    # Phase 1.5 — derivatives subsystem (all opt-in via DerivativesConfig.enabled)
    liquidation_stream: Any = None         # LiquidationStream
    derivatives_cache: Any = None          # DerivativesCache (Madde 3)
    coinalyze_client: Any = None           # CoinalyzeClient (Madde 2)
    # Macro event blackout — opt-in via EconomicCalendarConfig.enabled
    economic_calendar: Any = None          # EconomicCalendarService
    # Per-symbol ctVal (underlying per contract, internal canonical convention).
    # BTC=0.01, ETH=0.1, SOL=1, DOGE=1000. Populated at bootstrap; one hardcoded
    # value for all symbols trips Bybit insufficient-margin (110004).
    contract_sizes: dict[str, float] = field(default_factory=dict)
    # Per-symbol Bybit max leverage (BTC/ETH=100, SOL=50). Above this trips 110086.
    max_leverage_per_symbol: dict[str, int] = field(default_factory=dict)
    # Phase 7.A6 — cross-asset pillar bias snapshot. Updated each cycle from
    # BTC-USDT-SWAP and ETH-USDT-SWAP EMA stacks; consulted before altcoin
    # entries so trades against both pillars can be rejected.
    # Format: {pillar_symbol: (direction, updated_at_utc)}.
    pillar_bias: dict[str, tuple[Direction, datetime]] = field(default_factory=dict)
    # Main event loop captured at `run()` start — threaded callbacks (from
    # `PositionMonitor.poll` running under `asyncio.to_thread`) schedule
    # coroutines on this loop via `run_coroutine_threadsafe`.
    main_loop: Any = None
    # Katman 2 — Binance public futures client for the demo-wick artefact
    # cross-check (set by from_config). Optional in tests.
    binance_public: Any = None
    # 2026-04-21 — Arkham on-chain subsystem (Phase B).
    # Instantiated in `from_config` when `on_chain.enabled=true`. None
    # keeps the scheduler inert and every snapshot field None.
    arkham_client: Any = None
    # Cached snapshots refreshed on UTC-day boundary (daily) + refresh
    # cadence (stablecoin pulse). The runner attaches these to each
    # MarketState before the per-symbol cycle so gates / modifiers see
    # the same snapshot across all symbols in one tick.
    on_chain_snapshot: Any = None                     # OnChainSnapshot
    stablecoin_pulse_1h_usd: Optional[float] = None
    # WhaleBlackoutState — the in-memory registry the Phase D WS listener
    # writes to and entry_signals reads from. Stays as a default (empty)
    # instance so the gate can unconditionally check `.is_active()`
    # without None-guarding every call site.
    whale_blackout_state: Any = None
    # Scheduler bookkeeping. Monotonic timestamps for the daily-bundle and
    # hourly-pulse fetches. 0.0 means "never fetched".
    # 2026-04-23 — daily bundle flipped from UTC-date gate to monotonic
    # cadence so DB rows refresh intraday (see daily_snapshot_refresh_s).
    last_on_chain_daily_ts: float = 0.0
    last_on_chain_pulse_ts: float = 0.0
    # Phase F2 — Arkham altcoin index scalar + last-fetch monotonic ts.
    altcoin_index_value: Optional[int] = None
    last_altcoin_index_ts: float = 0.0
    # 2026-04-21 — Arkham whale-transfer WS listener (Phase D). Only
    # instantiated + started when `on_chain.enabled AND
    # whale_blackout_enabled`. Writes to `whale_blackout_state`; the
    # entry_signals gate reads from that registry via MarketState.
    arkham_ws: Any = None                             # ArkhamWebSocketListener
    # 2026-04-21 (eve, late) — on_chain_snapshots time-series dedup key.
    # Tuple of (bias, pulse, btc_flow, eth_flow, coinbase_skew, bnb_flow,
    # altcoin_idx, fresh, whale_blackout_active). Unchanged tick → skip
    # journal write; mutation → append a row. None on startup.
    # 2026-04-22 — fingerprint extended with Coinbase/Binance/Bybit
    # netflow + per-symbol token volume JSON.
    last_on_chain_snapshot_fingerprint: Any = None
    # 2026-04-22 — per-entity 24h netflow (last completed UTC day) for
    # Coinbase, Binance, Bybit via `/flow/entity/{entity}`. Refreshed
    # in the daily-snapshot branch (once per UTC day, like bias itself).
    # Journal-only; no gate / modifier reads these.
    cex_coinbase_netflow_24h_usd: Optional[float] = None
    cex_binance_netflow_24h_usd: Optional[float] = None
    cex_bybit_netflow_24h_usd: Optional[float] = None
    # 2026-04-23 (night-late) — 4th + 5th venues: Bitfinex + Kraken.
    # Biggest named inflow / outflow in live probe vs. `type:cex`
    # aggregate; named coverage (CB+BN+BY) alone captured only
    # ~1-6% of the full CEX BTC netflow signal. Journal-only;
    # _flow_alignment_score still reads the original 6 inputs.
    cex_bitfinex_netflow_24h_usd: Optional[float] = None
    cex_kraken_netflow_24h_usd: Optional[float] = None
    # 2026-04-24 — 6th venue: OKX. Bot trades here so its own netflow is a
    # natural self-signal. 24h net ≈ 0 structurally (turnover $1.86B but
    # balanced in/out, −0.12% bias); $58M max hourly |net|. Journal-only;
    # Pass 3 decides whether to add a short-window OKX slot separately.
    cex_okx_netflow_24h_usd: Optional[float] = None
    # 2026-04-22 — per-symbol most-recent-hour net CEX flow via
    # `/token/volume/{id}?granularity=1h`. JSON-encoded dict of
    # {OKX_symbol: usd_netflow_float}. Refreshed on its own cadence
    # (token_volume_refresh_s, default 3600). Journal-only.
    token_volume_1h_net_usd_json: Optional[str] = None
    last_token_volume_ts: float = 0.0
    # 2026-04-26 — per-venue × per-asset 24h netflow (BTC / ETH / stables).
    # Each is a dict keyed by entity slug ("coinbase"/"binance"/"bybit"/
    # "bitfinex"/"kraken"/"okx") → signed USD float (in - out). Refreshed
    # in a fire-and-forget background task off the daily-bundle cycle so
    # the trade cycle never waits on the 36 histogram calls this requires.
    # Serialised to JSON dict TEXT columns on the snapshot row. Journal-only.
    cex_per_venue_btc_netflow_24h_usd: dict[str, Optional[float]] = field(default_factory=dict)
    cex_per_venue_eth_netflow_24h_usd: dict[str, Optional[float]] = field(default_factory=dict)
    cex_per_venue_stables_netflow_24h_usd: dict[str, Optional[float]] = field(default_factory=dict)
    last_per_venue_per_asset_ts: float = 0.0
    per_venue_per_asset_task: Optional[asyncio.Task] = None
    # 2026-04-26 — per-symbol MarketState cache (entry-TF MarketState only).
    # Populated at the END of each per-symbol cycle so the intra-trade
    # position-snapshot writer can read oscillator + VWAP-band drift outside
    # the per-symbol cycle. Stale on first cycle for each symbol post-restart;
    # the writer treats absence as None and stamps NULL on the row.
    last_market_state_per_symbol: dict[str, MarketState] = field(default_factory=dict)
    # Monotonic ts of last position-snapshot batch write. Cadence-gated by
    # `journal.position_snapshot_cadence_s` (default 300s). 0.0 = never.
    last_position_snapshot_ts: float = 0.0


# ── Runner ──────────────────────────────────────────────────────────────────


class _DryRunRouter:
    """Stand-in router for --dry-run: mirrors OrderRouter surface (`place`,
    `place_limit_entry`, `attach_algos`) without touching the exchange. Keeps the
    zone-entry path runnable in --dry-run --once smoke tests."""

    def __init__(self, config: RouterConfig):
        self.config = config

    def place(self, plan, inst_id: Optional[str] = None):
        cfg = self.config
        if inst_id and inst_id != cfg.inst_id:
            cfg = RouterConfig(
                inst_id=inst_id,
                margin_mode=self.config.margin_mode,
                close_on_algo_failure=self.config.close_on_algo_failure,
            )
        return dry_run_report(plan, cfg)

    def place_limit_entry(
        self, plan, entry_px: float, inst_id: Optional[str] = None,
        ord_type: str = "post_only", fallback_to_limit: bool = True,
    ):
        return OrderResult(
            order_id="DRYRUN-LIMIT",
            client_order_id="DRYRUN-LIMIT",
            status=OrderStatus.PENDING,
            filled_sz=0.0,
            avg_price=entry_px,
        )

    def attach_algos(self, plan, inst_id: Optional[str] = None):
        return [AlgoResult(
            algo_id="DRYRUN-ALGO",
            client_algo_id="DRYRUN-ALGO",
            sl_trigger_px=plan.sl_price,
            tp_trigger_px=plan.tp_price,
        )]


class BotRunner:
    def __init__(
        self,
        ctx: BotContext,
        shutdown: Optional[asyncio.Event] = None,
        stop_after_closed_trades: Optional[int] = None,
        derivatives_only: bool = False,
        duration_seconds: Optional[int] = None,
        clear_halt: bool = False,
    ):
        self.ctx = ctx
        self.shutdown = shutdown or asyncio.Event()
        self.stop_after_closed_trades = stop_after_closed_trades
        # Phase 1.5 — data-collection modes.
        self.derivatives_only = derivatives_only
        self.duration_seconds = duration_seconds
        # Operator override: after _prime() replays the journal, also wipe any
        # halt state + daily counters that would block the very first tick.
        self.clear_halt = clear_halt

    # ── Construction ────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        cfg: BotConfig,
        *,
        dry_run: bool = False,
        stop_after_closed_trades: Optional[int] = None,
        derivatives_only: bool = False,
        duration_seconds: Optional[int] = None,
        clear_halt: bool = False,
    ) -> "BotRunner":
        bridge = TVBridge()
        reader = StructuredReader(bridge)
        ltf_reader = LTFReader(bridge, reader)
        multi_tf = MultiTFBuffer(bridge, max_size=cfg.analysis.candle_buffer_size)
        client = BybitClient(cfg.to_bybit_credentials())
        router_cfg = RouterConfig(
            inst_id=cfg.primary_symbol(),
            margin_mode=cfg.execution.margin_mode,
            partial_tp_enabled=cfg.execution.partial_tp_enabled,
            partial_tp_ratio=cfg.execution.partial_tp_ratio,
            partial_tp_rr=cfg.execution.partial_tp_rr,
            move_sl_to_be_after_tp1=cfg.execution.move_sl_to_be_after_tp1,
            algo_trigger_px_type=cfg.execution.algo_trigger_px_type,
        )
        router = _DryRunRouter(router_cfg) if dry_run else OrderRouter(client, router_cfg)
        journal = TradeJournal(cfg.journal.db_path)

        # The monitor needs a way to update algo_ids in the journal when
        # it moves SL to BE — inject a callback that uses open_trade_ids
        # on the context to find the matching trade row. `loop` is stashed
        # by `run()` at startup so threaded callbacks (from monitor.poll in
        # a worker thread) can schedule coroutines on the main loop.
        ctx_holder: dict[str, Any] = {}

        def _on_sl_moved(inst_id: str, pos_side: str, new_algo_ids: list[str]) -> None:
            # Called from `PositionMonitor.poll()` running in a worker thread
            # via `asyncio.to_thread`, so the thread has no running loop.
            # Schedule the DB write on the main loop via run_coroutine_threadsafe.
            c = ctx_holder.get("ctx")
            if c is None:
                return
            trade_id = c.open_trade_ids.get((inst_id, pos_side))
            if trade_id is None:
                return
            import asyncio as _asyncio
            coro = c.journal.update_algo_ids(trade_id, new_algo_ids)
            loop = getattr(c, "main_loop", None)
            if loop is not None:
                _asyncio.run_coroutine_threadsafe(coro, loop)
            else:
                try:
                    _asyncio.create_task(coro)
                except RuntimeError:
                    coro.close()

        monitor = PositionMonitor(
            client,
            margin_mode=router_cfg.margin_mode,
            move_sl_to_be_enabled=cfg.execution.move_sl_to_be_after_tp1,
            sl_be_offset_pct=cfg.execution.sl_be_offset_pct,
            on_sl_moved=_on_sl_moved,
            algo_trigger_px_type=cfg.execution.algo_trigger_px_type,
        )
        risk_mgr = RiskManager(cfg.bot.starting_balance, cfg.breakers())
        # Katman 2 — Binance public client for demo-wick artefact cross-check.
        # Opt-in via execution.artefact_check_enabled; disabled keeps ctx
        # field None so the cross-check code path short-circuits.
        binance_public = (
            BinancePublicClient(
                timeout_s=cfg.execution.artefact_check_timeout_s,
            )
            if cfg.execution.artefact_check_enabled else None
        )
        ctx = BotContext(
            reader=reader, multi_tf=multi_tf, journal=journal,
            router=router, monitor=monitor, risk_mgr=risk_mgr,
            bybit_client=client, config=cfg, bridge=bridge,
            ltf_reader=ltf_reader,
            binance_public=binance_public,
        )
        ctx_holder["ctx"] = ctx

        # Phase 1.5 — derivatives subsystem. Instances are created here so
        # shutdown cascade is deterministic; the actual WS task + cache
        # refresh loop are started from `BotRunner.run()`.
        if cfg.derivatives.enabled:
            deriv_journal = DerivativesJournal(cfg.journal.db_path)
            liq_stream = LiquidationStream(
                watched_symbols=list(cfg.trading.symbols),
                buffer_size_per_symbol=cfg.derivatives.liquidation_buffer_size,
            )
            liq_stream.attach_journal(deriv_journal)
            coinalyze = CoinalyzeClient(
                timeout_s=cfg.derivatives.coinalyze_timeout_s,
                max_retries=cfg.derivatives.coinalyze_max_retries,
            )
            cache = DerivativesCache(
                watched=list(cfg.trading.symbols),
                liq_stream=liq_stream,
                coinalyze=coinalyze,
                journal=deriv_journal,
                refresh_interval_s=cfg.derivatives.coinalyze_refresh_interval_s,
                regime_thresholds=cfg.derivatives.regime_thresholds,
                regime_per_symbol_overrides=cfg.derivatives.regime_per_symbol_overrides,
            )
            ctx.liquidation_stream = liq_stream
            ctx.coinalyze_client = coinalyze
            ctx.derivatives_cache = cache
            # Stash the journal on the cache for the start-sequence; the
            # runner calls `ensure_schema` through `_start_derivatives`.
            cache._deriv_journal_bootstrap = deriv_journal

        # 2026-04-21 — Arkham on-chain subsystem (Phase B).
        # Master `on_chain.enabled=false` keeps `arkham_client=None` and
        # the runner's `_refresh_on_chain_snapshots` short-circuits. When
        # true, the client reads ARKHAM_API_KEY from env at construction;
        # missing key → ArkhamClient warns and every fetch returns None
        # (fail-open, identical shape to a disabled-master tick).
        # `whale_blackout_state` stays allocated even when the Phase D WS
        # listener isn't wired so `entry_signals` can unconditionally
        # query `.is_active()` without per-call None guards.
        ctx.whale_blackout_state = WhaleBlackoutState()
        if cfg.on_chain.enabled:
            ctx.arkham_client = ArkhamClient(
                timeout_s=cfg.on_chain.api_client_timeout_s,
                auto_disable_pct=cfg.on_chain.api_usage_auto_disable_pct,
            )
            # Phase D — whale WS listener. Only spun up when the sub-
            # feature flag is on; otherwise the gate inside
            # entry_signals never fires even with an allocated
            # WhaleBlackoutState.
            if cfg.on_chain.whale_blackout_enabled:
                ctx.arkham_ws = ArkhamWebSocketListener(
                    ctx.arkham_client,
                    ctx.whale_blackout_state,
                    usd_gte=cfg.on_chain.whale_threshold_usd,
                    blackout_duration_s=cfg.on_chain.whale_blackout_duration_s,
                    tokens=list(cfg.on_chain.whale_tokens),
                )

        # Macro event blackout — independent of the derivatives subsystem.
        if cfg.economic_calendar.enabled:
            finnhub = (
                FinnhubClient(
                    api_key=cfg.economic_calendar.finnhub_api_key,
                    timeout_s=cfg.economic_calendar.finnhub_timeout_s,
                    max_retries=cfg.economic_calendar.finnhub_max_retries,
                )
                if cfg.economic_calendar.finnhub_enabled else None
            )
            faireconomy = (
                FairEconomyClient(
                    timeout_s=cfg.economic_calendar.faireconomy_timeout_s,
                    max_retries=cfg.economic_calendar.faireconomy_max_retries,
                )
                if cfg.economic_calendar.faireconomy_enabled else None
            )
            ctx.economic_calendar = EconomicCalendarService(
                config=cfg.economic_calendar,
                finnhub=finnhub,
                faireconomy=faireconomy,
            )

        return cls(
            ctx,
            stop_after_closed_trades=stop_after_closed_trades,
            derivatives_only=derivatives_only,
            duration_seconds=duration_seconds,
            clear_halt=clear_halt,
        )

    # ── Entry points ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop. Installs signal handlers; exits when `self.shutdown` is set."""
        try:
            install_shutdown_handlers(self.shutdown)
        except Exception:
            logger.exception("signal_install_failed")

        # Capture the main loop so threaded callbacks (PositionMonitor.poll runs
        # under asyncio.to_thread) can schedule DB writes on the right loop.
        self.ctx.main_loop = asyncio.get_running_loop()

        try:
            async with self.ctx.journal:
                await self._prime()
                await self._start_derivatives()
                await self._start_economic_calendar()
                await self._start_on_chain_ws()
                interval = self.ctx.config.bot.poll_interval_seconds
                deadline = (
                    time.monotonic() + self.duration_seconds
                    if self.duration_seconds is not None else None
                )
                if self.derivatives_only:
                    logger.info("derivatives_only_mode_enabled — entry pipeline "
                                "bypassed; close-poll + cache-refresh only")
                if deadline is not None:
                    logger.info("duration_limit_active seconds={}",
                                self.duration_seconds)
                while not self.shutdown.is_set():
                    try:
                        await self.run_once()
                    except Exception:
                        logger.exception("cycle_failed")
                    if self.stop_after_closed_trades is not None:
                        closed = len(await self.ctx.journal.list_closed_trades())
                        if closed >= self.stop_after_closed_trades:
                            logger.info(
                                "stop_after_closed_trades_reached closed={} limit={}",
                                closed, self.stop_after_closed_trades,
                            )
                            self.shutdown.set()
                            break
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            logger.info("duration_limit_reached — stopping")
                            self.shutdown.set()
                            break
                        wait_s = min(interval, remaining)
                    else:
                        wait_s = interval
                    try:
                        await asyncio.wait_for(self.shutdown.wait(), timeout=wait_s)
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self._stop_economic_calendar()
            await self._stop_derivatives()
            await self._stop_on_chain()

    async def _start_derivatives(self) -> None:
        """Boot the Phase 1.5 derivatives tasks. Safe to call when disabled."""
        cache = self.ctx.derivatives_cache
        if cache is not None:
            deriv_journal = getattr(cache, "_deriv_journal_bootstrap", None)
            if deriv_journal is not None:
                try:
                    await deriv_journal.ensure_schema()
                except Exception:
                    logger.exception("derivatives_schema_failed")
        if self.ctx.liquidation_stream is not None:
            try:
                await self.ctx.liquidation_stream.start()
            except Exception:
                logger.exception("liquidation_stream_start_failed")
        if cache is not None:
            try:
                await cache.start()
            except Exception:
                logger.exception("derivatives_cache_start_failed")

    async def _stop_derivatives(self) -> None:
        """Cascade stop (cache → stream → client). Best-effort, never raises."""
        cache = self.ctx.derivatives_cache
        if cache is not None:
            try:
                await cache.stop()
            except Exception:
                logger.exception("derivatives_cache_stop_failed")
        stream = self.ctx.liquidation_stream
        if stream is not None:
            try:
                await stream.stop()
            except Exception:
                logger.exception("liquidation_stream_stop_failed")
        client = self.ctx.coinalyze_client
        if client is not None:
            try:
                close = getattr(client, "close", None)
                if close is not None:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logger.exception("coinalyze_client_close_failed")
        binance_public = self.ctx.binance_public
        if binance_public is not None:
            try:
                binance_public.close()
            except Exception:
                logger.exception("binance_public_close_failed")

    async def _start_economic_calendar(self) -> None:
        """Warm the cache + spawn the periodic refresh task. No-op when
        the service is disabled. Best-effort: failure here never blocks
        the trading loop (blackout just stays inactive)."""
        svc = self.ctx.economic_calendar
        if svc is None:
            return
        try:
            await svc.refresh()
        except Exception:
            logger.exception("economic_calendar_initial_refresh_failed")
        try:
            svc._refresh_task = asyncio.create_task(
                svc.run_refresh_loop(self.shutdown))
        except Exception:
            logger.exception("economic_calendar_refresh_task_spawn_failed")

    async def _stop_economic_calendar(self) -> None:
        svc = self.ctx.economic_calendar
        if svc is None:
            return
        try:
            await svc.close()
        except Exception:
            logger.exception("economic_calendar_close_failed")

    async def _start_on_chain_ws(self) -> None:
        """Boot the Phase D whale-transfer WS listener if configured.

        Safe to call when `arkham_ws is None` (master off or sub-feature
        off) — early-returns.

        2026-04-22 (gece): wires the `on_transfer` journal callback +
        `main_loop` so whale events flow into the `whale_transfers`
        table. The hard gate they used to feed is gone; data capture is
        the new primary purpose.
        """
        ws = self.ctx.arkham_ws
        if ws is None:
            return
        # Attach journal callback + loop reference before start so the
        # first received event already has somewhere to land.
        try:
            ws._on_transfer = self._on_whale_transfer_from_ws  # noqa: SLF001
            ws._main_loop = self.ctx.main_loop                  # noqa: SLF001
        except Exception:
            # Listener surface may drift over time; callback is optional —
            # log and continue rather than crashing startup.
            logger.exception("arkham_whale_ws_callback_wire_failed")
        try:
            await ws.start()
        except Exception:
            logger.exception("arkham_whale_ws_start_failed")

    async def _on_whale_transfer_from_ws(
        self,
        *,
        token: str,
        usd_value: float,
        ts_ms: int,
        affected_symbols: list[str],
        extras: dict,
    ) -> None:
        """Journal a single whale transfer event. Fire-and-forget from WS.

        Invoked from the Arkham WS listener's `_handle` (worker thread
        path) via `asyncio.run_coroutine_threadsafe` onto the main loop.
        Exceptions are swallowed so a slow/failed DB write never kills
        the WS reader.
        """
        try:
            captured_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            await self.ctx.journal.record_whale_transfer(
                captured_at=captured_at,
                token=token,
                usd_value=float(usd_value),
                from_entity=extras.get("from_entity"),
                to_entity=extras.get("to_entity"),
                tx_hash=extras.get("tx_hash"),
                affected_symbols=list(affected_symbols),
            )
        except Exception:
            logger.exception(
                "whale_transfer_journal_write_failed token={} usd={:.0f}",
                token, usd_value,
            )

    async def _stop_on_chain(self) -> None:
        """Release the Arkham HTTP client + WS listener on shutdown.
        Best-effort, never raises. No-op when nothing was wired."""
        ws = self.ctx.arkham_ws
        if ws is not None:
            try:
                await ws.stop()
            except Exception:
                logger.exception("arkham_whale_ws_stop_failed")
        client = self.ctx.arkham_client
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logger.exception("arkham_client_close_failed")

    async def run_once_then_exit(self) -> None:
        """Smoke-test entry point: one full tick, then clean shutdown."""
        async with self.ctx.journal:
            await self._prime()
            await self.run_once()

    # ── One tick ────────────────────────────────────────────────────────────

    async def run_once(self) -> None:
        # Drain closes once at the start — frees slots, updates risk manager.
        # Monitor polls all tracked (inst_id, pos_side) pairs regardless of
        # which symbol the chart currently shows, so this is symbol-agnostic.
        await self._process_closes()

        # Phase 7.C4 — drain pending limit-entry events next. Filled pendings
        # transition into OPEN (OCO attach + journal); canceled pendings clear
        # the pending_setups slot so the symbol can re-plan the next cycle.
        await self._process_pending()

        # 2026-04-21 — Arkham on-chain snapshot refresh (Phase B). Runs
        # BEFORE the per-symbol loop so every symbol in this tick sees the
        # same snapshot (keeps journal `on_chain_context` consistent across
        # a BTC trade and a parallel SOL reject on the same tick).
        # Inert when `on_chain.enabled=false` or client was never built.
        await self._refresh_on_chain_snapshots()

        # Data-collection mode: stream + cache still run in the background via
        # _start_derivatives; here we just skip the entry/exit pipeline. Close
        # poll above still fires so any positions already on the book resolve.
        if self.derivatives_only:
            return

        for symbol in self.ctx.config.trading.symbols:
            if self.shutdown.is_set():
                return
            # Drain pending events between symbols so a fill during this cycle
            # attaches its OCO within seconds rather than waiting for the next
            # run_once tick (~180-240s). Minimises the fill → attach race that
            # causes Bybit insufficient-margin / order-rejected (110012) on
            # tight-SL zone entries.
            try:
                await self._process_pending()
            except Exception:
                logger.exception("inline_pending_drain_failed symbol={}", symbol)
            try:
                await self._run_one_symbol(symbol)
            except Exception:
                logger.exception("symbol_cycle_failed symbol={}", symbol)
                continue

    def _check_reentry_gate(
        self,
        symbol: str,
        side: str,
        *,
        proposed_confluence: int,
        current_price: float,
        atr: float,
        now: datetime,
    ) -> tuple[bool, Optional[str]]:
        """Return (allowed, reason). Reason is populated only when blocked.

        Four sequential gates — first fail wins:
          1. Time: elapsed < min_bars_after_close * entry_tf_seconds.
          2. ATR move: |price - last_close.price| < min_atr_move * ATR.
          3. Quality after WIN: proposed_confluence <= last.confluence.
          4. Quality after LOSS: proposed_confluence < last.confluence.

        BREAKEVEN bypasses the quality gate (treated as neutral).
        """
        last = self.ctx.last_close.get((symbol, side))
        if last is None:
            return True, None

        cfg = self.ctx.config.reentry
        tf_sec = _tf_seconds(self.ctx.config.trading.entry_timeframe)

        elapsed = (now - last.time).total_seconds()
        if elapsed < cfg.min_bars_after_close * tf_sec:
            return False, f"cooldown_{cfg.min_bars_after_close}bars"

        if atr > 0 and last.price > 0:
            if abs(current_price - last.price) / atr < cfg.min_atr_move:
                return False, "atr_move_insufficient"

        if cfg.require_higher_confluence_after_win and last.outcome == "WIN":
            if proposed_confluence <= last.confluence:
                return False, "post_win_needs_higher_confluence"

        if cfg.require_higher_or_equal_confluence_after_loss and last.outcome == "LOSS":
            if proposed_confluence < last.confluence:
                return False, "post_loss_needs_ge_confluence"

        return True, None

    # ── LTF reversal defensive close (Madde F) ──────────────────────────────

    def _get_open_side(self, symbol: str) -> Optional[str]:
        """Return the pos_side of the open position on `symbol`, if any."""
        for sym, side in self.ctx.open_trade_ids:
            if sym == symbol:
                return side
        return None

    async def _maybe_invalidate_pending_for(
        self, symbol: str, state: MarketState, candles: list,
    ) -> None:
        """2026-04-22 — Pending limit early-cancel on hard-gate flip.

        For any pending limit waiting on `symbol`, re-run the same HARD veto
        gates that decide a NEW entry. If a gate would now reject (sharp
        market turn, whale event, momentum flip, VWAP cross during the
        21-min wait), cancel the pending so we don't fill at a no-longer-
        favorable level.

        Pure consistency fix — same gates that reject NEW entries now also
        invalidate WAITING entries. Confluence is NOT rescored (pullback
        strategy expects natural confluence fluctuation while waiting). On
        cancel, the journal records `pending_hard_gate_invalidated` with
        the specific gate name carried via the cancel reason.

        Failures here log + swallow so a hiccup never blocks the symbol's
        normal cycle (worst case: pending sits one extra cycle).
        """
        cfg = self.ctx.config
        # Find the pending for this symbol (at most one per side; usually one).
        meta_keys = [k for k in self.ctx.pending_setups if k[0] == symbol]
        if not meta_keys:
            return
        for key in meta_keys:
            symbol_, pos_side = key
            meta = self.ctx.pending_setups.get(key)
            if meta is None:
                continue
            try:
                gate_reason = evaluate_pending_invalidation_gates(
                    state=state,
                    candles=candles,
                    direction=meta.plan.direction,
                    entry_price=float(meta.plan.entry_price),
                    pillar_opposition=self._pillar_opposition_for(symbol_),
                    vwap_hard_veto_enabled=cfg.analysis.vwap_hard_veto_enabled,
                    ema_veto_enabled=cfg.analysis.ema_veto_enabled,
                    ema_veto_fast_period=cfg.analysis.ema_veto_fast_period,
                    ema_veto_slow_period=cfg.analysis.ema_veto_slow_period,
                    now=_utc_now(),
                    vwap_reset_blackout_enabled=cfg.analysis.vwap_reset_blackout_enabled,
                    vwap_reset_blackout_pre_minutes=cfg.analysis.vwap_reset_blackout_window_pre_min,
                    vwap_reset_blackout_post_minutes=cfg.analysis.vwap_reset_blackout_window_post_min,
                )
            except Exception:
                logger.exception(
                    "pending_gate_eval_failed symbol={} side={}",
                    symbol_, pos_side,
                )
                continue
            if gate_reason is None:
                continue
            # Hard gate flipped — cancel the pending. Pass gate_reason as
            # cancel reason; `_handle_pending_canceled` maps it to a
            # journal `pending_hard_gate_invalidated` reject reason.
            logger.info(
                "pending_hard_gate_invalidated symbol={} side={} order_id={} "
                "gate={} entry_px={}",
                symbol_, pos_side, meta.order_id, gate_reason,
                meta.plan.entry_price,
            )
            try:
                ev = await asyncio.to_thread(
                    self.ctx.monitor.cancel_pending,
                    symbol_, pos_side,
                    reason=f"hard_gate:{gate_reason}",
                )
            except Exception:
                logger.exception(
                    "pending_hard_gate_cancel_failed symbol={} side={}",
                    symbol_, pos_side,
                )
                continue
            # `cancel_pending` returns the CANCELED event but does NOT
            # queue it — only `poll_pending` events flow through
            # `_process_pending`. We must dispatch the handler ourselves
            # so the journal records a `pending_hard_gate_invalidated`
            # rejected_signals row + the pending_setups slot is cleared.
            if ev is not None:
                try:
                    await self._handle_pending_canceled(ev)
                except Exception:
                    logger.exception(
                        "pending_hard_gate_handler_failed symbol={} side={}",
                        symbol_, pos_side,
                    )

    async def _maybe_revise_tp_dynamic(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> None:
        """Dynamic TP revision: re-anchor the runner OCO TP to the current
        ``target_rr_ratio × sl_distance`` whenever live data suggests the
        old placement has drifted past tolerance.

        Why it exists: at fill time we set TP = entry ± target_rr × sl_dist.
        That snapshot is correct *at fill*, but cancellation pressure (e.g.
        the entry slipped vs. the limit price, or a partial fill happened)
        and TP1/BE moves can leave the runner OCO at a stale ratio. The
        revise re-derives the "ideal 1:N TP" from the live entry/SL state
        held by the monitor and only fires when the delta passes
        ``tp_revise_min_delta_atr × ATR`` and at least
        ``tp_revise_cooldown_s`` have elapsed since the last revise.
        Disabled when ``execution.target_rr_ratio == 0`` (no contract to
        enforce) or ``execution.tp_dynamic_enabled == false``.
        """
        cfg = self.ctx.config
        if not cfg.execution.tp_dynamic_enabled:
            return
        target_rr = cfg.execution.target_rr_ratio
        if target_rr <= 0:
            return
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return
        # Use plan_sl_price (immutable, the SL at fill time) for ratio math —
        # after SL-to-BE the mutable sl_price collapses to ~0.1% of entry,
        # which produces a near-entry new_tp that Bybit rejects (110012).
        # plan_sl_price == 0.0 means "unknown" (post-BE rehydrate): skip.
        plan_sl = float(snap.get("plan_sl_price") or 0.0)
        entry = float(snap.get("entry_price") or 0.0)
        if plan_sl <= 0 or entry <= 0:
            return
        sl_distance = abs(entry - plan_sl)
        if sl_distance <= 0:
            return
        sign = 1 if pos_side == "long" else -1
        new_tp = entry + sign * target_rr * sl_distance
        # Guard against revising into a sub-floor RR if mark drifted past
        # entry (post-BE move or unusual book) — tp_min_rr_floor is a
        # hard backstop on the proposed RR.
        floor = cfg.execution.tp_min_rr_floor
        if floor > 0:
            min_tp = entry + sign * floor * sl_distance
            if sign > 0 and new_tp < min_tp:
                new_tp = min_tp
            elif sign < 0 and new_tp > min_tp:
                new_tp = min_tp
        cur_tp = snap.get("tp2_price")
        if cur_tp is None:
            return
        atr = float(getattr(state, "atr", 0.0) or 0.0)
        if atr > 0 and abs(float(cur_tp) - new_tp) < cfg.execution.tp_revise_min_delta_atr * atr:
            return
        last = snap.get("last_tp_revise_at")
        if last is not None:
            elapsed = (_utc_now() - last).total_seconds()
            if elapsed < cfg.execution.tp_revise_cooldown_s:
                return
        try:
            await asyncio.to_thread(
                self.ctx.monitor.revise_runner_tp, symbol, pos_side, new_tp,
            )
        except Exception:
            logger.exception(
                "tp_revise_dispatch_failed symbol={} side={}", symbol, pos_side,
            )

    async def _maybe_lock_sl_on_mfe(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> None:
        """MFE-triggered SL lock (Option A, 2026-04-20).

        Once a position's maximum favorable excursion (current price vs.
        entry, measured in plan-R multiples) crosses
        ``execution.sl_lock_mfe_r``, cancel + re-place the runner OCO
        with a new SL at ``entry + sign × sl_lock_at_r × plan_sl_dist``.
        With ``sl_lock_at_r: 0.0`` the new SL lands at entry + a tiny
        fee buffer (``sl_be_offset_pct``), turning the last
        ``target_rr - sl_lock_mfe_r`` of reward into risk-free upside.

        One-shot per position (monitor's ``sl_lock_applied`` gate). Skips
        when ``plan_sl_price <= 0`` (post-BE rehydrate) or when the runner
        OCO is already the BE replacement from the TP1 path (legacy
        partial-TP flow — those are already at BE, locking again is a no-op
        at best, a "tighten further" at worst).
        """
        cfg = self.ctx.config
        if not cfg.execution.sl_lock_enabled:
            return
        mfe_threshold = float(cfg.execution.sl_lock_mfe_r)
        if mfe_threshold <= 0:
            return
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return
        if snap.get("sl_lock_applied"):
            return
        # Leave post-TP1 BE positions alone — their runner OCO is already
        # at BE from the partial-TP cascade. Reapplying would either cancel
        # a protective SL and replace it with the same thing (churn) or, if
        # sl_lock_at_r > 0, would tighten further, which is out of scope
        # for a "first-time risk removal" gate.
        if snap.get("be_already_moved"):
            return
        plan_sl = float(snap.get("plan_sl_price") or 0.0)
        entry = float(snap.get("entry_price") or 0.0)
        if plan_sl <= 0 or entry <= 0:
            return
        sl_distance = abs(entry - plan_sl)
        if sl_distance <= 0:
            return
        current_px = float(getattr(state, "current_price", 0.0) or 0.0)
        if current_px <= 0:
            return
        sign = 1 if pos_side == "long" else -1
        mfe_r = sign * (current_px - entry) / sl_distance
        if mfe_r < mfe_threshold:
            return
        # Compute new SL: entry + sign × sl_lock_at_r × plan_sl_distance.
        # At sl_lock_at_r=0, layer the BE fee-buffer (sl_be_offset_pct) so
        # the stop sits a hair past entry on the profit side — covers the
        # remaining leg's exit taker fee + slippage (same pattern as the
        # TP1 BE replacement).
        lock_r = float(cfg.execution.sl_lock_at_r)
        new_sl = entry + sign * lock_r * sl_distance
        if lock_r == 0.0:
            new_sl = entry + sign * (entry * cfg.execution.sl_be_offset_pct)
        try:
            await asyncio.to_thread(
                self.ctx.monitor.lock_sl_at, symbol, pos_side, new_sl,
            )
        except Exception:
            logger.exception(
                "sl_lock_dispatch_failed symbol={} side={}", symbol, pos_side,
            )

    def _pillar_opposition_for(self, symbol: str) -> Optional[Direction]:
        """Cross-asset opposition signal for `symbol` (Phase 7.A6).

        Returns:
          * Direction.BULLISH when both pillars are BULLISH → blocks BEARISH alts
          * Direction.BEARISH when both pillars are BEARISH → blocks BULLISH alts
          * None when the veto is disabled, `symbol` is a pillar itself, either
            pillar's bias is missing / neutral / stale.
        """
        cfg = self.ctx.config.analysis
        if not cfg.cross_asset_veto_enabled:
            return None
        if symbol in _PILLAR_SYMBOLS:
            return None
        now = _utc_now()
        biases: list[Direction] = []
        for sym in _PILLAR_SYMBOLS:
            item = self.ctx.pillar_bias.get(sym)
            if item is None:
                return None
            bias, updated = item
            if bias == Direction.UNDEFINED:
                return None
            if (now - updated).total_seconds() > cfg.cross_asset_veto_max_age_s:
                return None
            biases.append(bias)
        if all(b == Direction.BULLISH for b in biases):
            return Direction.BULLISH
        if all(b == Direction.BEARISH for b in biases):
            return Direction.BEARISH
        return None

    def _pillar_bias_label(self, pillar_symbol: str) -> Optional[str]:
        """Current pillar bias as a string, or None if missing/stale.

        Used when stamping `cross_asset_opposition` rejects — the auditor
        needs to know which pillar pair tripped the veto on the exact signal.
        """
        cfg = self.ctx.config.analysis
        item = self.ctx.pillar_bias.get(pillar_symbol)
        if item is None:
            return None
        bias, updated = item
        if (_utc_now() - updated).total_seconds() > cfg.cross_asset_veto_max_age_s:
            return None
        return bias.value

    def _build_oscillator_raw_values(
        self,
        symbol: str,
        entry_state: Optional[MarketState],
    ) -> dict[str, dict]:
        """Snapshot oscillator numeric values across 1m/3m/15m TFs.

        Reads from three independent sources (the runner's per-cycle TF
        sweep populates each):

          * 3m (entry TF)  — `entry_state.oscillator` just read at entry
            settle; passed in explicitly because this helper is called
            after the entry state is built.
          * 15m (HTF)      — `ctx.htf_state_cache[symbol].oscillator`
            populated during the HTF pass (runner.py §2a). Cleared on
            the already-open skip, so entries on just-closed symbols
            may see the freshest HTF; entries skipping HTF see {}.
          * 1m (LTF)       — `ctx.ltf_cache[symbol].oscillator` (added
            2026-04-22 gece-late to LTFState). None when LTF read
            failed or bridge=None.

        Each TF's value is the `OscillatorTableData.model_dump()` dict
        (wt1, wt2, wt_state, wt_cross, wt_vwap_fast, rsi, rsi_state,
        rsi_mfi, rsi_mfi_bias, stoch_k, stoch_d, stoch_state,
        last_signal, last_signal_bars_ago, last_wt_div,
        last_wt_div_bars_ago, momentum). Missing TF → key absent (not
        an empty sub-dict — so downstream consumers can distinguish
        "wasn't captured" from "captured but all zero").

        Returned dict is JSON-serialisable and shape-stable across runs;
        suitable for direct forwarding to `record_open` /
        `record_rejected_signal`. Never raises — any access failure
        silently produces {} for that TF.
        """
        out: dict[str, dict] = {}
        try:
            if entry_state is not None:
                osc_3m = getattr(entry_state, "oscillator", None)
                if osc_3m is not None:
                    out["3m"] = osc_3m.model_dump()
        except Exception:
            pass
        try:
            htf = self.ctx.htf_state_cache.get(symbol)
            if htf is not None:
                osc_15m = getattr(htf, "oscillator", None)
                if osc_15m is not None:
                    out["15m"] = osc_15m.model_dump()
        except Exception:
            pass
        try:
            ltf = self.ctx.ltf_cache.get(symbol)
            if ltf is not None:
                osc_1m = getattr(ltf, "oscillator", None)
                if osc_1m is not None:
                    out["1m"] = osc_1m.model_dump()
        except Exception:
            pass
        return out

    def _per_symbol_cex_flow_for(self, symbol: str) -> Optional[float]:
        """Extract the per-symbol 1h CEX net flow (USD) from the Arkham
        snapshot's JSON dict.

        Snapshot stores `token_volume_1h_net_usd_json` as a JSON string
        keyed by internal-format perp symbol (e.g. "BTC-USDT-SWAP") with the most-recent-
        hour `inUSD - outUSD` as float value. Returns None when:
          * on_chain snapshot is absent (master off, or pre-first-refresh)
          * JSON column missing / unparseable (legacy row or fetch failure)
          * symbol not in the dict (Arkham coverage gap for that token)

        Positive value = token flowing INTO CEX (bearish for symbol);
        negative = flowing OUT (bullish). Downstream
        `_per_symbol_cex_flow_penalty` applies the misalignment bump.
        """
        snap = self.ctx.on_chain_snapshot
        if snap is None:
            return None
        raw = getattr(snap, "token_volume_1h_net_usd_json", None)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        val = parsed.get(symbol)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    async def _record_reject(
        self,
        *,
        symbol: str,
        reject_reason: str,
        state: MarketState,
        conf,
        candles: Optional[list] = None,
    ) -> None:
        """Persist a reject to `rejected_signals` (Phase 7.B1).

        Caller is responsible for try/except around this — any DB issue
        must never block the main cycle (reject logging is observational).
        All snapshot fields default to None so partial data is fine.
        """
        cfg = self.ctx.config
        entry_tf_minutes = _timeframe_to_minutes(cfg.trading.entry_timeframe)
        enrichment = _derive_enrichment(
            state, candles=candles, entry_tf_minutes=entry_tf_minutes,
        )
        htf_trend = state.trend_htf
        session = state.active_session
        price = state.current_price
        atr = state.atr
        signal_ts = state.timestamp or _utc_now()
        await self.ctx.journal.record_rejected_signal(
            symbol=symbol,
            direction=getattr(conf, "direction", Direction.UNDEFINED),
            reject_reason=reject_reason,
            signal_timestamp=signal_ts,
            price=float(price) if price else None,
            atr=float(atr) if atr else None,
            confluence_score=float(getattr(conf, "score", 0.0) or 0.0),
            confluence_factors=list(getattr(conf, "factor_names", []) or []),
            entry_timeframe=cfg.trading.entry_timeframe,
            htf_timeframe=cfg.trading.htf_timeframe,
            htf_bias=htf_trend.value if htf_trend != Direction.UNDEFINED else None,
            session=session.value if session != Session.OFF else None,
            market_structure=_structure_str(state),
            regime_at_entry=enrichment["regime_at_entry"],
            funding_z_at_entry=enrichment["funding_z_at_entry"],
            ls_ratio_at_entry=enrichment["ls_ratio_at_entry"],
            oi_change_24h_at_entry=enrichment["oi_change_24h_at_entry"],
            liq_imbalance_1h_at_entry=enrichment["liq_imbalance_1h_at_entry"],
            nearest_liq_cluster_above_price=enrichment["nearest_liq_cluster_above_price"],
            nearest_liq_cluster_below_price=enrichment["nearest_liq_cluster_below_price"],
            nearest_liq_cluster_above_notional=enrichment["nearest_liq_cluster_above_notional"],
            nearest_liq_cluster_below_notional=enrichment["nearest_liq_cluster_below_notional"],
            nearest_liq_cluster_above_distance_atr=enrichment["nearest_liq_cluster_above_distance_atr"],
            nearest_liq_cluster_below_distance_atr=enrichment["nearest_liq_cluster_below_distance_atr"],
            pillar_btc_bias=self._pillar_bias_label("BTC-USDT-SWAP"),
            pillar_eth_bias=self._pillar_bias_label("ETH-USDT-SWAP"),
            on_chain_context=self._on_chain_context_dict(),
            confluence_pillar_scores={
                f.name: float(f.weight)
                for f in getattr(conf, "factors", []) or []
            },
            oscillator_raw_values=self._build_oscillator_raw_values(
                symbol, state,
            ),
            # 2026-04-27 — derivatives + heatmap enrichment forwarding.
            # Gap acknowledged in 2026-04-24 changelog; until now the
            # `_derive_enrichment` output's 2026-04-23 fields landed only on
            # `trades` rows, never `rejected_signals`. Pass 3 counter-factual
            # GBT was missing OI / funding / liq notional / LS z-score / 1h
            # 4h price-change / heatmap top clusters on every reject. With
            # this forwarding all 132 historical rows (post-clean_since)
            # remain NULL — only new rejects from this commit forward will
            # be enriched. A back-fill script can replay these fields from
            # `derivatives_snapshots` joined on signal_timestamp if Pass 3
            # finds the gap material.
            open_interest_usd_at_entry=enrichment["open_interest_usd_at_entry"],
            oi_change_1h_pct_at_entry=enrichment["oi_change_1h_pct_at_entry"],
            funding_rate_current_at_entry=enrichment["funding_rate_current_at_entry"],
            funding_rate_predicted_at_entry=enrichment["funding_rate_predicted_at_entry"],
            long_liq_notional_1h_at_entry=enrichment["long_liq_notional_1h_at_entry"],
            short_liq_notional_1h_at_entry=enrichment["short_liq_notional_1h_at_entry"],
            ls_ratio_zscore_14d_at_entry=enrichment["ls_ratio_zscore_14d_at_entry"],
            price_change_1h_pct_at_entry=enrichment["price_change_1h_pct_at_entry"],
            price_change_4h_pct_at_entry=enrichment["price_change_4h_pct_at_entry"],
            liq_heatmap_top_clusters=enrichment["liq_heatmap_top_clusters"],
        )

    def _is_ltf_reversal(self, ltf: LTFState, open_side: str, max_age: int) -> bool:
        """True when the fresh LTF signal contradicts the open side.

        Long open → need BEARISH trend + fresh SELL signal.
        Short open → need BULLISH trend + fresh BUY signal.
        "Fresh" = last_signal_bars_ago <= max_age.
        """
        if ltf.last_signal_bars_ago > max_age:
            return False
        sig = (ltf.last_signal or "").upper()
        if open_side == "long":
            return ltf.trend == Direction.BEARISH and sig == "SELL"
        if open_side == "short":
            return ltf.trend == Direction.BULLISH and sig == "BUY"
        return False

    async def _defensive_close(self, symbol: str, side: str, reason: str) -> None:
        """Cancel algos + close the position, tagged with `close_reason`.

        Idempotent via `defensive_close_in_flight`. The monitor will emit a
        CloseFill on its next poll, and `_handle_close` will stamp the reason
        on the journal row.
        """
        key = (symbol, side)
        if key in self.ctx.defensive_close_in_flight:
            return
        self.ctx.defensive_close_in_flight.add(key)

        # On Bybit V5 the position-attached TP/SL clears automatically when
        # the position closes — no separate algo cancel needed (compare the
        # pre-migration cancel_algo loop that used to live here).
        try:
            await asyncio.to_thread(
                self.ctx.bybit_client.close_position, symbol, side,
            )
        except Exception:
            logger.exception("defensive_close_failed symbol={} side={}",
                             symbol, side)
            # Leave the guard set — next cycle's poll may still observe the
            # close on its own; we don't want to spam the exchange.
            return

        self.ctx.pending_close_reasons[key] = "EARLY_CLOSE_LTF_REVERSAL"
        logger.info("defensive_close_triggered symbol={} side={} reason={}",
                    symbol, side, reason)

    async def _read_last_bar(self) -> Optional[int]:
        """Best-effort read of the signal-table last_bar. None on any failure."""
        try:
            state = await self.ctx.reader.read_market_state()
            return state.signal_table.last_bar if state.signal_table else None
        except Exception:
            return None

    async def _wait_for_pine_settle(self, baseline: Optional[int]) -> bool:
        """Poll the signal table until `last_bar` differs from *baseline*,
        meaning Pine has re-rendered for the new symbol / timeframe. Returns
        True on change.

        Fallbacks:
          * `baseline is None` → Pine didn't expose a last_bar before the
            change (first boot, or Pine version without the field, or fake
            reader in tests). The static `tf_settle_seconds` sleep is assumed
            sufficient; return True immediately so the caller keeps going.
          * Timeout → False (caller skips the symbol cycle).
        """
        if baseline is None:
            return True
        cfg = self.ctx.config.trading
        deadline = time.monotonic() + cfg.pine_settle_max_wait_s
        while time.monotonic() < deadline:
            lb = await self._read_last_bar()
            if lb is not None and lb != baseline:
                return True
            await asyncio.sleep(cfg.pine_settle_poll_interval_s)
        return False

    async def _switch_timeframe(self, tf: str) -> bool:
        """Switch chart TF, sleep the static settle, then freshness-poll.

        Returns True when Pine data reflects the new TF. False on timeout
        or bridge failure — caller skips the current symbol cycle.

        Short-circuit: if TV is already on the requested resolution, the
        ``set_timeframe`` call would be a no-op and Pine would not re-render,
        so the freshness poll can't observe a change. Detect that up front
        and succeed immediately.
        """
        if self.ctx.bridge is None:
            return True            # tests skip — reader fake already correct
        normalized = TVBridge._normalize_tf(tf)
        try:
            status = await self.ctx.bridge.status()
            if status.get("chart_resolution") == normalized:
                return True
        except Exception:
            pass  # fall through to the full switch+poll path
        # Capture the pre-switch last_bar so we can detect Pine re-rendering
        # for the new TF (same-value = still-old-chart, any-change = re-rendered).
        baseline = await self._read_last_bar()
        try:
            await self.ctx.bridge.set_timeframe(tf)
        except Exception:
            logger.exception("set_timeframe_failed tf={}", tf)
            return False
        await asyncio.sleep(self.ctx.config.trading.tf_settle_seconds)
        settled = await self._wait_for_pine_settle(baseline)
        if settled and self.ctx.config.trading.pine_post_settle_grace_s > 0:
            await asyncio.sleep(self.ctx.config.trading.pine_post_settle_grace_s)
        return settled

    async def _run_one_symbol(self, symbol: str) -> None:
        cfg = self.ctx.config
        logger.info("symbol_cycle_start symbol={}", symbol)

        # 0. Macro event blackout — skip new entries inside ±window of a
        # scheduled HIGH-impact USD event (CPI/FOMC/NFP/PCE). Open positions
        # are untouched (their OCO algos already manage exit). Cheap sync
        # check, runs before the expensive TV symbol/TF switching.
        if self.ctx.economic_calendar is not None:
            try:
                blackout = self.ctx.economic_calendar.is_in_blackout(_utc_now())
            except Exception:
                logger.exception("economic_calendar_check_failed symbol={}", symbol)
                blackout = None
            if blackout is not None and blackout.active and blackout.event is not None:
                evt = blackout.event
                logger.info(
                    "symbol_decision symbol={} NO_TRADE reason=macro_event_blackout "
                    "event={!r} country={} impact={} secs_to_event={} "
                    "secs_after_event={} source={}",
                    symbol, evt.title, evt.country, evt.impact.value,
                    blackout.seconds_until_event, blackout.seconds_after_event,
                    evt.source,
                )
                return

        # 0b. VWAP daily-reset blackout — skip new entries inside the
        # ±window around UTC 00:00. Pine 1m/3m/15m VWAPs all anchor on
        # the daily session change, and the ±1σ band is collapsed for
        # the first ~10-30 min of the new day. Open positions are
        # untouched; resting pendings are re-checked in the pending-
        # invalidation pass below. Cheap pure-time check.
        if cfg.analysis.vwap_reset_blackout_enabled and in_vwap_reset_blackout(
            _utc_now(),
            pre_minutes=cfg.analysis.vwap_reset_blackout_window_pre_min,
            post_minutes=cfg.analysis.vwap_reset_blackout_window_post_min,
        ):
            logger.info(
                "symbol_decision symbol={} NO_TRADE reason=vwap_reset_blackout "
                "pre_min={} post_min={}",
                symbol,
                cfg.analysis.vwap_reset_blackout_window_pre_min,
                cfg.analysis.vwap_reset_blackout_window_post_min,
            )
            return

        # 1. Switch the TV chart to this symbol (production has a bridge;
        # tests pass bridge=None and the reader fake already knows the symbol).
        if self.ctx.bridge is not None:
            try:
                await self.ctx.bridge.set_symbol(internal_to_tv_symbol(symbol))
                await asyncio.sleep(cfg.trading.symbol_settle_seconds)
            except Exception:
                logger.exception("set_symbol_failed symbol={}", symbol)
                return

        # Early dedup probe — HTF S/R zones are only consumed by the entry
        # planner (SL push + TP ceiling). Defensive close (Madde F) only
        # reads LTF state, and step 3 below will dedup-block the entry
        # anyway, so skipping the HTF pass for already-open symbols saves
        # one tf_settle + freshness-poll + grace (~5-14s) per cycle per
        # held position. Stale cache is fine: next cycle after the position
        # closes, `already_open` flips False and HTF reloads before the
        # planner runs.
        already_open = any(k[0] == symbol for k in self.ctx.open_trade_ids)

        # 2a. HTF pass — switch TF, read S/R from HTF candles, cache.
        if not already_open:
            if self.ctx.bridge is not None:
                htf_ok = await self._switch_timeframe(cfg.trading.htf_timeframe)
                if not htf_ok:
                    logger.warning("htf_settle_timeout symbol={} — skipping symbol",
                                   symbol)
                    return
            try:
                htf_key = _timeframe_key(cfg.trading.htf_timeframe)
                await self.ctx.multi_tf.refresh(htf_key, count=200)
                htf_buf = self.ctx.multi_tf.get_buffer(htf_key)
                htf_candles = htf_buf.last(200) if htf_buf is not None else []
                if htf_candles:
                    self.ctx.htf_sr_cache[symbol] = detect_sr_zones(
                        htf_candles,
                        min_touches=cfg.analysis.sr_min_touches,
                        zone_atr_mult=cfg.analysis.sr_zone_atr_mult,
                    )
                else:
                    self.ctx.htf_sr_cache.pop(symbol, None)
            except Exception:
                logger.exception("htf_refresh_failed symbol={}", symbol)
                self.ctx.htf_sr_cache.pop(symbol, None)

            # Phase 7.B4 — snapshot HTF MarketState (Pine tables for 15m) so
            # the zone-entry planner can read HTF FVG / OB / trend without a
            # second TF switch. Only meaningful with a live bridge (fakes can
            # populate this directly); cleared on read failure so consumers
            # never see a stale entry-TF state mis-labelled as HTF.
            if self.ctx.bridge is not None:
                try:
                    self.ctx.htf_state_cache[symbol] = (
                        await self.ctx.reader.read_market_state()
                    )
                except Exception:
                    logger.exception("htf_state_read_failed symbol={}", symbol)
                    self.ctx.htf_state_cache.pop(symbol, None)
        else:
            # Already-open skip: stale HTF state must not feed a later setup
            # planner when this symbol's position closes and the gate reopens.
            self.ctx.htf_state_cache.pop(symbol, None)

        # 2b. LTF pass — read oscillator into LTFState, cache for Madde F.
        if self.ctx.bridge is not None and self.ctx.ltf_reader is not None:
            ltf_ok = await self._switch_timeframe(cfg.trading.ltf_timeframe)
            if not ltf_ok:
                logger.info("ltf_settle_timeout symbol={} — entry path continues "
                            "without LTF signal", symbol)
                self.ctx.ltf_cache.pop(symbol, None)
            else:
                try:
                    self.ctx.ltf_cache[symbol] = await self.ctx.ltf_reader.read(
                        symbol, cfg.trading.ltf_timeframe)
                except Exception:
                    logger.exception("ltf_read_failed symbol={}", symbol)
                    self.ctx.ltf_cache.pop(symbol, None)

        # 2c. Entry TF pass — switch + settle + read the entry state.
        if self.ctx.bridge is not None:
            entry_ok = await self._switch_timeframe(cfg.trading.entry_timeframe)
            if not entry_ok:
                logger.warning("entry_settle_timeout symbol={} — skipping",
                               symbol)
                return
        try:
            state = await self.ctx.reader.read_market_state()
            tf_key = _timeframe_key(cfg.trading.entry_timeframe)
            await self.ctx.multi_tf.refresh(tf_key, count=100)
        except Exception:
            logger.exception("fetch_failed symbol={}", symbol)
            return
        # 2026-04-21 — attach the cached Arkham snapshot + whale blackout
        # registry to MarketState so downstream consumers (Phase C
        # calculate_confluence modifier, Phase D / E gates) see the same
        # values already accounted in this tick's `_refresh_on_chain_snapshots`.
        # In Phase B both fields are carried but no gate / modifier reads
        # them — the attachment is for journal write consistency only.
        state.on_chain = self.ctx.on_chain_snapshot
        state.whale_blackout = self.ctx.whale_blackout_state
        # 2026-04-26 — cache entry-TF MarketState for the intra-trade
        # position-snapshot writer. Read by `_maybe_write_position_snapshots`
        # (called from `_process_closes`) without needing a fresh TF switch.
        # Cached AFTER on_chain/whale attachment so the snapshot row's
        # oscillator + VWAP fields match this cycle's downstream consumers.
        self.ctx.last_market_state_per_symbol[symbol] = state
        buf = self.ctx.multi_tf.get_buffer(tf_key)
        # 100 candles is enough for EMA55 seeding in the zone builder's
        # ema21_pullback source; legacy confluence consumers only read the tail.
        candles = buf.last(100) if buf is not None else []

        # 2c-alt. Cross-asset pillar bias (Phase 7.A6).
        # Snapshot BTC/ETH EMA stacks as they pass through their own cycle;
        # altcoin cycles below will consult the cache. Enough closes must
        # be available to seed the slow-period EMA — otherwise the helper
        # returns UNDEFINED and the snapshot entry is skipped.
        if symbol in _PILLAR_SYMBOLS and cfg.analysis.cross_asset_veto_enabled:
            bias = _pillar_bias_from(
                state,
                candles,
                fast_period=cfg.analysis.ema_veto_fast_period,
                slow_period=cfg.analysis.ema_veto_slow_period,
            )
            if bias != Direction.UNDEFINED:
                self.ctx.pillar_bias[symbol] = (bias, _utc_now())
            logger.info(
                "pillar_bias_update symbol={} bias={}",
                symbol, bias.value,
            )

        # 2c-bis. Attach derivatives state + liquidity heatmap (Phase 1.5).
        # Failure here must never crash the symbol cycle.
        if self.ctx.derivatives_cache is not None:
            try:
                deriv = self.ctx.derivatives_cache.get(symbol)
                state.derivatives = deriv
                if cfg.derivatives.heatmap_enabled and state.current_price > 0:
                    state.liquidity_heatmap = build_heatmap(
                        symbol=symbol,
                        current_price=state.current_price,
                        deriv_state=deriv,
                        liq_stream=self.ctx.liquidation_stream,
                        bucket_pct=cfg.derivatives.heatmap_bucket_pct,
                        historical_lookback_ms=cfg.derivatives.heatmap_historical_lookback_ms,
                        max_clusters_each_side=cfg.derivatives.heatmap_max_clusters_each_side,
                        leverage_buckets=cfg.derivatives.leverage_buckets,
                    )
            except Exception as e:
                logger.warning(
                    "deriv_attach_failed symbol={} err={!r}", symbol, e,
                )

        # 2d. LTF reversal defensive close (Madde F) — if we already hold a
        # position and the LTF oscillator just flipped against us, close it
        # before looking for new entries. Consumes this tick.
        open_side = self._get_open_side(symbol)
        if open_side and cfg.execution.ltf_reversal_close_enabled:
            ltf = self.ctx.ltf_cache.get(symbol)
            opened_at = self.ctx.open_trade_opened_at.get((symbol, open_side))
            if ltf is not None and opened_at is not None:
                entry_tf_sec = _tf_seconds(cfg.trading.entry_timeframe)
                elapsed_bars = (_utc_now() - opened_at).total_seconds() / entry_tf_sec
                if (
                    elapsed_bars >= cfg.execution.ltf_reversal_min_bars_in_position
                    and self._is_ltf_reversal(
                        ltf, open_side, cfg.execution.ltf_reversal_signal_max_age,
                    )
                ):
                    await self._defensive_close(symbol, open_side, "ltf_reversal")
                    return

        # 2e. Dynamic TP revision — when we still hold a position and the
        # runner OCO has drifted from the contracted 1:N RR target, cancel +
        # re-place at the current entry-anchored target. Off when
        # `execution.tp_dynamic_enabled` is false. Cheap no-op otherwise.
        if open_side:
            await self._maybe_revise_tp_dynamic(symbol, open_side, state)

        # 2f. MFE-triggered SL lock — when MFE crosses `sl_lock_mfe_r`, pull
        # the runner SL up to entry (± fee buffer) so the remaining target
        # is risk-free. Off when `execution.sl_lock_enabled` is false.
        # One-shot per position.
        if open_side:
            await self._maybe_lock_sl_on_mfe(symbol, open_side, state)

        # 3. Symbol-level dedup — skip open if we still hold anything OR
        # already have a pending limit entry waiting for fill (Phase 7.C4).
        if any(k[0] == symbol for k in self.ctx.open_trade_ids):
            return
        # 2026-04-22 — pending limit re-evaluation. Before short-circuiting,
        # re-run the HARD veto gates against current state for any pending
        # limit on this symbol. If the SAME setup wouldn't pass NOW
        # (cross-asset flipped, whale event, momentum reversed, VWAP cross),
        # cancel the pending so a fill at a no-longer-favorable level is
        # avoided. Pure consistency fix — same gates that reject NEW
        # entries now also invalidate WAITING entries. Confluence is NOT
        # rescored (pullback strategy expects natural fluctuation).
        await self._maybe_invalidate_pending_for(symbol, state, candles)
        if any(k[0] == symbol for k in self.ctx.pending_setups):
            return

        # 4. Plan. Risk budget (R = risk_pct × balance) is derived from TOTAL
        # equity so drawdowns scale R naturally but locked margin in other
        # positions doesn't shrink it. Margin-fit (notional/leverage ceiling)
        # uses the smaller of per-slot fair-share and live `availEq` so the
        # order still fits on Bybit right now and multiple concurrent positions
        # coexist. Bybit insufficient-margin (110004) avoidance lives on the
        # margin side.
        try:
            total_eq = await asyncio.to_thread(
                self.ctx.bybit_client.get_total_equity, "USDT"
            )
        except Exception:
            logger.exception("total_eq_sync_failed_using_cached")
            total_eq = self.ctx.risk_mgr.current_balance
        try:
            bybit_avail = await asyncio.to_thread(
                self.ctx.bybit_client.get_balance, "USDT"
            )
        except Exception:
            logger.exception("balance_sync_failed_using_cached")
            bybit_avail = self.ctx.risk_mgr.current_balance
        slot_count = max(1, int(cfg.trading.max_concurrent_positions))
        per_slot = total_eq / slot_count
        risk_balance = min(total_eq, self.ctx.risk_mgr.current_balance)
        margin_balance = min(per_slot, bybit_avail)
        sizing_balance = margin_balance  # retained for logging/back-compat

        # 2026-04-26 — auto-R mode. Resolve the per-trade $R override in
        # priority order:
        #   1. Operator env / YAML `risk_amount_usdt` (escape hatch)
        #   2. `auto_risk_pct_of_wallet > 0` → realized_wallet × pct
        #   3. None → rr_system falls back to `balance × risk_per_trade_pct`
        # Realized wallet (UPL excluded) keeps R from inflating during a
        # winning streak or shrinking during open-position drawdowns. The
        # probe failure path falls through to (3) so a single Bybit blip
        # doesn't halt sizing.
        risk_amount_override: Optional[float] = cfg.trading.risk_amount_usdt
        if (risk_amount_override is None
                and cfg.trading.auto_risk_pct_of_wallet > 0):
            try:
                wallet_realized = await asyncio.to_thread(
                    self.ctx.bybit_client.get_wallet_balance_realized, "USDT"
                )
            except Exception:
                logger.exception("wallet_realized_sync_failed_using_total_eq")
                wallet_realized = total_eq
            if wallet_realized > 0:
                risk_amount_override = (
                    wallet_realized * cfg.trading.auto_risk_pct_of_wallet
                )
                logger.info(
                    "auto_risk_resolved wallet_realized={:.2f} pct={:.4f} R={:.2f}",
                    wallet_realized,
                    cfg.trading.auto_risk_pct_of_wallet,
                    risk_amount_override,
                )

        # Phase 7.D3 — classify trend regime on the entry-TF closed buffer.
        # Used both as a scoring input (conditional factor weights) and as a
        # journal tag (`trend_regime_at_entry`). UNKNOWN is fail-open: the
        # scorer sees no regime signal and falls back to base weights.
        trend_regime_result = classify_trend_regime(
            candles,
            period=cfg.analysis.adx_period,
            ranging_threshold=cfg.analysis.trend_regime_ranging_threshold,
            strong_threshold=cfg.analysis.trend_regime_strong_threshold,
        )
        trend_regime = trend_regime_result.regime

        try:
            plan, reject_reason = build_trade_plan_with_reason(
                state, risk_balance,
                candles=candles,
                min_confluence_score=cfg.analysis.min_confluence_score,
                weights=cfg.analysis.confluence_weights or None,
                risk_pct=cfg.risk_pct_fraction(),
                rr_ratio=cfg.trading.default_rr_ratio,
                min_rr_ratio=cfg.trading.min_rr_ratio,
                max_leverage=min(
                    cfg.trading.max_leverage,
                    self.ctx.max_leverage_per_symbol.get(
                        symbol, cfg.trading.max_leverage),
                    cfg.trading.symbol_leverage_caps.get(
                        symbol, cfg.trading.max_leverage),
                ),
                contract_size=self.ctx.contract_sizes.get(
                    symbol, cfg.trading.contract_size),
                margin_balance=margin_balance,
                risk_amount_usdt_override=risk_amount_override,
                swing_lookback=cfg.swing_lookback_for(symbol),
                allowed_sessions=cfg.allowed_sessions_for(symbol) or None,
                htf_sr_zones=self.ctx.htf_sr_cache.get(symbol),
                htf_sr_ceiling_enabled=cfg.analysis.htf_sr_ceiling_enabled,
                htf_sr_buffer_atr=cfg.htf_sr_buffer_atr_for(symbol),
                crowded_skip_enabled=cfg.derivatives.crowded_skip_enabled,
                crowded_skip_z_threshold=cfg.derivatives.crowded_skip_z_threshold,
                ltf_state=self.ctx.ltf_cache.get(symbol),
                min_tp_distance_pct=cfg.analysis.min_tp_distance_pct,
                min_sl_distance_pct=cfg.min_sl_distance_pct_for(symbol),
                fee_reserve_pct=cfg.trading.fee_reserve_pct,
                partial_tp_enabled=cfg.execution.partial_tp_enabled,
                partial_tp_ratio=cfg.execution.partial_tp_ratio,
                min_rsi_mfi_magnitude=cfg.analysis.min_rsi_mfi_magnitude,
                liquidity_pool_max_atr_dist=cfg.analysis.liquidity_pool_max_atr_dist,
                vwap_hard_veto_enabled=cfg.analysis.vwap_hard_veto_enabled,
                ema_veto_enabled=cfg.analysis.ema_veto_enabled,
                ema_veto_fast_period=cfg.analysis.ema_veto_fast_period,
                ema_veto_slow_period=cfg.analysis.ema_veto_slow_period,
                pillar_opposition=self._pillar_opposition_for(symbol),
                premium_discount_veto_enabled=cfg.analysis.premium_discount_veto_enabled,
                premium_discount_lookback=cfg.analysis.premium_discount_lookback,
                displacement_atr_mult=cfg.analysis.displacement_atr_mult,
                displacement_max_bars_ago=cfg.analysis.displacement_max_bars_ago,
                divergence_fresh_bars=cfg.analysis.divergence_fresh_bars,
                divergence_decay_bars=cfg.analysis.divergence_decay_bars,
                divergence_max_bars=cfg.analysis.divergence_max_bars,
                trend_regime=trend_regime,
                trend_regime_conditional_scoring_enabled=
                    cfg.analysis.trend_regime_conditional_scoring_enabled,
                daily_bias_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.daily_bias_enabled
                ),
                daily_bias_delta=cfg.on_chain.daily_bias_modifier_delta,
                stablecoin_pulse_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.stablecoin_pulse_enabled
                ),
                stablecoin_pulse_usd=self.ctx.stablecoin_pulse_1h_usd,
                stablecoin_pulse_threshold_usd=(
                    cfg.on_chain.stablecoin_pulse_threshold_usd),
                stablecoin_pulse_penalty=(
                    cfg.on_chain.stablecoin_pulse_penalty),
                altcoin_index_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.altcoin_index_enabled
                ),
                altcoin_index_value=self.ctx.altcoin_index_value,
                altcoin_index_is_altcoin=(symbol not in _PILLAR_SYMBOLS),
                altcoin_index_bearish_threshold=(
                    cfg.on_chain.altcoin_index_bearish_threshold),
                altcoin_index_bullish_threshold=(
                    cfg.on_chain.altcoin_index_bullish_threshold),
                altcoin_index_penalty=(
                    cfg.on_chain.altcoin_index_modifier_delta),
                # 2026-04-22 — flow_alignment soft directional signal.
                # Replaces the whale hard gate; penalty defaults to 0.25
                # in Pass 1 (tuned in Pass 2).
                flow_alignment_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.flow_alignment_enabled
                ),
                flow_alignment_penalty=cfg.on_chain.flow_alignment_penalty,
                flow_alignment_noise_floor_usd=(
                    cfg.on_chain.flow_alignment_noise_floor_usd),
                flow_alignment_btc_netflow_24h_usd=(
                    self.ctx.on_chain_snapshot.cex_btc_netflow_24h_usd
                    if self.ctx.on_chain_snapshot is not None else None
                ),
                flow_alignment_eth_netflow_24h_usd=(
                    self.ctx.on_chain_snapshot.cex_eth_netflow_24h_usd
                    if self.ctx.on_chain_snapshot is not None else None
                ),
                flow_alignment_coinbase_netflow_24h_usd=(
                    self.ctx.on_chain_snapshot.cex_coinbase_netflow_24h_usd
                    if self.ctx.on_chain_snapshot is not None else None
                ),
                flow_alignment_binance_netflow_24h_usd=(
                    self.ctx.on_chain_snapshot.cex_binance_netflow_24h_usd
                    if self.ctx.on_chain_snapshot is not None else None
                ),
                flow_alignment_bybit_netflow_24h_usd=(
                    self.ctx.on_chain_snapshot.cex_bybit_netflow_24h_usd
                    if self.ctx.on_chain_snapshot is not None else None
                ),
                # 2026-04-22 (gece, late) — per-symbol 1h CEX volume penalty.
                # Looked up per-symbol from the snapshot's JSON dict.
                per_symbol_cex_flow_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.per_symbol_cex_flow_enabled
                ),
                per_symbol_cex_flow_usd=self._per_symbol_cex_flow_for(symbol),
                per_symbol_cex_flow_noise_floor_usd=(
                    cfg.on_chain.per_symbol_cex_flow_noise_floor_usd),
                per_symbol_cex_flow_penalty=(
                    cfg.on_chain.per_symbol_cex_flow_penalty),
            )
        except Exception:
            logger.exception("plan_build_failed symbol={}", symbol)
            return

        if plan is None:
            # reject_reason taxonomy: below_confluence / session_filter /
            # no_sl_source / vwap_misaligned / ema_momentum_contra /
            # cross_asset_opposition / wrong_side_of_premium_discount /
            # crowded_skip / zero_contracts / htf_tp_ceiling / tp_too_tight /
            # insufficient_contracts_for_split / macro_event_blackout.
            # Sub-floor SL distances are widened, not rejected.
            try:
                conf = calculate_confluence(
                    state,
                    ltf_candles=candles,
                    allowed_sessions=cfg.allowed_sessions_for(symbol) or None,
                    ltf_state=self.ctx.ltf_cache.get(symbol),
                    weights=cfg.analysis.confluence_weights or None,
                    min_rsi_mfi_magnitude=cfg.analysis.min_rsi_mfi_magnitude,
                    liquidity_pool_max_atr_dist=cfg.analysis.liquidity_pool_max_atr_dist,
                    displacement_atr_mult=cfg.analysis.displacement_atr_mult,
                    displacement_max_bars_ago=cfg.analysis.displacement_max_bars_ago,
                    divergence_fresh_bars=cfg.analysis.divergence_fresh_bars,
                    divergence_decay_bars=cfg.analysis.divergence_decay_bars,
                    divergence_max_bars=cfg.analysis.divergence_max_bars,
                    trend_regime=trend_regime,
                    trend_regime_conditional_scoring_enabled=
                        cfg.analysis.trend_regime_conditional_scoring_enabled,
                    daily_bias_enabled=(
                        cfg.on_chain.enabled
                        and cfg.on_chain.daily_bias_enabled
                    ),
                    daily_bias_delta=cfg.on_chain.daily_bias_modifier_delta,
                )
                logger.info(
                    "symbol_decision symbol={} NO_TRADE reason={} price={:.4f} "
                    "session={} direction={} confluence={:.2f}/{} factors={}",
                    symbol, reject_reason or "unknown",
                    float(state.current_price or 0.0),
                    getattr(state.active_session, "value", "NONE"),
                    getattr(conf.direction, "value", "UNDEFINED"),
                    conf.score, cfg.analysis.min_confluence_score,
                    ",".join(conf.factor_names) or "-",
                )
                # Phase 7.B1 — persist reject context for counter-factual audit.
                # Failure here must not block the cycle; downgrade to debug.
                try:
                    await self._record_reject(
                        symbol=symbol,
                        reject_reason=reject_reason or "unknown",
                        state=state,
                        conf=conf,
                        candles=candles,
                    )
                except Exception:
                    logger.debug("record_rejected_signal_failed symbol={}", symbol)
            except Exception:
                logger.debug("no_trade_log_failed symbol={}", symbol)
            return

        margin_locked = (plan.position_size_usdt / plan.leverage
                         if plan.leverage else 0.0)
        logger.info(
            "symbol_decision symbol={} PLANNED direction={} entry={:.4f} "
            "sl={:.4f} tp={:.4f} rr={:.2f} confluence={:.2f} "
            "contracts={} notional={:.2f} lev={}x margin={:.2f} "
            "risk={:.2f} risk_bal={:.2f} margin_bal={:.2f} factors={}",
            symbol, plan.direction.value, plan.entry_price, plan.sl_price,
            plan.tp_price, plan.rr_ratio, plan.confluence_score,
            plan.num_contracts, plan.position_size_usdt, plan.leverage,
            margin_locked, plan.risk_amount_usdt, risk_balance, margin_balance,
            ",".join(plan.confluence_factors) or "-",
        )

        # Reentry gate (Madde C): per-side cooldown + ATR move + quality.
        gate_side = _direction_to_pos_side(plan.direction)
        gate_allowed, gate_reason = self._check_reentry_gate(
            symbol, gate_side,
            proposed_confluence=int(plan.confluence_score),
            current_price=float(state.signal_table.price or plan.entry_price),
            atr=float(state.atr or 0.0),
            now=_utc_now(),
        )
        if not gate_allowed:
            logger.info("reentry_blocked symbol={} side={} reason={}",
                        symbol, gate_side, gate_reason)
            return

        allowed, reason = self.ctx.risk_mgr.can_trade(plan)
        if not allowed:
            logger.info("blocked symbol={} reason={}", symbol, reason)
            return

        # 5. Place order. Phase 7.C4: zone-entry path places a limit order
        # at a structural zone and registers a pending; fill processing
        # runs in `_process_pending` on a later cycle. Legacy market path
        # remains the default (fallback when zone-entry disabled or no
        # setup is available and `zone_require_setup=False`).
        pos_side = _direction_to_pos_side(plan.direction)
        if cfg.execution.zone_entry_enabled:
            placed = await self._try_place_zone_entry(
                symbol=symbol,
                pos_side=pos_side,
                plan=plan,
                state=state,
                candles=candles,
                trend_regime=trend_regime,
            )
            if placed:
                return  # wait for fill event
            if cfg.execution.zone_require_setup:
                logger.info(
                    "symbol_decision symbol={} NO_TRADE reason=no_setup_zone "
                    "direction={}", symbol, plan.direction.value,
                )
                try:
                    conf = calculate_confluence(
                        state,
                        ltf_candles=candles,
                        allowed_sessions=cfg.allowed_sessions_for(symbol) or None,
                        ltf_state=self.ctx.ltf_cache.get(symbol),
                        weights=cfg.analysis.confluence_weights or None,
                        min_rsi_mfi_magnitude=cfg.analysis.min_rsi_mfi_magnitude,
                        liquidity_pool_max_atr_dist=cfg.analysis.liquidity_pool_max_atr_dist,
                        displacement_atr_mult=cfg.analysis.displacement_atr_mult,
                        displacement_max_bars_ago=cfg.analysis.displacement_max_bars_ago,
                        divergence_fresh_bars=cfg.analysis.divergence_fresh_bars,
                        divergence_decay_bars=cfg.analysis.divergence_decay_bars,
                        divergence_max_bars=cfg.analysis.divergence_max_bars,
                        trend_regime=trend_regime,
                        trend_regime_conditional_scoring_enabled=
                            cfg.analysis.trend_regime_conditional_scoring_enabled,
                        daily_bias_enabled=(
                            cfg.on_chain.enabled
                            and cfg.on_chain.daily_bias_enabled
                        ),
                        daily_bias_delta=cfg.on_chain.daily_bias_modifier_delta,
                    )
                    await self._record_reject(
                        symbol=symbol, reject_reason="no_setup_zone",
                        state=state, conf=conf, candles=candles,
                    )
                except Exception:
                    logger.debug("no_setup_zone_reject_log_failed symbol={}", symbol)
                return
            # else: fall through to legacy market path

        try:
            report = await asyncio.to_thread(self.ctx.router.place, plan, symbol)
        except AlgoOrderError as exc:
            logger.error("algo_failure_position_auto_closed symbol={}: {}", symbol, exc)
            return
        except (LeverageSetError, OrderRejected, InsufficientMargin, ValueError) as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.error("order_rejected symbol={}: {} | code={} | payload={}",
                         symbol, exc, code, payload)
            return
        except Exception:
            logger.exception("order_unexpected_error symbol={}", symbol)
            return

        # 6. In-memory FIRST — can't meaningfully fail; keeps us honest even
        # if the journal write below errors out.
        algo_ids = [a.algo_id for a in report.algos if a.algo_id]
        runner_size = _runner_size(plan.num_contracts, cfg)
        self.ctx.monitor.register_open(
            symbol, pos_side, float(plan.num_contracts), plan.entry_price,
            algo_ids=algo_ids, tp2_price=plan.tp_price,
            sl_price=plan.sl_price, runner_size=runner_size,
            plan_sl_price=plan.sl_price,
        )
        self.ctx.risk_mgr.register_trade_opened()

        # 7. Persist to journal. Failure here leaves an orphan we'll see at
        # next startup via _reconcile_orphans(); do not undo the live position.
        try:
            rec = await self.ctx.journal.record_open(
                plan, report,
                symbol=symbol,
                signal_timestamp=_utc_now(),
                entry_timeframe=cfg.trading.entry_timeframe,
                htf_timeframe=cfg.trading.htf_timeframe,
                htf_bias=_bias_str(state),
                session=_session_str(state),
                market_structure=_structure_str(state),
                trend_regime_at_entry=(
                    trend_regime.value
                    if trend_regime and trend_regime != TrendRegime.UNKNOWN
                    else None
                ),
                on_chain_context=self._on_chain_context_dict(),
                confluence_pillar_scores=dict(plan.confluence_pillar_scores or {}),
                oscillator_raw_values=self._build_oscillator_raw_values(
                    symbol, state,
                ),
                **_derive_enrichment(
                    state,
                    candles=candles,
                    entry_tf_minutes=_timeframe_to_minutes(cfg.trading.entry_timeframe),
                ),
            )
            self.ctx.open_trade_ids[(symbol, pos_side)] = rec.trade_id
            self.ctx.open_trade_opened_at[(symbol, pos_side)] = _utc_now()
            logger.info("opened {} {} {}c @ {} trade_id={}",
                        plan.direction.value, symbol, plan.num_contracts,
                        plan.entry_price, rec.trade_id)
        except Exception:
            logger.exception("journal_write_failed_live_position_orphaned symbol={}",
                             symbol)

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _prime(self) -> None:
        await self.ctx.journal.replay_for_risk_manager(
            self.ctx.risk_mgr,
            since=self.ctx.config.rl_clean_since(),
        )
        if self.clear_halt:
            self._apply_clear_halt()
        # Bybit V5 prerequisite: hedge mode must be enabled for USDT linear
        # before the bot places its first order with positionIdx=1/2 — the
        # UTA default is one-way mode, which would reject every order with
        # retCode 110017. Idempotent: re-applying when already-hedge returns
        # 110025 which the client swallows. Best-effort: a transient failure
        # at startup logs but does not abort — the next entry attempt will
        # surface the underlying mode mismatch (and fail loudly) rather than
        # hide it behind a startup exception.
        try:
            await asyncio.to_thread(
                self.ctx.bybit_client.set_position_mode_hedge,
            )
            logger.info("bybit_position_mode_hedge_set category=linear coin=USDT")
        except Exception:
            logger.exception(
                "bybit_position_mode_hedge_set_failed — continuing; first "
                "entry will surface the underlying mode if still in one-way",
            )
        # Reconcile BEFORE rehydrate: the orphan-pending-limit sweep wipes
        # every resting limit on Bybit (including any pre-restart TP limits),
        # so we must let it run first — then rehydrate re-places fresh TP
        # limits for each tracked position as it rebuilds monitor state.
        await self._reconcile_orphans()
        await self._rehydrate_open_positions()
        await self._load_contract_sizes()

    def _apply_clear_halt(self) -> None:
        """Operator override (--clear-halt): wipe halt + daily counters + peak
        that the journal replay rebuilt. Three resets are needed because each
        breaker has its own state:
          * halted_until/reason  — daily-loss / consecutive-loss cooldown
          * daily_realized_pnl + day_start_balance — without this the next
            loss after restart re-trips the same daily-loss threshold
          * consecutive_losses   — same logic for the streak breaker
          * peak_balance         — max_drawdown is "manual restart required";
            without re-anchoring peak to current_balance the bot stays
            permanently halted as soon as drawdown_pct ≥ max_drawdown_pct
        """
        rm = self.ctx.risk_mgr
        prev_until = rm.halted_until
        prev_reason = rm.halt_reason
        prev_daily = rm.daily_realized_pnl
        prev_streak = rm.consecutive_losses
        prev_peak = rm.peak_balance
        prev_dd = rm.drawdown_pct
        rm.clear_halt()
        rm.daily_realized_pnl = 0.0
        rm.day_start_balance = rm.current_balance
        rm.consecutive_losses = 0
        rm.peak_balance = rm.current_balance
        logger.warning(
            "clear_halt_applied prev_halt={} prev_reason={!r} "
            "reset_daily_pnl={:.2f} reset_streak={} "
            "reset_peak={:.2f}->{:.2f} prev_dd={:.2f}%",
            prev_until.isoformat() if prev_until else None,
            prev_reason, prev_daily, prev_streak,
            prev_peak, rm.peak_balance, prev_dd,
        )

    async def _load_contract_sizes(self) -> None:
        """Pre-fetch ctVal + max leverage from Bybit instrument-info for every configured symbol.
        Falls back to YAML defaults on error so the bot still runs; logs
        the failure so the operator sees it."""
        cfg = self.ctx.config
        ct_fallback = cfg.trading.contract_size
        lev_fallback = cfg.trading.max_leverage
        for symbol in cfg.trading.symbols:
            try:
                spec = await asyncio.to_thread(
                    self.ctx.bybit_client.get_instrument_spec, symbol)
                ct = float(spec.get("ct_val") or 0.0)
                mx = int(spec.get("max_leverage") or 0)
                self.ctx.contract_sizes[symbol] = ct if ct > 0 else ct_fallback
                self.ctx.max_leverage_per_symbol[symbol] = (
                    mx if mx > 0 else lev_fallback)
                if ct <= 0 or mx <= 0:
                    logger.warning("instrument_spec_partial symbol={} spec={}",
                                   symbol, spec)
            except Exception:
                self.ctx.contract_sizes[symbol] = ct_fallback
                self.ctx.max_leverage_per_symbol[symbol] = lev_fallback
                logger.exception("instrument_spec_failed symbol={}", symbol)
        logger.info(
            "instrument_specs_loaded ctvals={} max_lev={}",
            self.ctx.contract_sizes, self.ctx.max_leverage_per_symbol,
        )

    async def _process_closes(self) -> None:
        try:
            fills, live_snaps = await asyncio.to_thread(self.ctx.monitor.poll)
        except Exception:
            logger.exception("monitor_poll_failed")
            return
        for fill in fills:
            await self._handle_close(fill)
        # 2026-04-26 — cadence-gated intra-trade journal snapshot writer.
        # Reads `live_snaps` from the same poll above (no extra Bybit call).
        # No-op when feature disabled or cadence not yet elapsed.
        await self._maybe_write_position_snapshots(live_snaps)

    async def _maybe_write_position_snapshots(
        self, live_snaps: list,
    ) -> None:
        """Cadence-gated intra-trade journal writer for RL trajectory data.

        For every OPEN position present in `live_snaps`, writes one row to
        `position_snapshots` capturing live mark/PnL, running MFE/MAE in R,
        active SL/TP, lifecycle flags, and drift fields for derivatives /
        on-chain / oscillator / VWAP. All inputs are read from cached state
        (BotContext + monitor._Tracked + per-symbol MarketState cache) —
        zero extra Bybit / TV / Arkham / Coinalyze calls.

        Cadence is bumped once per WRITE WINDOW after the loop, so all
        positions in a tick land on the same captured_at and the next
        window opens cadence_s seconds later.
        """
        cfg = self.ctx.config.journal
        if not cfg.position_snapshot_enabled:
            return
        now_mono = time.monotonic()
        if now_mono - self.ctx.last_position_snapshot_ts < cfg.position_snapshot_cadence_s:
            return
        if not live_snaps:
            return
        snap_arkham = self.ctx.on_chain_snapshot
        deriv_cache = self.ctx.derivatives_cache
        captured_at = _utc_now()
        wrote_any = False
        for snap in live_snaps:
            tracked = self.ctx.monitor.get_tracked(snap.inst_id, snap.pos_side)
            if tracked is None or tracked.plan_sl_price <= 0:
                continue
            trade_id = self.ctx.open_trade_ids.get((snap.inst_id, snap.pos_side))
            if not trade_id:
                continue
            sl_dist = abs(tracked.entry_price - tracked.plan_sl_price)
            if sl_dist <= 0:
                continue
            sign = 1.0 if snap.pos_side == "long" else -1.0
            r_now = sign * (snap.mark_price - tracked.entry_price) / sl_dist
            deriv = deriv_cache.get(snap.inst_id) if deriv_cache is not None else None
            funding_now = oi_now = ls_now = long_liq_now = short_liq_now = None
            if deriv is not None:
                funding_now = float(deriv.funding_rate_current)
                oi_now = float(deriv.open_interest_usd) if deriv.open_interest_usd else None
                ls_now = float(deriv.long_short_ratio) if deriv.long_short_ratio else None
                long_liq_now = float(deriv.long_liq_notional_1h) if deriv.long_liq_notional_1h else None
                short_liq_now = float(deriv.short_liq_notional_1h) if deriv.short_liq_notional_1h else None
            btc_netflow_now = stable_pulse_now = flow_align_now = None
            if snap_arkham is not None:
                btc_netflow_now = snap_arkham.cex_btc_netflow_24h_usd
                stable_pulse_now = snap_arkham.stablecoin_pulse_1h_usd
                flow_align_now = _flow_alignment_score(
                    stablecoin_pulse_1h_usd=snap_arkham.stablecoin_pulse_1h_usd,
                    btc_netflow_24h_usd=snap_arkham.cex_btc_netflow_24h_usd,
                    eth_netflow_24h_usd=snap_arkham.cex_eth_netflow_24h_usd,
                    coinbase_netflow_24h_usd=snap_arkham.cex_coinbase_netflow_24h_usd,
                    binance_netflow_24h_usd=snap_arkham.cex_binance_netflow_24h_usd,
                    bybit_netflow_24h_usd=snap_arkham.cex_bybit_netflow_24h_usd,
                )
            mstate = self.ctx.last_market_state_per_symbol.get(snap.inst_id)
            osc_3m_json: dict = {}
            vwap_3m_dist_atr = None
            if mstate is not None:
                try:
                    osc_3m_json = mstate.oscillator.model_dump()
                except Exception:
                    osc_3m_json = {}
                atr = mstate.atr or 0.0
                # 2026-04-27 (F4) — primary is the VWAP centerline distance:
                # `signal_table.vwap_3m` is populated reliably whenever the
                # bot is in 3m TF pass (used by zone builder + setup
                # planner). The ±1σ band fields go NULL ("—" → 0.0) for
                # the first few bars after Pine's daily VWAP reset (UTC
                # 00:00) when session-stdev is still too young, which
                # accounts for the 713/713 NULL pre-fix coverage. We keep
                # the band-midpoint path as a redundant secondary in case
                # `vwap_3m` itself is somehow unset; semantically band_mid
                # == centerline, so the result is identical when both are
                # available.
                vwap_3m = mstate.signal_table.vwap_3m
                if atr > 0 and vwap_3m > 0:
                    vwap_3m_dist_atr = (snap.mark_price - vwap_3m) / atr
                else:
                    upper = mstate.signal_table.vwap_3m_upper
                    lower = mstate.signal_table.vwap_3m_lower
                    if atr > 0 and upper > 0 and lower > 0:
                        band_mid = (upper + lower) / 2.0
                        vwap_3m_dist_atr = (snap.mark_price - band_mid) / atr
            try:
                await self.ctx.journal.record_position_snapshot(
                    trade_id=trade_id,
                    captured_at=captured_at,
                    mark_price=float(snap.mark_price),
                    unrealized_pnl_usdt=float(snap.unrealized_pnl),
                    unrealized_pnl_r=float(r_now),
                    mfe_r_so_far=float(tracked.mfe_r_high),
                    mae_r_so_far=float(tracked.mae_r_low),
                    current_sl_price=float(tracked.sl_price or tracked.plan_sl_price),
                    current_tp_price=float(tracked.tp2_price) if tracked.tp2_price else None,
                    sl_to_be_moved=bool(tracked.be_already_moved),
                    mfe_lock_applied=bool(tracked.sl_lock_applied),
                    derivatives_funding_now=funding_now,
                    derivatives_oi_now_usd=oi_now,
                    derivatives_ls_ratio_now=ls_now,
                    derivatives_long_liq_1h_now=long_liq_now,
                    derivatives_short_liq_1h_now=short_liq_now,
                    on_chain_btc_netflow_now_usd=btc_netflow_now,
                    on_chain_stablecoin_pulse_now=stable_pulse_now,
                    on_chain_flow_alignment_now=flow_align_now,
                    oscillator_3m_now_json=osc_3m_json,
                    vwap_3m_distance_atr_now=vwap_3m_dist_atr,
                )
                wrote_any = True
            except Exception:
                logger.exception(
                    "position_snapshot_write_failed inst={} side={}",
                    snap.inst_id, snap.pos_side,
                )
        if wrote_any:
            self.ctx.last_position_snapshot_ts = now_mono

    # ── Arkham on-chain snapshot scheduler (Phase B) ────────────────────────

    async def _refresh_on_chain_snapshots(self) -> None:
        """Refresh daily + hourly Arkham snapshots on their own cadences.

        Contract:
          * master `on_chain.enabled=false` → no-op, `on_chain_snapshot`
            stays whatever it was (expected None).
          * `arkham_client is None` (master flag flipped on but client
            not built, or hard-disabled at 95% label usage) → no-op.
          * daily-bundle (bias + BTC/ETH 24h netflow + per-entity
            Coinbase/Binance/Bybit 24h netflow) refreshes on
            `daily_snapshot_refresh_s` cadence (default 300s). Was once-
            per-UTC-day pre-2026-04-23; flipped to monotonic so
            `on_chain_snapshots` rows replace frozen values intraday.
          * hourly pulse refreshes when `now_monotonic -
            last_on_chain_pulse_ts >= refresh_s` (default 3600s).
          * Every fetch is wrapped in a broad try/except — Arkham outage
            can never crash the tick. Last-known snapshot stays cached
            through an outage; `fresh` flag falls through to False once
            `snapshot_age_s` exceeds the staleness threshold.
        """
        cfg = self.ctx.config
        if not cfg.on_chain.enabled:
            return
        client = self.ctx.arkham_client
        if client is None:
            return
        if getattr(client, "hard_disabled", False):
            return

        now_mono_daily = time.monotonic()
        daily_refresh_s = float(cfg.on_chain.daily_snapshot_refresh_s)
        daily_elapsed = now_mono_daily - self.ctx.last_on_chain_daily_ts
        # Daily bundle — cadence-gated (2026-04-23). Was UTC-day-gated;
        # flipped so DB rows refresh intraday (see config docstring).
        if daily_elapsed >= daily_refresh_s:
            try:
                snap = await fetch_daily_snapshot(
                    client,
                    stablecoin_threshold_usd=(
                        cfg.on_chain.daily_bias_stablecoin_threshold_usd),
                    btc_netflow_threshold_usd=(
                        cfg.on_chain.daily_bias_btc_netflow_threshold_usd),
                    stale_threshold_s=(
                        cfg.on_chain.snapshot_staleness_threshold_s),
                    snapshot_age_s=0,
                )
            except Exception:
                logger.exception("arkham_daily_snapshot_failed")
                snap = None
            if snap is not None:
                # 2026-04-22 — alongside the bias fetch, also pull
                # per-entity 24h netflow for Coinbase + Binance + Bybit.
                # 2026-04-23 fix — switched from `/flow/entity/{entity}`
                # (daily buckets, froze at UTC day close) to
                # `/transfers/histogram` with `base=<entity>&granularity=1h`
                # → true rolling 24h. Per-entity failures are isolated.
                if cfg.on_chain.entity_netflow_enabled:
                    # 2026-04-23 (night-late) — bitfinex + kraken added as
                    # journal-only 4th + 5th venues. Live probe vs.
                    # `type:cex` aggregate showed the original 3 captured
                    # only ~1-6% of the full CEX BTC netflow signal; these
                    # two were the biggest named single inflow / outflow.
                    # Not wired into _flow_alignment_score yet (Pass 3).
                    # 2026-04-24 — OKX added as 6th venue (journal-only).
                    # Bot trades on OKX so its own netflow is a natural
                    # self-signal even though 24h net ≈ 0 (balanced,
                    # $1.86B turnover with $58M max hourly |net|).
                    for entity in ("coinbase", "binance", "bybit",
                                   "bitfinex", "kraken", "okx"):
                        try:
                            await asyncio.sleep(1.1)  # 1 req/s rate cushion
                            value = await fetch_entity_netflow_24h(client, entity)
                        except Exception:
                            logger.exception(
                                "arkham_entity_netflow_failed entity={}", entity)
                            value = None
                        if value is not None:
                            setattr(self.ctx,
                                    f"cex_{entity}_netflow_24h_usd", value)
                            logger.info(
                                "arkham_entity_netflow_refreshed "
                                "entity={} netflow_24h_usd={:.2f}",
                                entity, float(value),
                            )

                    # 2026-04-26 — per-venue × per-asset (BTC / ETH / stables)
                    # 24h netflow. 6 venues × 3 assets × 2 flow = 36 histogram
                    # calls × 1.1s rate cushion ≈ 40-60s. Run as a fire-and-
                    # forget task so the trade cycle never blocks. Result lands
                    # on `ctx.cex_per_venue_<asset>_netflow_24h_usd` dicts; the
                    # NEXT daily-bundle / pulse / token-volume snapshot rebuild
                    # picks the values up via `_dump_per_venue_dict`.
                    self._kick_per_venue_per_asset_refresh(client)

                # Preserve any live stablecoin-pulse + per-entity netflows
                # + token volume JSON we already carry — the daily build
                # returns None for those, we patch them back in from cache.
                self.ctx.on_chain_snapshot = OnChainSnapshot(
                    daily_macro_bias=snap.daily_macro_bias,
                    stablecoin_pulse_1h_usd=self.ctx.stablecoin_pulse_1h_usd,
                    cex_btc_netflow_24h_usd=snap.cex_btc_netflow_24h_usd,
                    cex_eth_netflow_24h_usd=snap.cex_eth_netflow_24h_usd,
                    coinbase_asia_skew_usd=snap.coinbase_asia_skew_usd,
                    bnb_self_flow_24h_usd=snap.bnb_self_flow_24h_usd,
                    cex_coinbase_netflow_24h_usd=self.ctx.cex_coinbase_netflow_24h_usd,
                    cex_binance_netflow_24h_usd=self.ctx.cex_binance_netflow_24h_usd,
                    cex_bybit_netflow_24h_usd=self.ctx.cex_bybit_netflow_24h_usd,
                    cex_bitfinex_netflow_24h_usd=self.ctx.cex_bitfinex_netflow_24h_usd,
                    cex_kraken_netflow_24h_usd=self.ctx.cex_kraken_netflow_24h_usd,
                    cex_okx_netflow_24h_usd=self.ctx.cex_okx_netflow_24h_usd,
                    cex_per_venue_btc_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_btc_netflow_24h_usd),
                    cex_per_venue_eth_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_eth_netflow_24h_usd),
                    cex_per_venue_stables_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_stables_netflow_24h_usd),
                    token_volume_1h_net_usd_json=self.ctx.token_volume_1h_net_usd_json,
                    snapshot_age_s=0,
                    stale_threshold_s=snap.stale_threshold_s,
                )
                self.ctx.last_on_chain_daily_ts = now_mono_daily
                logger.info(
                    "arkham_daily_snapshot_refreshed bias={} "
                    "btc_netflow={} eth_netflow={}",
                    snap.daily_macro_bias,
                    snap.cex_btc_netflow_24h_usd,
                    snap.cex_eth_netflow_24h_usd,
                )
            # else: leave last-known snapshot in place; caller sees
            # stale snapshot flagged via `.fresh`.

        # Altcoin index — hourly scalar refresh (Phase F2). Fires on
        # its own cadence so a pulse failure doesn't starve the index
        # update and vice versa.
        now_mono_aci = time.monotonic()
        if cfg.on_chain.altcoin_index_enabled:
            aci_refresh_s = float(cfg.on_chain.altcoin_index_refresh_s)
            aci_elapsed = now_mono_aci - self.ctx.last_altcoin_index_ts
            if aci_elapsed >= aci_refresh_s:
                try:
                    aci = await client.get_altcoin_index()
                except Exception:
                    logger.exception("arkham_altcoin_index_fetch_failed")
                    aci = None
                if aci is not None:
                    self.ctx.altcoin_index_value = aci
                    self.ctx.last_altcoin_index_ts = now_mono_aci
                    logger.info("arkham_altcoin_index_refreshed value={}", aci)

        # Hourly stablecoin pulse — `refresh_s` cadence.
        now_mono = time.monotonic()
        refresh_s = float(cfg.on_chain.stablecoin_pulse_refresh_s)
        elapsed = now_mono - self.ctx.last_on_chain_pulse_ts
        if elapsed >= refresh_s:
            try:
                pulse = await fetch_hourly_stablecoin_pulse(client)
            except Exception:
                logger.exception("arkham_pulse_fetch_failed")
                pulse = None
            if pulse is not None:
                self.ctx.stablecoin_pulse_1h_usd = pulse
                self.ctx.last_on_chain_pulse_ts = now_mono
                # Patch the cached daily snapshot so downstream reads
                # see a consistent pulse value without a second daily
                # refresh.
                prev = self.ctx.on_chain_snapshot
                if prev is not None:
                    self.ctx.on_chain_snapshot = OnChainSnapshot(
                        daily_macro_bias=prev.daily_macro_bias,
                        stablecoin_pulse_1h_usd=pulse,
                        cex_btc_netflow_24h_usd=prev.cex_btc_netflow_24h_usd,
                        cex_eth_netflow_24h_usd=prev.cex_eth_netflow_24h_usd,
                        coinbase_asia_skew_usd=prev.coinbase_asia_skew_usd,
                        bnb_self_flow_24h_usd=prev.bnb_self_flow_24h_usd,
                        cex_coinbase_netflow_24h_usd=prev.cex_coinbase_netflow_24h_usd,
                        cex_binance_netflow_24h_usd=prev.cex_binance_netflow_24h_usd,
                        cex_bybit_netflow_24h_usd=prev.cex_bybit_netflow_24h_usd,
                        cex_bitfinex_netflow_24h_usd=prev.cex_bitfinex_netflow_24h_usd,
                        cex_kraken_netflow_24h_usd=prev.cex_kraken_netflow_24h_usd,
                        cex_okx_netflow_24h_usd=prev.cex_okx_netflow_24h_usd,
                        cex_per_venue_btc_netflow_24h_usd_json=prev.cex_per_venue_btc_netflow_24h_usd_json,
                        cex_per_venue_eth_netflow_24h_usd_json=prev.cex_per_venue_eth_netflow_24h_usd_json,
                        cex_per_venue_stables_netflow_24h_usd_json=prev.cex_per_venue_stables_netflow_24h_usd_json,
                        token_volume_1h_net_usd_json=prev.token_volume_1h_net_usd_json,
                        snapshot_age_s=prev.snapshot_age_s,
                        stale_threshold_s=prev.stale_threshold_s,
                    )
                logger.info(
                    "arkham_stablecoin_pulse_refreshed pulse_usd={:.2f}",
                    float(pulse),
                )

        # 2026-04-22 — per-symbol token volume (`/token/volume/{id}`
        # granularity=1h). Hourly cadence matches data granularity.
        # Probe-confirmed 0 label cost. Per-symbol failures isolated.
        if cfg.on_chain.token_volume_enabled:
            tv_refresh_s = float(cfg.on_chain.token_volume_refresh_s)
            tv_elapsed = now_mono - self.ctx.last_token_volume_ts
            if tv_elapsed >= tv_refresh_s:
                volumes: dict[str, float] = {}
                # Use whatever symbols are configured for this run +
                # are also in the slug map. Unknown symbols silently skipped.
                watched = list(getattr(cfg.trading, "symbols", []) or [])
                for sym in watched:
                    token_id = WATCHED_SYMBOL_TO_TOKEN_ID.get(sym)
                    if token_id is None:
                        continue
                    try:
                        await asyncio.sleep(1.1)  # 1 req/s rate cushion
                        v = await fetch_token_volume_last_hour(client, token_id)
                    except Exception:
                        logger.exception(
                            "arkham_token_volume_failed sym={} token={}",
                            sym, token_id,
                        )
                        v = None
                    if v is not None:
                        volumes[sym] = float(v)
                if volumes:
                    self.ctx.token_volume_1h_net_usd_json = json.dumps(volumes)
                    self.ctx.last_token_volume_ts = now_mono
                    # Patch cached snapshot so the next dedup tick sees the
                    # new value without waiting on another daily/pulse fetch.
                    prev = self.ctx.on_chain_snapshot
                    if prev is not None:
                        self.ctx.on_chain_snapshot = OnChainSnapshot(
                            daily_macro_bias=prev.daily_macro_bias,
                            stablecoin_pulse_1h_usd=prev.stablecoin_pulse_1h_usd,
                            cex_btc_netflow_24h_usd=prev.cex_btc_netflow_24h_usd,
                            cex_eth_netflow_24h_usd=prev.cex_eth_netflow_24h_usd,
                            coinbase_asia_skew_usd=prev.coinbase_asia_skew_usd,
                            bnb_self_flow_24h_usd=prev.bnb_self_flow_24h_usd,
                            cex_coinbase_netflow_24h_usd=prev.cex_coinbase_netflow_24h_usd,
                            cex_binance_netflow_24h_usd=prev.cex_binance_netflow_24h_usd,
                            cex_bybit_netflow_24h_usd=prev.cex_bybit_netflow_24h_usd,
                            cex_bitfinex_netflow_24h_usd=prev.cex_bitfinex_netflow_24h_usd,
                            cex_kraken_netflow_24h_usd=prev.cex_kraken_netflow_24h_usd,
                            cex_okx_netflow_24h_usd=prev.cex_okx_netflow_24h_usd,
                            cex_per_venue_btc_netflow_24h_usd_json=prev.cex_per_venue_btc_netflow_24h_usd_json,
                            cex_per_venue_eth_netflow_24h_usd_json=prev.cex_per_venue_eth_netflow_24h_usd_json,
                            cex_per_venue_stables_netflow_24h_usd_json=prev.cex_per_venue_stables_netflow_24h_usd_json,
                            token_volume_1h_net_usd_json=self.ctx.token_volume_1h_net_usd_json,
                            snapshot_age_s=prev.snapshot_age_s,
                            stale_threshold_s=prev.stale_threshold_s,
                        )
                    logger.info(
                        "arkham_token_volume_refreshed symbols={} sample={}",
                        list(volumes.keys()),
                        {k: f"{v:.2f}" for k, v in list(volumes.items())[:2]},
                    )

        # Time-series journal row — appends only when the composite
        # fingerprint actually changes. Cadence thus matches Arkham's
        # own refresh rhythm (≈ hourly pulse + hourly altcoin-index +
        # once-per-UTC-day bias) rather than the much faster tick loop.
        await self._maybe_record_on_chain_snapshot()

    def _kick_per_venue_per_asset_refresh(self, client: ArkhamClient) -> None:
        """Fire-and-forget the 36-call per-venue × per-asset netflow refresh.

        Skips if a previous task is still alive (avoids stacking concurrent
        fetchers when daily-bundle fires before the previous job completes).
        Result lands on `ctx.cex_per_venue_<asset>_netflow_24h_usd` dicts;
        the next snapshot rebuild path serialises them via
        `_dump_per_venue_dict` so the journal row carries fresh JSON.
        """
        prev_task = getattr(self.ctx, "per_venue_per_asset_task", None)
        if prev_task is not None and not prev_task.done():
            logger.info("arkham_per_venue_per_asset_skip_prev_inflight")
            return
        loop = asyncio.get_event_loop()
        self.ctx.per_venue_per_asset_task = loop.create_task(
            self._refresh_per_venue_per_asset(client)
        )

    async def _refresh_per_venue_per_asset(self, client: ArkhamClient) -> None:
        """Loop 6 venues × 3 assets, fetch 24h netflow, update ctx dicts.

        ~36 histogram calls × 1.1s rate cushion ≈ 40-60s. Per-(venue, asset)
        failures isolated — one fetch raising doesn't taint the rest.
        Label-free endpoint (probed 2026-04-23 night).
        """
        venues = ("coinbase", "binance", "bybit", "bitfinex", "kraken", "okx")
        assets = (
            ("btc", ["bitcoin"]),
            ("eth", ["ethereum"]),
            ("stables", ["tether", "usd-coin"]),
        )
        logger.info("arkham_per_venue_per_asset_refresh_started")
        try:
            for asset_key, token_ids in assets:
                target_dict: dict[str, Optional[float]] = getattr(
                    self.ctx, f"cex_per_venue_{asset_key}_netflow_24h_usd")
                for venue in venues:
                    try:
                        value = await fetch_entity_per_asset_netflow_24h(
                            client, venue, token_ids,
                        )
                    except Exception:
                        logger.exception(
                            "arkham_per_venue_per_asset_failed "
                            "venue={} asset={}", venue, asset_key,
                        )
                        value = None
                    target_dict[venue] = value
            self.ctx.last_per_venue_per_asset_ts = time.monotonic()
            # Patch the cached snapshot so the next `_maybe_record_on_chain_snapshot`
            # tick (every cycle ≈ 30s) fingerprint-mutates and writes a journal row
            # WITH the freshly-populated per-asset JSON. Without this patch the
            # snapshot would only pick the dicts up at the next daily-bundle
            # iteration (5min cadence), so the dashboard 24h slice would stay
            # empty for ~5min after every restart. Mirrors the pulse +
            # token-volume refresh patch pattern.
            prev = self.ctx.on_chain_snapshot
            if prev is not None:
                self.ctx.on_chain_snapshot = OnChainSnapshot(
                    daily_macro_bias=prev.daily_macro_bias,
                    stablecoin_pulse_1h_usd=prev.stablecoin_pulse_1h_usd,
                    cex_btc_netflow_24h_usd=prev.cex_btc_netflow_24h_usd,
                    cex_eth_netflow_24h_usd=prev.cex_eth_netflow_24h_usd,
                    coinbase_asia_skew_usd=prev.coinbase_asia_skew_usd,
                    bnb_self_flow_24h_usd=prev.bnb_self_flow_24h_usd,
                    cex_coinbase_netflow_24h_usd=prev.cex_coinbase_netflow_24h_usd,
                    cex_binance_netflow_24h_usd=prev.cex_binance_netflow_24h_usd,
                    cex_bybit_netflow_24h_usd=prev.cex_bybit_netflow_24h_usd,
                    cex_bitfinex_netflow_24h_usd=prev.cex_bitfinex_netflow_24h_usd,
                    cex_kraken_netflow_24h_usd=prev.cex_kraken_netflow_24h_usd,
                    cex_okx_netflow_24h_usd=prev.cex_okx_netflow_24h_usd,
                    cex_per_venue_btc_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_btc_netflow_24h_usd),
                    cex_per_venue_eth_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_eth_netflow_24h_usd),
                    cex_per_venue_stables_netflow_24h_usd_json=_dump_per_venue_dict(
                        self.ctx.cex_per_venue_stables_netflow_24h_usd),
                    token_volume_1h_net_usd_json=prev.token_volume_1h_net_usd_json,
                    snapshot_age_s=prev.snapshot_age_s,
                    stale_threshold_s=prev.stale_threshold_s,
                )
            logger.info(
                "arkham_per_venue_per_asset_refresh_finished "
                "btc_venues={} eth_venues={} stables_venues={}",
                len([v for v in self.ctx.cex_per_venue_btc_netflow_24h_usd.values() if v is not None]),
                len([v for v in self.ctx.cex_per_venue_eth_netflow_24h_usd.values() if v is not None]),
                len([v for v in self.ctx.cex_per_venue_stables_netflow_24h_usd.values() if v is not None]),
            )
        except Exception:
            logger.exception("arkham_per_venue_per_asset_refresh_crashed")

    async def _maybe_record_on_chain_snapshot(self) -> None:
        """Append one row to `on_chain_snapshots` when Arkham state mutates.

        Fingerprint-gated: a tuple of content-only fields (age excluded so
        no-op ticks don't churn rows) is compared against the last-written
        fingerprint on `ctx.last_on_chain_snapshot_fingerprint`. Match →
        skip; differ → write + update fingerprint. Phase 9 will join this
        table onto `trades` via `entry_timestamp <= captured_at <=
        exit_timestamp` to reconstruct the on-chain regime each trade
        lived through.

        Failures log + swallow — a journal hiccup must never crash the
        tick.
        """
        cfg = self.ctx.config
        if not cfg.on_chain.enabled:
            return
        snap = self.ctx.on_chain_snapshot
        if snap is None:
            return
        journal = getattr(self.ctx, "journal", None)
        if journal is None:
            return

        blackout = self.ctx.whale_blackout_state
        blackout_active = False
        if blackout is not None and blackout.blackouts:
            now_ms = int(_utc_now().timestamp() * 1000)
            blackout_active = any(
                v > now_ms for v in blackout.blackouts.values()
            )

        fp = (
            snap.daily_macro_bias,
            snap.stablecoin_pulse_1h_usd,
            snap.cex_btc_netflow_24h_usd,
            snap.cex_eth_netflow_24h_usd,
            snap.coinbase_asia_skew_usd,
            snap.bnb_self_flow_24h_usd,
            self.ctx.altcoin_index_value,
            bool(snap.fresh),
            blackout_active,
            # 2026-04-22 — entity netflows + per-symbol token volume.
            snap.cex_coinbase_netflow_24h_usd,
            snap.cex_binance_netflow_24h_usd,
            snap.cex_bybit_netflow_24h_usd,
            snap.token_volume_1h_net_usd_json,
            # 2026-04-23 (night-late) — bitfinex + kraken added to fingerprint
            # so any change in these two triggers a fresh journal row.
            # 2026-04-24 — okx added to fingerprint, same rationale.
            snap.cex_bitfinex_netflow_24h_usd,
            snap.cex_kraken_netflow_24h_usd,
            snap.cex_okx_netflow_24h_usd,
            # 2026-04-26 — per-venue × per-asset JSON dicts. Background
            # task mutation triggers a fresh journal row so the dashboard
            # 24h slice has actual time-series, not flat-line.
            snap.cex_per_venue_btc_netflow_24h_usd_json,
            snap.cex_per_venue_eth_netflow_24h_usd_json,
            snap.cex_per_venue_stables_netflow_24h_usd_json,
        )
        if self.ctx.last_on_chain_snapshot_fingerprint == fp:
            return

        try:
            await journal.record_on_chain_snapshot(
                captured_at=_utc_now(),
                daily_macro_bias=snap.daily_macro_bias,
                stablecoin_pulse_1h_usd=snap.stablecoin_pulse_1h_usd,
                cex_btc_netflow_24h_usd=snap.cex_btc_netflow_24h_usd,
                cex_eth_netflow_24h_usd=snap.cex_eth_netflow_24h_usd,
                coinbase_asia_skew_usd=snap.coinbase_asia_skew_usd,
                bnb_self_flow_24h_usd=snap.bnb_self_flow_24h_usd,
                altcoin_index=self.ctx.altcoin_index_value,
                snapshot_age_s=int(snap.snapshot_age_s),
                fresh=bool(snap.fresh),
                whale_blackout_active=blackout_active,
                cex_coinbase_netflow_24h_usd=snap.cex_coinbase_netflow_24h_usd,
                cex_binance_netflow_24h_usd=snap.cex_binance_netflow_24h_usd,
                cex_bybit_netflow_24h_usd=snap.cex_bybit_netflow_24h_usd,
                token_volume_1h_net_usd_json=snap.token_volume_1h_net_usd_json,
                cex_bitfinex_netflow_24h_usd=snap.cex_bitfinex_netflow_24h_usd,
                cex_kraken_netflow_24h_usd=snap.cex_kraken_netflow_24h_usd,
                cex_okx_netflow_24h_usd=snap.cex_okx_netflow_24h_usd,
                cex_per_venue_btc_netflow_24h_usd_json=snap.cex_per_venue_btc_netflow_24h_usd_json,
                cex_per_venue_eth_netflow_24h_usd_json=snap.cex_per_venue_eth_netflow_24h_usd_json,
                cex_per_venue_stables_netflow_24h_usd_json=snap.cex_per_venue_stables_netflow_24h_usd_json,
            )
            self.ctx.last_on_chain_snapshot_fingerprint = fp
        except Exception:
            logger.exception("arkham_snapshot_journal_failed")

    def _on_chain_context_dict(self) -> Optional[dict]:
        """Build the dict that gets JSON-serialised into
        `trades.on_chain_context` / `rejected_signals.on_chain_context`.

        Returns None when the master flag is off or no snapshot is
        cached — journal column writes NULL in that case, matching the
        pre-Arkham row shape exactly. When populated, the dict carries
        scalar fields only so downstream tooling can index by name
        without a schema contract.
        """
        cfg = self.ctx.config
        if not cfg.on_chain.enabled:
            return None
        snap = self.ctx.on_chain_snapshot
        if snap is None:
            return None
        blackout = self.ctx.whale_blackout_state
        blackout_active = False
        if blackout is not None and blackout.blackouts:
            # Summary flag — "any symbol currently blacked out". Per-symbol
            # detail lives in the separate `rejected_signals` reason
            # path; this dict is for cross-row audit, not per-gate trace.
            now_ms = int(_utc_now().timestamp() * 1000)
            blackout_active = any(
                v > now_ms for v in blackout.blackouts.values()
            )
        return {
            "daily_macro_bias": snap.daily_macro_bias,
            "stablecoin_pulse_1h_usd": snap.stablecoin_pulse_1h_usd,
            "cex_btc_netflow_24h_usd": snap.cex_btc_netflow_24h_usd,
            "cex_eth_netflow_24h_usd": snap.cex_eth_netflow_24h_usd,
            "coinbase_asia_skew_usd": snap.coinbase_asia_skew_usd,
            "bnb_self_flow_24h_usd": snap.bnb_self_flow_24h_usd,
            # 2026-04-22 — entity netflow + per-symbol token volume on the
            # entry-time snapshot. Phase 9 GBT can either consume from here
            # (entry-frozen) or join the on_chain_snapshots time-series for
            # mid-trade evolution. Both are valid analytic angles.
            "cex_coinbase_netflow_24h_usd": snap.cex_coinbase_netflow_24h_usd,
            "cex_binance_netflow_24h_usd": snap.cex_binance_netflow_24h_usd,
            "cex_bybit_netflow_24h_usd": snap.cex_bybit_netflow_24h_usd,
            # 2026-04-23 (night-late) — 4th + 5th venues, journal-only.
            "cex_bitfinex_netflow_24h_usd": snap.cex_bitfinex_netflow_24h_usd,
            "cex_kraken_netflow_24h_usd": snap.cex_kraken_netflow_24h_usd,
            # 2026-04-24 — 6th venue (OKX self-signal), journal-only.
            "cex_okx_netflow_24h_usd": snap.cex_okx_netflow_24h_usd,
            # 2026-04-26 — per-venue × per-asset (BTC / ETH / stables).
            # JSON dicts so adding a 7th venue won't change the schema.
            "cex_per_venue_btc_netflow_24h_usd_json": snap.cex_per_venue_btc_netflow_24h_usd_json,
            "cex_per_venue_eth_netflow_24h_usd_json": snap.cex_per_venue_eth_netflow_24h_usd_json,
            "cex_per_venue_stables_netflow_24h_usd_json": snap.cex_per_venue_stables_netflow_24h_usd_json,
            "token_volume_1h_net_usd_json": snap.token_volume_1h_net_usd_json,
            "snapshot_age_s": int(snap.snapshot_age_s),
            "fresh": bool(snap.fresh),
            "whale_blackout_active": blackout_active,
        }

    # ── Pending-entry lifecycle (Phase 7.C4) ────────────────────────────────

    async def _try_place_zone_entry(
        self,
        *,
        symbol: str,
        pos_side: str,
        plan: TradePlan,
        state: MarketState,
        candles: Optional[list] = None,
        trend_regime: Optional[TrendRegime] = None,
    ) -> bool:
        """Try to place a zone-based limit entry. Return True if a pending
        was registered, False otherwise (caller falls back to market path).
        """
        cfg = self.ctx.config
        htf_state = self.ctx.htf_state_cache.get(symbol)
        try:
            zone = build_zone_setup(
                direction=plan.direction,
                state=state,
                htf_state=htf_state,
                heatmap=state.liquidity_heatmap,
                ltf_candles=candles,
                zone_buffer_atr=cfg.execution.zone_buffer_atr,
                sl_buffer_atr=cfg.execution.zone_sl_buffer_atr,
                max_wait_bars=cfg.execution.zone_max_wait_bars,
                default_rr=cfg.execution.zone_default_rr,
                liq_entry_near_max_atr=cfg.execution.liq_entry_near_max_atr,
                liq_entry_magnitude_mult=cfg.execution.liq_entry_magnitude_mult,
                ema21_pullback_enabled=cfg.execution.ema21_pullback_enabled,
                ema_fast_period=cfg.analysis.ema_veto_fast_period,
                ema_slow_period=cfg.analysis.ema_veto_slow_period,
                htf_fvg_entry_enabled=cfg.execution.htf_fvg_entry_enabled,
                tp_ladder_enabled=cfg.execution.tp_ladder_enabled,
                tp_ladder_shares=tuple(cfg.execution.tp_ladder_shares),
                tp_ladder_min_notional_frac=cfg.execution.tp_ladder_min_notional_frac,
            )
        except Exception:
            logger.exception("zone_setup_build_failed symbol={}", symbol)
            return False
        if zone is None:
            logger.info(
                "zone_setup_none symbol={} direction={} — no source available",
                symbol, plan.direction.value,
            )
            return False

        contract_size = self.ctx.contract_sizes.get(
            symbol, cfg.trading.contract_size)
        try:
            zoned_plan = apply_zone_to_plan(
                plan, zone, contract_size,
                min_sl_distance_pct=cfg.min_sl_distance_pct_for(symbol),
                target_rr_cap=cfg.execution.target_rr_ratio,
                vwap_long_anchor=cfg.analysis.vwap_zone_long_anchor,
                vwap_short_anchor=cfg.analysis.vwap_zone_short_anchor,
            )
        except Exception:
            logger.exception(
                "zone_apply_failed symbol={} zone_source={}",
                symbol, zone.zone_source,
            )
            return False

        # Re-gate risk against the re-sized plan (R budget may have shifted
        # slightly with the structural SL).
        allowed, reason = self.ctx.risk_mgr.can_trade(zoned_plan)
        if not allowed:
            logger.info(
                "zone_plan_risk_blocked symbol={} reason={}", symbol, reason,
            )
            return False

        try:
            result = await asyncio.to_thread(
                self.ctx.router.place_limit_entry,
                zoned_plan, zoned_plan.entry_price, symbol,
            )
        except (LeverageSetError, OrderRejected, InsufficientMargin, ValueError) as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.error(
                "zone_limit_rejected symbol={}: {} | code={} | payload={}",
                symbol, exc, code, payload,
            )
            return False
        except Exception:
            logger.exception(
                "zone_limit_unexpected_error symbol={}", symbol,
            )
            return False

        tf_sec = _tf_seconds(cfg.trading.entry_timeframe)
        max_wait_s = float(zone.max_wait_bars * tf_sec)
        placed_at = _utc_now()
        self.ctx.monitor.register_pending(
            inst_id=symbol, pos_side=pos_side, order_id=result.order_id,
            num_contracts=float(zoned_plan.num_contracts),
            entry_px=zoned_plan.entry_price,
            max_wait_s=max_wait_s, placed_at=placed_at,
        )
        self.ctx.pending_setups[(symbol, pos_side)] = PendingSetupMeta(
            plan=zoned_plan,
            zone=zone,
            order_id=result.order_id,
            signal_state=state,
            placed_at=placed_at,
            trend_regime_at_entry=(
                trend_regime.value
                if trend_regime and trend_regime != TrendRegime.UNKNOWN
                else None
            ),
            # 2026-04-22 (gece, late) — capture oscillator snapshot at
            # placement time. Carried through to fill's record_open and
            # pending-cancel's record_rejected_signal so the journal row
            # reflects the decision moment's data, not the later
            # fill/cancel moment when caches may have rotated.
            oscillator_raw_values_at_placement=(
                self._build_oscillator_raw_values(symbol, state)
            ),
        )
        logger.info(
            "zone_limit_placed symbol={} side={} order_id={} entry={:.4f} "
            "sl={:.4f} tp={:.4f} zone_source={} max_wait_bars={}",
            symbol, pos_side, result.order_id, zoned_plan.entry_price,
            zoned_plan.sl_price, zoned_plan.tp_price, zone.zone_source,
            zone.max_wait_bars,
        )
        return True

    async def _process_pending(self) -> None:
        """Drain pending-limit events from the monitor.

        FILLED (reason="fill") or FILLED (reason="timeout_partial_fill")
        → attach OCO protection, register open position, journal the row.
        CANCELED (reason="external" / "timeout") → clear the pending slot.
        """
        try:
            events = await asyncio.to_thread(self.ctx.monitor.poll_pending)
        except Exception:
            logger.exception("pending_poll_failed")
            return
        for ev in events:
            try:
                if ev.event_type == "FILLED":
                    await self._handle_pending_filled(ev)
                elif ev.event_type == "CANCELED":
                    await self._handle_pending_canceled(ev)
            except Exception:
                logger.exception(
                    "pending_event_failed inst={} side={} type={} reason={}",
                    ev.inst_id, ev.pos_side, ev.event_type, ev.reason,
                )

    async def _handle_pending_filled(self, ev: PendingEvent) -> None:
        """Promote a filled pending entry to an open, protected position.

        Steps:
          1. Pop the stashed PendingSetupMeta (plan + zone + signal_state).
          2. Attach OCO algos via OrderRouter.attach_algos. Failure here
             leaves the position UNPROTECTED — log CRITICAL, leave the
             `open_trade_ids` empty so the reconciler surfaces it.
          3. register_open on the monitor so the close-poll path takes over.
          4. journal.record_open for persistence + risk accounting.
        """
        key = (ev.inst_id, ev.pos_side)
        meta = self.ctx.pending_setups.pop(key, None)
        if meta is None:
            logger.warning(
                "pending_filled_no_meta inst={} side={} order_id={}",
                ev.inst_id, ev.pos_side, ev.order_id,
            )
            return
        plan = meta.plan
        # Partial-fill on timeout: honour the actual filled size so the OCO
        # doesn't over-commit and sCode 51020 on close.
        if ev.reason == "timeout_partial_fill" and ev.filled_size > 0:
            filled_int = max(1, int(ev.filled_size))
            if filled_int < plan.num_contracts:
                logger.warning(
                    "pending_partial_fill inst={} side={} planned={} filled={}",
                    ev.inst_id, ev.pos_side, plan.num_contracts, filled_int,
                )
                from dataclasses import replace
                plan = replace(plan, num_contracts=filled_int)

        fill_px = ev.avg_price if ev.avg_price > 0 else plan.entry_price

        # Pre-attach SL-crossed guard. Bybit rejects trading-stop attach
        # (110012) when the trigger price is already on the wrong side of
        # mark — and without this check the position stays open UNPROTECTED.
        # If mark has already breached plan.sl_price, skip the attach and
        # best-effort close immediately.
        try:
            mark_px = await asyncio.to_thread(
                self.ctx.client.get_mark_price, ev.inst_id,
            )
        except Exception:
            mark_px = 0.0
        if mark_px > 0:
            crossed = (
                (ev.pos_side == "long" and mark_px <= plan.sl_price)
                or (ev.pos_side == "short" and mark_px >= plan.sl_price)
            )
            if crossed:
                logger.critical(
                    "pending_fill_sl_already_crossed_closing inst={} side={} "
                    "order_id={} mark={:.6f} sl={:.6f}",
                    ev.inst_id, ev.pos_side, ev.order_id, mark_px, plan.sl_price,
                )
                try:
                    await asyncio.to_thread(
                        self.ctx.client.close_position,
                        ev.inst_id, ev.pos_side,
                        self.ctx.config.execution.margin_mode,
                    )
                except Exception as close_exc:
                    logger.critical(
                        "pending_fill_emergency_close_failed_manual_intervention "
                        "inst={} side={} err={!r}",
                        ev.inst_id, ev.pos_side, close_exc,
                    )
                return

        algos: list[AlgoResult]
        try:
            algos = await asyncio.to_thread(
                self.ctx.router.attach_algos, plan, ev.inst_id,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.critical(
                "pending_fill_algo_attach_failed_position_UNPROTECTED "
                "inst={} side={} order_id={} err={!r} code={} payload={}",
                ev.inst_id, ev.pos_side, ev.order_id, exc, code, payload,
            )
            return

        algo_ids = [a.algo_id for a in algos if a.algo_id]
        runner_size = _runner_size(plan.num_contracts, self.ctx.config)

        # 2026-04-20 — co-place a resting reduce-only limit at the runner TP
        # so wicks fill maker at the exact price instead of tripping the
        # OCO's mark-trigger → market-on-fire path (which slips, or fails
        # to fire at all when demo last-wick never moves mark). OCO stays
        # as SL + market-TP fallback. Best-effort: a failure here is NOT
        # fatal — OCO still protects, we just lose the maker-TP capture
        # for this trade. Only placed for the runner-size slice; partial
        # TP1 algo (if re-enabled) keeps its own market-on-trigger TP.
        tp_limit_order_id = ""
        cfg_exec = self.ctx.config.execution
        if (
            cfg_exec.tp_resting_limit_enabled
            and runner_size > 0
            and plan.tp_price > 0
        ):
            try:
                tp_limit_res = await asyncio.to_thread(
                    self.ctx.bybit_client.place_reduce_only_limit,
                    ev.inst_id, ev.pos_side, int(runner_size),
                    float(plan.tp_price), cfg_exec.margin_mode, True, None,
                )
                tp_limit_order_id = tp_limit_res.order_id
                logger.info(
                    "tp_limit_placed inst={} side={} size={} tp={} ord={}",
                    ev.inst_id, ev.pos_side, runner_size,
                    plan.tp_price, tp_limit_order_id,
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                logger.warning(
                    "tp_limit_place_failed inst={} side={} tp={} err={!r} "
                    "code={} — OCO market-TP still protects, no maker-TP",
                    ev.inst_id, ev.pos_side, plan.tp_price, exc, code,
                )

        self.ctx.monitor.register_open(
            ev.inst_id, ev.pos_side, float(plan.num_contracts), fill_px,
            algo_ids=algo_ids, tp2_price=plan.tp_price,
            sl_price=plan.sl_price, runner_size=runner_size,
            plan_sl_price=plan.sl_price,
            tp_limit_order_id=tp_limit_order_id,
        )
        self.ctx.risk_mgr.register_trade_opened()

        entry_result = OrderResult(
            order_id=ev.order_id,
            client_order_id=ev.order_id,
            status=OrderStatus.FILLED,
            filled_sz=float(plan.num_contracts),
            avg_price=fill_px,
        )
        report = ExecutionReport(
            entry=entry_result,
            algos=algos,
            state=PositionState.OPEN,
            leverage_set=True,
            plan_reason=plan.reason,
        )

        state = meta.signal_state
        cfg = self.ctx.config
        try:
            rec = await self.ctx.journal.record_open(
                plan, report,
                symbol=ev.inst_id,
                signal_timestamp=meta.placed_at,
                entry_timeframe=cfg.trading.entry_timeframe,
                htf_timeframe=cfg.trading.htf_timeframe,
                htf_bias=_bias_str(state),
                session=_session_str(state),
                market_structure=_structure_str(state),
                trend_regime_at_entry=meta.trend_regime_at_entry,
                on_chain_context=self._on_chain_context_dict(),
                confluence_pillar_scores=dict(plan.confluence_pillar_scores or {}),
                # 2026-04-22 (gece, late) — journal oscillator snapshot
                # captured at pending PLACEMENT (not fill) so the row
                # reflects the decision moment. Fill may happen minutes
                # after placement and the caches may have rotated.
                oscillator_raw_values=dict(
                    meta.oscillator_raw_values_at_placement or {}
                ),
                # 2026-04-27 (F3) — zone metadata forwarding. Pre-fix the
                # 9 Bybit-era trades all had setup_zone_source / wait_bars
                # / fill_latency NULL despite being zone-based entries.
                # `zone_fill_latency_bars` is computed from wall-clock time
                # between placement and fill, divided by entry_tf_minutes.
                # Bounded above by zone.max_wait_bars (timeout cancels at
                # that boundary so the limit never sits longer).
                setup_zone_source=str(meta.zone.zone_source),
                zone_wait_bars=int(meta.zone.max_wait_bars),
                zone_fill_latency_bars=_zone_fill_latency_bars(
                    placed_at=meta.placed_at,
                    fill_at=_utc_now(),
                    entry_tf_minutes=_timeframe_to_minutes(
                        cfg.trading.entry_timeframe),
                    max_wait_bars=int(meta.zone.max_wait_bars),
                ),
                **_derive_enrichment(state),
            )
            self.ctx.open_trade_ids[key] = rec.trade_id
            self.ctx.open_trade_opened_at[key] = _utc_now()
            logger.info(
                "pending_filled_promoted inst={} side={} contracts={} "
                "fill_px={:.4f} zone={} trade_id={}",
                ev.inst_id, ev.pos_side, plan.num_contracts, fill_px,
                meta.zone.zone_source, rec.trade_id,
            )
        except Exception:
            logger.exception(
                "pending_fill_journal_write_failed_live_position_orphaned "
                "inst={} side={}",
                ev.inst_id, ev.pos_side,
            )

    async def _handle_pending_canceled(self, ev: PendingEvent) -> None:
        """Clear the pending slot when the limit was cancelled (timeout or
        external). Log a rejected_signal row for counter-factual analysis."""
        key = (ev.inst_id, ev.pos_side)
        meta = self.ctx.pending_setups.pop(key, None)
        reason_map = {
            "timeout": "zone_timeout_cancel",
            "external": "pending_invalidated",
            "manual": "pending_invalidated",
            "invalidated": "pending_invalidated",
        }
        # 2026-04-22 — hard-gate-driven cancels carry the specific gate
        # name in `reason` as `hard_gate:<gate_name>`. Map all such
        # variants to `pending_hard_gate_invalidated` so Phase 9 GBT can
        # filter by this category specifically. The original gate name
        # is preserved in the bot's log line at cancel time.
        if ev.reason and ev.reason.startswith("hard_gate:"):
            reject_reason = "pending_hard_gate_invalidated"
        else:
            reject_reason = reason_map.get(ev.reason, "pending_invalidated")
        logger.info(
            "pending_canceled inst={} side={} order_id={} reason={}",
            ev.inst_id, ev.pos_side, ev.order_id, ev.reason,
        )
        if meta is None:
            return
        plan = meta.plan
        state = meta.signal_state
        enrichment = _derive_enrichment(state)
        try:
            await self.ctx.journal.record_rejected_signal(
                symbol=ev.inst_id,
                direction=plan.direction,
                reject_reason=reject_reason,
                signal_timestamp=meta.placed_at,
                price=float(state.current_price) if state.current_price else None,
                atr=float(state.atr) if state.atr else None,
                confluence_score=float(plan.confluence_score or 0.0),
                confluence_factors=list(plan.confluence_factors),
                entry_timeframe=self.ctx.config.trading.entry_timeframe,
                htf_timeframe=self.ctx.config.trading.htf_timeframe,
                htf_bias=_bias_str(state),
                session=_session_str(state),
                market_structure=_structure_str(state),
                regime_at_entry=enrichment["regime_at_entry"],
                funding_z_at_entry=enrichment["funding_z_at_entry"],
                ls_ratio_at_entry=enrichment["ls_ratio_at_entry"],
                oi_change_24h_at_entry=enrichment["oi_change_24h_at_entry"],
                liq_imbalance_1h_at_entry=enrichment["liq_imbalance_1h_at_entry"],
                nearest_liq_cluster_above_price=enrichment["nearest_liq_cluster_above_price"],
                nearest_liq_cluster_below_price=enrichment["nearest_liq_cluster_below_price"],
                nearest_liq_cluster_above_notional=enrichment["nearest_liq_cluster_above_notional"],
                nearest_liq_cluster_below_notional=enrichment["nearest_liq_cluster_below_notional"],
                nearest_liq_cluster_above_distance_atr=enrichment["nearest_liq_cluster_above_distance_atr"],
                nearest_liq_cluster_below_distance_atr=enrichment["nearest_liq_cluster_below_distance_atr"],
                pillar_btc_bias=self._pillar_bias_label("BTC-USDT-SWAP"),
                pillar_eth_bias=self._pillar_bias_label("ETH-USDT-SWAP"),
                on_chain_context=self._on_chain_context_dict(),
                confluence_pillar_scores=dict(plan.confluence_pillar_scores or {}),
                # 2026-04-22 (gece, late) — same placement-time oscillator
                # snapshot used by the pending-fill path. The cancel might
                # fire 7 bars after placement; we still log the
                # decision-moment numerics so Pass 2 can segment cancels
                # by what the oscillator looked like when the limit went in.
                oscillator_raw_values=dict(
                    meta.oscillator_raw_values_at_placement or {}
                ),
                # 2026-04-27 — derivatives + heatmap enrichment forwarding,
                # parity with the `_record_reject` path. Note: candles is
                # not threaded here (`_derive_enrichment(state)` above ran
                # without candles) so price_change_1h/4h_pct will be NULL
                # on cancel rows by design — pending-fill paths don't
                # stash a placement-time candle buffer (CLAUDE.md
                # "pending-fill paths stay candles=None").
                open_interest_usd_at_entry=enrichment["open_interest_usd_at_entry"],
                oi_change_1h_pct_at_entry=enrichment["oi_change_1h_pct_at_entry"],
                funding_rate_current_at_entry=enrichment["funding_rate_current_at_entry"],
                funding_rate_predicted_at_entry=enrichment["funding_rate_predicted_at_entry"],
                long_liq_notional_1h_at_entry=enrichment["long_liq_notional_1h_at_entry"],
                short_liq_notional_1h_at_entry=enrichment["short_liq_notional_1h_at_entry"],
                ls_ratio_zscore_14d_at_entry=enrichment["ls_ratio_zscore_14d_at_entry"],
                price_change_1h_pct_at_entry=enrichment["price_change_1h_pct_at_entry"],
                price_change_4h_pct_at_entry=enrichment["price_change_4h_pct_at_entry"],
                liq_heatmap_top_clusters=enrichment["liq_heatmap_top_clusters"],
            )
        except Exception:
            logger.debug(
                "pending_cancel_reject_log_failed inst={} side={}",
                ev.inst_id, ev.pos_side,
            )

    async def _handle_close(self, fill: CloseFill) -> None:
        try:
            enriched = await asyncio.to_thread(
                self.ctx.bybit_client.enrich_close_fill, fill)
        except Exception:
            logger.exception("enrich_failed_using_raw_fill")
            enriched = fill

        key = (enriched.inst_id, enriched.pos_side)
        trade_id = self.ctx.open_trade_ids.pop(key, None)
        # Madde F — carry close_reason set by the defensive-close path.
        close_reason = self.ctx.pending_close_reasons.pop(key, None)
        # 2026-04-27 (F3) — natural close (SL/TP hit) has no explicit
        # reason set by the runner. Pre-fix this left close_reason NULL
        # on every Bybit-era row (9/9 NULL on the 6-closed dataset).
        # Infer from realized PnL sign so Pass 3 can segment closes by
        # reason without parsing the loss/gain magnitude separately.
        # Defensive-close reasons take precedence (already popped above).
        if close_reason is None:
            close_reason = _infer_close_reason(enriched.pnl_usdt)
        self.ctx.defensive_close_in_flight.discard(key)
        self.ctx.open_trade_opened_at.pop(key, None)

        if trade_id is None:
            logger.warning("orphan_close key={} (no matching trade_id)", key)
            # Still feed risk_mgr so our paper balance tracks reality.
            self.ctx.risk_mgr.register_trade_closed(TradeResult(
                pnl_usdt=enriched.pnl_usdt, pnl_r=0.0,
                timestamp=enriched.closed_at or _utc_now(),
            ))
            return

        try:
            updated = await self.ctx.journal.record_close(
                trade_id, enriched,
                fees_usdt=abs(enriched.fee_usdt),
                close_reason=close_reason,
            )
        except Exception:
            logger.exception("journal_close_failed trade_id={}", trade_id)
            # Still update risk_mgr so streaks / drawdown stay accurate.
            self.ctx.risk_mgr.register_trade_closed(TradeResult(
                pnl_usdt=enriched.pnl_usdt, pnl_r=0.0,
                timestamp=enriched.closed_at or _utc_now(),
            ))
            return

        self.ctx.risk_mgr.register_trade_closed(TradeResult(
            pnl_usdt=updated.pnl_usdt or 0.0,
            pnl_r=updated.pnl_r or 0.0,
            timestamp=enriched.closed_at or _utc_now(),
        ))
        # Remember the close for the reentry gate (Madde C). Conf score
        # comes from the original record; outcome from the post-close update.
        self.ctx.last_close[key] = LastCloseInfo(
            price=float(enriched.exit_price or 0.0),
            time=enriched.closed_at or _utc_now(),
            confluence=int(updated.confluence_score or 0),
            outcome=updated.outcome.value,
        )
        logger.info("closed trade_id={} outcome={} pnl_r={:.2f}",
                    trade_id, updated.outcome.value, updated.pnl_r or 0.0)

        # Katman 2 — cross-check entry/exit against real-market public feed.
        # Best-effort: swallow failures so journal close success doesn't
        # hinge on Binance availability.
        try:
            await self._cross_check_close_artefacts(
                trade_id=trade_id,
                symbol=updated.symbol,
                entry_ts=updated.entry_timestamp,
                entry_price=float(updated.entry_price),
                exit_ts=enriched.closed_at or _utc_now(),
                exit_price=float(enriched.exit_price or 0.0),
            )
        except Exception:
            logger.exception(
                "artefact_cross_check_failed trade_id={}", trade_id,
            )

    async def _cross_check_close_artefacts(
        self,
        *,
        trade_id: str,
        symbol: str,
        entry_ts: datetime,
        entry_price: float,
        exit_ts: datetime,
        exit_price: float,
    ) -> None:
        """Compare journaled entry/exit prices against Binance USD-M futures
        1m candles. Stamps `demo_artifact=True` when either price sits outside
        the concurrent real-market [low, high] band. Non-destructive — the
        trade stays in the journal; downstream filters use the flag.

        Disabled in two ways:
          - `execution.artefact_check_enabled=false` → `ctx.binance_public`
            is None and we return before any network call.
          - Any leg (entry or exit) whose candle couldn't be fetched leaves
            that side's `real_market_*_valid=None` (tri-state), and
            `demo_artifact` stays None only when BOTH sides fail. If at
            least one side could be checked and that side is invalid, we
            still flag the trade.
        """
        client = self.ctx.binance_public
        if client is None:
            return
        binance_symbol = internal_to_binance_futures(symbol)
        if not binance_symbol:
            logger.debug(
                "artefact_unmapped_symbol symbol={} trade_id={}",
                symbol, trade_id,
            )
            return
        tolerance = self.ctx.config.execution.artefact_check_tolerance_pct

        def _fetch(ts: datetime) -> Optional[RealCandle]:
            return client.get_kline_around(
                binance_symbol, int(ts.timestamp() * 1000),
            )

        try:
            entry_candle = await asyncio.to_thread(_fetch, entry_ts)
            exit_candle = await asyncio.to_thread(_fetch, exit_ts)
        except Exception:
            logger.exception(
                "artefact_kline_fetch_failed trade_id={} symbol={}",
                trade_id, binance_symbol,
            )
            return

        entry_valid: Optional[bool] = (
            price_inside_candle(entry_price, entry_candle, tolerance)
            if entry_candle is not None else None
        )
        exit_valid: Optional[bool] = (
            price_inside_candle(exit_price, exit_candle, tolerance)
            if exit_candle is not None else None
        )

        def _describe(prefix: str, price: float, candle: RealCandle) -> str:
            if price > candle.high:
                return f"{prefix}_above_binance_high"
            return f"{prefix}_below_binance_low"

        reasons: list[str] = []
        if entry_candle is not None and entry_valid is False:
            reasons.append(_describe("entry", entry_price, entry_candle))
        if exit_candle is not None and exit_valid is False:
            reasons.append(_describe("exit", exit_price, exit_candle))

        if entry_valid is None and exit_valid is None:
            artifact: Optional[bool] = None
        else:
            artifact = bool(reasons)  # flag iff at least one checked side invalid

        try:
            await self.ctx.journal.update_artifact_flags(
                trade_id,
                real_market_entry_valid=entry_valid,
                real_market_exit_valid=exit_valid,
                demo_artifact=artifact,
                artifact_reason=";".join(reasons) if reasons else None,
            )
        except KeyError:
            logger.warning(
                "artefact_update_unknown_trade trade_id={}", trade_id,
            )
            return

        if artifact:
            logger.warning(
                "demo_artifact_detected trade_id={} symbol={} reasons={}",
                trade_id, binance_symbol, reasons,
            )
        else:
            logger.debug(
                "artefact_check_ok trade_id={} symbol={} entry_valid={} exit_valid={}",
                trade_id, binance_symbol, entry_valid, exit_valid,
            )

    async def _rehydrate_open_positions(self) -> None:
        """Populate monitor + open_trade_ids from journal OPEN rows.

        `replay_for_risk_manager` already walked CLOSED trades; this covers
        OPEN rows so we know what to expect on the next poll.
        """
        cfg_exec = self.ctx.config.execution
        for rec in await self.ctx.journal.list_open_trades():
            pos_side = _direction_to_pos_side(rec.direction)
            runner_size = _runner_size(int(rec.num_contracts), self.ctx.config)
            # plan_sl_price is lost post-BE on restart (journal only stores the
            # current SL). Pre-BE: current SL == plan SL. Post-BE: pass 0 →
            # dynamic TP revision no-ops for this position (safer than reviving
            # with a near-zero sl_distance that Bybit rejects).
            plan_sl = 0.0 if rec.sl_moved_to_be else rec.sl_price
            # 2026-04-20 — TP-limit is in-memory only; `_cancel_orphan_pending_limits`
            # wipes every resting limit at startup, so any TP limit we placed
            # pre-restart is already gone by the time we rehydrate. Re-place a
            # fresh one here so the maker-TP coverage survives restart.
            tp_limit_order_id = ""
            if (
                cfg_exec.tp_resting_limit_enabled
                and runner_size > 0
                and rec.tp_price > 0
                and not rec.sl_moved_to_be
            ):
                try:
                    tp_limit_res = await asyncio.to_thread(
                        self.ctx.bybit_client.place_reduce_only_limit,
                        rec.symbol, pos_side, int(runner_size),
                        float(rec.tp_price), cfg_exec.margin_mode, True, None,
                    )
                    tp_limit_order_id = tp_limit_res.order_id
                    logger.info(
                        "tp_limit_replaced_on_rehydrate inst={} side={} "
                        "size={} tp={} ord={}",
                        rec.symbol, pos_side, runner_size,
                        rec.tp_price, tp_limit_order_id,
                    )
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    logger.warning(
                        "tp_limit_rehydrate_place_failed inst={} side={} "
                        "tp={} err={!r} code={} — OCO still protects",
                        rec.symbol, pos_side, rec.tp_price, exc, code,
                    )
            self.ctx.monitor.register_open(
                rec.symbol, pos_side,
                float(rec.num_contracts), rec.entry_price,
                # 2026-04-27 — `algo_ids` column dropped (Bybit V5 has
                # position-attached TP/SL, no separate algo orders to
                # track). Pass an empty list so the monitor's bookkeeping
                # stays happy on rehydrate.
                algo_ids=[],
                tp2_price=rec.tp_price,
                be_already_moved=rec.sl_moved_to_be,
                sl_price=rec.sl_price, runner_size=runner_size,
                plan_sl_price=plan_sl,
                tp_limit_order_id=tp_limit_order_id,
            )
            self.ctx.open_trade_ids[(rec.symbol, pos_side)] = rec.trade_id
            self.ctx.open_trade_opened_at[(rec.symbol, pos_side)] = rec.entry_timestamp
            # These don't count against RiskManager.open_positions because
            # replay already paired every recorded open with its close.

    async def _reconcile_orphans(self) -> None:
        """Reconcile Bybit state against the journal at startup.

        Three passes:

        1. Positions mismatch — log-only (operator decides). Unknown live
           positions with no journal row, or stale journal OPEN rows whose
           live position is gone.
        2. Orphan resting limit orders — CANCEL. The monitor's in-memory
           `_pending` dict is empty at startup, so any live pending limit
           on Bybit at this moment cannot be tracked by this process; if it
           filled we'd get an untracked (= unprotected) position. Safer to
           cancel now than to discover it as an orphan later.
        3. Surplus OCOs — CANCEL those not referenced by the journal's
           `algo_ids` for a (symbol, posSide) that has an OPEN row. This
           covers the 2026-04-20 DOGE 2-OCO bug: a pre-restart
           revise/lock placed a replacement OCO whose new algoId never made
           it to the journal, so rehydrate missed it and the unreferenced
           algo lives on as a phantom stop.

        Never auto-closes *positions* — that stays operator-decides.
        """
        try:
            live = await asyncio.to_thread(self.ctx.bybit_client.get_positions)
        except Exception:
            logger.exception("reconcile_fetch_failed")
            return
        live_keys = {(p.inst_id, p.pos_side) for p in live if p.size != 0}
        # Read journal OPEN rows directly — `open_trade_ids` is empty here
        # because reconcile now runs BEFORE rehydrate (so the pending-limit
        # sweep can't nuke freshly-placed TP limits).
        try:
            open_recs = await self.ctx.journal.list_open_trades()
        except Exception:
            logger.exception("reconcile_journal_read_failed")
            return
        journal_keys = {
            (rec.symbol, _direction_to_pos_side(rec.direction))
            for rec in open_recs
        }
        for k in live_keys - journal_keys:
            logger.error("orphan_live_position_no_journal_row key={}", k)
        for k in journal_keys - live_keys:
            logger.error("journal_open_but_no_live_position key={} (stale row)", k)

        await self._cancel_orphan_pending_limits()

    async def _cancel_orphan_pending_limits(self) -> None:
        """Cancel every resting limit order on Bybit at startup.

        The monitor's `_pending` dict is in-memory only and lost on
        restart, so any resting limit found here is untrackable — if it
        fills, `_handle_pending_filled` will never fire and the position
        is born unprotected.
        """
        try:
            rows = await asyncio.to_thread(
                self.ctx.bybit_client.list_open_orders,
            )
        except Exception:
            logger.exception("orphan_pending_limits_scan_failed")
            return
        for row in rows:
            ord_id = row.get("orderId")
            inst_id = row.get("symbol")
            if not ord_id or not inst_id:
                continue
            try:
                await asyncio.to_thread(
                    self.ctx.bybit_client.cancel_order, inst_id, ord_id,
                )
                logger.warning(
                    "orphan_pending_limit_canceled inst={} ord={} px={} sz={}",
                    inst_id, ord_id, row.get("price"), row.get("qty"),
                )
            except Exception:
                logger.exception(
                    "orphan_pending_limit_cancel_failed inst={} ord={}",
                    inst_id, ord_id,
                )

