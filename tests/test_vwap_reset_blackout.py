"""VWAP daily-reset blackout (2026-04-26).

Pine 1m/3m/15m VWAPs all anchor on the daily session change → reset at
UTC 00:00. The ±1σ band collapses for the first ~10-30 min and the
`vwap_composite_alignment` soft pillar reads near-noise. Time-based
blackout rejects new entries AND cancels resting pendings inside a
±window around UTC midnight.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.data.models import Direction
from src.strategy.entry_signals import (
    evaluate_pending_invalidation_gates,
    in_vwap_reset_blackout,
)
from tests.test_entry_signals import (
    _bull_ema_candles,
    _state_with_vwaps,
)


# ── pure helper: in_vwap_reset_blackout ─────────────────────────────────────


def _utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 4, 26, hour, minute, second, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now,expected",
    [
        (_utc(0, 0), True),    # exactly midnight
        (_utc(0, 14, 59), True),  # last second inside post-window
        (_utc(0, 15, 0), False),  # first second after post-window
        (_utc(23, 55, 0), True),  # first second of pre-window
        (_utc(23, 54, 59), False),  # last second before pre-window
        (_utc(12, 0, 0), False),  # mid-day
        (_utc(6, 30, 0), False),
        (_utc(18, 0, 0), False),
    ],
)
def test_blackout_window_edges(now, expected):
    """Window is [00:00 - 5min, 00:00 + 15min) — symmetric, half-open at end."""
    assert in_vwap_reset_blackout(
        now, pre_minutes=5, post_minutes=15
    ) is expected


def test_blackout_disabled_when_both_windows_zero():
    """Both knobs zero short-circuits to False even at midnight."""
    assert in_vwap_reset_blackout(
        _utc(0, 0), pre_minutes=0, post_minutes=0
    ) is False


def test_blackout_only_post_window():
    """pre_min=0 still catches post-midnight; pre side fails open."""
    assert in_vwap_reset_blackout(
        _utc(0, 5), pre_minutes=0, post_minutes=15
    ) is True
    assert in_vwap_reset_blackout(
        _utc(23, 50), pre_minutes=0, post_minutes=15
    ) is False


def test_blackout_only_pre_window():
    """post_min=0 still catches pre-midnight; post side fails open."""
    assert in_vwap_reset_blackout(
        _utc(23, 50), pre_minutes=15, post_minutes=0
    ) is True
    assert in_vwap_reset_blackout(
        _utc(0, 5), pre_minutes=15, post_minutes=0
    ) is False


def test_blackout_naive_datetime_treated_as_utc():
    """A naive datetime is read as UTC (matches `_utc_now()` runner output)."""
    naive = datetime(2026, 4, 26, 0, 5, 0)  # no tzinfo
    assert in_vwap_reset_blackout(
        naive, pre_minutes=5, post_minutes=15
    ) is True


def test_blackout_non_utc_timezone_converted():
    """A non-UTC timestamp is converted to UTC before the check.

    23:00 Asia/Tokyo (UTC+9) = 14:00 UTC → outside blackout even though the
    local-time clock reads "near midnight" in Tokyo. Catches the trap of
    using local time accidentally.
    """
    tokyo = timezone(timedelta(hours=9))
    aware = datetime(2026, 4, 26, 23, 0, 0, tzinfo=tokyo)
    assert in_vwap_reset_blackout(
        aware, pre_minutes=5, post_minutes=15
    ) is False


# ── integration: evaluate_pending_invalidation_gates ────────────────────────


def test_pending_eval_catches_vwap_reset_blackout():
    """Resting pending at 00:05 UTC → cancel with reason vwap_reset_blackout."""
    state = _state_with_vwaps(
        direction=Direction.BULLISH, price=100.0, vwaps=(99.0, 99.5, 99.0),
    )
    result = evaluate_pending_invalidation_gates(
        state=state,
        candles=_bull_ema_candles(),
        direction=Direction.BULLISH,
        entry_price=100.0,
        now=_utc(0, 5),
        vwap_reset_blackout_enabled=True,
        vwap_reset_blackout_pre_minutes=5,
        vwap_reset_blackout_post_minutes=15,
    )
    assert result == "vwap_reset_blackout"


def test_pending_eval_blackout_disabled_does_not_fire():
    """enabled=False → other gates evaluated normally even at midnight."""
    state = _state_with_vwaps(
        direction=Direction.BULLISH, price=100.0, vwaps=(99.0, 99.5, 99.0),
    )
    result = evaluate_pending_invalidation_gates(
        state=state,
        candles=_bull_ema_candles(),
        direction=Direction.BULLISH,
        entry_price=100.0,
        now=_utc(0, 5),
        vwap_reset_blackout_enabled=False,
        vwap_reset_blackout_pre_minutes=5,
        vwap_reset_blackout_post_minutes=15,
    )
    assert result is None


def test_pending_eval_blackout_outside_window_does_not_fire():
    """At 12:00 UTC the blackout sleeps; other gates take over."""
    state = _state_with_vwaps(
        direction=Direction.BULLISH, price=100.0, vwaps=(99.0, 99.5, 99.0),
    )
    result = evaluate_pending_invalidation_gates(
        state=state,
        candles=_bull_ema_candles(),
        direction=Direction.BULLISH,
        entry_price=100.0,
        now=_utc(12, 0),
        vwap_reset_blackout_enabled=True,
        vwap_reset_blackout_pre_minutes=5,
        vwap_reset_blackout_post_minutes=15,
    )
    assert result is None


def test_pending_eval_blackout_check_runs_before_vwap_misaligned():
    """When BOTH blackout AND vwap_misaligned would trigger, blackout wins.

    Order matters for journal stamping: blackout is the *root cause* during
    the reset window because vwap_misaligned itself reads the unreliable
    just-reset VWAPs. Catching it first attributes the reject correctly.
    """
    state = _state_with_vwaps(
        # price below all VWAPs → would be vwap_misaligned for a long
        direction=Direction.BULLISH, price=99.5, vwaps=(100.0, 101.0, 102.0),
    )
    result = evaluate_pending_invalidation_gates(
        state=state,
        candles=_bull_ema_candles(),
        direction=Direction.BULLISH,
        entry_price=99.5,
        vwap_hard_veto_enabled=True,
        now=_utc(0, 10),
        vwap_reset_blackout_enabled=True,
        vwap_reset_blackout_pre_minutes=5,
        vwap_reset_blackout_post_minutes=15,
    )
    assert result == "vwap_reset_blackout"


def test_pending_eval_default_kwargs_no_blackout():
    """Back-compat: default kwargs (enabled=False, both=0) leave behavior
    unchanged for callers that haven't been migrated."""
    state = _state_with_vwaps(
        direction=Direction.BULLISH, price=100.0, vwaps=(99.0, 99.5, 99.0),
    )
    result = evaluate_pending_invalidation_gates(
        state=state,
        candles=_bull_ema_candles(),
        direction=Direction.BULLISH,
        entry_price=100.0,
        # no `now=`, no blackout kwargs
    )
    assert result is None


# ── config validation ──────────────────────────────────────────────────────


def test_config_rejects_negative_pre_window():
    from src.bot.config import AnalysisConfig
    with pytest.raises(ValueError):
        AnalysisConfig(vwap_reset_blackout_window_pre_min=-1)


def test_config_rejects_pre_window_above_60():
    from src.bot.config import AnalysisConfig
    with pytest.raises(ValueError):
        AnalysisConfig(vwap_reset_blackout_window_pre_min=61)


def test_config_rejects_negative_post_window():
    from src.bot.config import AnalysisConfig
    with pytest.raises(ValueError):
        AnalysisConfig(vwap_reset_blackout_window_post_min=-1)


def test_config_defaults_match_yaml_intent():
    from src.bot.config import AnalysisConfig
    cfg = AnalysisConfig()
    assert cfg.vwap_reset_blackout_enabled is True
    assert cfg.vwap_reset_blackout_window_pre_min == 5
    assert cfg.vwap_reset_blackout_window_post_min == 15
