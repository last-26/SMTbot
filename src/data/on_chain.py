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
        params: Optional[dict] = None,
    ) -> Optional[Any]:
        """GET `path` with params; return parsed JSON or None on any failure.

        Short-circuits when:
          * no API key configured,
          * inside a 429 Retry-After pause,
          * auto-disabled by reaching the label-usage threshold.
        """
        if not self.api_key:
            return None
        if self._hard_disabled:
            return None
        now = time.monotonic()
        if now < self._rate_pause_until:
            return None
        params = params or {}
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.get(path, params=params)
                # Absorb usage headers unconditionally — even error
                # responses include them, and the operator wants to
                # see burn rate during transient 5xx windows.
                self._absorb_usage_headers(resp)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    self._rate_pause_until = time.monotonic() + retry_after
                    logger.warning(
                        "arkham_429 path={} retry_after={} pausing_on_chain",
                        path, retry_after,
                    )
                    return None
                if resp.status_code in (401, 403):
                    logger.error(
                        "arkham_{} invalid_api_key_or_forbidden path={}",
                        resp.status_code, path,
                    )
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # network errors, 5xx, parse errors
                logger.warning(
                    "arkham_request_failed path={} attempt={} err={!r}",
                    path, attempt + 1, e,
                )
                await asyncio.sleep(1.5 ** attempt)
        return None

    # ── Public endpoints ───────────────────────────────────────────────────

    async def get_entity_balance_changes(
        self,
        entity_ids: list[str],
        pricing_ids: list[str],
        interval: str = "24h",
    ) -> Optional[dict]:
        """Fetch aggregated balance changes across entities.

        `entity_ids` are Arkham entity identifiers (e.g. major CEXes).
        `pricing_ids` are the assets to price in (e.g. tether, usd-coin,
        bitcoin). `interval` accepts Arkham's duration strings ('1h',
        '24h'); defaults to 24h for the daily macro-bias pull.
        """
        params = {
            "entityIds": ",".join(entity_ids),
            "pricingIds": ",".join(pricing_ids),
            "interval": interval,
        }
        return await self._request("/intel/entity-balance-changes", params)

    async def create_ws_session(self) -> Optional[str]:
        """Request a one-time session token for the whale-transfer WS.

        Arkham's WS requires a short-lived session id obtained via REST.
        Returns the id on success, None on any failure.
        """
        data = await self._request("/intel/ws-session")
        if data is None:
            return None
        if not isinstance(data, dict):
            return None
        sid = data.get("sessionId") or data.get("session_id")
        if not sid:
            return None
        return str(sid)

    async def delete_ws_session(self, session_id: str) -> bool:
        """Best-effort release of a WS session token.

        Arkham auto-expires idle sessions but explicit release is polite
        under a trial quota. Returns True on 2xx, False otherwise. Uses
        the same failure-isolated path (no raise).
        """
        if not self.api_key or self._hard_disabled:
            return False
        if not session_id:
            return False
        try:
            resp = await self._client.delete(
                f"/intel/ws-session/{session_id}"
            )
            self._absorb_usage_headers(resp)
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.warning("arkham_ws_session_delete_failed err={!r}", e)
            return False

    async def get_subscription_usage(self) -> Optional[dict]:
        """Poll the explicit label-usage endpoint.

        The per-response `X-Intel-Datapoints-*` headers are the primary
        source; this endpoint exists for operator-directed audits and
        for priming `_last_usage_snapshot` at bot startup before any
        data endpoint has been hit.
        """
        return await self._request("/subscription/intel-usage")

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
    data: dict,
    pricing_id: str,
) -> Optional[float]:
    """Pull the aggregate `balance_change_usd` for `pricing_id` out of a
    balance-changes response. Returns None when the response shape is
    unexpected (a future API change should degrade, not crash).

    Arkham's response nests entities → pricing rows; we sum across
    entities for a single aggregated flow number.
    """
    try:
        entities = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(entities, dict):
            return None
        total = 0.0
        any_seen = False
        for _, rows in entities.items():
            if not isinstance(rows, dict):
                continue
            row = rows.get(pricing_id)
            if row is None:
                continue
            if isinstance(row, dict):
                change = row.get("balance_change_usd")
            else:
                change = row
            if change is None:
                continue
            try:
                total += float(change)
                any_seen = True
            except (TypeError, ValueError):
                continue
        return total if any_seen else None
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
) -> Optional[OnChainSnapshot]:
    """Build the daily macro-bias snapshot.

    Rule (mirrors plan §1 / Phase C):
      * bullish  when stablecoin CEX balance Δ ≥ `stablecoin_threshold`
                 AND BTC netflow ≤ `-btc_netflow_threshold` (BTC leaving CEX)
      * bearish  when the mirror holds (stablecoins leaving, BTC arriving)
      * neutral  otherwise (or any component missing)

    Returns None when the underlying HTTP call fails — caller sees the
    whole snapshot as absent rather than a partial / inconsistent view.
    """
    eids = entity_ids if entity_ids is not None else DEFAULT_CEX_ENTITY_IDS
    data = await client.get_entity_balance_changes(
        entity_ids=eids,
        pricing_ids=DEFAULT_DAILY_PRICING_IDS,
        interval="24h",
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
    # Netflow convention: Arkham's sign is from the CEX's point of view
    # (positive = asset arriving at CEX, negative = leaving). BTC leaving
    # = bullish, mirroring the plan's rule `cex_btc_netflow < -thr`.
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
        stablecoin_pulse_1h_usd=None,  # filled by fetch_hourly_stablecoin_pulse
        cex_btc_netflow_24h_usd=btc_netflow,
        cex_eth_netflow_24h_usd=eth_netflow,
        coinbase_asia_skew_usd=None,   # reserved for future per-venue pulls
        bnb_self_flow_24h_usd=None,    # reserved for future per-venue pulls
        snapshot_age_s=int(snapshot_age_s),
        stale_threshold_s=int(stale_threshold_s),
    )


async def fetch_hourly_stablecoin_pulse(
    client: "ArkhamClient",
    *,
    entity_ids: Optional[list[str]] = None,
) -> Optional[float]:
    """Aggregate USDT + USDC 1h CEX balance delta. Returns a signed USD
    number (positive = stablecoins entering CEXes, risk-on buying ammo;
    negative = leaving, risk-off). None on HTTP failure or empty response.
    """
    eids = entity_ids if entity_ids is not None else DEFAULT_CEX_ENTITY_IDS
    data = await client.get_entity_balance_changes(
        entity_ids=eids,
        pricing_ids=DEFAULT_STABLECOIN_PRICING_IDS,
        interval="1h",
    )
    if data is None:
        return None
    total = 0.0
    saw_any = False
    for sid in DEFAULT_STABLECOIN_PRICING_IDS:
        v = _extract_net_change_usd(data, sid)
        if v is not None:
            total += v
            saw_any = True
    if not saw_any:
        return None
    return total
