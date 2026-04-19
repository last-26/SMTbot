"""Print a performance summary from the trade journal.

Usage:
    .venv/Scripts/python.exe scripts/report.py --last 7d
    .venv/Scripts/python.exe scripts/report.py --last all --starting-balance 5000
    .venv/Scripts/python.exe scripts/report.py --db path/to/trades.db

When `--db` is omitted, reads `journal.db_path` from `config/default.yaml`.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.journal.database import TradeJournal
from src.journal.reporter import format_summary, summary


def _parse_window(arg: str) -> Optional[datetime]:
    """Parse '7d' / '30d' / '12h' / 'all' into a `since` datetime or None."""
    if arg == "all":
        return None
    m = re.fullmatch(r"(\d+)([dh])", arg)
    if not m:
        raise argparse.ArgumentTypeError(
            f"--last must be 'all' or NNd/NNh (got {arg!r})"
        )
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    return datetime.now(tz=timezone.utc) - delta


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_db_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    cfg = _load_cfg()
    return (cfg.get("journal") or {}).get("db_path", "data/trades.db")


def _resolve_clean_since() -> Optional[datetime]:
    """Return `rl.clean_since` from YAML as a UTC datetime, or None."""
    cfg = _load_cfg()
    raw = (cfg.get("rl") or {}).get("clean_since")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _run(
    db_path: str,
    since: Optional[datetime],
    starting_balance: float,
    exclude_artifacts: bool,
) -> int:
    async with TradeJournal(db_path) as j:
        closed = await j.list_closed_trades(since=since)
    if not closed:
        window = "all time" if since is None else f"since {since.isoformat()}"
        print(f"No closed trades in window ({window}).")
        return 0
    if exclude_artifacts:
        before = len(closed)
        closed = [t for t in closed if t.demo_artifact is not True]
        dropped = before - len(closed)
        if dropped:
            print(f"[INFO] --exclude-artifacts dropped {dropped}/{before} artefact-flagged trade(s).")
        if not closed:
            print("No non-artefact trades in window.")
            return 0
    print(format_summary(summary(closed, starting_balance)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade journal report")
    parser.add_argument("--db", default=None, help="Path to trades.db")
    parser.add_argument("--last", default="7d", help="Window: e.g. 7d, 30d, 12h, all")
    parser.add_argument(
        "--starting-balance", type=float, default=10_000.0,
        help="Starting balance used for DD/Calmar math (default: 10000)",
    )
    parser.add_argument(
        "--ignore-clean-since", action="store_true",
        help="Include trades before `rl.clean_since` (default: honour cutoff)",
    )
    parser.add_argument(
        "--exclude-artifacts", action="store_true",
        help="Drop trades flagged `demo_artifact=1` by the Binance cross-check.",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    try:
        since = _parse_window(args.last)
    except argparse.ArgumentTypeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if not args.ignore_clean_since:
        clean_since = _resolve_clean_since()
        if clean_since is not None:
            since = clean_since if since is None else max(since, clean_since)

    if not Path(db_path).exists() and db_path != ":memory:":
        print(f"[WARN] DB not found at {db_path} - nothing to report.")
        return 0

    return asyncio.run(
        _run(db_path, since, args.starting_balance, args.exclude_artifacts)
    )


if __name__ == "__main__":
    raise SystemExit(main())
