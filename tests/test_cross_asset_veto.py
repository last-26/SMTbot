"""Runner-level cross-asset pillar veto helpers (Phase 7.A6).

`BotRunner._pillar_opposition_for(symbol)` resolves the opposition
signal handed to `build_trade_plan_with_reason` when the cross-asset
veto is enabled. Covers the fail-open paths (disabled, pillar symbol
itself, missing pillar, neutral pillar, stale pillar) and the fires
paths (both pillars bullish / both bearish).
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from src.bot.runner import BotRunner, _utc_now
from src.data.models import Direction


pytestmark = pytest.mark.anyio


UTC = timezone.utc


def _enable_cross_asset(cfg) -> None:
    cfg.analysis.cross_asset_veto_enabled = True
    cfg.analysis.cross_asset_veto_max_age_s = 300.0


def test_returns_none_when_veto_disabled(make_ctx):
    ctx, _ = make_ctx()
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.BULLISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") is None


def test_returns_none_for_pillar_symbol_itself(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.BULLISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("BTC-USDT-SWAP") is None
    assert runner._pillar_opposition_for("ETH-USDT-SWAP") is None


def test_returns_none_when_pillar_missing(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {"BTC-USDT-SWAP": (Direction.BULLISH, now)}
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") is None


def test_returns_none_when_pillar_neutral(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.UNDEFINED, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") is None


def test_returns_none_when_pillar_stale(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    stale = now - timedelta(seconds=ctx.config.analysis.cross_asset_veto_max_age_s + 1)
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, stale),
        "ETH-USDT-SWAP": (Direction.BULLISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") is None


def test_returns_none_when_pillars_disagree(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.BEARISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") is None


def test_returns_bullish_when_both_pillars_bullish(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BULLISH, now),
        "ETH-USDT-SWAP": (Direction.BULLISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") == Direction.BULLISH


def test_returns_bearish_when_both_pillars_bearish(make_ctx):
    ctx, _ = make_ctx()
    _enable_cross_asset(ctx.config)
    now = _utc_now()
    ctx.pillar_bias = {
        "BTC-USDT-SWAP": (Direction.BEARISH, now),
        "ETH-USDT-SWAP": (Direction.BEARISH, now),
    }
    runner = BotRunner(ctx)
    assert runner._pillar_opposition_for("SOL-USDT-SWAP") == Direction.BEARISH
