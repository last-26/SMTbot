"""Arkham whale-transfer WebSocket listener (Phase D).

Subscribes to Arkham's real-time transfer feed (large on-chain
movements), filters by the configured `usdGte` threshold, and writes
per-symbol blackout windows into the shared `WhaleBlackoutState`
registry. The entry_signals gate reads that registry via
`state.whale_blackout` — fully decoupled from the listener's own
lifecycle. A dropped connection, a 429, or an unparseable message
must never crash the bot; reconnect with exponential backoff up to
`reconnect_max_s`.

Failure policy (mirrors `src.data.liquidation_stream.LiquidationStream`):
  * Missing session token → log + disable listener (one-shot per start).
  * WS disconnect → exponential backoff reconnect.
  * Parser exception → warn + skip that message.
  * `stop()` unblocks the reconnect-wait and tears down the socket.

The full Arkham WS protocol is out of public-documentation scope; this
module encodes the shape the operator-written integration plan
specifies. If Arkham ships a different subscribe envelope later, the
change lives in `_subscribe_message` — the rest of the listener is
protocol-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import websockets
from loguru import logger

from src.data.on_chain_types import WhaleBlackoutState, affected_symbols_for

# Arkham's whale-transfer stream endpoint. The session token (obtained
# via `ArkhamClient.create_ws_session`) rides in the URL or in an auth
# message after connect; current plan spec embeds it in the subscribe
# payload as `sessionId`.
ARKHAM_WS_URL = "wss://ws.arkm.com/intel/transfers"


def build_subscribe_message(
    session_id: str,
    tokens: list[str],
    usd_gte: float,
) -> str:
    """Serialise the subscribe envelope for Arkham's whale stream.

    Extracted as a pure function so tests can assert the shape without
    a live WebSocket connection.
    """
    payload = {
        "op": "subscribe",
        "sessionId": session_id,
        "filter": {
            "tokens": list(tokens),
            "usdGte": float(usd_gte),
        },
    }
    return json.dumps(payload)


def parse_transfer_message(
    raw: str,
    threshold_usd: float,
) -> Optional[tuple[str, float, int]]:
    """Parse a raw WS message into (token_id, usd_value, timestamp_ms).

    Returns None when the message is a heartbeat / subscription ack /
    malformed payload / under-threshold event. Callers should log-only
    (never crash) and consult `affected_symbols_for(token_id)` to get
    the blast radius.
    """
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") and msg.get("type") != "transfer":
        return None
    data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
    token = data.get("token") or data.get("tokenId") or data.get("asset")
    if not token:
        return None
    try:
        usd_value = float(data.get("usdValue") or data.get("usd_value") or 0)
    except (TypeError, ValueError):
        return None
    if usd_value < float(threshold_usd):
        return None
    try:
        ts_ms = int(data.get("timestamp") or data.get("ts_ms")
                    or (time.time() * 1000))
    except (TypeError, ValueError):
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
        ws_url: str = ARKHAM_WS_URL,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
        max_consecutive_failures: int = 3,
    ):
        self._client = arkham_client
        self._state = blackout_state
        self._usd_gte = float(usd_gte)
        self._duration_s = int(blackout_duration_s)
        self._tokens = tokens or [
            "bitcoin", "ethereum", "tether", "usd-coin",
            "solana", "dogecoin", "binancecoin",
        ]
        self._ws_url = ws_url
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
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=60,
                    ping_timeout=30,
                ) as ws:
                    logger.info(
                        "arkham_whale_ws_connected url={} session={} "
                        "usd_gte={:.0f}",
                        self._ws_url, sid, self._usd_gte,
                    )
                    await ws.send(build_subscribe_message(
                        sid, self._tokens, self._usd_gte,
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
