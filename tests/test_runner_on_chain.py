"""Tests for BotRunner's Arkham on-chain scheduler + context helper (Phase B)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from src.bot.config import BotConfig
from src.bot.runner import BotContext, BotRunner
from src.data.on_chain_types import OnChainSnapshot, WhaleBlackoutState
from src.journal.database import TradeJournal
from src.strategy.risk_manager import RiskManager
from tests.conftest import (
    FakeMonitor,
    FakeOKXClient,
    FakeReader,
    FakeRouter,
    make_config,
)

UTC = timezone.utc


class _FakeArkhamClient:
    """Minimal shape the runner scheduler touches.

    - `hard_disabled` attribute (read by the scheduler short-circuit).
    - Does NOT expose the actual HTTP path; scheduler calls the module-
      level fetcher functions, which we monkeypatch at the `on_chain`
      module boundary so this fake never has to speak the Arkham
      protocol.
    """

    def __init__(self, hard_disabled: bool = False) -> None:
        self.hard_disabled = hard_disabled

    async def close(self) -> None:
        pass


def _make_on_chain_cfg(**overrides) -> BotConfig:
    """BotConfig with `on_chain.enabled=True` plus overrides."""
    cfg = make_config()
    cfg_dict = cfg.model_dump()
    cfg_dict.setdefault("on_chain", {})
    cfg_dict["on_chain"]["enabled"] = True
    cfg_dict["on_chain"].update(overrides)
    return BotConfig(**cfg_dict)


def _make_runner(cfg: BotConfig, arkham_client: Any) -> BotRunner:
    ctx = BotContext(
        reader=FakeReader(),
        multi_tf=None,
        journal=TradeJournal(":memory:"),
        router=FakeRouter(),
        monitor=FakeMonitor(),
        risk_mgr=RiskManager(cfg.bot.starting_balance, cfg.breakers()),
        okx_client=FakeOKXClient(),
        config=cfg,
    )
    ctx.arkham_client = arkham_client
    ctx.whale_blackout_state = WhaleBlackoutState()
    return BotRunner(ctx)


# ── _refresh_on_chain_snapshots ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_noop_when_master_disabled(monkeypatch):
    cfg = make_config()  # master off by default
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())

    async def _unreachable(*a, **kw):
        raise AssertionError("fetch should not be called when master is off")

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _unreachable)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _unreachable)

    await runner._refresh_on_chain_snapshots()
    assert runner.ctx.on_chain_snapshot is None
    assert runner.ctx.stablecoin_pulse_1h_usd is None


@pytest.mark.asyncio
async def test_refresh_noop_when_client_is_none(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=None)

    async def _unreachable(*a, **kw):
        raise AssertionError("fetch should not be called when client is None")

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _unreachable)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _unreachable)

    await runner._refresh_on_chain_snapshots()
    assert runner.ctx.on_chain_snapshot is None


@pytest.mark.asyncio
async def test_refresh_noop_when_client_hard_disabled(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient(hard_disabled=True))

    calls: list[str] = []

    async def _daily(*a, **kw):
        calls.append("daily")

    async def _pulse(*a, **kw):
        calls.append("pulse")

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    await runner._refresh_on_chain_snapshots()
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_daily_fetches_once_per_utc_day(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())

    daily_calls: list[int] = []
    pulse_calls: list[int] = []

    async def _daily(client, **kw):
        daily_calls.append(1)
        return OnChainSnapshot(
            daily_macro_bias="bullish",
            stablecoin_pulse_1h_usd=None,
            cex_btc_netflow_24h_usd=-100_000_000.0,
            cex_eth_netflow_24h_usd=-50_000_000.0,
            snapshot_age_s=0,
            stale_threshold_s=kw["stale_threshold_s"],
        )

    async def _pulse(client, **kw):
        pulse_calls.append(1)
        return 75_000_000.0

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    await runner._refresh_on_chain_snapshots()
    assert len(daily_calls) == 1
    assert runner.ctx.on_chain_snapshot is not None
    assert runner.ctx.on_chain_snapshot.daily_macro_bias == "bullish"
    assert runner.ctx.on_chain_snapshot.stablecoin_pulse_1h_usd == 75_000_000.0
    assert runner.ctx.last_on_chain_daily_date == datetime.now(tz=UTC).date()

    # Second call on the same day — daily must NOT refetch. Pulse may
    # re-fire depending on monotonic clock, but both together prove the
    # daily cache is honored.
    await runner._refresh_on_chain_snapshots()
    assert len(daily_calls) == 1  # still 1


@pytest.mark.asyncio
async def test_refresh_daily_refetches_on_utc_day_rollover(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())

    daily_calls: list[int] = []

    async def _daily(client, **kw):
        daily_calls.append(1)
        return OnChainSnapshot(
            daily_macro_bias="bullish",
            stablecoin_pulse_1h_usd=None,
            snapshot_age_s=0, stale_threshold_s=7200,
        )

    async def _pulse(client, **kw):
        return 1.0

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    # Pretend we already fetched on a prior day.
    yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).date()
    runner.ctx.last_on_chain_daily_date = yesterday

    await runner._refresh_on_chain_snapshots()
    assert len(daily_calls) == 1
    assert runner.ctx.last_on_chain_daily_date == datetime.now(tz=UTC).date()


@pytest.mark.asyncio
async def test_refresh_pulse_respects_refresh_cadence(monkeypatch):
    cfg = _make_on_chain_cfg(stablecoin_pulse_refresh_s=3600)
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())

    pulse_calls: list[int] = []

    async def _daily(client, **kw):
        return OnChainSnapshot(
            daily_macro_bias="neutral",
            stablecoin_pulse_1h_usd=None,
            snapshot_age_s=0, stale_threshold_s=7200,
        )

    async def _pulse(client, **kw):
        pulse_calls.append(1)
        return 10_000_000.0

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    # First tick — both daily + pulse fire.
    await runner._refresh_on_chain_snapshots()
    assert len(pulse_calls) == 1

    # Second tick immediately — pulse skips (elapsed < refresh_s).
    await runner._refresh_on_chain_snapshots()
    assert len(pulse_calls) == 1

    # Rewind the monotonic bookkeeping so the next tick is "past
    # refresh_s ago" — pulse fires again.
    runner.ctx.last_on_chain_pulse_ts = runner.ctx.last_on_chain_pulse_ts - 3601.0
    await runner._refresh_on_chain_snapshots()
    assert len(pulse_calls) == 2


@pytest.mark.asyncio
async def test_refresh_daily_failure_keeps_previous_snapshot(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())

    # Seed a previous snapshot — simulates "last fetch was yesterday".
    prev = OnChainSnapshot(
        daily_macro_bias="bullish",
        stablecoin_pulse_1h_usd=10.0,
        snapshot_age_s=100,
        stale_threshold_s=7200,
    )
    runner.ctx.on_chain_snapshot = prev
    runner.ctx.last_on_chain_daily_date = (
        datetime.now(tz=UTC) - timedelta(days=1)
    ).date()

    async def _daily_fails(client, **kw):
        return None  # matches the fetcher's "failure → None" contract

    async def _pulse_fails(client, **kw):
        return None

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily_fails)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse_fails)

    await runner._refresh_on_chain_snapshots()
    # Previous snapshot preserved because failure doesn't overwrite.
    assert runner.ctx.on_chain_snapshot is prev


# ── _on_chain_context_dict ─────────────────────────────────────────────────


def test_context_dict_returns_none_when_master_off():
    cfg = make_config()
    runner = _make_runner(cfg, arkham_client=None)
    assert runner._on_chain_context_dict() is None


def test_context_dict_returns_none_when_snapshot_missing():
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())
    assert runner._on_chain_context_dict() is None


def test_context_dict_populated_from_snapshot():
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())
    runner.ctx.on_chain_snapshot = OnChainSnapshot(
        daily_macro_bias="bullish",
        stablecoin_pulse_1h_usd=75_000_000.0,
        cex_btc_netflow_24h_usd=-120_000_000.0,
        cex_eth_netflow_24h_usd=-50_000_000.0,
        coinbase_asia_skew_usd=20_000_000.0,
        bnb_self_flow_24h_usd=-5_000_000.0,
        snapshot_age_s=300,
        stale_threshold_s=7200,
    )
    d = runner._on_chain_context_dict()
    assert d is not None
    assert d["daily_macro_bias"] == "bullish"
    assert d["stablecoin_pulse_1h_usd"] == 75_000_000.0
    assert d["cex_btc_netflow_24h_usd"] == -120_000_000.0
    assert d["cex_eth_netflow_24h_usd"] == -50_000_000.0
    assert d["snapshot_age_s"] == 300
    assert d["fresh"] is True
    assert d["whale_blackout_active"] is False


def test_context_dict_whale_blackout_active_flag_reflects_state():
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())
    runner.ctx.on_chain_snapshot = OnChainSnapshot(
        daily_macro_bias="neutral",
        snapshot_age_s=0,
        stale_threshold_s=7200,
    )

    # Empty state → blackout_active False.
    assert runner._on_chain_context_dict()["whale_blackout_active"] is False

    # Active blackout far in the future → True.
    future_ms = int((datetime.now(tz=UTC).timestamp() + 3600) * 1000)
    runner.ctx.whale_blackout_state.set_blackout("BTC-USDT-SWAP", future_ms)
    assert runner._on_chain_context_dict()["whale_blackout_active"] is True

    # Expired blackout (far in the past) → False.
    past_ms = int((datetime.now(tz=UTC).timestamp() - 3600) * 1000)
    runner.ctx.whale_blackout_state.blackouts["BTC-USDT-SWAP"] = past_ms
    assert runner._on_chain_context_dict()["whale_blackout_active"] is False


def test_context_dict_preserves_none_optional_fields():
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())
    runner.ctx.on_chain_snapshot = OnChainSnapshot(
        daily_macro_bias="neutral",
        stablecoin_pulse_1h_usd=None,       # not yet fetched
        cex_btc_netflow_24h_usd=None,       # daily API failed
        snapshot_age_s=0,
        stale_threshold_s=7200,
    )
    d = runner._on_chain_context_dict()
    assert d["stablecoin_pulse_1h_usd"] is None
    assert d["cex_btc_netflow_24h_usd"] is None
    assert d["daily_macro_bias"] == "neutral"


# ── _stop_on_chain ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_on_chain_closes_client():
    cfg = _make_on_chain_cfg()
    client = _FakeArkhamClient()

    closed = []

    class _ObservableFake(_FakeArkhamClient):
        async def close(self) -> None:
            closed.append(1)

    client = _ObservableFake()
    runner = _make_runner(cfg, arkham_client=client)
    await runner._stop_on_chain()
    assert closed == [1]


@pytest.mark.asyncio
async def test_stop_on_chain_noop_when_client_absent():
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=None)
    # Must not raise.
    await runner._stop_on_chain()


@pytest.mark.asyncio
async def test_stop_on_chain_swallows_close_exception():
    cfg = _make_on_chain_cfg()

    class _RaisesOnClose:
        hard_disabled = False

        async def close(self) -> None:
            raise RuntimeError("network down")

    runner = _make_runner(cfg, arkham_client=_RaisesOnClose())
    # Must not propagate.
    await runner._stop_on_chain()


# ── on_chain_snapshots journal write (2026-04-21 eve late) ─────────────────


async def _make_runner_with_connected_journal(
    cfg: BotConfig, arkham_client: Any
) -> BotRunner:
    """Variant of `_make_runner` that connects the in-memory journal so the
    snapshot-row writer can actually exercise the SQLite layer."""
    runner = _make_runner(cfg, arkham_client=arkham_client)
    await runner.ctx.journal.connect()
    return runner


@pytest.mark.asyncio
async def test_refresh_writes_snapshot_row_on_first_fetch(monkeypatch):
    cfg = _make_on_chain_cfg()
    runner = await _make_runner_with_connected_journal(
        cfg, arkham_client=_FakeArkhamClient(),
    )

    async def _daily(client, **kw):
        return OnChainSnapshot(
            daily_macro_bias="bullish",
            stablecoin_pulse_1h_usd=None,
            cex_btc_netflow_24h_usd=-100_000_000.0,
            cex_eth_netflow_24h_usd=-50_000_000.0,
            snapshot_age_s=0, stale_threshold_s=7200,
        )

    async def _pulse(client, **kw):
        return 75_000_000.0

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    await runner._refresh_on_chain_snapshots()
    rows = await runner.ctx.journal.list_on_chain_snapshots()
    assert len(rows) == 1
    assert rows[0]["daily_macro_bias"] == "bullish"
    assert rows[0]["stablecoin_pulse_1h_usd"] == 75_000_000.0
    assert runner.ctx.last_on_chain_snapshot_fingerprint is not None
    await runner.ctx.journal.close()


@pytest.mark.asyncio
async def test_refresh_dedups_unchanged_snapshot(monkeypatch):
    cfg = _make_on_chain_cfg(stablecoin_pulse_refresh_s=3600)
    runner = await _make_runner_with_connected_journal(
        cfg, arkham_client=_FakeArkhamClient(),
    )

    async def _daily(client, **kw):
        return OnChainSnapshot(
            daily_macro_bias="neutral",
            stablecoin_pulse_1h_usd=None,
            snapshot_age_s=0, stale_threshold_s=7200,
        )

    async def _pulse(client, **kw):
        return 10_000_000.0

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    # First tick — both fetches fire, one row written.
    await runner._refresh_on_chain_snapshots()
    # Second tick — daily cached (same UTC day), pulse cooldown not elapsed.
    # Fingerprint unchanged → no new journal row.
    await runner._refresh_on_chain_snapshots()
    await runner._refresh_on_chain_snapshots()
    rows = await runner.ctx.journal.list_on_chain_snapshots()
    assert len(rows) == 1, "unchanged snapshot must not churn the table"
    await runner.ctx.journal.close()


@pytest.mark.asyncio
async def test_refresh_writes_new_row_when_pulse_changes(monkeypatch):
    cfg = _make_on_chain_cfg(stablecoin_pulse_refresh_s=3600)
    runner = await _make_runner_with_connected_journal(
        cfg, arkham_client=_FakeArkhamClient(),
    )

    async def _daily(client, **kw):
        return OnChainSnapshot(
            daily_macro_bias="bullish",
            stablecoin_pulse_1h_usd=None,
            snapshot_age_s=0, stale_threshold_s=7200,
        )

    pulse_values = iter([10_000_000.0, 80_000_000.0])

    async def _pulse(client, **kw):
        return next(pulse_values)

    monkeypatch.setattr("src.bot.runner.fetch_daily_snapshot", _daily)
    monkeypatch.setattr("src.bot.runner.fetch_hourly_stablecoin_pulse", _pulse)

    # First tick — daily + pulse=10M.
    await runner._refresh_on_chain_snapshots()
    # Rewind the cooldown so pulse fetches again with a new value.
    runner.ctx.last_on_chain_pulse_ts -= 3601.0
    await runner._refresh_on_chain_snapshots()

    rows = await runner.ctx.journal.list_on_chain_snapshots()
    assert len(rows) == 2
    assert rows[0]["stablecoin_pulse_1h_usd"] == 10_000_000.0
    assert rows[1]["stablecoin_pulse_1h_usd"] == 80_000_000.0
    await runner.ctx.journal.close()


@pytest.mark.asyncio
async def test_refresh_snapshot_journal_skipped_when_master_disabled(monkeypatch):
    cfg = make_config()  # master off
    runner = await _make_runner_with_connected_journal(
        cfg, arkham_client=_FakeArkhamClient(),
    )

    # Manually seed a snapshot — if the guard mis-fires it would still
    # be written despite master being off.
    runner.ctx.on_chain_snapshot = OnChainSnapshot(
        daily_macro_bias="bullish",
        snapshot_age_s=0, stale_threshold_s=7200,
    )

    await runner._maybe_record_on_chain_snapshot()
    rows = await runner.ctx.journal.list_on_chain_snapshots()
    assert rows == []
    await runner.ctx.journal.close()


@pytest.mark.asyncio
async def test_snapshot_journal_failure_does_not_raise():
    """Journal hiccup must never crash the tick."""
    cfg = _make_on_chain_cfg()
    runner = _make_runner(cfg, arkham_client=_FakeArkhamClient())
    # Journal intentionally NOT connected — `record_on_chain_snapshot` will
    # raise RuntimeError("TradeJournal not connected..."). The helper must
    # swallow that via `arkham_snapshot_journal_failed` log.
    runner.ctx.on_chain_snapshot = OnChainSnapshot(
        daily_macro_bias="bullish",
        snapshot_age_s=0, stale_threshold_s=7200,
    )
    # Must not propagate.
    await runner._maybe_record_on_chain_snapshot()
    # Fingerprint stays None — we only set it on successful write.
    assert runner.ctx.last_on_chain_snapshot_fingerprint is None
