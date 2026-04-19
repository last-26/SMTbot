"""Tests for `BotRunner._cross_check_close_artefacts` (Katman 2).

Exercises the end-to-end path from `_handle_close` → Binance cross-check
→ `journal.update_artifact_flags` with a stubbed `BinancePublicClient`.

Scenarios covered:
  - No client (disabled) → no journal mutation.
  - Unmappable symbol → early return, no journal mutation.
  - Entry + exit inside real-market candles → flags set, artifact=False.
  - Exit wicked above real-market high → flags set, artifact=True,
    reason contains "exit_above_binance_high".
  - One candle fetch returns None → tri-state entry_valid=True + exit_valid=None,
    artifact=False (no invalid side observed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.bot.runner import BotRunner
from src.data.public_market_feed import RealCandle
from tests.conftest import make_close_fill, make_plan, make_report


UTC = timezone.utc


class _StubBinanceClient:
    """Drop-in for `BinancePublicClient` that returns pre-programmed candles."""

    def __init__(self, candles: list):
        # candles: list of RealCandle-or-None, consumed in order by repeated calls.
        self._candles = list(candles)
        self.calls: list[tuple[str, int]] = []

    def get_kline_around(self, binance_symbol: str, ts_ms: int,
                         interval: str = "1m"):
        self.calls.append((binance_symbol, ts_ms))
        if not self._candles:
            return None
        return self._candles.pop(0)

    def close(self) -> None:
        pass


def _candle(low: float, high: float) -> RealCandle:
    return RealCandle(open_time_ms=0, open=low, high=high, low=low, close=high)


async def _seed_open_trade(ctx) -> str:
    rec = await ctx.journal.record_open(
        make_plan(), make_report(),
        symbol="BTC-USDT-SWAP",
        signal_timestamp=datetime.now(tz=UTC),
    )
    ctx.open_trade_ids[("BTC-USDT-SWAP", "long")] = rec.trade_id
    return rec.trade_id


async def test_cross_check_disabled_when_client_is_none(make_ctx):
    ctx, fakes = make_ctx()
    ctx.binance_public = None
    runner = BotRunner(ctx)

    async with ctx.journal:
        trade_id = await _seed_open_trade(ctx)
        enriched = make_close_fill(pnl_usdt=12.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(trade_id)

    assert got is not None
    # No cross-check ran → fields remain None.
    assert got.real_market_entry_valid is None
    assert got.real_market_exit_valid is None
    assert got.demo_artifact is None


async def test_cross_check_unmapped_symbol_skips(make_ctx):
    ctx, fakes = make_ctx()
    stub = _StubBinanceClient([_candle(100, 200)])
    ctx.binance_public = stub
    runner = BotRunner(ctx)

    async with ctx.journal:
        rec = await ctx.journal.record_open(
            make_plan(), make_report(),
            # Unmappable — no `-SWAP` suffix.
            symbol="BTC-USDT",
            signal_timestamp=datetime.now(tz=UTC),
        )
        ctx.open_trade_ids[("BTC-USDT", "long")] = rec.trade_id
        enriched = make_close_fill(inst_id="BTC-USDT", pnl_usdt=12.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(rec.trade_id)

    assert got is not None
    assert got.demo_artifact is None  # never called
    assert stub.calls == []           # no Binance request issued


async def test_cross_check_prices_inside_candles_marks_valid(make_ctx):
    ctx, fakes = make_ctx()
    # Plan: entry 67_000, exit 68_500. Candles wide enough to contain both.
    stub = _StubBinanceClient([
        _candle(66_000, 68_000),   # entry candle
        _candle(68_000, 69_000),   # exit candle
    ])
    ctx.binance_public = stub
    runner = BotRunner(ctx)

    async with ctx.journal:
        trade_id = await _seed_open_trade(ctx)
        enriched = make_close_fill(pnl_usdt=15.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(trade_id)

    assert got is not None
    assert got.real_market_entry_valid is True
    assert got.real_market_exit_valid is True
    assert got.demo_artifact is False
    assert got.artifact_reason is None


async def test_cross_check_exit_above_real_high_flags_artifact(make_ctx):
    ctx, fakes = make_ctx()
    # Entry 67_000 in-band, exit 68_500 wicked above real high 68_000.
    stub = _StubBinanceClient([
        _candle(66_000, 68_000),   # entry: contains 67_000 ✓
        _candle(66_000, 68_000),   # exit: 68_500 > 68_000 ✗
    ])
    ctx.binance_public = stub
    runner = BotRunner(ctx)

    async with ctx.journal:
        trade_id = await _seed_open_trade(ctx)
        enriched = make_close_fill(exit_price=68_500.0, pnl_usdt=15.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(trade_id)

    assert got is not None
    assert got.real_market_entry_valid is True
    assert got.real_market_exit_valid is False
    assert got.demo_artifact is True
    assert got.artifact_reason is not None
    assert "exit_above_binance_high" in got.artifact_reason


async def test_cross_check_partial_feed_none_side(make_ctx):
    ctx, fakes = make_ctx()
    # First candle (entry) ok, second (exit) unavailable.
    stub = _StubBinanceClient([
        _candle(66_000, 68_000),
        None,
    ])
    ctx.binance_public = stub
    runner = BotRunner(ctx)

    async with ctx.journal:
        trade_id = await _seed_open_trade(ctx)
        enriched = make_close_fill(pnl_usdt=15.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(trade_id)

    assert got is not None
    assert got.real_market_entry_valid is True
    assert got.real_market_exit_valid is None
    # No invalid side observed → not flagged.
    assert got.demo_artifact is False


async def test_cross_check_both_sides_missing_leaves_none(make_ctx):
    ctx, fakes = make_ctx()
    stub = _StubBinanceClient([None, None])
    ctx.binance_public = stub
    runner = BotRunner(ctx)

    async with ctx.journal:
        trade_id = await _seed_open_trade(ctx)
        enriched = make_close_fill(pnl_usdt=15.0)
        fakes.okx_client.enrich_return = enriched
        await runner._handle_close(enriched)

        got = await ctx.journal.get_trade(trade_id)

    assert got is not None
    # Feed fully down → every flag stays None.
    assert got.real_market_entry_valid is None
    assert got.real_market_exit_valid is None
    assert got.demo_artifact is None
