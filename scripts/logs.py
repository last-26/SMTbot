"""Terminal log viewer for the bot — tail + filter + ANSI highlight.

Usage (run from project root):

    .venv/Scripts/python.exe scripts/logs.py                  # follow all
    .venv/Scripts/python.exe scripts/logs.py --decisions      # only entry/exit decisions
    .venv/Scripts/python.exe scripts/logs.py --errors         # only ERROR/WARNING
    .venv/Scripts/python.exe scripts/logs.py --filter SOL     # lines matching SOL
    .venv/Scripts/python.exe scripts/logs.py --lines 200      # start with last 200
    .venv/Scripts/python.exe scripts/logs.py --no-follow      # print and exit

Combine freely, e.g. `--decisions --filter SOL --lines 500`.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "bot.log"

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

DECISION_PATTERNS = (
    "symbol_decision", "opened ", "order_rejected", "reentry_blocked",
    "blocked symbol=", "algo_failure", "defensive_close", "closed ",
    "stop_after_closed_trades_reached",
)


def _colorize(line: str) -> str:
    if "ERROR" in line:
        return f"{RED}{line}{RESET}"
    if "WARNING" in line:
        return f"{YELLOW}{line}{RESET}"
    if "PLANNED" in line:
        return f"{GREEN}{line}{RESET}"
    if "NO_TRADE" in line:
        return f"{DIM}{line}{RESET}"
    if "opened " in line or "closed " in line:
        return f"{BOLD}{CYAN}{line}{RESET}"
    if "order_rejected" in line or "algo_failure" in line:
        return f"{RED}{line}{RESET}"
    if "reentry_blocked" in line or "blocked symbol=" in line:
        return f"{MAGENTA}{line}{RESET}"
    if "symbol_cycle_start" in line:
        return f"{BLUE}{line}{RESET}"
    return line


def _matches(line: str, args: argparse.Namespace) -> bool:
    if args.decisions and not any(p in line for p in DECISION_PATTERNS):
        return False
    if args.errors and "ERROR" not in line and "WARNING" not in line:
        return False
    if args.filter and not re.search(args.filter, line):
        return False
    return True


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            text = data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return text.splitlines()[-lines:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--path", default=str(LOG_PATH),
                    help=f"log file path (default: {LOG_PATH})")
    ap.add_argument("--lines", type=int, default=50,
                    help="initial history lines to print (default: 50)")
    ap.add_argument("--decisions", action="store_true",
                    help="only entry/exit decision lines")
    ap.add_argument("--errors", action="store_true",
                    help="only ERROR / WARNING lines")
    ap.add_argument("--filter", default="",
                    help="regex; only lines matching this pattern")
    ap.add_argument("--no-follow", action="store_true",
                    help="print history and exit (no tail -F)")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors")
    args = ap.parse_args()

    if args.no_color:
        global _colorize
        _colorize = lambda s: s   # noqa: E731

    path = Path(args.path)
    if not path.exists():
        print(f"log file not found: {path}", file=sys.stderr)
        return 1

    for line in _tail(path, args.lines):
        if _matches(line, args):
            print(_colorize(line))

    if args.no_follow:
        return 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # jump to end
        while True:
            line = f.readline()
            if not line:
                try:
                    time.sleep(0.4)
                except KeyboardInterrupt:
                    return 0
                # Handle rotation: if file shrank, reopen
                try:
                    if f.tell() > path.stat().st_size:
                        f.seek(0)
                except OSError:
                    pass
                continue
            line = line.rstrip("\n")
            if _matches(line, args):
                print(_colorize(line))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
