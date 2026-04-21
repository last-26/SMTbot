"""Arkham whale-transfer WebSocket listener (Phase D).

Subscribes to Arkham's real-time `/ws/transfers` feed (API v1.1),
filters by the configured `usdGte` threshold, and writes per-symbol
blackout windows into the shared `WhaleBlackoutState` registry. The
entry_signals gate reads that registry via `state.whale_blackout` —
fully decoupled from the listener's own lifecycle. A dropped
connection, a 429, or an unparseable message must never crash the
bot; reconnect with exponential backoff up to `reconnect_max_s`.

Arkham v1 WS protocol (per https://intel.arkm.com/api/docs):
  1. `POST /ws/sessions` with `{}` → returns `session_id` (via
     ArkhamClient.create_ws_session).
  2. Connect `wss://api.arkm.com/ws/transfers?session_id=<sid>` with
     `API-Key` header.
  3. Send `{"id":"1","type":"subscribe","payload":{"filters":{...}}}`.
  4. Server emits `{"type":"transfer","payload":{"transfer":{...}}}`
     per matching transfer. Also `ack` / `error` types.

Failure policy (mirrors `src.data.liquidation_stream.LiquidationStream`):
  * Missing session token → log + disable listener (3-strike).
  * WS disconnect → exponential backoff reconnect.
  * Parser exception → warn + skip that message.
  * `stop()` unblocks the reconnect-wait and tears down the socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any, Optional

import websockets
from loguru import logger

from src.data.on_chain_types import WhaleBlackoutState, affected_symbols_for

# Base host for the WS endpoint. The full URL is built with
# `?session_id=<sid>` per the v1 API.
ARKHAM_WS_BASE = "wss://api.arkm.com/ws/transfers"


def build_ws_url(session_id: str, base: str = ARKHAM_WS_BASE) -> str:
    """Full WebSocket URL carrying the session id as a query param.

    Extracted so tests can assert the shape without opening a socket.
    """
    return f"{base}?session_id={session_id}"


def build_subscribe_message(
    tokens: list[str],
    usd_gte: float,
    *,
    message_id: str = "1",
) -> str:
    """Serialise the subscribe envelope for Arkham's whale stream (v1).

    Format per the public docs:
      {"id": "<id>", "type": "subscribe",
       "payload": {"filters": {"usdGte": <int>, "tokens": [...]}}}

    `usdGte` must be an integer per Arkham (docs example shows `10000`
    bare, not a string). The `tokens` filter narrows the stream to the
    configured watchlist; `usdGte` must individually be ≥ 250k when
    no other filter is set, but we always pair with `tokens`.
    """
    payload = {
        "id": str(message_id),
        "type": "subscribe",
        "payload": {
            "filters": {
                "tokens": list(tokens),
                "usdGte": int(usd_gte),
            },
        },
    }
    return json.dumps(payload)


def _parse_iso_to_ms(value: Any) -> Optional[int]:
    """Best-effort ISO-8601 → epoch ms. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def parse_transfer_message(
    raw: str,
    threshold_usd: float,
) -> Optional[tuple[str, float, int]]:
    """Parse one WS message into (token_id, usd_value, timestamp_ms).

    Expected shape (Arkham v1):
      {"type":"transfer","payload":{"transfer":{
          "tokenSymbol":"USDT","historicalUSD":503.9,
          "chain":"ethereum","blockTimestamp":"2025-08-28T11:01:35Z", ...}}}

    Returns None for heartbeats / acks / errors / malformed payloads /
    under-threshold events. Callers consult `affected_symbols_for()`
    for the blast radius.
    """
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "transfer":
        return None
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return None
    transfer = payload.get("transfer")
    if not isinstance(transfer, dict):
        return None
    # Token identifier — prefer tokenSymbol, fall back to tokenId / asset.
    token = (
        transfer.get("tokenSymbol")
        or transfer.get("tokenId")
        or transfer.get("token")
        or transfer.get("asset")
    )
    if not token:
        return None
    # USD value — prefer `historicalUSD` per v1 docs.
    raw_usd = (
        transfer.get("historicalUSD")
        or transfer.get("usdValue")
        or transfer.get("usd_value")
    )
    try:
        usd_value = float(raw_usd or 0)
    except (TypeError, ValueError):
        return None
    if usd_value < float(threshold_usd):
        return None
    # Timestamp — ISO string preferred, falls back to ms integer if
    # newer API variant, else `time.time()`.
    ts_ms = _parse_iso_to_ms(transfer.get("blockTimestamp"))
    if ts_ms is None:
        ts_ms = _parse_iso_to_ms(transfer.get("timestamp"))
    if ts_ms is None:
        raw_ts = transfer.get("ts_ms")
        try:
            ts_ms = int(raw_ts) if raw_ts is not None else None
        except (TypeError, ValueError):
            ts_ms = None
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return (str(token), usd_value, ts_ms)


class ArkhamWebSocketListener:
    """Background task that mirrors Arkham whale transfers into
    `WhaleBlackoutState`."""

    def __init__(
        self,
        arkham_client: Any,
        blackout_state: WhaleBlackoutState,
        *,
        usd_gte: float,
        blackout_duration_s: int,
        tokens: Optional[list[str]] = None,
        ws_base: str = ARKHAM_WS_BASE,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
        max_consecutive_failures: int = 3,
    ):
        self._client = arkham_client
        self._state = blackout_state
        self._usd_gte = float(usd_gte)
        self._duration_s = int(blackout_duration_s)
        # Token filter — NONE means only usdGte applies (bandwidth +
        # credit cost slightly higher but we keep the filter minimal
        # until the operator confirms Arkham's accepted token id format
        # for the `tokens` filter field). Our usd_gte default (100M)
        # is well above Arkham's 250k minimum so the subscribe is legal
        # without a tokens filter.
        self._tokens = tokens or []
        self._ws_base = ws_base
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._max_consecutive_failures = max_consecutive_failures
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disabled = False
        self._session_id: Optional[str] = None

    @property
    def disabled(self) -> bool:
        return self._disabled

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="arkham_whale_ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._session_id is not None and self._client is not None:
            try:
                await self._client.delete_ws_session(self._session_id)
            except Exception:
                pass
            self._session_id = None
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
        consecutive_failures = 0
        while not self._stop.is_set():
            if self._disabled:
                return
            # Fresh session token per reconnect — short-lived by Arkham's
            # design, avoids stale-token replay on long outages.
            try:
                sid = await self._client.create_ws_session()
            except Exception:
                sid = None
            if sid is None:
                consecutive_failures += 1
                logger.warning(
                    "arkham_ws_session_create_failed attempt={} backoff={}s",
                    consecutive_failures, backoff,
                )
                if consecutive_failures >= self._max_consecutive_failures:
                    logger.error(
                        "arkham_ws_disabled consecutive_failures={} — "
                        "whale blackout gate will run without WS updates",
                        consecutive_failures,
                    )
                    self._disabled = True
                    return
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._reconnect_max_s)
                continue
            self._session_id = sid

            try:
                # Per v1 API: API-Key rides as a header on the initial
                # handshake; session id goes in the URL query string.
                api_key = getattr(self._client, "api_key", None) or ""
                headers = {"API-Key": api_key} if api_key else {}
                ws_url = build_ws_url(sid, base=self._ws_base)
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=60,
                    ping_timeout=30,
                ) as ws:
                    logger.info(
                        "arkham_whale_ws_connected url={} session={} "
                        "usd_gte={:.0f}",
                        ws_url, sid, self._usd_gte,
                    )
                    # Subscribe message carries filters; session is in
                    # the URL, not the payload.
                    await ws.send(build_subscribe_message(
                        self._tokens, self._usd_gte,
                    ))
                    backoff = self._reconnect_min_s
                    consecutive_failures = 0
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._handle(raw)
                        except Exception as e:
                            logger.warning(
                                "arkham_whale_ws_parse_failed err={!r}", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    "arkham_whale_ws_disconnected err={!r} backoff={}s "
                    "consecutive_failures={}",
                    e, backoff, consecutive_failures,
                )
                if consecutive_failures >= self._max_consecutive_failures:
                    logger.error(
                        "arkham_ws_disabled consecutive_failures={} — "
                        "whale blackout gate will run without WS updates",
                        consecutive_failures,
                    )
                    self._disabled = True
                    return
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self._reconnect_max_s)

    def _handle(self, raw: str) -> None:
        """Route one WS message into the blackout registry.

        Ignores heartbeats / acks / under-threshold events silently
        (parse_transfer_message returns None). Applies the blackout to
        every symbol in `affected_symbols_for(token)` — stablecoin
        events fan out, chain-native events collapse to one symbol.
        """
        parsed = parse_transfer_message(raw, self._usd_gte)
        if parsed is None:
            return
        token, usd_value, ts_ms = parsed
        symbols = affected_symbols_for(token)
        if not symbols:
            return
        until_ms = ts_ms + self._duration_s * 1000
        for sym in symbols:
            self._state.set_blackout(sym, until_ms)
        logger.info(
            "arkham_whale_blackout_set token={} usd={:.0f} "
            "symbols={} until_ms={}",
            token, usd_value, list(symbols), until_ms,
        )
