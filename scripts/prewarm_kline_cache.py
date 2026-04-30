"""Pre-warm Bybit kline cache for replay tune (Pass 3 Faz-A).

scripts/tune_confluence.py'ın target_rr_ratio knob'u her trial'da
proposed_tp'yi recompute edip Bybit klinelerini RE-walk eder. Cache
olmadan: 600 trial × 1634 reject × 1 REST call ≈ 980k istek + Bybit
120/5s rate ihlali + dakikalar süren I/O. Pre-warm bir kez fetch'leyip
data/kline_cache.db'ye yazar (~6-7 dk one-time), sonra Optuna trial'ları
mikrosaniyede local SQLite'tan okur.

Idempotent: cache hit row'ları atlar; --force ile yeniden fetch.

Usage::

    .venv/Scripts/python.exe scripts/prewarm_kline_cache.py
    .venv/Scripts/python.exe scripts/prewarm_kline_cache.py --limit 20
    .venv/Scripts/python.exe scripts/prewarm_kline_cache.py --force
    .venv/Scripts/python.exe scripts/prewarm_kline_cache.py --interval 3 --max-bars 100

Bot must be stopped (or pre-warm runs between bot restarts) so the
journal isn't writing fresh reject rows mid-pass that this batch would
miss — re-running pre-warm afterwards is harmless (idempotent).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from src.data.kline_cache import KlineCache
from src.execution.bybit_client import _INTERNAL_TO_BYBIT_SYMBOL
from src.journal.database import TradeJournal
from src.strategy.kline_walk import signal_ts_to_bar_start_ms


@dataclass
class PrewarmStats:
    total_rows: int = 0
    skipped_no_proposed: int = 0
    skipped_cache_hit: int = 0
    fetched: int = 0
    failed: int = 0


def _to_bybit_symbol(internal: str) -> str:
    return _INTERNAL_TO_BYBIT_SYMBOL.get(internal, internal)


async def _prewarm_one(
    *,
    bybit: HTTP,
    cache: KlineCache,
    bybit_symbol: str,
    signal_ts: datetime,
    interval_minutes: int,
    max_bars: int,
    semaphore: asyncio.Semaphore,
    force: bool,
) -> str:
    """Returns one of: 'hit', 'fetched', 'failed'."""
    start_ms = signal_ts_to_bar_start_ms(
        signal_ts, interval_minutes=interval_minutes,
    )
    if not force:
        cached = cache.get(
            bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
            start_ms=start_ms, max_bars=max_bars,
        )
        if cached is not None:
            return "hit"
    async with semaphore:
        try:
            await asyncio.to_thread(
                cache.get_or_fetch,
                bybit_symbol=bybit_symbol,
                interval_minutes=interval_minutes,
                start_ms=start_ms, max_bars=max_bars,
                fetcher=bybit,
            )
            return "fetched"
        except Exception as e:  # noqa: BLE001 — fetch can fail; report and skip
            print(f"  [WARN] fetch failed: {bybit_symbol} sig_ts={signal_ts} {e}")
            return "failed"


async def _run(args: argparse.Namespace) -> int:
    load_dotenv()
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    demo = os.getenv("BYBIT_DEMO", "1") == "1"

    bybit = HTTP(
        testnet=False, demo=demo,
        api_key=api_key, api_secret=api_secret,
    )
    cache = KlineCache(args.kline_cache_db)
    journal = TradeJournal(args.db)
    await journal.connect()
    stats = PrewarmStats()
    semaphore = asyncio.Semaphore(args.concurrency)

    try:
        rows = await journal.list_rejected_signals()
        stats.total_rows = len(rows)
        eligible = [
            r for r in rows
            if r.proposed_sl_price is not None and r.signal_timestamp is not None
        ]
        stats.skipped_no_proposed = stats.total_rows - len(eligible)
        if args.limit is not None:
            eligible = eligible[: args.limit]

        print(f"prewarm: total_rows={stats.total_rows} "
              f"skipped_no_proposed={stats.skipped_no_proposed} "
              f"queued={len(eligible)} concurrency={args.concurrency} "
              f"interval={args.interval}m max_bars={args.max_bars} "
              f"force={args.force}")
        if not eligible:
            print("nothing to prewarm.")
            return 0

        tasks = [
            _prewarm_one(
                bybit=bybit, cache=cache,
                bybit_symbol=_to_bybit_symbol(r.symbol),
                signal_ts=r.signal_timestamp,
                interval_minutes=args.interval, max_bars=args.max_bars,
                semaphore=semaphore, force=args.force,
            )
            for r in eligible
        ]

        # Progress: report every ~10% milestone
        results: list[str] = []
        n = len(tasks)
        milestone = max(1, n // 10)
        for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
            res = await coro
            results.append(res)
            if i % milestone == 0 or i == n:
                progress_hits = sum(1 for r in results if r == "hit")
                progress_fetched = sum(1 for r in results if r == "fetched")
                progress_failed = sum(1 for r in results if r == "failed")
                print(f"  progress {i}/{n}  hits={progress_hits}  "
                      f"fetched={progress_fetched}  failed={progress_failed}")

        for res in results:
            if res == "hit":
                stats.skipped_cache_hit += 1
            elif res == "fetched":
                stats.fetched += 1
            elif res == "failed":
                stats.failed += 1

        cs = cache.stats()
        print()
        print(f"prewarm: done. total_rows={stats.total_rows}  "
              f"no_proposed={stats.skipped_no_proposed}  "
              f"cache_hits={stats.skipped_cache_hit}  "
              f"fetched={stats.fetched}  failed={stats.failed}")
        print(f"KlineCache after: {cs['n_rows']} rows  "
              f"oldest={cs['oldest']}  newest={cs['newest']}")
        return 0 if stats.failed == 0 else 1
    finally:
        await journal.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default="data/trades.db",
                   help="Path to journal SQLite (default: data/trades.db)")
    p.add_argument("--kline-cache-db", default="data/kline_cache.db",
                   help="Path to KlineCache SQLite (default: "
                        "data/kline_cache.db)")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N rows (smoke testing)")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even when cache hit (default: skip hits)")
    p.add_argument("--concurrency", type=int, default=5,
                   help="Concurrent kline fetches (default 5; Bybit V5 "
                        "ceiling 120/5s)")
    p.add_argument("--interval", type=int, default=3,
                   help="Kline TF in minutes (default 3 = entry TF)")
    p.add_argument("--max-bars", type=int, default=100,
                   help="Lookforward bar count per row (default 100)")
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
