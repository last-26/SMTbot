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
from datetime import datetime, timezone
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
    # Operator-side per-symbol leverage caps — applied on top of OKX's
    # instrument-level cap (fetched at startup). The effective ceiling is
    # min(trading.max_leverage, okx_instrument_cap, symbol_leverage_caps[sym]).
    # Useful when e.g. ETH's OKX cap is 100x but demo wicks make anything
    # above 30x unsafe. Unlisted symbols fall back to the global max.
    symbol_leverage_caps: dict[str, int] = Field(default_factory=dict)
    # Phase 6.9 B3 — per-symbol swing_lookback override for SL sourcing.
    # DOGE/XRP had 18 no_sl_source rejects across 35 trades because 3m
    # swing_lookback=20 doesn't reach far enough on thin-book pairs. Widen
    # to 30 for those two; BTC/ETH/SOL keep the analysis.swing_lookback.
    # Unlisted symbols fall back to analysis.swing_lookback.
    swing_lookback_per_symbol: dict[str, int] = Field(default_factory=dict)
    # Round-trip taker reserve added to SL % when sizing notional, so a
    # stop-out stays inside the USDT risk budget AFTER paying entry + exit
    # taker fees. 0 = off (price-only sizing, back-compat for tests); runtime
    # YAML sets ~0.001 (≈ 2× OKX demo taker 0.05%). TP price is unchanged —
    # fee compensation comes from size, not from widening TP.
    fee_reserve_pct: float = 0.0
    symbol_settle_seconds: float = 4.0            # wait after set_symbol (Madde A/B)
    tf_settle_seconds: float = 2.5                # wait after set_timeframe (Madde B)
    pine_settle_max_wait_s: float = 6.0           # freshness-poll timeout (Madde B)
    pine_settle_poll_interval_s: float = 0.3
    # Extra grace window AFTER the freshness poll observes last_bar flip,
    # before the table is read. The poll only watches the SMT Signals table;
    # the Oscillator table can lag a beat (especially on 1m where last_bar
    # flips every wall-clock minute regardless of full re-render). Sleeping
    # this short post-grace lets the rest of the tables catch up.
    pine_post_settle_grace_s: float = 0.0

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
    # Phase 6.9 B4 — per-symbol session filter override. 2026-04-17/18 data:
    # NY + ASIAN sessions went 0/6 post-cutoff, LONDON went 3/5 (60%). Let
    # thinner-book pairs (SOL/DOGE/XRP) opt into london-only while BTC/ETH
    # keep the global list. Missing key → global session_filter applied.
    session_filter_per_symbol: dict[str, list[str]] = Field(default_factory=dict)
    htf_sr_ceiling_enabled: bool = True      # Madde D
    htf_sr_buffer_atr: float = 0.2
    # Phase 6.9 B2 — per-symbol HTF S/R buffer. SOL 0/3 post-cutoff losses
    # cluster at the HTF TP ceiling; the global 0.2 × ATR padding clips
    # SOL's wide-ATR TP too aggressively. Lower means the ceiling kicks in
    # closer to the HTF zone, freeing more R. Unlisted → htf_sr_buffer_atr.
    htf_sr_buffer_atr_per_symbol: dict[str, float] = Field(default_factory=dict)
    # Fee-aware min TP distance — a TP closer than this fraction of entry
    # price cannot survive the partial-TP 3-fill lifecycle net of taker fees
    # + slippage. Default 0 = off for test back-compat; runtime YAML sets it.
    min_tp_distance_pct: float = 0.0
    # Min SL distance floor — a stop closer than this fraction of entry price
    # sits inside normal bid/ask + demo wick noise. Tight SL = high leverage
    # = large notional = fee drag. Default 0 = off (back-compat); runtime
    # YAML sets ~0.003 so stops sit at least ~3× spread + typical wick width.
    min_sl_distance_pct: float = 0.0
    # Confluence weight overrides. Empty dict = DEFAULT_WEIGHTS from
    # src/analysis/multi_timeframe.py. Only the keys you specify override;
    # the rest stay at their defaults (shallow merge). Unknown keys trigger
    # a warning at load time — typo guard, not a hard fail.
    confluence_weights: dict[str, float] = Field(default_factory=dict)
    # Phase 6.9 A1 — magnitude floor for money_flow_alignment. MFI bias tags
    # (BULLISH / BEARISH) near zero are noise; require |rsi_mfi| >= this.
    min_rsi_mfi_magnitude: float = 2.0
    # Phase 6.9 A2 — max distance (× ATR) to nearest Pine liquidity pool for
    # liquidity_pool_target to fire. 3×ATR ≈ reachable within a few 3m bars.
    liquidity_pool_max_atr_dist: float = 3.0
    # Phase 6.9 A4 — VWAP hard veto. When true, reject entries where price
    # sits on the wrong side of every available session VWAP (1m/3m/15m) for
    # the proposed direction. Opt-in; default off to avoid changing behaviour
    # for back-compat. Enable after A+B validation for liquidity-driven runs.
    vwap_hard_veto_enabled: bool = False

    @field_validator("confluence_weights")
    @classmethod
    def _warn_unknown_weight_keys(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            return v
        from src.analysis.multi_timeframe import DEFAULT_WEIGHTS
        unknown = [k for k in v.keys() if k not in DEFAULT_WEIGHTS]
        for k in unknown:
            warnings.warn(
                f"unknown confluence_weights key '{k}' — will be ignored "
                f"(known keys: {sorted(DEFAULT_WEIGHTS.keys())})",
                UserWarning,
                stacklevel=2,
            )
        return v


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
    # Buffer past entry used when moving SL to breakeven after TP1 fills.
    # `be_price = entry ± entry × sl_be_offset_pct` (sign follows direction),
    # so a touch-back to "near entry" closes at a true net-zero after the
    # remaining exit taker fee + slippage. 0 = off (legacy exact-entry behavior).
    # Runtime YAML sets ~0.001 (matches one round-trip taker on the remainder).
    sl_be_offset_pct: float = 0.0
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


class EconomicCalendarConfig(BaseModel):
    """Macro event blackout — skip new entries around scheduled HIGH-impact
    USD releases (CPI, FOMC, NFP, PCE, FED minutes). Two-provider union with
    failure isolation. Default OFF so existing setups don't break.

    `finnhub_api_key` is populated from `FINNHUB_API_KEY` env var by
    `load_config`; setting it directly in YAML is allowed for tests.
    """
    enabled: bool = False
    finnhub_api_key: str = ""
    finnhub_enabled: bool = True
    faireconomy_enabled: bool = True
    blackout_minutes_before: int = 30
    blackout_minutes_after: int = 15
    impact_filter: list[str] = Field(default_factory=lambda: ["High"])
    currencies: list[str] = Field(default_factory=lambda: ["USD"])
    refresh_interval_s: int = 21600     # 6h — events are scheduled
    lookahead_days: int = 7
    finnhub_timeout_s: float = 10.0
    finnhub_max_retries: int = 3
    faireconomy_timeout_s: float = 10.0
    faireconomy_max_retries: int = 3


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


class RLConfig(BaseModel):
    """Phase 7 RL section — currently only the dirty-data cutoff is wired.
    Unknown keys tolerated so Phase 7 can add `hyperparameters`, `walk_forward`,
    etc. without a back-compat YAML shim."""
    model_config = ConfigDict(extra="ignore")
    clean_since: Optional[str] = None


class BotConfig(BaseModel):
    """Top-level config mirroring config/default.yaml structure."""
    model_config = ConfigDict(extra="ignore")

    bot: RuntimeConfig
    trading: TradingConfig
    circuit_breakers: CircuitBreakerSection = Field(default_factory=CircuitBreakerSection)
    analysis: AnalysisConfig
    okx: OKXConfigBlock
    journal: JournalConfig = Field(default_factory=JournalConfig)
    reentry: ReentryConfig = Field(default_factory=ReentryConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    derivatives: DerivativesConfig = Field(default_factory=DerivativesConfig)
    economic_calendar: EconomicCalendarConfig = Field(
        default_factory=EconomicCalendarConfig)
    rl: RLConfig = Field(default_factory=RLConfig)

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
        return self._translate_sessions(self.analysis.session_filter)

    def allowed_sessions_for(self, symbol: str) -> list[Session]:
        """Per-symbol session filter (Phase 6.9 B4).

        Falls back to the global `allowed_sessions()` when `symbol` has no
        override. Overrides are total — they replace, not merge. A symbol
        opting into `[london]` does NOT inherit `new_york` from the global.
        """
        override = self.analysis.session_filter_per_symbol.get(symbol)
        if override is None:
            return self.allowed_sessions()
        return self._translate_sessions(override)

    def swing_lookback_for(self, symbol: str) -> int:
        """Per-symbol swing_lookback override (Phase 6.9 B3), else global."""
        return self.trading.swing_lookback_per_symbol.get(
            symbol, self.analysis.swing_lookback,
        )

    def htf_sr_buffer_atr_for(self, symbol: str) -> float:
        """Per-symbol HTF S/R buffer override (Phase 6.9 B2), else global."""
        return self.analysis.htf_sr_buffer_atr_per_symbol.get(
            symbol, self.analysis.htf_sr_buffer_atr,
        )

    def rl_clean_since(self) -> Optional[datetime]:
        """Parse `rl.clean_since` (ISO-8601 string or null) as a UTC datetime.
        Returned value is passed to `replay_for_risk_manager` and reporter so
        pre-cutoff trades don't poison peak/DD math or win-rate stats."""
        raw = self.rl.clean_since
        if not raw:
            return None
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @staticmethod
    def _translate_sessions(raw_list: list[str]) -> list[Session]:
        out: list[Session] = []
        for raw in raw_list:
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

    # Macro event blackout — pull FINNHUB_API_KEY from env into the section
    # so the YAML never has to carry the secret. YAML can still pre-set it
    # for tests; env wins (matches OKX behavior above).
    cal_section = dict(raw.get("economic_calendar") or {})
    env_finnhub = os.environ.get("FINNHUB_API_KEY", "")
    if env_finnhub:
        cal_section["finnhub_api_key"] = env_finnhub
    raw["economic_calendar"] = cal_section

    return BotConfig(**raw)
