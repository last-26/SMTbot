"""Binance Futures liquidation stream listener.

Subscribes to the public `!forceOrder@arr` WebSocket and keeps a rolling
in-memory buffer (per OKX symbol) of `LiquidationEvent`s. Optionally hands
events off to a `DerivativesJournal` for persistence (wired in Madde 3).

Failure policy:
  * WS disconnect → exponential backoff reconnect, never crash the bot.
  * Parser exception → warn + skip that message.
  * Binance 2025 throttle: only the *largest* liquidation in each 1000ms
    window is emitted, so aggregated totals under-report reality — Madde 2
    (Coinalyze) backfills aggregated liquidations as a complement.

Not a pydantic model: the hot path parses ~10s of messages/sec and we want
a cheap frozen dataclass, not full validation.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import websockets
from loguru import logger

BINANCE_FORCE_ORDER_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"


def internal_to_binance_symbol(internal_symbol: str) -> str:
    """'BTC-USDT-SWAP' → 'BTCUSDT'.

    Maps the internal canonical perp identifier to the Binance USDT-M
    perpetual ticker used on the forceOrder liquidation stream.
    """
    return internal_symbol.replace("-SWAP", "").replace("-", "")


def binance_to_internal_symbol(binance_symbol: str) -> Optional[str]:
    """'BTCUSDT' → 'BTC-USDT-SWAP'. Non-USDT perps return None (we ignore them)."""
    if binance_symbol.endswith("USDT"):
        base = binance_symbol[:-4]
        if not base:
            return None
        return f"{base}-USDT-SWAP"
    return None


@dataclass(frozen=True)
class LiquidationEvent:
    symbol: str           # internal canonical form, e.g. 'BTC-USDT-SWAP'
    side: str             # 'LONG_LIQ' (long liquidated) | 'SHORT_LIQ'
    price: float
    quantity: float       # base asset
    notional_usd: float   # price * quantity
    ts_ms: int            # trade timestamp (ms)


class LiquidationStream:
    """Background task that mirrors Binance `forceOrder` into per-symbol buffers."""

    def __init__(
        self,
        watched_symbols: list[str],
        buffer_size_per_symbol: int = 5000,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
    ):
        self.watched = set(watched_symbols)
        self.buffers: dict[str, deque[LiquidationEvent]] = {
            s: deque(maxlen=buffer_size_per_symbol) for s in self.watched
        }
        self._journal = None       # attach_journal() in Madde 3
        self._stop = asyncio.Event()
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._task: Optional[asyncio.Task] = None

    def attach_journal(self, journal) -> None:
        """Inject DerivativesJournal. Called after the journal's schema is ready."""
        self._journal = journal

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="liq_stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = self._reconnect_min_s
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    BINANCE_FORCE_ORDER_URL,
                    ping_interval=180,
                    ping_timeout=60,
                ) as ws:
                    logger.info("liquidation_stream_connected url={}",
                                BINANCE_FORCE_ORDER_URL)
                    backoff = self._reconnect_min_s
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._handle(raw)
                        except Exception as e:
                            logger.warning("liq_parse_failed err={!r}", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("liq_ws_disconnected err={!r} backoff={}s",
                               e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._reconnect_max_s)

    def _handle(self, raw: str) -> None:
        msg = json.loads(raw)
        o = msg.get("o") or {}
        binance_sym = o.get("s", "")
        internal_sym = binance_to_internal_symbol(binance_sym)
        if internal_sym is None or internal_sym not in self.watched:
            return

        side_raw = o.get("S")
        if side_raw not in ("BUY", "SELL"):
            return
        side = "LONG_LIQ" if side_raw == "SELL" else "SHORT_LIQ"

        try:
            price = float(o.get("ap") or o.get("p") or 0)
            qty = float(o.get("q") or 0)
        except (TypeError, ValueError):
            return
        if price <= 0 or qty <= 0:
            return

        ts_ms = int(o.get("T") or msg.get("E") or time.time() * 1000)
        ev = LiquidationEvent(
            symbol=internal_sym,
            side=side,
            price=price,
            quantity=qty,
            notional_usd=price * qty,
            ts_ms=ts_ms,
        )
        self.buffers[internal_sym].append(ev)
        if self._journal is not None:
            try:
                asyncio.create_task(self._journal.insert_liquidation(ev))
            except RuntimeError:
                pass

    # ── Query API ──────────────────────────────────────────────────────────

    def recent(self, symbol: str, lookback_ms: int) -> list[LiquidationEvent]:
        buf = self.buffers.get(symbol)
        if not buf:
            return []
        cutoff = int(time.time() * 1000) - lookback_ms
        return [e for e in buf if e.ts_ms >= cutoff]

    def stats(self, symbol: str, lookback_ms: int) -> dict:
        """Summary stats over the lookback window — feeds the regime classifier."""
        events = self.recent(symbol, lookback_ms)
        long_liqs = [e for e in events if e.side == "LONG_LIQ"]
        short_liqs = [e for e in events if e.side == "SHORT_LIQ"]
        return {
            "long_liq_notional": sum(e.notional_usd for e in long_liqs),
            "short_liq_notional": sum(e.notional_usd for e in short_liqs),
            "long_liq_count": len(long_liqs),
            "short_liq_count": len(short_liqs),
            "max_liq_notional": max((e.notional_usd for e in events), default=0.0),
        }
