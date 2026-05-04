"""TradingView MCP bridge — calls the TV CLI and returns parsed JSON.

The TV CLI (`node <tradingview-mcp>/src/cli/index.js`) communicates with
TradingView Desktop via CDP (Chrome DevTools Protocol) on port 9222.

This module wraps the CLI calls so the Python bot can read Pine Script
drawing objects (tables, labels, boxes, lines) and chart state.

Two execution modes (operatör 2026-05-04 kalıcı çözüm):

  * **persistent** (default) — long-lived daemon process started once at
    construction. Each public-API call sends a single JSON request over
    stdin, daemon dispatches to the same handler the legacy CLI would
    invoke, response comes back over stdout. Subprocess startup + V8 JIT
    + CDP setup paid ONCE; per-call latency drops from 150-350ms (legacy)
    to ~50-150ms (daemon). Cycle latency drops from 130-150s to 15-25s.

  * **legacy** — every call spawns a fresh `node` subprocess (the original
    behaviour). Kept for tests and as a fallback if the daemon protocol
    misbehaves. Enable with env var ``BOT_TV_BRIDGE_MODE=legacy``.

The daemon mode is process-local: each ``TVBridge`` instance owns one
daemon. On crash (daemon exits, stdout closed, etc.) the next call lazily
respawns. There is no client-side request retry — callers see the failure
in the response dict (``{"success": False, "error": ...}``) and decide.
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

# Operator-controlled bridge mode. Default "persistent" (daemon); set to
# "legacy" to force one-shot subprocess per call (slower but simpler).
_BRIDGE_MODE = os.getenv("BOT_TV_BRIDGE_MODE", "persistent").lower()


@dataclass
class TVBridge:
    """Async wrapper around the TradingView MCP CLI.

    Usage::

        bridge = TVBridge()
        status = await bridge.status()
        tables = await bridge.get_pine_tables()

    The daemon is spawned lazily on first call and reused thereafter.
    Caller does not need to ``await bridge.start()`` explicitly.
    """

    cli_script: str = _TV_CLI_SCRIPT
    debug_port: str = _TV_DEBUG_PORT
    timeout: float = 15.0  # seconds per CLI call

    _node_path: str = field(default="node", init=False)
    _mode: str = field(default=_BRIDGE_MODE, init=False)

    # Daemon state (only used when _mode == "persistent")
    _daemon_proc: Optional[asyncio.subprocess.Process] = field(default=None, init=False)
    _daemon_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _daemon_pending: dict[str, asyncio.Future] = field(default_factory=dict, init=False)
    _daemon_reader_task: Optional[asyncio.Task] = field(default=None, init=False)
    _daemon_seq: int = field(default=0, init=False)

    # ── Public API ──────────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        """Get chart status: symbol, timeframe, connection info."""
        return await self._call("status")

    async def get_quote(self, symbol: Optional[str] = None) -> dict[str, Any]:
        """Get real-time quote for current or specified symbol."""
        kwargs = {"symbol": symbol} if symbol else {}
        return await self._call("data quote", kwargs)

    async def get_ohlcv(
        self, count: int = 100, summary: bool = False,
    ) -> dict[str, Any]:
        """Get OHLCV bar data from the chart."""
        kwargs: dict[str, Any] = {"count": count}
        if summary:
            kwargs["summary"] = True
        return await self._call("data ohlcv", kwargs)

    async def get_pine_tables(
        self, study_filter: Optional[str] = None,
    ) -> dict[str, Any]:
        """Read Pine Script table data (e.g. Signal Table, MSS info table)."""
        kwargs = {"filter": study_filter} if study_filter else {}
        return await self._call("data tables", kwargs)

    async def get_pine_labels(
        self,
        study_filter: Optional[str] = None,
        max_labels: int = 200,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script label data (MSS/BOS labels, Sweep labels)."""
        kwargs: dict[str, Any] = {"max": max_labels}
        if study_filter:
            kwargs["filter"] = study_filter
        if verbose:
            kwargs["verbose"] = True
        return await self._call("data labels", kwargs)

    async def get_pine_boxes(
        self,
        study_filter: Optional[str] = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script box data (FVG zones, Order Blocks)."""
        kwargs: dict[str, Any] = {}
        if study_filter:
            kwargs["filter"] = study_filter
        if verbose:
            kwargs["verbose"] = True
        return await self._call("data boxes", kwargs)

    async def get_pine_lines(
        self,
        study_filter: Optional[str] = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Read Pine Script line data (Session levels, S/R lines)."""
        kwargs: dict[str, Any] = {}
        if study_filter:
            kwargs["filter"] = study_filter
        if verbose:
            kwargs["verbose"] = True
        return await self._call("data lines", kwargs)

    async def get_study_values(self) -> dict[str, Any]:
        """Get indicator values from the data window."""
        return await self._call("data values")

    async def screenshot(self, path: Optional[str] = None) -> dict[str, Any]:
        """Capture a chart screenshot."""
        kwargs = {"path": path} if path else {}
        return await self._call("screenshot", kwargs)

    async def set_symbol(self, symbol: str) -> dict[str, Any]:
        """Change chart symbol."""
        return await self._call("symbol", positionals=[symbol])

    async def set_timeframe(self, tf: str) -> dict[str, Any]:
        """Change chart timeframe (TV resolution format)."""
        return await self._call("timeframe", positionals=[self._normalize_tf(tf)])

    @staticmethod
    def _normalize_tf(tf: str) -> str:
        raw = tf.strip()
        if not raw:
            return raw
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

    async def fetch_all_pine_data(self) -> dict[str, Any]:
        """Fetch tables, labels, boxes, lines, status concurrently."""
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

    async def shutdown(self) -> None:
        """Cleanly stop the daemon (no-op in legacy mode)."""
        if self._mode != "persistent":
            return
        proc = self._daemon_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            await self._daemon_send_raw(
                json.dumps({"id": None, "cmd": "shutdown"}) + "\n"
            )
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            try:
                proc.kill()
            except Exception:
                pass
        self._daemon_proc = None
        if self._daemon_reader_task:
            self._daemon_reader_task.cancel()
            self._daemon_reader_task = None

    # ── Mode dispatcher ─────────────────────────────────────────────────

    async def _call(
        self,
        cmd: str,
        kwargs: Optional[dict[str, Any]] = None,
        positionals: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Mode-aware dispatch: persistent daemon JSON-RPC or legacy subprocess."""
        kwargs = kwargs or {}
        positionals = positionals or []
        if self._mode == "persistent":
            return await self._send_persistent(cmd, kwargs, positionals)
        return await self._run_legacy(cmd, kwargs, positionals)

    # ── Persistent daemon mode ──────────────────────────────────────────

    async def _ensure_daemon(self) -> bool:
        """Start daemon if not already running. Returns True on success."""
        proc = self._daemon_proc
        if proc is not None and proc.returncode is None:
            return True

        env = {**os.environ, "TV_DEBUG_PORT": self.debug_port}
        try:
            self._daemon_proc = await asyncio.create_subprocess_exec(
                self._node_path, self.cli_script, "server",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            logger.error("tv_bridge: node not found at PATH")
            return False
        except Exception as exc:
            logger.exception("tv_bridge: daemon spawn failed: {}", exc)
            return False

        # Wait for "tv-server: ready" on stderr (signals CDP loaded)
        try:
            await asyncio.wait_for(
                self._daemon_wait_ready(), timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error("tv_bridge: daemon ready signal timeout (10s)")
            try:
                self._daemon_proc.kill()
            except Exception:
                pass
            self._daemon_proc = None
            return False

        # Start background reader
        self._daemon_reader_task = asyncio.create_task(self._daemon_read_loop())
        logger.debug("tv_bridge: daemon ready (pid={})", self._daemon_proc.pid)
        return True

    async def _daemon_wait_ready(self) -> None:
        """Block until daemon writes 'tv-server: ready' to stderr."""
        assert self._daemon_proc is not None
        stderr = self._daemon_proc.stderr
        assert stderr is not None
        while True:
            line = await stderr.readline()
            if not line:
                raise RuntimeError("daemon stderr closed before ready")
            text = line.decode(errors="replace").strip()
            if text:
                logger.debug("tv_bridge daemon stderr: {}", text)
            if "ready" in text:
                return

    async def _daemon_read_loop(self) -> None:
        """Continuously read JSON responses from daemon stdout, dispatch to futures."""
        proc = self._daemon_proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break  # daemon exited / stdout closed
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "tv_bridge daemon non-JSON output: {}", text[:200],
                    )
                    continue
                req_id = msg.get("id")
                future = self._daemon_pending.pop(req_id, None)
                if future is None or future.done():
                    continue
                if "error" in msg:
                    future.set_result({
                        "success": False, "error": msg["error"],
                    })
                else:
                    future.set_result(msg.get("result") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tv_bridge daemon reader exception: {}", exc)
        finally:
            # Daemon dead: fail all pending futures so callers get a response.
            for fid, fut in list(self._daemon_pending.items()):
                if not fut.done():
                    fut.set_result({
                        "success": False, "error": "daemon_exited",
                    })
            self._daemon_pending.clear()

    async def _daemon_send_raw(self, line: str) -> None:
        """Write a line to daemon stdin (must end with newline)."""
        proc = self._daemon_proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("daemon not running")
        proc.stdin.write(line.encode())
        await proc.stdin.drain()

    async def _send_persistent(
        self,
        cmd: str,
        kwargs: dict[str, Any],
        positionals: list[str],
    ) -> dict[str, Any]:
        """Send a single JSON request to the daemon and await its response."""
        ok = await self._ensure_daemon()
        if not ok:
            return {"success": False, "error": "daemon_unavailable"}

        async with self._daemon_lock:
            self._daemon_seq += 1
            req_id = f"r{self._daemon_seq}"
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._daemon_pending[req_id] = future
            req = {
                "id": req_id,
                "cmd": cmd,
                "args": kwargs,
                "positionals": positionals,
            }
            try:
                await self._daemon_send_raw(json.dumps(req) + "\n")
            except Exception as exc:
                self._daemon_pending.pop(req_id, None)
                logger.warning("tv_bridge daemon write failed: {}", exc)
                return {"success": False, "error": f"daemon_write: {exc}"}

        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._daemon_pending.pop(req_id, None)
            logger.warning(
                "tv_bridge daemon timeout cmd={} after {}s", cmd, self.timeout,
            )
            return {"success": False, "error": "daemon_timeout"}

    # ── Legacy one-shot subprocess mode ─────────────────────────────────

    async def _run_legacy(
        self,
        cmd: str,
        kwargs: dict[str, Any],
        positionals: list[str],
    ) -> dict[str, Any]:
        """Legacy: spawn a fresh node subprocess for this single command."""
        cli_args = cmd.split()
        cli_args.extend(positionals)
        for k, v in kwargs.items():
            if v is True:
                cli_args.append(f"--{k}")
            elif v is False or v is None:
                continue
            else:
                cli_args.extend([f"--{k}", str(v)])

        full_cmd = [self._node_path, self.cli_script, *cli_args]
        env = {**os.environ, "TV_DEBUG_PORT": self.debug_port}
        logger.debug("TV CLI: {}", " ".join(full_cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.error("TV CLI timeout after {}s: {}", self.timeout, cli_args)
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


# ── Module-level helpers ─────────────────────────────────────────────────────


def internal_to_tv_symbol(internal_symbol: str) -> str:
    """'BTC-USDT-SWAP' → 'BYBIT:BTCUSDT.P'; 'BTC-USDT' → 'BYBIT:BTCUSDT'.

    Translate the internal canonical symbol format (kept across the
    runner / config / journal) to the TradingView ticker for the venue
    the bot actually trades on. The internal format originated with the
    pre-migration execution layer and is preserved as canonical to avoid
    a mass rename of journal rows + config keys.
    """
    raw = internal_symbol.strip()
    is_perp = raw.endswith("-SWAP")
    base = raw.replace("-SWAP", "").replace("-", "")
    suffix = ".P" if is_perp else ""
    return f"BYBIT:{base}{suffix}"
