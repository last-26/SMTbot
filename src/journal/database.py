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
from src.journal.models import TradeOutcome, TradeRecord
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

    notes               TEXT,
    screenshot_entry    TEXT,
    screenshot_exit     TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_outcome      ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts     ON trades(entry_timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts      ON trades(exit_timestamp);
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
    "algo_ids", "close_reason",
    "regime_at_entry", "funding_z_at_entry", "ls_ratio_at_entry",
    "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry",
    "nearest_liq_cluster_above_price", "nearest_liq_cluster_below_price",
    "nearest_liq_cluster_above_notional", "nearest_liq_cluster_below_notional",
    "notes", "screenshot_entry", "screenshot_exit",
]


# Idempotent migrations — each `ALTER TABLE ... ADD COLUMN` is wrapped in
# a try/except so re-running on a DB that already has the column is a no-op.
_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN algo_ids TEXT NOT NULL DEFAULT '[]'",
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
        json.dumps(rec.algo_ids), rec.close_reason,
        rec.regime_at_entry, rec.funding_z_at_entry, rec.ls_ratio_at_entry,
        rec.oi_change_24h_at_entry, rec.liq_imbalance_1h_at_entry,
        rec.nearest_liq_cluster_above_price, rec.nearest_liq_cluster_below_price,
        rec.nearest_liq_cluster_above_notional, rec.nearest_liq_cluster_below_notional,
        rec.notes, rec.screenshot_entry, rec.screenshot_exit,
    )


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
        notes=row["notes"],
        screenshot_entry=row["screenshot_entry"],
        screenshot_exit=row["screenshot_exit"],
    )


def _safe_col(row: aiosqlite.Row, name: str):
    """Access a column that may not exist on a pre-migration row."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


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

    async def update_algo_ids(self, trade_id: str, algo_ids: list[str]) -> None:
        """Rewrite the `algo_ids` column — used by the SL-to-BE path when
        the monitor replaces TP2 with a new algo and needs the new ID in
        the journal."""
        conn = self._require_conn()
        cur = await conn.execute(
            "UPDATE trades SET algo_ids = ? WHERE trade_id = ?",
            (json.dumps(list(algo_ids)), trade_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"No trade with id={trade_id!r}")

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

    async def replay_for_risk_manager(self, mgr: RiskManager) -> None:
        """Walk closed trades in order and replay them into `mgr` so its
        peak/DD/streak counters match reality before the loop resumes.

        We call `register_trade_opened` + `register_trade_closed` for each
        closed row — the open→close pairing matters because the manager tracks
        `open_positions` which must end at zero once we've replayed everything.
        """
        closed = await self.list_closed_trades()
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
