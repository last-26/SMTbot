"""Typed bot configuration loaded from YAML + .env.

YAML layout is kept 1:1 with `config/default.yaml` — top-level keys
`bot`, `trading`, `circuit_breakers`, `analysis`, `okx`, `journal` map
directly to Pydantic sections below. Secrets (`OKX_API_KEY/SECRET/PASSPHRASE`)
are read from `.env`/environment and merged into the `okx` section before
validation, so the YAML can live in git without leaking credentials.

`rl:` is tolerated at the top level but not parsed here — Phase 7 will own it.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.data.models import Session
from src.execution.okx_client import OKXCredentials
from src.strategy.risk_manager import CircuitBreakerConfig


class RuntimeConfig(BaseModel):
    """Outer bot loop settings (the YAML `bot:` block)."""
    mode: Literal["demo", "live", "dry_run"] = "demo"
    poll_interval_seconds: float = 5.0
    timezone: str = "UTC"
    starting_balance: float = 10_000.0


class TradingConfig(BaseModel):
    symbol: Optional[str] = None                  # deprecated single-symbol form
    symbols: list[str] = Field(default_factory=list)
    entry_timeframe: str                          # e.g. "3m"
    htf_timeframe: str                            # e.g. "15m"
    ltf_timeframe: str = "1m"                     # low-TF reversal (Madde F)
    risk_per_trade_pct: float                     # percent (e.g. 1.0 → 1 %)
    max_leverage: int
    default_rr_ratio: float
    min_rr_ratio: float
    max_concurrent_positions: int
    contract_size: float = 0.01                   # BTC-USDT-SWAP lot size
    symbol_settle_seconds: float = 4.0            # wait after set_symbol (Madde A/B)
    tf_settle_seconds: float = 2.5                # wait after set_timeframe (Madde B)
    pine_settle_max_wait_s: float = 6.0           # freshness-poll timeout (Madde B)
    pine_settle_poll_interval_s: float = 0.3

    @model_validator(mode="after")
    def _coerce_symbols(self) -> "TradingConfig":
        """Backward compat: single `symbol` → list. `symbols` takes precedence."""
        if not self.symbols:
            if self.symbol:
                warnings.warn(
                    "trading.symbol is deprecated; use trading.symbols: [<list>]",
                    DeprecationWarning, stacklevel=2,
                )
                self.symbols = [self.symbol]
            else:
                raise ValueError(
                    "trading.symbols must be non-empty "
                    "(or legacy trading.symbol set)"
                )
        return self


class CircuitBreakerSection(BaseModel):
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 5
    max_drawdown_pct: float = 10.0
    cooldown_hours: int = 24

    def to_dataclass(self, *, max_concurrent: int, max_leverage: int,
                     min_rr_ratio: float) -> CircuitBreakerConfig:
        """Combine with per-trade caps from `trading:` into the dataclass
        that RiskManager actually consumes."""
        return CircuitBreakerConfig(
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            max_drawdown_pct=self.max_drawdown_pct,
            max_concurrent_positions=max_concurrent,
            max_leverage=max_leverage,
            min_rr_ratio=min_rr_ratio,
            cooldown_hours=self.cooldown_hours,
        )


class SessionFilter(BaseModel):
    """Wrapper so pydantic validates the list members as lowercase strings."""
    values: list[str] = Field(default_factory=list)


class AnalysisConfig(BaseModel):
    min_confluence_score: float = 2.0
    candle_buffer_size: int = 500
    swing_lookback: int = 20
    sr_min_touches: int = 3
    sr_zone_atr_mult: float = 0.5
    session_filter: list[str] = Field(default_factory=list)
    htf_sr_ceiling_enabled: bool = True      # Madde D
    htf_sr_buffer_atr: float = 0.2
    # Fee-aware min TP distance — a TP closer than this fraction of entry
    # price cannot survive the partial-TP 3-fill lifecycle net of taker fees
    # + slippage. Default 0 = off for test back-compat; runtime YAML sets it.
    min_tp_distance_pct: float = 0.0
    # Min SL distance floor — a stop closer than this fraction of entry price
    # sits inside normal bid/ask + demo wick noise. Tight SL = high leverage
    # = large notional = fee drag. Default 0 = off (back-compat); runtime
    # YAML sets ~0.003 so stops sit at least ~3× spread + typical wick width.
    min_sl_distance_pct: float = 0.0


class OKXConfigBlock(BaseModel):
    base_url: str = "https://www.okx.com"
    demo_flag: str = "1"
    api_key: str
    api_secret: str
    passphrase: str

    def to_credentials(self) -> OKXCredentials:
        return OKXCredentials(
            api_key=self.api_key,
            api_secret=self.api_secret,
            passphrase=self.passphrase,
            demo_flag=self.demo_flag,
        )


class JournalConfig(BaseModel):
    db_path: str = "data/trades.db"


class ExecutionConfig(BaseModel):
    """Execution-layer knobs (partial TP + SL-to-BE in Madde E, plus the
    LTF defensive-close flags added in Madde F)."""
    # "isolated" = per-position margin silo; "cross" = shared account margin
    # pool (all positions share equity, better for running max_concurrent
    # slots concurrently without sCode 51008 blocking new entries).
    margin_mode: Literal["isolated", "cross"] = "isolated"
    partial_tp_enabled: bool = False
    partial_tp_ratio: float = 0.5
    partial_tp_rr: float = 1.5
    move_sl_to_be_after_tp1: bool = True
    trail_after_partial: bool = False
    # Madde F — LTF reversal defensive close (wired in Commit 6).
    ltf_reversal_close_enabled: bool = False
    ltf_reversal_min_confluence: int = 3
    ltf_reversal_min_bars_in_position: int = 2
    ltf_reversal_signal_max_age: int = 3


class DerivativesConfig(BaseModel):
    """Phase 1.5 — derivatives data layer configuration.

    Defaults are conservative / off so the bot works without a COINALYZE_API_KEY
    or a live Binance connection. Setting `enabled: true` (done in
    `config/default.yaml` for Phase 1.5) starts the Binance liquidation WS
    and the Coinalyze poll loop at startup.
    """
    enabled: bool = False
    liquidation_buffer_size: int = 5000
    liquidation_lookback_1h_ms: int = 60 * 60 * 1000
    liquidation_lookback_4h_ms: int = 4 * 60 * 60 * 1000
    liquidation_lookback_24h_ms: int = 24 * 60 * 60 * 1000
    # Coinalyze REST (Madde 2) — off by default; enabled by setting
    # COINALYZE_API_KEY and `derivatives.enabled: true`.
    coinalyze_refresh_interval_s: int = 60
    coinalyze_timeout_s: float = 10.0
    coinalyze_max_retries: int = 3
    # Liquidity heatmap (Madde 4).
    heatmap_enabled: bool = True
    heatmap_bucket_pct: float = 0.002
    heatmap_historical_lookback_ms: int = 48 * 60 * 60 * 1000
    heatmap_max_clusters_each_side: int = 10
    leverage_buckets: list[tuple[int, float]] = Field(
        default_factory=lambda: [(10, 0.30), (25, 0.35), (50, 0.20), (100, 0.15)]
    )
    # Regime classifier (Madde 5). Base thresholds default to BTC;
    # per-symbol overrides (ETH lighter OI, SOL even lighter) merge on top.
    regime_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "funding_crowded_z": 2.0,
            "ls_crowded_z": 2.0,
            "oi_surge_pct": 8.0,
            "oi_crash_pct": -10.0,
            "capitulation_liq_notional": 10_000_000.0,
            "stale_snapshot_s": 180.0,
        }
    )
    regime_per_symbol_overrides: dict[str, dict[str, float]] = Field(
        default_factory=dict
    )
    # Entry signal integration (Madde 6).
    confluence_slot_enabled: bool = True        # reserved; factors always
                                                # score, this flag is a switch
                                                # future tooling can read.
    crowded_skip_enabled: bool = True
    crowded_skip_z_threshold: float = 3.0


class ReentryConfig(BaseModel):
    """Per-side reentry gate (Madde C).

    When the same (symbol, side) just closed, block a second entry until:
      * at least `min_bars_after_close` bars have elapsed on the entry TF,
      * AND price has moved ≥ `min_atr_move * ATR` from the prior exit,
      * AND the new plan's confluence passes the WIN/LOSS quality gate.

    Goals: kill "bot revenge-trades right after TP" noise, avoid placing a
    weaker setup than the one that just won, and force a *better or equal*
    setup after a loss (not strictly better — equal is fine since the
    setup that lost might still be valid on a retest).
    """
    min_bars_after_close: int = 3
    min_atr_move: float = 0.5
    require_higher_confluence_after_win: bool = True
    require_higher_or_equal_confluence_after_loss: bool = True


_SESSION_MAP = {
    "asian": Session.ASIAN,
    "london": Session.LONDON,
    "new_york": Session.NEW_YORK,
    "ny": Session.NEW_YORK,
    "off": Session.OFF,
}


class BotConfig(BaseModel):
    """Top-level config mirroring config/default.yaml structure."""
    model_config = ConfigDict(extra="ignore")   # tolerate `rl:` etc.

    bot: RuntimeConfig
    trading: TradingConfig
    circuit_breakers: CircuitBreakerSection = Field(default_factory=CircuitBreakerSection)
    analysis: AnalysisConfig
    okx: OKXConfigBlock
    journal: JournalConfig = Field(default_factory=JournalConfig)
    reentry: ReentryConfig = Field(default_factory=ReentryConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    derivatives: DerivativesConfig = Field(default_factory=DerivativesConfig)

    @field_validator("okx")
    @classmethod
    def _okx_non_empty(cls, v: OKXConfigBlock) -> OKXConfigBlock:
        if not v.api_key or not v.api_secret or not v.passphrase:
            raise ValueError("okx.api_key / api_secret / passphrase must be set "
                             "(populate .env or set env vars before load_config)")
        return v

    # ── Derived views ───────────────────────────────────────────────────────

    def risk_pct_fraction(self) -> float:
        """risk_per_trade_pct is stored as a percent; rr_system wants a fraction."""
        return self.trading.risk_per_trade_pct / 100.0

    def allowed_sessions(self) -> list[Session]:
        """Translate ['london', 'new_york'] → [Session.LONDON, Session.NEW_YORK]."""
        out: list[Session] = []
        for raw in self.analysis.session_filter:
            mapped = _SESSION_MAP.get(raw.strip().lower())
            if mapped is not None and mapped not in out:
                out.append(mapped)
        return out

    def breakers(self) -> CircuitBreakerConfig:
        return self.circuit_breakers.to_dataclass(
            max_concurrent=self.trading.max_concurrent_positions,
            max_leverage=self.trading.max_leverage,
            min_rr_ratio=self.trading.min_rr_ratio,
        )

    def primary_symbol(self) -> str:
        """First symbol — used by legacy call sites that still assume single-symbol."""
        return self.trading.symbols[0]

    def to_okx_credentials(self) -> OKXCredentials:
        return self.okx.to_credentials()


# ── Loader ──────────────────────────────────────────────────────────────────


def load_config(path: str | Path, *, env_path: Optional[str | Path] = None) -> BotConfig:
    """Read YAML at `path`, merge OKX secrets from environment, validate.

    Env vars used (loaded via python-dotenv first if `env_path` exists):
      - OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE (required)
      - OKX_DEMO_FLAG                                (optional, defaults to "1")
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
    if Path(env_path).exists():
        load_dotenv(env_path)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    okx_section = dict(raw.get("okx") or {})
    okx_section.setdefault("api_key", os.environ.get("OKX_API_KEY", ""))
    okx_section.setdefault("api_secret", os.environ.get("OKX_API_SECRET", ""))
    okx_section.setdefault("passphrase", os.environ.get("OKX_PASSPHRASE", ""))
    okx_section["demo_flag"] = os.environ.get(
        "OKX_DEMO_FLAG", okx_section.get("demo_flag", "1"))
    raw["okx"] = okx_section

    return BotConfig(**raw)
