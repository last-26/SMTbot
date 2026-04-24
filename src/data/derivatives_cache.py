"""Per-symbol rolling derivatives cache (Phase 1.5 Madde 3).

Merges three data sources into a single `DerivativesState` snapshot that the
runner reads once per cycle:

  * `LiquidationStream` (Madde 1) — in-process WS buffer, no API cost.
  * `CoinalyzeClient`    (Madde 2) — current OI / funding / LS / aggregated
                                     liquidations. Paid API budget, hence
                                     the refresh_loop is configurable and
                                     OI-change queries fire every 5th tick.
  * Rolling z-score history kept locally (30d funding, 14d LS ratio).

Regime classification (Madde 5) annotates `state.regime` — this module
leaves it UNKNOWN and the classifier in `src/analysis/derivatives_regime.py`
fills it in on each refresh.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from src.analysis.derivatives_regime import (
    DEFAULT_THRESHOLDS,
    classify_regime,
    resolve_thresholds,
)


@dataclass
class DerivativesState:
    """One symbol's current derivatives view — what the runner reads."""
    symbol: str
    ts_ms: int = 0

    # Liquidation stats from Binance WS + Coinalyze aggregated fallback.
    long_liq_notional_1h: float = 0.0
    short_liq_notional_1h: float = 0.0
    long_liq_notional_4h: float = 0.0
    short_liq_notional_4h: float = 0.0
    liq_imbalance_1h: float = 0.0   # (short - long) / (short + long)

    funding_rate_current: float = 0.0
    funding_rate_predicted: float = 0.0
    funding_rate_zscore_30d: float = 0.0

    open_interest_usd: float = 0.0
    oi_change_1h_pct: float = 0.0
    oi_change_24h_pct: float = 0.0

    long_short_ratio: float = 1.0
    ls_ratio_zscore_14d: float = 0.0

    regime: str = "UNKNOWN"   # filled by classify_regime (Madde 5)

    liq_stream_healthy: bool = False
    coinalyze_snapshot_age_s: float = 9999.0

    # 2026-04-24 — per-exchange derivatives snapshot (Binance/Bybit/OKX). The
    # single-exchange fields above are the liquidity-ranked primary source
    # (usually Binance); these dicts capture the same metrics across the 3
    # venues the bot actually trades against. Journal-only (Pass 3 prep);
    # the classifier and runtime scoring keep using the single-exchange
    # values. Empty dict when `_per_exchange_symbol_map` is unpopulated
    # (startup, credits exhausted) or when a specific venue doesn't host the
    # symbol (DOGE on OKX, etc.).
    oi_per_exchange_usd: dict = field(default_factory=dict)
    funding_per_exchange: dict = field(default_factory=dict)
    funding_predicted_per_exchange: dict = field(default_factory=dict)


class DerivativesCache:
    def __init__(
        self,
        watched: list[str],
        liq_stream,
        coinalyze,
        journal,
        refresh_interval_s: float = 60.0,
        oi_refresh_every_n_cycles: int = 5,
        regime_thresholds: Optional[dict[str, float]] = None,
        regime_per_symbol_overrides: Optional[dict[str, dict[str, float]]] = None,
    ):
        self.watched = list(watched)
        self.liq_stream = liq_stream
        self.coinalyze = coinalyze
        self.journal = journal
        self.refresh_interval_s = refresh_interval_s
        self.oi_refresh_every_n_cycles = oi_refresh_every_n_cycles
        self.regime_thresholds = dict(regime_thresholds or DEFAULT_THRESHOLDS)
        self.regime_per_symbol_overrides = dict(regime_per_symbol_overrides or {})
        self._states: dict[str, DerivativesState] = {
            s: DerivativesState(symbol=s) for s in self.watched
        }
        self._funding_history: dict[str, list[float]] = {s: [] for s in self.watched}
        self._ls_history: dict[str, list[float]] = {s: [] for s in self.watched}
        self._oi_refresh_counter: dict[str, int] = {s: 0 for s in self.watched}
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self.coinalyze.ensure_symbol_map(self.watched)
        # Prime z-score buffers from Coinalyze history — one-off cost.
        for symbol in self.watched:
            cn_sym = self.coinalyze.coinalyze_symbol(symbol)
            if not cn_sym:
                continue
            funding_hist = await self.coinalyze.fetch_funding_history_series(
                cn_sym, interval="1hour", lookback_hours=720,
            )
            if funding_hist:
                self._funding_history[symbol] = list(funding_hist[-720:])
            ls_hist = await self.coinalyze.fetch_ls_ratio_history_series(
                cn_sym, interval="1hour", lookback_hours=336,
            )
            if ls_hist:
                self._ls_history[symbol] = list(ls_hist[-336:])
            logger.info(
                "deriv_history_loaded symbol={} funding_pts={} ls_pts={}",
                symbol,
                len(self._funding_history[symbol]),
                len(self._ls_history[symbol]),
            )
        self._task = asyncio.create_task(self._refresh_loop(), name="deriv_refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            for symbol in self.watched:
                if self._stop.is_set():
                    return
                try:
                    await self._refresh_one(symbol)
                except Exception as e:
                    logger.warning("deriv_refresh_failed symbol={} err={!r}",
                                   symbol, e)
            # 2026-04-24 — per-exchange batch capture for Pass 3 journal.
            # Runs once per full refresh cycle (not per symbol) because each
            # endpoint takes `symbols=sym1,sym2,...` up to 20 entries and we
            # cover 5 symbols × 3 exchanges = 15 in a single call. +3 calls
            # per cycle total, label-free, well inside the 40/min budget.
            # Failures isolate per metric — OI may land while funding misses.
            if not self._stop.is_set():
                try:
                    await self._refresh_per_exchange_snapshot()
                except Exception as e:
                    logger.warning(
                        "deriv_per_exchange_refresh_failed err={!r}", e,
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.refresh_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    # ── Refresh ───────────────────────────────────────────────────────────

    async def _refresh_one(self, symbol: str) -> None:
        state = self._states[symbol]
        now_ms = int(time.time() * 1000)

        # 1) Liquidation stats — cheap, in-process.
        stats_1h = self.liq_stream.stats(symbol, lookback_ms=60 * 60 * 1000)
        stats_4h = self.liq_stream.stats(symbol, lookback_ms=4 * 60 * 60 * 1000)
        state.long_liq_notional_1h = stats_1h["long_liq_notional"]
        state.short_liq_notional_1h = stats_1h["short_liq_notional"]
        state.long_liq_notional_4h = stats_4h["long_liq_notional"]
        state.short_liq_notional_4h = stats_4h["short_liq_notional"]
        total = state.long_liq_notional_1h + state.short_liq_notional_1h
        state.liq_imbalance_1h = (
            (state.short_liq_notional_1h - state.long_liq_notional_1h) / total
            if total > 0 else 0.0
        )
        state.liq_stream_healthy = self.liq_stream is not None

        # 2) Coinalyze snapshot (5 API calls).
        snap = await self.coinalyze.fetch_snapshot(symbol)
        if snap is not None:
            state.funding_rate_current = snap.funding_rate_current
            state.funding_rate_predicted = snap.funding_rate_predicted
            state.open_interest_usd = snap.open_interest_usd
            state.long_short_ratio = snap.long_short_ratio
            state.coinalyze_snapshot_age_s = 0.0

            # Coinalyze aggregated liq covers Binance's 1000ms throttle gap.
            coinalyze_long = snap.aggregated_long_liq_1h_usd
            coinalyze_short = snap.aggregated_short_liq_1h_usd
            if coinalyze_long > state.long_liq_notional_1h:
                state.long_liq_notional_1h = coinalyze_long
            if coinalyze_short > state.short_liq_notional_1h:
                state.short_liq_notional_1h = coinalyze_short

            # Z-score updates (funding, LS ratio).
            self._funding_history[symbol].append(snap.funding_rate_current)
            self._funding_history[symbol] = self._funding_history[symbol][-720:]
            state.funding_rate_zscore_30d = self._zscore(
                snap.funding_rate_current, self._funding_history[symbol],
            )
            self._ls_history[symbol].append(snap.long_short_ratio)
            self._ls_history[symbol] = self._ls_history[symbol][-336:]
            state.ls_ratio_zscore_14d = self._zscore(
                snap.long_short_ratio, self._ls_history[symbol],
            )

            # Persist with OI change enriched from state.
            snap.oi_change_1h_pct = state.oi_change_1h_pct
            snap.oi_change_24h_pct = state.oi_change_24h_pct
            await self.journal.insert_snapshot(snap)
        else:
            state.coinalyze_snapshot_age_s += self.refresh_interval_s

        # 3) OI change — heavier; refresh every N cycles only.
        self._oi_refresh_counter[symbol] += 1
        if self._oi_refresh_counter[symbol] >= self.oi_refresh_every_n_cycles:
            self._oi_refresh_counter[symbol] = 0
            cn_sym = self.coinalyze.coinalyze_symbol(symbol)
            if cn_sym:
                oi_24h = await self.coinalyze.fetch_oi_change_pct(
                    cn_sym, lookback_hours=24,
                )
                oi_1h = await self.coinalyze.fetch_oi_change_pct(
                    cn_sym, lookback_hours=1,
                )
                if oi_24h is not None:
                    state.oi_change_24h_pct = oi_24h
                if oi_1h is not None:
                    state.oi_change_1h_pct = oi_1h

        # 4) Regime classification (Madde 5) — after all state fields are set.
        thresholds = resolve_thresholds(
            symbol, self.regime_thresholds, self.regime_per_symbol_overrides,
        )
        analysis = classify_regime(state, **thresholds)
        state.regime = analysis.regime.value

        state.ts_ms = now_ms

    async def _refresh_per_exchange_snapshot(self) -> None:
        """2026-04-24 — fan out Binance/Bybit/OKX OI + funding + predicted
        funding into each `DerivativesState`. Journal-only; runtime scoring
        keeps reading the single-exchange fields. 3 API calls total per
        refresh cycle (each covers all watched symbols via comma batching).
        Each metric fails independently — a funding outage leaves the
        OI-per-exchange dict populated and vice versa."""
        try:
            oi_map = await self.coinalyze.fetch_per_exchange_oi_usd()
        except Exception as e:
            logger.warning("deriv_per_exchange_oi_failed err={!r}", e)
            oi_map = {}
        try:
            fund_map = await self.coinalyze.fetch_per_exchange_funding()
        except Exception as e:
            logger.warning("deriv_per_exchange_funding_failed err={!r}", e)
            fund_map = {}
        try:
            pred_map = await self.coinalyze.fetch_per_exchange_predicted_funding()
        except Exception as e:
            logger.warning(
                "deriv_per_exchange_predicted_funding_failed err={!r}", e,
            )
            pred_map = {}
        for symbol in self.watched:
            state = self._states[symbol]
            state.oi_per_exchange_usd = dict(oi_map.get(symbol, {}))
            state.funding_per_exchange = dict(fund_map.get(symbol, {}))
            state.funding_predicted_per_exchange = dict(
                pred_map.get(symbol, {}),
            )
        n_hits = sum(1 for s in self._states.values() if s.oi_per_exchange_usd)
        if n_hits > 0:
            logger.info(
                "deriv_per_exchange_refreshed symbols_with_oi={} "
                "symbols_with_funding={} symbols_with_predicted={}",
                n_hits,
                sum(1 for s in self._states.values() if s.funding_per_exchange),
                sum(1 for s in self._states.values()
                    if s.funding_predicted_per_exchange),
            )

    # ── Query ─────────────────────────────────────────────────────────────

    def get(self, symbol: str) -> DerivativesState:
        return self._states.get(symbol, DerivativesState(symbol=symbol))

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _zscore(value: float, history: list[float]) -> float:
        if len(history) < 10:
            return 0.0
        try:
            mean = statistics.mean(history)
            stdev = statistics.stdev(history)
        except statistics.StatisticsError:
            return 0.0
        if stdev < 1e-9:
            return 0.0
        return (value - mean) / stdev
