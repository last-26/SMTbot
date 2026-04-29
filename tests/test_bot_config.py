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
        "bybit": {
            "base_url": "https://api-demo.bybit.com",
            "demo": True, "account_type": "UNIFIED", "category": "linear",
            "api_key": "k", "api_secret": "s",
        },
        "journal": {"db_path": "data/trades.db"},
        # rl is tolerated but ignored
        "rl": {"foo": "bar"},
    }


def test_load_config_from_yaml_and_env(tmp_path, monkeypatch):
    raw = _valid_raw()
    # YAML-only bybit section (no secrets); env must fill them
    del raw["bybit"]["api_key"]; del raw["bybit"]["api_secret"]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("BYBIT_API_KEY", "env_key")
    monkeypatch.setenv("BYBIT_API_SECRET", "env_sec")
    monkeypatch.setenv("BYBIT_DEMO", "1")

    # Force env_path to an empty file so python-dotenv doesn't override monkeypatch
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    cfg = load_config(cfg_path, env_path=empty_env)
    assert isinstance(cfg, BotConfig)
    assert cfg.bybit.api_key == "env_key"
    assert cfg.bybit.demo is True
    # Legacy `trading.symbol:` form is coerced into `symbols=[symbol]`.
    assert cfg.trading.symbols == ["BTC-USDT-SWAP"]
    assert cfg.primary_symbol() == "BTC-USDT-SWAP"


def test_missing_bybit_credentials_raises(tmp_path, monkeypatch):
    raw = _valid_raw()
    del raw["bybit"]["api_key"]; del raw["bybit"]["api_secret"]
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    for v in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
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


def test_to_bybit_credentials_carries_demo_flag():
    cfg = BotConfig(**_valid_raw())
    creds = cfg.to_bybit_credentials()
    assert creds.demo is True
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


def test_risk_amount_usdt_null_default():
    """Absent → None (legacy percent mode)."""
    cfg = BotConfig(**_valid_raw())
    assert cfg.trading.risk_amount_usdt is None


def test_risk_amount_usdt_parsed_from_yaml():
    raw = _valid_raw()
    raw["trading"]["risk_amount_usdt"] = 50.0
    cfg = BotConfig(**raw)
    assert cfg.trading.risk_amount_usdt == 50.0


def test_risk_amount_usdt_rejects_non_positive():
    raw = _valid_raw()
    raw["trading"]["risk_amount_usdt"] = 0.0
    with pytest.raises(ValidationError, match="must be > 0"):
        BotConfig(**raw)
    raw["trading"]["risk_amount_usdt"] = -5.0
    with pytest.raises(ValidationError, match="must be > 0"):
        BotConfig(**raw)


def test_risk_amount_usdt_env_wins_over_yaml(tmp_path, monkeypatch):
    """RISK_AMOUNT_USDT env var overrides YAML so operator can bump $R
    between restarts without editing checked-in config."""
    raw = _valid_raw()
    raw["trading"]["risk_amount_usdt"] = 50.0   # YAML says $50
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("RISK_AMOUNT_USDT", "75.5")  # env says $75.5
    for v in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_DEMO"):
        monkeypatch.setenv(v, "1" if v == "BYBIT_DEMO" else "x")
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    cfg = load_config(cfg_path, env_path=empty_env)
    assert cfg.trading.risk_amount_usdt == 75.5


def test_risk_amount_usdt_env_empty_falls_back_to_yaml(tmp_path, monkeypatch):
    """Unset / empty env var preserves YAML behavior so dev boxes without
    .env entries keep percent-mode defaults."""
    raw = _valid_raw()
    raw["trading"]["risk_amount_usdt"] = 50.0
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.delenv("RISK_AMOUNT_USDT", raising=False)
    for v in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_DEMO"):
        monkeypatch.setenv(v, "1" if v == "BYBIT_DEMO" else "x")
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    cfg = load_config(cfg_path, env_path=empty_env)
    assert cfg.trading.risk_amount_usdt == 50.0


def test_risk_amount_usdt_env_rejects_invalid_float(tmp_path, monkeypatch):
    raw = _valid_raw()
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setenv("RISK_AMOUNT_USDT", "notanumber")
    for v in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_DEMO"):
        monkeypatch.setenv(v, "1" if v == "BYBIT_DEMO" else "x")
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="not a valid float"):
        load_config(cfg_path, env_path=empty_env)


def test_vwap_zone_anchor_defaults():
    """2026-04-21 default: 0.7 long / 0.3 short (pulls entry closer to VWAP
    than the pre-pivot 0.5σ midpoint, which was 0.75 / 0.25 in Convention X)."""
    cfg = BotConfig(**_valid_raw())
    assert cfg.analysis.vwap_zone_long_anchor == 0.7
    assert cfg.analysis.vwap_zone_short_anchor == 0.3


def test_vwap_zone_anchor_accepts_operator_eq_hug():
    """Operator-facing knob: 0.65 / 0.35 is a valid "EQ-hug" variant the
    operator explicitly called out as a nudge option."""
    raw = _valid_raw()
    raw["analysis"]["vwap_zone_long_anchor"] = 0.65
    raw["analysis"]["vwap_zone_short_anchor"] = 0.35
    cfg = BotConfig(**raw)
    assert cfg.analysis.vwap_zone_long_anchor == 0.65
    assert cfg.analysis.vwap_zone_short_anchor == 0.35


def test_vwap_zone_long_anchor_rejects_below_half():
    """Long anchor must stay on the upper half of the band — below 0.5
    would place a long entry below VWAP (wrong structural side)."""
    raw = _valid_raw()
    raw["analysis"]["vwap_zone_long_anchor"] = 0.45
    with pytest.raises(ValidationError, match=r"vwap_zone_long_anchor"):
        BotConfig(**raw)


def test_vwap_zone_long_anchor_rejects_above_one():
    raw = _valid_raw()
    raw["analysis"]["vwap_zone_long_anchor"] = 1.1
    with pytest.raises(ValidationError, match=r"vwap_zone_long_anchor"):
        BotConfig(**raw)


def test_vwap_zone_short_anchor_rejects_above_half():
    """Short anchor must stay on the lower half — above 0.5 would place a
    short entry above VWAP (wrong structural side)."""
    raw = _valid_raw()
    raw["analysis"]["vwap_zone_short_anchor"] = 0.6
    with pytest.raises(ValidationError, match=r"vwap_zone_short_anchor"):
        BotConfig(**raw)


def test_vwap_zone_short_anchor_rejects_negative():
    raw = _valid_raw()
    raw["analysis"]["vwap_zone_short_anchor"] = -0.1
    with pytest.raises(ValidationError, match=r"vwap_zone_short_anchor"):
        BotConfig(**raw)


def test_symbol_leverage_caps_lookup_missing_symbol_returns_default():
    """Unlisted symbols return the caller's default — the runner merges
    min(global, instrument_cap, ...dict.get(sym, global)) so missing key = no cap."""
    raw = _valid_raw()
    raw["trading"]["symbol_leverage_caps"] = {"ETH-USDT-SWAP": 30}
    cfg = BotConfig(**raw)
    # BTC not in dict → dict.get returns the default we pass at call site.
    assert cfg.trading.symbol_leverage_caps.get(
        "BTC-USDT-SWAP", cfg.trading.max_leverage
    ) == cfg.trading.max_leverage


# ── Phase 6.9 B2/B3/B4 — per-symbol overrides ──────────────────────────────


def test_swing_lookback_for_symbol_uses_override_when_present():
    raw = _valid_raw()
    raw["trading"]["swing_lookback_per_symbol"] = {"DOGE-USDT-SWAP": 30}
    cfg = BotConfig(**raw)
    assert cfg.swing_lookback_for("DOGE-USDT-SWAP") == 30
    # Unlisted → falls back to analysis.swing_lookback (20 in _valid_raw)
    assert cfg.swing_lookback_for("BTC-USDT-SWAP") == 20


def test_htf_sr_buffer_atr_for_symbol_uses_override_when_present():
    raw = _valid_raw()
    raw["analysis"]["htf_sr_buffer_atr_per_symbol"] = {"SOL-USDT-SWAP": 0.10}
    cfg = BotConfig(**raw)
    assert cfg.htf_sr_buffer_atr_for("SOL-USDT-SWAP") == pytest.approx(0.10)
    # Unlisted → analysis.htf_sr_buffer_atr default (0.2).
    assert cfg.htf_sr_buffer_atr_for("BTC-USDT-SWAP") == pytest.approx(0.2)


def test_allowed_sessions_for_symbol_override_replaces_global_not_merges():
    raw = _valid_raw()
    raw["analysis"]["session_filter"] = ["london", "new_york"]
    raw["analysis"]["session_filter_per_symbol"] = {"SOL-USDT-SWAP": ["london"]}
    cfg = BotConfig(**raw)
    # Override is total — SOL doesn't inherit new_york from the global.
    assert cfg.allowed_sessions_for("SOL-USDT-SWAP") == [Session.LONDON]
    # Unlisted symbol gets the global list.
    assert cfg.allowed_sessions_for("BTC-USDT-SWAP") == [
        Session.LONDON, Session.NEW_YORK,
    ]


def test_per_symbol_overrides_default_to_empty_dicts():
    cfg = BotConfig(**_valid_raw())
    assert cfg.trading.swing_lookback_per_symbol == {}
    assert cfg.analysis.htf_sr_buffer_atr_per_symbol == {}
    assert cfg.analysis.session_filter_per_symbol == {}


# ── Phase 6.9 D1 — rl.clean_since parsing ──────────────────────────────────


def test_rl_clean_since_none_when_unset():
    """`_valid_raw` has `rl: {foo: bar}` — no `clean_since`, resolver returns None."""
    cfg = BotConfig(**_valid_raw())
    assert cfg.rl_clean_since() is None


def test_rl_clean_since_parsed_as_utc_datetime():
    from datetime import datetime, timezone
    raw = _valid_raw()
    raw["rl"] = {"clean_since": "2026-04-17T23:50:00Z"}
    cfg = BotConfig(**raw)
    assert cfg.rl_clean_since() == datetime(
        2026, 4, 17, 23, 50, tzinfo=timezone.utc,
    )


def test_rl_clean_since_naive_iso_assumed_utc():
    from datetime import datetime, timezone
    raw = _valid_raw()
    raw["rl"] = {"clean_since": "2026-04-17T23:50:00"}  # no tz
    cfg = BotConfig(**raw)
    assert cfg.rl_clean_since() == datetime(
        2026, 4, 17, 23, 50, tzinfo=timezone.utc,
    )


def test_rl_unknown_keys_still_tolerated():
    """RLConfig has `extra=ignore` so Phase 7 additions don't break load."""
    raw = _valid_raw()
    raw["rl"] = {"clean_since": None, "foo": "bar", "hyperparams": {"lr": 0.01}}
    cfg = BotConfig(**raw)
    assert cfg.rl_clean_since() is None


# ── OnChainConfig (2026-04-21 Arkham integration — Phase A) ─────────────────


def test_on_chain_defaults_master_and_subfeatures_all_off():
    raw = _valid_raw()
    cfg = BotConfig(**raw)
    assert cfg.on_chain.enabled is False
    assert cfg.on_chain.daily_bias_enabled is False
    assert cfg.on_chain.stablecoin_pulse_enabled is False
    assert cfg.on_chain.whale_blackout_enabled is False
    # Default threshold / duration values match documented pivot.
    assert cfg.on_chain.daily_bias_modifier_delta == 0.15
    assert cfg.on_chain.stablecoin_pulse_penalty == 0.75
    # 2026-04-22: bumped 100M → 150M to halve label-lookup tax on
    # the WS whale stream (paired with whale_tokens whitelist).
    assert cfg.on_chain.whale_threshold_usd == 150_000_000.0
    assert cfg.on_chain.whale_blackout_duration_s == 600
    assert cfg.on_chain.api_usage_auto_disable_pct == 95.0
    # whale_tokens — 4 perps + 2 stablecoins, slug-format (Arkham coingecko ids).
    # 2026-04-25 Bybit migration: BNB→XRP swap; XRP intentionally omitted because
    # Arkham doesn't index XRPL per-token data (every XRP slug variant rejected).
    assert cfg.on_chain.whale_tokens == [
        "bitcoin", "ethereum", "solana", "dogecoin",
        "tether", "usd-coin",
    ]


def test_on_chain_flags_load_from_yaml():
    raw = _valid_raw()
    raw["on_chain"] = {
        "enabled": True,
        "daily_bias_enabled": True,
        "daily_bias_modifier_delta": 0.20,
        "whale_blackout_enabled": True,
        "whale_threshold_usd": 200_000_000.0,
    }
    cfg = BotConfig(**raw)
    assert cfg.on_chain.enabled is True
    assert cfg.on_chain.daily_bias_enabled is True
    assert cfg.on_chain.daily_bias_modifier_delta == 0.20
    assert cfg.on_chain.whale_threshold_usd == 200_000_000.0


def test_on_chain_daily_bias_delta_out_of_range_rejected():
    raw = _valid_raw()
    raw["on_chain"] = {"daily_bias_modifier_delta": 0.6}  # > 0.5
    with pytest.raises(ValidationError):
        BotConfig(**raw)

    raw["on_chain"] = {"daily_bias_modifier_delta": -0.1}
    with pytest.raises(ValidationError):
        BotConfig(**raw)


def test_on_chain_whale_threshold_below_arkham_minimum_rejected():
    raw = _valid_raw()
    raw["on_chain"] = {"whale_threshold_usd": 5_000_000.0}  # < 10M
    with pytest.raises(ValidationError):
        BotConfig(**raw)


def test_on_chain_whale_threshold_at_minimum_accepted():
    raw = _valid_raw()
    raw["on_chain"] = {"whale_threshold_usd": 10_000_000.0}
    cfg = BotConfig(**raw)
    assert cfg.on_chain.whale_threshold_usd == 10_000_000.0


def test_on_chain_durations_must_be_positive():
    for field in ("whale_blackout_duration_s", "stablecoin_pulse_refresh_s",
                  "snapshot_staleness_threshold_s"):
        raw = _valid_raw()
        raw["on_chain"] = {field: 0}
        with pytest.raises(ValidationError):
            BotConfig(**raw)


def test_on_chain_auto_disable_pct_must_be_in_open_100():
    for bad in (0.0, -5.0, 101.0, 200.0):
        raw = _valid_raw()
        raw["on_chain"] = {"api_usage_auto_disable_pct": bad}
        with pytest.raises(ValidationError):
            BotConfig(**raw)

    # Boundary: exactly 100 is accepted (no-op), 0.0 is not (would disable
    # immediately). Keeping the open-left / closed-right contract.
    raw = _valid_raw()
    raw["on_chain"] = {"api_usage_auto_disable_pct": 100.0}
    cfg = BotConfig(**raw)
    assert cfg.on_chain.api_usage_auto_disable_pct == 100.0


def test_on_chain_thresholds_reject_negatives():
    for field in ("daily_bias_stablecoin_threshold_usd",
                  "daily_bias_btc_netflow_threshold_usd",
                  "stablecoin_pulse_threshold_usd",
                  "stablecoin_pulse_penalty"):
        raw = _valid_raw()
        raw["on_chain"] = {field: -1.0}
        with pytest.raises(ValidationError):
            BotConfig(**raw)


def test_on_chain_section_absent_still_produces_default():
    raw = _valid_raw()
    raw.pop("on_chain", None)
    cfg = BotConfig(**raw)
    # Default factory gives us the fully-off OnChainConfig.
    assert cfg.on_chain.enabled is False


# ── JournalConfig — intra-trade snapshot writer ───────────────────────────


def test_journal_position_snapshot_defaults_match_yaml_intent():
    raw = _valid_raw()
    cfg = BotConfig(**raw)
    assert cfg.journal.position_snapshot_enabled is True
    assert cfg.journal.position_snapshot_cadence_s == 300


def test_journal_position_snapshot_cadence_rejects_below_floor():
    raw = _valid_raw()
    raw["journal"]["position_snapshot_cadence_s"] = 30
    with pytest.raises(ValidationError, match=r"position_snapshot_cadence_s"):
        BotConfig(**raw)


def test_journal_position_snapshot_cadence_rejects_above_ceiling():
    raw = _valid_raw()
    raw["journal"]["position_snapshot_cadence_s"] = 7200
    with pytest.raises(ValidationError, match=r"position_snapshot_cadence_s"):
        BotConfig(**raw)


def test_journal_position_snapshot_cadence_accepts_boundaries():
    raw = _valid_raw()
    raw["journal"]["position_snapshot_cadence_s"] = 60
    cfg = BotConfig(**raw)
    assert cfg.journal.position_snapshot_cadence_s == 60
    raw["journal"]["position_snapshot_cadence_s"] = 3600
    cfg = BotConfig(**raw)
    assert cfg.journal.position_snapshot_cadence_s == 3600


def test_journal_position_snapshot_can_be_disabled():
    raw = _valid_raw()
    raw["journal"]["position_snapshot_enabled"] = False
    cfg = BotConfig(**raw)
    assert cfg.journal.position_snapshot_enabled is False
