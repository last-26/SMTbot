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
    # entry_timeframe / htf_timeframe dropped 2026-04-27 (1-distinct config constants).
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


async def test_record_reject_forwards_derivatives_enrichment(make_ctx):
    """2026-04-27 plumbing fix — `_record_reject` must thread the
    derivatives + heatmap fields from `_derive_enrichment` into the
    journal row. Pre-fix every reject had NULL OI / funding / liq /
    LS-zscore (CLAUDE.md 2026-04-24 acknowledged gap, 132/132 NULL on
    post-clean rows). Lock the contract: a reject driven from a state
    that carries DerivativesState lands the row with non-NULL fields.
    """
    from src.data.derivatives_cache import DerivativesState

    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)

    state = _state_with_price(67_000.0, 120.0)
    state.derivatives = DerivativesState(
        symbol="BTC-USDT-SWAP",
        ts_ms=1700_000_000_000,
        open_interest_usd=2_500_000_000.0,
        oi_change_1h_pct=0.012,
        funding_rate_current=0.00015,
        funding_rate_predicted=0.00012,
        long_liq_notional_1h=42_000.0,
        short_liq_notional_1h=18_000.0,
        ls_ratio_zscore_14d=-0.85,
    )
    conf = _conf(1.5, ["recent_sweep"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    rows = await ctx.journal.list_rejected_signals()
    r = rows[0]
    assert r.open_interest_usd_at_entry == 2_500_000_000.0
    assert r.oi_change_1h_pct_at_entry == pytest.approx(0.012)
    assert r.funding_rate_current_at_entry == pytest.approx(0.00015)
    assert r.funding_rate_predicted_at_entry == pytest.approx(0.00012)
    assert r.long_liq_notional_1h_at_entry == 42_000.0
    assert r.short_liq_notional_1h_at_entry == 18_000.0
    assert r.ls_ratio_zscore_14d_at_entry == pytest.approx(-0.85)


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


# ── Pass 2.5 reject pegger plumbing ──────────────────────────────────────

def _expected_sl_distance(cfg, *, symbol: str, price: float, atr: float) -> float:
    """Mirror runner._compute_what_if_proposed_sltp's max(atr*1.5, floor)."""
    floor_pct = cfg.min_sl_distance_pct_for(symbol)
    return max(float(atr) * 1.5, float(price) * float(floor_pct))


def _expected_target_rr(cfg) -> float:
    return float(cfg.execution.target_rr_ratio or 0.0) or 1.5


async def test_record_reject_what_if_proposed_sltp_long_pre_fill(make_ctx):
    """Pre-fill rejects (below_confluence, hard-gate vetoes) must auto-
    compute ATR-based what-if SL/TP. Pass 2.5 pegger forward-walks Bybit
    klines from these targets to flag would-have-WIN/LOSS."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 500.0)  # large ATR → atr×1.5 dominates floor
    conf = _conf(1.5, ["recent_sweep"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    sl_distance = _expected_sl_distance(
        ctx.config, symbol="BTC-USDT-SWAP", price=67_000.0, atr=500.0,
    )
    target_rr = _expected_target_rr(ctx.config)
    assert r.proposed_sl_price == pytest.approx(67_000.0 - sl_distance)
    assert r.proposed_tp_price == pytest.approx(67_000.0 + sl_distance * target_rr)
    assert r.proposed_rr_ratio == pytest.approx(target_rr)


async def test_record_reject_what_if_proposed_sltp_short_pre_fill(make_ctx):
    """SHORT direction: SL above price, TP below."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 500.0)
    conf = _conf(1.5, ["mss_alignment"], Direction.BEARISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="ema_momentum_contra",
        state=state,
        conf=conf,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    sl_distance = _expected_sl_distance(
        ctx.config, symbol="BTC-USDT-SWAP", price=67_000.0, atr=500.0,
    )
    target_rr = _expected_target_rr(ctx.config)
    assert r.proposed_sl_price == pytest.approx(67_000.0 + sl_distance)
    assert r.proposed_tp_price == pytest.approx(67_000.0 - sl_distance * target_rr)
    assert r.proposed_rr_ratio == pytest.approx(target_rr)


async def test_record_reject_no_proposed_for_no_setup_zone_class(make_ctx):
    """Reasons that short-circuit before SL math (no_setup_zone, no_sl_source,
    session_filter, etc.) leave proposed_* NULL — pegger skips them."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 120.0)
    conf = _conf(4.0, ["mss_alignment"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="no_setup_zone",
        state=state,
        conf=conf,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    assert r.proposed_sl_price is None
    assert r.proposed_tp_price is None
    assert r.proposed_rr_ratio is None


async def test_record_reject_no_proposed_for_undefined_direction(make_ctx):
    """No direction = no SL/TP. Pegger has no side to forward-walk against."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 120.0)
    conf = _conf(2.0, [], Direction.UNDEFINED)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    assert r.proposed_sl_price is None
    assert r.proposed_tp_price is None
    assert r.proposed_rr_ratio is None


async def test_record_reject_caller_override_skips_what_if(make_ctx):
    """Pending-cancel path: caller passes plan.sl_price/tp_price/rr_ratio
    directly (more accurate than re-computing what-if). Helper must
    forward those exact values, NOT auto-compute."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 120.0)
    conf = _conf(3.5, ["mss_alignment"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="zone_timeout_cancel",
        state=state,
        conf=conf,
        proposed_sl_price=66_500.0,  # caller-provided exact pending SL
        proposed_tp_price=67_750.0,  # caller-provided exact pending TP
        proposed_rr_ratio=1.5,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    assert r.proposed_sl_price == pytest.approx(66_500.0)
    assert r.proposed_tp_price == pytest.approx(67_750.0)
    assert r.proposed_rr_ratio == pytest.approx(1.5)


async def test_record_reject_what_if_uses_per_symbol_floor_when_atr_tiny(
    make_ctx, monkeypatch,
):
    """ATR×1.5 < price×min_sl_distance_pct_per_symbol → floor binds.
    SL widening parity with the live trade-plan path."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    # Inject a measurable floor for BTC so the floor branch is exercised
    # regardless of test config defaults (which can be 0). 0.005 ≫ atr*1.5/price
    # for the values below: 67000*0.005 = 335 vs atr*1.5 = 15.
    monkeypatch.setitem(
        ctx.config.analysis.min_sl_distance_pct_per_symbol,
        "BTC-USDT-SWAP", 0.005,
    )
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 10.0)
    conf = _conf(1.0, ["recent_sweep"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state,
        conf=conf,
    )

    r = (await ctx.journal.list_rejected_signals())[0]
    expected_sl_distance = 67_000.0 * 0.005  # floor wins over atr*1.5=15
    target_rr = _expected_target_rr(ctx.config)
    assert r.proposed_sl_price == pytest.approx(67_000.0 - expected_sl_distance)
    assert r.proposed_tp_price == pytest.approx(
        67_000.0 + expected_sl_distance * target_rr
    )


async def test_update_rejected_outcome_stamps_pegger_result(make_ctx):
    """`update_rejected_outcome` lets the pegger script flag a reject as
    WIN/LOSS/TIMEOUT after Bybit kline forward-walk. Bar offsets only
    populate on the matching side."""
    ctx, _ = make_ctx()
    await ctx.journal.connect()
    runner = BotRunner(ctx)
    state = _state_with_price(67_000.0, 100.0)
    conf = _conf(1.5, ["recent_sweep"], Direction.BULLISH)

    await runner._record_reject(
        symbol="BTC-USDT-SWAP",
        reject_reason="below_confluence",
        state=state, conf=conf,
    )
    [r0] = await ctx.journal.list_rejected_signals()
    assert r0.hypothetical_outcome is None

    # Pegger says: would have been WIN at bar 7 (TP hit; no SL hit).
    await ctx.journal.update_rejected_outcome(
        r0.rejection_id,
        outcome="WIN",
        bars_to_tp=7,
        bars_to_sl=None,
    )
    [r1] = await ctx.journal.list_rejected_signals()
    assert r1.hypothetical_outcome == "WIN"
    assert r1.hypothetical_bars_to_tp == 7
    assert r1.hypothetical_bars_to_sl is None
