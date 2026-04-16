"""CLI entrypoint: `python -m src.bot --config config/default.yaml [--dry-run] [--once]`.

  --dry-run : swap OrderRouter for dry_run_report; no OKX orders placed.
  --once    : run exactly one tick and exit (smoke test for the wiring).

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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.bot")
    p.add_argument("--config", default="config/default.yaml",
                   help="Path to bot config YAML (default: config/default.yaml)")
    p.add_argument("--dry-run", action="store_true",
                   help="Use dry_run_report instead of placing real OKX orders")
    p.add_argument("--once", action="store_true",
                   help="Run exactly one tick and exit (smoke test)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logger.error("config_not_found path={}", cfg_path)
        return 2

    cfg = load_config(cfg_path)
    runner = BotRunner.from_config(cfg, dry_run=args.dry_run)

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
