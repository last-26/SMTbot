"""LTF reversal defensive close (Madde F).

When we already hold a position and the LTF oscillator just flipped against
us, the runner cancels outstanding algos + closes the position *before*
looking for new entries, tagging the journal row with
`close_reason="EARLY_CLOSE_LTF_REVERSAL"`.

Tests cover the gate predicate (`_is_ltf_reversal`), the close action
(`_defensive_close`), idempotence, minimum-holding-time guard, the
disabled flag, and that the close_reason round-trips through
`_handle_close → journal.record_close`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.bot.runner import BotRunner, _tf_seconds
from src.data.ltf_reader import LTFState
from src.data.models import Direction
from src.execution.models import PositionSnapshot
from tests.conftest import make_close_fill, make_config


UTC = timezone.utc


def _ltf(
    *,
    trend: Direction = Direction.BEARISH,
    last_signal: str = "SELL",
    bars_ago: int = 1,
    rsi: float = 35.0,
    wt_state: str = "OVERSOLD",
) -> LTFState:
    return LTFState(
        symbol="BTC-USDT-SWAP",
        timeframe="1m",
        price=67_000.0,
        rsi=rsi,
        wt_state=wt_state,
        wt_cross="DOWN",
        last_signal=last_signal,
        last_signal_bars_ago=bars_ago,
        trend=trend,
    )


def _mark_open(
    ctx,
    *,
    symbol: str = "BTC-USDT-SWAP",
    side: str = "long",
    trade_id: str = "T1",
    opened_ago_s: float = 10_000.0,
):
    """Simulate an open position so the reversal gate has something to close."""
    ctx.open_trade_ids[(symbol, side)] = trade_id
    ctx.open_trade_opened_at[(symbol, side)] = (
        datetime.now(tz=UTC) - timedelta(seconds=opened_ago_s)
    )
    # Give the monitor a tracked entry with algo_ids so defensive_close can
    # try to cancel them.
    ctx.monitor._tracked = getattr(ctx.monitor, "_tracked", {})  # type: ignore[attr-defined]
    ctx.monitor._tracked[(symbol, side)] = type(
        "_T", (), {"algo_ids": ["ALG1", "ALG2"]}
    )()


# ── Predicate: _is_ltf_reversal ─────────────────────────────────────────────


def test_is_ltf_reversal_long_sees_fresh_bearish(make_ctx):
    ctx, _ = make_ctx()
    runner = BotRunner(ctx)
    ltf = _ltf(trend=Direction.BEARISH, last_signal="SELL", bars_ago=1)
    assert runner._is_ltf_reversal(ltf, "long", max_age=3) is True


def test_stale_ltf_signal_does_not_trigger(make_ctx):
    ctx, _ = make_ctx()
    runner = BotRunner(ctx)
    ltf = _ltf(trend=Direction.BEARISH, last_signal="SELL", bars_ago=10)
    assert runner._is_ltf_reversal(ltf, "long", max_age=3) is False


def test_same_side_signal_ignored(make_ctx):
    # Long open + BULLISH trend + BUY signal — no reversal.
    ctx, _ = make_ctx()
    runner = BotRunner(ctx)
    ltf = _ltf(
        trend=Direction.BULLISH, last_signal="BUY",
        bars_ago=1, rsi=70.0, wt_state="OVERBOUGHT",
    )
    assert runner._is_ltf_reversal(ltf, "long", max_age=3) is False


def test_short_sees_fresh_bullish(make_ctx):
    ctx, _ = make_ctx()
    runner = BotRunner(ctx)
    ltf = _ltf(
        trend=Direction.BULLISH, last_signal="BUY",
        bars_ago=2, rsi=70.0, wt_state="OVERBOUGHT",
    )
    assert runner._is_ltf_reversal(ltf, "short", max_age=3) is True


# ── Defensive close action ──────────────────────────────────────────────────


async def test_defensive_close_cancels_algos_and_closes_position(make_ctx):
    ctx, fakes = make_ctx()
    _mark_open(ctx)
    runner = BotRunner(ctx)
    await runner._defensive_close("BTC-USDT-SWAP", "long", "ltf_reversal")

    assert fakes.okx_client.cancel_algo_calls == [
        ("BTC-USDT-SWAP", "ALG1"), ("BTC-USDT-SWAP", "ALG2"),
    ]
    assert fakes.okx_client.close_position_calls == [
        ("BTC-USDT-SWAP", "long"),
    ]
    assert ctx.pending_close_reasons[("BTC-USDT-SWAP", "long")] == \
        "EARLY_CLOSE_LTF_REVERSAL"
    assert ("BTC-USDT-SWAP", "long") in ctx.defensive_close_in_flight


async def test_defensive_close_idempotent_same_cycle(make_ctx):
    ctx, fakes = make_ctx()
    _mark_open(ctx)
    runner = BotRunner(ctx)
    await runner._defensive_close("BTC-USDT-SWAP", "long", "ltf_reversal")
    # Second call should short-circuit: no extra cancels / close.
    await runner._defensive_close("BTC-USDT-SWAP", "long", "ltf_reversal")

    assert len(fakes.okx_client.cancel_algo_calls) == 2          # one pass only
    assert len(fakes.okx_client.close_position_calls) == 1


# ── End-to-end wiring through _run_one_symbol ───────────────────────────────


def _patch_plan_builder(monkeypatch, plan_or_none):
    def _stub(*a, **kw):
        return plan_or_none
    monkeypatch.setattr("src.bot.runner.build_trade_plan_from_state", _stub)


async def test_long_bearish_ltf_fresh_signal_triggers_close(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(entry_timeframe="15m")
    cfg.execution.ltf_reversal_close_enabled = True
    cfg.execution.ltf_reversal_min_bars_in_position = 0
    ctx, fakes = make_ctx(config=cfg)
    _mark_open(ctx)
    ctx.ltf_cache["BTC-USDT-SWAP"] = _ltf(
        trend=Direction.BEARISH, last_signal="SELL", bars_ago=1,
    )
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._run_one_symbol("BTC-USDT-SWAP")

    assert fakes.okx_client.close_position_calls == [
        ("BTC-USDT-SWAP", "long"),
    ]
    assert ctx.pending_close_reasons[("BTC-USDT-SWAP", "long")] == \
        "EARLY_CLOSE_LTF_REVERSAL"


async def test_min_holding_time_blocks_close(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(entry_timeframe="15m")
    cfg.execution.ltf_reversal_close_enabled = True
    cfg.execution.ltf_reversal_min_bars_in_position = 2     # 2 × 15m = 1800 s
    ctx, fakes = make_ctx(config=cfg)
    # Only 1 bar elapsed — below the 2-bar minimum.
    _mark_open(ctx, opened_ago_s=15 * 60)
    ctx.ltf_cache["BTC-USDT-SWAP"] = _ltf(
        trend=Direction.BEARISH, last_signal="SELL", bars_ago=1,
    )
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._run_one_symbol("BTC-USDT-SWAP")

    assert fakes.okx_client.close_position_calls == []


async def test_disabled_flag_never_closes(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(entry_timeframe="15m")
    cfg.execution.ltf_reversal_close_enabled = False
    ctx, fakes = make_ctx(config=cfg)
    _mark_open(ctx)
    ctx.ltf_cache["BTC-USDT-SWAP"] = _ltf(
        trend=Direction.BEARISH, last_signal="SELL", bars_ago=1,
    )
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._run_one_symbol("BTC-USDT-SWAP")

    assert fakes.okx_client.close_position_calls == []
    assert ctx.pending_close_reasons == {}


# ── Journal round-trip via _handle_close ────────────────────────────────────


async def test_close_reason_journaled_via_handle_close(make_ctx):
    """After defensive_close tags pending_close_reasons, the next CloseFill
    processed by _handle_close must write close_reason onto the journal row.
    """
    ctx, fakes = make_ctx()
    runner = BotRunner(ctx)
    # Seed journal with an OPEN row so we have a trade_id to close.
    from tests.conftest import make_plan, make_report
    async with ctx.journal:
        rec = await ctx.journal.record_open(
            make_plan(), make_report(),
            symbol="BTC-USDT-SWAP",
            signal_timestamp=datetime.now(tz=UTC),
        )
        ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = rec.trade_id
        ctx.pending_close_reasons[("BTC-USDT-SWAP", "long")] = \
            "EARLY_CLOSE_LTF_REVERSAL"
        ctx.defensive_close_in_flight.add(("BTC-USDT-SWAP", "long"))

        enriched = make_close_fill(pnl_usdt=-5.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        fetched = await ctx.journal.get_trade(rec.trade_id)
    assert fetched is not None
    assert fetched.close_reason == "EARLY_CLOSE_LTF_REVERSAL"
    # Guard cleared after the close drained.
    assert ("BTC-USDT-SWAP", "long") not in ctx.defensive_close_in_flight
    assert ("BTC-USDT-SWAP", "long") not in ctx.pending_close_reasons
