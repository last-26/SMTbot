"""Arkham whale-transfer WebSocket listener (Phase D, v2 rewrite).

Subscribes to Arkham's real-time `/ws/v2/transfers` feed, filters by
the configured `usdGte` threshold, and writes per-symbol blackout
windows into the shared `WhaleBlackoutState` registry. Entry-signals
reads that registry via `state.whale_blackout` — fully decoupled from
the listener's lifecycle.

**Why v2 (2026-04-21 migration):** the v1 `/ws/sessions` endpoint
charges 500 credits per session creation (operator-observed on the
Arkham dashboard). Every bot restart and every reconnect after a WS
drop burned 500 credits. With a 10,000-credit trial budget, that
capped the bot at ~20 restarts / reconnects per billing period.

v2 flips the model: a stream is a PERSISTENT filter object owned by
the API key. Create once with `POST /ws/v2/streams`, reuse across
unlimited reconnects + bot restarts. Stream creation itself has no
credit fee per docs. The stream_id is persisted to
`data/arkham_stream_id.txt` so subsequent bot runs skip the REST call.

v2 WS protocol:
  1. `POST /ws/v2/streams` with `{"from":["type:cex"],"usdGte":"100000000"}`
     → returns `{streamId, id, createdAt}`. Filter is baked in.
  2. Connect `wss://api.arkm.com/ws/v2/transfers?stream_id=<sid>` with
     `API-Key` header. No subscribe message — transfers start flowing
     as soon as the socket opens.
  3. Server emits `{"type":"transfer","payload":{"transfer":{...}}}`
     per matching transfer.

Failure policy (unchanged from v1):
  * Stream create fails 3x → listener self-disables; gate fails open.
  * WS disconnect → reconnect with exponential backoff.
  * Parser exception → warn + skip that message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets
from loguru import logger

from src.data.on_chain_types import WhaleBlackoutState, affected_symbols_for


def parse_transfer_extras(raw: str) -> dict[str, Optional[str]]:
    """Best-effort extraction of optional transfer metadata for journaling.

    Returns a dict with `from_entity`, `to_entity`, `tx_hash`, all
    possibly-None. Used alongside `parse_transfer_message` when
    persisting whale events to the journal (added 2026-04-22). Never
    raises — a malformed message just yields an all-None dict.
    """
    out: dict[str, Optional[str]] = {
        "from_entity": None,
        "to_entity": None,
        "tx_hash": None,
    }
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return out
    if not isinstance(msg, dict):
        return out
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return out
    transfer = payload.get("transfer")
    if not isinstance(transfer, dict):
        return out

    def _label(obj: Any) -> Optional[str]:
        """Arkham surfaces `{name, arkhamEntity:{name}}`-style dicts for
        entity-labelled addresses. Fall back to the raw address string
        when no label is present."""
        if obj is None:
            return None
        if isinstance(obj, str):
            return obj or None
        if isinstance(obj, dict):
            entity = obj.get("arkhamEntity") or obj.get("entity")
            if isinstance(entity, dict):
                name = entity.get("name") or entity.get("id")
                if name:
                    return str(name)
            if obj.get("name"):
                return str(obj["name"])
            if obj.get("address"):
                return str(obj["address"])
        return None

    out["from_entity"] = _label(
        transfer.get("fromAddress") or transfer.get("from")
    )
    out["to_entity"] = _label(
        transfer.get("toAddress") or transfer.get("to")
    )
    tx_hash = (
        transfer.get("transactionHash")
        or transfer.get("txHash")
        or transfer.get("hash")
    )
    out["tx_hash"] = str(tx_hash) if tx_hash else None
    return out

# Base host for the v2 WS endpoint. The full URL is built with
# `?stream_id=<sid>` at connect time.
ARKHAM_WS_BASE = "wss://api.arkm.com/ws/v2/transfers"

# Default stream_id cache location. Gitignored (via `data/` wildcard
# in .gitignore). Relative to project root.
DEFAULT_STREAM_ID_PATH = Path("data") / "arkham_stream_id.txt"


def build_ws_url(stream_id: str, base: str = ARKHAM_WS_BASE) -> str:
    """Full WebSocket URL carrying the stream id as a query param."""
    return f"{base}?stream_id={stream_id}"


def build_stream_filters(
    tokens: list[str],
    usd_gte: float,
) -> dict:
    """Filter dict passed to `POST /ws/v2/streams`.

    `base=type:cex` anchors the stream on transfers touching CEXes.
    `usdGte` is sent as a STRING per Arkham's spec (verified via
    probe). `tokens` restricts to a subset when non-empty; omitted
    when empty to keep the filter minimal (usdGte alone satisfies
    Arkham's "at least one filter" rule at our 100M threshold).
    """
    filters: dict = {
        "from": ["type:cex"],
        "to": ["type:cex"],
        "usdGte": str(int(usd_gte)),
    }
    if tokens:
        filters["tokens"] = list(tokens)
    return filters


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

    Accepts v1/v2 shared shape:
      {"type":"transfer","payload":{"transfer":{
          "tokenSymbol":"USDT","historicalUSD":503.9,
          "chain":"ethereum","blockTimestamp":"2025-08-28T11:01:35Z"}}}

    Returns None for heartbeats / errors / malformed payloads /
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
    token = (
        transfer.get("tokenSymbol")
        or transfer.get("tokenId")
        or transfer.get("token")
        or transfer.get("asset")
    )
    if not token:
        return None
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


def _read_cached_stream_id(path: Path) -> Optional[str]:
    """Read the last-known stream_id from disk. None on missing / empty
    / read error."""
    try:
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8").strip()
        return content or None
    except Exception:
        return None


def _write_cached_stream_id(path: Path, stream_id: str) -> None:
    """Persist stream_id to disk. Best-effort — a failure to write is a
    one-tick regression (next restart re-creates), not fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stream_id, encoding="utf-8")
    except Exception as e:
        logger.warning("arkham_stream_id_write_failed err={!r}", e)


def _clear_cached_stream_id(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
    # Sidecar filter fingerprint is paired with stream_id — clear together
    # so a stale fingerprint can't survive an id wipe.
    _clear_cached_filter_fingerprint(path)


def _filter_fingerprint_path(stream_id_path: Path) -> Path:
    """Sidecar path for the filter fingerprint, derived from stream_id path.
    `data/arkham_stream_id.txt` → `data/arkham_stream_id.filter.txt`.
    Kept as a separate file so legacy plain-text stream_id consumers stay
    backwards-compatible (no JSON parsing in `_read_cached_stream_id`).
    """
    return stream_id_path.with_suffix(stream_id_path.suffix + ".filter")


def compute_filter_fingerprint(filters: dict) -> str:
    """Stable short hash of a filter dict. Order-insensitive (sorted JSON
    canonicalisation), so reordering keys in `build_stream_filters` won't
    spuriously trigger a stream recreate."""
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _read_cached_filter_fingerprint(stream_id_path: Path) -> Optional[str]:
    """Read the last-known filter fingerprint from the sidecar. None on
    missing / empty / read error → caller treats as fingerprint mismatch
    and forces a fresh stream create. Legacy installations (sidecar
    absent) pay one create on first run after this feature ships."""
    sidecar = _filter_fingerprint_path(stream_id_path)
    try:
        if not sidecar.exists():
            return None
        content = sidecar.read_text(encoding="utf-8").strip()
        return content or None
    except Exception:
        return None


def _write_cached_filter_fingerprint(stream_id_path: Path, fingerprint: str) -> None:
    sidecar = _filter_fingerprint_path(stream_id_path)
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(fingerprint, encoding="utf-8")
    except Exception as e:
        logger.warning("arkham_filter_fingerprint_write_failed err={!r}", e)


def _clear_cached_filter_fingerprint(stream_id_path: Path) -> None:
    sidecar = _filter_fingerprint_path(stream_id_path)
    try:
        if sidecar.exists():
            sidecar.unlink()
    except Exception:
        pass


class ArkhamWebSocketListener:
    """Background task that mirrors Arkham whale transfers into
    `WhaleBlackoutState` (v2 streams API)."""

    def __init__(
        self,
        arkham_client: Any,
        blackout_state: WhaleBlackoutState,
        *,
        usd_gte: float,
        blackout_duration_s: int,
        tokens: Optional[list[str]] = None,
        ws_base: str = ARKHAM_WS_BASE,
        stream_id_path: Path = DEFAULT_STREAM_ID_PATH,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
        max_consecutive_failures: int = 3,
        on_transfer: Optional[Any] = None,
        main_loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._client = arkham_client
        self._state = blackout_state
        self._usd_gte = float(usd_gte)
        self._duration_s = int(blackout_duration_s)
        self._tokens = tokens or []
        self._ws_base = ws_base
        self._stream_id_path = Path(stream_id_path)
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._max_consecutive_failures = max_consecutive_failures
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disabled = False
        self._stream_id: Optional[str] = None
        # 2026-04-22 — optional journal-write callback. Signature:
        #   on_transfer(token: str, usd: float, ts_ms: int,
        #               affected: list[str], extras: dict) -> Awaitable[None]
        # Callback is awaited on the main event loop via
        # `asyncio.run_coroutine_threadsafe` when `main_loop` is provided,
        # otherwise via `asyncio.create_task`. Exceptions are swallowed
        # inside `_handle` so a slow / broken journal write never kills
        # the WS reader. Pass None to disable journaling entirely.
        self._on_transfer = on_transfer
        self._main_loop = main_loop

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def stream_id(self) -> Optional[str]:
        return self._stream_id

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="arkham_whale_ws")

    async def stop(self) -> None:
        self._stop.set()
        # IMPORTANT: v2 streams are PERSISTENT and reused across
        # restarts — we do NOT delete the stream on stop(). Deleting
        # would force a new `POST /ws/v2/streams` on every bot cycle,
        # negating the credit-saving design. Streams are released
        # only via explicit operator action (or if the filter config
        # changes, handled at startup reuse-or-create).
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    async def _obtain_stream_id(self) -> Optional[str]:
        """Return a usable stream_id — reusing the cached one if valid,
        creating a new one otherwise.

        Reuse logic (post 2026-04-22 filter-fingerprint check):
          1. Read cached id + cached filter fingerprint from disk.
          2. Compute current filter fingerprint from `_tokens` + `_usd_gte`.
          3. If fingerprints match AND `GET /ws/v2/streams` shows the
             cached id is still alive → reuse (zero creation fee).
          4. If fingerprint MISMATCH (filter config changed since last
             run) → delete the old stream + clear cache + create fresh
             with the new filter. This is the path that ships when
             operator changes `whale_tokens` or `whale_threshold_usd`.
          5. If cached id is stale (server-side timeout) → drop cache,
             create fresh.

        Filter-fingerprint logic exists because Arkham v2 streams are
        immutable post-create — silently reusing a stream with a
        no-longer-current filter would deliver wrong data and waste
        label-lookup quota on tokens we don't care about.

        Any failure falls through to creation; creation failure is
        escalated to the caller's 3-strike disable loop.
        """
        filters = build_stream_filters(self._tokens, self._usd_gte)
        current_fp = compute_filter_fingerprint(filters)

        cached = _read_cached_stream_id(self._stream_id_path)
        cached_fp = _read_cached_filter_fingerprint(self._stream_id_path)

        if cached and cached_fp == current_fp:
            # Fingerprint match — verify stream is still alive on Arkham side.
            try:
                streams = await self._client.list_ws_streams()
            except Exception:
                streams = None
            if isinstance(streams, list):
                ids = {
                    str(s.get("streamId"))
                    for s in streams if isinstance(s, dict)
                }
                if cached in ids:
                    logger.info(
                        "arkham_ws_stream_reused stream_id={} fp={} "
                        "(no creation fee)",
                        cached, current_fp,
                    )
                    return cached
                logger.info(
                    "arkham_ws_stream_cache_stale cached={} fp={} — "
                    "creating new",
                    cached, current_fp,
                )
                _clear_cached_stream_id(self._stream_id_path)
        elif cached:
            # Filter changed since last run — old stream delivers wrong
            # data and burns labels on filtered-out tokens. Delete + recreate.
            logger.info(
                "arkham_ws_stream_filter_changed cached_sid={} cached_fp={} "
                "new_fp={} — deleting old stream + recreating",
                cached, cached_fp, current_fp,
            )
            try:
                await self._client.delete_ws_stream(cached)
            except Exception as e:
                # Non-fatal: Arkham reclaims orphaned streams via timeout.
                logger.warning(
                    "arkham_ws_stream_delete_failed err={!r} — "
                    "Arkham timeout will reclaim", e,
                )
            _clear_cached_stream_id(self._stream_id_path)

        # Create fresh.
        result = None
        try:
            result = await self._client.create_ws_stream(filters)
        except Exception as e:
            logger.warning("arkham_ws_stream_create_failed err={!r}", e)
            return None
        if not isinstance(result, dict):
            return None
        sid = result.get("streamId") or result.get("stream_id")
        if not sid:
            return None
        sid = str(sid)
        _write_cached_stream_id(self._stream_id_path, sid)
        _write_cached_filter_fingerprint(self._stream_id_path, current_fp)
        logger.info(
            "arkham_ws_stream_created stream_id={} fp={} filters={}",
            sid, current_fp, filters,
        )
        return sid

    async def _run(self) -> None:
        backoff = self._reconnect_min_s
        consecutive_failures = 0
        while not self._stop.is_set():
            if self._disabled:
                return

            sid = await self._obtain_stream_id()
            if sid is None:
                consecutive_failures += 1
                logger.warning(
                    "arkham_ws_stream_obtain_failed attempt={} backoff={}s",
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
            self._stream_id = sid

            try:
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
                        "arkham_whale_ws_connected url={} stream_id={} "
                        "usd_gte={:.0f}",
                        ws_url, sid, self._usd_gte,
                    )
                    # v2 streams carry the filter from creation — no
                    # subscribe message required.
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
        """Route one WS message into the blackout registry + optional journal."""
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

        # 2026-04-22 — journal raw transfer (post hard-gate-removal).
        # Runtime entry is no longer gated; GBT learns direction from the
        # `whale_transfers` table at Phase 9. Fire-and-forget so WS reader
        # never blocks on DB latency.
        if self._on_transfer is not None:
            try:
                extras = parse_transfer_extras(raw)
                coro = self._on_transfer(
                    token=token,
                    usd_value=usd_value,
                    ts_ms=ts_ms,
                    affected_symbols=list(symbols),
                    extras=extras,
                )
                if self._main_loop is not None:
                    asyncio.run_coroutine_threadsafe(coro, self._main_loop)
                else:
                    asyncio.create_task(coro)
            except Exception as e:
                logger.warning(
                    "arkham_transfer_journal_dispatch_failed err={!r}", e,
                )
