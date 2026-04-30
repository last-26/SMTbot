"""Local SQLite cache for Bybit V5 klines (Pass 3 replay tune).

Why this exists
===============
``scripts/tune_confluence.py`` runs Optuna over hundreds of trials.
For each trial that exercises ``target_rr_ratio`` or
``zone_max_wait_bars`` knobs, the replay engine re-walks Bybit klines
against fresh proposed_sl/tp targets — without a cache that's hundreds
of trials × ~1640 reject rows × 1 REST call each = thousands of fetches
in a tight loop, well past Bybit's 120 req/5s ceiling and minutes
per trial of pure I/O.

Klines themselves don't change once a bar closes, so we cache the
``(symbol, interval, start_ms, max_bars)`` → ``list[Kline]`` lookup
once (pre-warm pass), then every Optuna trial reads from local SQLite
in microseconds.

Pegger (``scripts/peg_rejected_outcomes.py``) was the one-time
producer of these fetches in Pass 2.5; the cache is an additive
layer the pegger can also adopt later if we want to skip its own
re-fetch on a re-run. Today the cache populates via a dedicated
pre-warm script (``scripts/prewarm_kline_cache.py``).

Cache layout
------------
Single table ``kline_cache``::

    cache_key    TEXT PRIMARY KEY  -- "BTCUSDT_3_1714680000000_100"
    klines_json  TEXT NOT NULL     -- JSON list of {bar_start_ms, o, h, l, c}
    cached_at    TEXT NOT NULL     -- ISO-8601 UTC

The cache_key encodes the entire fetch input so a different ``max_bars``
or interval simply lives in a different row — no risk of stale slice
boundaries.

Concurrency
-----------
SQLite WAL mode handles reader/writer concurrency fine for our
single-process tune loop. The cache file lives at
``data/kline_cache.db`` by default; a separate file from
``data/trades.db`` so a corrupt cache never threatens the journal.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Protocol


@dataclass(frozen=True)
class Kline:
    """Normalized OHLC kline (mirrors scripts/peg_rejected_outcomes.py)."""
    bar_start_ms: int
    open: float
    high: float
    low: float
    close: float


class _BybitKlineFetcher(Protocol):
    """Minimal pybit-shape interface for the cache's miss path.

    The real ``pybit.unified_trading.HTTP.get_kline`` matches this; tests
    can pass a tiny stub instead of mocking pybit's full surface area.
    """

    def get_kline(
        self,
        *,
        category: str,
        symbol: str,
        interval: str,
        start: int,
        end: int,
        limit: int,
    ) -> dict: ...


_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kline_cache (
    cache_key   TEXT PRIMARY KEY,
    klines_json TEXT NOT NULL,
    cached_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kline_cache_cached_at ON kline_cache(cached_at);
"""


def _cache_key(
    *, bybit_symbol: str, interval_minutes: int,
    start_ms: int, max_bars: int,
) -> str:
    return f"{bybit_symbol}_{interval_minutes}_{start_ms}_{max_bars}"


def _normalize_kline_response(raw: dict) -> list[Kline]:
    """Bybit V5 returns klines DESC; flip to ASC. Skip malformed rows."""
    rows = raw.get("result", {}).get("list", []) or []
    out: list[Kline] = []
    for row in rows:
        try:
            out.append(Kline(
                bar_start_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            ))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda k: k.bar_start_ms)
    return out


def _serialise(klines: list[Kline]) -> str:
    return json.dumps([
        {"t": k.bar_start_ms, "o": k.open, "h": k.high,
         "l": k.low, "c": k.close}
        for k in klines
    ])


def _deserialise(raw: str) -> list[Kline]:
    out: list[Kline] = []
    for d in json.loads(raw):
        out.append(Kline(
            bar_start_ms=int(d["t"]),
            open=float(d["o"]),
            high=float(d["h"]),
            low=float(d["l"]),
            close=float(d["c"]),
        ))
    return out


class KlineCache:
    """SQLite-backed local cache for Bybit kline fetches.

    Sync API (sqlite3, not aiosqlite) — Optuna replay loop is CPU-bound
    not I/O-bound, and the cache lookups happen inside a tight per-trial
    inner loop where async overhead would be wasted. Pre-warm script
    can wrap calls in ``asyncio.to_thread`` if it needs concurrency
    against Bybit.
    """

    def __init__(self, db_path: str | Path = "data/kline_cache.db"):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CACHE_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def get(
        self,
        *,
        bybit_symbol: str,
        interval_minutes: int,
        start_ms: int,
        max_bars: int,
    ) -> Optional[list[Kline]]:
        """Cache lookup. Returns None on miss (no fallback fetch)."""
        key = _cache_key(
            bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
            start_ms=start_ms, max_bars=max_bars,
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT klines_json FROM kline_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return _deserialise(row[0])

    def put(
        self,
        *,
        bybit_symbol: str,
        interval_minutes: int,
        start_ms: int,
        max_bars: int,
        klines: list[Kline],
    ) -> None:
        """Idempotent insert — overwrites on cache_key collision."""
        key = _cache_key(
            bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
            start_ms=start_ms, max_bars=max_bars,
        )
        cached_at = datetime.now(tz=timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kline_cache "
                "(cache_key, klines_json, cached_at) VALUES (?, ?, ?)",
                (key, _serialise(klines), cached_at),
            )
            conn.commit()

    def get_or_fetch(
        self,
        *,
        bybit_symbol: str,
        interval_minutes: int,
        start_ms: int,
        max_bars: int,
        fetcher: Optional[_BybitKlineFetcher] = None,
    ) -> list[Kline]:
        """Cache-first lookup; falls back to ``fetcher`` on miss.

        Raises RuntimeError if cache miss + fetcher None — the caller
        promised pre-warming but a row leaked through unfetched.
        """
        cached = self.get(
            bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
            start_ms=start_ms, max_bars=max_bars,
        )
        if cached is not None:
            return cached
        if fetcher is None:
            raise RuntimeError(
                f"kline cache miss for {bybit_symbol} interval={interval_minutes}m "
                f"start_ms={start_ms} max_bars={max_bars} and no fetcher provided"
            )
        end_ms = start_ms + max_bars * interval_minutes * 60 * 1000
        raw = fetcher.get_kline(
            category="linear",
            symbol=bybit_symbol,
            interval=str(interval_minutes),
            start=start_ms,
            end=end_ms,
            limit=max_bars,
        )
        klines = _normalize_kline_response(raw)
        self.put(
            bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
            start_ms=start_ms, max_bars=max_bars, klines=klines,
        )
        return klines

    def stats(self) -> dict:
        """Quick health snapshot for the pre-warm script + tune banner."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(cached_at), MAX(cached_at) FROM kline_cache"
            ).fetchone()
        return {
            "n_rows": row[0] if row else 0,
            "oldest": row[1] if row else None,
            "newest": row[2] if row else None,
        }
