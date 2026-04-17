"""Async persist layer for the derivatives data layer (Phase 1.5 Madde 3).

Shares the same SQLite file as `TradeJournal` but owns two separate tables
(`liquidations`, `derivatives_snapshots`). Keeping it in its own class means
the trade journal's migration list isn't cluttered with derivatives rows
that most users don't care about.

All writes wrap in try/except so a DB hiccup never propagates into the WS
listener or the poll loop that calls us.
"""

from __future__ import annotations

from typing import Optional

import aiosqlite
from loguru import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    price        REAL NOT NULL,
    quantity     REAL NOT NULL,
    notional_usd REAL NOT NULL,
    ts_ms        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_symbol_ts
    ON liquidations(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS derivatives_snapshots (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                        TEXT NOT NULL,
    ts_ms                         INTEGER NOT NULL,
    funding_rate_current          REAL,
    funding_rate_predicted        REAL,
    open_interest_usd             REAL,
    oi_change_1h_pct              REAL,
    oi_change_24h_pct             REAL,
    long_short_ratio              REAL,
    aggregated_long_liq_1h_usd    REAL,
    aggregated_short_liq_1h_usd   REAL
);
CREATE INDEX IF NOT EXISTS idx_deriv_symbol_ts
    ON derivatives_snapshots(symbol, ts_ms DESC);
"""


class DerivativesJournal:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def ensure_schema(self) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(_SCHEMA)
                await db.commit()
        except Exception as e:
            logger.warning("derivatives_schema_failed err={!r}", e)

    # ── Writes ────────────────────────────────────────────────────────────

    async def insert_liquidation(self, ev) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO liquidations "
                    "(symbol, side, price, quantity, notional_usd, ts_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ev.symbol, ev.side, ev.price, ev.quantity,
                     ev.notional_usd, ev.ts_ms),
                )
                await db.commit()
        except Exception as e:
            logger.warning("liq_insert_failed err={!r}", e)

    async def insert_snapshot(self, snap) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO derivatives_snapshots "
                    "(symbol, ts_ms, funding_rate_current, funding_rate_predicted, "
                    " open_interest_usd, oi_change_1h_pct, oi_change_24h_pct, "
                    " long_short_ratio, aggregated_long_liq_1h_usd, "
                    " aggregated_short_liq_1h_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (snap.symbol, snap.ts_ms,
                     snap.funding_rate_current, snap.funding_rate_predicted,
                     snap.open_interest_usd,
                     getattr(snap, "oi_change_1h_pct", 0.0),
                     getattr(snap, "oi_change_24h_pct", 0.0),
                     snap.long_short_ratio,
                     snap.aggregated_long_liq_1h_usd,
                     snap.aggregated_short_liq_1h_usd),
                )
                await db.commit()
        except Exception as e:
            logger.warning("snap_insert_failed err={!r}", e)

    # ── Reads (Phase 7 RL dataset helpers) ─────────────────────────────────

    async def fetch_funding_history(
        self, symbol: str, lookback_ms: int,
    ) -> list[tuple[int, Optional[float]]]:
        """Return [(ts_ms, funding_rate_current)] for the window, ASC order."""
        cutoff = _now_ms() - lookback_ms
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT ts_ms, funding_rate_current "
                    "FROM derivatives_snapshots "
                    "WHERE symbol=? AND ts_ms>=? "
                    "ORDER BY ts_ms ASC",
                    (symbol, cutoff),
                ) as cur:
                    rows = await cur.fetchall()
                    return [(int(r[0]), r[1]) for r in rows]
        except Exception as e:
            logger.warning("funding_history_fetch_failed err={!r}", e)
            return []

    async def fetch_oi_history(
        self, symbol: str, lookback_ms: int,
    ) -> list[tuple[int, Optional[float]]]:
        cutoff = _now_ms() - lookback_ms
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT ts_ms, open_interest_usd "
                    "FROM derivatives_snapshots "
                    "WHERE symbol=? AND ts_ms>=? "
                    "ORDER BY ts_ms ASC",
                    (symbol, cutoff),
                ) as cur:
                    rows = await cur.fetchall()
                    return [(int(r[0]), r[1]) for r in rows]
        except Exception as e:
            logger.warning("oi_history_fetch_failed err={!r}", e)
            return []


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
