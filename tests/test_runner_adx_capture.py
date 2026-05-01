"""Phase A.9 — runner-level tests for ADX numeric capture.

Verifies the `_adx_triad_kwargs` helper and (lightly) the wiring contract
between `BotContext.htf_adx_cache` and `_run_one_symbol` / placement-time
stash on `PendingSetupMeta`.
"""

from __future__ import annotations

from src.analysis.trend_regime import TrendRegime, TrendRegimeResult
from src.bot.runner import (
    BotContext,
    PendingSetupMeta,
    _adx_triad_kwargs,
)


def test_adx_triad_kwargs_emits_three_keys_for_known_regime():
    res = TrendRegimeResult(TrendRegime.STRONG_TREND, 32.4, 27.0, 12.0, 100)
    out = _adx_triad_kwargs("3m", res)
    assert out == {
        "adx_3m_at_entry": 32.4,
        "plus_di_3m_at_entry": 27.0,
        "minus_di_3m_at_entry": 12.0,
    }


def test_adx_triad_kwargs_15m_prefix_independent():
    res = TrendRegimeResult(TrendRegime.WEAK_TREND, 21.1, 18.0, 15.5, 100)
    out = _adx_triad_kwargs("15m", res)
    assert out == {
        "adx_15m_at_entry": 21.1,
        "plus_di_15m_at_entry": 18.0,
        "minus_di_15m_at_entry": 15.5,
    }


def test_adx_triad_kwargs_unknown_regime_emits_nulls():
    """UNKNOWN means insufficient bars / flat prices — classifier returns
    adx=0.0 in that case, but 0 is a legitimate computed value elsewhere.
    Helper must emit NULLs so 'insufficient data' stays distinguishable
    from 'computed zero' in downstream GBT."""
    res = TrendRegimeResult(TrendRegime.UNKNOWN, 0.0, 0.0, 0.0, 0)
    out = _adx_triad_kwargs("3m", res)
    assert out == {
        "adx_3m_at_entry": None,
        "plus_di_3m_at_entry": None,
        "minus_di_3m_at_entry": None,
    }


def test_adx_triad_kwargs_none_result_emits_nulls():
    """None → cache cold (e.g. already-open skip). Same NULL semantics."""
    out = _adx_triad_kwargs("15m", None)
    assert out == {
        "adx_15m_at_entry": None,
        "plus_di_15m_at_entry": None,
        "minus_di_15m_at_entry": None,
    }


def test_bot_context_has_htf_adx_cache_default():
    """BotContext must expose `htf_adx_cache: dict` so HTF pass can stash
    15m ADX results per-symbol without requiring construction-site init."""
    ctx = BotContext.__new__(BotContext)
    # default_factory contract via dataclass: re-instantiate via fields()
    from dataclasses import fields
    f = {f.name for f in fields(BotContext)}
    assert "htf_adx_cache" in f


def test_pending_setup_meta_carries_adx_results():
    """`PendingSetupMeta` stashes both 3m + 15m placement-time ADX results
    so the eventual fill / cancel journal row stamps the regime that
    drove the limit placement (not the later moment)."""
    from datetime import datetime, timezone
    from src.data.models import Direction
    from src.strategy.trade_plan import TradePlan
    from src.strategy.setup_planner import ZoneSetup

    plan = TradePlan(
        direction=Direction.BULLISH,
        entry_price=100.0, sl_price=99.0, tp_price=101.5,
        rr_ratio=1.5, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=100.0, leverage=10, required_leverage=10.0,
        num_contracts=1, risk_amount_usdt=1.0, max_risk_usdt=1.0,
        capped=False, sl_source="atr", confluence_score=4.0,
        confluence_factors=["mss"], reason="t",
    )
    zone = ZoneSetup(
        direction=Direction.BULLISH,
        entry_zone=(99.5, 100.0),
        trigger_type="zone_touch",
        sl_beyond_zone=99.0,
        tp_primary=101.5,
        max_wait_bars=2,
        zone_source="vwap_retest",
    )
    res_3m = TrendRegimeResult(TrendRegime.STRONG_TREND, 33.0, 28.0, 11.0, 100)
    res_15m = TrendRegimeResult(TrendRegime.WEAK_TREND, 21.0, 17.0, 16.0, 100)

    class _DummyState:
        timestamp = datetime.now(timezone.utc)

    meta = PendingSetupMeta(
        plan=plan, zone=zone, order_id="oid",
        signal_state=_DummyState(),
        placed_at=datetime.now(timezone.utc),
        adx_3m_result_at_placement=res_3m,
        adx_15m_result_at_placement=res_15m,
    )
    assert meta.adx_3m_result_at_placement is res_3m
    assert meta.adx_15m_result_at_placement is res_15m
