"""Per-side reentry cooldown + quality gate (Madde C).

Tests `BotRunner._check_reentry_gate()` directly — it's a pure function of
`ctx.last_close`, `ctx.config.reentry`, and the proposed plan. The gate is
also integration-tested once via `run_once` to confirm wiring into the
entry path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.bot.config import ReentryConfig
from src.bot.runner import BotRunner, LastCloseInfo, _tf_seconds
from tests.conftest import make_config


UTC = timezone.utc


def _now() -> datetime:
    return datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


def _seed_close(ctx, *, symbol="BTC-USDT-SWAP", side="long",
                price=67_000.0, confluence=3, outcome="WIN",
                closed_ago_s: float = 0.0) -> None:
    ctx.last_close[(symbol, side)] = LastCloseInfo(
        price=price,
        time=_now() - timedelta(seconds=closed_ago_s),
        confluence=confluence,
        outcome=outcome,
    )


# ── Time gate ───────────────────────────────────────────────────────────────


def test_cooldown_blocks_too_soon(make_ctx):
    # entry_tf=15m → 900s; min_bars=3 → 2700s needed.
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, closed_ago_s=900)          # only 1 bar elapsed
    runner = BotRunner(ctx)
    ok, reason = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=5, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is False
    assert reason == "cooldown_3bars"


def test_cooldown_passes_after_enough_bars(make_ctx):
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, closed_ago_s=3 * 900 + 1)
    runner = BotRunner(ctx)
    ok, _ = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=5, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is True


# ── ATR move gate ───────────────────────────────────────────────────────────


def test_atr_move_insufficient(make_ctx):
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, price=67_000.0, closed_ago_s=3 * 900 + 1)
    runner = BotRunner(ctx)
    # 100 price move / 500 ATR = 0.2 → below 0.5 threshold → block
    ok, reason = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=5, current_price=67_100.0,
        atr=500.0, now=_now(),
    )
    assert ok is False
    assert reason == "atr_move_insufficient"


# ── Quality gate: WIN ───────────────────────────────────────────────────────


def test_win_requires_higher_confluence(make_ctx):
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, confluence=3, outcome="WIN", closed_ago_s=3 * 900 + 1)
    runner = BotRunner(ctx)

    # Equal confluence → block
    ok, reason = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=3, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is False and reason == "post_win_needs_higher_confluence"

    # Higher → pass
    ok, _ = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=4, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is True


# ── Quality gate: LOSS ──────────────────────────────────────────────────────


def test_loss_requires_ge_confluence(make_ctx):
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, confluence=4, outcome="LOSS", closed_ago_s=3 * 900 + 1)
    runner = BotRunner(ctx)

    # Lower → block
    ok, reason = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=3, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is False and reason == "post_loss_needs_ge_confluence"

    # Equal → pass (loss doesn't require STRICTLY higher)
    ok, _ = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=4, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is True


# ── Side isolation ──────────────────────────────────────────────────────────


def test_opposite_side_no_cooldown(make_ctx):
    cfg = make_config(entry_timeframe="15m")
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, side="long", closed_ago_s=10)       # just closed a long
    runner = BotRunner(ctx)
    # Short entry is untouched by the long's cooldown
    ok, _ = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "short",
        proposed_confluence=3, current_price=70_000.0,
        atr=500.0, now=_now(),
    )
    assert ok is True


# ── Disabled flags ──────────────────────────────────────────────────────────


def test_flag_disabled_bypasses_quality_gate(make_ctx):
    """With both quality flags off, only time+ATR gates apply."""
    cfg = make_config(entry_timeframe="15m")
    # Override reentry config directly on the parsed object
    cfg.reentry = ReentryConfig(
        min_bars_after_close=3, min_atr_move=0.5,
        require_higher_confluence_after_win=False,
        require_higher_or_equal_confluence_after_loss=False,
    )
    ctx, _ = make_ctx(config=cfg)
    _seed_close(ctx, confluence=5, outcome="WIN", closed_ago_s=3 * 900 + 1)
    runner = BotRunner(ctx)
    ok, _ = runner._check_reentry_gate(
        "BTC-USDT-SWAP", "long",
        proposed_confluence=2,                # lower than last — would normally block
        current_price=70_000.0, atr=500.0, now=_now(),
    )
    assert ok is True


# ── Helper ──────────────────────────────────────────────────────────────────


def test_tf_seconds_helper():
    assert _tf_seconds("3m") == 180
    assert _tf_seconds("15m") == 900
    assert _tf_seconds("1h") == 3600
    assert _tf_seconds("4H") == 14400
    assert _tf_seconds("1D") == 86400
