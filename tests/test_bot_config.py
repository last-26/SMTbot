"""Tests for src.bot.config — YAML + .env → BotConfig, derived views."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from src.bot.config import BotConfig, load_config
from src.data.models import Session


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
        "circuit_breakers": {
            "max_daily_loss_pct": 3.0, "max_consecutive_losses": 5,
            "max_drawdown_pct": 10.0, "cooldown_hours": 24,
        },
        "analysis": {
            "min_confluence_score": 2, "candle_buffer_size": 500,
            "swing_lookback": 20, "sr_min_touches": 3,
            "sr_zone_atr_mult": 0.5,
            "session_filter": ["london", "new_york"],
        },
        "okx": {
            "base_url": "https://www.okx.com",
            "demo_flag": "1",
            "api_key": "k", "api_secret": "s", "passphrase": "p",
        },
        "journal": {"db_path": "data/trades.db"},
        # rl is tolerated but ignored
        "rl": {"foo": "bar"},
    }


def test_load_config_from_yaml_and_env(tmp_path, monkeypatch):
    raw = _valid_raw()
    # YAML-only okx section (no secrets); env must fill them
    del raw["okx"]["api_key"]; del raw["okx"]["api_secret"]; del raw["okx"]["passphrase"]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("OKX_API_KEY", "env_key")
    monkeypatch.setenv("OKX_API_SECRET", "env_sec")
    monkeypatch.setenv("OKX_PASSPHRASE", "env_pass")
    monkeypatch.setenv("OKX_DEMO_FLAG", "1")

    # Force env_path to an empty file so python-dotenv doesn't override monkeypatch
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    cfg = load_config(cfg_path, env_path=empty_env)
    assert isinstance(cfg, BotConfig)
    assert cfg.okx.api_key == "env_key"
    assert cfg.okx.demo_flag == "1"
    # Legacy `trading.symbol:` form is coerced into `symbols=[symbol]`.
    assert cfg.trading.symbols == ["BTC-USDT-SWAP"]
    assert cfg.primary_symbol() == "BTC-USDT-SWAP"


def test_missing_okx_credentials_raises(tmp_path, monkeypatch):
    raw = _valid_raw()
    del raw["okx"]["api_key"]; del raw["okx"]["api_secret"]; del raw["okx"]["passphrase"]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    for v in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(v, raising=False)

    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(cfg_path, env_path=empty_env)


def test_risk_pct_fraction_converts_percent_to_fraction():
    cfg = BotConfig(**_valid_raw())
    assert cfg.risk_pct_fraction() == pytest.approx(0.01)


def test_allowed_sessions_maps_strings_to_enums():
    cfg = BotConfig(**_valid_raw())
    sessions = cfg.allowed_sessions()
    assert sessions == [Session.LONDON, Session.NEW_YORK]


def test_to_okx_credentials_carries_demo_flag():
    cfg = BotConfig(**_valid_raw())
    creds = cfg.to_okx_credentials()
    assert creds.demo_flag == "1"
    assert creds.api_key == "k"


def test_breakers_pulls_caps_from_trading_section():
    cfg = BotConfig(**_valid_raw())
    cb = cfg.breakers()
    # From circuit_breakers:
    assert cb.max_daily_loss_pct == 3.0
    assert cb.max_consecutive_losses == 5
    # From trading:
    assert cb.max_concurrent_positions == 2
    assert cb.max_leverage == 20
    assert cb.min_rr_ratio == 2.0


def test_symbol_leverage_caps_default_empty():
    """Omitting `trading.symbol_leverage_caps` falls back to an empty dict."""
    cfg = BotConfig(**_valid_raw())
    assert cfg.trading.symbol_leverage_caps == {}


def test_symbol_leverage_caps_parsed_from_yaml():
    raw = _valid_raw()
    raw["trading"]["symbol_leverage_caps"] = {
        "ETH-USDT-SWAP": 30,
        "SOL-USDT-SWAP": 25,
    }
    cfg = BotConfig(**raw)
    assert cfg.trading.symbol_leverage_caps["ETH-USDT-SWAP"] == 30
    assert cfg.trading.symbol_leverage_caps["SOL-USDT-SWAP"] == 25


def test_symbol_leverage_caps_lookup_missing_symbol_returns_default():
    """Unlisted symbols return the caller's default — the runner merges
    min(global, okx_cap, ...dict.get(sym, global)) so missing key = no cap."""
    raw = _valid_raw()
    raw["trading"]["symbol_leverage_caps"] = {"ETH-USDT-SWAP": 30}
    cfg = BotConfig(**raw)
    # BTC not in dict → dict.get returns the default we pass at call site.
    assert cfg.trading.symbol_leverage_caps.get(
        "BTC-USDT-SWAP", cfg.trading.max_leverage
    ) == cfg.trading.max_leverage
