"""Strategy engine — R:R sizing, entry signals, circuit breakers.

Public surface:
  - `TradePlan`                — the sized, risk-bounded trade
  - `calculate_trade_plan`     — pure R:R math
  - `generate_entry_intent`    — MarketState + confluence → EntryIntent
  - `build_trade_plan_from_state` — full pipeline wrapper
  - `RiskManager`              — circuit breakers + balance bookkeeping
"""

from src.strategy.entry_signals import (
    EntryIntent,
    build_trade_plan_from_state,
    generate_entry_intent,
    select_sl_price,
)
from src.strategy.risk_manager import (
    CircuitBreakerConfig,
    RiskManager,
    TradeResult,
)
from src.strategy.rr_system import (
    break_even_win_rate,
    calculate_trade_plan,
    expected_value_r,
)
from src.strategy.trade_plan import TradePlan

__all__ = [
    "TradePlan",
    "calculate_trade_plan",
    "break_even_win_rate",
    "expected_value_r",
    "EntryIntent",
    "generate_entry_intent",
    "build_trade_plan_from_state",
    "select_sl_price",
    "RiskManager",
    "CircuitBreakerConfig",
    "TradeResult",
]
