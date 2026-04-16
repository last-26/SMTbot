"""SMTbot runtime package: wires TV → analysis → strategy → execution → journal.

The outer async loop lives in `runner.py`; `__main__.py` is the CLI entrypoint
invoked by `python -m src.bot`. `config.py` exposes `load_config(path)` which
merges `.env` secrets into the YAML config tree and validates the result.
"""

from __future__ import annotations

from src.bot.config import BotConfig, load_config

__all__ = ["BotConfig", "load_config"]
