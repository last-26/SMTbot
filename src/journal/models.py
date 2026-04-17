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
from typing import Optional

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

    # Notes / screenshots (manual or future automation)
    notes: Optional[str] = None
    screenshot_entry: Optional[str] = None
    screenshot_exit: Optional[str] = None

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
