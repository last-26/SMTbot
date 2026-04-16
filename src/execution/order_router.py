"""TradePlan → live orders, via OKXClient.

Flow:
  1. Set leverage (isolated margin by default — each position is its own silo).
  2. Place market entry (size = plan.num_contracts).
  3. Immediately place OCO algo (SL + TP) on that position.
  4. If the algo fails, the position is UNPROTECTED — raise AlgoOrderError
     so the caller can react (close the position or alert).

Retry policy: none here. A single failure surfaces to the caller, which
owns the decision (retry, skip next candle, halt).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data.models import Direction
from src.execution.errors import AlgoOrderError, LeverageSetError
from src.execution.models import (
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.execution.okx_client import OKXClient
from src.strategy.trade_plan import TradePlan


@dataclass
class RouterConfig:
    inst_id: str = "BTC-USDT-SWAP"
    margin_mode: str = "isolated"         # "isolated" or "cross"
    close_on_algo_failure: bool = True    # auto-close if OCO placement fails


def _pos_side(direction: Direction) -> str:
    if direction == Direction.BULLISH:
        return "long"
    if direction == Direction.BEARISH:
        return "short"
    raise ValueError(f"Cannot route plan with direction={direction}")


def _entry_side(direction: Direction) -> str:
    return "buy" if direction == Direction.BULLISH else "sell"


class OrderRouter:
    """Stateless-ish: holds a client + config, places one plan at a time."""

    def __init__(self, client: OKXClient, config: RouterConfig | None = None):
        self.client = client
        self.config = config or RouterConfig()

    def place(self, plan: TradePlan, inst_id: Optional[str] = None) -> ExecutionReport:
        if plan.num_contracts <= 0:
            raise ValueError(f"plan.num_contracts={plan.num_contracts} <= 0")

        inst = inst_id or self.config.inst_id
        pos_side = _pos_side(plan.direction)

        # 1. Leverage. If this fails, nothing is open — safe to raise.
        try:
            self.client.set_leverage(
                inst_id=inst,
                leverage=plan.leverage,
                mgn_mode=self.config.margin_mode,
                pos_side=pos_side if self.config.margin_mode == "isolated" else None,
            )
            leverage_set = True
        except LeverageSetError:
            raise  # abort — don't try to place the order at wrong leverage

        # 2. Entry. If this raises, no position open, no algo needed.
        entry = self.client.place_market_order(
            inst_id=inst,
            side=_entry_side(plan.direction),
            pos_side=pos_side,
            size_contracts=plan.num_contracts,
            td_mode=self.config.margin_mode,
        )
        # OKX returns an ord_id immediately; fill status arrives on the next
        # poll. We mark it PENDING → the monitor flips it to FILLED later.

        # 3. Algo. If it fails, the position is live and unprotected.
        try:
            algo = self.client.place_oco_algo(
                inst_id=inst,
                pos_side=pos_side,
                size_contracts=plan.num_contracts,
                sl_trigger_px=plan.sl_price,
                tp_trigger_px=plan.tp_price,
                td_mode=self.config.margin_mode,
            )
        except Exception as exc:
            if self.config.close_on_algo_failure:
                try:
                    self.client.close_position(inst, pos_side, self.config.margin_mode)
                except Exception:
                    # Best effort — if close also fails, the caller must
                    # intervene manually. Surface the algo error regardless.
                    pass
            raise AlgoOrderError(
                f"OCO algo placement failed after entry {entry.order_id}: {exc}"
            ) from exc

        return ExecutionReport(
            entry=entry,
            algo=algo,
            state=PositionState.OPEN,
            leverage_set=leverage_set,
            plan_reason=plan.reason,
        )


# ── Dry-run helper ──────────────────────────────────────────────────────────


def dry_run_report(plan: TradePlan, config: RouterConfig | None = None) -> ExecutionReport:
    """Build a fake ExecutionReport without touching the network.

    Useful for demo-of-demo: running the pipeline end-to-end with the bot
    in PAPER mode before any real API call.
    """
    config = config or RouterConfig()
    fake_entry = OrderResult(
        order_id="DRYRUN",
        client_order_id="DRYRUN",
        status=OrderStatus.FILLED,
        filled_sz=float(plan.num_contracts),
        avg_price=plan.entry_price,
    )
    from src.execution.models import AlgoResult  # local import to avoid cycle noise
    fake_algo = AlgoResult(
        algo_id="DRYRUN",
        client_algo_id="DRYRUN",
        sl_trigger_px=plan.sl_price,
        tp_trigger_px=plan.tp_price,
    )
    return ExecutionReport(
        entry=fake_entry, algo=fake_algo,
        state=PositionState.OPEN, leverage_set=True,
        plan_reason=plan.reason,
    )
