"""Economic calendar — scheduled-event blackout for new entries.

Two providers fetched in parallel and unioned. If either source flags a
HIGH-impact USD event in the configured window, blackout is active and the
runner skips new entries for the cycle. Open positions are untouched (their
SL/TP algos already cover risk).

Providers
  * Finnhub  — `/calendar/economic`. Requires `FINNHUB_API_KEY`. Free tier
    60 req/min. Token-bucket throttled.
  * FairEconomy — weekly `ff_calendar_thisweek.json` (no auth, ForexFactory
    mirror). One snapshot covers a week so we don't need a rate budget.

Failure policy mirrors `CoinalyzeClient`: 401 → silent (key bad, log + None);
429 → honor `Retry-After`; missing key → warn + always-None; any other
exception → exponential retry up to `max_retries`, then None. The
orchestrator tolerates *either* provider failing — only when both fail does
the cache go stale, and even then `is_in_blackout` returns False (soft fail
keeps the bot trading).

Out of scope: news sentiment classification, real-time crypto-specific
events. Deferred to post-Phase-7 to avoid overfitting the parameter tuner
on a noisy text classifier.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from loguru import logger
from pydantic import BaseModel, Field

FINNHUB_BASE = "https://finnhub.io/api/v1"
# FairEconomy publishes two weekly snapshots (Sunday–Saturday windows).
# Fetching both gives a true 7-day lookahead even when the bot boots near
# the end of the current week — without `nextweek`, a Friday boot would
# miss every Mon/Tue HIGH-impact release.
FAIRECONOMY_URLS = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
)

# Window inside which two events from different providers are considered the
# same event (after title normalization). 15 min covers timezone rounding,
# minor announcement-time drift, and intraday-vs-EOD refresh differences.
_DEDUP_WINDOW_S = 15 * 60


# ── Models ────────────────────────────────────────────────────────────────


class EconomicEventImpact(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EconomicEvent(BaseModel):
    """One scheduled macro release.

    `scheduled_time` is always UTC. `country` is the ISO currency code as
    reported by the provider (e.g. ``"USD"``, ``"EUR"``); we filter on this.
    `source` traces provenance — joined with ``"+"`` after dedup.
    """
    title: str
    country: str
    impact: EconomicEventImpact
    scheduled_time: datetime
    source: str
    forecast: Optional[str] = None
    previous: Optional[str] = None


class BlackoutInfo(BaseModel):
    """Result of `is_in_blackout(now)`."""
    active: bool
    event: Optional[EconomicEvent] = None
    seconds_until_event: Optional[int] = None
    seconds_after_event: Optional[int] = None
    reason: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────


def _normalize_impact(raw: Any) -> Optional[EconomicEventImpact]:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("high", "h", "3"):
        return EconomicEventImpact.HIGH
    if s in ("medium", "med", "m", "2"):
        return EconomicEventImpact.MEDIUM
    if s in ("low", "l", "1"):
        return EconomicEventImpact.LOW
    return None


def _normalize_title(title: str) -> str:
    """Lowercase + strip + collapse whitespace for dedup matching."""
    return " ".join(title.lower().split())


# Finnhub returns ISO-3166 alpha-2 country codes ("US", "GB"); FairEconomy
# returns currency codes ("USD", "GBP"). Normalize Finnhub at parse time so
# downstream filter/dedup compare like-with-like. Pass-through for codes
# already 3 chars (USD/EUR/etc.) keeps idempotence.
_COUNTRY_TO_CURRENCY: dict[str, str] = {
    "US": "USD", "GB": "GBP", "EU": "EUR",
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "NL": "EUR", "BE": "EUR", "AT": "EUR", "PT": "EUR",
    "IE": "EUR", "FI": "EUR", "GR": "EUR",
    "JP": "JPY", "CA": "CAD", "AU": "AUD", "CH": "CHF",
    "NZ": "NZD", "CN": "CNY", "HK": "HKD", "SG": "SGD",
    "KR": "KRW", "IN": "INR", "MX": "MXN", "BR": "BRL",
    "ZA": "ZAR", "RU": "RUB", "TR": "TRY", "SE": "SEK",
    "NO": "NOK", "DK": "DKK", "PL": "PLN",
}


def _country_to_currency(code: str) -> str:
    s = (code or "").strip().upper()
    if len(s) == 3:
        return s
    return _COUNTRY_TO_CURRENCY.get(s, s)


def _passes_filter(
    event: EconomicEvent,
    impact_filter: list[str],
    currencies: list[str],
) -> bool:
    """True when event matches the configured impact + currency allowlist.

    Empty allowlist = no filter on that dimension.
    """
    if impact_filter:
        wanted = {_normalize_impact(s) for s in impact_filter}
        wanted.discard(None)
        if event.impact not in wanted:
            return False
    if currencies:
        wanted_ccy = {c.strip().upper() for c in currencies if c}
        if event.country.upper() not in wanted_ccy:
            return False
    return True


# ── Finnhub ───────────────────────────────────────────────────────────────


class FinnhubClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        rate_per_min: float = 60.0,
    ):
        self.api_key = api_key or os.getenv("FINNHUB_API_KEY") or None
        if not self.api_key:
            logger.warning("finnhub_api_key_missing; "
                           "economic_calendar will fall back to other sources")
        self._client = httpx.AsyncClient(
            base_url=FINNHUB_BASE, timeout=timeout_s,
        )
        self._max_retries = max_retries
        self._rate_per_min = rate_per_min
        self._rate_tokens = rate_per_min
        self._rate_capacity = rate_per_min
        self._rate_last_refill = time.monotonic()
        self._rate_lock = asyncio.Lock()

    async def _consume_token(self, cost: int = 1) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._rate_last_refill
            refill = elapsed * (self._rate_per_min / 60.0)
            self._rate_tokens = min(
                self._rate_capacity, self._rate_tokens + refill)
            self._rate_last_refill = now
            if self._rate_tokens < cost:
                wait = (cost - self._rate_tokens) * (60.0 / self._rate_per_min)
                await asyncio.sleep(wait)
                self._rate_tokens = 0.0
            else:
                self._rate_tokens -= cost

    async def _request(self, path: str, params: dict) -> Optional[Any]:
        if not self.api_key:
            return None
        full_params = dict(params)
        full_params["token"] = self.api_key
        for attempt in range(self._max_retries):
            await self._consume_token(cost=1)
            try:
                resp = await self._client.get(path, params=full_params)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    logger.warning("finnhub_429 path={} retry_after={}",
                                   path, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code == 401:
                    logger.error("finnhub_401 invalid_api_key")
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("finnhub_request_failed path={} attempt={} err={!r}",
                               path, attempt + 1, e)
                await asyncio.sleep(1.5 ** attempt)
        return None

    async def fetch_events(
        self,
        start: date,
        end: date,
        impact_filter: Optional[list[str]] = None,
        currencies: Optional[list[str]] = None,
    ) -> Optional[list[EconomicEvent]]:
        """Pull `/calendar/economic?from=…&to=…`.

        Returns None on hard failure (lets the orchestrator distinguish
        "no events" from "couldn't reach provider"). Empty list = provider
        responded but no events matched the filter.
        """
        data = await self._request(
            "/calendar/economic",
            {"from": start.isoformat(), "to": end.isoformat()},
        )
        if data is None:
            return None
        raw_events = (data or {}).get("economicCalendar") or []
        out: list[EconomicEvent] = []
        for raw in raw_events:
            try:
                evt = self._parse_event(raw)
            except Exception as e:
                logger.debug("finnhub_event_parse_failed err={!r} raw={}", e, raw)
                continue
            if evt is None:
                continue
            if not _passes_filter(
                evt, impact_filter or [], currencies or []
            ):
                continue
            out.append(evt)
        return out

    @staticmethod
    def _parse_event(raw: dict) -> Optional[EconomicEvent]:
        impact = _normalize_impact(raw.get("impact"))
        if impact is None:
            return None
        title = (raw.get("event") or "").strip()
        country_raw = (raw.get("country") or "").strip()
        time_str = raw.get("time")
        if not title or not country_raw or not time_str:
            return None
        scheduled = _parse_finnhub_time(time_str)
        if scheduled is None:
            return None
        return EconomicEvent(
            title=title,
            country=_country_to_currency(country_raw),
            impact=impact,
            scheduled_time=scheduled,
            source="finnhub",
            forecast=_to_optional_str(raw.get("estimate")),
            previous=_to_optional_str(raw.get("prev")),
        )

    async def close(self) -> None:
        await self._client.aclose()


def _parse_finnhub_time(s: str) -> Optional[datetime]:
    """Finnhub returns ``"YYYY-MM-DD HH:MM:SS"`` in UTC."""
    s = s.strip()
    if not s:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_optional_str(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    return str(v)


# ── FairEconomy ───────────────────────────────────────────────────────────


class FairEconomyClient:
    """No-auth weekly snapshot. Refreshes infrequently — fetches both the
    current and next week's JSON to give a true 7-day lookahead regardless
    of where in the week the bot boots."""

    def __init__(
        self,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        urls: tuple[str, ...] = FAIRECONOMY_URLS,
    ):
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._max_retries = max_retries
        self._urls = urls

    async def _request_one(self, url: str) -> Optional[Any]:
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.get(url)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    logger.warning("faireconomy_429 url={} retry_after={}",
                                   url, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                # 404 on nextweek.json is normal — FairEconomy publishes it
                # mid-week, so early-week boots see it missing. Don't retry,
                # don't warn (it's expected, would just be log noise).
                if resp.status_code == 404:
                    logger.debug("faireconomy_404 url={}", url)
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("faireconomy_request_failed url={} attempt={} err={!r}",
                               url, attempt + 1, e)
                await asyncio.sleep(1.5 ** attempt)
        return None

    async def fetch_events(
        self,
        start: date,
        end: date,
        impact_filter: Optional[list[str]] = None,
        currencies: Optional[list[str]] = None,
    ) -> Optional[list[EconomicEvent]]:
        # Fetch each weekly snapshot in parallel — partial failure is fine
        # (any successful one contributes events). Hard-fail only when ALL
        # weekly URLs fail, so the orchestrator knows to flag the provider.
        results = await asyncio.gather(
            *(self._request_one(u) for u in self._urls),
            return_exceptions=True,
        )
        any_ok = False
        out: list[EconomicEvent] = []
        for url, data in zip(self._urls, results):
            if isinstance(data, Exception):
                logger.warning("faireconomy_url_exception url={} err={!r}",
                               url, data)
                continue
            if data is None:
                continue
            if not isinstance(data, list):
                logger.warning("faireconomy_unexpected_payload url={} type={}",
                               url, type(data).__name__)
                continue
            any_ok = True
            for raw in data:
                try:
                    evt = self._parse_event(raw)
                except Exception as e:
                    logger.debug("faireconomy_event_parse_failed err={!r} raw={}",
                                 e, raw)
                    continue
                if evt is None:
                    continue
                if (evt.scheduled_time.date() < start
                        or evt.scheduled_time.date() > end):
                    continue
                if not _passes_filter(
                        evt, impact_filter or [], currencies or []):
                    continue
                out.append(evt)
        if not any_ok:
            return None
        return out

    @staticmethod
    def _parse_event(raw: dict) -> Optional[EconomicEvent]:
        impact = _normalize_impact(raw.get("impact"))
        if impact is None:
            return None
        title = (raw.get("title") or "").strip()
        country = (raw.get("country") or "").strip()
        date_str = raw.get("date")
        if not title or not country or not date_str:
            return None
        scheduled = _parse_faireconomy_time(date_str)
        if scheduled is None:
            return None
        return EconomicEvent(
            title=title,
            country=country,
            impact=impact,
            scheduled_time=scheduled,
            source="faireconomy",
            forecast=_to_optional_str(raw.get("forecast")),
            previous=_to_optional_str(raw.get("previous")),
        )

    async def close(self) -> None:
        await self._client.aclose()


def _parse_faireconomy_time(s: str) -> Optional[datetime]:
    """FairEconomy publishes ISO 8601 with a UTC offset, e.g.
    ``"2026-04-17T12:30:00-04:00"``. We normalize to UTC."""
    s = s.strip()
    if not s:
        return None
    # Python 3.11+ accepts the trailing "Z" form, but FairEconomy uses
    # "+HH:MM" / "-HH:MM"; fromisoformat handles both since 3.11.
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Service ───────────────────────────────────────────────────────────────


class EconomicCalendarService:
    """Orchestrator: parallel provider fetch → dedup → in-memory cache.

    `is_in_blackout` is sync + cheap (walks the cached event list); the
    runner can call it every cycle without measurable cost. The async
    refresh loop fetches at `refresh_interval_s` (default 6h — events are
    scheduled, polling more aggressively wastes the rate budget).
    """

    def __init__(
        self,
        config: Any,                      # EconomicCalendarConfig (duck-typed)
        finnhub: Optional[FinnhubClient] = None,
        faireconomy: Optional[FairEconomyClient] = None,
    ):
        self._config = config
        self._finnhub = finnhub
        self._faireconomy = faireconomy
        self._cached_events: list[EconomicEvent] = []
        self._last_refresh: Optional[datetime] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    @property
    def cached_events(self) -> list[EconomicEvent]:
        return list(self._cached_events)

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    async def refresh(self, *, now: Optional[datetime] = None) -> int:
        """Fetch from every enabled provider, dedup, store. Returns event count.

        Both providers running in parallel; either one failing leaves the
        cache at whatever the other returned. Both failing leaves the prior
        cache untouched (logged warning).
        """
        now = now or datetime.now(tz=timezone.utc)
        start = now.date()
        end = (now + timedelta(days=int(self._config.lookahead_days))).date()
        tasks = []
        if self._finnhub is not None and getattr(self._config, "finnhub_enabled", True):
            tasks.append(("finnhub", self._finnhub.fetch_events(
                start, end,
                impact_filter=self._config.impact_filter,
                currencies=self._config.currencies,
            )))
        if self._faireconomy is not None and getattr(
                self._config, "faireconomy_enabled", True):
            tasks.append(("faireconomy", self._faireconomy.fetch_events(
                start, end,
                impact_filter=self._config.impact_filter,
                currencies=self._config.currencies,
            )))
        if not tasks:
            logger.warning("economic_calendar_no_providers_enabled")
            return len(self._cached_events)

        results = await asyncio.gather(
            *(t for _, t in tasks), return_exceptions=True)

        merged: list[EconomicEvent] = []
        any_ok = False
        for (name, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.warning("economic_calendar_provider_failed "
                               "provider={} err={!r}", name, result)
                continue
            if result is None:
                logger.info("economic_calendar_provider_no_data provider={}", name)
                continue
            any_ok = True
            merged.extend(result)

        if not any_ok:
            logger.warning("economic_calendar_all_providers_failed; "
                           "keeping {} cached events ({})",
                           len(self._cached_events),
                           "stale" if self._last_refresh else "empty")
            return len(self._cached_events)

        deduped = self._dedup_events(merged)
        deduped.sort(key=lambda e: e.scheduled_time)
        self._cached_events = deduped
        self._last_refresh = now
        logger.info(
            "economic_calendar_refreshed events={} window=[{}..{}] sources={}",
            len(deduped), start, end,
            ",".join(name for (name, _), r in zip(tasks, results)
                     if not isinstance(r, Exception) and r is not None),
        )
        return len(deduped)

    @staticmethod
    def _dedup_events(events: list[EconomicEvent]) -> list[EconomicEvent]:
        """Group by (normalized title, country, ±_DEDUP_WINDOW_S window).

        Cluster representative keeps the higher impact (HIGH > MEDIUM > LOW)
        and earlier scheduled_time (more conservative blackout). Source is
        joined as ``"finnhub+faireconomy"``.
        """
        # Sort by scheduled_time first so window-based grouping is monotonic.
        events_sorted = sorted(events, key=lambda e: e.scheduled_time)
        impact_rank = {
            EconomicEventImpact.HIGH: 3,
            EconomicEventImpact.MEDIUM: 2,
            EconomicEventImpact.LOW: 1,
        }
        clusters: list[list[EconomicEvent]] = []
        for evt in events_sorted:
            placed = False
            for cluster in clusters:
                head = cluster[0]
                if (
                    _normalize_title(head.title) == _normalize_title(evt.title)
                    and head.country.upper() == evt.country.upper()
                    and abs((head.scheduled_time - evt.scheduled_time)
                            .total_seconds()) <= _DEDUP_WINDOW_S
                ):
                    cluster.append(evt)
                    placed = True
                    break
            if not placed:
                clusters.append([evt])

        out: list[EconomicEvent] = []
        for cluster in clusters:
            if len(cluster) == 1:
                out.append(cluster[0])
                continue
            best = max(
                cluster,
                key=lambda e: (impact_rank.get(e.impact, 0),
                               -e.scheduled_time.timestamp()),
            )
            sources = sorted({e.source for e in cluster})
            merged = best.model_copy(update={"source": "+".join(sources)})
            out.append(merged)
        return out

    def is_in_blackout(self, now: datetime) -> BlackoutInfo:
        """Walk cached events; return the most-relevant blackout, if any.

        "Most relevant" = active blackout where the event itself is closest
        to ``now`` (smaller |delta| wins). Returns ``active=False`` when no
        cached event is inside the window.
        """
        if not self._cached_events:
            return BlackoutInfo(active=False, reason="no_cached_events")
        before_s = int(self._config.blackout_minutes_before) * 60
        after_s = int(self._config.blackout_minutes_after) * 60
        best: Optional[EconomicEvent] = None
        best_abs_delta: float = float("inf")
        for evt in self._cached_events:
            delta_s = (evt.scheduled_time - now).total_seconds()
            # Inside [-after, +before]:
            #   delta_s > 0 → event is in the future, blackout starts at
            #     `delta_s <= before_s`.
            #   delta_s < 0 → event has passed, blackout active while
            #     `-delta_s <= after_s`.
            if delta_s >= 0 and delta_s > before_s:
                continue
            if delta_s < 0 and -delta_s > after_s:
                continue
            if abs(delta_s) < best_abs_delta:
                best_abs_delta = abs(delta_s)
                best = evt
        if best is None:
            return BlackoutInfo(active=False, reason="no_event_in_window")
        delta_s = (best.scheduled_time - now).total_seconds()
        return BlackoutInfo(
            active=True,
            event=best,
            seconds_until_event=int(delta_s) if delta_s >= 0 else None,
            seconds_after_event=int(-delta_s) if delta_s < 0 else None,
            reason="event_within_window",
        )

    def next_event(self, now: datetime) -> Optional[EconomicEvent]:
        """Closest upcoming event (operator visibility / log line)."""
        future = [e for e in self._cached_events if e.scheduled_time >= now]
        if not future:
            return None
        return min(future, key=lambda e: e.scheduled_time)

    async def run_refresh_loop(self, stop_event: asyncio.Event) -> None:
        """Background task: refresh on `refresh_interval_s` cadence until
        `stop_event` is set."""
        self._stop_event = stop_event
        interval = max(60, int(self._config.refresh_interval_s))
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                # If wait returns cleanly, stop_event is set → exit.
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.refresh()
            except Exception:
                logger.exception("economic_calendar_refresh_loop_failed")

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        for client in (self._finnhub, self._faireconomy):
            if client is None:
                continue
            try:
                await client.close()
            except Exception:
                logger.exception("economic_calendar_client_close_failed")
