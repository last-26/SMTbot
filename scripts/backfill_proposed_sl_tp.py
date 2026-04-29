"""Backfill ATR-based what-if proposed SL/TP on pre-Pass-2.5 reject rows.

Pass 2.5.C wired `_record_reject` to compute and persist proposed_*
fields at insert time, but the ~1671 reject rows already in the journal
(post-Bybit-cut) were inserted BEFORE that path landed. This script
walks every such row, computes the SAME what-if SL/TP via the shared
`compute_what_if_proposed_sltp` helper, and persists via the journal's
`update_rejected_proposed_sltp` helper.

Without this backfill, the pegger (`scripts/peg_rejected_outcomes.py`)
has nothing to walk against on legacy rows — `WHERE proposed_sl_price
IS NULL` filters them out. Backfill THEN pegger gives Pass 3 GBT a
~1640-row counter-factual feature matrix.

Skip rules (mirror live `_record_reject` behavior):
  - row already has proposed_sl_price (--rerun-all to override)
  - reject_reason in NO_PROPOSED_SLTP_REASONS
  - direction == UNDEFINED
  - price or atr missing on the row (cancel-path rows where state was
    half-populated; rare)

Usage
-----
    .venv/Scripts/python.exe scripts/backfill_proposed_sl_tp.py
    .venv/Scripts/python.exe scripts/backfill_proposed_sl_tp.py --dry-run
    .venv/Scripts/python.exe scripts/backfill_proposed_sl_tp.py --limit 20
    .venv/Scripts/python.exe scripts/backfill_proposed_sl_tp.py --rerun-all

Bot must be stopped during backfill; the journal would otherwise insert
new rows mid-run that this batch would miss (a follow-up `--limit
unlimited` pass would catch them, harmless but wasteful).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.bot.config import load_config
from src.journal.database import TradeJournal
from src.strategy.what_if_sltp import compute_what_if_proposed_sltp


@dataclass
class BackfillStats:
    total_rows: int = 0
    already_filled: int = 0  # skipped because proposed_sl_price already set
    skip_reason_class: int = 0  # NO_PROPOSED_SLTP_REASONS
    skip_undefined_direction: int = 0
    skip_missing_price_atr: int = 0
    updated: int = 0


async def _backfill(args: argparse.Namespace) -> BackfillStats:
    cfg = load_config(args.config)
    target_rr = cfg.execution.target_rr_ratio
    journal = TradeJournal(args.db)
    await journal.connect()
    stats = BackfillStats()
    try:
        rows = await journal.list_rejected_signals()
        stats.total_rows = len(rows)
        for r in rows:
            if r.proposed_sl_price is not None and not args.rerun_all:
                stats.already_filled += 1
                continue
            floor_pct = cfg.min_sl_distance_pct_for(r.symbol)
            sl, tp, rr = compute_what_if_proposed_sltp(
                symbol=r.symbol,
                direction=r.direction,
                price=r.price,
                atr=r.atr,
                reject_reason=r.reject_reason,
                floor_pct=floor_pct,
                target_rr=target_rr,
            )
            if sl is None or tp is None or rr is None:
                # Classify the skip reason for stats reporting
                from src.strategy.what_if_sltp import (
                    NO_PROPOSED_SLTP_REASONS,
                )
                from src.data.models import Direction
                if r.reject_reason in NO_PROPOSED_SLTP_REASONS:
                    stats.skip_reason_class += 1
                elif r.direction == Direction.UNDEFINED:
                    stats.skip_undefined_direction += 1
                else:
                    stats.skip_missing_price_atr += 1
                continue
            if not args.dry_run:
                await journal.update_rejected_proposed_sltp(
                    r.rejection_id,
                    proposed_sl_price=sl,
                    proposed_tp_price=tp,
                    proposed_rr_ratio=rr,
                )
            stats.updated += 1
            if args.limit is not None and stats.updated >= args.limit:
                break
    finally:
        await journal.close()
    return stats


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default="data/trades.db",
                   help="Path to journal SQLite (default: data/trades.db)")
    p.add_argument("--config", default="config/default.yaml",
                   help="Config path for floor + target_rr lookup")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N updates (smoke testing)")
    p.add_argument("--rerun-all", action="store_true",
                   help="Re-stamp rows that already have proposed_sl_price")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute but skip the UPDATE")
    return p


def _print_stats(stats: BackfillStats, *, dry_run: bool) -> None:
    verb = "would update" if dry_run else "updated"
    print(f"backfill: total_rows={stats.total_rows}")
    print(f"          already_filled={stats.already_filled} (skipped)")
    print(f"          skip_reason_class={stats.skip_reason_class} "
          f"(NO_PROPOSED_SLTP_REASONS — meaningless target)")
    print(f"          skip_undefined_direction={stats.skip_undefined_direction}")
    print(f"          skip_missing_price_atr={stats.skip_missing_price_atr}")
    print(f"          {verb}={stats.updated}")
    accounted = (stats.already_filled + stats.skip_reason_class
                 + stats.skip_undefined_direction
                 + stats.skip_missing_price_atr + stats.updated)
    if accounted != stats.total_rows:
        print(f"WARNING: counts don't sum: {accounted} != {stats.total_rows}")


async def _run(args: argparse.Namespace) -> int:
    stats = await _backfill(args)
    _print_stats(stats, dry_run=args.dry_run)
    return 0


def main() -> int:
    args = _build_argparser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
