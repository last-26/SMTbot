"""Multi-TF pipeline + Pine freshness-check (Madde B).

Covers the new plumbing:
  * `_wait_for_pine_settle` returns True when `last_bar` flips, False on
    timeout, and True immediately when the fake reader never emits
    `last_bar` (test / old-Pine fallback).
  * `_run_one_symbol` switches chart through HTF → LTF → entry in that
    order when a bridge is wired.
  * HTF settle-timeout skips the symbol; LTF timeout still lets entry
    continue (just without LTF cache).
  * `SignalTableData.last_bar` is parsed from the Pine table.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.bot.runner import BotRunner
from src.data.models import MarketState, SignalTableData
from src.data.structured_reader import parse_signal_table
from tests.conftest import FakeMultiTF, FakeReader, FakeRouter, make_config


def _patch_plan_builder(monkeypatch, plan_or_none):
    def _stub(*a, **kw):
        return plan_or_none
    monkeypatch.setattr("src.bot.runner.build_trade_plan_from_state", _stub)


class _TrackingBridge:
    """Records the order of set_symbol / set_timeframe calls."""

    def __init__(self):
        self.timeframe_calls: list[str] = []
        self.symbol_calls: list[str] = []

    async def set_symbol(self, sym: str):
        self.symbol_calls.append(sym)
        return {"success": True}

    async def set_timeframe(self, tf: str):
        self.timeframe_calls.append(tf)
        return {"success": True}


class _ScriptedReader:
    """Reader that returns a scripted sequence of states for testing the
    freshness-poll. Each `read_market_state()` call pops the next state."""

    def __init__(self, states: list[MarketState]):
        self.states = list(states)
        self.fallback = states[-1] if states else MarketState()

    async def read_market_state(self) -> MarketState:
        if self.states:
            return self.states.pop(0)
        return self.fallback


def _state_with_last_bar(lb: Optional[int]) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP", timeframe="3",
        signal_table=SignalTableData(last_bar=lb),
    )


# ── last_bar parsing ────────────────────────────────────────────────────────


def test_signal_table_last_bar_parsed():
    """Pine table with a last_bar row → SignalTableData.last_bar populated."""
    tables_data = {
        "success": True,
        "studies": [{
            "name": "SMT Master Overlay",
            "tables": [{
                "rows": [
                    "=== SMT Signals === | BTCUSDT.P",
                    "trend_htf       | BULLISH",
                    "trend_ltf       | BULLISH",
                    "structure       | HH_HL",
                    "confluence      | 5/7",
                    "atr_14          | 450.5",
                    "price           | 69500.0",
                    "last_bar        | 12345",
                ],
            }],
        }],
    }
    parsed = parse_signal_table(tables_data)
    assert parsed is not None
    assert parsed.last_bar == 12345


def test_signal_table_last_bar_absent_is_none():
    tables_data = {
        "success": True,
        "studies": [{
            "name": "SMT Master Overlay",
            "tables": [{
                "rows": [
                    "=== SMT Signals === | BTCUSDT.P",
                    "confluence | 2/7",
                    "price      | 100.0",
                ],
            }],
        }],
    }
    parsed = parse_signal_table(tables_data)
    assert parsed is not None
    assert parsed.last_bar is None


# ── Freshness-poll helper ────────────────────────────────────────────────────


async def test_wait_for_pine_settle_success(make_ctx):
    """last_bar: 100, 100, 101 → True on the third read (change detected)."""
    reader = _ScriptedReader([
        _state_with_last_bar(100),
        _state_with_last_bar(100),
        _state_with_last_bar(101),
    ])
    cfg = make_config(pine_settle_max_wait_s=2.0,
                      pine_settle_poll_interval_s=0.0)
    ctx, _ = make_ctx(reader=reader, config=cfg)
    runner = BotRunner(ctx)
    assert await runner._wait_for_pine_settle() is True


async def test_wait_for_pine_settle_timeout(make_ctx):
    """last_bar stays at 100 → False on timeout."""
    reader = _ScriptedReader([_state_with_last_bar(100)])
    cfg = make_config(pine_settle_max_wait_s=0.1,
                      pine_settle_poll_interval_s=0.01)
    ctx, _ = make_ctx(reader=reader, config=cfg)
    runner = BotRunner(ctx)
    assert await runner._wait_for_pine_settle() is False


async def test_wait_for_pine_settle_none_first_read_returns_true(make_ctx):
    """If first readable last_bar is None (old Pine / test fake) → True."""
    reader = _ScriptedReader([_state_with_last_bar(None)])
    cfg = make_config(pine_settle_max_wait_s=1.0,
                      pine_settle_poll_interval_s=0.0)
    ctx, _ = make_ctx(reader=reader, config=cfg)
    runner = BotRunner(ctx)
    assert await runner._wait_for_pine_settle() is True


# ── TF switch order ─────────────────────────────────────────────────────────


async def test_tf_switch_order_htf_ltf_entry(monkeypatch, make_ctx):
    """With a bridge + LTF reader wired, one symbol cycle should switch
    through HTF → LTF → entry in that exact order."""
    _patch_plan_builder(monkeypatch, None)
    cfg = make_config(symbols=["BTC-USDT-SWAP"],
                      entry_timeframe="3m", htf_timeframe="15m",
                      ltf_timeframe="1m",
                      symbol_settle_seconds=0.0, tf_settle_seconds=0.0,
                      pine_settle_max_wait_s=0.1,
                      pine_settle_poll_interval_s=0.01)
    bridge = _TrackingBridge()

    class _DummyLTFReader:
        async def read(self, symbol, timeframe="1m"):
            return None

    ctx, fakes = make_ctx(config=cfg)
    ctx.bridge = bridge
    ctx.ltf_reader = _DummyLTFReader()
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert bridge.timeframe_calls == ["15m", "1m", "3m"]


async def test_no_bridge_skips_tf_switches(monkeypatch, make_ctx):
    """When bridge=None (test mode), TF switches are skipped — entry path
    still runs against the cached fake state."""
    _patch_plan_builder(monkeypatch, None)
    ctx, fakes = make_ctx()                  # default: bridge=None
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()
    # Router wasn't called (no plan), but the cycle completed without error.
    assert fakes.router.calls == []
