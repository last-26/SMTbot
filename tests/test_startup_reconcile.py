"""Tests for `_reconcile_orphans` startup orphan-position reconciliation.

Covers the four cases exposed by the 2026-04-28 SOL incident:
  A. Live position with no journal row     → synthetic insert + TP/SL attach
  B. Live size > journal size              → grow journal num_contracts
  C. Journal row but no live position      → log only (org. close flow handles)
  D. Clean state (1:1 match)               → no-op
"""

from __future__ import annotations

import pytest

from src.bot.runner import BotRunner
from src.data.models import Direction
from src.execution.models import PositionSnapshot
from src.journal.models import TradeOutcome
from tests.conftest import FakeBybitClient


pytestmark = pytest.mark.asyncio


# ── Case A: live without journal → synthetic insert ─────────────────────────


async def test_reconcile_synthetic_inserts_row_for_live_without_journal(make_ctx):
    """SOL 2026-04-28 case: a pending limit filled during restart and the
    new bot session has no DB row for the live position. Reconcile must
    synthetic-insert so the position gets tracked + managed."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="SOL-USDT-SWAP", pos_side="long",
            size=13.0, entry_price=145.20,
            mark_price=145.30, unrealized_pnl=1.30, leverage=10,
            take_profit=0.0, stop_loss=0.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._reconcile_orphans()
        opens = await ctx.journal.list_open_trades()

    assert len(opens) == 1
    rec = opens[0]
    assert rec.symbol == "SOL-USDT-SWAP"
    assert rec.direction == Direction.BULLISH
    assert rec.outcome == TradeOutcome.OPEN
    assert rec.num_contracts == 13
    assert rec.entry_price == pytest.approx(145.20)
    assert rec.demo_artifact is True
    assert rec.artifact_reason and rec.artifact_reason.startswith(
        "startup_reconcile_synthetic_"
    )
    # SL below entry for a long; TP above entry; both populated.
    assert 0 < rec.sl_price < rec.entry_price
    assert rec.tp_price > rec.entry_price


async def test_reconcile_synthetic_attaches_tpsl_when_bybit_has_none(make_ctx):
    """When live position has no TP/SL leg attached, reconcile must call
    set_position_tpsl with the synthetic plan's prices so the position
    isn't naked."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="BTC-USDT-SWAP", pos_side="short",
            size=2.0, entry_price=67_000.0,
            mark_price=67_050.0, unrealized_pnl=-1.0, leverage=10,
            take_profit=0.0, stop_loss=0.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._reconcile_orphans()

    assert len(client.set_position_tpsl_calls) == 1
    call = client.set_position_tpsl_calls[0]
    assert call["inst_id"] == "BTC-USDT-SWAP"
    assert call["pos_side"] == "short"
    # Short: SL above entry, TP below entry.
    assert call["stop_loss"] is not None and call["stop_loss"] > 67_000.0
    assert call["take_profit"] is not None and call["take_profit"] < 67_000.0


async def test_reconcile_synthetic_skips_tpsl_attach_when_already_present(make_ctx):
    """If Bybit's position already carries TP + SL, reconcile must NOT
    re-attach (idempotent). Synthetic row still gets inserted."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="ETH-USDT-SWAP", pos_side="long",
            size=5.0, entry_price=3_500.0,
            mark_price=3_510.0, unrealized_pnl=5.0, leverage=10,
            take_profit=3_550.0, stop_loss=3_480.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._reconcile_orphans()
        opens = await ctx.journal.list_open_trades()

    assert len(opens) == 1
    assert opens[0].sl_price == pytest.approx(3_480.0)
    assert opens[0].tp_price == pytest.approx(3_550.0)
    assert client.set_position_tpsl_calls == []


# ── Case B: live size > journal size → grow journal ─────────────────────────


async def test_reconcile_grows_journal_num_contracts_when_live_size_exceeds(make_ctx):
    """SOL 2026-04-28 case: DB had 14 contracts, live had 27 (second fill
    landed). Reconcile must update the journal row to match live size and
    scale notional/risk linearly."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="SOL-USDT-SWAP", pos_side="long",
            size=27.0, entry_price=145.20,
            mark_price=145.30, unrealized_pnl=2.70, leverage=10,
            take_profit=0.0, stop_loss=0.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        # Seed an OPEN row with size 14 (the pre-incident DB state).
        original = await ctx.journal.record_open_synthetic(
            symbol="SOL-USDT-SWAP",
            direction=Direction.BULLISH,
            entry_price=145.20,
            sl_price=143.50,
            tp_price=148.00,
            num_contracts=14,
            position_size_usdt=14 * 145.20,
            risk_amount_usdt=14 * 1.70,
            leverage=10,
            artifact_reason="seed_for_test",
        )
        await runner._reconcile_orphans()
        rec = await ctx.journal.get_trade(original.trade_id)

    assert rec is not None
    assert rec.num_contracts == 27
    # Notional + risk scaled by ratio 27/14 ≈ 1.9286.
    assert rec.position_size_usdt == pytest.approx(14 * 145.20 * 27 / 14)
    assert rec.risk_amount_usdt == pytest.approx(14 * 1.70 * 27 / 14)
    assert rec.artifact_reason and rec.artifact_reason.startswith(
        "startup_reconcile_size_grow_"
    )


async def test_reconcile_does_not_shrink_journal_when_live_size_smaller(make_ctx):
    """Live < DB suggests a partial close already happened. The bot's
    monitor.poll() handles that organically; reconcile must NOT touch
    the row (would lose risk-amount fidelity)."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="BTC-USDT-SWAP", pos_side="long",
            size=3.0, entry_price=67_000.0,
            mark_price=67_100.0, unrealized_pnl=3.0, leverage=10,
            take_profit=68_500.0, stop_loss=66_500.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        original = await ctx.journal.record_open_synthetic(
            symbol="BTC-USDT-SWAP",
            direction=Direction.BULLISH,
            entry_price=67_000.0,
            sl_price=66_500.0,
            tp_price=68_500.0,
            num_contracts=5,  # DB had 5, live shows 3
            position_size_usdt=5 * 670.0,
            risk_amount_usdt=5 * 5.0,
            leverage=10,
            artifact_reason="seed_for_test",
        )
        await runner._reconcile_orphans()
        rec = await ctx.journal.get_trade(original.trade_id)

    assert rec is not None
    assert rec.num_contracts == 5  # untouched
    assert rec.artifact_reason == "seed_for_test"  # untouched


# ── Case C: journal without live → log only ─────────────────────────────────


async def test_reconcile_does_not_modify_db_when_journal_open_but_no_live(make_ctx):
    """A journal OPEN row whose live position is gone is left alone —
    rehydrate + monitor.poll() emit a CloseFill on the next tick which
    enrich_close_fill resolves. Reconcile only logs."""
    client = FakeBybitClient(positions=[])  # no live positions
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        original = await ctx.journal.record_open_synthetic(
            symbol="DOGE-USDT-SWAP",
            direction=Direction.BEARISH,
            entry_price=0.18,
            sl_price=0.185,
            tp_price=0.17,
            num_contracts=10,
            position_size_usdt=1_800.0,
            risk_amount_usdt=50.0,
            leverage=10,
            artifact_reason="seed_for_test",
        )
        await runner._reconcile_orphans()
        rec = await ctx.journal.get_trade(original.trade_id)

    assert rec is not None
    assert rec.outcome == TradeOutcome.OPEN
    assert rec.num_contracts == 10
    assert rec.artifact_reason == "seed_for_test"


# ── Case D: clean state → no-op ─────────────────────────────────────────────


async def test_reconcile_noop_when_db_and_live_match(make_ctx):
    """1:1 match between journal and live state: no synthetic insert,
    no size grow, no TP/SL attach call."""
    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="ETH-USDT-SWAP", pos_side="long",
            size=5.0, entry_price=3_500.0,
            mark_price=3_510.0, unrealized_pnl=5.0, leverage=10,
            take_profit=3_550.0, stop_loss=3_480.0,
        ),
    ])
    ctx, _ = make_ctx(bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        original = await ctx.journal.record_open_synthetic(
            symbol="ETH-USDT-SWAP",
            direction=Direction.BULLISH,
            entry_price=3_500.0,
            sl_price=3_480.0,
            tp_price=3_550.0,
            num_contracts=5,
            position_size_usdt=1_750.0,
            risk_amount_usdt=10.0,
            leverage=10,
            artifact_reason="seed_for_test",
        )
        await runner._reconcile_orphans()
        opens = await ctx.journal.list_open_trades()
        rec = await ctx.journal.get_trade(original.trade_id)

    assert len(opens) == 1
    assert rec is not None
    assert rec.num_contracts == 5  # untouched
    assert rec.artifact_reason == "seed_for_test"  # untouched
    assert client.set_position_tpsl_calls == []


# ── Disabled flag ────────────────────────────────────────────────────────────


async def test_reconcile_disabled_flag_keeps_log_only_behaviour(make_ctx):
    """When `bot.startup_orphan_reconcile_enabled` is False, reconcile
    must NOT mutate the journal — only log the mismatch (legacy
    pre-2026-04-28 behavior)."""
    from tests.conftest import make_config
    cfg = make_config()
    cfg.bot.startup_orphan_reconcile_enabled = False

    client = FakeBybitClient(positions=[
        PositionSnapshot(
            inst_id="SOL-USDT-SWAP", pos_side="long",
            size=13.0, entry_price=145.20,
            mark_price=145.30, unrealized_pnl=1.30, leverage=10,
        ),
    ])
    ctx, _ = make_ctx(config=cfg, bybit_client=client)
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner._reconcile_orphans()
        opens = await ctx.journal.list_open_trades()

    assert opens == []  # NO synthetic insert when disabled
    assert client.set_position_tpsl_calls == []
