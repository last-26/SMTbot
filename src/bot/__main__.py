"""CLI entrypoint: `python -m src.bot --config config/default.yaml [--dry-run] [--once]`.

  --dry-run           : swap OrderRouter for dry_run_report; no Bybit orders placed.
  --once              : run exactly one tick and exit (smoke test for the wiring).
  --max-closed-trades : stop after N WIN/LOSS/BREAKEVEN rows hit the journal.
  --derivatives-only  : bypass the entry/exit pipeline; run liq-stream + cache
                        refresh only. Close-poll still fires so live positions
                        resolve. Pairs with --duration for timed data grabs.
  --duration N        : stop gracefully after N seconds (int).
  --clear-halt        : after journal replay, wipe halt state + reset daily PnL
                        and consecutive_losses. Use to resume trading after a
                        circuit-breaker cooldown without waiting for the timer.

Ctrl-C on Windows terminal short-circuits asyncio.run with a
KeyboardInterrupt, so we catch it here as a reliable backstop to the
signal handlers installed inside the runner.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

from src.bot.config import load_config
from src.bot.runner import BotRunner


def _configure_logging() -> None:
    """Stderr keeps ANSI colors for the terminal; the on-disk sink is plain
    text so `tail -f logs/bot.log` stays readable on any shell (default
    loguru leaks escape codes into the file on Windows)."""
    logger.remove()
    logger.add(sys.stderr, colorize=True, level="INFO",
               backtrace=False, diagnose=False)
    Path("logs").mkdir(exist_ok=True)
    logger.add(
        "logs/bot.log",
        colorize=False,
        enqueue=True,
        rotation="50 MB",
        retention=10,
        level="INFO",
        backtrace=False,
        diagnose=False,
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.bot")
    p.add_argument("--config", default="config/default.yaml",
                   help="Path to bot config YAML (default: config/default.yaml)")
    p.add_argument("--dry-run", action="store_true",
                   help="Use dry_run_report instead of placing real Bybit orders")
    p.add_argument("--once", action="store_true",
                   help="Run exactly one tick and exit (smoke test)")
    p.add_argument("--max-closed-trades", type=int, default=None,
                   help="Stop gracefully once N closed trades are in the journal "
                        "(WIN/LOSS/BREAKEVEN). Useful for RL data collection.")
    p.add_argument("--derivatives-only", action="store_true",
                   help="Bypass the entry pipeline; run only the derivatives "
                        "liquidation stream + cache refresh. Pairs with "
                        "--duration for timed data collection.")
    p.add_argument("--duration", type=int, default=None,
                   help="Stop gracefully after N seconds. Works with or "
                        "without --derivatives-only.")
    p.add_argument("--clear-halt", action="store_true",
                   help="After replaying the journal, clear halt state and "
                        "reset daily_realized_pnl + consecutive_losses so the "
                        "bot can trade immediately. Use after a circuit-breaker "
                        "cooldown when you've manually verified positions.")
    return p


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _parser().parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logger.error("config_not_found path={}", cfg_path)
        return 2

    cfg = load_config(cfg_path)
    runner = BotRunner.from_config(
        cfg,
        dry_run=args.dry_run,
        stop_after_closed_trades=args.max_closed_trades,
        derivatives_only=args.derivatives_only,
        duration_seconds=args.duration,
        clear_halt=args.clear_halt,
    )

    try:
        if args.once:
            asyncio.run(runner.run_once_then_exit())
        else:
            asyncio.run(runner.run())
    except KeyboardInterrupt:
        logger.info("interrupted_by_user")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
