"""Tests for the Arkham altcoin-index penalty (Phase F2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.bot.config import BotConfig
from src.data.models import Direction
from src.strategy.entry_signals import _altcoin_index_penalty


# ── _altcoin_index_penalty pure function ───────────────────────────────────


def test_penalty_zero_when_penalty_config_zero():
    assert _altcoin_index_penalty(
        direction=Direction.BULLISH, index_value=5, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.0,
    ) == 0.0


def test_penalty_zero_when_index_is_none():
    assert _altcoin_index_penalty(
        direction=Direction.BULLISH, index_value=None, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.0


def test_penalty_zero_when_not_altcoin():
    """Majors (BTC / ETH) never pay the penalty regardless of index."""
    for direction in (Direction.BULLISH, Direction.BEARISH):
        for index_value in (0, 10, 50, 90, 100):
            assert _altcoin_index_penalty(
                direction=direction, index_value=index_value, is_altcoin=False,
                bearish_threshold=25, bullish_threshold=75, penalty=0.5,
            ) == 0.0


def test_penalty_altcoin_long_btc_dominance_misaligned():
    # index=19 ≤ bearish_threshold=25 → altcoin long is misaligned.
    assert _altcoin_index_penalty(
        direction=Direction.BULLISH, index_value=19, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.5


def test_penalty_altcoin_short_altseason_misaligned():
    # index=80 ≥ bullish_threshold=75 → altcoin short is misaligned.
    assert _altcoin_index_penalty(
        direction=Direction.BEARISH, index_value=80, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.5


def test_penalty_altcoin_long_altseason_aligned():
    # High index + altcoin long = aligned (riding altseason). No penalty.
    assert _altcoin_index_penalty(
        direction=Direction.BULLISH, index_value=85, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.0


def test_penalty_altcoin_short_btc_dominance_aligned():
    # Low index + altcoin short = aligned (fading weak alts). No penalty.
    assert _altcoin_index_penalty(
        direction=Direction.BEARISH, index_value=15, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.0


def test_penalty_zero_in_neutral_band():
    """Neutral band (bearish < v < bullish) → no penalty either direction."""
    for direction in (Direction.BULLISH, Direction.BEARISH):
        assert _altcoin_index_penalty(
            direction=direction, index_value=50, is_altcoin=True,
            bearish_threshold=25, bullish_threshold=75, penalty=0.5,
        ) == 0.0


def test_penalty_at_exact_thresholds_fires():
    """Boundary: index == bearish_threshold → long penalty; index ==
    bullish_threshold → short penalty (inclusive)."""
    assert _altcoin_index_penalty(
        direction=Direction.BULLISH, index_value=25, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.5
    assert _altcoin_index_penalty(
        direction=Direction.BEARISH, index_value=75, is_altcoin=True,
        bearish_threshold=25, bullish_threshold=75, penalty=0.5,
    ) == 0.5


# ── OnChainConfig validators (Phase F2) ────────────────────────────────────


def _valid_raw() -> dict:
    return {
        "bot": {"mode": "demo", "poll_interval_seconds": 5,
                "timezone": "UTC", "starting_balance": 10000.0},
        "trading": {
            "symbol": "BTC-USDT-SWAP", "entry_timeframe": "15m",
            "htf_timeframe": "4H", "risk_per_trade_pct": 1.0,
            "max_leverage": 20, "default_rr_ratio": 3.0,
            "min_rr_ratio": 2.0, "max_concurrent_positions": 2,
            "contract_size": 0.01,
        },
        "circuit_breakers": {"max_daily_loss_pct": 3.0,
                             "max_consecutive_losses": 5,
                             "max_drawdown_pct": 10.0, "cooldown_hours": 24},
        "analysis": {"min_confluence_score": 2, "candle_buffer_size": 500,
                     "swing_lookback": 20, "sr_min_touches": 3,
                     "sr_zone_atr_mult": 0.5,
                     "session_filter": ["london", "new_york"]},
        "bybit": {"api_key": "k", "api_secret": "s", "demo": True},
        "journal": {"db_path": ":memory:"},
    }


def test_altcoin_index_defaults_off():
    cfg = BotConfig(**_valid_raw())
    assert cfg.on_chain.altcoin_index_enabled is False
    assert cfg.on_chain.altcoin_index_bearish_threshold == 25
    assert cfg.on_chain.altcoin_index_bullish_threshold == 75
    assert cfg.on_chain.altcoin_index_modifier_delta == 0.5
    assert cfg.on_chain.altcoin_index_refresh_s == 3600


def test_altcoin_index_thresholds_must_be_in_0_100():
    raw = _valid_raw()
    raw["on_chain"] = {"altcoin_index_bearish_threshold": -1}
    with pytest.raises(ValidationError):
        BotConfig(**raw)
    raw["on_chain"] = {"altcoin_index_bullish_threshold": 101}
    with pytest.raises(ValidationError):
        BotConfig(**raw)


def test_altcoin_index_bearish_must_be_less_than_bullish():
    raw = _valid_raw()
    raw["on_chain"] = {
        "altcoin_index_bearish_threshold": 80,
        "altcoin_index_bullish_threshold": 30,
    }
    with pytest.raises(ValidationError):
        BotConfig(**raw)


def test_altcoin_index_equal_thresholds_rejected():
    """Equal thresholds would collapse the neutral band — every index
    value would fire a penalty. Must be strictly less."""
    raw = _valid_raw()
    raw["on_chain"] = {
        "altcoin_index_bearish_threshold": 50,
        "altcoin_index_bullish_threshold": 50,
    }
    with pytest.raises(ValidationError):
        BotConfig(**raw)


def test_altcoin_index_refresh_must_be_positive():
    raw = _valid_raw()
    raw["on_chain"] = {"altcoin_index_refresh_s": 0}
    with pytest.raises(ValidationError):
        BotConfig(**raw)
