"""CLI entry — `python -m src.dashboard`.

Usage:
    .venv/Scripts/python.exe -m src.dashboard
    .venv/Scripts/python.exe -m src.dashboard --port 8765
    .venv/Scripts/python.exe -m src.dashboard --config config/default.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from src.dashboard.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="SMTbot trade dashboard")
    parser.add_argument(
        "--config", default="config/default.yaml",
        help="Path to YAML config (defaults to config/default.yaml)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default 127.0.0.1 — localhost only)",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="Port (default 8765)",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        print(f"[ERROR] config not found: {cfg_path}")
        return 2

    app = create_app(cfg_path)
    print(f"Dashboard running at http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
