"""Shared fixtures + fakes for bot-runner integration tests.

The runner is duck-typed: reader / router / monitor / bybit_client are all
interface-only. These fakes implement just enough surface for the runner
to exercise its full decision tree without touching TV, Bybit, or disk.

Constructors:
  - `make_plan(**overrides)`   — a TradePlan ready for the router
  - `make_report(**overrides)` — an ExecutionReport mirroring a successful place
  - `make_state()`             — a minimal MarketState (all defaults)
  - `make_config()`            — a BotConfig with dummy Bybit creds
  - `make_close_fill(...)`     — a CloseFill with non-zero pnl (post-enrichment)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from src.bot.config import BotConfig
from src.data.models import Direction, MarketState
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    PositionState,
)
from src.strategy.trade_plan import TradePlan


# ── Builders ────────────────────────────────────────────────────────────────


def make_plan(**overrides) -> TradePlan:
    defaults = dict(
        direction=Direction.BULLISH,
        entry_price=67_000.0,
        sl_price=66_500.0,
        tp_price=68_500.0,
        rr_ratio=3.0,
        sl_distance=500.0,
        sl_pct=500.0 / 67_000.0,
        position_size_usdt=1_000.0,
        leverage=10,
        required_leverage=10.0,
        num_contracts=5,
        risk_amount_usdt=10.0,
        max_risk_usdt=10.0,
        capped=False,
        sl_source="order_block",
        confluence_score=5.0,
        confluence_factors=["OB_test", "FVG_active"],
        reason="test plan",
    )
    defaults.update(overrides)
    return TradePlan(**defaults)


def make_report(**overrides) -> ExecutionReport:
    defaults = dict(
        entry=OrderResult(
            order_id="ORD-1", client_order_id="cli-ord-1",
            status=OrderStatus.PENDING,
        ),
        algo=AlgoResult(
            algo_id="ALG-1", client_algo_id="cli-alg-1",
            sl_trigger_px=66_500.0, tp_trigger_px=68_500.0,
        ),
        state=PositionState.OPEN,
        leverage_set=True,
    )
    defaults.update(overrides)
    return ExecutionReport(**defaults)


def make_state() -> MarketState:
    return MarketState(symbol="BTC-USDT-SWAP", timeframe="15")


def make_close_fill(**overrides) -> CloseFill:
    defaults = dict(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        entry_price=67_000.0, exit_price=68_500.0, size=5.0,
        pnl_usdt=30.0,                                   # post-enrichment
    )
    defaults.update(overrides)
    return CloseFill(**defaults)


def make_config(**trading_overrides) -> BotConfig:
    """Build a valid BotConfig without round-tripping through YAML."""
    raw = {
        "bot": {"mode": "demo", "poll_interval_seconds": 0.01,
                "timezone": "UTC", "starting_balance": 1_000.0},
        "trading": {
            "symbols": ["BTC-USDT-SWAP"], "entry_timeframe": "15m",
            "htf_timeframe": "4H", "risk_per_trade_pct": 1.0,
            "max_leverage": 20, "default_rr_ratio": 3.0,
            "min_rr_ratio": 2.0, "max_concurrent_positions": 2,
            "contract_size": 0.01,
        },
        "circuit_breakers": {
            "max_daily_loss_pct": 3.0, "max_consecutive_losses": 5,
            "max_drawdown_pct": 10.0, "cooldown_hours": 24,
        },
        "analysis": {
            "min_confluence_score": 2, "candle_buffer_size": 500,
            "swing_lookback": 20, "sr_min_touches": 3,
            "sr_zone_atr_mult": 0.5,
            "session_filter": ["london", "new_york"],
        },
        "bybit": {
            "base_url": "https://api-demo.bybit.com", "demo": True,
            "account_type": "UNIFIED", "category": "linear",
            "api_key": "k", "api_secret": "s",
        },
        "journal": {"db_path": ":memory:"},
    }
    raw["trading"].update(trading_overrides)
    return BotConfig(**raw)


# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeReader:
    def __init__(self, state: Optional[MarketState] = None,
                 raise_exc: Optional[Exception] = None):
        self.state = state or make_state()
        self.raise_exc = raise_exc
        self.call_count = 0

    async def read_market_state(self) -> MarketState:
        self.call_count += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.state


class FakeMultiTF:
    def __init__(self):
        self.refresh_calls: list[tuple[str, int]] = []

    async def refresh(self, timeframe: str, count: int = 100) -> int:
        self.refresh_calls.append((timeframe, count))
        return 0

    def get_buffer(self, timeframe: str) -> Any:
        # Return a dummy buffer whose `last(n)` returns an empty list.
        return SimpleNamespace(last=lambda n=50: [])


class FakeRouter:
    def __init__(self, report: Optional[ExecutionReport] = None,
                 raise_exc: Optional[Exception] = None):
        self.calls: list[tuple[TradePlan, Optional[str]]] = []
        self.report = report or make_report()
        self.raise_exc = raise_exc

    def place(self, plan: TradePlan, inst_id: Optional[str] = None) -> ExecutionReport:
        self.calls.append((plan, inst_id))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.report


class FakeMonitor:
    def __init__(self):
        self.registered: list[tuple[str, str, float, float]] = []
        self.register_extras: list[dict] = []
        self.queued_fills: list[CloseFill] = []
        # 2026-04-26 — runner.poll() now returns (fills, live_snaps).
        # Tests that need the live-snap path push PositionSnapshot rows here;
        # default empty preserves existing test contracts.
        self.queued_live_snaps: list[PositionSnapshot] = []
        self.tracked_overrides: dict[tuple[str, str], object] = {}
        self.poll_count = 0
        self.revise_calls: list[tuple[str, str, float]] = []

    def register_open(self, inst_id: str, pos_side: str,
                      size: float, entry_price: float,
                      *, algo_ids: Optional[list[str]] = None,
                      tp2_price: Optional[float] = None,
                      be_already_moved: bool = False,
                      sl_price: float = 0.0,
                      runner_size: int = 0,
                      plan_sl_price: Optional[float] = None,
                      tp_limit_order_id: str = "") -> None:
        self.registered.append((inst_id, pos_side, size, entry_price))
        self.register_extras.append(
            {"algo_ids": list(algo_ids or []), "tp2_price": tp2_price,
             "be_already_moved": be_already_moved,
             "sl_price": sl_price, "runner_size": runner_size,
             "plan_sl_price": plan_sl_price,
             "tp_limit_order_id": tp_limit_order_id}
        )

    def poll(self, inst_id: Optional[str] = None) -> tuple[list[CloseFill], list[PositionSnapshot]]:
        self.poll_count += 1
        fills = self.queued_fills
        self.queued_fills = []
        live_snaps = self.queued_live_snaps
        self.queued_live_snaps = []
        return fills, live_snaps

    def get_tracked_runner(self, inst_id: str, pos_side: str):
        return None  # no-op: dynamic-TP gate exits early in tests

    def get_tracked(self, inst_id: str, pos_side: str):
        return self.tracked_overrides.get((inst_id, pos_side))

    def revise_runner_tp(self, inst_id: str, pos_side: str, new_tp: float) -> bool:
        self.revise_calls.append((inst_id, pos_side, new_tp))
        return True

    def poll_pending(self) -> list:
        # 2026-04-29 — runner.run_once now drains pending-limit events via
        # `_process_pending` → `monitor.poll_pending()`. Default empty
        # preserves the existing test contract (no pending events fired).
        # Tests that need the pending path push events into
        # `queued_pending_events` (initialised lazily here).
        out = getattr(self, "queued_pending_events", [])
        self.queued_pending_events = []
        return out


class FakeBybitClient:
    """Surface the runner touches: enrich_close_fill, get_positions,
    get_balance, set_position_tpsl, close_position, plus a few back-compat
    shims (cancel_algo) for tests written before the Bybit migration."""

    def __init__(self, positions: Optional[list[PositionSnapshot]] = None,
                 enrich_return: Optional[CloseFill] = None,
                 balance: float = 10_000.0,
                 tp_limit_place_raises: Optional[Exception] = None):
        self.positions = positions or []
        self.enrich_return = enrich_return
        self.balance = balance
        # Legacy name kept for back-compat with tests that asserted on it.
        # On Bybit there's no separate cancel_algo; this is just a record.
        self.cancel_algo_calls: list[tuple[str, str]] = []
        self.close_position_calls: list[tuple[str, str]] = []
        self.tp_limits_placed: list[dict] = []
        self.tp_limit_place_raises = tp_limit_place_raises
        self.set_position_tpsl_calls: list[dict] = []
        self.list_open_orders_return: list[dict] = []
        self._next_tp_limit_id = 0

    def get_positions(self, inst_id: Optional[str] = None) -> list[PositionSnapshot]:
        return list(self.positions)

    def enrich_close_fill(self, fill: CloseFill) -> CloseFill:
        return self.enrich_return if self.enrich_return is not None else fill

    def get_balance(self, ccy: str = "USDT") -> float:
        return self.balance

    def get_total_equity(self, ccy: str = "USDT") -> float:
        return self.balance

    def get_wallet_balance_realized(self, ccy: str = "USDT") -> float:
        # Tests overriding `balance` get the same value back; UPL-aware
        # behaviour is exercised in the bybit_client raw-response tests.
        return self.balance

    def cancel_algo(self, inst_id: str, algo_id: str) -> dict:
        self.cancel_algo_calls.append((inst_id, algo_id))
        return {}

    def set_position_tpsl(
        self, inst_id: str, pos_side: str,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        tpsl_mode: str = "Full",
        trigger_px_type: str = "mark",
    ) -> dict:
        self.set_position_tpsl_calls.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "take_profit": take_profit, "stop_loss": stop_loss,
            "tpsl_mode": tpsl_mode, "trigger_px_type": trigger_px_type,
        })
        return {}

    def list_open_orders(
        self, inst_id: Optional[str] = None,
        order_filter: str = "Order",
    ) -> list[dict]:
        return list(self.list_open_orders_return)

    def close_position(self, inst_id: str, pos_side: str,
                       td_mode: str = "isolated") -> dict:
        self.close_position_calls.append((inst_id, pos_side))
        return {}

    def place_reduce_only_limit(
        self,
        inst_id: str,
        pos_side: str,
        size_contracts: int,
        px: float,
        td_mode: str = "isolated",
        post_only: bool = True,
        client_order_id: Optional[str] = None,
    ):
        self.tp_limits_placed.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "size_contracts": size_contracts, "px": px,
            "td_mode": td_mode, "post_only": post_only,
        })
        if self.tp_limit_place_raises is not None:
            raise self.tp_limit_place_raises
        self._next_tp_limit_id += 1
        oid = f"TP_LIMIT_{self._next_tp_limit_id}"
        from src.execution.models import OrderResult, OrderStatus
        return OrderResult(
            order_id=oid, client_order_id=oid,
            status=OrderStatus.PENDING,
        )


# ── Composite helper ────────────────────────────────────────────────────────


@pytest.fixture
def make_ctx():
    """Factory returning a (BotContext, fakes-namespace) tuple per call.

    Callers can override any piece by passing kwargs:
      ctx, fakes = make_ctx(router=FakeRouter(raise_exc=AlgoOrderError("x")))
    """
    from src.bot.runner import BotContext
    from src.journal.database import TradeJournal
    from src.strategy.risk_manager import RiskManager

    def _factory(**overrides):
        cfg = overrides.pop("config", None) or make_config()
        reader = overrides.pop("reader", None) or FakeReader()
        multi_tf = overrides.pop("multi_tf", None) or FakeMultiTF()
        router = overrides.pop("router", None) or FakeRouter()
        monitor = overrides.pop("monitor", None) or FakeMonitor()
        bybit_client = overrides.pop("bybit_client", None) or FakeBybitClient()
        journal = overrides.pop("journal", None) or TradeJournal(":memory:")
        risk_mgr = overrides.pop("risk_mgr", None) or RiskManager(
            cfg.bot.starting_balance, cfg.breakers())
        ctx = BotContext(
            reader=reader, multi_tf=multi_tf, journal=journal,
            router=router, monitor=monitor, risk_mgr=risk_mgr,
            bybit_client=bybit_client, config=cfg,
        )
        fakes = SimpleNamespace(
            reader=reader, multi_tf=multi_tf, router=router,
            monitor=monitor, bybit_client=bybit_client,
            journal=journal, risk_mgr=risk_mgr, config=cfg,
        )
        return ctx, fakes

    return _factory
