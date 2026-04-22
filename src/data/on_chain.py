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
        """**DEPRECATED v1 endpoint — 500 credits per call.** Kept for
        back-compat with older listener code; new code should call
        `create_ws_stream(filters)` instead. The v2 stream endpoint
        has zero session-creation fee per Arkham's docs; operator
        observed v1 burning 500 credits / call on the dashboard.
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
        """**DEPRECATED v1 endpoint.** Best-effort release of a v1
        session. New code uses `delete_ws_stream(stream_id)`.
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

    # ── WebSocket v2 streams (2026-04-21) ──────────────────────────────────
    #
    # v2 replaces v1's ephemeral sessions with persistent streams.
    # Trade-offs:
    #   * Stream creation has NO session-creation fee (v1 burned 500
    #     credits / call → operator dashboard confirmed).
    #   * Filter (usdGte / tokens / from / to) is baked into the stream
    #     at creation time. The WS connection carries no subscribe
    #     message — just connect and receive matching transfers.
    #   * Streams persist across bot restarts. Persisting stream_id to
    #     disk lets subsequent restarts reuse instead of re-create.

    async def create_ws_stream(
        self,
        filters: dict,
    ) -> Optional[dict]:
        """Create a new v2 WebSocket stream with the given filter.

        `filters` follows Arkham's spec (verified via live probe):
          `{"from": ["type:cex"], "usdGte": "100000000"}` — at least one
          of base / from / to / tokens / usdGte (≥ 250_000) required.

        Returns the full response dict `{streamId, id, createdAt}` or
        None on any failure. Callers should persist `streamId` to disk
        and reuse across restarts.
        """
        data = await self._request(
            "/ws/v2/streams", method="POST", json_body=dict(filters))
        if not isinstance(data, dict):
            return None
        return data

    async def list_ws_streams(self) -> Optional[list]:
        """List the user's current v2 streams.

        Returned shape (verified via live probe):
          `[{"streamId": ..., "id": ..., "createdAt": ISO,
             "isConnected": bool, "lastActive": ISO, "transfersUsed": int}]`

        Used on bot startup to check whether a previous run's stream is
        still alive (→ reuse, avoid recreation).
        """
        data = await self._request("/ws/v2/streams", method="GET")
        if isinstance(data, list):
            return data
        return None

    async def delete_ws_stream(self, stream_id: str) -> bool:
        """Delete a v2 stream by its `streamId`. Best-effort, never raises.

        Used for cleanup of orphan streams at startup (streams that
        don't match the current filter config) or at shutdown when the
        operator wants a clean slate.
        """
        if not self.api_key or self._hard_disabled:
            return False
        if not stream_id:
            return False
        try:
            resp = await self._client.delete(
                f"/ws/v2/streams/{stream_id}")
            self._absorb_usage_headers(resp)
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.warning("arkham_ws_stream_delete_failed err={!r}", e)
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

    async def get_transfers_histogram(
        self,
        *,
        base: Optional[str] = None,
        tokens: Optional[list[str]] = None,
        flow: Optional[str] = None,       # "in" | "out" | "self" | "all"
        time_last: str = "24h",
        granularity: str = "1h",          # "1h" | "1d"
        usd_gte: Optional[float] = None,
        chains: Optional[list[str]] = None,
        limit: int = 100,
    ) -> Optional[list]:
        """Aggregated histogram of transfers over time (count + USD).

        Calls `GET /transfers/histogram`. Returns a list of per-bucket
        dicts `[{"time": ISO, "count": int, "usd": float}]`.

        Key Arkham syntax (verified via live probe):
          * `base="type:cex"` → any CEX entity; `base="binance"` → one.
          * `flow="in"` → into the base entity; `"out"` → out of it.
          * `tokens="tether,usd-coin"` comma-joined pricing IDs.
          * `time_last="24h"` + `granularity="1h"` → 25 buckets (24 hours
            + boundary). `granularity="1d"` for daily buckets.
          * Rate limit is stricter: 1 req/s. Callers should pace.

        None on any failure; empty list when no matching transfers.
        """
        params: dict = {
            "timeLast": time_last,
            "granularity": granularity,
            "limit": int(limit),
        }
        if base is not None:
            params["base"] = base
        if tokens:
            # Arkham's `tokens` param on /transfers/histogram accepts
            # a comma-joined string (unlike `entityIds` which takes a
            # list-of-repeated). Verified via probe.
            params["tokens"] = ",".join(tokens)
        if flow:
            params["flow"] = flow
        if chains:
            params["chains"] = ",".join(chains)
        if usd_gte is not None:
            params["usdGte"] = float(usd_gte)
        return await self._request(
            "/transfers/histogram",
            method="GET", params=params,
        )

    async def get_altcoin_index(self) -> Optional[int]:
        """Current Altcoin Index (scalar 0-100).

        Low values → altcoins underperforming BTC (BTC dominance
        season). High values → altcoins outperforming. Single-call
        endpoint, cheap. Docs don't specify update cadence; treat as
        an hourly-refresh signal.

        Returns None on HTTP failure or unexpected response shape.
        """
        data = await self._request(
            "/marketdata/altcoin_index", method="GET",
        )
        if not isinstance(data, dict):
            return None
        raw = data.get("altcoinIndex")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

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


async def _net_flow_via_histogram(
    client: "ArkhamClient",
    *,
    tokens: list[str],
    time_last: str,
    rate_pause_s: float = 1.1,
) -> Optional[float]:
    """Return net CEX flow (inflow − outflow) for `tokens` over the
    given window. Two `/transfers/histogram` calls with 1s rate-limit
    cushion between them.

    Returns None if either leg fails; 0.0 when both return empty.
    """
    inflow = await client.get_transfers_histogram(
        base="type:cex", tokens=tokens, flow="in",
        time_last=time_last, granularity="1d",
    )
    await asyncio.sleep(rate_pause_s)
    outflow = await client.get_transfers_histogram(
        base="type:cex", tokens=tokens, flow="out",
        time_last=time_last, granularity="1d",
    )
    if inflow is None or outflow is None:
        return None
    total_in = sum(float(b.get("usd") or 0) for b in inflow
                   if isinstance(b, dict))
    total_out = sum(float(b.get("usd") or 0) for b in outflow
                    if isinstance(b, dict))
    return total_in - total_out


async def fetch_daily_snapshot(
    client: "ArkhamClient",
    *,
    stablecoin_threshold_usd: float,
    btc_netflow_threshold_usd: float,
    stale_threshold_s: int,
    snapshot_age_s: int = 0,
    entity_ids: Optional[list[str]] = None,
    time_last: str = "24h",
    interval: str = "",  # kept for backwards compat; ignored
) -> Optional[OnChainSnapshot]:
    """Build the macro-bias snapshot over `time_last` (default 24h).

    **2026-04-21 rebuild (Phase F3):** rebuilt on `/transfers/histogram`
    with `granularity=1d` which supports arbitrary `timeLast` windows
    (24h / 48h / 7d / ...). Prior version was locked to Arkham's
    `/intelligence/entity_balance_changes` 7d minimum, making the
    "daily" signal actually a 7-day rolling average. Histogram path is
    more reactive and matches the original plan's 24h intent.

    Rule (Phase C classifier, unchanged):
      * bullish  stablecoin CEX net ≥ `stablecoin_threshold` AND
                 BTC net ≤ `-btc_netflow_threshold` (BTC leaving CEX)
      * bearish  mirror
      * neutral  otherwise or any component missing

    Cost: 6 `/transfers/histogram` calls per invocation (in+out × 3
    tokens sets — stablecoins, BTC, ETH). At 4 credits/call × 1
    call/day = 24 credits/day = ~720 credits/month. Well inside budget
    for a once-daily refresh.

    `entity_ids` and `interval` kwargs are preserved for backwards
    compat but ignored by the histogram path. Returns None when both
    stablecoin + BTC legs fail; partial failure degrades to neutral.
    """
    _ = entity_ids, interval  # ignored; kept for call-site back-compat

    # Stablecoins (USDT + USDC) in a single call by passing both tokens.
    stablecoin_change = await _net_flow_via_histogram(
        client,
        tokens=list(DEFAULT_STABLECOIN_PRICING_IDS),
        time_last=time_last,
    )
    await asyncio.sleep(1.1)
    btc_netflow = await _net_flow_via_histogram(
        client, tokens=["bitcoin"], time_last=time_last,
    )
    await asyncio.sleep(1.1)
    eth_netflow = await _net_flow_via_histogram(
        client, tokens=["ethereum"], time_last=time_last,
    )

    if stablecoin_change is None and btc_netflow is None:
        return None
    bias: str = "neutral"
    if (stablecoin_change is not None
            and btc_netflow is not None
            and stablecoin_change >= stablecoin_threshold_usd
            and btc_netflow <= -btc_netflow_threshold_usd):
        bias = "bullish"
    elif (stablecoin_change is not None
            and btc_netflow is not None
            and stablecoin_change <= -stablecoin_threshold_usd
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
    time_last: str = "1h",
) -> Optional[float]:
    """Net hourly stablecoin flow into / out of centralised exchanges.

    Uses `/transfers/histogram` with `base=type:cex, tokens=USDT+USDC,
    flow=in` and `flow=out` over the last `time_last` window, summing
    the USD buckets. Returns a **signed** number:
      * positive → stablecoins flowing INTO CEXes (buying ammo, risk-on).
      * negative → stablecoins flowing OUT of CEXes (cashing out, risk-off).
      * None     → one or both calls failed; caller treats as "no signal".

    Cost: 2 `/transfers/histogram` calls per invocation. Rate-limit: the
    endpoint is capped at 1 req/s; this function pauses ~1.1s between
    its two calls so back-to-back scheduler ticks don't 429.

    `entity_ids` is ignored (the `type:cex` meta filter already covers
    every Arkham-tracked CEX). Kept in the signature for backwards
    compat with the Phase B scheduler's call site.
    """
    _ = entity_ids
    tokens = list(DEFAULT_STABLECOIN_PRICING_IDS)
    inflow = await client.get_transfers_histogram(
        base="type:cex",
        tokens=tokens,
        flow="in",
        time_last=time_last,
        granularity="1h",
    )
    # Best-effort rate-limit cushion between back-to-back histogram calls.
    await asyncio.sleep(1.1)
    outflow = await client.get_transfers_histogram(
        base="type:cex",
        tokens=tokens,
        flow="out",
        time_last=time_last,
        granularity="1h",
    )
    if inflow is None or outflow is None:
        return None

    def _sum_usd(buckets: list) -> float:
        total = 0.0
        for b in buckets or []:
            if not isinstance(b, dict):
                continue
            try:
                total += float(b.get("usd") or 0)
            except (TypeError, ValueError):
                continue
        return total

    return _sum_usd(inflow) - _sum_usd(outflow)
