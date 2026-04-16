"""Circuit-breakers — hard guardrails that can halt trading.

The strategy engine produces valid TradePlans all day long. The risk manager
decides whether any of them are ALLOWED to go to execution.

Checks (CLAUDE.md, all non-negotiable):
  - daily realized PnL ≤ -max_daily_loss_pct → halt for cooldown_hours
  - consecutive losing trades ≥ max_consecutive_losses → halt + alert
  - drawdown from peak balance ≥ max_drawdown_pct → full stop, manual restart
  - open positions ≥ max_concurrent_positions → block new entries
  - leverage ≤ max_leverage (enforced upstream in rr_system, re-checked here)
  - rr_ratio ≥ min_rr_ratio

Pure state + records; no persistence. Journal (Phase 5) will replay trades
to rebuild this state at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.strategy.trade_plan import TradePlan


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── Config / records ────────────────────────────────────────────────────────


@dataclass
class CircuitBreakerConfig:
    max_daily_loss_pct: float = 3.0           # fraction of starting-of-day balance, in %
    max_consecutive_losses: int = 5
    max_drawdown_pct: float = 10.0            # from peak, in %
    max_concurrent_positions: int = 2
    max_leverage: int = 20
    min_rr_ratio: float = 2.0
    cooldown_hours: int = 24


@dataclass
class TradeResult:
    """Minimal record the journal will also store — fed back into RiskManager."""
    pnl_usdt: float
    pnl_r: float
    timestamp: datetime = field(default_factory=_utc_now)

    @property
    def is_win(self) -> bool:
        return self.pnl_usdt > 0

    @property
    def is_loss(self) -> bool:
        return self.pnl_usdt < 0


# ── Manager ─────────────────────────────────────────────────────────────────


class RiskManager:
    """Tracks balance, drawdown, losses, and open-position count.

    Two write entry points:
      - `register_trade_opened()` when a TradePlan is sent to the exchange
      - `register_trade_closed(TradeResult)` when a position exits

    One read entry point:
      - `can_trade(plan, now=...)` → (allowed, reason)
    """

    def __init__(
        self,
        starting_balance: float,
        config: Optional[CircuitBreakerConfig] = None,
        now: Optional[datetime] = None,
    ):
        if starting_balance <= 0:
            raise ValueError("starting_balance must be positive")
        now = now or _utc_now()
        self.config = config or CircuitBreakerConfig()
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.peak_balance = starting_balance
        self.day_start_balance = starting_balance
        self.day_started_at = now
        self.daily_realized_pnl = 0.0
        self.consecutive_losses = 0
        self.open_positions = 0
        self.halted_until: Optional[datetime] = None
        self.halt_reason: str = ""
        self.trades: list[TradeResult] = []

    # ── Time-of-day bookkeeping ─────────────────────────────────────────────

    def _maybe_roll_day(self, now: datetime) -> None:
        """Reset daily counters when we cross into a new UTC day."""
        if now.date() != self.day_started_at.date():
            self.day_started_at = now
            self.day_start_balance = self.current_balance
            self.daily_realized_pnl = 0.0

    # ── Derived metrics ─────────────────────────────────────────────────────

    @property
    def drawdown_pct(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return max(0.0, (self.peak_balance - self.current_balance) / self.peak_balance * 100.0)

    @property
    def daily_loss_pct(self) -> float:
        """Positive number when losing; 0 when flat or winning."""
        if self.day_start_balance <= 0 or self.daily_realized_pnl >= 0:
            return 0.0
        return abs(self.daily_realized_pnl) / self.day_start_balance * 100.0

    # ── Gate ────────────────────────────────────────────────────────────────

    def can_trade(
        self,
        plan: Optional[TradePlan] = None,
        now: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """Return (allowed, reason). Reason is "" when allowed."""
        now = now or _utc_now()
        self._maybe_roll_day(now)

        # Drawdown comes first: per CLAUDE.md it's a "manual restart required"
        # stop, which is more severe than a cooldown halt.
        if self.drawdown_pct >= self.config.max_drawdown_pct:
            return False, (
                f"max_drawdown exceeded: {self.drawdown_pct:.2f}% "
                f">= {self.config.max_drawdown_pct:.2f}% (manual restart required)"
            )

        if self.halted_until is not None and now < self.halted_until:
            return False, f"halted until {self.halted_until.isoformat()}: {self.halt_reason}"
        if self.halted_until is not None and now >= self.halted_until:
            # Cooldown over — clear the halt
            self.halted_until = None
            self.halt_reason = ""

        if self.daily_loss_pct >= self.config.max_daily_loss_pct:
            return False, (
                f"daily_loss exceeded: {self.daily_loss_pct:.2f}% "
                f">= {self.config.max_daily_loss_pct:.2f}%"
            )

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return False, (
                f"consecutive_losses={self.consecutive_losses} "
                f">= {self.config.max_consecutive_losses}"
            )

        if self.open_positions >= self.config.max_concurrent_positions:
            return False, (
                f"open_positions={self.open_positions} "
                f">= {self.config.max_concurrent_positions}"
            )

        if plan is not None:
            if plan.leverage > self.config.max_leverage:
                return False, (
                    f"plan.leverage={plan.leverage} > max_leverage={self.config.max_leverage}"
                )
            if plan.rr_ratio < self.config.min_rr_ratio:
                return False, (
                    f"plan.rr_ratio={plan.rr_ratio} < min_rr_ratio={self.config.min_rr_ratio}"
                )
            if plan.num_contracts <= 0:
                return False, "plan.num_contracts <= 0"

        return True, ""

    # ── State mutations ─────────────────────────────────────────────────────

    def register_trade_opened(self) -> None:
        self.open_positions += 1

    def register_trade_closed(
        self,
        result: TradeResult,
        now: Optional[datetime] = None,
    ) -> None:
        """Apply a realized PnL to balance, update streaks, maybe halt."""
        now = now or result.timestamp or _utc_now()
        self._maybe_roll_day(now)

        self.trades.append(result)
        self.current_balance += result.pnl_usdt
        self.daily_realized_pnl += result.pnl_usdt
        self.peak_balance = max(self.peak_balance, self.current_balance)
        self.open_positions = max(0, self.open_positions - 1)

        if result.is_loss:
            self.consecutive_losses += 1
        elif result.is_win:
            self.consecutive_losses = 0
        # Break-even trades leave the streak unchanged.

        # Trigger cooldown halt when daily-loss or consecutive-loss thresholds hit.
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self._halt(
                now,
                reason=f"consecutive_losses={self.consecutive_losses}",
                hours=self.config.cooldown_hours,
            )
        elif self.daily_loss_pct >= self.config.max_daily_loss_pct:
            self._halt(
                now,
                reason=f"daily_loss={self.daily_loss_pct:.2f}%",
                hours=self.config.cooldown_hours,
            )

    def _halt(self, now: datetime, reason: str, hours: int) -> None:
        self.halted_until = now + timedelta(hours=hours)
        self.halt_reason = reason

    def force_halt(
        self,
        reason: str,
        hours: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """Manual halt from the operator — used for news blackouts, etc."""
        hours = hours if hours is not None else self.config.cooldown_hours
        self._halt(now or _utc_now(), reason=reason, hours=hours)

    def clear_halt(self) -> None:
        """Manual resume (e.g. after investigating a max-drawdown stop)."""
        self.halted_until = None
        self.halt_reason = ""
