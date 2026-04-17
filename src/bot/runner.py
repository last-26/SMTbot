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
from src.bot.config import BotConfig
from src.bot.lifecycle import install_shutdown_handlers
from src.data.candle_buffer import MultiTFBuffer
from src.data.derivatives_api import CoinalyzeClient
from src.data.derivatives_cache import DerivativesCache
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
from src.execution.models import CloseFill
from src.execution.okx_client import OKXClient
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.execution.position_monitor import PositionMonitor
from src.journal.database import TradeJournal
from src.journal.derivatives_journal import DerivativesJournal
from src.strategy.entry_signals import (
    build_trade_plan_with_reason,
)
from src.strategy.risk_manager import RiskManager, TradeResult


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
        if na is not None:
            out["nearest_liq_cluster_above_price"] = getattr(na, "price", None)
            out["nearest_liq_cluster_above_notional"] = getattr(na, "notional_usd", None)
        if nb is not None:
            out["nearest_liq_cluster_below_price"] = getattr(nb, "price", None)
            out["nearest_liq_cluster_below_notional"] = getattr(nb, "notional_usd", None)
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
    # Latest LTF snapshot per-symbol (Madde B → F)
    ltf_cache: dict[str, LTFState] = field(default_factory=dict)
    # Last close per (symbol, side) — reentry gate (Madde C)
    last_close: dict[tuple[str, str], LastCloseInfo] = field(default_factory=dict)
    # Madde F — LTF reversal defensive close bookkeeping
    defensive_close_in_flight: set = field(default_factory=set)
    pending_close_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
    open_trade_opened_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    # Phase 1.5 — derivatives subsystem (all opt-in via DerivativesConfig.enabled)
    liquidation_stream: Any = None         # LiquidationStream
    derivatives_cache: Any = None          # DerivativesCache (Madde 3)
    coinalyze_client: Any = None           # CoinalyzeClient (Madde 2)
    # OKX per-symbol ctVal (underlying per contract). BTC=0.01, ETH=0.1, SOL=1.
    # Populated at bootstrap; one hardcoded value for all symbols trips 51008.
    contract_sizes: dict[str, float] = field(default_factory=dict)
    # Per-symbol OKX max leverage (BTC/ETH=100, SOL=50). Above this trips 59102.
    max_leverage_per_symbol: dict[str, int] = field(default_factory=dict)
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
    ):
        self.ctx = ctx
        self.shutdown = shutdown or asyncio.Event()
        self.stop_after_closed_trades = stop_after_closed_trades
        # Phase 1.5 — data-collection modes.
        self.derivatives_only = derivatives_only
        self.duration_seconds = duration_seconds

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
            trail_after_partial=cfg.execution.trail_after_partial,
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

        return cls(
            ctx,
            stop_after_closed_trades=stop_after_closed_trades,
            derivatives_only=derivatives_only,
            duration_seconds=duration_seconds,
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
        return await self._wait_for_pine_settle(baseline)

    async def _run_one_symbol(self, symbol: str) -> None:
        cfg = self.ctx.config
        logger.info("symbol_cycle_start symbol={}", symbol)

        # 1. Switch the TV chart to this symbol (production has a bridge;
        # tests pass bridge=None and the reader fake already knows the symbol).
        if self.ctx.bridge is not None:
            try:
                await self.ctx.bridge.set_symbol(okx_to_tv_symbol(symbol))
                await asyncio.sleep(cfg.trading.symbol_settle_seconds)
            except Exception:
                logger.exception("set_symbol_failed symbol={}", symbol)
                return

        # 2a. HTF pass — switch TF, read S/R from HTF candles, cache.
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
        candles = buf.last(50) if buf is not None else []

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

        # 3. Symbol-level dedup — skip open if we still hold anything.
        if any(k[0] == symbol for k in self.ctx.open_trade_ids):
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
        try:
            plan, reject_reason = build_trade_plan_with_reason(
                state, risk_balance,
                candles=candles,
                min_confluence_score=cfg.analysis.min_confluence_score,
                risk_pct=cfg.risk_pct_fraction(),
                rr_ratio=cfg.trading.default_rr_ratio,
                min_rr_ratio=cfg.trading.min_rr_ratio,
                max_leverage=min(
                    cfg.trading.max_leverage,
                    self.ctx.max_leverage_per_symbol.get(
                        symbol, cfg.trading.max_leverage),
                ),
                contract_size=self.ctx.contract_sizes.get(
                    symbol, cfg.trading.contract_size),
                margin_balance=margin_balance,
                swing_lookback=cfg.analysis.swing_lookback,
                allowed_sessions=cfg.allowed_sessions() or None,
                htf_sr_zones=self.ctx.htf_sr_cache.get(symbol),
                htf_sr_ceiling_enabled=cfg.analysis.htf_sr_ceiling_enabled,
                htf_sr_buffer_atr=cfg.analysis.htf_sr_buffer_atr,
                crowded_skip_enabled=cfg.derivatives.crowded_skip_enabled,
                crowded_skip_z_threshold=cfg.derivatives.crowded_skip_z_threshold,
                ltf_state=self.ctx.ltf_cache.get(symbol),
            )
        except Exception:
            logger.exception("plan_build_failed symbol={}", symbol)
            return

        if plan is None:
            # reject_reason is one of: below_confluence / session_filter /
            # no_sl_source / crowded_skip / zero_contracts / htf_tp_ceiling.
            try:
                conf = calculate_confluence(
                    state,
                    ltf_candles=candles,
                    allowed_sessions=cfg.allowed_sessions() or None,
                    ltf_state=self.ctx.ltf_cache.get(symbol),
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

        # 5. Place order (sync SDK → to_thread).
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
        pos_side = _direction_to_pos_side(plan.direction)
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
        await self.ctx.journal.replay_for_risk_manager(self.ctx.risk_mgr)
        await self._rehydrate_open_positions()
        await self._reconcile_orphans()
        await self._load_contract_sizes()

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
                trade_id, enriched, close_reason=close_reason,
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
