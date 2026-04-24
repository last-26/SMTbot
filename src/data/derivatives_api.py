"""Coinalyze REST client — funding rate, open interest, long/short ratio,
aggregated liquidations.

Rate limit: 40 requests/minute per API key. We enforce it with an in-process
token bucket (Madde 2 — this is *per-symbol*: one symbol in a `?symbols=A`
query spends 1 token; a comma-separated multi-symbol query spends N).

Failure policy:
  * 401 → log + return None once (no retry loop, the key is bad).
  * 429 → honor `Retry-After` header, then continue.
  * Any other exception → warn + exponential retry up to `max_retries`.
  * Missing API key → warn at construction, all fetches silently return None
    so the rest of the bot keeps running.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from loguru import logger

COINALYZE_BASE = "https://api.coinalyze.net/v1"

# Binance (.A) > Bybit (.6) > OKX (.3) > Deribit (.F) > HTX (.H) — liquidity ranked.
EXCHANGE_PRIORITY = ["A", "6", "3", "F", "H"]


@dataclass
class DerivativesSnapshot:
    """Point-in-time derivatives view for one symbol (OKX form)."""
    symbol: str
    ts_ms: int
    funding_rate_current: float = 0.0
    funding_rate_predicted: float = 0.0
    open_interest_usd: float = 0.0
    long_short_ratio: float = 1.0
    long_share: float = 0.5
    short_share: float = 0.5
    aggregated_long_liq_1h_usd: float = 0.0
    aggregated_short_liq_1h_usd: float = 0.0
    # Enriched by DerivativesCache (Madde 3) before persist — default 0 keeps
    # the dataclass zero-arg constructible for unit tests.
    oi_change_1h_pct: float = 0.0
    oi_change_24h_pct: float = 0.0


class CoinalyzeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.getenv("COINALYZE_API_KEY")
        if not self.api_key:
            logger.warning("coinalyze_api_key_missing; derivatives_api will "
                           "return None snapshots")
        self._client = httpx.AsyncClient(
            base_url=COINALYZE_BASE,
            timeout=timeout_s,
            headers={"api_key": self.api_key or ""},
        )
        self._max_retries = max_retries
        self._symbol_map: dict[str, str] = {}
        self._symbol_map_loaded = False
        # Token bucket — 40 tokens/minute.
        self._rate_tokens = 40.0
        self._rate_capacity = 40.0
        self._rate_last_refill = time.monotonic()
        self._rate_lock = asyncio.Lock()
        # 429 backoff — populated when Coinalyze returns Retry-After so that
        # subsequent requests short-circuit (return None) instead of awaiting
        # asyncio.sleep inside the shared request path and blocking the event
        # loop for every other coroutine (pending-poll, monitor, per-symbol
        # cycles). `_rate_pause_until` is a monotonic deadline.
        self._rate_pause_until = 0.0

    # ── Rate limiting ──────────────────────────────────────────────────────

    async def _consume_token(self, cost: int = 1) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._rate_last_refill
            refill = elapsed * (40.0 / 60.0)
            self._rate_tokens = min(self._rate_capacity, self._rate_tokens + refill)
            self._rate_last_refill = now
            if self._rate_tokens < cost:
                wait = (cost - self._rate_tokens) * (60.0 / 40.0)
                await asyncio.sleep(wait)
                self._rate_tokens = 0.0
            else:
                self._rate_tokens -= cost

    async def _request(self, path: str, params: dict,
                       cost: int = 1) -> Optional[Any]:
        if not self.api_key:
            return None
        # Honour Retry-After from a prior 429 without blocking — callers fall
        # back to stale/None snapshots (already their failure-isolation path)
        # rather than every coroutine stalling on asyncio.sleep(retry_after).
        now = time.monotonic()
        if now < self._rate_pause_until:
            return None
        for attempt in range(self._max_retries):
            await self._consume_token(cost=cost)
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    self._rate_pause_until = time.monotonic() + retry_after
                    logger.warning(
                        "coinalyze_429 path={} retry_after={} pausing_derivatives",
                        path, retry_after,
                    )
                    return None
                if resp.status_code == 401:
                    logger.error("coinalyze_401 invalid_api_key")
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("coinalyze_request_failed path={} attempt={} err={!r}",
                               path, attempt + 1, e)
                await asyncio.sleep(1.5 ** attempt)
        return None

    # ── Symbol mapping (OKX → Coinalyze) ───────────────────────────────────

    async def ensure_symbol_map(self, watched: list[str]) -> None:
        """Populate `self._symbol_map` from `/future-markets`.

        Idempotent on success — only calls the endpoint once (flag sticks
        so we never thrash the rate budget during outages).

        2026-04-24 — retry once on transient 429 at startup. Previously a
        single 429 on `/future-markets` (e.g. from a rapid restart pushing
        the Coinalyze rate-counter over) would permanently latch
        `_symbol_map_loaded=True` with an empty map, disabling the entire
        derivatives stack for the session. Now: if `_request` shortcircuits
        because `_rate_pause_until` is set, wait for the pause to clear
        (capped at 90s) and retry once. A second 429 after the full wait
        = genuine outage, latch as before.
        """
        if self._symbol_map_loaded:
            return

        data = await self._request("/future-markets", {}, cost=1)
        # If we likely 429'd, wait for the pause window and retry once.
        if not data and self._rate_pause_until > time.monotonic():
            wait_s = min(
                self._rate_pause_until - time.monotonic() + 1.0, 90.0,
            )
            logger.warning(
                "coinalyze_symbol_map_rate_limited waiting_s={:.1f} retrying_once",
                wait_s,
            )
            await asyncio.sleep(wait_s)
            data = await self._request("/future-markets", {}, cost=1)

        if not data:
            logger.warning("coinalyze_symbol_map_empty; derivatives will be None")
            self._symbol_map_loaded = True
            return

        for okx_sym in watched:
            base = okx_sym.split("-")[0]
            candidates = [
                m for m in data
                if m.get("base_asset") == base
                and m.get("quote_asset") == "USDT"
                and m.get("is_perpetual") is True
                and m.get("margined") == "STABLE"
            ]
            if not candidates:
                logger.warning("coinalyze_no_market_for_symbol okx={} base={}",
                               okx_sym, base)
                continue

            chosen = None
            for prio in EXCHANGE_PRIORITY:
                for c in candidates:
                    if c.get("symbol", "").endswith(f".{prio}"):
                        chosen = c
                        break
                if chosen:
                    break
            if chosen is None:
                chosen = candidates[0]

            self._symbol_map[okx_sym] = chosen["symbol"]
            logger.info("coinalyze_mapping okx={} coinalyze={}",
                        okx_sym, chosen["symbol"])

        self._symbol_map_loaded = True

    def coinalyze_symbol(self, okx_symbol: str) -> Optional[str]:
        return self._symbol_map.get(okx_symbol)

    # ── Current snapshot endpoints (flat: {symbol, value, update}) ────────

    async def _fetch_current_value(self, path: str,
                                   coinalyze_symbol: str,
                                   params: Optional[dict] = None) -> Optional[float]:
        q = {"symbols": coinalyze_symbol}
        if params:
            q.update(params)
        data = await self._request(path, q, cost=1)
        if not data or not isinstance(data, list):
            return None
        try:
            return float(data[0].get("value", 0.0))
        except (KeyError, ValueError, TypeError):
            return None

    async def fetch_current_oi_usd(self, coinalyze_symbol: str) -> Optional[float]:
        return await self._fetch_current_value(
            "/open-interest", coinalyze_symbol,
            params={"convert_to_usd": "true"},
        )

    async def fetch_current_funding(self, coinalyze_symbol: str) -> Optional[float]:
        return await self._fetch_current_value("/funding-rate", coinalyze_symbol)

    async def fetch_predicted_funding(self, coinalyze_symbol: str) -> Optional[float]:
        return await self._fetch_current_value(
            "/predicted-funding-rate", coinalyze_symbol,
        )

    # ── History endpoints (nested: {symbol, history: [...]}) ──────────────

    async def fetch_liquidation_history(
        self, coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 1,
    ) -> Optional[dict]:
        """Returns summed {long_usd, short_usd, bucket_count} over the window."""
        now = int(time.time())
        data = await self._request(
            "/liquidation-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
                "convert_to_usd": "true",
            },
            cost=1,
        )
        if not data or not isinstance(data, list) or not data[0].get("history"):
            return None
        history = data[0]["history"]
        return {
            "long_usd": sum(float(h.get("l", 0)) for h in history),
            "short_usd": sum(float(h.get("s", 0)) for h in history),
            "bucket_count": len(history),
        }

    async def fetch_long_short_ratio(
        self, coinalyze_symbol: str,
        interval: str = "1hour",
    ) -> Optional[dict]:
        """Most recent {ratio, long_share, short_share} bar."""
        now = int(time.time())
        data = await self._request(
            "/long-short-ratio-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - 2 * 3600,
                "to": now,
            },
            cost=1,
        )
        if not data or not isinstance(data, list) or not data[0].get("history"):
            return None
        latest = data[0]["history"][-1]
        return {
            "ratio": float(latest.get("r", 1.0)),
            "long_share": float(latest.get("l", 0.5)),
            "short_share": float(latest.get("s", 0.5)),
        }

    async def fetch_funding_history_series(
        self, coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 720,
    ) -> Optional[list[float]]:
        """Return only the close (`c`) series for z-score calibration."""
        now = int(time.time())
        data = await self._request(
            "/funding-rate-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
            },
            cost=1,
        )
        if not data or not isinstance(data, list) or not data[0].get("history"):
            return None
        return [float(h.get("c", 0.0)) for h in data[0]["history"]]

    async def fetch_ls_ratio_history_series(
        self, coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 336,
    ) -> Optional[list[float]]:
        now = int(time.time())
        data = await self._request(
            "/long-short-ratio-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
            },
            cost=1,
        )
        if not data or not isinstance(data, list) or not data[0].get("history"):
            return None
        return [float(h.get("r", 1.0)) for h in data[0]["history"]]

    async def fetch_oi_change_pct(
        self, coinalyze_symbol: str,
        lookback_hours: int = 24,
    ) -> Optional[float]:
        """% change between first and last bar of an OI history query."""
        now = int(time.time())
        data = await self._request(
            "/open-interest-history",
            {
                "symbols": coinalyze_symbol,
                "interval": "1hour",
                "from": now - (lookback_hours + 1) * 3600,
                "to": now,
                "convert_to_usd": "true",
            },
            cost=1,
        )
        if (not data or not isinstance(data, list)
                or not data[0].get("history")
                or len(data[0]["history"]) < 2):
            return None
        history = data[0]["history"]
        start = float(history[0].get("c", 0))
        end = float(history[-1].get("c", 0))
        if start <= 0:
            return None
        return (end - start) / start * 100.0

    # ── Aggregate snapshot ────────────────────────────────────────────────

    async def fetch_snapshot(self, okx_symbol: str) -> Optional[DerivativesSnapshot]:
        """5 sequential per-symbol calls. Paralleling would blow the bucket."""
        cn_sym = self._symbol_map.get(okx_symbol)
        if not cn_sym:
            return None

        oi = await self.fetch_current_oi_usd(cn_sym)
        funding = await self.fetch_current_funding(cn_sym)
        predicted = await self.fetch_predicted_funding(cn_sym)
        liq = await self.fetch_liquidation_history(cn_sym, "1hour", 1)
        ls = await self.fetch_long_short_ratio(cn_sym, "1hour")

        return DerivativesSnapshot(
            symbol=okx_symbol,
            ts_ms=int(time.time() * 1000),
            funding_rate_current=funding or 0.0,
            funding_rate_predicted=predicted or 0.0,
            open_interest_usd=oi or 0.0,
            long_short_ratio=(ls or {}).get("ratio", 1.0),
            long_share=(ls or {}).get("long_share", 0.5),
            short_share=(ls or {}).get("short_share", 0.5),
            aggregated_long_liq_1h_usd=(liq or {}).get("long_usd", 0.0),
            aggregated_short_liq_1h_usd=(liq or {}).get("short_usd", 0.0),
        )

    async def close(self) -> None:
        await self._client.aclose()
