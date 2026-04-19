"""Pydantic data models for structured Pine Script outputs and market state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"
    UNDEFINED = "UNDEFINED"


class Session(str, Enum):
    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OFF = "OFF"


class TradeOutcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


# ── Individual structure models ──────────────────────────────────────────────


class MSSEvent(BaseModel):
    """Market Structure Shift event from mss_detector.pine labels."""
    event_type: str = "MSS"  # MSS or BOS
    direction: Direction
    price: float
    bar_index: Optional[int] = None


class FVGZone(BaseModel):
    """Fair Value Gap zone from fvg_mapper.pine boxes/tooltips."""
    direction: Direction
    bottom: float
    top: float
    size_pct: Optional[float] = None
    status: str = "ACTIVE"  # ACTIVE or MITIGATED


class OrderBlock(BaseModel):
    """Order Block zone from order_block.pine boxes/tooltips."""
    direction: Direction
    bottom: float
    top: float
    status: str = "ACTIVE"  # ACTIVE or BROKEN
    tests: int = 0


class LiquidityLevel(BaseModel):
    """Liquidity level (equal highs/lows) from liquidity_sweep.pine."""
    price: float
    touches: int = 2
    side: str = "above"  # "above" or "below" relative to current price


class SweepEvent(BaseModel):
    """Liquidity sweep event from liquidity_sweep.pine labels."""
    direction: Direction
    level: float
    touches: Optional[int] = None
    bar_index: Optional[int] = None


class SessionLevel(BaseModel):
    """Session high/low level from session_levels.pine lines."""
    name: str  # e.g. "Asian High", "PDH", "PWL"
    price: float


# ── Signal Table (master aggregation) ───────────────────────────────────────


class SignalTableData(BaseModel):
    """Parsed data from smt_overlay.pine master table (SMT Signals).

    Primary data source for price action + VMC Cipher A overlay signals.
    Bot reads via: tv stream tables --filter "SMT Signals"
    """
    # Price Action
    trend_htf: Direction = Direction.UNDEFINED
    trend_ltf: Direction = Direction.UNDEFINED
    structure: str = ""  # e.g. "HH_HL_bullish"
    last_mss: Optional[str] = None  # e.g. "BULLISH@69450"
    active_fvg: Optional[str] = None  # e.g. "BULL@68900-69100"
    active_ob: Optional[str] = None  # e.g. "BULL@68500-68700"
    liquidity_above: list[float] = Field(default_factory=list)
    liquidity_below: list[float] = Field(default_factory=list)
    last_sweep: Optional[str] = None  # e.g. "BEAR@70350"

    # Sessions
    session: Session = Session.OFF

    # VMC Cipher A (overlay momentum: EMA ribbon + WaveTrend shape signals)
    vmc_ribbon: str = "BEARISH"  # EMA ribbon bias: BULLISH / BEARISH
    vmc_wt_bias: str = "NEUTRAL"  # WT state: OVERBOUGHT / OVERSOLD / NEUTRAL
    vmc_wt_value: float = 0.0  # WT2 numeric value extracted from vmc_wt_bias
    vmc_wt_cross: str = "\u2014"  # WT cross: UP / DOWN / \u2014
    vmc_last_signal: str = "\u2014"  # YELLOW_X_BUY@price, BLOOD_DIAMOND_SELL@price, etc.
    vmc_rsi_mfi: float = 0.0  # RSI+MFI combo value

    # Summary
    confluence: int = 0  # 0-7 with VMC Cipher A
    atr_14: float = 0.0
    price: float = 0.0

    # Multi-TF session-anchored VWAP (overlay request.security on 1m/3m/15m).
    # 0.0 means missing/unparsed; consumers must guard with > 0.
    vwap_1m: float = 0.0
    vwap_3m: float = 0.0
    vwap_15m: float = 0.0
    # 3m VWAP ±1σ bands (session-anchored). 0.0 when Pine did not emit them
    # (older script) or when session is too young for a valid stdev. The
    # band-based zone logic in setup_planner falls back to ATR buffer in
    # that case.
    vwap_3m_upper: float = 0.0
    vwap_3m_lower: float = 0.0

    last_bar: Optional[int] = None  # bar_index of last Pine update — used
                                    # by the runner's freshness-poll to
                                    # detect when a symbol / timeframe
                                    # switch has settled on the chart.


class OscillatorTableData(BaseModel):
    """Parsed data from smt_oscillator.pine table (SMT Oscillator).

    Secondary data source for momentum analysis, divergences, and trade signals.
    Bot reads via: tv stream tables --filter "SMT Oscillator"
    """
    # WaveTrend
    wt1: float = 0.0
    wt2: float = 0.0
    wt_state: str = "NEUTRAL"  # OVERBOUGHT / OVERSOLD / NEUTRAL
    wt_cross: str = "\u2014"  # UP / DOWN / \u2014
    wt_vwap_fast: float = 0.0  # wt1 - wt2

    # RSI & MFI
    rsi: float = 50.0
    rsi_state: str = "NEUTRAL"  # OVERSOLD / OVERBOUGHT / NEUTRAL
    rsi_mfi: float = 0.0
    rsi_mfi_bias: str = "NEUTRAL"  # BULLISH / BEARISH / NEUTRAL

    # Stochastic RSI
    stoch_k: float = 50.0
    stoch_d: float = 50.0
    stoch_state: str = "K>D (bullish)"

    # Signals & Divergences
    last_signal: str = "\u2014"  # BUY / SELL / GOLD_BUY / BUY_DIV / SELL_DIV
    last_signal_bars_ago: int = 0
    last_wt_div: str = "\u2014"  # BULL_REG / BEAR_REG / BULL_HIDDEN / BEAR_HIDDEN
    last_wt_div_bars_ago: int = 0

    # Summary
    momentum: int = 0  # 0-5


# ── Unified Market State ────────────────────────────────────────────────────


class MarketState(BaseModel):
    """Complete market state assembled from all Pine Script outputs.

    The bot uses this as the single source of truth for analysis decisions.
    Updated every poll cycle from TradingView MCP data.

    Two tables feed this state:
      - SMT Signals (smt_overlay.pine) — PA + VMC Cipher A overlay
      - SMT Oscillator (smt_oscillator.pine) — momentum + divergences
    """
    # Metadata
    symbol: str = ""
    timeframe: str = ""
    timestamp: Optional[datetime] = None

    # From SMT Signals table (primary — price action + VMC A)
    signal_table: SignalTableData = Field(default_factory=SignalTableData)

    # From SMT Oscillator table (secondary — momentum + divergences)
    oscillator: OscillatorTableData = Field(default_factory=OscillatorTableData)

    # From individual Pine Script drawing objects (supplementary detail)
    mss_events: list[MSSEvent] = Field(default_factory=list)
    fvg_zones: list[FVGZone] = Field(default_factory=list)
    order_blocks: list[OrderBlock] = Field(default_factory=list)
    liquidity_levels: list[LiquidityLevel] = Field(default_factory=list)
    sweep_events: list[SweepEvent] = Field(default_factory=list)
    session_levels: list[SessionLevel] = Field(default_factory=list)

    # Phase 1.5 — attached by runner when derivatives layer is enabled.
    # Typed as Any so the Pydantic model doesn't need to import the dataclasses.
    derivatives: Optional[Any] = None           # DerivativesState
    liquidity_heatmap: Optional[Any] = None     # LiquidityHeatmap

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Convenience accessors — overlay

    @property
    def trend_htf(self) -> Direction:
        return self.signal_table.trend_htf

    @property
    def trend_ltf(self) -> Direction:
        return self.signal_table.trend_ltf

    @property
    def confluence_score(self) -> int:
        return self.signal_table.confluence

    @property
    def current_price(self) -> float:
        return self.signal_table.price

    @property
    def atr(self) -> float:
        return self.signal_table.atr_14

    @property
    def active_session(self) -> Session:
        return self.signal_table.session

    # Convenience accessors — oscillator

    @property
    def momentum_score(self) -> int:
        return self.oscillator.momentum

    @property
    def rsi(self) -> float:
        return self.oscillator.rsi

    @property
    def last_osc_signal(self) -> str:
        return self.oscillator.last_signal

    def active_bull_fvgs(self) -> list[FVGZone]:
        return [z for z in self.fvg_zones
                if z.direction == Direction.BULLISH and z.status == "ACTIVE"]

    def active_bear_fvgs(self) -> list[FVGZone]:
        return [z for z in self.fvg_zones
                if z.direction == Direction.BEARISH and z.status == "ACTIVE"]

    def active_bull_obs(self) -> list[OrderBlock]:
        return [ob for ob in self.order_blocks
                if ob.direction == Direction.BULLISH and ob.status == "ACTIVE"]

    def active_bear_obs(self) -> list[OrderBlock]:
        return [ob for ob in self.order_blocks
                if ob.direction == Direction.BEARISH and ob.status == "ACTIVE"]
