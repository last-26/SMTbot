"""Arkham Intel REST client — on-chain flow enrichment.

Rate limit: per-label (daily datapoints), quota reported in every
response via `X-Intel-Datapoints-*` headers. We track usage in memory
and auto-disable once the reported usage fraction crosses
`auto_disable_pct` — prevents the trial key from being rate-limited or
the paid plan from over-spending its datapoint budget.

Failure policy (mirrors `src.data.derivatives_api.CoinalyzeClient`):
  * 401 / 403   → log + return None once; key is bad, no retry loop.
  * 429         → honor `Retry-After` header, then continue.
  * Any other   → warn + exponential retry up to `max_retries`.
  * Missing key → warn at construction, all fetches silently return None
                  so the rest of the bot keeps running.

None of the public methods raise; callers see `None` on any failure and
degrade to the pure-price strategy (same contract as Coinalyze).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import httpx
from loguru import logger

ARKHAM_BASE = "https://api.arkm.com"


class ArkhamClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = ARKHAM_BASE,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        auto_disable_pct: float = 95.0,
    ):
        self.api_key = api_key or os.getenv("ARKHAM_API_KEY")
        if not self.api_key:
            logger.warning(
                "arkham_api_key_missing; on_chain pipeline will "
                "return None snapshots"
            )
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={"API-Key": self.api_key or ""},
        )
        self._max_retries = max_retries
        self._auto_disable_pct = float(auto_disable_pct)
        # 429 backoff — mirrors Coinalyze. Monotonic deadline so
        # subsequent calls short-circuit instead of awaiting inside the
        # shared request path and blocking the event loop for every
        # other coroutine (snapshot refresh, WS listener, per-symbol
        # cycles). Populated when Arkham returns Retry-After.
        self._rate_pause_until: float = 0.0
        # Label-usage auto-disable. When the reported datapoints
        # usage fraction crosses `_auto_disable_pct`, every subsequent
        # call short-circuits to None so the remaining budget is
        # preserved for operator-directed diagnostics rather than
        # burnt on bot telemetry. One-shot; operator clears via
        # process restart (or by bumping the pct threshold).
        self._hard_disabled: bool = False
        self._last_usage_snapshot: dict[str, float] = {}

    # ── Usage accounting ───────────────────────────────────────────────────

    @property
    def hard_disabled(self) -> bool:
        return self._hard_disabled

    @property
    def last_usage_snapshot(self) -> dict[str, float]:
        """Most recent datapoints header snapshot (read-only copy)."""
        return dict(self._last_usage_snapshot)

    def _absorb_usage_headers(self, resp: httpx.Response) -> None:
        """Parse `X-Intel-Datapoints-*` headers into the in-memory
        snapshot. Arkham reports usage on every response; keeping the
        latest values lets the operator track spend without a separate
        `/subscription/intel-usage` poll.
        """
        try:
            usage = float(resp.headers.get("X-Intel-Datapoints-Usage", "0") or 0)
            limit = float(resp.headers.get("X-Intel-Datapoints-Limit", "0") or 0)
            remaining = float(
                resp.headers.get("X-Intel-Datapoints-Remaining", "0") or 0
            )
        except ValueError:
            return
        self._last_usage_snapshot = {
            "usage": usage,
            "limit": limit,
            "remaining": remaining,
        }
        if limit > 0:
            pct = (usage / limit) * 100.0
            if pct >= self._auto_disable_pct and not self._hard_disabled:
                self._hard_disabled = True
                logger.critical(
                    "arkham_auto_disabled usage={:.0f} limit={:.0f} pct={:.1f} "
                    "threshold={:.1f}",
                    usage, limit, pct, self._auto_disable_pct,
                )

    # ── Core request path ──────────────────────────────────────────────────

    async def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Optional[Any] = None,
        json_body: Optional[dict] = None,
    ) -> Optional[Any]:
        """HTTP request helper; return parsed JSON or None on any failure.

        Short-circuits when:
          * no API key configured,
          * inside a 429 Retry-After pause,
          * auto-disabled by reaching the label-usage threshold.
        `params` may be a dict whose values are either scalars or lists;
        httpx serialises list values as repeated query params, which is
        what Arkham's array-typed params (entityIds, pricingIds, ...)
        require.
        """
        if not self.api_key:
            return None
        if self._hard_disabled:
            return None
        now = time.monotonic()
        if now < self._rate_pause_until:
            return None
        for attempt in range(self._max_retries):
            try:
                if method == "GET":
                    resp = await self._client.get(path, params=params)
                elif method == "POST":
                    resp = await self._client.post(
                        path, params=params, json=json_body or {})
                elif method == "DELETE":
                    resp = await self._client.delete(path, params=params)
                else:
                    logger.error("arkham_unsupported_method method={}", method)
                    return None
                # Absorb usage headers unconditionally — even error
                # responses include them, and the operator wants to
                # see burn rate during transient 5xx windows.
                self._absorb_usage_headers(resp)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    self._rate_pause_until = time.monotonic() + retry_after
                    logger.warning(
                        "arkham_429 method={} path={} retry_after={} "
                        "pausing_on_chain",
                        method, path, retry_after,
                    )
                    return None
                if resp.status_code in (401, 403):
                    logger.error(
                        "arkham_{} invalid_api_key_or_forbidden "
                        "method={} path={}",
                        resp.status_code, method, path,
                    )
                    return None
                if resp.status_code == 405:
                    # Method Not Allowed is deterministic — same method
                    # always returns 405. Retrying is pointless and
                    # burns rate budget; log loudly so operator sees
                    # the shape mismatch.
                    logger.error(
                        "arkham_405_method_not_allowed method={} path={} "
                        "— client / API shape mismatch, endpoint disabled",
                        method, path,
                    )
                    return None
                if resp.status_code == 400:
                    # Bad Request is deterministic on param shape. Log
                    # the response body so the operator can debug which
                    # param Arkham rejected, then short-circuit (no
                    # retry — same request → same 400).
                    try:
                        body_preview = (resp.text or "")[:400]
                    except Exception:
                        body_preview = "<body unreadable>"
                    logger.error(
                        "arkham_400_bad_request method={} path={} body={}",
                        method, path, body_preview,
                    )
                    return None
                resp.raise_for_status()
                if resp.status_code == 204 or not resp.content:
                    return {}
                return resp.json()
            except Exception as e:  # network errors, 5xx, parse errors
                logger.warning(
                    "arkham_request_failed method={} path={} attempt={} "
                    "err={!r}",
                    method, path, attempt + 1, e,
                )
                await asyncio.sleep(1.5 ** attempt)
        return None

    # ── Public endpoints ───────────────────────────────────────────────────

    async def get_entity_balance_changes(
        self,
        entity_ids: Optional[list[str]] = None,
        pricing_ids: Optional[list[str]] = None,
        interval: str = "7d",
        order_by: str = "balanceUsd",
        order_dir: str = "desc",
        limit: int = 20,
        entity_types: Optional[list[str]] = None,
    ) -> Optional[Any]:
        """Fetch ranked entity balance changes.

        Calls `GET /intelligence/entity_balance_changes` (Arkham v1.1).
        Returns a JSON list of entities with their 7d+ balance changes.

        **Interval constraint:** Arkham only accepts `7d`, `14d`, `30d`
        on this endpoint. Shorter windows return 400. For real-time
        flow, a different endpoint is needed (probably
        `/transfers/histogram` or the WS stream).

        **`orderBy` is server-required** (undocumented but enforced);
        omitting it returns 400 with `"orderBy parameter is required"`.

        Either `entity_ids`, `entity_types`, or filter-free is valid;
        empty filters return the top N entities overall.
        """
        params: dict = {
            "interval": interval,
            "orderBy": order_by,
            "orderDir": order_dir,
            "limit": int(limit),
        }
        if entity_ids:
            params["entityIds"] = list(entity_ids)
        if pricing_ids:
            params["pricingIds"] = list(pricing_ids)
        if entity_types:
            params["entityTypes"] = list(entity_types)
        return await self._request(
            "/intelligence/entity_balance_changes",
            method="GET", params=params,
        )

    async def create_ws_session(self) -> Optional[str]:
        """Create a WebSocket session for the whale-transfer stream.

        Calls `POST /ws/sessions` with an empty body. Arkham returns a
        session id that must ride as `?session_id=<sid>` on the
        subsequent `wss://api.arkm.com/ws/transfers` connection.
        Returns None on any failure.
        """
        data = await self._request(
            "/ws/sessions", method="POST", json_body={})
        if data is None:
            return None
        if not isinstance(data, dict):
            return None
        sid = (
            data.get("session_id")
            or data.get("sessionId")
            or data.get("id")
        )
        if not sid:
            return None
        return str(sid)

    async def delete_ws_session(self, session_id: str) -> bool:
        """Best-effort release of a WS session.

        Calls `DELETE /ws/sessions/{id}`. Returns True on 2xx, False
        otherwise. Failure-isolated (never raises).
        """
        if not self.api_key or self._hard_disabled:
            return False
        if not session_id:
            return False
        try:
            resp = await self._client.delete(f"/ws/sessions/{session_id}")
            self._absorb_usage_headers(resp)
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.warning("arkham_ws_session_delete_failed err={!r}", e)
            return False

    async def get_subscription_usage(self) -> Optional[dict]:
        """Fetch current subscription + datapoints usage.

        Arkham exposes this at `GET /user/usage` (or similar — some
        deployments have renamed; callers tolerate None). Primary usage
        tracking is the `X-Intel-Datapoints-*` headers absorbed on
        every data request, not this endpoint.
        """
        return await self._request(
            "/user/usage", method="GET",
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release the underlying HTTP client. Idempotent."""
        try:
            await self._client.aclose()
        except Exception:
            pass


# ── Snapshot fetchers (Phase B) ────────────────────────────────────────────
#
# Thin wrappers over ArkhamClient.get_entity_balance_changes that build the
# OnChainSnapshot / stablecoin-pulse scalar used by downstream gates and
# modifiers. Keep these as module-level functions (not ArkhamClient
# methods) so the client stays transport-only; snapshot-derivation rules
# live outside the HTTP layer, making them easy to unit-test with a
# mocked client.

from src.data.on_chain_types import OnChainSnapshot  # noqa: E402 — after class def


# Arkham entity IDs for the major CEXes tracked in the daily snapshot.
# Kept as a module constant so tests can monkeypatch and the operator can
# tweak without touching the fetcher logic. Binance + Coinbase + OKX +
# Bybit + Kraken + Bitfinex cover ~80% of stablecoin flow volume per
# Arkham's own coverage; one missing exchange degrades gracefully because
# the daily_macro_bias rule operates on NET change, not per-exchange
# resolution.
DEFAULT_CEX_ENTITY_IDS: list[str] = [
    "binance", "coinbase", "okx", "bybit", "kraken", "bitfinex",
]

# Stablecoin + BTC + ETH pricing IDs for the daily macro-bias snapshot.
# `tether` + `usd-coin` together represent ~90% of CEX stablecoin
# volume on a typical day.
DEFAULT_STABLECOIN_PRICING_IDS: list[str] = ["tether", "usd-coin"]
DEFAULT_DAILY_PRICING_IDS: list[str] = [
    "tether", "usd-coin", "bitcoin", "ethereum",
]


def _extract_net_change_usd(
    data: Any,
    pricing_id: str,
) -> Optional[float]:
    """Pull the signed USD balance change for `pricing_id` out of an
    `entity_balance_changes` response (Arkham v1.1).

    Actual response shape (verified via live probe 2026-04-21):
      [
        {"entityId": "binance", "entityType": "cex",
         "balanceUsd": ..., "prevBalanceUsd": ...,
         "tokenBalances": [
            {"tokenId": "tether", "tokenSymbol": "usdt",
             "balanceUsd": 4.4e10, "prevBalanceUsd": 4.3e10},
            ...
         ]},
        ...
      ]

    Balance change = `balanceUsd - prevBalanceUsd` summed across every
    entity's `tokenBalances` entry whose `tokenId` or `tokenSymbol`
    matches `pricing_id`. Positive = funds arrived at CEX in the
    window; negative = funds left.

    Fallback shapes (kept so legacy tests + alternate deployments still
    work):
      * Flat list of per-(entity, pricing) rows with `balanceChangeUsd`.
      * Legacy dict form `{"entities":{"<id>":{"<pricing>":{...}}}}`.

    Returns None when no matching token is found in any entity — caller
    treats None as "no signal" and short-circuits the bias classifier.
    """
    pid = pricing_id.lower()
    total = 0.0
    any_seen = False

    def _pick_delta_from_prev(row: dict) -> Optional[float]:
        """Compute balance change from `balanceUsd` − `prevBalanceUsd`
        when both are present. Signed float."""
        b = row.get("balanceUsd")
        p = row.get("prevBalanceUsd")
        if b is None or p is None:
            return None
        try:
            return float(b) - float(p)
        except (TypeError, ValueError):
            return None

    def _pick_change_usd(row: dict) -> Optional[float]:
        """Flat-shape fallback: try explicit change fields."""
        for key in (
            "balanceChangeUsd", "balance_change_usd",
            "changeUsd", "change_usd",
            "deltaUsd", "delta_usd",
        ):
            v = row.get(key)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def _row_matches_token(row: dict) -> bool:
        for key in ("tokenId", "tokenSymbol", "token_id", "token",
                    "pricingId", "pricing_id", "asset", "symbol"):
            v = row.get(key)
            if v is None:
                continue
            if str(v).lower() == pid:
                return True
        return False

    try:
        if isinstance(data, list):
            for entity in data:
                if not isinstance(entity, dict):
                    continue
                # Primary shape: per-entity with `tokenBalances` list.
                inner_list = entity.get("tokenBalances")
                if isinstance(inner_list, list):
                    for tb in inner_list:
                        if not isinstance(tb, dict):
                            continue
                        if not _row_matches_token(tb):
                            continue
                        delta = _pick_delta_from_prev(tb)
                        if delta is None:
                            delta = _pick_change_usd(tb)
                        if delta is not None:
                            total += delta
                            any_seen = True
                    continue
                # Flat fallback: per-row is already per-(entity, pricing).
                if _row_matches_token(entity):
                    delta = (
                        _pick_delta_from_prev(entity)
                        or _pick_change_usd(entity)
                    )
                    if delta is not None:
                        total += delta
                        any_seen = True
                # Legacy nested-breakdown fallback.
                for nested_key in ("balances", "changes", "breakdown", "pricings"):
                    breakdown = entity.get(nested_key)
                    if not isinstance(breakdown, list):
                        continue
                    for inner in breakdown:
                        if not isinstance(inner, dict):
                            continue
                        if _row_matches_token(inner):
                            delta = (
                                _pick_delta_from_prev(inner)
                                or _pick_change_usd(inner)
                            )
                            if delta is not None:
                                total += delta
                                any_seen = True
            return total if any_seen else None

        # Legacy dict fixture form (tests only).
        if isinstance(data, dict):
            entities = data.get("entities")
            if isinstance(entities, dict):
                for _, rows in entities.items():
                    if not isinstance(rows, dict):
                        continue
                    row = rows.get(pricing_id) or rows.get(pid)
                    if row is None:
                        continue
                    if isinstance(row, dict):
                        delta = (
                            _pick_delta_from_prev(row)
                            or _pick_change_usd(row)
                            or row.get("balance_change_usd")
                        )
                        if delta is not None:
                            try:
                                total += float(delta)
                                any_seen = True
                            except (TypeError, ValueError):
                                pass
                    else:
                        try:
                            total += float(row)
                            any_seen = True
                        except (TypeError, ValueError):
                            continue
                return total if any_seen else None
            inner = data.get("data")
            if isinstance(inner, list):
                return _extract_net_change_usd(inner, pricing_id)
        return None
    except Exception:
        return None


async def fetch_daily_snapshot(
    client: "ArkhamClient",
    *,
    stablecoin_threshold_usd: float,
    btc_netflow_threshold_usd: float,
    stale_threshold_s: int,
    snapshot_age_s: int = 0,
    entity_ids: Optional[list[str]] = None,
    interval: str = "7d",
) -> Optional[OnChainSnapshot]:
    """Build the macro-bias snapshot over a 7d / 14d / 30d window.

    **Window constraint (server-enforced):** Arkham's
    `entity_balance_changes` endpoint only accepts `7d`, `14d`, `30d`.
    The "24h daily" framing in earlier Phase A/B design notes was
    aspirational — this endpoint can't serve it. Slower signal, still
    directional. Operator can bump `interval` to `14d` / `30d` for an
    even slower / smoother signal.

    Rule (Phase C classifier):
      * bullish  when stablecoin CEX balance Δ ≥ `stablecoin_threshold`
                 AND BTC netflow ≤ `-btc_netflow_threshold` (BTC leaving CEX)
      * bearish  mirror
      * neutral  otherwise or any component missing

    Returns None when the underlying HTTP call fails.
    """
    # `entity_types=cex` is safer than an explicit entity_ids list:
    # operator doesn't need to know Arkham's exact entity names, and
    # new CEXes Arkham tracks automatically flow in.
    data = await client.get_entity_balance_changes(
        entity_ids=entity_ids,
        pricing_ids=DEFAULT_DAILY_PRICING_IDS,
        interval=interval,
        order_by="balanceUsd",
        order_dir="desc",
        limit=50,  # cap to avoid over-fetching long tail of CEXes
        entity_types=None if entity_ids else ["cex"],
    )
    if data is None:
        return None
    stablecoin_change = 0.0
    saw_any_stable = False
    for sid in DEFAULT_STABLECOIN_PRICING_IDS:
        v = _extract_net_change_usd(data, sid)
        if v is not None:
            stablecoin_change += v
            saw_any_stable = True
    btc_netflow = _extract_net_change_usd(data, "bitcoin")
    eth_netflow = _extract_net_change_usd(data, "ethereum")
    if not saw_any_stable and btc_netflow is None:
        return None
    bias: str = "neutral"
    if (saw_any_stable
            and stablecoin_change >= stablecoin_threshold_usd
            and btc_netflow is not None
            and btc_netflow <= -btc_netflow_threshold_usd):
        bias = "bullish"
    elif (saw_any_stable
            and stablecoin_change <= -stablecoin_threshold_usd
            and btc_netflow is not None
            and btc_netflow >= btc_netflow_threshold_usd):
        bias = "bearish"
    return OnChainSnapshot(
        daily_macro_bias=bias,
        stablecoin_pulse_1h_usd=None,
        cex_btc_netflow_24h_usd=btc_netflow,
        cex_eth_netflow_24h_usd=eth_netflow,
        coinbase_asia_skew_usd=None,
        bnb_self_flow_24h_usd=None,
        snapshot_age_s=int(snapshot_age_s),
        stale_threshold_s=int(stale_threshold_s),
    )


async def fetch_hourly_stablecoin_pulse(
    client: "ArkhamClient",
    *,
    entity_ids: Optional[list[str]] = None,
) -> Optional[float]:
    """**Disabled / stub (2026-04-21).** Arkham's
    `entity_balance_changes` endpoint doesn't support sub-7d windows —
    the original 1h pulse design was based on a misread of the API. A
    proper hourly flow signal would come from `/transfers/histogram`
    (or aggregating the WS transfers stream in-process); neither is
    wired yet.

    Returns None unconditionally so the Phase E penalty in
    `entry_signals.py` stays inert. Keeping the function signature
    + name so the runner scheduler + wiring don't need to change when
    a real hourly source lands.
    """
    _ = client, entity_ids  # unused; kept for future real implementation
    return None
