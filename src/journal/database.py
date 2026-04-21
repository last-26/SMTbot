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
from src.journal.models import RejectedSignal, TradeOutcome, TradeRecord
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
    algo_id             TEXT,
    client_order_id     TEXT,
    client_algo_id      TEXT,

    entry_timeframe     TEXT,
    htf_timeframe       TEXT,
    htf_bias            TEXT,
    session             TEXT,
    market_structure    TEXT,

    exit_price          REAL,
    pnl_usdt            REAL,
    pnl_r               REAL,
    fees_usdt           REAL NOT NULL DEFAULT 0,

    algo_ids            TEXT NOT NULL DEFAULT '[]',
    sl_moved_to_be      INTEGER NOT NULL DEFAULT 0,
    close_reason        TEXT,

    regime_at_entry                     TEXT,
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
    funding_z_6h            REAL,
    funding_z_24h           REAL,

    notes               TEXT,
    screenshot_entry    TEXT,
    screenshot_exit     TEXT,

    real_market_entry_valid INTEGER,
    real_market_exit_valid  INTEGER,
    demo_artifact           INTEGER,
    artifact_reason         TEXT,

    on_chain_context        TEXT
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

    entry_timeframe     TEXT,
    htf_timeframe       TEXT,
    htf_bias            TEXT,
    session             TEXT,
    market_structure    TEXT,

    proposed_sl_price   REAL,
    proposed_tp_price   REAL,
    proposed_rr_ratio   REAL,

    regime_at_entry                     TEXT,
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

    hypothetical_outcome    TEXT,
    hypothetical_bars_to_tp INTEGER,
    hypothetical_bars_to_sl INTEGER,

    on_chain_context        TEXT
);

CREATE INDEX IF NOT EXISTS idx_rejected_symbol_ts  ON rejected_signals(symbol, signal_timestamp);
CREATE INDEX IF NOT EXISTS idx_rejected_reason     ON rejected_signals(reject_reason);
CREATE INDEX IF NOT EXISTS idx_rejected_outcome    ON rejected_signals(hypothetical_outcome);

-- 2026-04-21 — Arkham on-chain snapshot time-series (Phase 8 data layer).
-- One row per detected snapshot MUTATION (not per tick). Runner writes
-- through `record_on_chain_snapshot` only when the fingerprint changes,
-- so cadence matches Arkham's own refresh rhythm (~hourly pulse, hourly
-- altcoin index, daily bias). Phase 9 joins this onto `trades` via
-- `entry_timestamp <= captured_at <= exit_timestamp` to reconstruct
-- what on-chain regime the trade lived through.
CREATE TABLE IF NOT EXISTS on_chain_snapshots (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at                TEXT NOT NULL,
    daily_macro_bias           TEXT,
    stablecoin_pulse_1h_usd    REAL,
    cex_btc_netflow_24h_usd    REAL,
    cex_eth_netflow_24h_usd    REAL,
    coinbase_asia_skew_usd     REAL,
    bnb_self_flow_24h_usd      REAL,
    altcoin_index              REAL,
    snapshot_age_s             INTEGER,
    fresh                      INTEGER,
    whale_blackout_active      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_on_chain_snap_captured_at ON on_chain_snapshots(captured_at);
"""


# Column order for INSERT — kept in sync with _SCHEMA above so that
# _row_to_record and _record_to_row can round-trip without string matching.
_COLUMNS = [
    "trade_id", "symbol", "direction", "outcome",
    "signal_timestamp", "entry_timestamp", "exit_timestamp",
    "entry_price", "sl_price", "tp_price", "rr_ratio",
    "leverage", "num_contracts", "position_size_usdt", "risk_amount_usdt",
    "sl_source", "reason", "confluence_score", "confluence_factors",
    "order_id", "algo_id", "client_order_id", "client_algo_id",
    "entry_timeframe", "htf_timeframe", "htf_bias", "session", "market_structure",
    "exit_price", "pnl_usdt", "pnl_r", "fees_usdt",
    "algo_ids", "sl_moved_to_be", "close_reason",
    "regime_at_entry", "funding_z_at_entry", "ls_ratio_at_entry",
    "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry",
    "nearest_liq_cluster_above_price", "nearest_liq_cluster_below_price",
    "nearest_liq_cluster_above_notional", "nearest_liq_cluster_below_notional",
    "nearest_liq_cluster_above_distance_atr", "nearest_liq_cluster_below_distance_atr",
    "setup_zone_source", "zone_wait_bars", "zone_fill_latency_bars",
    "trend_regime_at_entry", "funding_z_6h", "funding_z_24h",
    "notes", "screenshot_entry", "screenshot_exit",
    "real_market_entry_valid", "real_market_exit_valid",
    "demo_artifact", "artifact_reason",
    "on_chain_context",
]


_REJECTED_COLUMNS = [
    "rejection_id", "symbol", "direction", "reject_reason", "signal_timestamp",
    "price", "atr", "confluence_score", "confluence_factors",
    "entry_timeframe", "htf_timeframe", "htf_bias", "session", "market_structure",
    "proposed_sl_price", "proposed_tp_price", "proposed_rr_ratio",
    "regime_at_entry", "funding_z_at_entry", "ls_ratio_at_entry",
    "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry",
    "nearest_liq_cluster_above_price", "nearest_liq_cluster_below_price",
    "nearest_liq_cluster_above_notional", "nearest_liq_cluster_below_notional",
    "nearest_liq_cluster_above_distance_atr", "nearest_liq_cluster_below_distance_atr",
    "pillar_btc_bias", "pillar_eth_bias",
    "hypothetical_outcome", "hypothetical_bars_to_tp", "hypothetical_bars_to_sl",
    "on_chain_context",
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
        rec.order_id, rec.algo_id, rec.client_order_id, rec.client_algo_id,
        rec.entry_timeframe, rec.htf_timeframe, rec.htf_bias, rec.session, rec.market_structure,
        rec.exit_price, rec.pnl_usdt, rec.pnl_r, rec.fees_usdt,
        json.dumps(rec.algo_ids), int(rec.sl_moved_to_be), rec.close_reason,
        rec.regime_at_entry, rec.funding_z_at_entry, rec.ls_ratio_at_entry,
        rec.oi_change_24h_at_entry, rec.liq_imbalance_1h_at_entry,
        rec.nearest_liq_cluster_above_price, rec.nearest_liq_cluster_below_price,
        rec.nearest_liq_cluster_above_notional, rec.nearest_liq_cluster_below_notional,
        rec.nearest_liq_cluster_above_distance_atr, rec.nearest_liq_cluster_below_distance_atr,
        rec.setup_zone_source, rec.zone_wait_bars, rec.zone_fill_latency_bars,
        rec.trend_regime_at_entry, rec.funding_z_6h, rec.funding_z_24h,
        rec.notes, rec.screenshot_entry, rec.screenshot_exit,
        (None if rec.real_market_entry_valid is None
         else int(rec.real_market_entry_valid)),
        (None if rec.real_market_exit_valid is None
         else int(rec.real_market_exit_valid)),
        (None if rec.demo_artifact is None else int(rec.demo_artifact)),
        rec.artifact_reason,
        (json.dumps(rec.on_chain_context)
         if rec.on_chain_context is not None else None),
    )


def _rejected_to_row(rec: RejectedSignal) -> tuple:
    return (
        rec.rejection_id, rec.symbol, rec.direction.value, rec.reject_reason,
        _iso(rec.signal_timestamp),
        rec.price, rec.atr, rec.confluence_score,
        json.dumps(rec.confluence_factors),
        rec.entry_timeframe, rec.htf_timeframe, rec.htf_bias,
        rec.session, rec.market_structure,
        rec.proposed_sl_price, rec.proposed_tp_price, rec.proposed_rr_ratio,
        rec.regime_at_entry, rec.funding_z_at_entry, rec.ls_ratio_at_entry,
        rec.oi_change_24h_at_entry, rec.liq_imbalance_1h_at_entry,
        rec.nearest_liq_cluster_above_price, rec.nearest_liq_cluster_below_price,
        rec.nearest_liq_cluster_above_notional, rec.nearest_liq_cluster_below_notional,
        rec.nearest_liq_cluster_above_distance_atr, rec.nearest_liq_cluster_below_distance_atr,
        rec.pillar_btc_bias, rec.pillar_eth_bias,
        rec.hypothetical_outcome, rec.hypothetical_bars_to_tp, rec.hypothetical_bars_to_sl,
        (json.dumps(rec.on_chain_context)
         if rec.on_chain_context is not None else None),
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
        entry_timeframe=row["entry_timeframe"],
        htf_timeframe=row["htf_timeframe"],
        htf_bias=row["htf_bias"],
        session=row["session"],
        market_structure=row["market_structure"],
        proposed_sl_price=row["proposed_sl_price"],
        proposed_tp_price=row["proposed_tp_price"],
        proposed_rr_ratio=row["proposed_rr_ratio"],
        regime_at_entry=row["regime_at_entry"],
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
        hypothetical_outcome=row["hypothetical_outcome"],
        hypothetical_bars_to_tp=row["hypothetical_bars_to_tp"],
        hypothetical_bars_to_sl=row["hypothetical_bars_to_sl"],
        on_chain_context=_parse_on_chain_context(row),
    )


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
        algo_id=row["algo_id"],
        client_order_id=row["client_order_id"],
        client_algo_id=row["client_algo_id"],
        entry_timeframe=row["entry_timeframe"],
        htf_timeframe=row["htf_timeframe"],
        htf_bias=row["htf_bias"],
        session=row["session"],
        market_structure=row["market_structure"],
        exit_price=row["exit_price"],
        pnl_usdt=row["pnl_usdt"],
        pnl_r=row["pnl_r"],
        fees_usdt=row["fees_usdt"] or 0.0,
        algo_ids=json.loads(_safe_col(row, "algo_ids") or "[]"),
        sl_moved_to_be=bool(_safe_col(row, "sl_moved_to_be") or 0),
        close_reason=_safe_col(row, "close_reason"),
        regime_at_entry=_safe_col(row, "regime_at_entry"),
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
        funding_z_6h=_safe_col(row, "funding_z_6h"),
        funding_z_24h=_safe_col(row, "funding_z_24h"),
        notes=row["notes"],
        screenshot_entry=row["screenshot_entry"],
        screenshot_exit=row["screenshot_exit"],
        real_market_entry_valid=_safe_bool(row, "real_market_entry_valid"),
        real_market_exit_valid=_safe_bool(row, "real_market_exit_valid"),
        demo_artifact=_safe_bool(row, "demo_artifact"),
        artifact_reason=_safe_col(row, "artifact_reason"),
        on_chain_context=_parse_on_chain_context(row),
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
        entry_timeframe: Optional[str] = None,
        htf_timeframe: Optional[str] = None,
        htf_bias: Optional[str] = None,
        session: Optional[str] = None,
        market_structure: Optional[str] = None,
        regime_at_entry: Optional[str] = None,
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
    ) -> TradeRecord:
        """Insert an OPEN row describing a freshly-placed trade.

        The returned `TradeRecord` carries the journal's own `trade_id`, which
        the caller MUST pass to `record_close` later.
        """
        conn = self._require_conn()
        algo = report.algo
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
            algo_id=algo.algo_id if algo else None,
            client_order_id=report.entry.client_order_id or None,
            client_algo_id=algo.client_algo_id if algo else None,
            algo_ids=[a.algo_id for a in report.algos if a.algo_id],
            entry_timeframe=entry_timeframe,
            htf_timeframe=htf_timeframe,
            htf_bias=htf_bias,
            session=session,
            market_structure=market_structure,
            regime_at_entry=regime_at_entry,
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
        entry_timeframe: Optional[str] = None,
        htf_timeframe: Optional[str] = None,
        htf_bias: Optional[str] = None,
        session: Optional[str] = None,
        market_structure: Optional[str] = None,
        proposed_sl_price: Optional[float] = None,
        proposed_tp_price: Optional[float] = None,
        proposed_rr_ratio: Optional[float] = None,
        regime_at_entry: Optional[str] = None,
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
    ) -> RejectedSignal:
        """Insert a single row into `rejected_signals`.

        Only called by the runner on `plan is None` return. Never raises on
        duplicate — we generate a fresh uuid per call, the table is
        append-only. Counter-factual outcome fields stay NULL until
        `peg_rejected_outcomes.py` walks candles forward and stamps them.
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
            entry_timeframe=entry_timeframe,
            htf_timeframe=htf_timeframe,
            htf_bias=htf_bias,
            session=session,
            market_structure=market_structure,
            proposed_sl_price=proposed_sl_price,
            proposed_tp_price=proposed_tp_price,
            proposed_rr_ratio=proposed_rr_ratio,
            regime_at_entry=regime_at_entry,
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
        hypothetical_outcome: str,
        bars_to_tp: Optional[int] = None,
        bars_to_sl: Optional[int] = None,
    ) -> None:
        """Stamp the N-bar counter-factual on an existing rejected_signals row.

        Called by `scripts/peg_rejected_outcomes.py`. `hypothetical_outcome`
        is one of 'WIN' (TP hit first), 'LOSS' (SL hit first), 'NEITHER'.
        Raises `KeyError` on unknown id so the script's dry-run catches stale data.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """UPDATE rejected_signals SET
                   hypothetical_outcome = ?,
                   hypothetical_bars_to_tp = ?,
                   hypothetical_bars_to_sl = ?
               WHERE rejection_id = ?""",
            (hypothetical_outcome, bars_to_tp, bars_to_sl, rejection_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"No rejected_signal with id={rejection_id!r}")

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
        operator aborted before SL/TP could evaluate."""
        conn = self._require_conn()
        cur = await conn.execute(
            "UPDATE trades SET outcome = ?, notes = ? WHERE trade_id = ?",
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
        coinbase_asia_skew_usd: Optional[float],
        bnb_self_flow_24h_usd: Optional[float],
        altcoin_index: Optional[float],
        snapshot_age_s: Optional[int],
        fresh: bool,
        whale_blackout_active: bool,
    ) -> int:
        """Append one row to `on_chain_snapshots` — time-series of Arkham state.

        Intended cadence: ONLY when the upstream snapshot fingerprint actually
        changes. Runner's `_maybe_record_on_chain_snapshot` owns dedup; this
        method is a dumb writer and will insert whatever it's given. Returns
        the new row's `id` for callers that want to reference it.
        """
        conn = self._require_conn()
        cur = await conn.execute(
            """INSERT INTO on_chain_snapshots (
                   captured_at,
                   daily_macro_bias,
                   stablecoin_pulse_1h_usd,
                   cex_btc_netflow_24h_usd,
                   cex_eth_netflow_24h_usd,
                   coinbase_asia_skew_usd,
                   bnb_self_flow_24h_usd,
                   altcoin_index,
                   snapshot_age_s,
                   fresh,
                   whale_blackout_active
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _iso(captured_at),
                daily_macro_bias,
                stablecoin_pulse_1h_usd,
                cex_btc_netflow_24h_usd,
                cex_eth_netflow_24h_usd,
                coinbase_asia_skew_usd,
                bnb_self_flow_24h_usd,
                altcoin_index,
                snapshot_age_s,
                int(fresh),
                int(whale_blackout_active),
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
