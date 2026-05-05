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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

from src.analysis.liquidity_heatmap import build_heatmap
from src.analysis.multi_timeframe import (
    ConfluenceScore,
    calculate_confluence,
    score_direction,
)
from src.analysis.support_resistance import detect_sr_zones
from src.analysis.trend_regime import (
    TrendRegime,
    TrendRegimeResult,
    classify_trend_regime,
)
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
from src.strategy.ha_state import HAStateRegistry
from src.strategy.ha_native_exit import (
    ExitContext as HANativeExitContext,
    HANativeExitConfig,
    evaluate_exit as evaluate_ha_native_exit,
)
from src.strategy.rr_system import calculate_trade_plan
from src.strategy.entry_signals import (
    _flow_alignment_score,
    evaluate_pending_invalidation_gates,
    in_vwap_reset_blackout,
)
from src.strategy.risk_manager import RiskManager, TradeResult
# Legacy `setup_planner` module deleted 2026-05-05 v3 (Faz 5 Yol A cleanup).
# PendingSetupMeta.zone is always None for HA-native plans — typing kept
# as Optional[Any] for backward-compat with any rehydrate-path consumer.
from src.strategy.trade_plan import TradePlan
from src.strategy.what_if_sltp import (
    NO_PROPOSED_SLTP_REASONS,
    compute_what_if_proposed_sltp,
)


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
# BTC and ETH move the rest of the crypto book. The cross-asset lock
# turns on only when one of the two pillars holds an OPEN position:
# the second pillar — and any altcoin — cannot then take the opposite
# direction. The pre-2026-05-01 EMA-stack snapshot model was retired
# 2026-05-01 (ikinci tighten): in EMA-neutral chop both pillars
# returned UNDEFINED and the fail-closed sentinel blocked every symbol
# for hours even on 6+ confluence signals. Open-position truth is
# direction-binary and decays only when the position closes — no stale
# tolerance, no fail-closed storm.

_PILLAR_SYMBOLS: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")


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


def _adx_triad_kwargs(
    prefix: str,
    result: Optional["TrendRegimeResult"],
) -> dict:
    """Phase A.9 — build the 3-key journal kwargs dict for an ADX result.

    Emits `{adx_<prefix>_at_entry, plus_di_<prefix>_at_entry,
    minus_di_<prefix>_at_entry}` with raw `compute_adx` values. UNKNOWN
    (insufficient bars / flat prices) → all three NULL: the classifier
    returns `adx=0.0` in that case but 0 is a legitimate computed value
    elsewhere, so persist NULL to keep "insufficient data" distinguishable
    from "computed zero" downstream.
    """
    keys = (
        f"adx_{prefix}_at_entry",
        f"plus_di_{prefix}_at_entry",
        f"minus_di_{prefix}_at_entry",
    )
    if result is None or result.regime == TrendRegime.UNKNOWN:
        return {k: None for k in keys}
    return {
        keys[0]: float(result.adx),
        keys[1]: float(result.plus_di),
        keys[2]: float(result.minus_di),
    }


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
    # 2026-05-05 v3 — Yol A: HA-native plans skip zone search; meta.zone
    # is always None now (legacy ZoneSetup dataclass retired in Faz 5).
    zone: Optional[Any]
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
    # 2026-05-02 — Phase A.9 ADX result captured at PLACEMENT TIME for
    # entry TF + HTF. Carried through to fill's record_open and
    # pending-cancel's record_rejected_signal so the journal row reflects
    # the regime when the limit was placed (not when it filled / was
    # canceled). None when the classifier returned no result for that TF
    # (cache cold at placement, e.g. already-open skip on the same symbol
    # in a prior cycle that the planner re-evaluated). The downstream
    # `_adx_triad_kwargs` helper turns None / UNKNOWN into NULL columns.
    adx_3m_result_at_placement: Optional[TrendRegimeResult] = None
    adx_15m_result_at_placement: Optional[TrendRegimeResult] = None
    # 2026-05-05 v4 — Yol A confluence support signal stamped at placement
    # time. Used as direction-confirmation + Pass 3 GBT feature only;
    # never gates the entry decision (HA-native dispatcher owns that).
    # Forwarded to journal `confluence_pillar_scores` on fill.
    confluence_at_placement: Optional[ConfluenceScore] = None


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
    # 2026-05-02 — Phase A.9 HTF ADX result cached per-symbol after the HTF
    # pass. Computed from the same htf_candles buffer used for S/R + state,
    # so zero extra TV/Bybit calls. Pop'd alongside `htf_state_cache` on
    # already-open skip / refresh error so a stale 15m regime never
    # accompanies a fresh 3m row.
    htf_adx_cache: dict[str, TrendRegimeResult] = field(default_factory=dict)
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
    # Last per-slot margin (computed each `_run_one_symbol` cycle). Threaded
    # into `apply_zone_to_plan` so zone re-sizing can recompute leverage off
    # the same margin budget the original sizing saw — without this, a
    # tight zone SL grows notional past the initial-margin ceiling and
    # Bybit returns 110007 (DOGE 2026-04-28).
    last_margin_balance: float = 0.0
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
    # {internal_symbol: usd_netflow_float}. Refreshed on its own cadence
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
    # 2026-05-04 — HA-native runtime state registry. Per-symbol in-memory
    # history buffer (HASymbolState with deque(maxlen=60)) for 3-bar delta
    # direction (MFI/RSI), color flip detection, dominant_color analysis.
    # Pumped each cycle from `last_market_state_per_symbol[symbol]`. Bot
    # restart sıfırlanır; backfill helper Bybit kline'dan startup'ta
    # 50-bar geçmiş push edebilir (separate runner step).
    ha_state_registry: HAStateRegistry = field(default_factory=HAStateRegistry)
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
    # 2026-05-05 v3 — Yol A is the only entry strategy. Legacy 5-pillar
    # path (build_trade_plan_with_reason + _LEGACY_5PILLAR_ENABLED flag +
    # pillar helpers + cross-asset veto) was deleted in this commit.

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
        # Per-symbol throttle for macro_event_blackout log lines. Without it
        # the blackout dal short-circuits each symbol cycle in ~6s, producing
        # ~2 log lines/sec for the full ±window of a HIGH-impact event.
        self._macro_blackout_log_ts: dict[str, float] = {}

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
                await self._backfill_ha_history()
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

    async def _backfill_ha_history(self) -> None:
        """Seed `HAStateRegistry` from Bybit kline so HA analytics are
        immediately available on cycle 1 (no first-cycle history gap).

        Operatör 2026-05-04: bot başlatıldığında her sembol için son 50
        × 3m bar Bybit'ten çekilir, Pine v6 formülüyle birebir Heikin Ashi
        OHLC + color + streak + body% + shadow flags hesaplanır,
        `HASymbolState.history` deque'sine push edilir.

        Sonuç:
          - `dominant_color_3m()` cycle 1'den itibaren çalışır (operatör'ün
            "5+ yeşil mum varsa kırmızı bekle" filtresi baştan aktif)
          - HA streak counter Pine'la senkron başlar
          - Color flip detection ilk cycle'dan doğru çalışır

        Multi-TF (1m/15m) ve MFI/RSI runtime Pine cycle'larından dolar —
        backfill sadece 3m HA color/streak/body/shadow için.

        Failure-tolerant: bir sembol başarısız olsa loglar + sonrakilere
        devam eder. Bybit demo bağlantısı yoksa bütün backfill skip.
        """
        if self.ctx.bybit_client is None:
            logger.info("ha_backfill_skipped reason=no_bybit_client")
            return
        from src.strategy.ha_history_backfill import fetch_and_backfill
        symbols = list(self.ctx.config.trading.symbols)
        for symbol in symbols:
            try:
                raw_klines = await asyncio.to_thread(
                    self.ctx.bybit_client.get_kline,
                    symbol, "3", 50,
                )
                if not raw_klines:
                    logger.warning("ha_backfill_empty symbol={}", symbol)
                    continue
                n = fetch_and_backfill(
                    self.ctx.ha_state_registry, symbol, raw_klines,
                )
                state = self.ctx.ha_state_registry.get(symbol)
                latest = state.latest if state else None
                logger.info(
                    "ha_backfill_done symbol={} bars={} "
                    "latest_color={} latest_streak={}",
                    symbol, n,
                    latest.ha_color_3m if latest else None,
                    latest.ha_streak_3m if latest else None,
                )
            except Exception:
                logger.exception("ha_backfill_failed symbol={}", symbol)

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
        # 2026-05-02 — Phase A.10. Before draining closes, finalise any
        # maker-first defensive close LIMIT that has passed its deadline
        # without filling — cancel the limit + fire market fallback. The
        # market reduce shows up as a CloseFill in the same cycle's
        # _process_closes() drain below.
        await self._finalize_expired_defensive_closes()

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

        # 2026-05-05 v3 — Yol A: final pending drain after the symbol loop.
        # Last symbol's freshly-placed limit can fill in the same tick
        # (production: rare but possible; tests: synthetic FILLED queued
        # by FakeMonitor). No-op when there are no pending events.
        try:
            await self._process_pending()
        except Exception:
            logger.exception("final_pending_drain_failed")

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
                # 2026-05-05 v3 — Yol A: HA-native pending limit'leri için
                # sadece time-based gate (vwap_reset_blackout) kalır. Legacy
                # `pillar_opposition` / `vwap_hard_veto` / `ema_veto` config
                # default'ları False; HA-native exit kendi `_maybe_close_on_ha_flip`
                # path'ini kullanıyor.
                gate_reason = evaluate_pending_invalidation_gates(
                    state=state,
                    candles=candles,
                    direction=meta.plan.direction,
                    entry_price=float(meta.plan.entry_price),
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
            # `cancel_pending` usually returns CANCELED, but the
            # 2026-04-27 phantom-cancel resistance path can also return
            # FILLED with reason="phantom_cancel_recovery" — Bybit gone-
            # codes cover BOTH "already cancelled" AND "already filled",
            # and `_verify_cancel_terminal_state` discovers the latter.
            # Route by event_type: FILLED → fill flow (attach TP/SL +
            # register + journal) so the live position is tracked, NOT
            # silently lost. CANCELED → journal the
            # `pending_hard_gate_invalidated` rejected_signals row.
            #
            # Why dispatch ourselves: only `poll_pending` events flow
            # through `_process_pending`; cancel_pending's return is
            # synchronous and out-of-band.
            if ev is not None:
                try:
                    if ev.event_type == "FILLED":
                        await self._handle_pending_filled(ev)
                    else:
                        await self._handle_pending_canceled(ev)
                except Exception:
                    logger.exception(
                        "pending_hard_gate_handler_failed symbol={} "
                        "side={} event_type={}",
                        symbol_, pos_side, ev.event_type,
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
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return
        # 2026-05-02 — Phase A regime-aware RR. The position carries the ADX
        # regime captured at entry-time; resolve the per-regime override
        # (`target_rr_ratio_per_regime[regime]` if set, else global). UNKNOWN
        # / pre-Phase-A rehydrate rows fall back to the global value via the
        # `effective_*` helpers — same behavior as before this commit when
        # `target_rr_ratio_per_regime` is empty (default).
        regime = snap.get("regime_at_entry")
        target_rr = cfg.execution.effective_target_rr_ratio(regime)
        if target_rr <= 0:
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
        # hard backstop on the proposed RR. Per-regime override applies
        # the same way as target_rr above.
        floor = cfg.execution.effective_tp_min_rr_floor(regime)
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

    async def _maybe_trail_sl_after_mfe(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> None:
        """Multi-step trailing SL after MFE-lock (Phase A.5, 2026-05-02).

        Where MFE-lock (`_maybe_lock_sl_on_mfe`) is a one-shot move-to-BE
        at MFE 1.0R, this gate pulls SL forward in `trail_step_r`-sized
        increments AFTER the position passes `trail_arm_at_mfe_r` (default
        1.5R, i.e. at least 0.5R past BE-lock). On each cycle:

          1. Read current MFE in plan-R units.
          2. Snap to a step-aligned target lock R: floor((mfe_r - dist) / step) * step.
          3. If target ≤ last_trail_lock_r → no-op (monotonic guard).
          4. Else compute new_sl = entry + sign × target_lock_r × plan_sl_dist
             and call `monitor.trail_sl_to(...)`.

        Disabled regimes (default RANGING) skip the gate entirely — TP at
        1.2R fires before trailing would arm at 1.5R, so trailing in
        RANGING is wasted churn.

        Distance = 0.5R behind MFE gives wick clearance: a typical 3m
        candle wick from peak can't reach the locked SL.
        """
        cfg = self.ctx.config
        if not cfg.execution.trail_sl_enabled:
            return
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return
        regime = snap.get("regime_at_entry")
        if cfg.execution.is_trailing_disabled_for(regime):
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
        arm = float(cfg.execution.trail_arm_at_mfe_r)
        if mfe_r < arm:
            return
        step = float(cfg.execution.trail_step_r)
        distance = float(cfg.execution.trail_distance_r)
        if step <= 0 or distance < 0:
            return
        # Snap MFE-distance to step-aligned R-grid. floor() means we lock at
        # a CONSERVATIVE level (always behind current MFE), never overshoot.
        import math as _math
        raw_target = mfe_r - distance
        target_lock_r = _math.floor(raw_target / step) * step
        last_lock = float(snap.get("last_trail_lock_r") or 0.0)
        if target_lock_r <= last_lock:
            return
        new_sl = entry + sign * target_lock_r * sl_distance
        try:
            await asyncio.to_thread(
                self.ctx.monitor.trail_sl_to, symbol, pos_side, new_sl,
                target_lock_r,
            )
        except Exception:
            logger.exception(
                "sl_trail_dispatch_failed symbol={} side={}",
                symbol, pos_side,
            )

    async def _maybe_lock_sl_on_mae_recovery(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> None:
        """MAE-triggered BE-lock with LIMIT-based exit (Phase A.6, 2026-05-02).

        Two-stage gate:
          stage 1: arm when MAE crosses `mae_be_lock_threshold_r` (e.g. -0.6R).
                   The position has been deep underwater; we mark it as
                   "danger zone" but don't act yet.
          stage 2: when mark recovers to within `mae_be_lock_recovery_band_r`
                   of entry AND the cycle's LTF direction signal is still
                   adverse to the position → place a reduce-only post-only
                   LIMIT at entry + fee_buffer (long) or entry - fee_buffer
                   (short). Touch from below (long) or above (short) fills
                   the limit at micro-profit covering the round-trip taker
                   on entry. Position-attached SL at -1R stays as backup.

        One-shot per position via `mae_be_lock_applied` flag in monitor.
        Disabled regimes skip the gate.

        LTF "adverse" check: same direction model as `_is_ltf_reversal`
        but softer — just `ltf.trend` opposing position; no `last_signal`
        / `bars_ago` requirement (we want the gate to be sensitive to
        the cycle's current bias, not its freshness).
        """
        cfg = self.ctx.config
        if not cfg.execution.mae_be_lock_enabled:
            return
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return
        if snap.get("mae_be_lock_applied"):
            return
        regime = snap.get("regime_at_entry")
        if cfg.execution.is_mae_be_lock_disabled_for(regime):
            return
        plan_sl = float(snap.get("plan_sl_price") or 0.0)
        entry = float(snap.get("entry_price") or 0.0)
        if plan_sl <= 0 or entry <= 0:
            return
        sl_distance = abs(entry - plan_sl)
        if sl_distance <= 0:
            return

        threshold = float(cfg.execution.mae_be_lock_threshold_r)
        mae_low = float(snap.get("mae_r_low") or 0.0)

        # Stage 1: arm when MAE has reached the threshold (deeper than
        # -0.6R). This is sticky — once armed, stays armed until applied
        # or position closes.
        if not snap.get("mae_be_lock_armed"):
            if mae_low <= threshold:
                self.ctx.monitor.arm_mae_be_lock(symbol, pos_side)
                logger.info(
                    "mae_be_lock_armed symbol={} side={} mae_r_low={:.3f} "
                    "threshold={:.3f}",
                    symbol, pos_side, mae_low, threshold,
                )
            return  # don't fire stage 2 in the same cycle as stage 1

        # Stage 2 conditions: recovery to entry zone + adverse cycle.
        current_px = float(getattr(state, "current_price", 0.0) or 0.0)
        if current_px <= 0:
            return
        sign = 1 if pos_side == "long" else -1
        cur_r = sign * (current_px - entry) / sl_distance
        recovery_band = float(cfg.execution.mae_be_lock_recovery_band_r)
        if abs(cur_r) > recovery_band:
            return  # mark not close enough to entry yet

        # Adverse-cycle check: LTF trend opposite to position direction.
        ltf = self.ctx.ltf_cache.get(symbol)
        if ltf is None or getattr(ltf, "trend", None) is None:
            return  # no fresh LTF signal → conservative skip
        if pos_side == "long" and ltf.trend != Direction.BEARISH:
            return
        if pos_side == "short" and ltf.trend != Direction.BULLISH:
            return

        # Fee buffer: same convention as MFE-lock / SL-to-BE — a hair past
        # entry on the profit side. LONG sells slightly above entry,
        # SHORT buys slightly below.
        fee_buffer = entry * float(cfg.execution.sl_be_offset_pct)
        limit_px = entry + sign * fee_buffer

        try:
            order_id = await asyncio.to_thread(
                self.ctx.monitor.place_be_recovery_limit,
                symbol, pos_side, limit_px, cfg.execution.margin_mode,
            )
        except Exception:
            logger.exception(
                "mae_be_lock_dispatch_failed symbol={} side={}",
                symbol, pos_side,
            )
            return
        if order_id:
            logger.info(
                "mae_be_lock_fired symbol={} side={} entry={:.4f} "
                "limit_px={:.4f} cur_r={:.3f} mae_low={:.3f}",
                symbol, pos_side, entry, limit_px, cur_r, mae_low,
            )

    async def _maybe_close_on_momentum_fade(
        self, symbol: str, pos_side: str, state: MarketState,
        candles: Optional[list] = None,
    ) -> bool:
        """Weakening-momentum defensive close (Phase A.8, 2026-05-02).

        Each call:
          1. Compute the directional confluence score for the position's
             direction via `score_direction(state, pos_direction, ...)`.
             Same scoring path as the entry-side plan-builder, but for one
             direction only (the position's). Cheap CPU; no TV/Bybit calls.
          2. Append the score to `_Tracked.recent_confluence_history`
             (truncated to `weakening_max_history`).
          3. If history has >= `weakening_min_cycles` entries AND every
             step-to-step delta is at least `weakening_min_score_drop`
             (monotonic decline) AND `mfe_r_high >= weakening_min_mfe_r`
             (only close from profit, not MAE), fire a defensive close
             with the `momentum_fade` reason → journal stamps
             `EARLY_CLOSE_MOMENTUM_FADE`.

        Returns True when a close was fired, False otherwise. Caller uses
        the return to short-circuit downstream gates (no point running
        TP-revise on a position we just closed).
        """
        cfg = self.ctx.config
        if not cfg.execution.weakening_exit_enabled:
            return False
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side)
        if snap is None:
            return False
        pos_direction = (
            Direction.BULLISH if pos_side == "long" else Direction.BEARISH
        )
        try:
            score_obj = score_direction(
                state, pos_direction,
                ltf_candles=candles,
                allowed_sessions=cfg.allowed_sessions_for(symbol) or None,
                ltf_state=self.ctx.ltf_cache.get(symbol),
                htf_state=self.ctx.htf_state_cache.get(symbol),
                weights=cfg.analysis.confluence_weights or None,
                min_rsi_mfi_magnitude=cfg.analysis.min_rsi_mfi_magnitude,
                liquidity_pool_max_atr_dist=(
                    cfg.analysis.liquidity_pool_max_atr_dist
                ),
                displacement_atr_mult=cfg.analysis.displacement_atr_mult,
                displacement_max_bars_ago=(
                    cfg.analysis.displacement_max_bars_ago
                ),
                divergence_fresh_bars=cfg.analysis.divergence_fresh_bars,
                divergence_decay_bars=cfg.analysis.divergence_decay_bars,
                divergence_max_bars=cfg.analysis.divergence_max_bars,
                trend_regime=None,  # regime modifiers stay off here — we
                # want raw directional alignment trajectory, not regime-
                # weighted (which biases the trend over time).
                trend_regime_conditional_scoring_enabled=False,
            )
        except Exception:
            logger.exception(
                "weakening_score_compute_failed symbol={} side={}",
                symbol, pos_side,
            )
            return False
        score_now = float(score_obj.score)
        self.ctx.monitor.append_confluence_score(
            symbol, pos_side, score_now,
            int(cfg.execution.weakening_max_history),
        )
        # Re-fetch tracked-runner snap so history reflects the just-appended
        # value (the previous snap was a frozen tuple).
        snap = self.ctx.monitor.get_tracked_runner(symbol, pos_side) or {}
        history = list(snap.get("recent_confluence_history") or ())
        min_cycles = int(cfg.execution.weakening_min_cycles)
        if len(history) < min_cycles:
            return False
        # Only inspect the last `min_cycles` entries — once the run starts,
        # we don't want a brief mid-trade dip that recovered to lock us in
        # forever waiting for fresh decline.
        recent = history[-min_cycles:]
        drop_threshold = float(cfg.execution.weakening_min_score_drop)
        for i in range(1, len(recent)):
            if recent[i - 1] - recent[i] < drop_threshold:
                # Step-to-step drop didn't clear the threshold → not a
                # confirmed weakening pattern.
                return False
        # Profitability gate — operator-described "kar bölgesinde" exit.
        # MFE check uses peak-favorable, not current PnL, so a brief MFE
        # spike without retest still counts (we're close-by-default once
        # we've seen good profits AND a fading signal).
        mfe_r_high = float(snap.get("mfe_r_high") or 0.0)
        if mfe_r_high < float(cfg.execution.weakening_min_mfe_r):
            return False
        logger.info(
            "weakening_exit_fired symbol={} side={} history={} "
            "mfe_r_high={:.3f}",
            symbol, pos_side, recent, mfe_r_high,
        )
        await self._defensive_close(symbol, pos_side, "momentum_fade")
        return True

    async def _maybe_close_on_ha_flip(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> bool:
        """HA-native pozisyon için multi-TF HA renk dönüşü exit gate.

        Operatör 2026-05-04 spec'i (Yol A primary mode'un simetrik tarafı):
        HA color primary direction signal, multi-TF (3m streak + 15m
        opposing) + RCS (volume_3m_ratio) confirm + whipsaw guard. Sadece
        `_Tracked.is_ha_native=True` olan pozisyonlar üzerinde fire eder;
        legacy 5-pillar pozisyonlar mevcut momentum_fade / MAE-BE-recovery
        / trailing exit'lerini korur (kademeli geçiş — yeni gate eski
        gate'leri ezmez, sadece HA-native trade'lere uygulanır).

        Decision flow (saf fonksiyon `evaluate_ha_native_exit`):
          1. config.ha_native_exit_enabled toggle.
          2. ha_state.latest var mı (boş history → HOLD).
          3. bars_since_open >= min_bars_held (whipsaw guard ilk cycle).
          4. 3m streak opposing (≥ min_opposing_bars_3m) VEYA
             15m HA color opposing (tek bar yeterli).
          5. RCS gate: volume_3m_ratio ≥ confirm → CLOSE,
             ≤ noise → HOLD, arada → CLOSE (default exit fires).

        Returns True when a defensive close was fired, False otherwise.
        Caller (runner cycle) returns early when True so subsequent gates
        (TP-revise, MFE-lock, MAE-recovery) don't fire on a closing
        position.
        """
        cfg = self.ctx.config
        if not cfg.execution.ha_native_exit_enabled:
            return False

        tracked = self.ctx.monitor.get_tracked(symbol, pos_side)
        if tracked is None or not getattr(tracked, "is_ha_native", False):
            return False

        sym_state = self.ctx.ha_state_registry.get(symbol)
        if sym_state is None or sym_state.latest is None:
            return False

        pos_dir = (
            Direction.BULLISH if pos_side == "long" else Direction.BEARISH
        )

        # bars_since_open: 3m bar bazında (180s/bar). opened_at threading
        # `register_open` üzerinden kuruldu; rehydrate path'i de aynı
        # `entry_timestamp` kullanır → restart sonrası tutarlı.
        try:
            elapsed_s = (
                _utc_now() - tracked.opened_at
            ).total_seconds()
        except Exception:
            elapsed_s = 0.0
        bars_held = max(0, int(elapsed_s // 180))

        exit_ctx = HANativeExitContext(
            position_direction=pos_dir,
            ha_state=sym_state,
            bars_since_open=bars_held,
            volume_3m_ratio=float(state.signal_table.volume_3m_ratio or 1.0),
        )
        exit_cfg = HANativeExitConfig(
            enabled=cfg.execution.ha_native_exit_enabled,
            min_opposing_bars_3m=cfg.execution.ha_native_exit_min_opposing_bars_3m,
            enable_15m_opposing=cfg.execution.ha_native_exit_enable_15m_opposing,
            rcs_volume_ratio_confirm=cfg.execution.ha_native_exit_rcs_confirm,
            rcs_volume_ratio_noise=cfg.execution.ha_native_exit_rcs_noise,
            min_bars_held=cfg.execution.ha_native_exit_min_bars_held,
        )
        decision = evaluate_ha_native_exit(exit_ctx, exit_cfg)

        if not decision.should_close:
            return False

        logger.info(
            "ha_flip_exit_fired symbol={} side={} reason={} bars_held={} "
            "vol_ratio={:.2f}",
            symbol, pos_side, decision.reason, bars_held,
            exit_ctx.volume_3m_ratio,
        )
        await self._defensive_close(symbol, pos_side, "ha_flip_reversal")
        return True

    async def _maybe_close_on_counter_reversal(
        self, symbol: str, pos_side: str, state: MarketState,
    ) -> bool:
        """2026-05-05 — Faz 7: "İp üstündeki cambaz" exit (operatör spec).

        Pozisyondayken yeni cycle'da Major Reversal sinyali pozisyonun
        TERSİ yönde (ve yeterince güçlü skorla) gelirse → hemen close.
        Bir sonraki cycle yeni yönde entry alabilir → hızlı yön değişimi.

        Çift taraflı kazanç hedefi: trend dönüşünü kaçırmadan yakala.
        Mevcut HA-flip exit gate'inden ÖNCE çalışır (öncelik): yapısal
        Major Reversal sinyali HA-flip'ten daha güvenilir.

        Şartlar (hepsi pass = close):
          * Pozisyon HA-native (`is_ha_native=True`)
          * Yeni `evaluate_entry()` çağrısı:
              - decision.entry_path == "major_reversal"
              - decision.direction != current_pos_direction
              - decision.major_reversal_score ≥ counter_reversal_score_min
                (sıkı eşik 5.0 — chop'tan korunmak için)
          * config.counter_reversal_exit_enabled

        Returns True when close fired.
        """
        cfg = self.ctx.config
        if not getattr(
            cfg.execution, "counter_reversal_exit_enabled", True,
        ):
            return False

        tracked = self.ctx.monitor.get_tracked(symbol, pos_side)
        if tracked is None or not getattr(tracked, "is_ha_native", False):
            return False

        # Yeni evaluate_entry çağrısı — context inşa et
        try:
            decision = await self._evaluate_ha_native_entry(symbol, state)
        except Exception:
            logger.exception(
                "counter_reversal_evaluate_failed symbol={}", symbol,
            )
            return False
        if decision is None:
            return False

        # Sadece Major Reversal sinyali + ters yön + sıkı skor eşiği
        if getattr(decision, "entry_path", None) != "major_reversal":
            return False
        pos_dir = (
            Direction.BULLISH if pos_side == "long" else Direction.BEARISH
        )
        if decision.direction == pos_dir:
            return False  # aynı yön — counter değil
        score_min = getattr(
            cfg.execution, "counter_reversal_score_min", 5.0,
        )
        if decision.major_reversal_score < score_min:
            return False

        logger.info(
            "counter_reversal_exit_fired symbol={} side={} new_dir={} "
            "score={:.2f} threshold={:.2f}",
            symbol, pos_side,
            decision.direction.value if decision.direction else "?",
            decision.major_reversal_score, score_min,
        )
        await self._defensive_close(
            symbol, pos_side, "counter_reversal_signal",
        )
        return True

    def _open_position_direction(self, symbol: str) -> Optional[Direction]:
        """Direction of the OPEN position on `symbol`, or None.

        Reads `ctx.open_trade_ids`, keyed by (inst_id, pos_side). pos_side
        is the Bybit-style 'long' / 'short' string captured at register-open
        time. None means no open position on the symbol.
        """
        for (sym, pos_side) in self.ctx.open_trade_ids.keys():
            if sym != symbol:
                continue
            side = (pos_side or "").lower()
            if side == "long":
                return Direction.BULLISH
            if side == "short":
                return Direction.BEARISH
        return None

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

    def _ha_native_record_kwargs(
        self,
        plan: TradePlan,
        state: Optional[MarketState],
        decision: Optional[Any] = None,
    ) -> dict:
        """2026-05-04 — Build the HA-native journal-field kwargs for
        `record_open`. Returns a dict with `is_ha_native` boolean +
        7 HA snapshot fields read off `state.signal_table` at entry-time.

        is_ha_native logic: `plan.reason` carries the `ha_native:` prefix
        when `_build_ha_native_trade_plan` produced the plan (HA-native
        primary mode OVERRIDE block). Legacy 5-pillar plans land here
        with `is_ha_native=False` so Pass 3 GBT can segment by entry
        strategy instead of inferring from a free-text reason field.

        State guard: `state is None` (pending-fill rehydrate path doesn't
        thread state) → all HA fields None; is_ha_native still derived
        from plan.reason. State present → HA snapshot fields populated
        from `state.signal_table`. None / 0.0 defaults are kept on the
        write side (downstream Pass 3 reads NULLs via _safe_col).

        2026-05-05 — Faz 6 ek: decision parametresi 3-tip dispatcher
        çıktısını taşır (entry_path + skor'lar + mss_break_detected +
        target_rr + risk_multiplier). NOT NULL DB constraint sebebiyle
        tüm field'lar default değerlerle dolu döner.
        """
        is_ha_native = bool(
            plan.reason and plan.reason.startswith("ha_native:")
        )
        kwargs: dict = {"is_ha_native": is_ha_native}

        # 2026-05-05 — Faz 6/8 dispatcher fields. Operatör 2026-05-05
        # düzeltme: 0.0 fallback YANLIŞ — gerçek 0.0 score (mandatory
        # fail) NULL ile karışıyor. None geçerse DB'ye NULL yazılır
        # (semantik: "veri yok"). decision varsa runner gerçek değerleri
        # yazar; pending-fill rehydrate path'i sadece entry_path parse
        # eder (plan.reason'dan), diğer alanlar NULL kalır.
        if decision is not None:
            entry_path = getattr(decision, "entry_path", None)
            mss_results = (
                getattr(decision, "gate_results", {}) or {}
            ).get(entry_path or "", {}) if entry_path else {}
            mss_break = bool(
                mss_results.get("mss_direction_aligned")
                or mss_results.get("mss_direction_main")
            ) if mss_results else None
            kwargs.update({
                "entry_path": entry_path,
                "major_reversal_score": getattr(
                    decision, "major_reversal_score", None,
                ),
                "continuation_score": getattr(
                    decision, "continuation_score", None,
                ),
                "micro_reversal_score": getattr(
                    decision, "micro_reversal_score", None,
                ),
                "mss_break_detected": mss_break,
                "target_rr_ratio_at_entry": getattr(
                    decision, "target_rr", None,
                ),
                "risk_multiplier_at_entry": getattr(
                    decision, "risk_multiplier", None,
                ),
            })
        else:
            # Pending-fill rehydrate — plan.reason'dan entry_path parse
            # ("ha_native:<entry_path>:<reason>" format). Diğer field'lar
            # decision snapshot olmadan rehidrate edilemez → NULL.
            entry_path = None
            if plan.reason and plan.reason.startswith("ha_native:"):
                parts = plan.reason.split(":", 2)
                if len(parts) >= 2:
                    entry_path = parts[1] or None
            kwargs.update({
                "entry_path": entry_path,
                "major_reversal_score": None,
                "continuation_score": None,
                "micro_reversal_score": None,
                "mss_break_detected": None,
                "target_rr_ratio_at_entry": None,
                "risk_multiplier_at_entry": None,
            })

        if state is None or state.signal_table is None:
            return kwargs
        sig = state.signal_table
        kwargs.update({
            "ha_color_3m_at_entry": sig.ha_color_3m or None,
            "ha_color_15m_at_entry": sig.ha_color_15m or None,
            "ha_streak_3m_at_entry": sig.ha_streak_3m,
            "ha_streak_15m_at_entry": sig.ha_streak_15m,
            "ha_body_pct_3m_at_entry": (
                sig.ha_body_pct_3m if sig.ha_body_pct_3m else None
            ),
            "ema200_3m_at_entry": (
                sig.ema200_3m if sig.ema200_3m else None
            ),
            "volume_3m_ratio_at_entry": (
                sig.volume_3m_ratio if sig.volume_3m_ratio else None
            ),
        })
        return kwargs

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

    # Pure helper lives in src/strategy/what_if_sltp.py — `scripts/
    # backfill_proposed_sl_tp.py` shares it for retroactive backfill of
    # pre-Pass-2.5 reject rows. Same math both paths so live-insert
    # outcomes and backfilled outcomes don't split spuriously by vintage.
    _NO_PROPOSED_SLTP_REASONS = NO_PROPOSED_SLTP_REASONS

    def _compute_what_if_proposed_sltp(
        self,
        *,
        symbol: str,
        state: MarketState,
        conf,
        reject_reason: str,
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """ATR-based what-if SL/TP for pre-fill rejects (Pass 2.5).

        Thin wrapper that pulls per-symbol floor + target_rr from config
        and delegates to the pure helper. See
        src/strategy/what_if_sltp.compute_what_if_proposed_sltp for the
        actual SL/TP math contract.
        """
        cfg = self.ctx.config
        return compute_what_if_proposed_sltp(
            symbol=symbol,
            direction=getattr(conf, "direction", Direction.UNDEFINED),
            price=state.current_price,
            atr=state.atr,
            reject_reason=reject_reason,
            floor_pct=cfg.min_sl_distance_pct_for(symbol),
            target_rr=cfg.execution.target_rr_ratio,
        )

    async def _record_reject(
        self,
        *,
        symbol: str,
        reject_reason: str,
        state: MarketState,
        conf,
        candles: Optional[list] = None,
        proposed_sl_price: Optional[float] = None,
        proposed_tp_price: Optional[float] = None,
        proposed_rr_ratio: Optional[float] = None,
        adx_3m_result: Optional[TrendRegimeResult] = None,
        adx_15m_result: Optional[TrendRegimeResult] = None,
    ) -> None:
        """Persist a reject to `rejected_signals` (Phase 7.B1).

        Caller is responsible for try/except around this — any DB issue
        must never block the main cycle (reject logging is observational).
        All snapshot fields default to None so partial data is fine.

        2026-04-29 Pass 2.5: when caller doesn't pass `proposed_*` (the
        pre-fill reject path), runs `_compute_what_if_proposed_sltp` to
        derive ATR-based what-if SL/TP. Pending-cancel caller passes the
        original `plan.sl_price/tp_price/rr_ratio` directly (more accurate
        than what-if since the limit was actually placed at those levels).
        """
        if proposed_sl_price is None and proposed_tp_price is None:
            proposed_sl_price, proposed_tp_price, proposed_rr_ratio = (
                self._compute_what_if_proposed_sltp(
                    symbol=symbol, state=state, conf=conf,
                    reject_reason=reject_reason,
                )
            )
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
            # 2026-04-29 Pass 2.5 — counter-factual targets. Caller-provided
            # (pending-cancel path) or auto-computed via what-if helper above.
            proposed_sl_price=proposed_sl_price,
            proposed_tp_price=proposed_tp_price,
            proposed_rr_ratio=proposed_rr_ratio,
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
            # pillar_btc_bias / pillar_eth_bias dropped 2026-05-05 v3
            # (legacy cross-asset open-position lock retired with Yol A
            # cleanup). Schema columns kept on RejectedSignal for backward
            # compat; just stop writing.
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
            # 2026-05-02 — Phase A.9 ADX numeric capture (entry TF + HTF).
            **_adx_triad_kwargs("3m", adx_3m_result),
            **_adx_triad_kwargs("15m", adx_15m_result),
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

        2026-05-02 — Phase A.10 maker-first close. When
        `execution.defensive_close_use_maker=true` (default), tries to place
        a post-only reduce-only LIMIT just outside the spread first
        (ask+N*tick for long-close, bid-N*tick for short-close). On post-only
        reject / placement failure / book-quote unavailable, falls back to
        the legacy `close_position()` market reduce. The maker LIMIT is
        timeout-cancelled by `_finalize_expired_defensive_closes()` in
        run_once if it doesn't fill within
        `defensive_close_maker_timeout_s`.
        """
        key = (symbol, side)
        if key in self.ctx.defensive_close_in_flight:
            return
        self.ctx.defensive_close_in_flight.add(key)

        # 2026-05-02 — close_reason set BEFORE any close attempt so the
        # journal stamping path has the reason regardless of which leg
        # (maker LIMIT fill vs market fallback) actually closes.
        close_reason_map = {
            "ltf_reversal": "EARLY_CLOSE_LTF_REVERSAL",
            "momentum_fade": "EARLY_CLOSE_MOMENTUM_FADE",
        }
        close_reason = close_reason_map.get(
            reason, f"EARLY_CLOSE_{reason.upper()}"
        )
        self.ctx.pending_close_reasons[key] = close_reason

        cfg_exec = self.ctx.config.execution
        # Phase A.10 maker-first attempt. Skipped on master-toggle off; falls
        # through to legacy market reduce.
        if cfg_exec.defensive_close_use_maker:
            placed = await self._try_place_defensive_maker_limit(
                symbol, side, cfg_exec,
            )
            if placed:
                logger.info(
                    "defensive_close_triggered symbol={} side={} reason={} "
                    "close_reason={} mode=maker_limit",
                    symbol, side, reason, close_reason,
                )
                return  # market fallback fires only on timeout

        # Legacy / fallback: market reduce. On Bybit V5 the position-attached
        # TP/SL clears automatically when the position closes — no separate
        # algo cancel needed.
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

        logger.info("defensive_close_triggered symbol={} side={} reason={} "
                    "close_reason={} mode=market",
                    symbol, side, reason, close_reason)

    async def _try_place_defensive_maker_limit(
        self, symbol: str, side: str, cfg_exec,
    ) -> bool:
        """Phase A.10 — try to place a post-only reduce-only LIMIT for a
        defensive close. Returns True on placement success (caller skips
        market fallback), False on any failure (caller market-closes
        immediately). Failure modes:
          * book quote unavailable (zero bid/ask)
          * tick_size unknown
          * post-only reject (110047) or other Bybit error
        """
        try:
            bid, ask, _mark = await asyncio.to_thread(
                self.ctx.bybit_client.get_top_book, symbol,
            )
        except Exception:
            logger.exception(
                "defensive_close_top_book_failed symbol={} side={} — "
                "falling back to market", symbol, side,
            )
            return False
        if bid <= 0 or ask <= 0:
            return False
        try:
            spec = await asyncio.to_thread(
                self.ctx.bybit_client.get_instrument_spec, symbol,
            )
        except Exception:
            logger.exception(
                "defensive_close_spec_failed symbol={} side={} — falling "
                "back to market", symbol, side,
            )
            return False
        tick = float(spec.get("tick_size") or 0.0)
        if tick <= 0:
            return False
        offset_ticks = max(1, int(cfg_exec.defensive_close_maker_offset_ticks))
        # LONG close = SELL above ask. SHORT close = BUY below bid.
        # Both placements are guaranteed post-only valid (above ask /
        # below bid is on the maker side of the book by definition).
        if side == "long":
            limit_px = ask + tick * offset_ticks
        else:
            limit_px = bid - tick * offset_ticks
            if limit_px <= 0:
                return False
        try:
            order_id = await asyncio.to_thread(
                self.ctx.monitor.place_defensive_close_maker_limit,
                symbol, side, limit_px,
                int(cfg_exec.defensive_close_maker_timeout_s),
            )
        except Exception:
            logger.exception(
                "defensive_close_maker_place_exception symbol={} side={}",
                symbol, side,
            )
            return False
        return bool(order_id)

    async def _finalize_expired_defensive_closes(self) -> None:
        """Phase A.10 (2026-05-02). Run at the start of every `run_once`.

        For each tracked position whose defensive-close maker LIMIT has
        passed its deadline without filling: cancel the limit, then call
        `close_position()` market reduce as fallback. The next
        `_process_closes()` drain in the same cycle picks up the resulting
        CloseFill (close_reason was already stamped in
        `pending_close_reasons` at original `_defensive_close()` time).

        No-op when nothing is in flight. Best-effort on cancel + market —
        any exchange error is logged and `defensive_close_in_flight` stays
        set so the runner doesn't re-fire defensive logic against the same
        position before its close is observed.
        """
        try:
            expired = self.ctx.monitor.iter_expired_defensive_close_limits()
        except Exception:
            logger.exception("iter_expired_defensive_close_limits_failed")
            return
        for inst_id, pos_side, order_id in expired:
            logger.info(
                "defensive_close_maker_timeout inst={} side={} ord={} — "
                "cancel + market fallback", inst_id, pos_side, order_id,
            )
            try:
                await asyncio.to_thread(
                    self.ctx.bybit_client.cancel_order, inst_id, order_id,
                )
            except Exception as exc:
                # Already-gone codes are fine (the limit may have just
                # filled in a race with the deadline check). Other errors
                # are logged but we still attempt the market fallback —
                # if the limit DID just fill, market-reduce on a closed
                # position returns empty + the next poll surfaces the
                # close normally.
                logger.warning(
                    "defensive_close_maker_cancel_failed inst={} side={} "
                    "ord={} err={!r} — proceeding with market fallback",
                    inst_id, pos_side, order_id, exc,
                )
            try:
                await asyncio.to_thread(
                    self.ctx.bybit_client.close_position, inst_id, pos_side,
                )
            except Exception:
                logger.exception(
                    "defensive_close_market_fallback_failed inst={} side={}",
                    inst_id, pos_side,
                )
            # Clear monitor state regardless — if market fallback failed,
            # the next cycle's poll either sees the position still open
            # (defensive_close_in_flight prevents re-firing maker LIMIT)
            # or sees it closed and emits CloseFill normally.
            self.ctx.monitor.clear_defensive_close_state(inst_id, pos_side)

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

        Legacy single-table version. Production switch paths now use
        `_wait_for_pine_settle_dual()` which polls both Signals + Oscillator
        last_bar beacons (Operatör 2026-05-04 dinamik settle). Kept here for
        backward compat with callers that only need the single-table check.
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

    async def _read_last_bars(self) -> tuple[Optional[int], Optional[int]]:
        """Lightweight freshness probe — Pine tables ONLY, parse last_bar.

        Operatör 2026-05-04: önceki implementation `read_market_state()`
        çağırıyordu — bu 5 paralel node subprocess fırlatıyor (tables +
        labels + boxes + lines + status), polling sırasında %80'i çöp.
        Şimdi sadece `get_pine_tables()` (1 subprocess), parse, last_bar
        çıkarımı. Polling overhead'i %80 azalır; cycle başına ~100s tasarruf.

        Tests without a bridge (or with a recording-only bridge that lacks
        `get_pine_tables`) fall back to the legacy `reader.read_market_state()`
        path so the freshness invariants the test suite checks still trigger.

        Returns (signals_lb, oscillator_lb) tuple. Either side may be None
        when that table isn't present (first boot, Pine version mismatch,
        fake reader in tests).
        """
        bridge = self.ctx.bridge
        # Fast path — production bridge with get_pine_tables. One subprocess
        # (or one daemon round-trip) instead of full read_market_state.
        if bridge is not None and hasattr(bridge, "get_pine_tables"):
            try:
                tables = await bridge.get_pine_tables()
                from src.data.structured_reader import (
                    parse_oscillator_table,
                    parse_signal_table,
                )
                sig_table = parse_signal_table(tables)
                osc_table = parse_oscillator_table(tables)
                sig_lb = sig_table.last_bar if sig_table else None
                osc_lb = osc_table.last_bar if osc_table else None
                return (sig_lb, osc_lb)
            except Exception:
                # Fall through to reader-based fallback if the bridge call
                # blew up (mock bridge in tests, transient daemon error, etc).
                pass
        # Fallback — read_market_state via the reader. Heavier but covers
        # test mocks and any environment where the bridge is unavailable.
        try:
            state = await self.ctx.reader.read_market_state()
            sig_lb = state.signal_table.last_bar if state.signal_table else None
            osc_lb = state.oscillator.last_bar if state.oscillator else None
            return (sig_lb, osc_lb)
        except Exception:
            return (None, None)

    async def _wait_for_pine_settle_dual(
        self,
        baselines: tuple[Optional[int], Optional[int]],
        max_wait_s: Optional[float] = None,
    ) -> bool:
        """Poll until Pine tables show fresh data (data-ready check).

        Operatör 2026-05-04 v3: önceki "last_bar baseline comparison" mantığı
        bozuktu — TV chart'ın `bar_index` (= last_bar) sembol switch'te aynı
        kalır (her sembol aynı bar count'lu history yükler), TF switch'te
        de yeni bar print etmedikçe değişmez. Polling baseline-eşleşmesini
        ASLA göremedi → her seferinde timeout fire etti.

        Yeni mantık: tablo doluluk check. Pine yeni sembol/TF için render
        bitirdiğinde `signal_table.price` ve `last_bar` populate olur; bot
        bunu doğrudan görür ve exit eder. Tipik gerçek timing'ler:
          - Symbol switch:  set_symbol 10s + Pine settle ~100ms = ~10.1s
          - TF switch:      set_timeframe 1.5-2.5s + settle ~100ms = ~1.6-2.6s
        Bot bu süreleri set_*'in `await`'inde zaten harcıyor; dual-poll
        artık ek 100-300ms eklemeli, tüm timeout'u değil.

        Args:
            baselines: (signals_lb, osc_lb) — IGNORED (legacy compat).
            max_wait_s: override config default for slow Pine cold starts.

        Returns True on data-ready, False on timeout.
        """
        cfg = self.ctx.config.trading
        wait_s = max_wait_s if max_wait_s is not None else cfg.pine_settle_max_wait_s
        deadline = time.monotonic() + wait_s
        bridge = self.ctx.bridge
        use_bridge = bridge is not None and hasattr(bridge, "get_pine_tables")

        while time.monotonic() < deadline:
            try:
                if use_bridge:
                    tables = await bridge.get_pine_tables()
                    from src.data.structured_reader import (
                        parse_oscillator_table,
                        parse_signal_table,
                    )
                    sig = parse_signal_table(tables)
                    osc = parse_oscillator_table(tables)
                else:
                    # Test fallback — no bridge or recording-only mock.
                    state = await self.ctx.reader.read_market_state()
                    sig = state.signal_table
                    osc = state.oscillator
                # Operatör 2026-05-04 v4: data-ready check sıkılaştırması.
                # Eski (price + last_bar) yetersizdi — Pine yarı render
                # durumda price doldurmuş ama HA / ATR boş olabiliyor.
                # Şimdi: HA color'lar + ATR dolu olmadan asla "ready" deme.
                # Bot eski kısmi veriyle bir sonraki TF/sembole geçmesin.
                if (
                    sig is not None
                    and sig.last_bar is not None
                    and sig.price > 0
                    and sig.atr_14 > 0
                    and sig.ha_color_3m  # truthy = non-empty string
                    and sig.ha_color_15m
                    and osc is not None
                    and osc.last_bar is not None
                ):
                    return True
            except Exception:
                pass  # transient bridge error — keep polling
            await asyncio.sleep(cfg.pine_settle_poll_interval_s)
        return False

    async def _switch_timeframe(self, tf: str) -> bool:
        """Switch chart TF then dual-table poll until BOTH tables fresh.

        Operatör 2026-05-04 dinamik settle: `tf_settle_seconds` sabit sleep +
        `pine_post_settle_grace_s` kaldırıldı; Signals + Oscillator dual-poll
        verileri gelir gelmez bir sonraki TF'e geçer. Hızlı yüklenenlerde
        ~0.3-1.0s'de döner; yavaşlarda `pine_settle_max_wait_s` timeout.

        Short-circuit: TV zaten istenen TF'deyse `set_timeframe` no-op'tur,
        Pine yeniden render etmez ve dual-poll değişikliği gözleyemez —
        burada erken success.

        Returns True on dual-fresh, False on bridge error / timeout.
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
        # Capture pre-switch dual baselines for the freshness poll.
        baselines = await self._read_last_bars()
        try:
            await self.ctx.bridge.set_timeframe(tf)
        except Exception:
            logger.exception("set_timeframe_failed tf={}", tf)
            return False
        # Floor — Pine needs ~200-300ms to start re-rendering after the TF
        # switch; without this gap the polling loop reads the same baseline
        # immediately and the dual-poll burns the full timeout despite Pine
        # being mid-render. Same pattern as set_symbol path (2026-05-04 fix).
        await asyncio.sleep(0.3)
        # Dynamic settle — dual-poll exits as soon as both tables reflect
        # the new TF (Operatör 2026-05-04 dinamik settle).
        return await self._wait_for_pine_settle_dual(baselines)

    def _compute_btc_eth_open_directions(self) -> tuple[Optional[str], Optional[str]]:
        """2026-05-05 Faz 8: BTC + ETH OPEN-position direction'larını
        decision_log'a yazmak için compute eder. None = no open position.

        Cross-asset open-position lock'un audit görünümü — Pass 3 GBT
        bu segmentation ile "BTC long açıkken ETH short açtı mı?" gibi
        pattern'leri öğrenir.
        """
        btc_dir: Optional[str] = None
        eth_dir: Optional[str] = None
        for (sym, pos_side) in self.ctx.open_trade_ids.keys():
            side_str = "BULLISH" if (pos_side or "").lower() == "long" else "BEARISH"
            if sym == "BTC-USDT-SWAP" and btc_dir is None:
                btc_dir = side_str
            elif sym == "ETH-USDT-SWAP" and eth_dir is None:
                eth_dir = side_str
        return btc_dir, eth_dir

    async def _compute_first_entry_missed_for(
        self, symbol: str, ha_state: Any,
    ) -> bool:
        """2026-05-05 — Tip 2 Continuation skoru için "kaçırılmış trend" flag.

        Tanım: 3m HA dominant_color son K=30 bar same direction (yani
        belirgin bir trend var) AND bot bu trend penceresi içinde bu
        sembol+yön için `is_ha_native=True` trade almadı → flag=True
        (kaçırıldı, +0.5 skor katkısı).

        Implementation:
          * Dominant trend'in başlangıç ts'i kabaca: now - 30 × 3m =
            now - 90 dk (default dominant_color_window). Daha hassas
            timestamp tracking gereksiz; window pencere yeterli.
          * Journal'da `is_ha_native=1 AND symbol=? AND direction=?
            AND entry_timestamp >= cutoff` → varsa flag=False.
          * Dominant_color None (chop) → flag=False (Tip 2 zaten reject olur).
        """
        try:
            dominant = ha_state.dominant_color_3m()
        except Exception:
            return False
        if dominant not in ("GREEN", "RED"):
            return False  # chop / belirsiz → flag relevant değil
        direction = "BULLISH" if dominant == "GREEN" else "BEARISH"
        # Trend window: 30 bar × 3m = 90 dakika (varsayılan)
        cutoff = _utc_now() - timedelta(minutes=90)
        try:
            conn = self.ctx.journal._require_conn()
            row = await (await conn.execute(
                "SELECT 1 FROM trades "
                "WHERE symbol = ? AND direction = ? "
                "AND is_ha_native = 1 "
                "AND entry_timestamp >= ? "
                "LIMIT 1",
                (symbol, direction, cutoff.isoformat()),
            )).fetchone()
            return row is None  # row yok → kaçırıldı
        except Exception:
            # Journal henüz hazır değil veya schema mismatch → güvenli
            # default flag=False (Continuation'a +0.5 vermez).
            return False

    async def _evaluate_ha_native_entry(
        self, symbol: str, market_state: MarketState,
        confluence: Optional[ConfluenceScore] = None,
    ) -> Any:
        """Run HA-native planner + write decision_log row + return decision.

        `confluence` (operatör 2026-05-05 v4): caller-precomputed
        ConfluenceScore. HA-native dispatcher kendi soft skoruna aligned
        bonus / opposing penalty / strong extra olarak entegre eder.
        Mandatory gate'lere etki etmez. None → tarafsız (no impact).

        Operatör 2026-05-04 HA-native runner integration. Yol A primary mode
        için sonuç döndürür: caller `EntryDecision.is_take` kontrol eder ve
        HA-native plan ile TradePlan oluşturur. Yol B (audit-only) caller
        ise sonucu yok sayar; decision_log her durumda yazılır.

        Returns:
            EntryDecision | None — None on any internal failure (hata
            log'lanır, runner cycle bypass eder). Caller None'ı NO_ACTION
            gibi davranır.

        Failure tolerated: state pump henüz yapılmadıysa veya planner
        başarısız olursa None döner.
        """
        try:
            from src.data.models import Direction
            from src.strategy.ha_native_planner import (
                EntryContext,
                HANativeConfig,
                evaluate_entry,
            )

            ha_state = self.ctx.ha_state_registry.get(symbol)
            if ha_state is None:
                return None  # state pump henüz çalışmadı bu sembol için

            sig = market_state.signal_table

            # MSS direction parse: "BULLISH@69450" → Direction.BULLISH
            last_mss_dir: Optional[Direction] = None
            if sig.last_mss:
                upper = sig.last_mss.upper()
                if "BULL" in upper:
                    last_mss_dir = Direction.BULLISH
                elif "BEAR" in upper:
                    last_mss_dir = Direction.BEARISH

            # Best bid/ask from Bybit ticker (cached snapshot, fast)
            best_bid: Optional[float] = None
            best_ask: Optional[float] = None
            if self.ctx.bybit_client is not None:
                try:
                    bid, ask, _mark = await asyncio.to_thread(
                        self.ctx.bybit_client.get_top_book, symbol,
                    )
                    if bid > 0 and ask > 0:
                        best_bid, best_ask = bid, ask
                except Exception:
                    pass  # ticker fail — gates'le ilgili değil

            # 2026-05-05 v3 — Structural swing anchor for SL.
            # Eski mantık: `sig.liquidity_below` (Pine likidite havuzu) en
            # yakın değer alınıyordu — ama bu structural swing low DEĞİL,
            # sadece nearby liquidity cluster. Operatör spec: "SL son
            # swingin altına eklenmeli; SL'e değerse zaten MSS gelmiş
            # olur" → yapısal swing point gerekli. 3m candle buffer'dan
            # son 20 bar (60 dk) min/max + 0.5×ATR buffer (legacy
            # apply_zone_to_plan ile aynı buffer). Buffer yoksa eski
            # liquidity-pool fallback'i kalsın (degraded mode).
            last_swing_low: Optional[float] = None
            last_swing_high: Optional[float] = None
            try:
                buf_3m = self.ctx.multi_tf.get_buffer("3m")
                if buf_3m is not None and len(buf_3m) >= 5:
                    candles_swing = buf_3m.last(20)
                    if candles_swing:
                        atr = sig.atr_14 if sig.atr_14 > 0 else 0.0
                        buf_dist = 0.5 * atr if atr > 0 else 0.0
                        lows = [c.low for c in candles_swing if c.low > 0]
                        highs = [c.high for c in candles_swing if c.high > 0]
                        if lows:
                            last_swing_low = min(lows) - buf_dist
                        if highs:
                            last_swing_high = max(highs) + buf_dist
            except Exception:
                pass  # buffer hatası — liquidity-pool fallback'e düş
            # Fallback: candle buffer yoksa eski liquidity-pool proxy
            if last_swing_low is None and sig.liquidity_below:
                last_swing_low = max(sig.liquidity_below)
            if last_swing_high is None and sig.liquidity_above:
                last_swing_high = min(sig.liquidity_above)

            # Pending + open pairs from BotContext state
            def _side_to_dir(pos_side: str) -> Direction:
                s = (pos_side or "").lower()
                if s in ("buy", "long", "bullish"):
                    return Direction.BULLISH
                return Direction.BEARISH

            pending_pairs = frozenset(
                (sym, _side_to_dir(side))
                for (sym, side) in self.ctx.pending_setups.keys()
            )
            open_pairs = frozenset(
                (sym, _side_to_dir(side))
                for (sym, side) in self.ctx.open_trade_ids.keys()
            )

            # 2026-05-05 — Faz 2 Continuation skoru için first_entry_missed
            # flag. Cycle başında DB query ile hesaplanır (cache'lenmez —
            # 2 sembol × 1 sorgu = ~10ms ekstra, ihmal edilebilir).
            first_entry_missed = await self._compute_first_entry_missed_for(
                symbol, ha_state,
            )

            ctx = EntryContext(
                symbol=symbol,
                market_state=market_state,
                ha_state=ha_state,
                last_mss_direction=last_mss_dir,
                best_bid=best_bid,
                best_ask=best_ask,
                last_swing_low=last_swing_low,
                last_swing_high=last_swing_high,
                pending_pairs=pending_pairs,
                open_pairs=open_pairs,
                first_entry_missed=first_entry_missed,
                # 2026-05-05 v4 — confluence destek sinyali (caller hesapladı)
                confluence=confluence,
                # adx_3m, plus_di_3m, minus_di_3m, mss_count_recent —
                # not yet wired (sıradaki commit'te ADX cache + MSS density
                # counter eklenecek). Şu an None bırakılır; gate'leri
                # etkilemez (mss_count_recent default 0 → mss_density gate
                # PASS; adx artık planner gate'lerinde değil — kaldırıldı
                # operatör 2026-05-04 revize'sinde).
            )

            ha_cfg = HANativeConfig()  # default knobs
            decision = evaluate_entry(ctx, ha_cfg)

            # Map planner decision → decision_log enum
            if decision.is_take:
                decision_str = "ENTRY_TAKEN"
            elif decision.decision == "REJECT":
                decision_str = "ENTRY_REJECTED"
            else:  # NO_SETUP
                decision_str = "NO_ACTION"

            osc = market_state.oscillator
            session_name = (
                sig.session.value if hasattr(sig.session, "value") else None
            )
            vwap_3m_side = (
                "above" if sig.price > sig.vwap_3m and sig.vwap_3m > 0
                else "below" if sig.vwap_3m > 0
                else None
            )

            # 2026-05-05 — Faz 8: NULL cleanup. Entry-TF (3m) ADX inline
            # hesabı — _run_one_symbol'de ileride yapılan classify_trend_regime
            # call'undan ÖNCE bu fonksiyon çağrıldığı için cache yok. ~5ms
            # ek hesap, decision_log'da Pass 3 GBT için continuous regime
            # features doldurmak değer var.
            adx_3m_val: Optional[float] = None
            plus_di_3m_val: Optional[float] = None
            minus_di_3m_val: Optional[float] = None
            trend_regime_label: Optional[str] = None
            try:
                buf = self.ctx.multi_tf.get_buffer("3m")
                if buf is not None:
                    candles_for_adx = buf.last(50)
                    if candles_for_adx and len(candles_for_adx) >= 15:
                        cfg_analysis = self.ctx.config.analysis
                        regime_result = classify_trend_regime(
                            candles_for_adx,
                            period=cfg_analysis.adx_period,
                            ranging_threshold=(
                                cfg_analysis.trend_regime_ranging_threshold
                            ),
                            strong_threshold=(
                                cfg_analysis.trend_regime_strong_threshold
                            ),
                        )
                        adx_3m_val = float(regime_result.adx)
                        plus_di_3m_val = float(regime_result.plus_di)
                        minus_di_3m_val = float(regime_result.minus_di)
                        regime_obj = regime_result.regime
                        if regime_obj is not None:
                            trend_regime_label = (
                                regime_obj.value if hasattr(regime_obj, "value")
                                else str(regime_obj)
                            )
            except Exception:
                pass  # ADX hesap hatası — NULL kal (gerçek "veri yok")

            btc_open_dir, eth_open_dir = self._compute_btc_eth_open_directions()

            # cycle_id: per-bot-instance monotonic counter
            self._decision_log_cycle_counter = (
                getattr(self, "_decision_log_cycle_counter", 0) + 1
            )
            cycle_id = f"cyc-{self._decision_log_cycle_counter:08d}"

            # confluence_score: 0 valid değer; None sadece sig.confluence
            # gerçekten None ise (Pine'dan gelmezse). Pine her zaman 0+
            # int emit ediyor, yani normalde 0 dolu (gerçek skor 0).
            confluence_val = (
                float(sig.confluence) if sig.confluence is not None else None
            )

            await self.ctx.journal.record_decision_log(
                timestamp=_utc_now(),
                symbol=symbol,
                cycle_id=cycle_id,
                decision=decision_str,
                decision_reason=decision.reason,
                price=sig.price,
                atr_14=sig.atr_14,
                ha_color_1m=sig.ha_color_1m,
                ha_color_3m=sig.ha_color_3m,
                ha_color_15m=sig.ha_color_15m,
                ha_color_4h=sig.ha_color_4h,
                ha_streak_1m=sig.ha_streak_1m,
                ha_streak_3m=sig.ha_streak_3m,
                ha_streak_15m=sig.ha_streak_15m,
                ha_streak_4h=sig.ha_streak_4h,
                ha_no_lower_shadow_3m=sig.ha_no_lower_shadow_3m,
                ha_no_upper_shadow_3m=sig.ha_no_upper_shadow_3m,
                ha_body_pct_3m=sig.ha_body_pct_3m,
                ema200_3m=sig.ema200_3m,
                ha_mfi_1m=osc.ha_mfi_1m,
                ha_mfi_3m=osc.ha_mfi_3m,
                ha_mfi_15m=osc.ha_mfi_15m,
                ha_rsi_1m=osc.ha_rsi_1m,
                ha_rsi_3m=osc.ha_rsi_3m,
                ha_rsi_15m=osc.ha_rsi_15m,
                # 3-bar deltas from runtime ha_state (None if <3 history
                # — gerçek "veri yok" semantiği, NULL kalmasına izin ver).
                mfi_3m_delta_dir=ha_state.mfi_3m_delta_dir,
                rsi_3m_delta_dir=ha_state.rsi_3m_delta_dir,
                mfi_3m_delta_value=ha_state.mfi_3m_delta_value,
                rsi_3m_delta_value=ha_state.rsi_3m_delta_value,
                gate_results=decision.gate_results,
                confluence_score=confluence_val,
                # 2026-05-05 Faz 8: ADX triad + trend regime + open dirs
                adx_3m=adx_3m_val,
                plus_di_3m=plus_di_3m_val,
                minus_di_3m=minus_di_3m_val,
                trend_regime=trend_regime_label,
                btc_open_direction=btc_open_dir,
                eth_open_direction=eth_open_dir,
                session=session_name,
                vwap_3m_side=vwap_3m_side,
                # 2026-05-05 Faz 5: dispatcher fields (per-tip skor +
                # entry_path winner). Decision'dan direkt geçer.
                entry_path=getattr(decision, "entry_path", None),
                major_reversal_score=getattr(
                    decision, "major_reversal_score", None,
                ),
                continuation_score=getattr(
                    decision, "continuation_score", None,
                ),
                micro_reversal_score=getattr(
                    decision, "micro_reversal_score", None,
                ),
            )

            # 2026-05-05 — operatör görsün: 3 tip skor + Major Reversal
            # gate sonuçları (per-cycle özet). Multi-line + tab hizalı.
            # Pass 3 GBT'den önce manuel gözlem için.
            try:
                gate_results = decision.gate_results or {}
                mr_gates = gate_results.get("major_reversal", {}) or {}
                cont_gates = gate_results.get("continuation", {}) or {}

                def _gate_pretty(gates: dict) -> str:
                    """Pass+fail görsel: ✓gate_name ✗gate_name ..."""
                    if not gates:
                        return "(no gates evaluated)"
                    return " ".join(
                        f"{'✓' if v else '✗'}{k}"
                        for k, v in gates.items()
                    )

                mr_summary = _gate_pretty(mr_gates)
                cont_summary = _gate_pretty(cont_gates)
                ms_3m_dir = ha_state.mfi_3m_delta_dir or "?"
                rs_3m_dir = ha_state.rsi_3m_delta_dir or "?"
                streak_3m = sig.ha_streak_3m
                streak_str = f"{streak_3m:+d}" if streak_3m else "0"

                logger.info(
                    "ha_native_decision symbol={} dir={} outcome={}"
                    "\n\tscores  MR={:.2f}/{:.1f}  C={:.2f}/{:.1f}  μR={:.2f}/{:.1f}"
                    "\n\tha      3m={}({})  15m={}  body={:.1f}%  delta=(mfi={} rsi={})"
                    "\n\tMR      {}"
                    "\n\tC       {}"
                    "\n\treason  {}",
                    symbol,
                    decision.direction.value if decision.direction else "?",
                    decision.decision,
                    float(decision.major_reversal_score or 0.0),
                    float(ha_cfg.major_reversal_threshold),
                    float(decision.continuation_score or 0.0),
                    float(ha_cfg.continuation_threshold),
                    float(decision.micro_reversal_score or 0.0),
                    float(ha_cfg.micro_reversal_threshold),
                    sig.ha_color_3m or "?",
                    streak_str,
                    sig.ha_color_15m or "?",
                    float(sig.ha_body_pct_3m or 0.0),
                    ms_3m_dir, rs_3m_dir,
                    mr_summary,
                    cont_summary,
                    decision.reason,
                )
            except Exception:
                logger.debug(
                    "ha_native_decision_log_failed symbol={}", symbol,
                )

            return decision
        except Exception:
            logger.exception("ha_native_audit_failed symbol={}", symbol)
            return None

    def _build_ha_native_trade_plan(
        self,
        decision: Any,  # EntryDecision (avoid circular import in type hint)
        symbol: str,
        cycle_confluence: Optional[ConfluenceScore] = None,
    ) -> Optional[TradePlan]:
        """Build a TradePlan from HA-native EntryDecision (3-tip differansiyel).

        Operatör 2026-05-05 Yol A primary mode: planner 3 entry tipinden
        en yüksek skoru olanı seçer (entry_path = "major_reversal" /
        "continuation" / "micro_reversal"). Her tipin kendine özgü:
          * `target_rr` (1.5 / 1.0 / 0.7)
          * `risk_multiplier` (1.0 / 1.0 / 0.5 — Tip 3 yarı R)
        TradePlan bu per-tip parametreler ile inşa edilir.

        Returns:
            TradePlan if decision.is_take and pricing valid, else None.
        """
        if not decision.is_take:
            return None
        if (
            decision.suggested_entry_price is None
            or decision.suggested_sl_price is None
            or decision.suggested_entry_price <= 0
            or decision.suggested_sl_price <= 0
        ):
            return None

        cfg = self.ctx.config
        # 2026-05-05 — Faz 6: per-tip risk + RR differansiyel.
        # decision.target_rr ve decision.risk_multiplier dispatcher'da
        # entry_path'e göre set edildi (Major Reversal=1.5/1.0,
        # Continuation=1.0/1.0, Micro Reversal=0.7/0.5). risk_amount
        # base'i operatör flat-$ override ($25) — risk_multiplier ile
        # ölçülür: Tip 3'te $12.5, diğerlerinde $25.
        base_risk = cfg.trading.risk_amount_usdt
        risk_amount = base_risk * float(decision.risk_multiplier or 1.0)
        target_rr = float(decision.target_rr or 1.0)

        margin_balance = (
            self.ctx.last_margin_balance
            if self.ctx.last_margin_balance > 0
            else cfg.bot.starting_balance
        )
        contract_size = self.ctx.contract_sizes.get(
            symbol, cfg.trading.contract_size,
        )
        max_leverage = self.ctx.max_leverage_per_symbol.get(
            symbol, cfg.trading.max_leverage,
        )

        # SL source per-tip differansiyel:
        #   major_reversal → "ha_mss_swing" (structural swing SL)
        #   continuation → "ha_mss_swing" (aynı, ana trend swing)
        #   micro_reversal → "ha_micro_swing" (kısa pozisyon, küçük SL)
        entry_path = decision.entry_path or "major_reversal"
        sl_source = (
            "ha_micro_swing" if entry_path == "micro_reversal"
            else "ha_mss_swing"
        )
        # Reason includes entry_path for full traceability + journal segmentation
        plan_reason = f"ha_native:{entry_path}:{decision.reason}"

        # 2026-05-05 v3 — per-symbol min_sl_distance_pct floor (HA-native plan
        # builder). Sub-floor structural SL → notional-için leverage talebi
        # global `trading.max_leverage` cap'ini aşıyor, risk_manager `blocked
        # plan.leverage=N > max_leverage` ile reddediyordu (BTC %0.025 SL →
        # 100x → 75 cap fail). Legacy `entry_signals.build_trade_plan_with_reason`
        # path'inde aynı floor uygulanıyor (entry_signals.py:1180); HA-native
        # bypass ediyordu. Floor'a widen et — risk amount sabit, position size
        # auto-shrinks (risk_amount / sl_pct), TP otomatik recompute (rr_ratio
        # × wider SL distance) `calculate_trade_plan` içinde.
        entry_px = decision.suggested_entry_price
        sl_px = decision.suggested_sl_price
        min_sl_pct = cfg.min_sl_distance_pct_for(symbol)
        if min_sl_pct > 0.0 and entry_px > 0.0:
            sl_dist_pct = abs(entry_px - sl_px) / entry_px
            if sl_dist_pct < min_sl_pct:
                min_dist = entry_px * min_sl_pct
                if decision.direction == Direction.BULLISH:
                    sl_px = entry_px - min_dist
                else:
                    sl_px = entry_px + min_dist

        try:
            plan = calculate_trade_plan(
                direction=decision.direction,
                entry_price=entry_px,
                sl_price=sl_px,
                account_balance=margin_balance,
                risk_pct=cfg.trading.risk_per_trade_pct / 100.0,
                rr_ratio=target_rr,
                max_leverage=max_leverage,
                contract_size=contract_size,
                margin_balance=margin_balance,
                fee_reserve_pct=cfg.trading.fee_reserve_pct,
                risk_amount_usdt_override=risk_amount,
                sl_source=sl_source,
                reason=plan_reason,
            )
            # 2026-05-05 v5 — forward cycle_confluence onto the plan so the
            # PLANNED log line + journal trade row carry the actual
            # directional confluence score (was 0.0/empty since Faz 3 cleanup
            # — Pass 3 GBT feature poisoning). HA-native entry decision is
            # already made; this is purely for downstream observability.
            if cycle_confluence is not None:
                try:
                    plan.confluence_score = float(cycle_confluence.score)
                    plan.confluence_factors = list(
                        cycle_confluence.factor_names
                    )
                except Exception:
                    pass
            return plan
        except Exception:
            logger.exception("ha_native_trade_plan_build_failed symbol={}", symbol)
            return None

    async def _run_one_symbol(self, symbol: str) -> None:
        cfg = self.ctx.config

        # 0. Macro event blackout — skip new entries inside ±window of a
        # scheduled HIGH-impact USD event (CPI/FOMC/NFP/PCE). Open positions
        # are untouched (their OCO algos already manage exit). Cheap sync
        # check, runs before the expensive TV symbol/TF switching AND before
        # the symbol_cycle_start log so the blackout dal does not spam the
        # log file at ~2 lines/sec for the full ±window.
        if self.ctx.economic_calendar is not None:
            try:
                blackout = self.ctx.economic_calendar.is_in_blackout(_utc_now())
            except Exception:
                logger.exception("economic_calendar_check_failed symbol={}", symbol)
                blackout = None
            if blackout is not None and blackout.active and blackout.event is not None:
                evt = blackout.event
                # Per-symbol throttle: at most one line per minute per symbol.
                now_mono = time.monotonic()
                last_mono = self._macro_blackout_log_ts.get(symbol, 0.0)
                if now_mono - last_mono >= 60.0:
                    logger.info(
                        "symbol_decision symbol={} NO_TRADE reason=macro_event_blackout "
                        "event={!r} country={} impact={} secs_to_event={} "
                        "secs_after_event={} source={}",
                        symbol, evt.title, evt.country, evt.impact.value,
                        blackout.seconds_until_event, blackout.seconds_after_event,
                        evt.source,
                    )
                    self._macro_blackout_log_ts[symbol] = now_mono
                return

        logger.info("symbol_cycle_start symbol={}", symbol)

        # 1. Switch the TV chart to this symbol (production has a bridge;
        # tests pass bridge=None and the reader fake already knows the symbol).
        # 2026-04-27 — moved BEFORE the VWAP-reset-blackout early-return so
        # the chart still cycles through symbols during the blackout window.
        # Pre-fix the bot would early-return at ~1ms per symbol cycle,
        # leaving TradingView frozen on whichever symbol was loaded when
        # the window opened — looked like the bot was hung. Now the
        # symbol switches each cycle even though the trade decision is
        # skipped; the operator can keep eyeballing the chart.
        if self.ctx.bridge is not None:
            tv_symbol = internal_to_tv_symbol(symbol)
            # Short-circuit: chart already on this symbol → set_symbol would be
            # a no-op and Pine wouldn't re-render, so dual-poll burns the full
            # max_wait_s timeout watching an unchanged last_bar (2026-05-04 fix).
            skip_switch = False
            try:
                status = await self.ctx.bridge.status()
                skip_switch = status.get("chart_symbol") == tv_symbol
            except Exception:
                pass  # status() failed — fall through to full switch+poll
            if not skip_switch:
                # Operatör 2026-05-04 dinamik settle: capture pre-switch dual
                # baselines and replace the static `symbol_settle_seconds`
                # sleep with a dual-poll wait. Bir sonraki sembole geçtiği
                # anda Signals + Oscillator fresh olunca cycle devam eder.
                sym_baselines = await self._read_last_bars()
                try:
                    await self.ctx.bridge.set_symbol(tv_symbol)
                except Exception:
                    logger.exception("set_symbol_failed symbol={}", symbol)
                    return
                # set_symbol already waits ~10s for chart settle (Node side
                # blocks on TV CDP until chart re-render completes). After
                # it returns, Pine table fills within ~100ms — the data-ready
                # poll below catches that quickly. No floor sleep needed.
                # Symbol switch may still need 2× cap on slow Pine cold starts
                # (HA recursion + multi-TF security recompute beyond the chart
                # render Node already waited for). Operatör 2026-05-04 v3.
                symbol_max_wait = cfg.trading.pine_settle_max_wait_s * 2
                settled = await self._wait_for_pine_settle_dual(
                    sym_baselines, max_wait_s=symbol_max_wait,
                )
                if not settled:
                    logger.warning(
                        "symbol_settle_timeout symbol={} max_wait_s={}",
                        symbol, symbol_max_wait,
                    )
                    # Don't return — caller's downstream calls
                    # (read_market_state, _switch_timeframe) will get whatever
                    # Pine has; existing error handling skips the cycle if
                    # data is stale. Parity with legacy "sleep then continue".

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
                    # 2026-05-04 — pop stale htf_state_cache + htf_adx_cache
                    # entries when the HTF settle fails. Same rationale as
                    # the read-failure handler below: a settle timeout
                    # signals Pine HTF data is unreliable; downstream
                    # consumers must not see a stale snapshot mis-labelled
                    # as fresh.
                    self.ctx.htf_state_cache.pop(symbol, None)
                    self.ctx.htf_adx_cache.pop(symbol, None)
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
                    # 2026-05-02 — Phase A.9 HTF ADX numeric capture. Reuses
                    # the same htf_candles buffer (no extra TV/Bybit call).
                    # UNKNOWN result is still stashed so consumers can
                    # disambiguate "computed but undefined" from "missing".
                    self.ctx.htf_adx_cache[symbol] = classify_trend_regime(
                        htf_candles,
                        period=cfg.analysis.adx_period,
                        ranging_threshold=cfg.analysis.trend_regime_ranging_threshold,
                        strong_threshold=cfg.analysis.trend_regime_strong_threshold,
                    )
                else:
                    self.ctx.htf_sr_cache.pop(symbol, None)
                    self.ctx.htf_adx_cache.pop(symbol, None)
            except Exception:
                logger.exception("htf_refresh_failed symbol={}", symbol)
                self.ctx.htf_sr_cache.pop(symbol, None)
                self.ctx.htf_adx_cache.pop(symbol, None)

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
            # Same rationale for the 15m ADX cache — when the position closes
            # and the symbol reopens for entries, a stale regime from N
            # cycles ago must not stamp the new trade row.
            self.ctx.htf_adx_cache.pop(symbol, None)

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
        # 2026-05-04 — HA-native state pump. Each cycle's entry-TF MarketState
        # feeds the per-symbol HASymbolState history (deque maxlen=60). The
        # planner / exit suite reads derived properties (delta dirs, color
        # flips, dominant_color) without re-parsing tables.
        try:
            self.ctx.ha_state_registry.update(symbol, state, _utc_now())
        except Exception:
            logger.exception("ha_state_registry_update_failed symbol={}", symbol)
        # 2026-05-04 — HA-native planner evaluate + decision_log row.
        # Returns EntryDecision (or None on internal failure). Caller may
        # use it for primary-mode TradePlan in step 3, or ignore it
        # (audit-only) until then. decision_log is written either way.
        # 2026-05-05 v4 — confluence destek sinyali cycle başına BİR KEZ
        # hesaplanır, hem dispatcher'a (yön teyidi soft factor) hem
        # NO_TRADE branch + entry path journal'ına geçer. Buffer + state
        # önceden hazır.
        buf = self.ctx.multi_tf.get_buffer(tf_key)
        # 100 candles is enough for confluence consumers to read the tail.
        candles = buf.last(100) if buf is not None else []
        try:
            cycle_confluence = calculate_confluence(
                state,
                ltf_candles=candles,
                allowed_sessions=cfg.allowed_sessions_for(symbol) or None,
                ltf_state=self.ctx.ltf_cache.get(symbol),
                htf_state=self.ctx.htf_state_cache.get(symbol),
                weights=cfg.analysis.confluence_weights or None,
                min_rsi_mfi_magnitude=cfg.analysis.min_rsi_mfi_magnitude,
                liquidity_pool_max_atr_dist=cfg.analysis.liquidity_pool_max_atr_dist,
                displacement_atr_mult=cfg.analysis.displacement_atr_mult,
                displacement_max_bars_ago=cfg.analysis.displacement_max_bars_ago,
                divergence_fresh_bars=cfg.analysis.divergence_fresh_bars,
                divergence_decay_bars=cfg.analysis.divergence_decay_bars,
                divergence_max_bars=cfg.analysis.divergence_max_bars,
                daily_bias_enabled=(
                    cfg.on_chain.enabled
                    and cfg.on_chain.daily_bias_enabled
                ),
                daily_bias_delta=cfg.on_chain.daily_bias_modifier_delta,
            )
        except Exception:
            logger.debug("cycle_confluence_failed symbol={}", symbol)
            cycle_confluence = None
        ha_decision = await self._evaluate_ha_native_entry(
            symbol, state, confluence=cycle_confluence,
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

        # 2g. Phase A.5 (2026-05-02) — multi-step trailing SL after MFE-lock.
        # Distinct from 2f: this runs after BE-lock fires and pulls SL
        # forward in 0.5R steps as MFE keeps growing. Disabled in RANGING
        # by default (TP at 1.2R fires before trailing would arm at 1.5R).
        if open_side:
            await self._maybe_trail_sl_after_mfe(symbol, open_side, state)

        # 2h. Phase A.6 (2026-05-02) — MAE-triggered BE-lock with LIMIT-based
        # exit. Protects trades that went deep into MAE then recovered to
        # entry zone with adverse cycle data. Two-stage: arm at -0.6R MAE,
        # fire when mark recovers + LTF still adverse → place reduce-only
        # post-only limit at entry+fee_buffer (long) / entry-fee_buffer
        # (short). Maker exit, fee-positive close.
        if open_side:
            await self._maybe_lock_sl_on_mae_recovery(symbol, open_side, state)

        # 2i. Phase A.8 (2026-05-02) — weakening-momentum exit. Computes
        # directional confluence in the position's direction each cycle,
        # tracks a short history, fires `_defensive_close()` with
        # `momentum_fade` reason once N cycles of monotonic decline + MFE
        # in profit confirm a fading signal. Returns True on close so we
        # short-circuit downstream entry-side work.
        if open_side:
            closed = await self._maybe_close_on_momentum_fade(
                symbol, open_side, state, candles,
            )
            if closed:
                return

        # 2j. Yol A HA-native exit gate (2026-05-04). Multi-TF HA color
        # reversal + RCS volume confirm — fires only for positions opened
        # via the HA-native primary path (`is_ha_native=True` flag stamped
        # at register_open). Legacy 5-pillar positions retain their
        # pre-existing exit suite (momentum_fade above already ran);
        # this gate is purely additive for HA-native trades.
        # 2026-05-05 — Faz 7: counter-reversal exit ha_flip'ten ÖNCE.
        # "İp üstündeki cambaz" — pozisyonun TERSİ yönde güçlü Major
        # Reversal sinyali gelirse hemen close, sonraki cycle yeni yönde
        # entry alabilir. HA-flip'ten daha sıkı (skor ≥ 5.0 + structural
        # MR confirmation).
        if open_side:
            cr_closed = await self._maybe_close_on_counter_reversal(
                symbol, open_side, state,
            )
            if cr_closed:
                return
        if open_side:
            ha_closed = await self._maybe_close_on_ha_flip(
                symbol, open_side, state,
            )
            if ha_closed:
                return

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
        # Stash for zone re-sizing path (apply_zone_to_plan needs the same
        # margin budget the original plan was sized against).
        self.ctx.last_margin_balance = margin_balance

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
        # 2026-05-02 — Phase A.9 HTF ADX numeric pulled from cache populated
        # in the HTF pass above. None when this is the already-open skip
        # branch or HTF refresh failed; downstream `_adx_triad_kwargs` then
        # writes NULLs for the 15m triad.
        htf_regime_result = self.ctx.htf_adx_cache.get(symbol)

        # 2026-05-05 v3 — Yol A: HA-native primary mode is the only entry path.
        # Legacy 5-pillar block (build_trade_plan_with_reason +
        # _LEGACY_5PILLAR_ENABLED flag) was deleted in this commit.
        plan: Optional[TradePlan] = None
        reject_reason: Optional[str] = None
        if ha_decision is not None and ha_decision.is_take:
            # 2026-05-05 — Faz 7: ema200_warmup gate. Operatör DB null
            # kuralı: ema200_3m=0.0 (Pine henüz 200-bar warmup'ta) →
            # trade alma. Aksi halde journal'a 0.0 yazılır ki bu
            # null-equivalent (Pine emit edemedi). Production'da TV
            # chart 200+ bar açıldığı için pratik olarak hep dolu, ama
            # edge case için savunmacı kontrol.
            ema200 = float(state.signal_table.ema200_3m or 0.0)
            if (
                cfg.execution.ema200_warmup_gate_enabled
                and ema200 <= 0.0
            ):
                logger.warning(
                    "ema200_warmup_gate_blocked symbol={} ema200={} — "
                    "skipping HA-native entry (Pine henüz 200-bar warmup'ta)",
                    symbol, ema200,
                )
                reject_reason = "ema200_warmup_not_ready"
            else:
                ha_plan = self._build_ha_native_trade_plan(
                    ha_decision, symbol,
                    cycle_confluence=cycle_confluence,
                )
                if ha_plan is not None:
                    plan = ha_plan
                    reject_reason = None
                    # 2026-05-05 v5 — `prev_5pillar_reject` token retired
                    # (Faz 5 deleted legacy 5-pillar entirely, value was
                    # always None — log noise).
                    logger.info(
                        "ha_native_plan_override symbol={} dir={} "
                        "entry_path={} entry={} sl={} tp={} contracts={} "
                        "rr={:.2f} risk_mult={:.2f} "
                        "scores=(MR={:.2f},C={:.2f},MicR={:.2f})",
                        symbol,
                        ha_plan.direction.value if hasattr(
                            ha_plan.direction, "value"
                        ) else ha_plan.direction,
                        ha_decision.entry_path,
                        ha_plan.entry_price, ha_plan.sl_price,
                        ha_plan.tp_price, ha_plan.num_contracts,
                        ha_decision.target_rr,
                        ha_decision.risk_multiplier,
                        ha_decision.major_reversal_score,
                        ha_decision.continuation_score,
                        ha_decision.micro_reversal_score,
                    )

        if plan is None:
            # 2026-05-04 — Yol A: derive a sensible reject_reason for the
            # NO_TRADE branch when HA-native is the sole entry path.
            # reject_reason will be None whenever _LEGACY_5PILLAR_ENABLED is
            # False AND HA-native didn't take (the legacy taxonomy below
            # only fires when the legacy block ran). Map ha_decision outcome
            # to a clean reason so journal/audit lines stay searchable.
            if reject_reason is None:
                if ha_decision is None:
                    reject_reason = "ha_native_audit_skipped"
                elif ha_decision.decision == "REJECT":
                    reject_reason = (
                        f"ha_native_reject:{ha_decision.reason}"
                        if ha_decision.reason else "ha_native_reject"
                    )
                else:
                    reject_reason = "ha_native_no_setup"
            # 2026-05-05 v4 — cycle_confluence zaten yukarıda hesaplandı
            # (dispatcher yön-teyidi soft factor için kullandı); journal'a
            # da onu yaz, redundant ikinci çağrı yapma.
            try:
                conf = cycle_confluence
                if conf is None:
                    # Failsafe: cycle confluence hesabı fail ettiyse minimal
                    # stub geçirelim ki record_reject schema patlamasın.
                    ha_dir = (
                        ha_decision.direction
                        if (ha_decision is not None
                            and ha_decision.direction is not None)
                        else Direction.UNDEFINED
                    )
                    conf = ConfluenceScore(
                        direction=ha_dir, score=0.0, factors=[],
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
                try:
                    await self._record_reject(
                        symbol=symbol,
                        reject_reason=reject_reason or "unknown",
                        state=state,
                        conf=conf,
                        candles=candles,
                        adx_3m_result=trend_regime_result,
                        adx_15m_result=htf_regime_result,
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

        # 5. Yol A: HA-native plan → marketable limit at plan.entry_price.
        # Planner already computed entry_price as last_close ± marketable_offset
        # (default 5 bps); no zone-search needed. 1-cycle timeout, fill
        # processing in `_process_pending` next cycle.
        # 2026-05-05 v4 — cycle_confluence zaten dispatcher öncesi
        # hesaplandı; journal/log için reuse + PendingSetupMeta'ya
        # stash'le, fill'de yazılır (yön teyidi + Pass 3 GBT feature).
        pos_side = _direction_to_pos_side(plan.direction)
        placed = await self._place_ha_native_limit(
            symbol=symbol, pos_side=pos_side, plan=plan, state=state,
            adx_3m_result=trend_regime_result,
            adx_15m_result=htf_regime_result,
            confluence=cycle_confluence,
        )
        if placed:
            return  # wait for fill event

        # Limit placement failed (router exception). Record reject + return;
        # no fallback to market on Yol A.
        try:
            conf_stub = ConfluenceScore(
                direction=plan.direction, score=0.0, factors=[],
            )
            await self._record_reject(
                symbol=symbol, reject_reason="ha_native_limit_place_failed",
                state=state, conf=conf_stub, candles=candles,
                adx_3m_result=trend_regime_result,
                adx_15m_result=htf_regime_result,
            )
        except Exception:
            logger.debug("ha_native_limit_fail_log_failed symbol={}", symbol)
        return

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
            # 2026-05-02 — Phase A.7/A.8 directional confluence at snap time.
            # Reads the latest entry of the tracked deque populated by
            # `_maybe_close_on_momentum_fade` each cycle. None when the
            # weakening-exit gate hasn't run yet for this position
            # (master flag off, or first cycle post-fill).
            history = getattr(tracked, "recent_confluence_history", None) or []
            confluence_score_now = (
                float(history[-1]) if history else None
            )
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
                    confluence_score_now=confluence_score_now,
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

    async def _place_ha_native_limit(
        self,
        *,
        symbol: str,
        pos_side: str,
        plan: TradePlan,
        state: MarketState,
        adx_3m_result: Optional[TrendRegimeResult] = None,
        adx_15m_result: Optional[TrendRegimeResult] = None,
        confluence: Optional[ConfluenceScore] = None,
    ) -> bool:
        """Yol A: marketable limit at plan.entry_price (already last_close ±
        marketable_offset_pct from the HA-native dispatcher). 1-cycle
        timeout; fill is processed in `_process_pending` next cycle.

        Returns True when the limit was registered (caller awaits fill),
        False when the router rejected the order (caller logs reject).
        """
        cfg = self.ctx.config
        try:
            result = await asyncio.to_thread(
                self.ctx.router.place_limit_entry,
                plan, plan.entry_price, symbol,
            )
        except (LeverageSetError, OrderRejected, InsufficientMargin, ValueError) as exc:
            logger.error(
                "ha_native_limit_rejected symbol={}: {} | code={} | payload={}",
                symbol, exc,
                getattr(exc, "code", None), getattr(exc, "payload", None),
            )
            return False
        except Exception:
            logger.exception("ha_native_limit_unexpected_error symbol={}", symbol)
            return False

        # 1-cycle timeout = entry-TF bar duration (planner's
        # `entry_cycle_timeout=1`). PositionMonitor cancels at this boundary.
        tf_sec = _tf_seconds(cfg.trading.entry_timeframe)
        max_wait_s = float(tf_sec)
        placed_at = _utc_now()

        self.ctx.monitor.register_pending(
            inst_id=symbol, pos_side=pos_side, order_id=result.order_id,
            num_contracts=float(plan.num_contracts),
            entry_px=plan.entry_price,
            max_wait_s=max_wait_s, placed_at=placed_at,
        )
        self.ctx.pending_setups[(symbol, pos_side)] = PendingSetupMeta(
            plan=plan,
            zone=None,  # HA-native: no zone source (planner-direct limit)
            order_id=result.order_id,
            signal_state=state,
            placed_at=placed_at,
            oscillator_raw_values_at_placement=(
                self._build_oscillator_raw_values(symbol, state)
            ),
            adx_3m_result_at_placement=adx_3m_result,
            adx_15m_result_at_placement=adx_15m_result,
            confluence_at_placement=confluence,
        )
        logger.info(
            "ha_native_limit_placed symbol={} side={} order_id={} "
            "entry={:.4f} sl={:.4f} tp={:.4f} contracts={} max_wait_s={:.0f}",
            symbol, pos_side, result.order_id, plan.entry_price,
            plan.sl_price, plan.tp_price, plan.num_contracts, max_wait_s,
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

        # 2026-05-04 — Yol A: pending-fill path also stamps is_ha_native.
        # `meta` is the PendingSetupMeta stashed at limit-place time; the
        # plan reused here is the same one that produced the pending
        # entry, so the `ha_native:` reason prefix carries through.
        is_ha_native = bool(plan.reason and plan.reason.startswith("ha_native:"))
        self.ctx.monitor.register_open(
            ev.inst_id, ev.pos_side, float(plan.num_contracts), fill_px,
            algo_ids=algo_ids, tp2_price=plan.tp_price,
            sl_price=plan.sl_price, runner_size=runner_size,
            plan_sl_price=plan.sl_price,
            tp_limit_order_id=tp_limit_order_id,
            regime_at_entry=meta.trend_regime_at_entry,
            opened_at=_utc_now(),
            is_ha_native=is_ha_native,
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
                # 2026-05-02 — Phase A.9 ADX numeric stamped from PLACEMENT
                # TIME (mirrors `trend_regime_at_entry` provenance, not from
                # the current fill-moment cache). Fill may land minutes
                # after placement; the regime that drove the decision is
                # the one we want on the row.
                **_adx_triad_kwargs(
                    "3m", meta.adx_3m_result_at_placement,
                ),
                **_adx_triad_kwargs(
                    "15m", meta.adx_15m_result_at_placement,
                ),
                on_chain_context=self._on_chain_context_dict(),
                # 2026-05-05 v4 — Yol A: confluence destek sinyali
                # placement-time'da hesaplandı + meta'ya stash'lendi;
                # fill'de journal'a yazılır. Plan kendi
                # `confluence_pillar_scores`'unu set etmediği (HA-native
                # builder boş bırakır) için meta'dan okuruz.
                confluence_pillar_scores=(
                    {
                        f.name: float(f.weight)
                        for f in (
                            meta.confluence_at_placement.factors
                            if meta.confluence_at_placement is not None
                            else []
                        )
                    }
                    if meta.confluence_at_placement is not None
                    else dict(plan.confluence_pillar_scores or {})
                ),
                # 2026-04-22 (gece, late) — journal oscillator snapshot
                # captured at pending PLACEMENT (not fill) so the row
                # reflects the decision moment. Fill may happen minutes
                # after placement and the caches may have rotated.
                oscillator_raw_values=dict(
                    meta.oscillator_raw_values_at_placement or {}
                ),
                # 2026-05-05 v3 — Yol A: HA-native plans have meta.zone=None
                # (no zone-search). Source labeled "ha_native"; wait bars
                # default 1 (planner's entry_cycle_timeout). Legacy zone
                # branches keep their zone source label.
                setup_zone_source=(
                    str(meta.zone.zone_source) if meta.zone is not None
                    else "ha_native"
                ),
                zone_wait_bars=(
                    int(meta.zone.max_wait_bars) if meta.zone is not None
                    else 1
                ),
                zone_fill_latency_bars=_zone_fill_latency_bars(
                    placed_at=meta.placed_at,
                    fill_at=_utc_now(),
                    entry_tf_minutes=_timeframe_to_minutes(
                        cfg.trading.entry_timeframe),
                    max_wait_bars=(
                        int(meta.zone.max_wait_bars) if meta.zone is not None
                        else 1
                    ),
                ),
                **_derive_enrichment(state),
                # 2026-05-04 — HA-native (Yol A) journal fields. State here
                # is `meta.signal_state` (placement-time snapshot), so HA
                # fields reflect the entry decision moment, not fill.
                **self._ha_native_record_kwargs(plan, state),
            )
            self.ctx.open_trade_ids[key] = rec.trade_id
            self.ctx.open_trade_opened_at[key] = _utc_now()
            logger.info(
                "pending_filled_promoted inst={} side={} contracts={} "
                "fill_px={:.4f} zone={} trade_id={}",
                ev.inst_id, ev.pos_side, plan.num_contracts, fill_px,
                (
                    meta.zone.zone_source if meta.zone is not None
                    else "ha_native"
                ),
                rec.trade_id,
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
                # 2026-04-29 Pass 2.5 — pending was placed at these exact
                # SL/TP levels, so use plan values directly (more accurate
                # than re-computing what-if). Pegger forward-walks Bybit
                # klines from `placed_at` to flag would-have-WIN/LOSS.
                proposed_sl_price=float(plan.sl_price) if plan.sl_price else None,
                proposed_tp_price=float(plan.tp_price) if plan.tp_price else None,
                proposed_rr_ratio=(
                    float(plan.rr_ratio) if plan.rr_ratio else None
                ),
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
                # pillar_btc_bias / pillar_eth_bias dropped 2026-05-05 v3.
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
                # 2026-05-02 — Phase A.9 ADX numeric stamped from PLACEMENT
                # TIME (cancel may fire many bars after placement; the
                # decision-moment regime is what we want for the
                # counter-factual).
                **_adx_triad_kwargs(
                    "3m", meta.adx_3m_result_at_placement,
                ),
                **_adx_triad_kwargs(
                    "15m", meta.adx_15m_result_at_placement,
                ),
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
            # 2026-05-04 — Yol A: rehydrate path stamps is_ha_native from
            # the journal `reason` column. Plans built by the HA-native
            # planner write `ha_native:` prefix; rehydrated rows that
            # match get the flag so the HA-flip exit gate fires after
            # restart too. Pre-Yol-A rows have plain reasons → False.
            is_ha_native = bool(
                rec.reason and rec.reason.startswith("ha_native:")
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
                regime_at_entry=rec.trend_regime_at_entry,
                opened_at=rec.entry_timestamp,
                is_ha_native=is_ha_native,
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

