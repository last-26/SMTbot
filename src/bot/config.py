"""Typed bot configuration loaded from YAML + .env.

YAML layout is kept 1:1 with `config/default.yaml` — top-level keys
`bot`, `trading`, `circuit_breakers`, `analysis`, `bybit`, `journal` map
directly to Pydantic sections below. Secrets (`BYBIT_API_KEY/SECRET`)
are read from `.env`/environment and merged into the `bybit` section
before validation, so the YAML can live in git without leaking credentials.

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
from src.execution.bybit_client import BybitCredentials
from src.strategy.risk_manager import CircuitBreakerConfig


class RuntimeConfig(BaseModel):
    """Outer bot loop settings (the YAML `bot:` block)."""
    mode: Literal["demo", "live", "dry_run"] = "demo"
    poll_interval_seconds: float = 5.0
    timezone: str = "UTC"
    starting_balance: float = 10_000.0
    # 2026-04-28 — startup orphan-position reconciliation. SOL incident on
    # this date: a pending limit filled DURING a bot restart; the new
    # session's `_cancel_orphan_pending_limits` sweep saw status=Filled
    # (no longer in `list_open_orders`) and the journal had no row for the
    # filled position, so 13 SOL contracts ran unmanaged. With this flag
    # on, `_reconcile_orphans` actively closes the gap: synthetic-inserts
    # rows for live-without-DB positions, grows DB num_contracts when
    # live size exceeds journal, and stamps `artifact_reason` so Pass 3
    # GBT can drop them. Off = legacy log-only behaviour (operator must
    # manually patch the DB on mismatch, like the 2026-04-28 SOL fix).
    startup_orphan_reconcile_enabled: bool = True


class TradingConfig(BaseModel):
    symbol: Optional[str] = None                  # deprecated single-symbol form
    symbols: list[str] = Field(default_factory=list)
    entry_timeframe: str                          # e.g. "3m"
    htf_timeframe: str                            # e.g. "15m"
    ltf_timeframe: str = "1m"                     # low-TF reversal (Madde F)
    risk_per_trade_pct: float                     # percent (e.g. 1.0 → 1 %)
    # Operator-set absolute USDT risk per trade. When populated (either via
    # YAML `trading.risk_amount_usdt` or the `RISK_AMOUNT_USDT` env var),
    # bypasses the `balance × risk_per_trade_pct` sizing path and uses this
    # number directly as max_risk. Null/absent = auto mode (see below).
    # Operator-controlled escape hatch: pin $R to a flat number regardless
    # of bankroll movement. Safety rail enforces override ≤ 10% of
    # account_balance inside `calculate_trade_plan`.
    risk_amount_usdt: Optional[float] = None
    # 2026-04-26 — auto-R mode. When `risk_amount_usdt` is null AND this
    # value is > 0, every trade sizes R = realized_wallet_balance × pct
    # at entry time. "realized" = Bybit V5 `totalWalletBalance` (UPL
    # EXCLUDED) so R doesn't inflate during a winning streak or shrink
    # during an open-position drawdown. The fresh probe runs once per
    # cycle in the runner; if the probe fails the runner falls back to
    # the env override (if set) or `current_balance × risk_per_trade_pct`.
    # 0.0 = disabled (use legacy percent mode against `risk_per_trade_pct`).
    auto_risk_pct_of_wallet: float = 0.0
    max_leverage: int
    default_rr_ratio: float
    min_rr_ratio: float
    max_concurrent_positions: int
    contract_size: float = 0.01                   # BTC-USDT-SWAP lot size
    # Operator-side per-symbol leverage caps — applied on top of Bybit's
    # instrument-level cap (fetched at startup). The effective ceiling is
    # min(trading.max_leverage, bybit_instrument_cap, symbol_leverage_caps[sym]).
    # Useful when e.g. ETH's Bybit cap is 100x but demo wicks make anything
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
    # YAML sets ~0.001 (≈ 2× Bybit demo taker 0.055%). TP price is unchanged —
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

    @field_validator("risk_amount_usdt")
    @classmethod
    def _risk_amount_positive(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError(
                "trading.risk_amount_usdt must be > 0 when set "
                "(use null/absent to fall back to auto / percent mode)"
            )
        return v

    @field_validator("auto_risk_pct_of_wallet")
    @classmethod
    def _auto_risk_pct_in_range(cls, v: float) -> float:
        # 0 = disabled. Upper cap mirrors the rr_system safety ceiling
        # (override ≤ 10% of balance) so a misconfigured 0.5 can't
        # silently size a half-bankroll position.
        if not (0.0 <= v <= 0.1):
            raise ValueError(
                f"trading.auto_risk_pct_of_wallet must be in [0.0, 0.1] "
                f"(0 = disabled, 0.02 = 2% of realized wallet); got {v}"
            )
        return v


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
    # Phase 7.A1 — per-symbol SL floor override. Sprint 3 diagnostic: every
    # trade landed at exactly sl_pct=0.500% because the global 0.005 floor
    # was too restrictive for wide-ATR pairs (SOL/DOGE/XRP routinely need
    # more breathing room) and too loose for tight-book BTC. Unlisted
    # symbols fall back to `min_sl_distance_pct`.
    min_sl_distance_pct_per_symbol: dict[str, float] = Field(default_factory=dict)
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
    # Phase 7.A5 — EMA stack momentum veto. When true, reject entries that
    # oppose the local EMA stack regime on the entry TF:
    #   * bull stack (price > EMA21 > EMA55) blocks bearish entries;
    #   * bear stack (price < EMA21 < EMA55) blocks bullish entries.
    # Neutral stacks and insufficient-data bars fail open (no veto).
    ema_veto_enabled: bool = False
    ema_veto_fast_period: int = 21
    ema_veto_slow_period: int = 55
    # Phase 7.A6 — cross-asset BTC/ETH veto. When true, altcoin entries are
    # rejected when BOTH BTC-USDT-SWAP and ETH-USDT-SWAP show opposing EMA
    # stacks (i.e. an altcoin short fighting a clean bull tape on both
    # pillars, or a long fighting a clean bear tape). Fails open when
    # either pillar bias is missing, neutral, or older than
    # `cross_asset_veto_max_age_s`. BTC / ETH cycles always fall through;
    # they set the snapshot but the gate skips them by symbol.
    cross_asset_veto_enabled: bool = False
    cross_asset_veto_max_age_s: float = 300.0
    # Phase 7.D1 — premium/discount zone veto. When true, reject longs that
    # enter above the last N-bar swing midpoint and shorts below it (the
    # "chase the move" pattern sprint 3 flagged). Midpoint = (N-bar high +
    # N-bar low) / 2 on the entry TF. Missing candles / degenerate range
    # fails open. Opt-in; default off for back-compat. `_lookback` controls
    # how many entry-TF bars feed the swing range.
    premium_discount_veto_enabled: bool = False
    premium_discount_lookback: int = 40
    # Phase 7.D1 — displacement candle factor tunables (see DEFAULT_WEIGHTS
    # `displacement_candle`). A directional candle with body ≥ atr_mult × ATR
    # within the last max_bars_ago closed bars scores a confluence point.
    # Defaults match `DEFAULT_DISPLACEMENT_*` constants in multi_timeframe.py.
    displacement_atr_mult: float = 1.5
    displacement_max_bars_ago: int = 5
    # Phase 7.D2 — divergence_signal bar-ago decay bands. Matches Pine
    # `last_wt_div_bars_ago` values (int). Weight is full up to fresh_bars,
    # 0.5× up to decay_bars, 0.25× up to max_bars, then skipped. Defaults
    # (3 / 6 / 9) give divergences ~30 minutes of edge on a 3m TF before
    # the scorer drops them.
    divergence_fresh_bars: int = 3
    divergence_decay_bars: int = 6
    divergence_max_bars: int = 9
    # Phase 7.D3 — ADX trend-regime classifier + conditional scoring.
    # Classifier runs on the entry-TF closed-bar buffer and labels each
    # cycle RANGING / WEAK_TREND / STRONG_TREND / UNKNOWN based on Wilder-
    # smoothed ADX. The conditional scoring flag (opt-in, off by default)
    # scales `htf_trend_alignment` ×1.5 and `recent_sweep` ×0.5 in STRONG_
    # TREND tape, and mirrors the ratio in RANGING tape. WEAK_TREND and
    # UNKNOWN leave weights unchanged.
    trend_regime_conditional_scoring_enabled: bool = False
    adx_period: int = 14
    trend_regime_ranging_threshold: float = 20.0
    trend_regime_strong_threshold: float = 30.0

    # 2026-04-21 — VWAP-band zone anchor (Convention X: absolute position on
    # the [lower_band, upper_band] axis where 0.5 = VWAP). Long entries sit
    # above VWAP on the upper side; short entries sit below VWAP on the
    # lower side. Previously the `_vwap_zone` limit landed at the 0.5σ
    # midpoint (equivalent to long_anchor=0.75 / short_anchor=0.25), chosen
    # arbitrarily. 0.7 / 0.3 pulls the entry closer to VWAP (Fib-lite 0.6
    # retracement from the outer band), catching the pullback before it
    # fully retraces to VWAP. Knob is per-direction so the operator can
    # EQ-hug further (e.g. 0.65 / 0.35) without touching the other leg.
    vwap_zone_long_anchor: float = 0.7
    vwap_zone_short_anchor: float = 0.3

    # 2026-04-26 — VWAP daily-reset blackout. Pine 3m VWAP uses
    # `anchor=timeframe.change("D")` and 1m/15m use default session anchor;
    # all three reset at UTC 00:00. For the first ~10-30 min after reset
    # the ±1σ band is collapsed (stdev≈0) and `vwap_composite_alignment`
    # picks up gibberish (close vs a near-noise VWAP value). Hard veto is
    # off so no false reject, but the soft pillar contributes spurious
    # weight. Time-based blackout rejects ALL new entry attempts AND
    # cancels resting pendings inside [-pre, +post] minutes around UTC
    # midnight. Reject reason: `vwap_reset_blackout` (early-skip; written
    # to journal via `evaluate_pending_invalidation_gates` for pendings,
    # logged-only for cycle entries to mirror the macro_event_blackout
    # pattern). Disable by flipping enabled=False.
    vwap_reset_blackout_enabled: bool = True
    vwap_reset_blackout_window_pre_min: int = 5
    vwap_reset_blackout_window_post_min: int = 15

    @field_validator("vwap_reset_blackout_window_pre_min")
    @classmethod
    def _vwap_blackout_pre_nonneg(cls, v: int) -> int:
        if v < 0 or v > 60:
            raise ValueError(
                f"vwap_reset_blackout_window_pre_min must be in [0, 60] "
                f"(minutes before UTC 00:00); got {v}"
            )
        return v

    @field_validator("vwap_reset_blackout_window_post_min")
    @classmethod
    def _vwap_blackout_post_nonneg(cls, v: int) -> int:
        if v < 0 or v > 60:
            raise ValueError(
                f"vwap_reset_blackout_window_post_min must be in [0, 60] "
                f"(minutes after UTC 00:00); got {v}"
            )
        return v

    @field_validator("vwap_zone_long_anchor")
    @classmethod
    def _long_anchor_on_upper_half(cls, v: float) -> float:
        if not (0.5 <= v <= 1.0):
            raise ValueError(
                f"vwap_zone_long_anchor must be in [0.5, 1.0] "
                f"(0.5=VWAP, 1.0=upper band); got {v}"
            )
        return v

    @field_validator("vwap_zone_short_anchor")
    @classmethod
    def _short_anchor_on_lower_half(cls, v: float) -> float:
        if not (0.0 <= v <= 0.5):
            raise ValueError(
                f"vwap_zone_short_anchor must be in [0.0, 0.5] "
                f"(0.0=lower band, 0.5=VWAP); got {v}"
            )
        return v

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


class BybitConfigBlock(BaseModel):
    base_url: str = "https://api-demo.bybit.com"
    demo: bool = True
    account_type: str = "UNIFIED"
    category: str = "linear"
    api_key: str
    api_secret: str

    def to_credentials(self) -> BybitCredentials:
        return BybitCredentials(
            api_key=self.api_key,
            api_secret=self.api_secret,
            demo=self.demo,
            account_type=self.account_type,
            category=self.category,
        )


class JournalConfig(BaseModel):
    db_path: str = "data/trades.db"
    # Intra-trade snapshot writer — captures live mark/PnL + running MFE/MAE
    # + active SL/TP + lifecycle flags + derivatives/on-chain/oscillator drift
    # every cadence_s seconds for every OPEN position. Reuses cached state;
    # zero extra Bybit / TV calls. RL trajectory analysis only — never feeds
    # entry/exit decisions.
    position_snapshot_enabled: bool = True
    position_snapshot_cadence_s: int = 300

    @field_validator("position_snapshot_cadence_s")
    @classmethod
    def _validate_position_snapshot_cadence(cls, v: int) -> int:
        if not 60 <= v <= 3600:
            raise ValueError(
                "position_snapshot_cadence_s must be in [60, 3600] (seconds)"
            )
        return v


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

    # Phase 7.C4 — zone-based limit entry. When False the runner keeps the
    # legacy market-order path (safer default while the pivot stabilises).
    # When True the planner builds a ZoneSetup from HTF FVG / liq pool /
    # VWAP / sweep sources, rewrites the plan's entry/SL/TP to structural
    # levels, and places a maker-preferred limit order. Pending entries
    # time out after `zone_max_wait_bars` (entry-TF bars) if unfilled.
    zone_entry_enabled: bool = False
    zone_max_wait_bars: int = 7
    zone_buffer_atr: float = 0.25
    zone_sl_buffer_atr: float = 0.5
    zone_default_rr: float = 2.0
    zone_require_setup: bool = False   # True → reject when no zone source

    # 2026-04-19 rebalance — EMA21 pullback entry source (scalp-native).
    # When True the zone builder checks whether price sits within
    # `zone_buffer_atr × ATR` of the fast EMA with an aligned EMA stack.
    ema21_pullback_enabled: bool = True

    # 2026-04-19 rebalance — HTF 15m FVG as an ENTRY source. Off by default:
    # HTF FVG is a slow drift target, poor fit for the 3m scalp TF. Kept
    # available so an operator can opt in for structural runs.
    htf_fvg_entry_enabled: bool = False

    # 2026-04-19 rebalance — near-liq entry gates. The old source placed
    # limit orders AT the top cluster (sweep-reversal thesis). Rewritten so
    # a liq-pool entry only fires when the nearest cluster on the correct
    # side is (a) within `liq_entry_near_max_atr × ATR` of price AND (b)
    # notional ≥ `liq_entry_magnitude_mult × median(side_clusters)`.
    liq_entry_near_max_atr: float = 1.5
    liq_entry_magnitude_mult: float = 2.5

    # 2026-04-19 rebalance — partial-TP ladder. When enabled the zone
    # builder produces a multi-leg TP list from liquidity clusters on the
    # target side; shares renormalise when fewer clusters pass the notional
    # filter (`tp_ladder_min_notional_frac × largest_side`).
    tp_ladder_enabled: bool = True
    tp_ladder_shares: list[float] = Field(default_factory=lambda: [0.40, 0.35, 0.25])
    tp_ladder_min_notional_frac: float = 0.30

    # 2026-04-19 (post-pivot diagnostic) — hard 1:N RR cap on the final TP.
    # Operator log on 2026-04-19 showed 5 zone_limit_placed orders all sized
    # off heatmap clusters that landed 8-12R away (e.g. BTC sl=$300 → tp=$3600,
    # 12:1) despite the symbol_decision log claiming RR=4.5. Root cause:
    # `apply_zone_to_plan` overrode `plan.tp_price` with `zone.tp_primary` =
    # nearest heatmap cluster, with no RR bound. When > 0, the final primary
    # TP is forced to ``entry ± target_rr_ratio × sl_distance`` and every
    # ladder rung is clamped to the same boundary. 0 = off (legacy heatmap
    # behavior, kept for back-compat).
    target_rr_ratio: float = 0.0

    # Dynamic TP revision. Off by default. When True, the runner periodically
    # recomputes a fresh target TP from current state (`target_rr_ratio` × the
    # SL distance fixed at fill, applied to the live entry-fill price) and
    # revises the runner OCO (cancel + place) when the new target differs by
    # at least `tp_revise_min_delta_atr × ATR` and at least
    # `tp_revise_cooldown_s` seconds have elapsed since the last revision.
    # `tp_min_rr_floor` prevents revising into a sub-floor RR if the live
    # mark drifts past the entry. 0 = off (don't revise).
    tp_dynamic_enabled: bool = False
    # Default dropped 1.5 → 1.0 on 2026-04-21 (eve) alongside hard RR cap
    # 3.0 → 2.0. Under 1:2, a 1.5R floor would bind on almost every revise.
    tp_min_rr_floor: float = 1.0
    tp_revise_min_delta_atr: float = 0.5
    tp_revise_cooldown_s: float = 30.0

    # 2026-04-20 — MFE-triggered SL lock (Option A). When MFE (maximum favorable
    # excursion, measured in R multiples of plan_sl_distance) crosses
    # `sl_lock_mfe_r`, cancel + re-place the runner OCO with a new SL at
    # ``entry + sign × sl_lock_at_r × plan_sl_distance``. At 0.0 the new SL
    # sits at entry (± fee buffer via sl_be_offset_pct), turning the trade
    # risk-free; at >0 it locks in that fraction of R as guaranteed profit.
    # One-shot: once applied, the `sl_lock_applied` flag blocks further locks
    # on the same position (subsequent tightening would need a proper trail,
    # see Phase 12 Option B). `plan_sl_price <= 0` (post-BE rehydrate) skips.
    sl_lock_enabled: bool = False
    # Default dropped 2.0 → 1.3 on 2026-04-21 (eve) alongside RR cap
    # 3.0 → 2.0. Old 2R threshold coincided with new 2R TP (lock would
    # never fire before TP). 1.3R ≈ 65% of 2R TP.
    sl_lock_mfe_r: float = 1.3
    sl_lock_at_r: float = 0.0

    # Position-attached TP/SL trigger-price source. "mark" = index-weighted
    # price across the major real-market venues (cross-exchange VWAP).
    # "last" = last trade on the Bybit book (default in Bybit V5).
    # Mark is strongly preferred on demo: demo-only wicks have no
    # counterpart on the index and so can't fire mark-based triggers,
    # preventing stop-hunt artefacts from poisoning the RL dataset. Kept
    # configurable so a live deploy can pick "last" if it wants
    # book-native triggering.
    algo_trigger_px_type: str = "mark"

    # 2026-04-20 — resting TP limit alongside OCO. Trigger-market TP (OCO
    # tpOrdPx=-1) fires a market order when mark crosses the trigger; on a
    # fast wick-reversal the market order slips badly (or the mark-smoothed
    # trigger never fires at all). A reduce-only post-only limit at the TP
    # price, resting in the book from entry onward, fills as a maker the
    # instant bid/ask touches TP — capturing wicks that the OCO trigger
    # misses. The OCO stays in place as the SL leg + a market-TP fallback;
    # whichever exits first wins (reduce-only prevents double-close). The
    # bot tracks the limit order id via `_Tracked.tp_limit_order_id` and
    # cancels it on close / cancel-replaces it alongside the OCO on
    # dynamic-TP revise. MFE-lock leaves the TP limit alone (TP unchanged).
    tp_resting_limit_enabled: bool = True

    # Katman 2 — post-close demo-wick artefact cross-check. When True, on
    # every trade close we fetch the concurrent 1m candle from Binance
    # USD-M futures and stamp `demo_artifact=True` when entry or exit
    # sits outside the real-market [low, high] band. Non-destructive: the
    # trade still persists, but downstream reporting/RL can filter.
    # `artefact_check_tolerance_pct` widens the band to tolerate routine
    # exchange-vs-Binance microstructure skew without flagging it as artefact.
    artefact_check_enabled: bool = True
    artefact_check_timeout_s: float = 5.0
    artefact_check_tolerance_pct: float = 0.0005   # 5 bps


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


class OnChainConfig(BaseModel):
    """Arkham on-chain integration (2026-04-21, trial 30-day key).

    All flags default OFF. `enabled=false` (master switch) keeps the
    entire pipeline idle: no HTTP requests, no WebSocket connection,
    no modifier effects, MarketState.on_chain stays None. Rolling back
    after the trial window = set `enabled: false` and restart. No code
    removal required; schema columns stay in place for the historical
    `on_chain_context` JSON on pre-rollback trades.

    Sub-features (layered on top of master):
      * daily_bias_enabled         — Phase C: ±delta confluence modifier.
      * stablecoin_pulse_enabled   — Phase E: below_confluence penalty.
      * whale_blackout_enabled     — 2026-04-22: HARD GATE REMOVED. Now
        purely controls whether the WS listener runs (streams whale
        transfers into the `whale_transfers` journal + `whale_blackout_active`
        snapshot bool). No runtime entry / pending invalidation effect.
        Legacy name preserved to avoid YAML migration; rename tracked as
        Pass 2 cleanup.
      * flow_alignment_enabled     — 2026-04-22: soft directional signal.
        Combines stablecoin pulse + BTC/ETH + Coinbase/Binance/Bybit
        netflow into a score [-1, +1]; misaligned direction pays additive
        penalty on min_confluence. Per-entity inputs added late-gece as
        journal-only promotion. Default-weighted (no tuning in Pass 1).
        Tuned in Pass 2 once Arkham coverage is uniform across dataset.
      * per_symbol_cex_flow_enabled — 2026-04-22 (gece, late): per-symbol
        1h CEX volume penalty. Symbol's own token INTO CEX = bearish /
        OUT = bullish; misaligned direction pays additive threshold bump.
        Default-weighted; tuned in Pass 2.
    Phase B (journal enrichment) is always-on when `enabled=true` — no
    separate flag, the `on_chain_context` JSON column just stays NULL
    whenever the snapshot is unavailable.
    """

    enabled: bool = False

    # Phase C — daily macro bias (see src/analysis/multi_timeframe.py).
    # Default delta bumped 0.10 → 0.15 on 2026-04-21 (eve) after observing
    # every bias-opposed setup still cleared the threshold. GBT-tunable.
    daily_bias_enabled: bool = False
    daily_bias_modifier_delta: float = 0.15
    daily_bias_stablecoin_threshold_usd: float = 50_000_000.0
    daily_bias_btc_netflow_threshold_usd: float = 50_000_000.0
    # 2026-04-23 — "daily" is a misnomer now. The bundle (bias + BTC/ETH
    # 24h netflow + per-entity Coinbase/Binance/Bybit 24h netflow) used to
    # refresh once per UTC day, which froze DB rows for hours once the
    # underlying Arkham queries were rewritten from daily buckets to rolling
    # 24h histogram windows. Refresh now on a monotonic cadence so new
    # `on_chain_snapshots` rows actually replace stale values.
    #   5 min chosen from live probe: Arkham indexer repopulates the
    #   active-hour bucket every 60-120s; 5 min is safely above that,
    #   catches intraday inflection without polling identical buckets.
    #   12 histogram calls per cycle × 12 cycles/h = 144 calls/h; label-
    #   free endpoints, so label budget (10k/mo, currently 558) untouched.
    daily_snapshot_refresh_s: int = 300

    # Phase E — stablecoin pulse cross-asset penalty.
    # Default penalty bumped 0.5 → 0.75 on 2026-04-21 (eve), paired with the
    # daily_bias_modifier_delta bump. Effective threshold 3.0 → 3.75 on misaligned.
    stablecoin_pulse_enabled: bool = False
    stablecoin_pulse_refresh_s: int = 3600
    stablecoin_pulse_threshold_usd: float = 50_000_000.0
    stablecoin_pulse_penalty: float = 0.75

    # Phase D — Arkham whale-transfer pipeline.
    # 2026-04-22 (gece): RUNTIME HARD GATE REMOVED. This flag now only
    # controls whether the WS listener (`ArkhamWebSocketListener`) runs —
    # whale events still stream into the `whale_transfers` journal table
    # and flip the informational `whale_blackout_active` bool on every
    # `on_chain_snapshots` row. Entry pipeline no longer rejects on whale
    # events; pending limits are no longer invalidated. The flag name is
    # preserved to avoid YAML migration.
    # Arkham's WS filter requires `usdGte >= 10_000_000` per the API docs;
    # the validator enforces this. Default 150M (bumped 100M → 150M on
    # 2026-04-22) — at $100M the trial label-lookup quota was burning
    # ~17k/month from incidental whale events.
    whale_blackout_enabled: bool = False
    whale_threshold_usd: float = 150_000_000.0
    # State-activity window. Within this window after a whale event the
    # `WhaleBlackoutState.is_active()` returns True → snapshot logs
    # `whale_blackout_active=true`. NOT a runtime gate (removed 2026-04-22).
    whale_blackout_duration_s: int = 600  # 10 minutes
    # Token slug filter on the WS stream (Arkham coingecko-style ids).
    # Restricts incoming whale events to the tokens we actually trade
    # (5 perps + 2 stablecoins). Without this, every $100M+ CEX↔CEX
    # transfer in any token reaches us — XRP, ADA, MATIC, LINK, etc.
    # consume label lookups (each transfer = from + to entity labels)
    # without affecting any of our blackout decisions, since
    # `affected_symbols_for` returns empty tuple for unmapped tokens.
    # Bumped 2026-04-22 with the threshold change above.
    whale_tokens: list[str] = Field(default_factory=lambda: [
        # XRP intentionally omitted: Arkham doesn't index XRPL per-token
        # data (probed 2026-04-25, all XRP slug variants rejected). Adding
        # it would just burn label-lookup quota on events that
        # `affected_symbols_for` would silently drop.
        "bitcoin", "ethereum", "solana", "dogecoin",
        "tether", "usd-coin",
    ])

    # Phase F2 (2026-04-21 post-integration) — Arkham altcoin index
    # modifier. Index is a scalar 0-100 from `/marketdata/altcoin_index`
    # (low = altcoins underperforming BTC, high = altcoins outperforming).
    # Applies only to altcoin symbols (not BTC / ETH). Misaligned direction
    # (long alt in BTC-dominance season, or short alt in altseason) takes
    # a penalty bump on the effective `min_confluence_score`.
    altcoin_index_enabled: bool = False
    altcoin_index_bearish_threshold: int = 25
    altcoin_index_bullish_threshold: int = 75
    altcoin_index_modifier_delta: float = 0.5
    # Refresh cadence (seconds) — index is macro-scale, hourly is plenty.
    altcoin_index_refresh_s: int = 3600

    # 2026-04-22 (gece) — flow_alignment soft directional signal.
    # Combines six Arkham inputs into a [-1, +1] directional score:
    #   stablecoin pulse (0.25)  — hourly, natural sign (IN=bullish)
    #   BTC netflow      (0.25)  — daily, inverted (OUT=bullish)
    #   ETH netflow      (0.15)  — daily, inverted
    #   Coinbase netflow (0.15)  — daily, inverted ("Coinbase premium" pattern)
    #   Binance netflow  (0.10)  — daily, inverted
    #   Bybit netflow    (0.10)  — daily, inverted
    # Per-entity inputs (Coinbase/Binance/Bybit) added 2026-04-22 (gece,
    # late) when their journal-only columns were promoted to runtime ahead
    # of the Pass 1 clean restart. Misaligned direction (long on bearish
    # score OR short on bullish score) pays additive penalty on
    # `min_confluence_score`, scaled linearly by `|score|`. Default-
    # weighted for Pass 1 (no tuning on current 42 trades where Arkham
    # coverage is inconsistent). Pass 2 tunes penalty + noise_floor +
    # individual weights against uniform-coverage dataset.
    flow_alignment_enabled: bool = True
    flow_alignment_penalty: float = 0.25
    # Signals below this USD magnitude treated as noise → contribute 0
    # (not +1 / -1) to the score, regardless of sign. Prevents random
    # sub-$1M flow ticks from dragging the signal around.
    flow_alignment_noise_floor_usd: float = 1_000_000.0

    # 2026-04-22 (gece, late) — per-symbol 1h CEX volume penalty.
    # Unlike stablecoins, token flowing INTO exchange is BEARISH for that
    # symbol (selling setup), OUT is BULLISH (cold/DEX accumulation).
    # Misaligned direction on the traded symbol pays additive threshold
    # bump. Source: Arkham `/token/volume/{id}?granularity=1h`
    # most-recent bucket's `inUSD - outUSD` (positive = net inflow).
    # Promoted to runtime alongside per-entity netflow expansion.
    per_symbol_cex_flow_enabled: bool = True
    per_symbol_cex_flow_penalty: float = 0.25
    # Higher floor than flow_alignment: token volume on a 1h bucket is
    # noisier than macro stablecoin pulse ($1M moves are routine).
    per_symbol_cex_flow_noise_floor_usd: float = 5_000_000.0

    # 2026-04-22 — per-entity (Coinbase + Binance + Bybit) 24h netflow.
    # Refreshes on the same UTC-day cadence as `daily_macro_bias`.
    # Probe (2026-04-22) confirmed: 3 credits / call, 0 label lookups,
    # all three slugs valid. Journal-only signal, no runtime effect.
    entity_netflow_enabled: bool = True

    # 2026-04-22 — per-symbol CEX hourly volume via `/token/volume/{id}`.
    # Probe (2026-04-22) confirmed: granularity=1h works, 5m/15m/30m
    # return 500. Refresh cadence intentionally hourly (matches data
    # granularity). 5 symbols * 1 call/h * 3 credits = 360/day = ~11k/mo
    # credits, 0 label lookups. Journal-only signal.
    token_volume_enabled: bool = True
    token_volume_refresh_s: int = 3600

    # Safety / budget controls.
    # Auto-disable the master when the reported label-usage fraction
    # crosses this percent. Prevents the trial key from being exhausted
    # and the paid plan from over-spending on bot telemetry.
    api_usage_auto_disable_pct: float = 95.0
    api_client_timeout_s: float = 10.0
    # Snapshots older than this are considered stale by downstream
    # gates (daily_bias modifier falls through to 1.0, penalty to 0.0).
    snapshot_staleness_threshold_s: int = 7200

    @field_validator("daily_bias_modifier_delta")
    @classmethod
    def _daily_bias_delta_sane(cls, v: float) -> float:
        if not (0.0 <= v <= 0.5):
            raise ValueError(
                f"on_chain.daily_bias_modifier_delta must be in [0.0, 0.5] "
                f"(delta >0.5 would swap long/short rather than nudge); got {v}"
            )
        return v

    @field_validator("daily_bias_stablecoin_threshold_usd",
                     "daily_bias_btc_netflow_threshold_usd",
                     "stablecoin_pulse_threshold_usd",
                     "stablecoin_pulse_penalty",
                     "flow_alignment_penalty",
                     "flow_alignment_noise_floor_usd",
                     "per_symbol_cex_flow_penalty",
                     "per_symbol_cex_flow_noise_floor_usd")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(
                f"on_chain thresholds / penalties must be ≥ 0; got {v}"
            )
        return v

    @field_validator("whale_threshold_usd")
    @classmethod
    def _whale_threshold_meets_ws_minimum(cls, v: float) -> float:
        # Arkham WS filter requires `usdGte >= 10M` per API documentation.
        if v < 10_000_000.0:
            raise ValueError(
                f"on_chain.whale_threshold_usd must be ≥ 10_000_000 "
                f"(Arkham WS `usdGte` minimum); got {v}"
            )
        return v

    @field_validator("whale_blackout_duration_s", "stablecoin_pulse_refresh_s",
                     "snapshot_staleness_threshold_s",
                     "altcoin_index_refresh_s",
                     "token_volume_refresh_s")
    @classmethod
    def _positive_duration(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                f"on_chain duration fields must be > 0 seconds; got {v}"
            )
        return v

    @field_validator("altcoin_index_bearish_threshold",
                     "altcoin_index_bullish_threshold")
    @classmethod
    def _altcoin_thresholds_in_range(cls, v: int) -> int:
        if not (0 <= v <= 100):
            raise ValueError(
                f"on_chain.altcoin_index_* thresholds must be in [0, 100]; "
                f"got {v}"
            )
        return v

    @model_validator(mode="after")
    def _altcoin_thresholds_ordered(self) -> "OnChainConfig":
        if (self.altcoin_index_bearish_threshold
                >= self.altcoin_index_bullish_threshold):
            raise ValueError(
                f"on_chain.altcoin_index_bearish_threshold "
                f"({self.altcoin_index_bearish_threshold}) must be strictly "
                f"less than altcoin_index_bullish_threshold "
                f"({self.altcoin_index_bullish_threshold}); otherwise the "
                f"neutral band collapses and every value triggers a penalty."
            )
        return self

    @field_validator("api_usage_auto_disable_pct")
    @classmethod
    def _usage_pct_in_range(cls, v: float) -> float:
        if not (0.0 < v <= 100.0):
            raise ValueError(
                f"on_chain.api_usage_auto_disable_pct must be in (0, 100]; got {v}"
            )
        return v


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
    bybit: BybitConfigBlock
    journal: JournalConfig = Field(default_factory=JournalConfig)
    reentry: ReentryConfig = Field(default_factory=ReentryConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    derivatives: DerivativesConfig = Field(default_factory=DerivativesConfig)
    economic_calendar: EconomicCalendarConfig = Field(
        default_factory=EconomicCalendarConfig)
    on_chain: OnChainConfig = Field(default_factory=OnChainConfig)
    rl: RLConfig = Field(default_factory=RLConfig)

    @field_validator("bybit")
    @classmethod
    def _bybit_non_empty(cls, v: BybitConfigBlock) -> BybitConfigBlock:
        if not v.api_key or not v.api_secret:
            raise ValueError("bybit.api_key / api_secret must be set "
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

    def min_sl_distance_pct_for(self, symbol: str) -> float:
        """Per-symbol SL-floor override (Phase 7.A1), else global.

        Wide-ATR pairs (SOL/DOGE/XRP) need larger floors than the tight-book
        BTC/ETH baseline; a uniform floor forced every trade's sl_pct to
        exactly 0.5% regardless of symbol volatility during Sprint 3.
        """
        return self.analysis.min_sl_distance_pct_per_symbol.get(
            symbol, self.analysis.min_sl_distance_pct,
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

    def to_bybit_credentials(self) -> BybitCredentials:
        return self.bybit.to_credentials()


# ── Loader ──────────────────────────────────────────────────────────────────


def load_config(path: str | Path, *, env_path: Optional[str | Path] = None) -> BotConfig:
    """Read YAML at `path`, merge Bybit secrets from environment, validate.

    Env vars used (loaded via python-dotenv first if `env_path` exists):
      - BYBIT_API_KEY, BYBIT_API_SECRET                (required)
      - BYBIT_DEMO                                     (optional, "1"/"0", defaults to "1")
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
    if Path(env_path).exists():
        load_dotenv(env_path)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    bybit_section = dict(raw.get("bybit") or {})
    bybit_section.setdefault("api_key", os.environ.get("BYBIT_API_KEY", ""))
    bybit_section.setdefault("api_secret", os.environ.get("BYBIT_API_SECRET", ""))
    # BYBIT_DEMO is "1"/"0" string in env; convert to bool for the config block.
    env_demo = os.environ.get("BYBIT_DEMO")
    if env_demo is not None:
        bybit_section["demo"] = env_demo.strip() not in ("0", "false", "False", "")
    raw["bybit"] = bybit_section

    # Macro event blackout — pull FINNHUB_API_KEY from env into the section
    # so the YAML never has to carry the secret. YAML can still pre-set it
    # for tests; env wins (matches the Bybit-block behavior above).
    cal_section = dict(raw.get("economic_calendar") or {})
    env_finnhub = os.environ.get("FINNHUB_API_KEY", "")
    if env_finnhub:
        cal_section["finnhub_api_key"] = env_finnhub
    raw["economic_calendar"] = cal_section

    # Operator-set absolute $R override — env wins over YAML so an operator
    # can bump the number between bot restarts without editing config. Empty
    # string / unset → YAML value (or None → legacy percent mode).
    env_risk_amount = os.environ.get("RISK_AMOUNT_USDT", "").strip()
    if env_risk_amount:
        try:
            parsed_risk_amount = float(env_risk_amount)
        except ValueError as exc:
            raise ValueError(
                f"RISK_AMOUNT_USDT={env_risk_amount!r} is not a valid float"
            ) from exc
        trading_section = dict(raw.get("trading") or {})
        trading_section["risk_amount_usdt"] = parsed_risk_amount
        raw["trading"] = trading_section

    # Arkham on-chain — the API key stays in env only. YAML carries every
    # on_chain.* flag/threshold but never the secret; ArkhamClient reads
    # `ARKHAM_API_KEY` directly at construction time (matches Coinalyze's
    # COINALYZE_API_KEY handling). load_config stays out of the secret
    # path so `cfg.on_chain` never carries credentials.

    return BotConfig(**raw)
