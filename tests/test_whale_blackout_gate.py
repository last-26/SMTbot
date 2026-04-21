"""Tests for the `whale_transfer_blackout` hard gate (Phase D)."""

from __future__ import annotations

import time

import pytest

from src.data.models import Direction, MarketState
from src.data.on_chain_types import WhaleBlackoutState
from src.strategy.entry_signals import build_trade_plan_with_reason


def _state_with_confluence_path(symbol: str) -> MarketState:
    """Minimal state that gets far enough into the pipeline that the
    whale gate matters. No VWAPs / OBs / FVGs — the pipeline bails with
    `below_confluence` / `no_sl_source` way before our gate, so we
    don't need to construct full signals. The gate tests here rely on
    the `intent is None` short-circuit and the gate's relative order.
    """
    return MarketState(symbol=symbol, timeframe="3")


def _build_plan(symbol: str, **overrides):
    """Wrap build_trade_plan_with_reason with the minimum set of kwargs."""
    kwargs = dict(
        state=_state_with_confluence_path(symbol),
        account_balance=1_000.0,
        candles=None,
        min_confluence_score=2.0,
        risk_pct=0.01,
        rr_ratio=3.0,
        min_rr_ratio=2.0,
        max_leverage=20,
        contract_size=0.01,
    )
    kwargs.update(overrides)
    return build_trade_plan_with_reason(**kwargs)


# ── Gate flag-off paths ─────────────────────────────────────────────────────
#
# When the gate is disabled OR blackout state absent OR symbol missing,
# the pipeline should never return `whale_transfer_blackout` — it must
# fall through to the standard reject reasons (below_confluence / etc.).


def test_gate_disabled_never_fires():
    state = WhaleBlackoutState()
    # Even with an active blackout, flag off → no fire.
    future_ms = int((time.time() + 3600) * 1000)
    state.set_blackout("BTC-USDT-SWAP", future_ms)
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=False,
        whale_blackout=state,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    assert plan is None
    assert reason != "whale_transfer_blackout"


def test_gate_no_state_never_fires():
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=None,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    assert plan is None
    assert reason != "whale_transfer_blackout"


def test_gate_no_symbol_never_fires():
    state = WhaleBlackoutState()
    future_ms = int((time.time() + 3600) * 1000)
    state.set_blackout("BTC-USDT-SWAP", future_ms)
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=state,
        whale_blackout_symbol=None,
    )
    assert plan is None
    assert reason != "whale_transfer_blackout"


def test_gate_empty_blackouts_never_fires():
    state = WhaleBlackoutState()  # no blackouts set
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=state,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    assert plan is None
    assert reason != "whale_transfer_blackout"


def test_gate_expired_blackout_never_fires():
    state = WhaleBlackoutState()
    past_ms = int((time.time() - 3600) * 1000)
    state.set_blackout("BTC-USDT-SWAP", past_ms)
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=state,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    assert plan is None
    assert reason != "whale_transfer_blackout"


# ── Gate fires ──────────────────────────────────────────────────────────────
#
# These tests use monkeypatching to force the pipeline past the
# `below_confluence` short-circuit so the whale gate has a chance to
# fire. Without that, the pipeline rejects before reaching the gate.


@pytest.fixture
def _past_intent(monkeypatch):
    """Stub `generate_entry_intent` to return a usable intent so
    `build_trade_plan_with_reason` reaches the hard-gate chain."""
    from src.strategy.entry_signals import EntryIntent
    from src.analysis.multi_timeframe import ConfluenceScore

    intent = EntryIntent(
        direction=Direction.BULLISH,
        entry_price=100.0,
        sl_price=99.0,
        sl_source="order_block",
        atr=0.5,
        confluence=ConfluenceScore(
            direction=Direction.BULLISH, score=3.0, factors=[],
        ),
    )

    def _stub(*a, **kw):
        return intent

    monkeypatch.setattr(
        "src.strategy.entry_signals.generate_entry_intent", _stub)
    yield


def test_gate_fires_when_symbol_blackout_active(_past_intent):
    state = WhaleBlackoutState()
    future_ms = int((time.time() + 3600) * 1000)
    state.set_blackout("BTC-USDT-SWAP", future_ms)
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=state,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    assert plan is None
    assert reason == "whale_transfer_blackout"


def test_gate_does_not_fire_for_different_symbol(_past_intent):
    state = WhaleBlackoutState()
    # Blackout ETH only.
    future_ms = int((time.time() + 3600) * 1000)
    state.set_blackout("ETH-USDT-SWAP", future_ms)
    # We evaluate BTC's gate — must not fire.
    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=state,
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    # Different reason, but not whale_transfer_blackout.
    assert reason != "whale_transfer_blackout"


def test_gate_stablecoin_blackout_blocks_every_symbol(_past_intent):
    state = WhaleBlackoutState()
    future_ms = int((time.time() + 3600) * 1000)
    # Simulate a stablecoin event by setting blackouts for ALL 5.
    for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                "DOGE-USDT-SWAP", "BNB-USDT-SWAP"):
        state.set_blackout(sym, future_ms)
    # Every symbol should report whale_transfer_blackout.
    for sym in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                "DOGE-USDT-SWAP", "BNB-USDT-SWAP"):
        plan, reason = _build_plan(
            sym, whale_blackout_enabled=True,
            whale_blackout=state, whale_blackout_symbol=sym,
        )
        assert plan is None
        assert reason == "whale_transfer_blackout", f"failed for {sym}"


def test_gate_handles_corrupt_state_without_crashing(_past_intent):
    """A `whale_blackout` object without `.is_active` must not crash."""

    class _Broken:
        def is_active(self, symbol: str, now_ms: int) -> bool:
            raise RuntimeError("corrupt state")

    plan, reason = _build_plan(
        "BTC-USDT-SWAP",
        whale_blackout_enabled=True,
        whale_blackout=_Broken(),
        whale_blackout_symbol="BTC-USDT-SWAP",
    )
    # Gate swallowed the error; pipeline continues to downstream reasons.
    assert reason != "whale_transfer_blackout"
