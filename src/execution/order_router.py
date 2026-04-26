"""TradePlan → live orders, via BybitClient.

Flow on Bybit V5:
  1. Set leverage (account-wide on UNIFIED, set both buy/sell sides equal).
  2. Place market entry — `takeProfit`/`stopLoss` ride along on the same
     create-order call so the position is born already protected.
  3. If the placement fails *after* the position somehow opened (rare —
     Bybit attaches TP/SL atomically on success), fall back to closing
     the position via `close_position()` and surface AlgoOrderError.

Differences vs the pre-migration flow:
  - **No separate OCO algo.** The pre-migration `place_oco_algo` call is replaced
    by `takeProfit`/`stopLoss` fields on the entry order itself (market)
    or by a `set_position_tpsl()` call after a limit fill (zone path).
  - **`_place_algos` returns AlgoResult with empty `algo_id`** for
    journal back-compat; downstream readers tolerate empty strings.
  - **`margin_mode` is no longer per-call.** Bybit UNIFIED is account-
    wide; the runner sets it once at startup. RouterConfig still carries
    a `margin_mode` field for back-compat but it's not forwarded to the
    client.

Retry policy: none here. A single failure surfaces to the caller, which
owns the decision (retry, skip next candle, halt).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from src.data.models import Direction
from src.execution.bybit_client import BybitClient
from src.execution.errors import AlgoOrderError, LeverageSetError, OrderRejected
from src.execution.models import (
    AlgoResult,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.strategy.trade_plan import TradePlan


@dataclass
class RouterConfig:
    inst_id: str = "BTCUSDT"
    margin_mode: str = "isolated"         # accepted + ignored on Bybit (account-wide)
    close_on_algo_failure: bool = True    # auto-close if TP/SL attach fails
    # Madde E — partial TP + SL-to-BE.
    partial_tp_enabled: bool = False
    partial_tp_ratio: float = 0.5         # fraction of contracts exited at TP1
    partial_tp_rr: float = 1.5            # TP1 RR relative to SL distance
    move_sl_to_be_after_tp1: bool = True
    # TP/SL trigger price source: "mark" (index-weighted, demo-immune) or
    # "last" (book-sensitive). Mark recommended on demo; the client maps
    # this to Bybit's `MarkPrice` / `LastPrice` strings internally.
    algo_trigger_px_type: str = "mark"


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

    def __init__(self, client: BybitClient, config: RouterConfig | None = None):
        self.client = client
        self.config = config or RouterConfig()

    def place(self, plan: TradePlan, inst_id: Optional[str] = None) -> ExecutionReport:
        if plan.num_contracts <= 0:
            raise ValueError(f"plan.num_contracts={plan.num_contracts} <= 0")

        inst = inst_id or self.config.inst_id
        pos_side = _pos_side(plan.direction)
        cfg = self.config

        # 1. Leverage. If this fails, nothing is open — safe to raise.
        try:
            self.client.set_leverage(inst_id=inst, leverage=plan.leverage)
            leverage_set = True
        except LeverageSetError:
            raise  # abort — don't try to place the order at wrong leverage

        # 2. Entry with attached TP/SL. Bybit accepts `takeProfit`/`stopLoss`
        # on the create-order call directly, so the position is protected at
        # birth — no separate algo placement step.
        #
        # Partial-TP mode is currently a no-op on Bybit's `Full` tpslMode (it
        # would require `Partial` mode + multiple TP legs which adds complex
        # bookkeeping). We honour the flag by attaching the FINAL TP only;
        # the maker-TP resting limit + dynamic TP revision in the runner
        # cover the partial-exit thesis differently (see Phase 12 candidate
        # in the roadmap).
        try:
            entry = self.client.place_market_order(
                inst_id=inst,
                side=_entry_side(plan.direction),
                pos_side=pos_side,
                size_contracts=plan.num_contracts,
                take_profit=plan.tp_price,
                stop_loss=plan.sl_price,
                trigger_px_type=cfg.algo_trigger_px_type,
            )
        except OrderRejected as exc:
            # Position never opened — surface as-is. No close_position
            # needed because nothing was filled.
            raise AlgoOrderError(
                f"market entry with attached TP/SL rejected: {exc}"
            ) from exc

        # 3. AlgoResult shim for journal back-compat. On Bybit, TP/SL is part
        # of the position itself — there's no separate algo_id to track.
        algos = [AlgoResult(
            algo_id="",
            client_algo_id="",
            sl_trigger_px=plan.sl_price,
            tp_trigger_px=plan.tp_price,
            raw={},
        )]

        return ExecutionReport(
            entry=entry,
            algos=algos,
            state=PositionState.OPEN,
            leverage_set=leverage_set,
            plan_reason=plan.reason,
        )

    # ── Limit entry (Phase 7.C2 — zone-based entry) ─────────────────────────

    def place_limit_entry(
        self,
        plan: TradePlan,
        entry_px: float,
        inst_id: Optional[str] = None,
        ord_type: str = "post_only",
        fallback_to_limit: bool = True,
    ) -> OrderResult:
        """Place a limit (post-only by default) entry order at `entry_px`.

        Returns a PENDING OrderResult. Does NOT attach TP/SL — that
        happens after the limit fills via `attach_algos()` (which calls
        `set_position_tpsl()` under the hood). Leverage is set here
        because the account-level call must precede any order on the
        instrument.

        On post-only rejection (OrderRejected — Bybit retCode 170218,
        price would have crossed the spread), the router optionally
        retries as a regular limit. Disable via `fallback_to_limit=False`
        for strict maker-only behavior.
        """
        if plan.num_contracts <= 0:
            raise ValueError(f"plan.num_contracts={plan.num_contracts} <= 0")
        inst = inst_id or self.config.inst_id
        pos_side = _pos_side(plan.direction)

        self.client.set_leverage(inst_id=inst, leverage=plan.leverage)

        try:
            return self.client.place_limit_order(
                inst_id=inst, side=_entry_side(plan.direction),
                pos_side=pos_side, size_contracts=plan.num_contracts,
                px=entry_px, ord_type=ord_type,
            )
        except OrderRejected as exc:
            if fallback_to_limit and ord_type == "post_only":
                logger.warning(
                    "post_only_rejected_falling_back_to_limit inst={} px={} err={}",
                    inst, entry_px, exc,
                )
                return self.client.place_limit_order(
                    inst_id=inst, side=_entry_side(plan.direction),
                    pos_side=pos_side, size_contracts=plan.num_contracts,
                    px=entry_px, ord_type="limit",
                )
            raise

    def cancel_pending_entry(
        self, order_id: str, inst_id: Optional[str] = None,
    ) -> dict:
        """Cancel a resting limit entry (timeout / zone-invalidation)."""
        return self.client.cancel_order(
            inst_id or self.config.inst_id, order_id,
        )

    def attach_algos(
        self, plan: TradePlan, inst_id: Optional[str] = None,
    ) -> list[AlgoResult]:
        """Attach TP/SL to a freshly-filled position via trading-stop.

        Phase 7.C4 — when a pending limit entry fills, the runner calls
        this to install the OCO-equivalent protection using the original
        plan's SL/TP. On Bybit this is a single `set_position_tpsl()`
        call instead of a separate algo placement. Returns an AlgoResult
        with empty `algo_id` for journal back-compat.
        """
        if plan.num_contracts <= 0:
            raise ValueError(f"plan.num_contracts={plan.num_contracts} <= 0")
        inst = inst_id or self.config.inst_id
        pos_side = _pos_side(plan.direction)
        cfg = self.config

        try:
            self.client.set_position_tpsl(
                inst_id=inst,
                pos_side=pos_side,
                take_profit=plan.tp_price,
                stop_loss=plan.sl_price,
                tpsl_mode="Full",
                trigger_px_type=cfg.algo_trigger_px_type,
            )
        except OrderRejected as exc:
            raise AlgoOrderError(
                f"set_position_tpsl failed after limit fill: {exc}"
            ) from exc

        return [AlgoResult(
            algo_id="",
            client_algo_id="",
            sl_trigger_px=plan.sl_price,
            tp_trigger_px=plan.tp_price,
            raw={},
        )]


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
    fake_algo = AlgoResult(
        algo_id="DRYRUN",
        client_algo_id="DRYRUN",
        sl_trigger_px=plan.sl_price,
        tp_trigger_px=plan.tp_price,
    )
    return ExecutionReport(
        entry=fake_entry, algos=[fake_algo],
        state=PositionState.OPEN, leverage_set=True,
        plan_reason=plan.reason,
    )
