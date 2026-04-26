"""Pydantic model + lifecycle enum for persisted trades.

A TradeRecord is the single row we write to the `trades` SQLite table. It's
the journal's own view of a trade — distinct from `TradePlan` (pre-execution
intent) and `ExecutionReport` (exchange-side outcome). Those two feed into
`record_open`, which produces this record; `CloseFill` feeds `record_close`,
which stamps exit fields onto this record.

Why Pydantic (not dataclass): matches the data-layer convention in
`src.data.models`, gives us JSON round-tripping for `confluence_factors` and
`algo_ids`, and validates datetimes on the way back out of SQLite.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.data.models import Direction


class TradeOutcome(str, Enum):
    """Lifecycle state for a journaled trade.

    OPEN        → entry filled, algo live, position still on the book.
    WIN/LOSS    → position closed with realized PnL > 0 / < 0.
    BREAKEVEN   → position closed with realized PnL == 0 (rare; usually fees flip it).
    CANCELED    → entry never filled or manually aborted — SL/TP never evaluated.

    Kept separate from `src.data.models.TradeOutcome` (WIN/LOSS/BREAKEVEN only)
    because the journal needs lifecycle states the pure-outcome enum lacks.
    """
    OPEN = "OPEN"
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"
    CANCELED = "CANCELED"


class TradeRecord(BaseModel):
    """One row in the `trades` table.

    Fields fall into four groups:
      1. Identity & symbol        — always present
      2. Plan snapshot            — present from open; immutable after
      3. Exit fields              — NULL until close; set by record_close
      4. Optional context         — best-effort, may be None on old rows
    """

    # Identity
    trade_id: str
    symbol: str
    direction: Direction
    outcome: TradeOutcome = TradeOutcome.OPEN

    # Timestamps (UTC — journal always writes tz-aware datetimes)
    signal_timestamp: datetime
    entry_timestamp: datetime
    exit_timestamp: Optional[datetime] = None

    # Plan snapshot (from TradePlan — never mutated after open)
    entry_price: float
    sl_price: float
    tp_price: float
    rr_ratio: float
    leverage: int
    num_contracts: int
    position_size_usdt: float
    risk_amount_usdt: float
    sl_source: str = ""
    reason: str = ""
    confluence_score: float = 0.0
    confluence_factors: list[str] = Field(default_factory=list)

    # Execution context (from ExecutionReport — may be blank in dry-run)
    order_id: Optional[str] = None
    algo_id: Optional[str] = None
    client_order_id: Optional[str] = None
    client_algo_id: Optional[str] = None

    # Market context (threaded by the caller — optional)
    entry_timeframe: Optional[str] = None
    htf_timeframe: Optional[str] = None
    htf_bias: Optional[str] = None
    session: Optional[str] = None
    market_structure: Optional[str] = None

    # Exit (filled at close)
    exit_price: Optional[float] = None
    pnl_usdt: Optional[float] = None
    pnl_r: Optional[float] = None
    fees_usdt: float = 0.0

    # Partial-TP bookkeeping (Madde E) — list of algo IDs attached to the
    # position; rewritten by the monitor after SL-to-BE replaces TP2.
    algo_ids: list[str] = Field(default_factory=list)
    # True once TP1 has filled and the SL has been replaced at break-even.
    # Persisted so that after a restart the monitor does not re-attempt the
    # (already done) cancel-and-replace dance on the still-open remainder.
    sl_moved_to_be: bool = False
    # Why the position was closed — "EARLY_CLOSE_LTF_REVERSAL" etc. (Madde F).
    close_reason: Optional[str] = None

    # Derivatives snapshot at entry (Phase 1.5 Madde 7) — feed for Phase 7
    # RL features. All optional so legacy rows stay readable.
    regime_at_entry: Optional[str] = None
    funding_z_at_entry: Optional[float] = None
    ls_ratio_at_entry: Optional[float] = None
    oi_change_24h_at_entry: Optional[float] = None
    liq_imbalance_1h_at_entry: Optional[float] = None
    nearest_liq_cluster_above_price: Optional[float] = None
    nearest_liq_cluster_below_price: Optional[float] = None
    nearest_liq_cluster_above_notional: Optional[float] = None
    nearest_liq_cluster_below_notional: Optional[float] = None
    # BLOK D-7 — pre-computed ATR distance to the nearest cluster on each
    # side. Avoids post-hoc join against price+ATR (which may not be in the
    # row for older migrations). None when heatmap or ATR is missing.
    nearest_liq_cluster_above_distance_atr: Optional[float] = None
    nearest_liq_cluster_below_distance_atr: Optional[float] = None

    # Phase 7.B5 schema v2 — nullable on pre-pivot rows, filled by 7.C/7.D.
    # setup_zone_source: which zone source produced the limit entry
    #   (fvg_htf | liq_pool | vwap_retest | sweep_retest | market).
    # zone_wait_bars: how many bars the limit sat pending before fill/cancel.
    # zone_fill_latency_bars: bars between placement and fill (<= zone_wait_bars).
    # trend_regime_at_entry: ADX-classifier label (RANGING/WEAK/STRONG_TREND).
    # funding_z_{6h,24h}: windowed funding-rate z-scores (future derivatives work).
    setup_zone_source: Optional[str] = None
    zone_wait_bars: Optional[int] = None
    zone_fill_latency_bars: Optional[int] = None
    trend_regime_at_entry: Optional[str] = None
    funding_z_6h: Optional[float] = None
    funding_z_24h: Optional[float] = None

    # Notes / screenshots (manual or future automation)
    notes: Optional[str] = None
    screenshot_entry: Optional[str] = None
    screenshot_exit: Optional[str] = None

    # 2026-04-19 — demo-wick artefact flags. Populated at record_close time
    # by cross-checking entry / exit candles against a real-market public
    # feed (Binance). `demo_artifact=True` when either side appears to be a
    # demo-only wick that did not happen on a real exchange. Non-destructive
    # (we still keep the trade), but the reporter + RL can filter on the
    # flag so artefact fills don't poison WR or parameter tuning.
    # real_market_entry_valid: True when entry price sits inside the
    #   concurrent real-market candle's [low, high]. False when not.
    #   None when the cross-check couldn't run (feed down, missing candle).
    # real_market_exit_valid:  Same, for the exit candle.
    # demo_artifact: True when at least one side is invalid. None when
    #   neither side could be checked.
    # artifact_reason: short human-readable reason (e.g. "entry_above_binance_high").
    real_market_entry_valid: Optional[bool] = None
    real_market_exit_valid: Optional[bool] = None
    demo_artifact: Optional[bool] = None
    artifact_reason: Optional[str] = None

    # 2026-04-21 — Arkham on-chain enrichment. Opaque dict serialised as
    # JSON when persisted; kept as a dict in the model for structured
    # read-back. None whenever `on_chain.enabled=false` at open time or
    # the snapshot was missing. Downstream tooling (factor_audit.py,
    # GBT feature extraction) can index by the known keys without a
    # schema contract — the runner is the single writer, so the shape
    # stays stable across a single datasets.
    on_chain_context: Optional[dict] = None

    # 2026-04-22 — per-pillar raw confluence scores (factor name → weight).
    # Captured from `ConfluenceScore.factors` at entry time so Pass 2 can
    # replay-tune per-pillar weights without re-fetching market state.
    # Empty dict on rows written before this column (pass-1 backfill not
    # needed; absent factors just decode as {}). Single-writer = runner =
    # shape stays stable. JSON dict in SQLite.
    confluence_pillar_scores: dict[str, float] = Field(default_factory=dict)

    # 2026-04-22 (gece, late) — oscillator raw numeric snapshot at entry
    # (+ placement time for pending-fills). Shape:
    #   {"1m": {...OscillatorTableData fields...},
    #    "3m": {...},
    #    "15m": {...}}
    # Any TF may be missing when the cache was cleared (already-open HTF
    # skip, LTF read failure, non-bridge tests). Feeds Pass 2 GBT as
    # continuous features — wt1/wt2, rsi, rsi_mfi, stoch_k/d, momentum,
    # divergence flags per TF. Enables multi-TF patterns ("1m oversold +
    # 15m trending up") that factor names alone can't express.
    oscillator_raw_values: dict[str, dict] = Field(default_factory=dict)

    # 2026-04-23 — extended derivatives enrichment (Pass 3 GBT inputs).
    # All already populated on `DerivativesState` each cycle; previously
    # only a subset landed in journal. Captures OI absolute + 1h change
    # (classic OI×price combinatorial inference), absolute funding +
    # predicted-next (basis/cost-of-carry), 1h liquidation notional per
    # side (flow pressure), LS z-score (crowded-positioning speed), and
    # price changes over 1h/4h from the entry-TF candle buffer
    # (price-OI divergence patterns). None on rows where the cache was
    # unavailable (bridge=None tests, early tick before Coinalyze warms).
    open_interest_usd_at_entry: Optional[float] = None
    oi_change_1h_pct_at_entry: Optional[float] = None
    funding_rate_current_at_entry: Optional[float] = None
    funding_rate_predicted_at_entry: Optional[float] = None
    long_liq_notional_1h_at_entry: Optional[float] = None
    short_liq_notional_1h_at_entry: Optional[float] = None
    ls_ratio_zscore_14d_at_entry: Optional[float] = None
    price_change_1h_pct_at_entry: Optional[float] = None
    price_change_4h_pct_at_entry: Optional[float] = None
    # Top-N liq heatmap clusters (JSON). Shape:
    #   {"above": [{"price": .., "notional_usd": .., "distance_atr": ..}, ...],
    #    "below": [{...}, ...]}
    # Default empty dict; richer target/magnet modelling in Pass 3 vs just
    # the nearest-above/nearest-below pair.
    liq_heatmap_top_clusters: dict = Field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.outcome == TradeOutcome.OPEN

    @property
    def is_closed(self) -> bool:
        return self.outcome in (
            TradeOutcome.WIN, TradeOutcome.LOSS, TradeOutcome.BREAKEVEN,
        )

    @property
    def is_win(self) -> bool:
        return self.outcome == TradeOutcome.WIN

    @property
    def is_loss(self) -> bool:
        return self.outcome == TradeOutcome.LOSS


class RejectedSignal(BaseModel):
    """One row in the `rejected_signals` table (Phase 7.B1).

    Persists the context around every `plan is None` return from
    `build_trade_plan_with_reason`. Feeds two downstream uses:
      1. `scripts/peg_rejected_outcomes.py` walks candles forward N bars
         and stamps a hypothetical outcome on each reject — the
         counter-factual dataset that validates/invalidates our veto logic.
      2. `scripts/factor_audit.py` joins rejects vs. trades for a fair
         apples-to-apples WR comparison across reject_reason buckets.

    All snapshot fields are nullable — some reject paths short-circuit
    before SL/TP math is reached (e.g. `below_confluence`) so proposed_*
    columns stay None on those rows.
    """

    rejection_id: str
    symbol: str
    direction: Direction
    reject_reason: str
    signal_timestamp: datetime

    # Snapshot at reject time
    price: Optional[float] = None
    atr: Optional[float] = None
    confluence_score: float = 0.0
    confluence_factors: list[str] = Field(default_factory=list)

    entry_timeframe: Optional[str] = None
    htf_timeframe: Optional[str] = None
    htf_bias: Optional[str] = None
    session: Optional[str] = None
    market_structure: Optional[str] = None

    # Proposed plan — populated when reject happens after SL/TP math
    # (htf_tp_ceiling, tp_too_tight, insufficient_contracts_for_split);
    # stays None on pre-math rejects (below_confluence, session_filter, etc.)
    proposed_sl_price: Optional[float] = None
    proposed_tp_price: Optional[float] = None
    proposed_rr_ratio: Optional[float] = None

    # Derivatives snapshot — same fields as TradeRecord
    regime_at_entry: Optional[str] = None
    funding_z_at_entry: Optional[float] = None
    ls_ratio_at_entry: Optional[float] = None
    oi_change_24h_at_entry: Optional[float] = None
    liq_imbalance_1h_at_entry: Optional[float] = None
    nearest_liq_cluster_above_price: Optional[float] = None
    nearest_liq_cluster_below_price: Optional[float] = None
    nearest_liq_cluster_above_notional: Optional[float] = None
    nearest_liq_cluster_below_notional: Optional[float] = None
    nearest_liq_cluster_above_distance_atr: Optional[float] = None
    nearest_liq_cluster_below_distance_atr: Optional[float] = None

    # Cross-asset pillar state (Phase 7.A6) — essential for auditing
    # `cross_asset_opposition` rejects: were BTC + ETH really opposing?
    pillar_btc_bias: Optional[str] = None
    pillar_eth_bias: Optional[str] = None

    # Counter-factual outcome — filled by peg_rejected_outcomes.py after N bars
    # "WIN" = TP hit first, "LOSS" = SL hit first, "NEITHER" = neither inside window.
    hypothetical_outcome: Optional[str] = None
    hypothetical_bars_to_tp: Optional[int] = None
    hypothetical_bars_to_sl: Optional[int] = None

    # 2026-04-21 — mirrors TradeRecord.on_chain_context. Carried on
    # rejects so factor_audit.py can segment reject reasons by on-chain
    # context (e.g., were `cross_asset_opposition` rejects concentrated
    # on days with bearish daily_macro_bias?).
    on_chain_context: Optional[dict] = None

    # 2026-04-22 — mirrors TradeRecord.confluence_pillar_scores. Feeds
    # Pass 2 per-pillar weight tuning on the rejected-signal counter-
    # factual dataset too: if removing `mss_alignment` would have accepted
    # a reject that pegged WIN, that's useful signal.
    confluence_pillar_scores: dict[str, float] = Field(default_factory=dict)

    # 2026-04-22 (gece, late) — mirrors TradeRecord.oscillator_raw_values.
    # Captured at reject time (or pending placement time for mid-pending
    # cancels). Shape identical: {"1m": {...}, "3m": {...}, "15m": {...}}.
    oscillator_raw_values: dict[str, dict] = Field(default_factory=dict)

    # 2026-04-23 — mirrors TradeRecord.* extended derivatives enrichment.
    # Lets Pass 3 counter-factual analysis test "would trade have opened
    # at this OI/funding/LS state?" for the reject subset too.
    open_interest_usd_at_entry: Optional[float] = None
    oi_change_1h_pct_at_entry: Optional[float] = None
    funding_rate_current_at_entry: Optional[float] = None
    funding_rate_predicted_at_entry: Optional[float] = None
    long_liq_notional_1h_at_entry: Optional[float] = None
    short_liq_notional_1h_at_entry: Optional[float] = None
    ls_ratio_zscore_14d_at_entry: Optional[float] = None
    price_change_1h_pct_at_entry: Optional[float] = None
    price_change_4h_pct_at_entry: Optional[float] = None
    liq_heatmap_top_clusters: dict = Field(default_factory=dict)


class WhaleTransferRecord(BaseModel):
    """One row in the `whale_transfers` table (Phase 8 data layer).

    Streamed from the Arkham WebSocket listener (`on_chain_ws.py`) when
    a transfer crosses the `whale_threshold_usd` threshold. Runtime entry
    pipeline does NOT react (gate removed 2026-04-22); this row exists
    for Phase 9 GBT analysis — joining whale events against trade
    outcomes via `captured_at` vs `entry_timestamp`/`exit_timestamp`
    reveals whether specific directional flows (Coinbase→Binance BTC,
    exchange→cold wallet ETH, etc.) correlate with subsequent price move.

    `from_entity` / `to_entity` / `tx_hash` may be None when Arkham's
    feed didn't include them on a given message — the gate keeps these
    optional rather than dropping the row.
    """

    captured_at: datetime
    token: str
    usd_value: float
    from_entity: Optional[str] = None
    to_entity: Optional[str] = None
    tx_hash: Optional[str] = None
    # JSON list of OKX perp symbols affected (stablecoin events fan out
    # to every watched symbol; chain-native collapses to one). Mirrors
    # `affected_symbols_for()` output at event time.
    affected_symbols: list[str] = Field(default_factory=list)


class PositionSnapshotRecord(BaseModel):
    """One intra-trade state snapshot for an OPEN position.

    Joined back to `trades.trade_id` at read time. Captured every
    `journal.position_snapshot_cadence_s` seconds (default 300s) for every
    OPEN position, so the post-hoc RL/GBT layer can answer "could this trade
    have been exited at +1.3R when MFE peaked?" without any extra exchange
    API cost — `mark_price` + `unrealized_pnl_usdt` come from the live
    `get_positions` payload the monitor already polls every 5s, the drift
    fields read from `BotContext.{derivatives_cache, on_chain_snapshot,
    last_market_state_per_symbol}` caches.

    Optional fields (all `*_now*` columns) may be None when the relevant
    cache is cold (first cycle for a symbol post-restart, Arkham fetch
    failed, etc.) — readers should treat None as MISSING-by-coverage, not
    "value was zero".
    """

    trade_id: str
    captured_at: datetime
    mark_price: float
    unrealized_pnl_usdt: float
    unrealized_pnl_r: float
    mfe_r_so_far: float
    mae_r_so_far: float
    current_sl_price: float
    current_tp_price: Optional[float] = None
    sl_to_be_moved: bool = False
    mfe_lock_applied: bool = False
    derivatives_funding_now: Optional[float] = None
    derivatives_oi_now_usd: Optional[float] = None
    derivatives_ls_ratio_now: Optional[float] = None
    derivatives_long_liq_1h_now: Optional[float] = None
    derivatives_short_liq_1h_now: Optional[float] = None
    on_chain_btc_netflow_now_usd: Optional[float] = None
    on_chain_stablecoin_pulse_now: Optional[float] = None
    on_chain_flow_alignment_now: Optional[float] = None
    oscillator_3m_now_json: dict[str, Any] = Field(default_factory=dict)
    vwap_3m_distance_atr_now: Optional[float] = None
