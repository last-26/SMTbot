"""Runner-level `_record_reject` helper (Phase 7.B1).

When `build_trade_plan_with_reason` returns `plan is None`, the runner calls
`_record_reject` to persist the snapshot into `rejected_signals`. These tests
drive the helper directly (not the full `_run_one_symbol` flow) so we can
check field-by-field that the journal row mirrors the `MarketState`
+ `ConfluenceScore` at decision time — that's the data the counter-factual
audit script will consume.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.analysis.multi_timeframe import ConfluenceFactor, ConfluenceScore
from src.bot.runner import BotRunner, _utc_now
from src.data.models import Direction, MarketState, Session, SignalTableData


pytestmark = pytest.mark.anyio


UTC = timezone.utc


def _state_with_price(price: float, atr: float) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP",
        timeframe="3m",
        timestamp=datetime(2026, 4, 19, 12, tzinfo=UTC),
        signal_table=SignalTableData(
            price=price,
            atr_14=atr,
            session=Session.LONDON,
            trend_htf=Direction.BULLISH,
        ),
    )


def _conf(score: float, factors: list[str], direction: Direction) -> ConfluenceScore:
    return ConfluenceScore(
        direction=direction,
        score=score,
        factors=[
            ConfluenceFactor(name=n, weight=1.0, direction=direction)
            for n in factors
        ],
    )


async def test_record_reject_persists_minimum_fields(make_ctx):
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 120.0)
    conf = _conf(1.5, ["recent_sweep"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    rows = await ctx.journal.list_rejected_signals()
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "BTC-USDT-SWAP"
    assert r.reject_reason == "below_confluence"
    assert r.direction == Direction.BULLISH
    assert r.price == 67_000.0
    assert r.atr == 120.0
    assert r.confluence_score == 1.5
    assert r.confluence_factors == ["recent_sweep"]
    assert r.entry_timeframe == ctx.config.trading.entry_timeframe
    assert r.htf_timeframe == ctx.config.trading.htf_timeframe
    assert r.htf_bias == "BULLISH"
    assert r.session == "LONDON"


async def test_record_reject_captures_pillar_bias_for_cross_asset_rejects(make_ctx):
    """cross_asset_opposition rejects must show BOTH pillar biases — that's
    the audit column used to verify the veto actually had evidence."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.BULLISH, now),
    }
    runner = BotRunner(ctx)
    state = _state_with_price(140.0, 1.2)
    conf = _conf(3.0, ["mss_alignment"], Direction.BEARISH)

    await runner._record_reject(
        symbol="SOL-USDT-SWAP",
        reject_reason="cross_asset_opposition",
        state=state,
        conf=conf,
    )

    rows = await ctx.journal.list_rejected_signals()
    assert len(rows) == 1
    assert rows[0].pillar_btc_bias == "BULLISH"
    assert rows[0].pillar_eth_bias == "BULLISH"


async def test_record_reject_pillar_bias_none_when_stale(make_ctx):
    """Stale pillar > max_age_s must NOT be stamped — else auditor can't
    distinguish 'fresh veto' from 'pillar data went stale' rejects."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    from datetime import timedelta
    stale = _utc_now() - timedelta(
        seconds=ctx.config.analysis.cross_asset_veto_max_age_s + 30
    )
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, stale),
        "ETH-USDT-SWAP": (Direction.BULLISH, _utc_now()),
    }
    runner = BotRunner(ctx)
    state = _state_with_price(140.0, 1.2)
    conf = _conf(3.0, [], Direction.BEARISH)

    await runner._record_reject(
        symbol="SOL-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    rows = await ctx.journal.list_rejected_signals()
    assert rows[0].pillar_btc_bias is None
    assert rows[0].pillar_eth_bias == "BULLISH"
