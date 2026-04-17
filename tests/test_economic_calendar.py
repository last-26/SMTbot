"""Unit tests for src/data/economic_calendar.py.

We never hit the real Finnhub or FairEconomy network — `httpx.AsyncClient`
inside each client is replaced with a fake whose `get()` is scripted per
test. Pattern mirrors `tests/test_derivatives_api.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from src.data.economic_calendar import (
    BlackoutInfo,
    EconomicCalendarService,
    EconomicEvent,
    EconomicEventImpact,
    FairEconomyClient,
    FinnhubClient,
)


# ── Helpers ───────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200,
                 headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[_FakeResponse] = []
        self.raise_exc: Exception | None = None

    def queue(self, resp: _FakeResponse) -> None:
        self.responses.append(resp)

    async def get(self, path: str, params: dict | None = None) -> _FakeResponse:
        self.calls.append((path, dict(params or {})))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse({}, status_code=200)

    async def aclose(self) -> None:
        pass


def _make_finnhub(api_key: str = "test-key") -> FinnhubClient:
    c = FinnhubClient(api_key=api_key)
    c._client = _FakeClient()       # type: ignore
    return c


def _make_faireconomy() -> FairEconomyClient:
    c = FairEconomyClient()
    c._client = _FakeClient()       # type: ignore
    return c


def _no_sleep(monkeypatch):
    async def fake(_):
        return None
    import src.data.economic_calendar as mod
    monkeypatch.setattr(mod.asyncio, "sleep", fake)


def _cfg(**overrides) -> Any:
    """Duck-typed config object the service expects."""
    from types import SimpleNamespace
    defaults = dict(
        enabled=True,
        finnhub_enabled=True,
        faireconomy_enabled=True,
        blackout_minutes_before=30,
        blackout_minutes_after=15,
        impact_filter=["High"],
        currencies=["USD"],
        refresh_interval_s=21600,
        lookahead_days=7,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── FinnhubClient — construction + auth ───────────────────────────────────


def test_finnhub_missing_api_key_constructs_warns():
    c = FinnhubClient(api_key="")
    assert c.api_key is None or c.api_key == ""


@pytest.mark.asyncio
async def test_finnhub_missing_api_key_returns_none():
    c = FinnhubClient(api_key="")
    c.api_key = None
    res = await c.fetch_events(date(2026, 4, 17), date(2026, 4, 24))
    assert res is None


@pytest.mark.asyncio
async def test_finnhub_401_returns_none_no_retry(monkeypatch):
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({}, status_code=401))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(date(2026, 4, 17), date(2026, 4, 24))
    assert res is None
    assert len(fc.calls) == 1


@pytest.mark.asyncio
async def test_finnhub_429_honors_retry_after(monkeypatch):
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({}, status_code=429, headers={"Retry-After": "2"}))
    fc.queue(_FakeResponse({"economicCalendar": []}, status_code=200))
    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    import src.data.economic_calendar as mod
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    res = await c.fetch_events(date(2026, 4, 17), date(2026, 4, 24))
    assert res == []
    assert 2.0 in slept


# ── FinnhubClient — parsing + filters ─────────────────────────────────────


@pytest.mark.asyncio
async def test_finnhub_parses_high_impact_usd_event(monkeypatch):
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({"economicCalendar": [
        {"event": "CPI YoY", "country": "US", "time": "2026-04-22 12:30:00",
         "impact": "high", "estimate": "3.1", "prev": "3.2"},
        {"event": "Some Low Event", "country": "US",
         "time": "2026-04-22 14:00:00", "impact": "low"},
    ]}, status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"], currencies=["USD"],
    )
    assert res is not None
    assert len(res) == 1
    evt = res[0]
    assert evt.title == "CPI YoY"
    # ISO-2 'US' is normalized to currency 'USD' at parse time so it
    # aligns with FairEconomy's currency-coded events for filter+dedup.
    assert evt.country == "USD"
    assert evt.impact is EconomicEventImpact.HIGH
    assert evt.scheduled_time == datetime(
        2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    assert evt.source == "finnhub"
    assert evt.forecast == "3.1"
    assert evt.previous == "3.2"


@pytest.mark.asyncio
async def test_finnhub_currency_filter_drops_non_match(monkeypatch):
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({"economicCalendar": [
        {"event": "ECB Rate Decision", "country": "EU",
         "time": "2026-04-22 12:30:00", "impact": "high"},
        {"event": "FOMC Statement", "country": "US",
         "time": "2026-04-22 18:00:00", "impact": "high"},
    ]}, status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"], currencies=["USD"],
    )
    assert [e.title for e in res] == ["FOMC Statement"]


@pytest.mark.asyncio
async def test_finnhub_skips_unparseable_rows(monkeypatch):
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({"economicCalendar": [
        {"event": "", "country": "US", "time": "2026-04-22 12:30:00",
         "impact": "high"},                                 # missing title
        {"event": "Good Event", "country": "US",
         "time": "not-a-date", "impact": "high"},           # bad time
        {"event": "Valid", "country": "US",
         "time": "2026-04-22 12:30:00", "impact": "high"},
    ]}, status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"], currencies=["USD"],
    )
    assert [e.title for e in res] == ["Valid"]


@pytest.mark.asyncio
async def test_finnhub_country_iso2_normalized_to_currency(monkeypatch):
    """Finnhub uses ISO-2 country codes (US, GB, JP); FairEconomy uses
    currency codes (USD, GBP, JPY). Parse-time normalization aligns the
    two so config filters + dedup compare apples-to-apples.
    """
    c = _make_finnhub()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({"economicCalendar": [
        {"event": "FOMC Statement", "country": "US",
         "time": "2026-04-22 18:00:00", "impact": "high"},
        {"event": "BoE Rate Decision", "country": "GB",
         "time": "2026-04-22 11:00:00", "impact": "high"},
        {"event": "ECB Rate Decision", "country": "EU",
         "time": "2026-04-22 12:00:00", "impact": "high"},
        {"event": "BoJ Rate Decision", "country": "JP",
         "time": "2026-04-22 03:00:00", "impact": "high"},
    ]}, status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"],
    )
    assert res is not None
    by_title = {e.title: e.country for e in res}
    assert by_title == {
        "FOMC Statement": "USD",
        "BoE Rate Decision": "GBP",
        "ECB Rate Decision": "EUR",
        "BoJ Rate Decision": "JPY",
    }


# ── FairEconomyClient — parsing + failure ─────────────────────────────────


@pytest.mark.asyncio
async def test_faireconomy_parses_high_impact_usd(monkeypatch):
    c = _make_faireconomy()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse([
        {"title": "CPI m/m", "country": "USD", "impact": "High",
         "date": "2026-04-22T12:30:00-04:00",
         "forecast": "0.3%", "previous": "0.4%"},
        {"title": "Low Event", "country": "USD", "impact": "Low",
         "date": "2026-04-22T13:00:00-04:00"},
    ], status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"], currencies=["USD"],
    )
    assert res is not None
    assert len(res) == 1
    evt = res[0]
    assert evt.title == "CPI m/m"
    assert evt.impact is EconomicEventImpact.HIGH
    # -04:00 → UTC: 12:30 + 4h = 16:30
    assert evt.scheduled_time == datetime(
        2026, 4, 22, 16, 30, tzinfo=timezone.utc)
    assert evt.source == "faireconomy"


@pytest.mark.asyncio
async def test_faireconomy_returns_none_on_all_failures(monkeypatch):
    c = _make_faireconomy()
    fc: _FakeClient = c._client      # type: ignore
    fc.raise_exc = RuntimeError("boom")
    _no_sleep(monkeypatch)
    res = await c.fetch_events(date(2026, 4, 17), date(2026, 4, 24))
    assert res is None
    # 2 URLs (thisweek + nextweek) × 3 retries each = 6 attempts.
    assert len(fc.calls) == 6


@pytest.mark.asyncio
async def test_faireconomy_returns_none_on_unexpected_payload(monkeypatch):
    c = _make_faireconomy()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse({"not": "a list"}, status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(date(2026, 4, 17), date(2026, 4, 24))
    assert res is None


@pytest.mark.asyncio
async def test_faireconomy_drops_events_outside_window(monkeypatch):
    c = _make_faireconomy()
    fc: _FakeClient = c._client      # type: ignore
    fc.queue(_FakeResponse([
        {"title": "Past Event", "country": "USD", "impact": "High",
         "date": "2026-04-10T12:30:00+00:00"},                # before start
        {"title": "Future Event", "country": "USD", "impact": "High",
         "date": "2026-04-30T12:30:00+00:00"},                # after end
        {"title": "In Window", "country": "USD", "impact": "High",
         "date": "2026-04-22T12:30:00+00:00"},
    ], status_code=200))
    _no_sleep(monkeypatch)
    res = await c.fetch_events(
        date(2026, 4, 17), date(2026, 4, 24),
        impact_filter=["High"], currencies=["USD"],
    )
    assert [e.title for e in res] == ["In Window"]


# ── Service — refresh + dedup ─────────────────────────────────────────────


def _evt(title: str, ts: datetime, source: str,
         impact: EconomicEventImpact = EconomicEventImpact.HIGH,
         country: str = "USD") -> EconomicEvent:
    return EconomicEvent(
        title=title, country=country, impact=impact,
        scheduled_time=ts, source=source,
    )


class _StubFinnhub:
    def __init__(self, events: list[EconomicEvent] | None = None,
                 raise_exc: Exception | None = None):
        self.events = events
        self.raise_exc = raise_exc
        self.closed = False

    async def fetch_events(self, start, end, impact_filter=None, currencies=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.events) if self.events is not None else None

    async def close(self) -> None:
        self.closed = True


class _StubFairEconomy(_StubFinnhub):
    pass


@pytest.mark.asyncio
async def test_service_dedup_merges_same_event_from_two_providers():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    fh = _StubFinnhub([_evt("CPI YoY", when, "finnhub")])
    fe = _StubFairEconomy([_evt("CPI YoY", when, "faireconomy")])
    svc = EconomicCalendarService(_cfg(), finnhub=fh, faireconomy=fe)

    n = await svc.refresh(now=datetime(2026, 4, 17, tzinfo=timezone.utc))
    assert n == 1
    [merged] = svc.cached_events
    assert merged.title == "CPI YoY"
    assert merged.source == "faireconomy+finnhub"


@pytest.mark.asyncio
async def test_service_dedup_window_groups_near_duplicates():
    base = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    from datetime import timedelta as _td
    fh = _StubFinnhub([_evt("CPI YoY", base, "finnhub")])
    fe = _StubFairEconomy([_evt("CPI YoY", base + _td(minutes=10), "faireconomy")])
    svc = EconomicCalendarService(_cfg(), finnhub=fh, faireconomy=fe)
    await svc.refresh(now=base)
    assert len(svc.cached_events) == 1


@pytest.mark.asyncio
async def test_service_dedup_keeps_separate_when_outside_window():
    base = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    from datetime import timedelta as _td
    fh = _StubFinnhub([_evt("CPI YoY", base, "finnhub")])
    fe = _StubFairEconomy([_evt("CPI YoY", base + _td(minutes=30), "faireconomy")])
    svc = EconomicCalendarService(_cfg(), finnhub=fh, faireconomy=fe)
    await svc.refresh(now=base)
    assert len(svc.cached_events) == 2


@pytest.mark.asyncio
async def test_service_partial_failure_uses_remaining_provider():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    fh = _StubFinnhub(raise_exc=RuntimeError("network"))
    fe = _StubFairEconomy([_evt("FOMC", when, "faireconomy")])
    svc = EconomicCalendarService(_cfg(), finnhub=fh, faireconomy=fe)

    n = await svc.refresh(now=datetime(2026, 4, 17, tzinfo=timezone.utc))
    assert n == 1
    assert svc.cached_events[0].source == "faireconomy"


@pytest.mark.asyncio
async def test_service_total_failure_keeps_prior_cache():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    fh = _StubFinnhub([_evt("Old Event", when, "finnhub")])
    fe = _StubFairEconomy([])     # returns []
    svc = EconomicCalendarService(_cfg(), finnhub=fh, faireconomy=fe)
    await svc.refresh(now=datetime(2026, 4, 17, tzinfo=timezone.utc))
    assert len(svc.cached_events) == 1     # baseline cached

    # Now both providers fail.
    fh.events = None
    fh.raise_exc = RuntimeError("a")
    fe.raise_exc = RuntimeError("b")
    n = await svc.refresh(now=datetime(2026, 4, 17, tzinfo=timezone.utc))
    assert n == 1                          # cache preserved
    assert svc.cached_events[0].title == "Old Event"


# ── Service — blackout window math ────────────────────────────────────────


def _svc_with_event(scheduled_at: datetime,
                    minutes_before: int = 30,
                    minutes_after: int = 15) -> EconomicCalendarService:
    cfg = _cfg(blackout_minutes_before=minutes_before,
               blackout_minutes_after=minutes_after)
    svc = EconomicCalendarService(cfg)
    svc._cached_events = [_evt("CPI", scheduled_at, "finnhub")]
    return svc


def test_blackout_inactive_before_window():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    svc = _svc_with_event(when, minutes_before=30, minutes_after=15)
    # 31 minutes before → outside before-window
    now = datetime(2026, 4, 22, 11, 59, tzinfo=timezone.utc)
    info = svc.is_in_blackout(now)
    assert info.active is False


def test_blackout_active_just_inside_before_window():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    svc = _svc_with_event(when, minutes_before=30, minutes_after=15)
    # 29 minutes before → inside before-window
    now = datetime(2026, 4, 22, 12, 1, tzinfo=timezone.utc)
    info = svc.is_in_blackout(now)
    assert info.active is True
    assert info.event is not None and info.event.title == "CPI"
    assert info.seconds_until_event == 29 * 60
    assert info.seconds_after_event is None


def test_blackout_active_just_inside_after_window():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    svc = _svc_with_event(when, minutes_before=30, minutes_after=15)
    # 14 minutes after → inside after-window
    now = datetime(2026, 4, 22, 12, 44, tzinfo=timezone.utc)
    info = svc.is_in_blackout(now)
    assert info.active is True
    assert info.seconds_until_event is None
    assert info.seconds_after_event == 14 * 60


def test_blackout_inactive_after_window():
    when = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    svc = _svc_with_event(when, minutes_before=30, minutes_after=15)
    # 16 minutes after → outside after-window
    now = datetime(2026, 4, 22, 12, 46, tzinfo=timezone.utc)
    info = svc.is_in_blackout(now)
    assert info.active is False


def test_blackout_inactive_when_no_cached_events():
    svc = EconomicCalendarService(_cfg())
    info = svc.is_in_blackout(datetime(2026, 4, 17, tzinfo=timezone.utc))
    assert info.active is False
    assert isinstance(info, BlackoutInfo)


def test_blackout_picks_closest_event_when_multiple_in_window():
    near = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    far = datetime(2026, 4, 22, 12, 55, tzinfo=timezone.utc)
    cfg = _cfg(blackout_minutes_before=60, blackout_minutes_after=15)
    svc = EconomicCalendarService(cfg)
    svc._cached_events = [
        _evt("Far Event", far, "finnhub"),
        _evt("Near Event", near, "finnhub"),
    ]
    # Now 12:25 — both events are in the future, "Near Event" is closer.
    now = datetime(2026, 4, 22, 12, 25, tzinfo=timezone.utc)
    info = svc.is_in_blackout(now)
    assert info.active is True
    assert info.event is not None
    assert info.event.title == "Near Event"


# ── Service — next_event ──────────────────────────────────────────────────


def test_next_event_returns_closest_future():
    e1 = _evt("CPI", datetime(2026, 4, 22, tzinfo=timezone.utc), "finnhub")
    e2 = _evt("FOMC", datetime(2026, 4, 24, tzinfo=timezone.utc), "finnhub")
    svc = EconomicCalendarService(_cfg())
    svc._cached_events = [e2, e1]
    nxt = svc.next_event(datetime(2026, 4, 21, tzinfo=timezone.utc))
    assert nxt is not None and nxt.title == "CPI"


def test_next_event_returns_none_when_all_past():
    e1 = _evt("CPI", datetime(2026, 4, 1, tzinfo=timezone.utc), "finnhub")
    svc = EconomicCalendarService(_cfg())
    svc._cached_events = [e1]
    assert svc.next_event(datetime(2026, 4, 22, tzinfo=timezone.utc)) is None


# ── Config integration ────────────────────────────────────────────────────


def test_config_loads_economic_calendar_section_from_env(monkeypatch, tmp_path):
    """End-to-end: YAML + FINNHUB_API_KEY env → BotConfig.economic_calendar."""
    import yaml
    cfg_yaml = {
        "bot": {"mode": "demo", "starting_balance": 1000.0},
        "trading": {
            "symbols": ["BTC-USDT-SWAP"], "entry_timeframe": "3m",
            "htf_timeframe": "15m", "risk_per_trade_pct": 1.0,
            "max_leverage": 10, "default_rr_ratio": 3.0,
            "min_rr_ratio": 2.0, "max_concurrent_positions": 1,
        },
        "analysis": {
            "min_confluence_score": 2, "candle_buffer_size": 500,
            "swing_lookback": 20, "sr_min_touches": 3,
            "sr_zone_atr_mult": 0.5,
        },
        "okx": {"demo_flag": "1"},
        "economic_calendar": {
            "enabled": True,
            "blackout_minutes_before": 45,
            "impact_filter": ["High", "Medium"],
            "currencies": ["USD", "EUR"],
        },
    }
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg_yaml))
    monkeypatch.setenv("OKX_API_KEY", "x")
    monkeypatch.setenv("OKX_API_SECRET", "y")
    monkeypatch.setenv("OKX_PASSPHRASE", "z")
    monkeypatch.setenv("FINNHUB_API_KEY", "from-env-key")

    from src.bot.config import load_config
    cfg = load_config(str(yaml_path), env_path="/nonexistent.env")
    assert cfg.economic_calendar.enabled is True
    assert cfg.economic_calendar.blackout_minutes_before == 45
    assert cfg.economic_calendar.impact_filter == ["High", "Medium"]
    assert cfg.economic_calendar.currencies == ["USD", "EUR"]
    assert cfg.economic_calendar.finnhub_api_key == "from-env-key"
