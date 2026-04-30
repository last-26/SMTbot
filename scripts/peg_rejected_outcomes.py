"""Bybit-native counter-factual reject pegger (Pass 2.5).

For every row in `rejected_signals` with `proposed_sl_price` and
`proposed_tp_price` populated but `hypothetical_outcome IS NULL`,
fetch Bybit linear-perp 3m klines from `signal_timestamp + 1 bar`
forward (max 100 bars = 5 hours), walk candle-by-candle, and stamp
the row WIN/LOSS/TIMEOUT depending on which target hit first.

Why this exists
===============
Pass 3 GBT counter-factual analysis ("which rejected setups would have
actually won?") needs every reject row to carry a hypothetical outcome.
Without it, GBT can only train hard-gate-toggle decisions on the 50
closed-trade outcomes — too small a dataset. The pegger adds ~1640
counter-factual data points (50 closed + ~1600 reject = ~1690-row
feature matrix).

Algorithm
---------
For each row (LONG):
    For bar in klines[1:101]:  # skip placement bar; up to 100 lookforward
        if bar.low <= proposed_sl_price:  return LOSS, bars_to_sl=offset
        if bar.high >= proposed_tp_price: return WIN, bars_to_tp=offset
        # Same-bar SL+TP collision: SL evaluated first (pessimistic).
    return TIMEOUT (neither hit within 100 bars)

SHORT mirrors: bar.high >= sl → LOSS, bar.low <= tp → WIN.

Idempotent — rows with `hypothetical_outcome IS NOT NULL` are skipped
unless `--rerun-all` is passed.

Rate-limit budget
-----------------
Bybit V5 public REST: 120 req / 5s = 24 req/s. We default to a
concurrency-5 semaphore (5 req/s sustained, far below ceiling). One
kline call per row → ~1640 rows × 0.2s/row × 5 parallel ≈ 6-7 minutes.

Usage
-----
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --limit 10
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --dry-run
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --symbols BTC,ETH
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --rerun-all

Bot must be stopped (or pegger must run between bot restarts) so the
journal isn't writing reject rows in parallel — concurrent inserts
themselves are fine, but a fresh row inserted mid-pegger run would be
missed by this batch and need a follow-up pass.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from src.data.kline_cache import Kline, _normalize_kline_response
from src.execution.bybit_client import _INTERNAL_TO_BYBIT_SYMBOL
from src.journal.database import TradeJournal
from src.strategy.kline_walk import (
    PegResult,
    signal_ts_to_bar_start_ms as _signal_ts_to_bar_start_ms,
    walk_klines,
)


# ── Domain types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PegInput:
    """Minimal subset of `RejectedSignal` the walk needs."""
    rejection_id: str
    symbol: str  # internal format, e.g. "BTC-USDT-SWAP"
    direction: str  # "BULLISH" or "BEARISH"
    signal_timestamp: datetime
    proposed_sl_price: float
    proposed_tp_price: float


# ── Bybit kline fetch ───────────────────────────────────────────────────────


def _to_bybit_symbol(internal: str) -> str:
    return _INTERNAL_TO_BYBIT_SYMBOL.get(internal, internal)


async def _fetch_klines_for_peg(
    *,
    bybit: HTTP,
    symbol_internal: str,
    signal_ts: datetime,
    interval_minutes: int,
    max_bars: int,
) -> list[Kline]:
    """Fetch up to `max_bars` candles starting from `signal_ts + 1 bar`."""
    bybit_symbol = _to_bybit_symbol(symbol_internal)
    bar_ms = interval_minutes * 60 * 1000
    start_ms = _signal_ts_to_bar_start_ms(
        signal_ts, interval_minutes=interval_minutes,
    )
    end_ms = start_ms + max_bars * bar_ms
    raw = await asyncio.to_thread(
        bybit.get_kline,
        category="linear",
        symbol=bybit_symbol,
        interval=str(interval_minutes),
        start=start_ms,
        end=end_ms,
        limit=max_bars,
    )
    return _normalize_kline_response(raw)


# ── Orchestration ───────────────────────────────────────────────────────────


async def _peg_one(
    *,
    bybit: HTTP,
    journal: TradeJournal,
    inp: PegInput,
    interval_minutes: int,
    max_bars: int,
    semaphore: asyncio.Semaphore,
    dry_run: bool,
) -> tuple[PegInput, PegResult]:
    async with semaphore:
        klines = await _fetch_klines_for_peg(
            bybit=bybit,
            symbol_internal=inp.symbol,
            signal_ts=inp.signal_timestamp,
            interval_minutes=interval_minutes,
            max_bars=max_bars,
        )
        result = walk_klines(
            direction=inp.direction,
            proposed_sl_price=inp.proposed_sl_price,
            proposed_tp_price=inp.proposed_tp_price,
            klines=klines,
            max_bars=max_bars,
        )
        if not dry_run and result.outcome in ("WIN", "LOSS", "TIMEOUT"):
            await journal.update_rejected_outcome(
                inp.rejection_id,
                outcome=result.outcome,
                bars_to_tp=result.bars_to_tp,
                bars_to_sl=result.bars_to_sl,
            )
        return inp, result


async def _gather_pegs(
    *,
    bybit: HTTP,
    journal: TradeJournal,
    inputs: list[PegInput],
    interval_minutes: int,
    max_bars: int,
    concurrency: int,
    dry_run: bool,
) -> list[tuple[PegInput, PegResult]]:
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _peg_one(
            bybit=bybit, journal=journal, inp=inp,
            interval_minutes=interval_minutes, max_bars=max_bars,
            semaphore=semaphore, dry_run=dry_run,
        )
        for inp in inputs
    ]
    return await asyncio.gather(*tasks, return_exceptions=False)


async def _load_pegging_inputs(
    journal: TradeJournal,
    *,
    rerun_all: bool,
    symbol_filter: Optional[set[str]],
    limit: Optional[int],
) -> list[PegInput]:
    """Read rejected_signals → PegInput list, applying skip rules."""
    rows = await journal.list_rejected_signals()
    out: list[PegInput] = []
    for r in rows:
        if r.proposed_sl_price is None or r.proposed_tp_price is None:
            continue  # peg has no targets
        if not rerun_all and r.hypothetical_outcome is not None:
            continue  # already pegged
        if symbol_filter is not None and r.symbol not in symbol_filter:
            continue
        if r.signal_timestamp is None:
            continue
        out.append(PegInput(
            rejection_id=r.rejection_id,
            symbol=r.symbol,
            direction=r.direction.value,
            signal_timestamp=r.signal_timestamp,
            proposed_sl_price=float(r.proposed_sl_price),
            proposed_tp_price=float(r.proposed_tp_price),
        ))
        if limit is not None and len(out) >= limit:
            break
    return out


def _expand_symbol_filter(arg: Optional[str]) -> Optional[set[str]]:
    """`--symbols BTC,ETH` → {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}."""
    if not arg:
        return None
    out: set[str] = set()
    for raw in arg.split(","):
        token = raw.strip().upper()
        if not token:
            continue
        if "-USDT-SWAP" in token:
            out.add(token)
        else:
            out.add(f"{token}-USDT-SWAP")
    return out or None


async def _run(args: argparse.Namespace) -> int:
    load_dotenv()
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    demo = os.getenv("BYBIT_DEMO", "1") == "1"
    bybit = HTTP(
        testnet=False, demo=demo,
        api_key=api_key, api_secret=api_secret,
    )
    journal = TradeJournal(args.db)
    await journal.connect()
    try:
        inputs = await _load_pegging_inputs(
            journal,
            rerun_all=args.rerun_all,
            symbol_filter=_expand_symbol_filter(args.symbols),
            limit=args.limit,
        )
        print(f"pegger: {len(inputs)} rows queued (concurrency={args.concurrency}, "
              f"interval={args.interval_minutes}m, max_bars={args.max_bars}, "
              f"dry_run={args.dry_run})")
        if not inputs:
            print("nothing to peg.")
            return 0
        results = await _gather_pegs(
            bybit=bybit, journal=journal, inputs=inputs,
            interval_minutes=args.interval_minutes,
            max_bars=args.max_bars,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
        # Tally
        tally: dict[str, int] = {"WIN": 0, "LOSS": 0, "TIMEOUT": 0, "SKIP": 0}
        for _inp, res in results:
            tally[res.outcome] = tally.get(res.outcome, 0) + 1
        n = len(results)
        wr = (tally["WIN"] / max(tally["WIN"] + tally["LOSS"], 1)) * 100
        print(f"pegger: done. n={n}  WIN={tally['WIN']}  "
              f"LOSS={tally['LOSS']}  TIMEOUT={tally['TIMEOUT']}  "
              f"SKIP={tally['SKIP']}  hypothetical_WR={wr:.1f}%")
        if args.verbose:
            for inp, res in results[:20]:
                print(f"  {inp.symbol:20s} {inp.direction:8s} "
                      f"signal={inp.signal_timestamp.isoformat()} "
                      f"-> {res.outcome} (tp_bar={res.bars_to_tp} "
                      f"sl_bar={res.bars_to_sl})")
        return 0
    finally:
        await journal.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default="data/trades.db",
                   help="Path to journal SQLite (default: data/trades.db)")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N rows (smoke testing)")
    p.add_argument("--rerun-all", action="store_true",
                   help="Re-peg rows that already have hypothetical_outcome")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk klines but skip the UPDATE")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated filter, e.g. 'BTC,ETH'")
    p.add_argument("--concurrency", type=int, default=5,
                   help="Concurrent kline fetches (default 5)")
    p.add_argument("--interval-minutes", type=int, default=3,
                   help="Kline TF in minutes (default 3 = entry TF)")
    p.add_argument("--max-bars", type=int, default=100,
                   help="Lookforward bar count (default 100 = 5h on 3m)")
    p.add_argument("--verbose", action="store_true",
                   help="Print per-row outcome for first 20 rows")
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
