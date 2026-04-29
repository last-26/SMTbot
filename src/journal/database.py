"""Async SQLite journal backed by `aiosqlite`.

Single-writer model. The bot's outer loop owns one `TradeJournal` instance
and hits it on entry fill and on close. Reads (for reports / RL training)
can happen from the same instance or a separate read-only connection — the
schema is small and SQLite handles the concurrency fine.

Lifecycle:
    async with TradeJournal("data/trades.db") as j:
        record = await j.record_open(plan, report, symbol="BTC-USDT-SWAP",
                                      signal_timestamp=when)
        ...
        await j.record_close(record.trade_id, close_fill)

On startup:
    await j.replay_for_risk_manager(risk_manager)
    # RiskManager now sees every past close, reconstructs peak/DD/streaks.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import aiosqlite

from src.data.models import Direction
from src.execution.models import CloseFill, ExecutionReport
from src.journal.models import (
    PositionSnapshotRecord,
    RejectedSignal,
    TradeOutcome,
    TradeRecord,
    WhaleTransferRecord,
)
from src.strategy.risk_manager import RiskManager, TradeResult
from src.strategy.trade_plan import TradePlan


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id            TEXT PRIMARY KEY,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    outcome             TEXT NOT NULL,

    signal_timestamp    TEXT NOT NULL,
    entry_timestamp     TEXT NOT NULL,
    exit_timestamp      TEXT,

    entry_price         REAL NOT NULL,
    sl_price            REAL NOT NULL,
    tp_price            REAL NOT NULL,
    rr_ratio            REAL NOT NULL,
    leverage            INTEGER NOT NULL,
    num_contracts       INTEGER NOT NULL,
    position_size_usdt  REAL NOT NULL,
    risk_amount_usdt    REAL NOT NULL,
    sl_source           TEXT NOT NULL DEFAULT '',
    reason              TEXT NOT NULL DEFAULT '',
    confluence_score    REAL NOT NULL DEFAULT 0,
    confluence_factors  TEXT NOT NULL DEFAULT '[]',

    order_id            TEXT,
    client_order_id     TEXT,
    -- Bybit V5 has position-attached TP/SL; algo_id / client_algo_id /
    -- algo_ids columns dropped 2026-04-27 (audit confirmed empty across
    -- all Bybit-era rows). See _MIGRATIONS DROP COLUMN block for re-add
    -- conditions if migrating to an exchange with separate algo orders.

    -- entry_timeframe / htf_timeframe / regime_at_entry dropped
    -- 2026-04-27 (1-distinct constants). htf_bias, session,
    -- market_structure stay — multi-distinct, semantically informative.
    htf_bias            TEXT,
    session             TEXT,
    market_structure    TEXT,

    exit_price          REAL,
    pnl_usdt            REAL,
    pnl_r               REAL,
    fees_usdt           REAL NOT NULL DEFAULT 0,

    sl_moved_to_be      INTEGER NOT NULL DEFAULT 0,
    close_reason        TEXT,

    funding_z_at_entry                  REAL,
    ls_ratio_at_entry                   REAL,
    oi_change_24h_at_entry              REAL,
    liq_imbalance_1h_at_entry           REAL,
    nearest_liq_cluster_above_price     REAL,
    nearest_liq_cluster_below_price     REAL,
    nearest_liq_cluster_above_notional  REAL,
    nearest_liq_cluster_below_notional  REAL,
    nearest_liq_cluster_above_distance_atr REAL,
    nearest_liq_cluster_below_distance_atr REAL,

    setup_zone_source       TEXT,
    zone_wait_bars          INTEGER,
    zone_fill_latency_bars  INTEGER,
    trend_regime_at_entry   TEXT,
    -- funding_z_6h / funding_z_24h dropped 2026-04-27 (Phase 12 deferred,
    -- never populated; RL pipeline computes rolling z over
    -- derivatives_snapshots directly).

    -- notes / screenshot_entry / screenshot_exit dropped 2026-04-27
    -- (manual operator-fill columns, bot never wrote them).

    real_market_entry_valid INTEGER,
    real_market_exit_valid  INTEGER,
    demo_artifact           INTEGER,
    artifact_reason         TEXT,

    on_chain_context        TEXT,

    -- 2026-04-22 — per-pillar raw scores (ConfluenceFactor.name → weight)
    -- as JSON dict. Enables Pass 2 replay-tuning of per-pillar weights
    -- without re-fetching market state. '{}' on rows written before this
    -- column (backfill not required).
    confluence_pillar_scores TEXT NOT NULL DEFAULT '{}',

    -- 2026-04-22 (gece, late) — oscillator raw numeric snapshot at entry
    -- time. JSON dict keyed by TF ("1m" / "3m" / "15m"); each value is a
    -- dump of `OscillatorTableData` fields (wt1, wt2, rsi, rsi_mfi,
    -- stoch_k, stoch_d, momentum, divergence flags, etc.). Any TF may be
    -- missing when cache was empty (already-open HTF skip, LTF timeout).
    -- Empty dict '{}' on pre-migration rows.
    oscillator_raw_values TEXT NOT NULL DEFAULT '{}',

    -- 2026-04-23 — extended derivatives enrichment (Pass 3 GBT inputs).
    -- All 9 columns were already available on `DerivativesState` at cycle
    -- time but previously only 4 fields landed in journal. Unpopulated on
    -- pre-migration rows; new rows carry every field when the Coinalyze
    -- cache has fresh data.
    open_interest_usd_at_entry       REAL,   -- absolute OI $; pairs with oi_change_* for crowding context
    oi_change_1h_pct_at_entry        REAL,   -- 1h window OI change % (short-term positioning shift)
    funding_rate_current_at_entry    REAL,   -- current funding rate (raw decimal, e.g. 0.0001 = 0.01%/8h)
    funding_rate_predicted_at_entry  REAL,   -- next-funding prediction
    long_liq_notional_1h_at_entry    REAL,   -- long-side liquidation USD past 1h
    short_liq_notional_1h_at_entry   REAL,   -- short-side liquidation USD past 1h
    ls_ratio_zscore_14d_at_entry     REAL,   -- LS ratio z-score over 14 days
    -- price_change_1h_pct_at_entry / price_change_4h_pct_at_entry dropped
    -- 2026-04-27 (by-design NULL on pending-fill entries — every Bybit-era
    -- trade is pending-fill, candles=None plumbed by design). Re-add only
    -- if a market-entry path is reactivated. RL pipeline can compute %
    -- change over candle buffers from raw derivatives_snapshots.
    -- Top-N liquidation heatmap clusters (JSON list of dicts):
    -- `{"above": [{price, notional_usd, distance_atr}, ...],
    --   "below": [{...}, ...]}`.
    -- Default '{}' when heatmap missing or already-open HTF skip empties cache.
    liq_heatmap_top_clusters_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_trades_outcome      ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts     ON trades(entry_timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts      ON trades(exit_timestamp);

CREATE TABLE IF NOT EXISTS rejected_signals (
    rejection_id        TEXT PRIMARY KEY,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    reject_reason       TEXT NOT NULL,
    signal_timestamp    TEXT NOT NULL,

    price               REAL,
    atr                 REAL,
    confluence_score    REAL NOT NULL DEFAULT 0,
    confluence_factors  TEXT NOT NULL DEFAULT '[]',

    -- entry_timeframe / htf_timeframe / regime_at_entry dropped 2026-04-27
    -- (parity with trades; 1-distinct constants).
    htf_bias            TEXT,
    session             TEXT,
    market_structure    TEXT,

    -- 2026-04-29 — Pass 2.5 reject pegger re-add. proposed_sl_price /
    -- proposed_tp_price / proposed_rr_ratio populated by `_record_reject`
    -- at reject time (ATR-based what-if for pre-fill rejects, pending
    -- plan_sl/tp forward for pending-cancel rejects). Counter-factual
    -- outcome (`hypothetical_*` below) stamped by Bybit-native pegger
    -- (`scripts/peg_rejected_outcomes.py`).
    proposed_sl_price   REAL,
    proposed_tp_price   REAL,
    proposed_rr_ratio   REAL,

    funding_z_at_entry                  REAL,
    ls_ratio_at_entry                   REAL,
    oi_change_24h_at_entry              REAL,
    liq_imbalance_1h_at_entry           REAL,
    nearest_liq_cluster_above_price     REAL,
    nearest_liq_cluster_below_price     REAL,
    nearest_liq_cluster_above_notional  REAL,
    nearest_liq_cluster_below_notional  REAL,
    nearest_liq_cluster_above_distance_atr REAL,
    nearest_liq_cluster_below_distance_atr REAL,

    pillar_btc_bias     TEXT,
    pillar_eth_bias     TEXT,

    -- 2026-04-29 — Pass 2.5 reject pegger re-add. Forward-walk Bybit
    -- klines from signal_timestamp; LONG → first SL hit = LOSS, first TP
    -- hit = WIN; SHORT → mirrored. Same-bar SL+TP collision resolves
    -- pessimistic (SL first). 100-bar lookforward → TIMEOUT.
    hypothetical_outcome      TEXT,
    hypothetical_bars_to_tp   INTEGER,
    hypothetical_bars_to_sl   INTEGER,

    on_chain_context        TEXT,

    -- 2026-04-22 — mirrors trades.confluence_pillar_scores. Rejected-signal
    -- counter-factuals feed Pass 2 per-pillar weight tuning too: removing
    -- `mss_alignment` weight might flip a reject into accept; GBT/Optuna
    -- needs the raw weights to simulate that.
    confluence_pillar_scores TEXT NOT NULL DEFAULT '{}',

    -- 2026-04-22 (gece, late) — mirrors trades.oscillator_raw_values.
    -- Rejected signals also carry per-TF oscillator numerics so Pass 2
    -- counter-factual analysis has continuous features for the reject
    -- subset too (e.g., "would a lower oscillator_raw_values.3m.rsi
    -- threshold have admitted this reject?").
    oscillator_raw_values TEXT NOT NULL DEFAULT '{}',

    -- 2026-04-23 — mirrors trades.* extended derivatives enrichment.
    -- Rejected signals carry the same snapshot so Pass 3 counter-factual
    -- analysis can test "would trade have opened at this OI/funding
    -- state?" even for the reject subset.
    open_interest_usd_at_entry       REAL,
    oi_change_1h_pct_at_entry        REAL,
    funding_rate_current_at_entry    REAL,
    funding_rate_predicted_at_entry  REAL,
    long_liq_notional_1h_at_entry    REAL,
    short_liq_notional_1h_at_entry   REAL,
    ls_ratio_zscore_14d_at_entry     REAL,
    -- price_change_1h_pct_at_entry / price_change_4h_pct_at_entry kept
    -- on rejected_signals (F1 plumbing fills these for synchronous
    -- rejects with candle buffer). Pending-cancel rows stay NULL.
    price_change_1h_pct_at_entry     REAL,
    price_change_4h_pct_at_entry     REAL,
    liq_heatmap_top_clusters_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_rejected_symbol_ts  ON rejected_signals(symbol, signal_timestamp);
CREATE INDEX IF NOT EXISTS idx_rejected_reason     ON rejected_signals(reject_reason);
-- 2026-04-29 — Pass 2.5 reject pegger re-add (paired with hypothetical_outcome above).
CREATE INDEX IF NOT EXISTS idx_rejected_outcome    ON rejected_signals(hypothetical_outcome);

-- 2026-04-21 — Arkham on-chain snapshot time-series (Phase 8 data layer).
-- One row per detected snapshot MUTATION (not per tick). Runner writes
-- through `record_on_chain_snapshot` only when the fingerprint changes,
-- so cadence matches Arkham's own refresh rhythm (~hourly pulse, hourly
-- altcoin index, daily bias). Phase 9 joins this onto `trades` via
-- `entry_timestamp <= captured_at <= exit_timestamp` to reconstruct
-- what on-chain regime the trade lived through.
CREATE TABLE IF NOT EXISTS on_chain_snapshots (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at                     TEXT NOT NULL,
    daily_macro_bias                TEXT,
    stablecoin_pulse_1h_usd         REAL,
    cex_btc_netflow_24h_usd         REAL,
    cex_eth_netflow_24h_usd         REAL,
    altcoin_index                   REAL,
    -- coinbase_asia_skew_usd / bnb_self_flow_24h_usd dropped 2026-04-27
    -- (schema placeholders, never implemented).
    -- snapshot_age_s / fresh / whale_blackout_active dropped 2026-04-27
    -- (1-distinct constants — always 0 / 1 / 0 post-2026-04-22 whale
    -- gate removal). Snapshot freshness implicit in `captured_at`.
    -- 2026-04-22 — per-entity 24h netflow (last completed UTC day) via
    -- /flow/entity/{entity}. Journal-only; Phase 9 GBT decides predictive value.
    cex_coinbase_netflow_24h_usd    REAL,
    cex_binance_netflow_24h_usd     REAL,
    cex_bybit_netflow_24h_usd       REAL,
    -- 2026-04-22 — per-symbol most-recent-hour CEX flow via
    -- /token/volume/{id}?granularity=1h. JSON-encoded dict so adding a 6th
    -- watched symbol won't trigger a schema migration.
    token_volume_1h_net_usd_json    TEXT,
    -- 2026-04-23 (night-late) — 4th + 5th venues. Live probe vs.
    -- `type:cex` aggregate showed named-entity coverage (CB+BN+BY) captured
    -- only ~1-6% of the full CEX BTC netflow signal. Bitfinex (+$193M/24h,
    -- Tether-adjacent, historical BTC lead) and Kraken (−$216M/24h,
    -- Western retail/institutional exit) were the largest single named
    -- inflow / outflow. Journal-only; Pass 3 Optuna decides whether to
    -- wire into _flow_alignment_score (today 6 inputs, weights 0.25/0.25/
    -- 0.15/0.15/0.10/0.10).
    cex_bitfinex_netflow_24h_usd    REAL,
    cex_kraken_netflow_24h_usd      REAL,
    -- 2026-04-24 — 6th venue: OKX self-signal. Bot trades on OKX so
    -- this captures the venue's own flow. 24h net ≈ 0 structurally
    -- (turnover ~$1.86B but balanced in/out, max hourly |net| $58M).
    -- Journal-only; Pass 3 decides whether a 1h-window OKX slot adds value.
    cex_okx_netflow_24h_usd         REAL,
    -- 2026-04-26 — per-venue × per-asset 24h netflow (BTC / ETH / stables).
    -- JSON dict keyed by entity slug (coinbase/binance/bybit/bitfinex/kraken/
    -- okx) → signed USD float. Adding a 7th venue won't require schema
    -- migration. Powers the dashboard's per-venue per-asset chart; not yet
    -- wired into any runtime scoring (Pass 3 candidate).
    cex_per_venue_btc_netflow_24h_usd_json     TEXT,
    cex_per_venue_eth_netflow_24h_usd_json     TEXT,
    cex_per_venue_stables_netflow_24h_usd_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_on_chain_snap_captured_at ON on_chain_snapshots(captured_at);

-- 2026-04-22 — whale_transfers time-series (Phase 8 data layer).
-- Streamed from `ArkhamWebSocketListener._handle` when a transfer crosses
-- `whale_threshold_usd` and the token is in the configured whitelist.
-- Runtime entry gate removed 2026-04-22 — this table exists purely for
-- Phase 9 GBT analysis (join against trades via captured_at). Stores the
-- raw event so directional classification (Coinbase→Binance etc.) can be
-- learned from outcomes instead of hardcoded.
CREATE TABLE IF NOT EXISTS whale_transfers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,
    token            TEXT NOT NULL,
    usd_value        REAL NOT NULL,
    from_entity      TEXT,
    to_entity        TEXT,
    tx_hash          TEXT,
    -- JSON list of internal canonical perp symbols affected (from `affected_symbols_for`).
    affected_symbols TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_whale_transfers_captured_at ON whale_transfers(captured_at);
CREATE INDEX IF NOT EXISTS idx_whale_transfers_token       ON whale_transfers(token);

-- 2026-04-26 — position_snapshots intra-trade time-series.
-- One row per OPEN position per `journal.position_snapshot_cadence_s`
-- (default 300s) with live mark/PnL + running MFE/MAE in R + current
-- SL/TP + lifecycle flags + drift fields for derivatives + on-chain +
-- 3m oscillator + VWAP-band distance. Joined back to `trades.trade_id`
-- at read time (soft FK; same pattern as on_chain_snapshots).
-- Cost-free: live mark/PnL come from the `get_positions` payload the
-- monitor already polls every 5s; drift fields read from BotContext
-- caches (no extra Bybit, no extra TV switch).
CREATE TABLE IF NOT EXISTS position_snapshots (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                        TEXT NOT NULL,
    captured_at                     TEXT NOT NULL,

    -- Live position state (Bybit get_positions response).
    mark_price                      REAL NOT NULL,
    unrealized_pnl_usdt             REAL NOT NULL,
    unrealized_pnl_r                REAL NOT NULL,

    -- Running excursion (tracked on every 5s poll, not just on snapshot
    -- write — peaks aren't missed between cadence ticks).
    mfe_r_so_far                    REAL NOT NULL,
    mae_r_so_far                    REAL NOT NULL,

    -- Active SL/TP and lifecycle flags (from monitor._Tracked).
    current_sl_price                REAL NOT NULL,
    current_tp_price                REAL,
    sl_to_be_moved                  INTEGER NOT NULL DEFAULT 0,
    mfe_lock_applied                INTEGER NOT NULL DEFAULT 0,

    -- Derivatives drift (from BotContext.derivatives_cache, may be NULL
    -- if Coinalyze fetch failed or symbol map cold).
    derivatives_funding_now         REAL,
    derivatives_oi_now_usd          REAL,
    derivatives_ls_ratio_now        REAL,
    derivatives_long_liq_1h_now     REAL,
    derivatives_short_liq_1h_now    REAL,

    -- On-chain drift (from BotContext.on_chain_snapshot, may be NULL
    -- if Arkham snapshot stale / disabled).
    on_chain_btc_netflow_now_usd    REAL,
    on_chain_stablecoin_pulse_now   REAL,
    on_chain_flow_alignment_now     REAL,

    -- Oscillator + VWAP drift (from BotContext.last_market_state_per_symbol,
    -- NULL on first cycle for that symbol post-restart).
    oscillator_3m_now_json          TEXT,
    vwap_3m_distance_atr_now        REAL
);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_trade_id    ON position_snapshots(trade_id);
CREATE INDEX IF NOT EXISTS idx_position_snapshots_captured_at ON position_snapshots(captured_at);
"""


# Column order for INSERT — kept in sync with _SCHEMA above so that
# _row_to_record and _record_to_row can round-trip without string matching.
_COLUMNS = [
    "trade_id", "symbol", "direction", "outcome",
    "signal_timestamp", "entry_timestamp", "exit_timestamp",
    "entry_price", "sl_price", "tp_price", "rr_ratio",
    "leverage", "num_contracts", "position_size_usdt", "risk_amount_usdt",
    "sl_source", "reason", "confluence_score", "confluence_factors",
    # 2026-04-27 drops on trades: algo_id, client_algo_id, algo_ids,
    # entry_timeframe, htf_timeframe, regime_at_entry, funding_z_6h,
    # funding_z_24h, notes, screenshot_entry, screenshot_exit,
    # price_change_1h_pct_at_entry, price_change_4h_pct_at_entry.
    "order_id", "client_order_id",
    "htf_bias", "session", "market_structure",
    "exit_price", "pnl_usdt", "pnl_r", "fees_usdt",
    "sl_moved_to_be", "close_reason",
    "funding_z_at_entry", "ls_ratio_at_entry",
    "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry",
    "nearest_liq_cluster_above_price", "nearest_liq_cluster_below_price",
    "nearest_liq_cluster_above_notional", "nearest_liq_cluster_below_notional",
    "nearest_liq_cluster_above_distance_atr", "nearest_liq_cluster_below_distance_atr",
    "setup_zone_source", "zone_wait_bars", "zone_fill_latency_bars",
    "trend_regime_at_entry",
    "real_market_entry_valid", "real_market_exit_valid",
    "demo_artifact", "artifact_reason",
    "on_chain_context",
    "confluence_pillar_scores",
    "oscillator_raw_values",
    "open_interest_usd_at_entry",
    "oi_change_1h_pct_at_entry",
    "funding_rate_current_at_entry",
    "funding_rate_predicted_at_entry",
    "long_liq_notional_1h_at_entry",
    "short_liq_notional_1h_at_entry",
    "ls_ratio_zscore_14d_at_entry",
    "liq_heatmap_top_clusters_json",
]


_REJECTED_COLUMNS = [
    "rejection_id", "symbol", "direction", "reject_reason", "signal_timestamp",
    "price", "atr", "confluence_score", "confluence_factors",
    # 2026-04-29 — Pass 2.5 reject pegger re-add. proposed_* set by
    # `_record_reject` at reject time (ATR-based what-if for pre-fill,
    # plan_sl/tp forward for pending-cancel). hypothetical_* set by
    # `scripts/peg_rejected_outcomes.py` (Bybit kline forward-walk).
    # 2026-04-27 drops still in effect: entry_timeframe, htf_timeframe,
    # regime_at_entry (1-distinct constants).
    "proposed_sl_price", "proposed_tp_price", "proposed_rr_ratio",
    "htf_bias", "session", "market_structure",
    "funding_z_at_entry", "ls_ratio_at_entry",
    "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry",
    "nearest_liq_cluster_above_price", "nearest_liq_cluster_below_price",
    "nearest_liq_cluster_above_notional", "nearest_liq_cluster_below_notional",
    "nearest_liq_cluster_above_distance_atr", "nearest_liq_cluster_below_distance_atr",
    "pillar_btc_bias", "pillar_eth_bias",
    "hypothetical_outcome", "hypothetical_bars_to_tp", "hypothetical_bars_to_sl",
    "on_chain_context",
    "confluence_pillar_scores",
    "oscillator_raw_values",
    "open_interest_usd_at_entry",
    "oi_change_1h_pct_at_entry",
    "funding_rate_current_at_entry",
    "funding_rate_predicted_at_entry",
    "long_liq_notional_1h_at_entry",
    "short_liq_notional_1h_at_entry",
    "ls_ratio_zscore_14d_at_entry",
    "price_change_1h_pct_at_entry",
    "price_change_4h_pct_at_entry",
    "liq_heatmap_top_clusters_json",
]


# 2026-04-26 — Column order for INSERT into position_snapshots.
# Mirrors `_SCHEMA` table layout (excluding the AUTOINCREMENT `id`).
_POSITION_SNAPSHOT_COLUMNS = [
    "trade_id", "captured_at",
    "mark_price", "unrealized_pnl_usdt", "unrealized_pnl_r",
    "mfe_r_so_far", "mae_r_so_far",
    "current_sl_price", "current_tp_price",
    "sl_to_be_moved", "mfe_lock_applied",
    "derivatives_funding_now", "derivatives_oi_now_usd",
    "derivatives_ls_ratio_now",
    "derivatives_long_liq_1h_now", "derivatives_short_liq_1h_now",
    "on_chain_btc_netflow_now_usd", "on_chain_stablecoin_pulse_now",
    "on_chain_flow_alignment_now",
    "oscillator_3m_now_json", "vwap_3m_distance_atr_now",
]


# Idempotent migrations — each `ALTER TABLE ... ADD COLUMN` is wrapped in
# a try/except so re-running on a DB that already has the column is a no-op.
_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN algo_ids TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE trades ADD COLUMN sl_moved_to_be INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN close_reason TEXT",
    # Phase 1.5 Madde 7 — derivatives snapshot at entry time.
    "ALTER TABLE trades ADD COLUMN regime_at_entry TEXT",
    "ALTER TABLE trades ADD COLUMN funding_z_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN ls_ratio_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN oi_change_24h_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN liq_imbalance_1h_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_above_price REAL",
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_below_price REAL",
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_above_notional REAL",
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_below_notional REAL",
    # BLOK D-7 — cluster distance in ATR units, pre-computed at entry.
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_above_distance_atr REAL",
    "ALTER TABLE trades ADD COLUMN nearest_liq_cluster_below_distance_atr REAL",
    # Phase 7.B5 schema v2 — zone-entry context + ADX regime + windowed funding.
    "ALTER TABLE trades ADD COLUMN setup_zone_source TEXT",
    "ALTER TABLE trades ADD COLUMN zone_wait_bars INTEGER",
    "ALTER TABLE trades ADD COLUMN zone_fill_latency_bars INTEGER",
    "ALTER TABLE trades ADD COLUMN trend_regime_at_entry TEXT",
    "ALTER TABLE trades ADD COLUMN funding_z_6h REAL",
    "ALTER TABLE trades ADD COLUMN funding_z_24h REAL",
    # 2026-04-19 — demo-wick artefact cross-check. SQLite has no BOOLEAN
    # type; we use INTEGER (0/1) with NULL for "couldn't run the check".
    "ALTER TABLE trades ADD COLUMN real_market_entry_valid INTEGER",
    "ALTER TABLE trades ADD COLUMN real_market_exit_valid INTEGER",
    "ALTER TABLE trades ADD COLUMN demo_artifact INTEGER",
    "ALTER TABLE trades ADD COLUMN artifact_reason TEXT",
    # 2026-04-21 — Arkham on-chain enrichment. JSON-serialised dict
    # (daily_macro_bias, stablecoin_pulse_1h_usd, cex_*_netflow_24h_usd,
    # whale_blackout_active, snapshot_age_s). NULL on rows written
    # before the Arkham pipeline was enabled, or when `on_chain.enabled`
    # was off at open-time. Present on both trades and rejected_signals
    # so factor-audit can segment rejects by on-chain context too.
    "ALTER TABLE trades ADD COLUMN on_chain_context TEXT",
    "ALTER TABLE rejected_signals ADD COLUMN on_chain_context TEXT",
    # 2026-04-22 — per-entity (Coinbase, Binance, Bybit) 24h netflow + per-symbol
    # 1h CEX volume (JSON dict). Journal-only enrichment for Phase 9 GBT.
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_coinbase_netflow_24h_usd REAL",
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_binance_netflow_24h_usd REAL",
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_bybit_netflow_24h_usd REAL",
    "ALTER TABLE on_chain_snapshots ADD COLUMN token_volume_1h_net_usd_json TEXT",
    # 2026-04-22 (gece) — per-pillar raw confluence scores JSON. Unlocks
    # Pass 2 per-pillar weight tuning. Default '{}' so pre-migration rows
    # decode as an empty dict (no attributed weights).
    "ALTER TABLE trades ADD COLUMN confluence_pillar_scores TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE rejected_signals ADD COLUMN confluence_pillar_scores TEXT NOT NULL DEFAULT '{}'",
    # 2026-04-22 (gece, late) — per-TF oscillator raw values JSON. Unlocks
    # continuous-feature GBT on RSI / WaveTrend / Stoch / MFI magnitudes
    # across 1m/3m/15m. Default '{}' for legacy rows. Empty dict on fresh
    # rows when upstream caches are unavailable (tests without bridge etc.).
    "ALTER TABLE trades ADD COLUMN oscillator_raw_values TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE rejected_signals ADD COLUMN oscillator_raw_values TEXT NOT NULL DEFAULT '{}'",
    # 2026-04-23 — extended derivatives enrichment (9 REAL + 1 TEXT).
    # All 9 numeric fields were already on DerivativesState; 4 were being
    # written (regime / funding_z_30d / ls_ratio / oi_change_24h / liq_imb_1h);
    # the other 9 joined-later for Pass 3 GBT continuous-feature search.
    # price_change_1h/4h_pct_at_entry derived from the entry-TF candle
    # buffer (zero extra API cost). liq_heatmap_top_clusters_json carries
    # top-5 above + top-5 below JSON for richer magnet / target modelling.
    "ALTER TABLE trades ADD COLUMN open_interest_usd_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN oi_change_1h_pct_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN funding_rate_current_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN funding_rate_predicted_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN long_liq_notional_1h_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN short_liq_notional_1h_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN ls_ratio_zscore_14d_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN price_change_1h_pct_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN price_change_4h_pct_at_entry REAL",
    "ALTER TABLE trades ADD COLUMN liq_heatmap_top_clusters_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE rejected_signals ADD COLUMN open_interest_usd_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN oi_change_1h_pct_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN funding_rate_current_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN funding_rate_predicted_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN long_liq_notional_1h_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN short_liq_notional_1h_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN ls_ratio_zscore_14d_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN price_change_1h_pct_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN price_change_4h_pct_at_entry REAL",
    "ALTER TABLE rejected_signals ADD COLUMN liq_heatmap_top_clusters_json TEXT NOT NULL DEFAULT '{}'",
    # 2026-04-24 (evening) — per-exchange derivatives JSON columns REMOVED.
    # Tier A ADDs (trades + rejected_signals × 3 cols = 6 migrations) were
    # rolled back after 4 iterations of chasing Coinalyze 429 rate-limits
    # (free tier 40/min enforced server-side, even on /open-interest
    # endpoint alone). Per-symbol refresh baseline at ~20 calls/cycle left
    # no stable budget for 3 additional per-exchange batch calls. SQLite's
    # DROP COLUMN requires 3.35+ and `ALTER TABLE ... DROP COLUMN` is
    # idempotent-friendly when wrapped in _apply_migrations' try/except
    # (IFEXISTS not supported for DROP COLUMN on trades/rejected_signals).
    # Old data on fresh post-restart rows is all '{}' defaults, no real
    # data lost. Drops below are harmless no-ops on DBs that never had
    # these columns (the catch block in _apply_migrations swallows
    # "no such column" errors, same as existing schema drift tolerance).
    "ALTER TABLE trades DROP COLUMN oi_per_exchange_usd_json_at_entry",
    "ALTER TABLE trades DROP COLUMN funding_rate_per_exchange_json_at_entry",
    "ALTER TABLE trades DROP COLUMN funding_rate_predicted_per_exchange_json_at_entry",
    "ALTER TABLE rejected_signals DROP COLUMN oi_per_exchange_usd_json_at_entry",
    "ALTER TABLE rejected_signals DROP COLUMN funding_rate_per_exchange_json_at_entry",
    "ALTER TABLE rejected_signals DROP COLUMN funding_rate_predicted_per_exchange_json_at_entry",
    # 2026-04-23 (night-late) — 4th + 5th venues added journal-only. Live probe vs.
    # `type:cex` aggregate showed named-entity coverage (CB+BN+BY) captured only
    # ~1-6% of the full CEX BTC netflow signal. Bitfinex (biggest single named
    # INFLOW, Tether-adjacent) and Kraken (biggest single named OUTFLOW, Western
    # retail/institutional exit) added as the two most informative additions.
    # Pre-migration rows keep NULL — Pass 3 tune drops first ~N rows where
    # these are NULL from per-entity feature columns.
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_bitfinex_netflow_24h_usd REAL",
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_kraken_netflow_24h_usd REAL",
    # 2026-04-24 — 6th venue: OKX (bot's own trading exchange). Journal-only;
    # 24h net ≈ 0 by design but captured for parity + Pass 3 exploration.
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_okx_netflow_24h_usd REAL",
    # 2026-04-26 — per-venue × per-asset 24h netflow (BTC / ETH / stables).
    # JSON dicts keyed by entity slug. Powers the dashboard's per-venue
    # per-asset chart; not yet wired into runtime scoring (Pass 3 candidate).
    # Adding a 7th venue won't trigger a migration.
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_per_venue_btc_netflow_24h_usd_json TEXT",
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_per_venue_eth_netflow_24h_usd_json TEXT",
    "ALTER TABLE on_chain_snapshots ADD COLUMN cex_per_venue_stables_netflow_24h_usd_json TEXT",
    # 2026-04-26 — position_snapshots intra-trade time-series for RL trajectory
    # data. Idempotent CREATE on existing DBs (CREATE TABLE IF NOT EXISTS already
    # in _SCHEMA; explicit migration here lets the indexes land too on bases
    # where _SCHEMA was applied before this commit). Each statement wrapped in
    # the OperationalError swallow loop in connect() so re-running is a no-op.
    "CREATE TABLE IF NOT EXISTS position_snapshots ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "trade_id TEXT NOT NULL, captured_at TEXT NOT NULL, "
    "mark_price REAL NOT NULL, unrealized_pnl_usdt REAL NOT NULL, "
    "unrealized_pnl_r REAL NOT NULL, "
    "mfe_r_so_far REAL NOT NULL, mae_r_so_far REAL NOT NULL, "
    "current_sl_price REAL NOT NULL, current_tp_price REAL, "
    "sl_to_be_moved INTEGER NOT NULL DEFAULT 0, "
    "mfe_lock_applied INTEGER NOT NULL DEFAULT 0, "
    "derivatives_funding_now REAL, derivatives_oi_now_usd REAL, "
    "derivatives_ls_ratio_now REAL, "
    "derivatives_long_liq_1h_now REAL, derivatives_short_liq_1h_now REAL, "
    "on_chain_btc_netflow_now_usd REAL, on_chain_stablecoin_pulse_now REAL, "
    "on_chain_flow_alignment_now REAL, "
    "oscillator_3m_now_json TEXT, vwap_3m_distance_atr_now REAL"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_trade_id "
    "ON position_snapshots(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_captured_at "
    "ON position_snapshots(captured_at)",
    # 2026-04-27 — schema cleanup pass. Drop 27 columns that audit
    # confirmed are either 100% NULL across the Bybit dataset
    # (kod doldurmuyor) or 1-distinct constants (no information).
    # Operator directive: "veri gelmemişse hiç droplayalım. RL için 50
    # trade biriktiğinde gerekirse re-add candidate." Each DROP is
    # wrapped by `_apply_migrations`'s OperationalError swallow loop
    # so re-running on already-cleaned DBs is a no-op (matches the
    # 2026-04-24 per-exchange rollback pattern).
    #
    # Re-add candidates (with reasons + how to revive):
    #   - trades.algo_id / client_algo_id / algo_ids: Bybit V5 has
    #     position-attached TP/SL (no separate algo orders), so these
    #     columns stay empty by architecture. Re-add ONLY if migrating
    #     back to an exchange with separate algo orders.
    #   - trades.notes / screenshot_entry / screenshot_exit: manual
    #     operator-fill columns; bot never writes them. Re-add if a
    #     post-hoc annotation workflow gets implemented.
    #   - trades.funding_z_6h / funding_z_24h: Phase 12 deferred — needs
    #     timestamp-aware refactor of the funding history buffer. Re-add
    #     when that refactor lands. RL pipeline can compute rolling z
    #     over `derivatives_snapshots` directly in the meantime.
    #   - trades.price_change_1h / 4h_pct_at_entry: by-design NULL on
    #     pending-fill entries (every Bybit-era trade is pending-fill).
    #     Re-add ONLY if a market-entry path is reactivated and that
    #     path stashes entry-TF candles for the writer.
    #   - trades.entry_timeframe / htf_timeframe / regime_at_entry: 1
    #     distinct value (config sabit '3m' / '15m', and DerivativesRegime
    #     classifier always returns 'BALANCED'). Re-add entry/htf
    #     timeframe columns when multiple TF configs run side-by-side.
    #     regime_at_entry re-add when DerivativesRegime classifier is
    #     reworked to emit non-BALANCED states (`trend_regime_at_entry`
    #     ADX-based, 3-distinct, stays in schema).
    #   - rejected_signals.proposed_sl/tp/rr_price + hypothetical_*:
    #     entry path doesn't compute proposed SL/TP at reject time and
    #     the peg-script (which would forward-walk and stamp outcomes)
    #     was deleted in the post-migration cleanup Phase 3 when its
    #     python-okx dependency was removed. Re-add as a pair if a
    #     Bybit-native peg script gets written AND `_record_reject`
    #     starts computing ATR-based proposed SL/TP for what-if analysis.
    #   - rejected_signals.entry_timeframe / htf_timeframe /
    #     regime_at_entry: same parity rationale as trades.
    #   - on_chain_snapshots.coinbase_asia_skew_usd / bnb_self_flow_24h:
    #     schema placeholders never implemented. Re-add only if the
    #     specific signal gets defined and a fetcher built.
    #   - on_chain_snapshots.snapshot_age_s / fresh /
    #     whale_blackout_active: 1 distinct constant (always 0 / 1 / 0
    #     respectively post-2026-04-22 whale-gate removal). Snapshot
    #     freshness is implicit in `captured_at`; the boolean flags
    #     carry no information. Re-add if a future use makes them
    #     actually mutate.
    #
    # trades drops (13)
    "ALTER TABLE trades DROP COLUMN algo_id",
    "ALTER TABLE trades DROP COLUMN client_algo_id",
    "ALTER TABLE trades DROP COLUMN algo_ids",
    "ALTER TABLE trades DROP COLUMN notes",
    "ALTER TABLE trades DROP COLUMN screenshot_entry",
    "ALTER TABLE trades DROP COLUMN screenshot_exit",
    "ALTER TABLE trades DROP COLUMN funding_z_6h",
    "ALTER TABLE trades DROP COLUMN funding_z_24h",
    "ALTER TABLE trades DROP COLUMN price_change_1h_pct_at_entry",
    "ALTER TABLE trades DROP COLUMN price_change_4h_pct_at_entry",
    "ALTER TABLE trades DROP COLUMN entry_timeframe",
    "ALTER TABLE trades DROP COLUMN htf_timeframe",
    "ALTER TABLE trades DROP COLUMN regime_at_entry",
    # idx_rejected_outcome targets the dropped hypothetical_outcome
    # column. SQLite DROP COLUMN auto-removes column-bound indexes but
    # explicit DROP INDEX is harmless and clearer.
    "DROP INDEX IF EXISTS idx_rejected_outcome",
    # rejected_signals drops (9)
    "ALTER TABLE rejected_signals DROP COLUMN proposed_sl_price",
    "ALTER TABLE rejected_signals DROP COLUMN proposed_tp_price",
    "ALTER TABLE rejected_signals DROP COLUMN proposed_rr_ratio",
    "ALTER TABLE rejected_signals DROP COLUMN hypothetical_outcome",
    "ALTER TABLE rejected_signals DROP COLUMN hypothetical_bars_to_tp",
    "ALTER TABLE rejected_signals DROP COLUMN hypothetical_bars_to_sl",
    "ALTER TABLE rejected_signals DROP COLUMN entry_timeframe",
    "ALTER TABLE rejected_signals DROP COLUMN htf_timeframe",
    "ALTER TABLE rejected_signals DROP COLUMN regime_at_entry",
    # on_chain_snapshots drops (5)
    "ALTER TABLE on_chain_snapshots DROP COLUMN coinbase_asia_skew_usd",
    "ALTER TABLE on_chain_snapshots DROP COLUMN bnb_self_flow_24h_usd",
    "ALTER TABLE on_chain_snapshots DROP COLUMN snapshot_age_s",
    "ALTER TABLE on_chain_snapshots DROP COLUMN fresh",
    "ALTER TABLE on_chain_snapshots DROP COLUMN whale_blackout_active",
    # 2026-04-29 — Pass 2.5 reject pegger re-add. Reverses the 2026-04-27
    # rejected_signals proposed_*/hypothetical_* drops above. Order matters:
    # the DROP statements run first (no-op on DBs that never had the
    # columns), then the ADD COLUMN statements re-create them. Idempotent
    # via the OperationalError swallow loop in `connect()`.
    "ALTER TABLE rejected_signals ADD COLUMN proposed_sl_price REAL",
    "ALTER TABLE rejected_signals ADD COLUMN proposed_tp_price REAL",
    "ALTER TABLE rejected_signals ADD COLUMN proposed_rr_ratio REAL",
    "ALTER TABLE rejected_signals ADD COLUMN hypothetical_outcome TEXT",
    "ALTER TABLE rejected_signals ADD COLUMN hypothetical_bars_to_tp INTEGER",
    "ALTER TABLE rejected_signals ADD COLUMN hypothetical_bars_to_sl INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_rejected_outcome ON rejected_signals(hypothetical_outcome)",
]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _record_to_row(rec: TradeRecord) -> tuple:
    return (
        rec.trade_id, rec.symbol, rec.direction.value, rec.outcome.value,
        _iso(rec.signal_timestamp), _iso(rec.entry_timestamp), _iso(rec.exit_timestamp),
        rec.entry_price, rec.sl_price, rec.tp_price, rec.rr_ratio,
        rec.leverage, rec.num_contracts, rec.position_size_usdt, rec.risk_amount_usdt,
        rec.sl_source, rec.reason, rec.confluence_score,
        json.dumps(rec.confluence_factors),
        rec.order_id, rec.client_order_id,
        rec.htf_bias, rec.session, rec.market_structure,
        rec.exit_price, rec.pnl_usdt, rec.pnl_r, rec.fees_usdt,
        int(rec.sl_moved_to_be), rec.close_reason,
        rec.funding_z_at_entry, rec.ls_ratio_at_entry,
        rec.oi_change_24h_at_entry, rec.liq_imbalance_1h_at_entry,
        rec.nearest_liq_cluster_above_price, rec.nearest_liq_cluster_below_price,
        rec.nearest_liq_cluster_above_notional, rec.nearest_liq_cluster_below_notional,
        rec.nearest_liq_cluster_above_distance_atr, rec.nearest_liq_cluster_below_distance_atr,
        rec.setup_zone_source, rec.zone_wait_bars, rec.zone_fill_latency_bars,
        rec.trend_regime_at_entry,
        (None if rec.real_market_entry_valid is None
         else int(rec.real_market_entry_valid)),
        (None if rec.real_market_exit_valid is None
         else int(rec.real_market_exit_valid)),
        (None if rec.demo_artifact is None else int(rec.demo_artifact)),
        rec.artifact_reason,
        (json.dumps(rec.on_chain_context)
         if rec.on_chain_context is not None else None),
        json.dumps(rec.confluence_pillar_scores or {}),
        json.dumps(rec.oscillator_raw_values or {}),
        rec.open_interest_usd_at_entry,
        rec.oi_change_1h_pct_at_entry,
        rec.funding_rate_current_at_entry,
        rec.funding_rate_predicted_at_entry,
        rec.long_liq_notional_1h_at_entry,
        rec.short_liq_notional_1h_at_entry,
        rec.ls_ratio_zscore_14d_at_entry,
        json.dumps(rec.liq_heatmap_top_clusters or {}),
    )


def _rejected_to_row(rec: RejectedSignal) -> tuple:
    return (
        rec.rejection_id, rec.symbol, rec.direction.value, rec.reject_reason,
        _iso(rec.signal_timestamp),
        rec.price, rec.atr, rec.confluence_score,
        json.dumps(rec.confluence_factors),
        rec.proposed_sl_price, rec.proposed_tp_price, rec.proposed_rr_ratio,
        rec.htf_bias, rec.session, rec.market_structure,
        rec.funding_z_at_entry, rec.ls_ratio_at_entry,
        rec.oi_change_24h_at_entry, rec.liq_imbalance_1h_at_entry,
        rec.nearest_liq_cluster_above_price, rec.nearest_liq_cluster_below_price,
        rec.nearest_liq_cluster_above_notional, rec.nearest_liq_cluster_below_notional,
        rec.nearest_liq_cluster_above_distance_atr, rec.nearest_liq_cluster_below_distance_atr,
        rec.pillar_btc_bias, rec.pillar_eth_bias,
        rec.hypothetical_outcome, rec.hypothetical_bars_to_tp, rec.hypothetical_bars_to_sl,
        (json.dumps(rec.on_chain_context)
         if rec.on_chain_context is not None else None),
        json.dumps(rec.confluence_pillar_scores or {}),
        json.dumps(rec.oscillator_raw_values or {}),
        rec.open_interest_usd_at_entry,
        rec.oi_change_1h_pct_at_entry,
        rec.funding_rate_current_at_entry,
        rec.funding_rate_predicted_at_entry,
        rec.long_liq_notional_1h_at_entry,
        rec.short_liq_notional_1h_at_entry,
        rec.ls_ratio_zscore_14d_at_entry,
        rec.price_change_1h_pct_at_entry,
        rec.price_change_4h_pct_at_entry,
        json.dumps(rec.liq_heatmap_top_clusters or {}),
    )


def _row_to_rejected(row: aiosqlite.Row) -> RejectedSignal:
    return RejectedSignal(
        rejection_id=row["rejection_id"],
        symbol=row["symbol"],
        direction=Direction(row["direction"]),
        reject_reason=row["reject_reason"],
        signal_timestamp=_parse_iso(row["signal_timestamp"]),
        price=row["price"],
        atr=row["atr"],
        confluence_score=row["confluence_score"],
        confluence_factors=json.loads(row["confluence_factors"] or "[]"),
        proposed_sl_price=_safe_col(row, "proposed_sl_price"),
        proposed_tp_price=_safe_col(row, "proposed_tp_price"),
        proposed_rr_ratio=_safe_col(row, "proposed_rr_ratio"),
        htf_bias=row["htf_bias"],
        session=row["session"],
        market_structure=row["market_structure"],
        funding_z_at_entry=row["funding_z_at_entry"],
        ls_ratio_at_entry=row["ls_ratio_at_entry"],
        oi_change_24h_at_entry=row["oi_change_24h_at_entry"],
        liq_imbalance_1h_at_entry=row["liq_imbalance_1h_at_entry"],
        nearest_liq_cluster_above_price=row["nearest_liq_cluster_above_price"],
        nearest_liq_cluster_below_price=row["nearest_liq_cluster_below_price"],
        nearest_liq_cluster_above_notional=row["nearest_liq_cluster_above_notional"],
        nearest_liq_cluster_below_notional=row["nearest_liq_cluster_below_notional"],
        nearest_liq_cluster_above_distance_atr=row["nearest_liq_cluster_above_distance_atr"],
        nearest_liq_cluster_below_distance_atr=row["nearest_liq_cluster_below_distance_atr"],
        pillar_btc_bias=row["pillar_btc_bias"],
        pillar_eth_bias=row["pillar_eth_bias"],
        hypothetical_outcome=_safe_col(row, "hypothetical_outcome"),
        hypothetical_bars_to_tp=_safe_col(row, "hypothetical_bars_to_tp"),
        hypothetical_bars_to_sl=_safe_col(row, "hypothetical_bars_to_sl"),
        on_chain_context=_parse_on_chain_context(row),
        confluence_pillar_scores=_parse_pillar_scores(row),
        oscillator_raw_values=_parse_oscillator_raw_values(row),
        open_interest_usd_at_entry=_safe_col(row, "open_interest_usd_at_entry"),
        oi_change_1h_pct_at_entry=_safe_col(row, "oi_change_1h_pct_at_entry"),
        funding_rate_current_at_entry=_safe_col(row, "funding_rate_current_at_entry"),
        funding_rate_predicted_at_entry=_safe_col(row, "funding_rate_predicted_at_entry"),
        long_liq_notional_1h_at_entry=_safe_col(row, "long_liq_notional_1h_at_entry"),
        short_liq_notional_1h_at_entry=_safe_col(row, "short_liq_notional_1h_at_entry"),
        ls_ratio_zscore_14d_at_entry=_safe_col(row, "ls_ratio_zscore_14d_at_entry"),
        price_change_1h_pct_at_entry=_safe_col(row, "price_change_1h_pct_at_entry"),
        price_change_4h_pct_at_entry=_safe_col(row, "price_change_4h_pct_at_entry"),
        liq_heatmap_top_clusters=_parse_liq_heatmap_clusters(row),
    )


def _parse_liq_heatmap_clusters(row: aiosqlite.Row) -> dict:
    """Decode `liq_heatmap_top_clusters_json`; `{}` on any issue.

    Expected shape: `{"above": [{price, notional_usd, distance_atr}, ...],
    "below": [...]}`. Missing/malformed → empty dict; non-dict top-level
    → empty dict; list values with non-dict entries filtered."""
    raw = _safe_col(row, "liq_heatmap_top_clusters_json")
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, list] = {}
    for side in ("above", "below"):
        value = parsed.get(side)
        if isinstance(value, list):
            out[side] = [dict(c) for c in value if isinstance(c, dict)]
    return out


def _parse_oscillator_raw_values(row: aiosqlite.Row) -> dict[str, dict]:
    """Decode `oscillator_raw_values` JSON; empty dict on any issue.

    Expected shape: `{"1m": {...}, "3m": {...}, "15m": {...}}` — any TF
    subset is valid. Each TF value is a dict of OscillatorTableData
    fields. Malformed rows, legacy rows, and non-dict values decode as
    `{}` so downstream consumers never see a surprising shape.
    """
    raw = _safe_col(row, "oscillator_raw_values")
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # Filter to well-formed entries — each value must itself be a dict.
    out: dict[str, dict] = {}
    for tf, value in parsed.items():
        if isinstance(value, dict):
            out[str(tf)] = dict(value)
    return out


def _parse_pillar_scores(row: aiosqlite.Row) -> dict[str, float]:
    """Decode `confluence_pillar_scores` JSON; empty dict on any issue so
    legacy rows and malformed entries read as empty rather than erroring."""
    raw = _safe_col(row, "confluence_pillar_scores")
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # Coerce values to float where possible; drop non-numeric entries.
    out: dict[str, float] = {}
    for key, value in parsed.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _parse_on_chain_context(row: aiosqlite.Row) -> Optional[dict]:
    """Decode the JSON `on_chain_context` column; None on missing / null /
    invalid JSON so legacy rows and migration edges read as absent rather
    than erroring."""
    raw = _safe_col(row, "on_chain_context")
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _row_to_record(row: aiosqlite.Row) -> TradeRecord:
    return TradeRecord(
        trade_id=row["trade_id"],
        symbol=row["symbol"],
        direction=Direction(row["direction"]),
        outcome=TradeOutcome(row["outcome"]),
        signal_timestamp=_parse_iso(row["signal_timestamp"]),
        entry_timestamp=_parse_iso(row["entry_timestamp"]),
        exit_timestamp=_parse_iso(row["exit_timestamp"]),
        entry_price=row["entry_price"],
        sl_price=row["sl_price"],
        tp_price=row["tp_price"],
        rr_ratio=row["rr_ratio"],
        leverage=row["leverage"],
        num_contracts=row["num_contracts"],
        position_size_usdt=row["position_size_usdt"],
        risk_amount_usdt=row["risk_amount_usdt"],
        sl_source=row["sl_source"] or "",
        reason=row["reason"] or "",
        confluence_score=row["confluence_score"],
        confluence_factors=json.loads(row["confluence_factors"] or "[]"),
        order_id=row["order_id"],
        client_order_id=row["client_order_id"],
        htf_bias=row["htf_bias"],
        session=row["session"],
        market_structure=row["market_structure"],
        exit_price=row["exit_price"],
        pnl_usdt=row["pnl_usdt"],
        pnl_r=row["pnl_r"],
        fees_usdt=row["fees_usdt"] or 0.0,
        sl_moved_to_be=bool(_safe_col(row, "sl_moved_to_be") or 0),
        close_reason=_safe_col(row, "close_reason"),
        funding_z_at_entry=_safe_col(row, "funding_z_at_entry"),
        ls_ratio_at_entry=_safe_col(row, "ls_ratio_at_entry"),
        oi_change_24h_at_entry=_safe_col(row, "oi_change_24h_at_entry"),
        liq_imbalance_1h_at_entry=_safe_col(row, "liq_imbalance_1h_at_entry"),
        nearest_liq_cluster_above_price=_safe_col(row, "nearest_liq_cluster_above_price"),
        nearest_liq_cluster_below_price=_safe_col(row, "nearest_liq_cluster_below_price"),
        nearest_liq_cluster_above_notional=_safe_col(row, "nearest_liq_cluster_above_notional"),
        nearest_liq_cluster_below_notional=_safe_col(row, "nearest_liq_cluster_below_notional"),
        nearest_liq_cluster_above_distance_atr=_safe_col(row, "nearest_liq_cluster_above_distance_atr"),
        nearest_liq_cluster_below_distance_atr=_safe_col(row, "nearest_liq_cluster_below_distance_atr"),
        setup_zone_source=_safe_col(row, "setup_zone_source"),
        zone_wait_bars=_safe_col(row, "zone_wait_bars"),
        zone_fill_latency_bars=_safe_col(row, "zone_fill_latency_bars"),
        trend_regime_at_entry=_safe_col(row, "trend_regime_at_entry"),
        real_market_entry_valid=_safe_bool(row, "real_market_entry_valid"),
        real_market_exit_valid=_safe_bool(row, "real_market_exit_valid"),
        demo_artifact=_safe_bool(row, "demo_artifact"),
        artifact_reason=_safe_col(row, "artifact_reason"),
        on_chain_context=_parse_on_chain_context(row),
        confluence_pillar_scores=_parse_pillar_scores(row),
        oscillator_raw_values=_parse_oscillator_raw_values(row),
        open_interest_usd_at_entry=_safe_col(row, "open_interest_usd_at_entry"),
        oi_change_1h_pct_at_entry=_safe_col(row, "oi_change_1h_pct_at_entry"),
        funding_rate_current_at_entry=_safe_col(row, "funding_rate_current_at_entry"),
        funding_rate_predicted_at_entry=_safe_col(row, "funding_rate_predicted_at_entry"),
        long_liq_notional_1h_at_entry=_safe_col(row, "long_liq_notional_1h_at_entry"),
        short_liq_notional_1h_at_entry=_safe_col(row, "short_liq_notional_1h_at_entry"),
        ls_ratio_zscore_14d_at_entry=_safe_col(row, "ls_ratio_zscore_14d_at_entry"),
        liq_heatmap_top_clusters=_parse_liq_heatmap_clusters(row),
    )


def _safe_col(row: aiosqlite.Row, name: str):
    """Access a column that may not exist on a pre-migration row."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _safe_bool(row: aiosqlite.Row, name: str) -> Optional[bool]:
    """Tri-state bool: None when column missing or NULL, else cast 0/1."""
    v = _safe_col(row, name)
    if v is None:
        return None
    return bool(v)


def _classify(pnl_usdt: float) -> TradeOutcome:
    if pnl_usdt > 0:
        return TradeOutcome.WIN
    if pnl_usdt < 0:
        return TradeOutcome.LOSS
    return TradeOutcome.BREAKEVEN


# ── Journal ─────────────────────────────────────────────────────────────────


class TradeJournal:
    """Async SQLite store for trade lifecycle records.

    Open/close symmetry:
        journal = TradeJournal("data/trades.db")
        await journal.connect()
        ...
        await journal.close()

    or use as an async context manager.
    """

    def __init__(self, db_path: Union[str, Path]):
        self._db_path = str(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        # In-memory DBs skip the mkdir step.
        if self._db_path != ":memory:":
            parent = Path(self._db_path).parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        # Idempotent migrations for databases created before Madde E.
        for sql in _MIGRATIONS:
            try:
                await self._conn.execute(sql)
            except aiosqlite.OperationalError:
                pass
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "TradeJournal":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TradeJournal not connected; call .connect() first")
        return self._conn

    # ── Writes ──────────────────────────────────────────────────────────────

    async def record_open(
        self,
        plan: TradePlan,
        report: ExecutionReport,
        *,
        symbol: str,
        signal_timestamp: datetime,
        entry_timestamp: Optional[datetime] = None,
        # entry_timeframe / htf_timeframe / regime_at_entry kwargs dropped
        # 2026-04-27 — 1-distinct constants in the audit.
        htf_bias: Optional[str] = None,
        session: Optional[str] = None,
        market_structure: Optional[str] = None,
        funding_z_at_entry: Optional[float] = None,
        ls_ratio_at_entry: Optional[float] = None,
        oi_change_24h_at_entry: Optional[float] = None,
        liq_imbalance_1h_at_entry: Optional[float] = None,
        nearest_liq_cluster_above_price: Optional[float] = None,
        nearest_liq_cluster_below_price: Optional[float] = None,
        nearest_liq_cluster_above_notional: Optional[float] = None,
        nearest_liq_cluster_below_notional: Optional[float] = None,
        nearest_liq_cluster_above_distance_atr: Optional[float] = None,
        nearest_liq_cluster_below_distance_atr: Optional[float] = None,
        trend_regime_at_entry: Optional[str] = None,
        on_chain_context: Optional[dict] = None,
        confluence_pillar_scores: Optional[dict[str, float]] = None,
        oscillator_raw_values: Optional[dict[str, dict]] = None,
        open_interest_usd_at_entry: Optional[float] = None,
        oi_change_1h_pct_at_entry: Optional[float] = None,
        funding_rate_current_at_entry: Optional[float] = None,
        funding_rate_predicted_at_entry: Optional[float] = None,
        long_liq_notional_1h_at_entry: Optional[float] = None,
        short_liq_notional_1h_at_entry: Optional[float] = None,
        ls_ratio_zscore_14d_at_entry: Optional[float] = None,
        liq_heatmap_top_clusters: Optional[dict] = None,
        # price_change_1h/4h_pct_at_entry kwargs dropped 2026-04-27 —
        # by-design NULL on every Bybit-era trade (all pending-fill,
        # candles=None plumbed by design).
        # 2026-04-27 (F3) — zone metadata plumbing. Schema columns existed
        # since the zone-based pivot but the runner's pending-fill path
        # never forwarded them, leaving 9/9 NULL on the Bybit dataset.
        # `setup_zone_source` is one of the ZoneSource Literal values
        # ("vwap_retest" / "ema21_pullback" / "fvg_entry" / ...);
        # `zone_wait_bars` is the static `max_wait_bars` from ZoneSetup;
        # `zone_fill_latency_bars` is round((fill_ts - placed_ts).total_s
        # / 60 / entry_tf_minutes) — never exceeds zone_wait_bars by
        # construction (timeout cancels the limit at that boundary).
        setup_zone_source: Optional[str] = None,
        zone_wait_bars: Optional[int] = None,
        zone_fill_latency_bars: Optional[int] = None,
        # Back-compat tail: callers may still pass these via direct kwargs
        # or `**enrichment` unpacking. Accepted but silently ignored
        # (kwargs no longer forwarded into TradeRecord — columns dropped
        # 2026-04-27).
        entry_timeframe: Optional[str] = None,  # noqa: ARG002 (1-distinct config constant)
        htf_timeframe: Optional[str] = None,    # noqa: ARG002 (1-distinct config constant)
        regime_at_entry: Optional[str] = None,  # noqa: ARG002 (was 1-distinct constant 'BALANCED')
        price_change_1h_pct_at_entry: Optional[float] = None,  # noqa: ARG002
        price_change_4h_pct_at_entry: Optional[float] = None,  # noqa: ARG002
    ) -> TradeRecord:
        """Insert an OPEN row describing a freshly-placed trade.

        The returned `TradeRecord` carries the journal's own `trade_id`, which
        the caller MUST pass to `record_close` later.
        """
        conn = self._require_conn()
        entry_ts = entry_timestamp or report.entry.submitted_at
        rec = TradeRecord(
            trade_id=uuid.uuid4().hex,
            symbol=symbol,
            direction=plan.direction,
            outcome=TradeOutcome.OPEN,
            signal_timestamp=signal_timestamp,
            entry_timestamp=entry_ts,
            entry_price=plan.entry_price,
            sl_price=plan.sl_price,
            tp_price=plan.tp_price,
            rr_ratio=plan.rr_ratio,
            leverage=plan.leverage,
            num_contracts=plan.num_contracts,
            position_size_usdt=plan.position_size_usdt,
            risk_amount_usdt=plan.risk_amount_usdt,
            sl_source=plan.sl_source,
            reason=plan.reason,
            confluence_score=plan.confluence_score,
            confluence_factors=list(plan.confluence_factors),
            order_id=report.entry.order_id or None,
            client_order_id=report.entry.client_order_id or None,
            htf_bias=htf_bias,
            session=session,
            market_structure=market_structure,
            funding_z_at_entry=funding_z_at_entry,
            ls_ratio_at_entry=ls_ratio_at_entry,
            oi_change_24h_at_entry=oi_change_24h_at_entry,
            liq_imbalance_1h_at_entry=liq_imbalance_1h_at_entry,
            nearest_liq_cluster_above_price=nearest_liq_cluster_above_price,
            nearest_liq_cluster_below_price=nearest_liq_cluster_below_price,
            nearest_liq_cluster_above_notional=nearest_liq_cluster_above_notional,
            nearest_liq_cluster_below_notional=nearest_liq_cluster_below_notional,
            nearest_liq_cluster_above_distance_atr=nearest_liq_cluster_above_distance_atr,
            nearest_liq_cluster_below_distance_atr=nearest_liq_cluster_below_distance_atr,
            trend_regime_at_entry=trend_regime_at_entry,
            on_chain_context=on_chain_context,
            confluence_pillar_scores=dict(confluence_pillar_scores or {}),
            oscillator_raw_values=dict(oscillator_raw_values or {}),
            open_interest_usd_at_entry=open_interest_usd_at_entry,
            oi_change_1h_pct_at_entry=oi_change_1h_pct_at_entry,
            funding_rate_current_at_entry=funding_rate_current_at_entry,
            funding_rate_predicted_at_entry=funding_rate_predicted_at_entry,
            long_liq_notional_1h_at_entry=long_liq_notional_1h_at_entry,
            short_liq_notional_1h_at_entry=short_liq_notional_1h_at_entry,
            ls_ratio_zscore_14d_at_entry=ls_ratio_zscore_14d_at_entry,
            liq_heatmap_top_clusters=dict(liq_heatmap_top_clusters or {}),
            setup_zone_source=setup_zone_source,
            zone_wait_bars=zone_wait_bars,
            zone_fill_latency_bars=zone_fill_latency_bars,
        )
        placeholders = ", ".join("?" * len(_COLUMNS))
        cols = ", ".join(_COLUMNS)
        await conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            _record_to_row(rec),
        )
        await conn.commit()
        return rec

    async def record_close(
        self,
        trade_id: str,
        close_fill: CloseFill,
        fees_usdt: float = 0.0,
        *,
        close_reason: Optional[str] = None,
    ) -> TradeRecord:
        """Stamp exit fields on an existing OPEN row and return the updated record.

        Computes `pnl_r = pnl_usdt / risk_amount_usdt` from the open row.
        `close_reason` (e.g. "EARLY_CLOSE_LTF_REVERSAL") is persisted for
        post-hoc analysis. Raises `KeyError` if `trade_id` isn't in the journal.
        """
        existing = await self.get_trade(trade_id)
        if existing is None:
            raise KeyError(f"No trade with id={trade_id!r}")

        conn = self._require_conn()
        pnl_usdt = close_fill.pnl_usdt
        outcome = _classify(pnl_usdt)
        pnl_r = (
            pnl_usdt / existing.risk_amount_usdt
            if existing.risk_amount_usdt > 0 else 0.0
        )
        await conn.execute(
            """UPDATE trades SET
                   outcome = ?, exit_timestamp = ?, exit_price = ?,
                   pnl_usdt = ?, pnl_r = ?, fees_usdt = ?,
                   close_reason = COALESCE(?, close_reason)
               WHERE trade_id = ?""",
            (
                outcome.value, _iso(close_fill.closed_at), close_fill.exit_price,
                pnl_usdt, pnl_r, fees_usdt, close_reason, trade_id,
            ),
        )
        await conn.commit()
        updated = await self.get_trade(trade_id)
        assert updated is not None
        return updated

    async def update_artifact_flags(
        self,
        trade_id: str,
        *,
        real_market_entry_valid: Optional[bool],
        real_market_exit_valid: Optional[bool],
        demo_artifact: Optional[bool],
        artifact_reason: Optional[str],
    ) -> None:
        """Stamp demo-wick artefact flags on a closed trade. Non-destructive —
        the trade stays in the journal; downstream reporting / RL filter on
        `demo_artifact=1` to exclude artefact fills. Raises KeyError on
        unknown trade_id so the caller notices stale state."""
        conn = self._require_conn()
        cur = await conn.execute(
            """UPDATE trades SET
                   real_market_entry_valid = ?,
                   real_market_exit_valid  = ?,
                   demo_artifact           = ?,
                   artifact_reason         = ?
               WHERE trade_id = ?""",
            (
                None if real_market_entry_valid is None
                else int(real_market_entry_valid),
                None if real_market_exit_valid is None
                else int(real_market_exit_valid),
                None if demo_artifact is None else int(demo_artifact),
                artifact_reason,
                trade_id,
            ),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"No trade with id={trade_id!r}")

    async def update_algo_ids(self, trade_id: str, algo_ids: list[str]) -> None:
        """Rewrite the `algo_ids` column AND stamp `sl_moved_to_be = 1`.

        Used by the SL-to-BE path when the monitor replaces TP2 with a new
        OCO (SL at entry + remainder TP). Persisting the flag is what lets
        `_rehydrate_open_positions` skip the re-move after a restart — see
        `PositionMonitor._detect_tp1_and_move_sl` for the consumer side.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            "UPDATE trades SET algo_ids = ?, sl_moved_to_be = 1 WHERE trade_id = ?",
            (json.dumps(list(algo_ids)), trade_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"No trade with id={trade_id!r}")

    async def record_rejected_signal(
        self,
        *,
        symbol: str,
        direction: Direction,
        reject_reason: str,
        signal_timestamp: datetime,
        price: Optional[float] = None,
        atr: Optional[float] = None,
        confluence_score: float = 0.0,
        confluence_factors: Optional[list[str]] = None,
        # entry_timeframe / htf_timeframe / regime_at_entry kwargs accepted
        # for back-compat with `**enrichment` unpacking but no longer
        # forwarded into RejectedSignal (columns dropped 2026-04-27).
        entry_timeframe: Optional[str] = None,  # noqa: ARG002
        htf_timeframe: Optional[str] = None,  # noqa: ARG002
        regime_at_entry: Optional[str] = None,  # noqa: ARG002
        # 2026-04-29 — Pass 2.5 reject pegger re-add. proposed_* set here
        # at insert time by `_record_reject` (caller computes ATR-based
        # what-if for pre-fill rejects, plan_sl/tp forward for pending-
        # cancel rejects). hypothetical_* NOT taken here — pegger
        # (`scripts/peg_rejected_outcomes.py`) issues UPDATE statements
        # against rejection_id after Bybit kline forward-walk.
        proposed_sl_price: Optional[float] = None,
        proposed_tp_price: Optional[float] = None,
        proposed_rr_ratio: Optional[float] = None,
        htf_bias: Optional[str] = None,
        session: Optional[str] = None,
        market_structure: Optional[str] = None,
        funding_z_at_entry: Optional[float] = None,
        ls_ratio_at_entry: Optional[float] = None,
        oi_change_24h_at_entry: Optional[float] = None,
        liq_imbalance_1h_at_entry: Optional[float] = None,
        nearest_liq_cluster_above_price: Optional[float] = None,
        nearest_liq_cluster_below_price: Optional[float] = None,
        nearest_liq_cluster_above_notional: Optional[float] = None,
        nearest_liq_cluster_below_notional: Optional[float] = None,
        nearest_liq_cluster_above_distance_atr: Optional[float] = None,
        nearest_liq_cluster_below_distance_atr: Optional[float] = None,
        pillar_btc_bias: Optional[str] = None,
        pillar_eth_bias: Optional[str] = None,
        on_chain_context: Optional[dict] = None,
        confluence_pillar_scores: Optional[dict[str, float]] = None,
        oscillator_raw_values: Optional[dict[str, dict]] = None,
        open_interest_usd_at_entry: Optional[float] = None,
        oi_change_1h_pct_at_entry: Optional[float] = None,
        funding_rate_current_at_entry: Optional[float] = None,
        funding_rate_predicted_at_entry: Optional[float] = None,
        long_liq_notional_1h_at_entry: Optional[float] = None,
        short_liq_notional_1h_at_entry: Optional[float] = None,
        ls_ratio_zscore_14d_at_entry: Optional[float] = None,
        price_change_1h_pct_at_entry: Optional[float] = None,
        price_change_4h_pct_at_entry: Optional[float] = None,
        liq_heatmap_top_clusters: Optional[dict] = None,
    ) -> RejectedSignal:
        """Insert a single row into `rejected_signals`.

        Only called by the runner on `plan is None` return. Never raises on
        duplicate — we generate a fresh uuid per call, the table is
        append-only. Counter-factual `hypothetical_*` outcome fields stay
        NULL on insert; `scripts/peg_rejected_outcomes.py` runs Bybit
        kline forward-walk and stamps them via UPDATE statements (Pass 2.5
        re-add of the 2026-04-27-dropped peg path; legacy OKX-era pegger
        script was deleted in the post-migration cleanup).
        """
        conn = self._require_conn()
        rec = RejectedSignal(
            rejection_id=uuid.uuid4().hex,
            symbol=symbol,
            direction=direction,
            reject_reason=reject_reason,
            signal_timestamp=signal_timestamp,
            price=price,
            atr=atr,
            confluence_score=confluence_score,
            confluence_factors=list(confluence_factors or []),
            proposed_sl_price=proposed_sl_price,
            proposed_tp_price=proposed_tp_price,
            proposed_rr_ratio=proposed_rr_ratio,
            htf_bias=htf_bias,
            session=session,
            market_structure=market_structure,
            funding_z_at_entry=funding_z_at_entry,
            ls_ratio_at_entry=ls_ratio_at_entry,
            oi_change_24h_at_entry=oi_change_24h_at_entry,
            liq_imbalance_1h_at_entry=liq_imbalance_1h_at_entry,
            nearest_liq_cluster_above_price=nearest_liq_cluster_above_price,
            nearest_liq_cluster_below_price=nearest_liq_cluster_below_price,
            nearest_liq_cluster_above_notional=nearest_liq_cluster_above_notional,
            nearest_liq_cluster_below_notional=nearest_liq_cluster_below_notional,
            nearest_liq_cluster_above_distance_atr=nearest_liq_cluster_above_distance_atr,
            nearest_liq_cluster_below_distance_atr=nearest_liq_cluster_below_distance_atr,
            pillar_btc_bias=pillar_btc_bias,
            pillar_eth_bias=pillar_eth_bias,
            on_chain_context=on_chain_context,
            confluence_pillar_scores=dict(confluence_pillar_scores or {}),
            oscillator_raw_values=dict(oscillator_raw_values or {}),
            open_interest_usd_at_entry=open_interest_usd_at_entry,
            oi_change_1h_pct_at_entry=oi_change_1h_pct_at_entry,
            funding_rate_current_at_entry=funding_rate_current_at_entry,
            funding_rate_predicted_at_entry=funding_rate_predicted_at_entry,
            long_liq_notional_1h_at_entry=long_liq_notional_1h_at_entry,
            short_liq_notional_1h_at_entry=short_liq_notional_1h_at_entry,
            ls_ratio_zscore_14d_at_entry=ls_ratio_zscore_14d_at_entry,
            price_change_1h_pct_at_entry=price_change_1h_pct_at_entry,
            price_change_4h_pct_at_entry=price_change_4h_pct_at_entry,
            liq_heatmap_top_clusters=dict(liq_heatmap_top_clusters or {}),
        )
        placeholders = ", ".join("?" * len(_REJECTED_COLUMNS))
        cols = ", ".join(_REJECTED_COLUMNS)
        await conn.execute(
            f"INSERT INTO rejected_signals ({cols}) VALUES ({placeholders})",
            _rejected_to_row(rec),
        )
        await conn.commit()
        return rec

    async def update_rejected_outcome(
        self,
        rejection_id: str,
        *,
        outcome: str,
        bars_to_tp: Optional[int] = None,
        bars_to_sl: Optional[int] = None,
    ) -> None:
        """Stamp counter-factual outcome on a `rejected_signals` row.

        Called by `scripts/peg_rejected_outcomes.py` after Bybit kline
        forward-walk resolves the row. `outcome` is one of `WIN`,
        `LOSS`, `TIMEOUT`. `bars_to_tp` / `bars_to_sl` are bar offsets
        from `signal_timestamp + 1 bar`; only the matching side is set
        (the other stays NULL — peg's "didn't happen" signal).

        Idempotent re-runs overwrite — `UPDATE` is unconditional. To
        skip already-pegged rows, the pegger filters
        `WHERE hypothetical_outcome IS NULL` at fetch time.
        """
        conn = self._require_conn()
        await conn.execute(
            """
            UPDATE rejected_signals
               SET hypothetical_outcome      = ?,
                   hypothetical_bars_to_tp   = ?,
                   hypothetical_bars_to_sl   = ?
             WHERE rejection_id = ?
            """,
            (outcome, bars_to_tp, bars_to_sl, rejection_id),
        )
        await conn.commit()

    async def list_rejected_signals(
        self,
        *,
        since: Optional[datetime] = None,
        symbol: Optional[str] = None,
        reject_reason: Optional[str] = None,
    ) -> list[RejectedSignal]:
        """Read rejects in signal-timestamp order.

        Filters stack (AND): `since` excludes older rows, `symbol` narrows
        to one pair, `reject_reason` narrows to a single reason bucket.
        Returns [] if nothing matches. No pagination — call it with a tight
        `since` for large journals.
        """
        conn = self._require_conn()
        sql = "SELECT * FROM rejected_signals WHERE 1=1"
        params: list = []
        if since is not None:
            sql += " AND signal_timestamp >= ?"
            params.append(_iso(since))
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        if reject_reason is not None:
            sql += " AND reject_reason = ?"
            params.append(reject_reason)
        sql += " ORDER BY signal_timestamp ASC"
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_rejected(r) for r in rows]

    async def mark_canceled(self, trade_id: str, reason: str = "") -> None:
        """Flip an OPEN row to CANCELED — used when the entry never filled or the
        operator aborted before SL/TP could evaluate.

        2026-04-27: cancel reason now lands in `close_reason` instead of
        `notes` (the latter was dropped as a manual-fill-only column the
        bot never touched outside this one path)."""
        conn = self._require_conn()
        cur = await conn.execute(
            "UPDATE trades SET outcome = ?, close_reason = ? WHERE trade_id = ?",
            (TradeOutcome.CANCELED.value, reason or None, trade_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"No trade with id={trade_id!r}")

    async def record_on_chain_snapshot(
        self,
        *,
        captured_at: datetime,
        daily_macro_bias: Optional[str],
        stablecoin_pulse_1h_usd: Optional[float],
        cex_btc_netflow_24h_usd: Optional[float],
        cex_eth_netflow_24h_usd: Optional[float],
        altcoin_index: Optional[float],
        # 2026-04-27 — back-compat tail. Accepted but no longer forwarded:
        # coinbase_asia_skew_usd / bnb_self_flow_24h_usd (schema
        # placeholders), snapshot_age_s / fresh / whale_blackout_active
        # (1-distinct constants).
        coinbase_asia_skew_usd: Optional[float] = None,  # noqa: ARG002
        bnb_self_flow_24h_usd: Optional[float] = None,   # noqa: ARG002
        snapshot_age_s: Optional[int] = None,            # noqa: ARG002
        fresh: bool = True,                              # noqa: ARG002
        whale_blackout_active: bool = False,             # noqa: ARG002
        cex_coinbase_netflow_24h_usd: Optional[float] = None,
        cex_binance_netflow_24h_usd: Optional[float] = None,
        cex_bybit_netflow_24h_usd: Optional[float] = None,
        token_volume_1h_net_usd_json: Optional[str] = None,
        cex_bitfinex_netflow_24h_usd: Optional[float] = None,
        cex_kraken_netflow_24h_usd: Optional[float] = None,
        cex_okx_netflow_24h_usd: Optional[float] = None,
        cex_per_venue_btc_netflow_24h_usd_json: Optional[str] = None,
        cex_per_venue_eth_netflow_24h_usd_json: Optional[str] = None,
        cex_per_venue_stables_netflow_24h_usd_json: Optional[str] = None,
    ) -> int:
        """Append one row to `on_chain_snapshots` — time-series of Arkham state.

        Intended cadence: ONLY when the upstream snapshot fingerprint actually
        changes. Runner's `_maybe_record_on_chain_snapshot` owns dedup; this
        method is a dumb writer and will insert whatever it's given. Returns
        the new row's `id` for callers that want to reference it.

        2026-04-22 — added Coinbase/Binance/Bybit entity netflow + per-symbol
        token volume JSON. New params have default None for backwards compat
        with any test fixtures that still call the original signature.
        2026-04-23 (night-late) — added Bitfinex + Kraken (biggest named inflow
        / outflow in live probe vs. `type:cex` aggregate). Journal-only;
        _flow_alignment_score still reads the original 6 inputs.
        2026-04-24 — added OKX as 6th venue (bot's own exchange). Self-signal;
        24h net ≈ 0 by design (balanced turnover) but captured for parity.
        2026-04-26 — added 3 JSON dict TEXT columns for per-venue × per-asset
        breakdown (BTC / ETH / stables). Each is a dict keyed by entity slug;
        adding a 7th venue won't trigger a migration. Powers the dashboard's
        per-venue per-asset chart; not yet wired into runtime scoring.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """INSERT INTO on_chain_snapshots (
                   captured_at,
                   daily_macro_bias,
                   stablecoin_pulse_1h_usd,
                   cex_btc_netflow_24h_usd,
                   cex_eth_netflow_24h_usd,
                   altcoin_index,
                   cex_coinbase_netflow_24h_usd,
                   cex_binance_netflow_24h_usd,
                   cex_bybit_netflow_24h_usd,
                   token_volume_1h_net_usd_json,
                   cex_bitfinex_netflow_24h_usd,
                   cex_kraken_netflow_24h_usd,
                   cex_okx_netflow_24h_usd,
                   cex_per_venue_btc_netflow_24h_usd_json,
                   cex_per_venue_eth_netflow_24h_usd_json,
                   cex_per_venue_stables_netflow_24h_usd_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _iso(captured_at),
                daily_macro_bias,
                stablecoin_pulse_1h_usd,
                cex_btc_netflow_24h_usd,
                cex_eth_netflow_24h_usd,
                altcoin_index,
                cex_coinbase_netflow_24h_usd,
                cex_binance_netflow_24h_usd,
                cex_bybit_netflow_24h_usd,
                token_volume_1h_net_usd_json,
                cex_bitfinex_netflow_24h_usd,
                cex_kraken_netflow_24h_usd,
                cex_okx_netflow_24h_usd,
                cex_per_venue_btc_netflow_24h_usd_json,
                cex_per_venue_eth_netflow_24h_usd_json,
                cex_per_venue_stables_netflow_24h_usd_json,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)

    async def list_on_chain_snapshots(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[dict]:
        """Read on-chain snapshots in capture order, optionally bounded by a
        `[since, until]` window. Returns plain dicts — this table has no
        model class since it's consumed by Phase 9 analysis scripts, not
        by the runtime strategy.
        """
        conn = self._require_conn()
        sql = "SELECT * FROM on_chain_snapshots WHERE 1=1"
        params: list = []
        if since is not None:
            sql += " AND captured_at >= ?"
            params.append(_iso(since))
        if until is not None:
            sql += " AND captured_at <= ?"
            params.append(_iso(until))
        sql += " ORDER BY captured_at ASC, id ASC"
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def record_whale_transfer(
        self,
        *,
        captured_at: datetime,
        token: str,
        usd_value: float,
        from_entity: Optional[str] = None,
        to_entity: Optional[str] = None,
        tx_hash: Optional[str] = None,
        affected_symbols: Optional[list[str]] = None,
    ) -> int:
        """Append one row to `whale_transfers`.

        Called from the Arkham WS listener on every qualifying event
        (post-2026-04-22: no longer gates runtime; raw event captured so
        Phase 9 GBT can learn directional classification from outcomes).
        Returns the new row id; caller ignores it in the normal fire-and-
        forget path.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """INSERT INTO whale_transfers (
                   captured_at, token, usd_value,
                   from_entity, to_entity, tx_hash,
                   affected_symbols
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _iso(captured_at),
                token,
                float(usd_value),
                from_entity,
                to_entity,
                tx_hash,
                json.dumps(list(affected_symbols or [])),
            ),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)

    async def list_whale_transfers(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        token: Optional[str] = None,
    ) -> list[WhaleTransferRecord]:
        """Read whale transfers in capture-time order. Filters stack (AND).

        Parses `affected_symbols` JSON on the way out — empty list if
        missing / malformed so downstream analysis never crashes on a
        bad row.
        """
        conn = self._require_conn()
        sql = "SELECT * FROM whale_transfers WHERE 1=1"
        params: list = []
        if since is not None:
            sql += " AND captured_at >= ?"
            params.append(_iso(since))
        if until is not None:
            sql += " AND captured_at <= ?"
            params.append(_iso(until))
        if token is not None:
            sql += " AND token = ?"
            params.append(token)
        sql += " ORDER BY captured_at ASC, id ASC"
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        out: list[WhaleTransferRecord] = []
        for r in rows:
            raw_symbols = r["affected_symbols"] if "affected_symbols" in r.keys() else "[]"
            try:
                parsed_symbols = json.loads(raw_symbols or "[]")
                if not isinstance(parsed_symbols, list):
                    parsed_symbols = []
            except (TypeError, ValueError):
                parsed_symbols = []
            out.append(WhaleTransferRecord(
                captured_at=_parse_iso(r["captured_at"]),
                token=r["token"],
                usd_value=r["usd_value"],
                from_entity=r["from_entity"],
                to_entity=r["to_entity"],
                tx_hash=r["tx_hash"],
                affected_symbols=[str(s) for s in parsed_symbols],
            ))
        return out

    async def record_position_snapshot(
        self,
        *,
        trade_id: str,
        captured_at: datetime,
        mark_price: float,
        unrealized_pnl_usdt: float,
        unrealized_pnl_r: float,
        mfe_r_so_far: float,
        mae_r_so_far: float,
        current_sl_price: float,
        current_tp_price: Optional[float] = None,
        sl_to_be_moved: bool = False,
        mfe_lock_applied: bool = False,
        derivatives_funding_now: Optional[float] = None,
        derivatives_oi_now_usd: Optional[float] = None,
        derivatives_ls_ratio_now: Optional[float] = None,
        derivatives_long_liq_1h_now: Optional[float] = None,
        derivatives_short_liq_1h_now: Optional[float] = None,
        on_chain_btc_netflow_now_usd: Optional[float] = None,
        on_chain_stablecoin_pulse_now: Optional[float] = None,
        on_chain_flow_alignment_now: Optional[float] = None,
        oscillator_3m_now_json: Optional[dict] = None,
        vwap_3m_distance_atr_now: Optional[float] = None,
    ) -> int:
        """Append one row to `position_snapshots` — intra-trade time-series.

        Cadence-gated by the runner (`_maybe_write_position_snapshots`); this
        method is a dumb writer. Returns the new row id; the runner's batch
        loop ignores it.

        Drift fields default to None so unit tests can write the bare minimum.
        Production caller passes everything it has from
        `BotContext.{derivatives_cache, on_chain_snapshot,
        last_market_state_per_symbol}` and lets None propagate when the
        relevant cache is cold.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """INSERT INTO position_snapshots (
                   trade_id, captured_at,
                   mark_price, unrealized_pnl_usdt, unrealized_pnl_r,
                   mfe_r_so_far, mae_r_so_far,
                   current_sl_price, current_tp_price,
                   sl_to_be_moved, mfe_lock_applied,
                   derivatives_funding_now, derivatives_oi_now_usd,
                   derivatives_ls_ratio_now,
                   derivatives_long_liq_1h_now, derivatives_short_liq_1h_now,
                   on_chain_btc_netflow_now_usd, on_chain_stablecoin_pulse_now,
                   on_chain_flow_alignment_now,
                   oscillator_3m_now_json, vwap_3m_distance_atr_now
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade_id,
                _iso(captured_at),
                float(mark_price),
                float(unrealized_pnl_usdt),
                float(unrealized_pnl_r),
                float(mfe_r_so_far),
                float(mae_r_so_far),
                float(current_sl_price),
                (None if current_tp_price is None else float(current_tp_price)),
                int(bool(sl_to_be_moved)),
                int(bool(mfe_lock_applied)),
                derivatives_funding_now,
                derivatives_oi_now_usd,
                derivatives_ls_ratio_now,
                derivatives_long_liq_1h_now,
                derivatives_short_liq_1h_now,
                on_chain_btc_netflow_now_usd,
                on_chain_stablecoin_pulse_now,
                on_chain_flow_alignment_now,
                (None if oscillator_3m_now_json is None
                 else json.dumps(oscillator_3m_now_json)),
                vwap_3m_distance_atr_now,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)

    async def get_position_snapshots(
        self, trade_id: str,
    ) -> list[PositionSnapshotRecord]:
        """Read all snapshots for one trade ordered by capture time.

        JSON `oscillator_3m_now_json` parses to dict (empty on missing /
        malformed). Optional drift columns surface as None when NULL.
        """
        conn = self._require_conn()
        async with conn.execute(
            "SELECT * FROM position_snapshots "
            "WHERE trade_id = ? ORDER BY captured_at ASC, id ASC",
            (trade_id,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[PositionSnapshotRecord] = []
        for r in rows:
            raw_osc = r["oscillator_3m_now_json"]
            try:
                parsed_osc = json.loads(raw_osc) if raw_osc else {}
                if not isinstance(parsed_osc, dict):
                    parsed_osc = {}
            except (TypeError, ValueError):
                parsed_osc = {}
            out.append(PositionSnapshotRecord(
                trade_id=r["trade_id"],
                captured_at=_parse_iso(r["captured_at"]),
                mark_price=r["mark_price"],
                unrealized_pnl_usdt=r["unrealized_pnl_usdt"],
                unrealized_pnl_r=r["unrealized_pnl_r"],
                mfe_r_so_far=r["mfe_r_so_far"],
                mae_r_so_far=r["mae_r_so_far"],
                current_sl_price=r["current_sl_price"],
                current_tp_price=r["current_tp_price"],
                sl_to_be_moved=bool(r["sl_to_be_moved"]),
                mfe_lock_applied=bool(r["mfe_lock_applied"]),
                derivatives_funding_now=r["derivatives_funding_now"],
                derivatives_oi_now_usd=r["derivatives_oi_now_usd"],
                derivatives_ls_ratio_now=r["derivatives_ls_ratio_now"],
                derivatives_long_liq_1h_now=r["derivatives_long_liq_1h_now"],
                derivatives_short_liq_1h_now=r["derivatives_short_liq_1h_now"],
                on_chain_btc_netflow_now_usd=r["on_chain_btc_netflow_now_usd"],
                on_chain_stablecoin_pulse_now=r["on_chain_stablecoin_pulse_now"],
                on_chain_flow_alignment_now=r["on_chain_flow_alignment_now"],
                oscillator_3m_now_json=parsed_osc,
                vwap_3m_distance_atr_now=r["vwap_3m_distance_atr_now"],
            ))
        return out

    # ── Reads ───────────────────────────────────────────────────────────────

    async def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list_open_trades(self) -> list[TradeRecord]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT * FROM trades WHERE outcome = ? ORDER BY entry_timestamp ASC",
            (TradeOutcome.OPEN.value,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def list_closed_trades(
        self,
        since: Optional[datetime] = None,
    ) -> list[TradeRecord]:
        """Return all non-OPEN, non-CANCELED trades in entry-timestamp order.

        CANCELED trades are excluded — they have no PnL and would skew reports.
        """
        conn = self._require_conn()
        closed_outcomes = (
            TradeOutcome.WIN.value, TradeOutcome.LOSS.value, TradeOutcome.BREAKEVEN.value,
        )
        placeholders = ",".join("?" * len(closed_outcomes))
        params: list = list(closed_outcomes)
        sql = (
            f"SELECT * FROM trades WHERE outcome IN ({placeholders})"
        )
        if since is not None:
            sql += " AND exit_timestamp >= ?"
            params.append(_iso(since))
        sql += " ORDER BY entry_timestamp ASC"
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    # ── Replay ──────────────────────────────────────────────────────────────

    async def replay_for_risk_manager(
        self,
        mgr: RiskManager,
        since: Optional[datetime] = None,
    ) -> None:
        """Walk closed trades in order and replay them into `mgr` so its
        peak/DD/streak counters match reality before the loop resumes.

        `since` (typically `rl.clean_since`) filters out pre-cutoff rows so a
        dirty-regime loss streak can't poison the fresh-start peak/DD math.
        Old rows stay in the DB for comparison but never touch the manager.

        We call `register_trade_opened` + `register_trade_closed` for each
        closed row — the open→close pairing matters because the manager tracks
        `open_positions` which must end at zero once we've replayed everything.
        """
        closed = await self.list_closed_trades(since=since)
        for rec in closed:
            if rec.pnl_usdt is None or rec.exit_timestamp is None:
                continue
            mgr.register_trade_opened()
            mgr.register_trade_closed(
                TradeResult(
                    pnl_usdt=rec.pnl_usdt,
                    pnl_r=rec.pnl_r or 0.0,
                    timestamp=rec.exit_timestamp,
                ),
                now=rec.exit_timestamp,
            )
