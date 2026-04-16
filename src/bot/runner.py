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

from src.analysis.support_resistance import detect_sr_zones
from src.bot.config import BotConfig
from src.bot.lifecycle import install_shutdown_handlers
from src.data.candle_buffer import MultiTFBuffer
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
from src.strategy.entry_signals import build_trade_plan_from_state
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


# ── Context ─────────────────────────────────────────────────────────────────


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
    ):
        self.ctx = ctx
        self.shutdown = shutdown or asyncio.Event()
        self.stop_after_closed_trades = stop_after_closed_trades

    # ── Construction ────────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        cfg: BotConfig,
        *,
        dry_run: bool = False,
        stop_after_closed_trades: Optional[int] = None,
    ) -> "BotRunner":
        bridge = TVBridge()
        reader = StructuredReader(bridge)
        ltf_reader = LTFReader(bridge, reader)
        multi_tf = MultiTFBuffer(bridge, max_size=cfg.analysis.candle_buffer_size)
        client = OKXClient(cfg.to_okx_credentials())
        router_cfg = RouterConfig(inst_id=cfg.primary_symbol())
        router = _DryRunRouter(router_cfg) if dry_run else OrderRouter(client, router_cfg)
        monitor = PositionMonitor(client)
        journal = TradeJournal(cfg.journal.db_path)
        risk_mgr = RiskManager(cfg.bot.starting_balance, cfg.breakers())
        ctx = BotContext(
            reader=reader, multi_tf=multi_tf, journal=journal,
            router=router, monitor=monitor, risk_mgr=risk_mgr,
            okx_client=client, config=cfg, bridge=bridge,
            ltf_reader=ltf_reader,
        )
        return cls(ctx, stop_after_closed_trades=stop_after_closed_trades)

    # ── Entry points ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop. Installs signal handlers; exits when `self.shutdown` is set."""
        try:
            install_shutdown_handlers(self.shutdown)
        except Exception:
            logger.exception("signal_install_failed")

        async with self.ctx.journal:
            await self._prime()
            interval = self.ctx.config.bot.poll_interval_seconds
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
                try:
                    await asyncio.wait_for(self.shutdown.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

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

        for symbol in self.ctx.config.trading.symbols:
            if self.shutdown.is_set():
                return
            try:
                await self._run_one_symbol(symbol)
            except Exception:
                logger.exception("symbol_cycle_failed symbol={}", symbol)
                continue

    async def _wait_for_pine_settle(self) -> bool:
        """Poll the signal table until `last_bar` changes, meaning Pine has
        re-rendered for the new symbol / timeframe. Returns True on change.

        Fallbacks:
          * First readable `last_bar == None` → Pine version doesn't emit
            the field (or we're in a unit test with a fake reader). The
            static `tf_settle_seconds` sleep is assumed sufficient; return
            True immediately so the caller keeps going.
          * Timeout → False (caller skips the symbol cycle).
        """
        cfg = self.ctx.config.trading
        deadline = time.monotonic() + cfg.pine_settle_max_wait_s
        baseline: Optional[int] = None
        first_read = True
        while time.monotonic() < deadline:
            try:
                state = await self.ctx.reader.read_market_state()
                lb = state.signal_table.last_bar if state.signal_table else None
                if first_read and lb is None:
                    return True  # Pine doesn't emit last_bar on this chart
                first_read = False
                if lb is not None:
                    if baseline is None:
                        baseline = lb
                    elif lb != baseline:
                        return True
            except Exception:
                pass
            await asyncio.sleep(cfg.pine_settle_poll_interval_s)
        return False

    async def _switch_timeframe(self, tf: str) -> bool:
        """Switch chart TF, sleep the static settle, then freshness-poll.

        Returns True when Pine data reflects the new TF. False on timeout
        or bridge failure — caller skips the current symbol cycle.
        """
        if self.ctx.bridge is None:
            return True            # tests skip — reader fake already correct
        try:
            await self.ctx.bridge.set_timeframe(tf)
        except Exception:
            logger.exception("set_timeframe_failed tf={}", tf)
            return False
        await asyncio.sleep(self.ctx.config.trading.tf_settle_seconds)
        return await self._wait_for_pine_settle()

    async def _run_one_symbol(self, symbol: str) -> None:
        cfg = self.ctx.config

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

        # 3. Symbol-level dedup — skip open if we still hold anything.
        if any(k[0] == symbol for k in self.ctx.open_trade_ids):
            return

        # 4. Plan. Size against the *actual* OKX USDT balance — the risk
        # manager's current_balance drifts from reality (fees, funding), and
        # OKX rejects sCode 51008 when the bot over-estimates available margin.
        try:
            okx_balance = await asyncio.to_thread(
                self.ctx.okx_client.get_balance, "USDT"
            )
        except Exception:
            logger.exception("balance_sync_failed_using_cached")
            okx_balance = self.ctx.risk_mgr.current_balance
        sizing_balance = min(okx_balance, self.ctx.risk_mgr.current_balance)
        try:
            plan = build_trade_plan_from_state(
                state, sizing_balance,
                candles=candles,
                min_confluence_score=cfg.analysis.min_confluence_score,
                risk_pct=cfg.risk_pct_fraction(),
                rr_ratio=cfg.trading.default_rr_ratio,
                min_rr_ratio=cfg.trading.min_rr_ratio,
                max_leverage=cfg.trading.max_leverage,
                contract_size=cfg.trading.contract_size,
                swing_lookback=cfg.analysis.swing_lookback,
                allowed_sessions=cfg.allowed_sessions() or None,
            )
        except Exception:
            logger.exception("plan_build_failed symbol={}", symbol)
            return

        if plan is None:
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
        self.ctx.monitor.register_open(symbol, pos_side, float(plan.num_contracts),
                                       plan.entry_price)
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
            )
            self.ctx.open_trade_ids[(symbol, pos_side)] = rec.trade_id
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

        if trade_id is None:
            logger.warning("orphan_close key={} (no matching trade_id)", key)
            # Still feed risk_mgr so our paper balance tracks reality.
            self.ctx.risk_mgr.register_trade_closed(TradeResult(
                pnl_usdt=enriched.pnl_usdt, pnl_r=0.0,
                timestamp=enriched.closed_at or _utc_now(),
            ))
            return

        try:
            updated = await self.ctx.journal.record_close(trade_id, enriched)
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
            )
            self.ctx.open_trade_ids[(rec.symbol, pos_side)] = rec.trade_id
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
