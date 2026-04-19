"""BotRunner — the async outer loop that wires every subsystem.

Shape of one tick (`run_once`):
  1. Fetch MarketState + recent candles from the TV bridge.
  2. Drain closed-position fills from PositionMonitor → enrich PnL via OKX
     positions-history → record_close in journal → update RiskManager.
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
monitor.register_open, okx_client.enrich_close_fill / get_positions).
"""

from __future__ import annotations

import asyncio
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
from src.data.structured_reader import StructuredReader
from src.data.tv_bridge import TVBridge, okx_to_tv_symbol
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
from src.execution.okx_client import OKXClient
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.execution.position_monitor import PendingEvent, PositionMonitor
from src.journal.database import TradeJournal
from src.journal.derivatives_journal import DerivativesJournal
from src.strategy.entry_signals import (
    build_trade_plan_with_reason,
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


def _derive_enrichment(state: MarketState) -> dict:
    """Pull derivatives + heatmap snapshot fields out of MarketState for
    journal persistence. All keys are None when a source is missing — the
    journal's ALTER TABLE columns default to NULL so that's safe."""
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
    }
    deriv = getattr(state, "derivatives", None)
    if deriv is not None:
        out["regime_at_entry"] = getattr(deriv, "regime", None)
        out["funding_z_at_entry"] = getattr(deriv, "funding_rate_zscore_30d", None)
        out["ls_ratio_at_entry"] = getattr(deriv, "long_short_ratio", None)
        out["oi_change_24h_at_entry"] = getattr(deriv, "oi_change_24h_pct", None)
        out["liq_imbalance_1h_at_entry"] = getattr(deriv, "liq_imbalance_1h", None)
    hm = getattr(state, "liquidity_heatmap", None)
    if hm is not None:
        na = getattr(hm, "nearest_above", None)
        nb = getattr(hm, "nearest_below", None)
        price = float(getattr(state, "current_price", 0.0) or 0.0)
        atr = float(getattr(state, "atr", 0.0) or 0.0)
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
    okx_client: Any            # `.enrich_close_fill`, `.get_positions`
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
    # OKX per-symbol ctVal (underlying per contract). BTC=0.01, ETH=0.1, SOL=1.
    # Populated at bootstrap; one hardcoded value for all symbols trips 51008.
    contract_sizes: dict[str, float] = field(default_factory=dict)
    # Per-symbol OKX max leverage (BTC/ETH=100, SOL=50). Above this trips 59102.
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


# ── Runner ──────────────────────────────────────────────────────────────────


class _DryRunRouter:
    """Stand-in router for --dry-run: mirrors OrderRouter.place(plan) signature."""

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
        client = OKXClient(cfg.to_okx_credentials())
        router_cfg = RouterConfig(
            inst_id=cfg.primary_symbol(),
            margin_mode=cfg.execution.margin_mode,
            partial_tp_enabled=cfg.execution.partial_tp_enabled,
            partial_tp_ratio=cfg.execution.partial_tp_ratio,
            partial_tp_rr=cfg.execution.partial_tp_rr,
            move_sl_to_be_after_tp1=cfg.execution.move_sl_to_be_after_tp1,
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
        )
        risk_mgr = RiskManager(cfg.bot.starting_balance, cfg.breakers())
        ctx = BotContext(
            reader=reader, multi_tf=multi_tf, journal=journal,
            router=router, monitor=monitor, risk_mgr=risk_mgr,
            okx_client=client, config=cfg, bridge=bridge,
            ltf_reader=ltf_reader,
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

        # Data-collection mode: stream + cache still run in the background via
        # _start_derivatives; here we just skip the entry/exit pipeline. Close
        # poll above still fires so any positions already on the book resolve.
        if self.derivatives_only:
            return

        for symbol in self.ctx.config.trading.symbols:
            if self.shutdown.is_set():
                return
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

    async def _record_reject(
        self,
        *,
        symbol: str,
        reject_reason: str,
        state: MarketState,
        conf,
    ) -> None:
        """Persist a reject to `rejected_signals` (Phase 7.B1).

        Caller is responsible for try/except around this — any DB issue
        must never block the main cycle (reject logging is observational).
        All snapshot fields default to None so partial data is fine.
        """
        cfg = self.ctx.config
        enrichment = _derive_enrichment(state)
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

        # Cancel any outstanding algos for this (inst, side) via the monitor's
        # tracked state — best-effort, keep going on failure.
        tracked = getattr(self.ctx.monitor, "_tracked", {}).get(key)
        algo_ids = list(tracked.algo_ids) if tracked is not None else []
        for algo_id in algo_ids:
            try:
                await asyncio.to_thread(
                    self.ctx.okx_client.cancel_algo, symbol, algo_id,
                )
            except Exception:
                logger.exception("defensive_cancel_algo_failed "
                                 "symbol={} algo_id={}", symbol, algo_id)

        try:
            await asyncio.to_thread(
                self.ctx.okx_client.close_position, symbol, side,
                self.ctx.config.execution.margin_mode,
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

        # 1. Switch the TV chart to this symbol (production has a bridge;
        # tests pass bridge=None and the reader fake already knows the symbol).
        if self.ctx.bridge is not None:
            try:
                await self.ctx.bridge.set_symbol(okx_to_tv_symbol(symbol))
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

        # 3. Symbol-level dedup — skip open if we still hold anything OR
        # already have a pending limit entry waiting for fill (Phase 7.C4).
        if any(k[0] == symbol for k in self.ctx.open_trade_ids):
            return
        if any(k[0] == symbol for k in self.ctx.pending_setups):
            return

        # 4. Plan. Risk budget (R = risk_pct × balance) is derived from TOTAL
        # equity so drawdowns scale R naturally but locked margin in other
        # positions doesn't shrink it. Margin-fit (notional/leverage ceiling)
        # uses the smaller of per-slot fair-share and live `availEq` so the
        # order still fits on OKX right now and multiple concurrent positions
        # coexist. sCode 51008 avoidance lives on the margin side.
        try:
            total_eq = await asyncio.to_thread(
                self.ctx.okx_client.get_total_equity, "USDT"
            )
        except Exception:
            logger.exception("total_eq_sync_failed_using_cached")
            total_eq = self.ctx.risk_mgr.current_balance
        try:
            okx_avail = await asyncio.to_thread(
                self.ctx.okx_client.get_balance, "USDT"
            )
        except Exception:
            logger.exception("balance_sync_failed_using_cached")
            okx_avail = self.ctx.risk_mgr.current_balance
        slot_count = max(1, int(cfg.trading.max_concurrent_positions))
        per_slot = total_eq / slot_count
        risk_balance = min(total_eq, self.ctx.risk_mgr.current_balance)
        margin_balance = min(per_slot, okx_avail)
        sizing_balance = margin_balance  # retained for logging/back-compat

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
                    )
                    await self._record_reject(
                        symbol=symbol, reject_reason="no_setup_zone",
                        state=state, conf=conf,
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
        self.ctx.monitor.register_open(
            symbol, pos_side, float(plan.num_contracts), plan.entry_price,
            algo_ids=algo_ids, tp2_price=plan.tp_price,
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
                **_derive_enrichment(state),
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
        await self._rehydrate_open_positions()
        await self._reconcile_orphans()
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
        """Pre-fetch OKX ctVal + max leverage for every configured symbol.
        Falls back to YAML defaults on error so the bot still runs; logs
        the failure so the operator sees it."""
        cfg = self.ctx.config
        ct_fallback = cfg.trading.contract_size
        lev_fallback = cfg.trading.max_leverage
        for symbol in cfg.trading.symbols:
            try:
                spec = await asyncio.to_thread(
                    self.ctx.okx_client.get_instrument_spec, symbol)
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
            fills = await asyncio.to_thread(self.ctx.monitor.poll)
        except Exception:
            logger.exception("monitor_poll_failed")
            return
        for fill in fills:
            await self._handle_close(fill)

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
            zoned_plan = apply_zone_to_plan(plan, zone, contract_size)
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
        algos: list[AlgoResult]
        try:
            algos = await asyncio.to_thread(
                self.ctx.router.attach_algos, plan, ev.inst_id,
            )
        except Exception as exc:
            logger.critical(
                "pending_fill_algo_attach_failed_position_UNPROTECTED "
                "inst={} side={} order_id={} err={!r}",
                ev.inst_id, ev.pos_side, ev.order_id, exc,
            )
            return

        algo_ids = [a.algo_id for a in algos if a.algo_id]
        self.ctx.monitor.register_open(
            ev.inst_id, ev.pos_side, float(plan.num_contracts), fill_px,
            algo_ids=algo_ids, tp2_price=plan.tp_price,
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
            )
        except Exception:
            logger.debug(
                "pending_cancel_reject_log_failed inst={} side={}",
                ev.inst_id, ev.pos_side,
            )

    async def _handle_close(self, fill: CloseFill) -> None:
        try:
            enriched = await asyncio.to_thread(
                self.ctx.okx_client.enrich_close_fill, fill)
        except Exception:
            logger.exception("enrich_failed_using_raw_fill")
            enriched = fill

        key = (enriched.inst_id, enriched.pos_side)
        trade_id = self.ctx.open_trade_ids.pop(key, None)
        # Madde F — carry close_reason set by the defensive-close path.
        close_reason = self.ctx.pending_close_reasons.pop(key, None)
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

    async def _rehydrate_open_positions(self) -> None:
        """Populate monitor + open_trade_ids from journal OPEN rows.

        `replay_for_risk_manager` already walked CLOSED trades; this covers
        OPEN rows so we know what to expect on the next poll.
        """
        for rec in await self.ctx.journal.list_open_trades():
            pos_side = _direction_to_pos_side(rec.direction)
            self.ctx.monitor.register_open(
                rec.symbol, pos_side,
                float(rec.num_contracts), rec.entry_price,
                algo_ids=list(rec.algo_ids),
                tp2_price=rec.tp_price,
                be_already_moved=rec.sl_moved_to_be,
            )
            self.ctx.open_trade_ids[(rec.symbol, pos_side)] = rec.trade_id
            self.ctx.open_trade_opened_at[(rec.symbol, pos_side)] = rec.entry_timestamp
            # These don't count against RiskManager.open_positions because
            # replay already paired every recorded open with its close.

    async def _reconcile_orphans(self) -> None:
        """Log-only: compare live OKX positions against journal OPEN rows.

        Never auto-closes — operator decides. We only emit one error per
        mismatch so restart logs are actionable.
        """
        try:
            live = await asyncio.to_thread(self.ctx.okx_client.get_positions)
        except Exception:
            logger.exception("reconcile_fetch_failed")
            return
        live_keys = {(p.inst_id, p.pos_side) for p in live if p.size != 0}
        journal_keys = set(self.ctx.open_trade_ids.keys())
        for k in live_keys - journal_keys:
            logger.error("orphan_live_position_no_journal_row key={}", k)
        for k in journal_keys - live_keys:
            logger.error("journal_open_but_no_live_position key={} (stale row)", k)
