"""TradingView MCP bridge — calls the TV CLI and returns parsed JSON.

The TV CLI (`node <tradingview-mcp>/src/cli/index.js`) communicates with
TradingView Desktop via CDP (Chrome DevTools Protocol) on port 9222.

This module wraps the CLI calls so the Python bot can read Pine Script
drawing objects (tables, labels, boxes, lines) and chart state.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger


# Default paths — override via env vars
_TV_MCP_DIR = os.getenv(
    "TV_MCP_DIR",
    str(Path.home() / "Desktop" / "tradingview-mcp"),
)
_TV_CLI_SCRIPT = os.path.join(_TV_MCP_DIR, "src", "cli", "index.js")
_TV_DEBUG_PORT = os.getenv("TV_DEBUG_PORT", "9222")


@dataclass
class TVBridge:
    """Async wrapper around the TradingView MCP CLI.

    Usage::

        bridge = TVBridge()
        status = await bridge.status()
        tables = await bridge.get_pine_tables()
    """

    cli_script: str = _TV_CLI_SCRIPT
    debug_port: str = _TV_DEBUG_PORT
    timeout: float = 15.0  # seconds per CLI call
    _node_path: str = field(default="node", init=False)

    async def _run(self, *args: str) -> dict[str, Any]:
        """Run a TV CLI command and return parsed JSON output."""
        cmd = [self._node_path, self.cli_script, *args]
        env = {**os.environ, "TV_DEBUG_PORT": self.debug_port}

        logger.debug("TV CLI: {}", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.error("TV CLI timeout after {}s: {}", self.timeout, args)
            return {"success": False, "error": "timeout"}
        except FileNotFoundError:
            logger.error("Node.js not found. Is it installed?")
            return {"success": False, "error": "node not found"}

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip()
            logger.error("TV CLI error (rc={}): {}", proc.returncode, err_text)
            return {"success": False, "error": err_text}

        raw = stdout.decode(errors="replace").strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("TV CLI non-JSON output: {}", raw[:200])
            return {"success": False, "error": "invalid JSON", "raw": raw[:500]}

    # ── High-level API ───────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        """Get chart status: symbol, timeframe, connection info."""
        return await self._run("status")

    async def get_quote(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Get real-time quote for current or specified symbol."""
        args = ["data", "quote"]
        if symbol:
            args.extend(["--symbol", symbol])
        return await self._run(*args)

    async def get_ohlcv(
        self, count: int = 100, summary: bool = False
    ) -> dict[str, Any]:
        """Get OHLCV bar data from the chart."""
        args = ["ohlcv", "--count", str(count)]
        if summary:
            args.append("--summary")
        return await self._run(*args)

    async def get_pine_tables(
        self, study_filter: Optional[str] = None
    ) -> dict[str, Any]:
        """Read Pine Script table data (e.g. Signal Table, MSS info table)."""
        args = ["data", "tables"]
        if study_filter:
            args.extend(["--filter", study_filter])
        return await self._run(*args)

    async def get_pine_labels(
        self,
        study_filter: Optional[str] = None,
        max_labels: int = 200,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script label data (MSS/BOS labels, Sweep labels)."""
        args = ["data", "labels", "--max", str(max_labels)]
        if study_filter:
            args.extend(["--filter", study_filter])
        if verbose:
            args.append("--verbose")
        return await self._run(*args)

    async def get_pine_boxes(
        self,
        study_filter: Optional[str] = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script box data (FVG zones, Order Blocks)."""
        args = ["data", "boxes"]
        if study_filter:
            args.extend(["--filter", study_filter])
        if verbose:
            args.append("--verbose")
        return await self._run(*args)

    async def get_pine_lines(
        self,
        study_filter: Optional[str] = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script line data (Session levels, S/R lines)."""
        args = ["data", "lines"]
        if study_filter:
            args.extend(["--filter", study_filter])
        if verbose:
            args.append("--verbose")
        return await self._run(*args)

    async def get_study_values(self) -> dict[str, Any]:
        """Get indicator values from the data window."""
        return await self._run("data", "values")

    async def screenshot(self, path: Optional[str] = None) -> dict[str, Any]:
        """Capture a chart screenshot."""
        args = ["screenshot"]
        if path:
            args.extend(["--path", path])
        return await self._run(*args)

    async def set_symbol(self, symbol: str) -> dict[str, Any]:
        """Change chart symbol."""
        return await self._run("symbol", symbol)

    async def set_timeframe(self, tf: str) -> dict[str, Any]:
        """Change chart timeframe.

        Normalizes common shorthand (``"15m"`` / ``"1h"`` / ``"1d"``) into the
        format TradingView's ``chart.setResolution`` accepts:

        * minutes → bare number (``"15m"`` → ``"15"``)
        * hours   → minute count  (``"1h"``  → ``"60"``, ``"4h"`` → ``"240"``)
        * days / weeks / months   → uppercase letter (``"1d"`` → ``"D"``)
        * seconds → ``"15s"`` → ``"15S"``

        Without this, TV silently falls back to whatever resolution is cached
        on the chart — including premium-only ones like ``"1R"`` (Range bars).
        """
        return await self._run("timeframe", self._normalize_tf(tf))

    @staticmethod
    def _normalize_tf(tf: str) -> str:
        raw = tf.strip()
        if not raw:
            return raw
        # Already in native TV form (bare digits, or ends in upper-case unit).
        if raw.isdigit() or raw[-1] in "SHDWM":
            return raw
        unit = raw[-1].lower()
        try:
            qty = int(raw[:-1])
        except ValueError:
            return raw
        if unit == "m":
            return str(qty)
        if unit == "h":
            return str(qty * 60)
        if unit in ("s", "d", "w"):
            return f"{qty}{unit.upper()}"
        return raw

    # ── Convenience: fetch all Pine data in parallel ─────────────────────

    async def fetch_all_pine_data(self) -> dict[str, Any]:
        """Fetch tables, labels, boxes, and lines concurrently.

        Returns a dict with keys: tables, labels, boxes, lines, status.
        """
        results = await asyncio.gather(
            self.get_pine_tables(),
            self.get_pine_labels(max_labels=200),
            self.get_pine_boxes(verbose=True),
            self.get_pine_lines(verbose=True),
            self.status(),
            return_exceptions=True,
        )

        def safe(r: Any) -> dict:
            if isinstance(r, Exception):
                return {"success": False, "error": str(r)}
            return r

        return {
            "tables": safe(results[0]),
            "labels": safe(results[1]),
            "boxes": safe(results[2]),
            "lines": safe(results[3]),
            "status": safe(results[4]),
        }


# ── Module-level helpers ─────────────────────────────────────────────────────


def okx_to_tv_symbol(okx_symbol: str) -> str:
    """'BTC-USDT-SWAP' → 'BYBIT:BTCUSDT.P'; 'BTC-USDT' → 'BYBIT:BTCUSDT'.

    Function name preserved (back-compat with existing call sites) — the
    *input* still uses the OKX-style identifier we keep as the internal
    canonical format; the *output* now points TV at the BYBIT chart so
    the bot reads candles from the venue we actually trade on.
    """
    raw = okx_symbol.strip()
    is_perp = raw.endswith("-SWAP")
    base = raw.replace("-SWAP", "").replace("-", "")
    suffix = ".P" if is_perp else ""
    return f"BYBIT:{base}{suffix}"
