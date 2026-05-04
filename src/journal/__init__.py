"""Trade journal: persist every trade to SQLite, compute performance reports.

The journal is the bridge between one bot run and the next:
  - It records every TradePlan/ExecutionReport at open time.
  - It records the realized PnL at close time.
  - On startup, it replays closed trades to rebuild RiskManager state
    (drawdown, consecutive losses, peak balance, etc).
  - In Phase 6 it becomes the training-data source for the RL tuner.
"""

from __future__ import annotations

from src.journal.database import TradeJournal
from src.journal.models import (
    DecisionLogRecord,
    RejectedSignal,
    TradeOutcome,
    TradeRecord,
    WhaleTransferRecord,
)
from src.journal.reporter import (
    avg_r,
    calmar,
    equity_curve,
    expectancy_r,
    format_summary,
    max_consecutive_losses,
    max_consecutive_wins,
    max_drawdown,
    profit_factor,
    regime_breakdown,
    sharpe_r,
    summary,
    win_rate,
    win_rate_by_factor,
    win_rate_by_factor_combo,
    win_rate_by_score_bucket,
    win_rate_by_session,
    win_rate_by_symbol,
)

__all__ = [
    "DecisionLogRecord",
    "RejectedSignal",
    "TradeJournal",
    "TradeRecord",
    "TradeOutcome",
    "WhaleTransferRecord",
    "avg_r",
    "calmar",
    "equity_curve",
    "expectancy_r",
    "format_summary",
    "max_consecutive_losses",
    "max_consecutive_wins",
    "max_drawdown",
    "profit_factor",
    "regime_breakdown",
    "sharpe_r",
    "summary",
    "win_rate",
    "win_rate_by_factor",
    "win_rate_by_factor_combo",
    "win_rate_by_score_bucket",
    "win_rate_by_session",
    "win_rate_by_symbol",
]
