"""Tests for src.execution.order_router — TradePlan → Bybit flow."""

from __future__ import annotations

import pytest

from src.data.models import Direction
from src.execution.errors import AlgoOrderError, LeverageSetError
from src.execution.models import AlgoResult, OrderResult, OrderStatus, PositionState
from src.execution.order_router import OrderRouter, RouterConfig, dry_run_report
from src.strategy.trade_plan import TradePlan


def _plan(direction: Direction = Direction.BULLISH) -> TradePlan:
    return TradePlan(
        direction=direction,
        entry_price=100.0,
        sl_price=99.0 if direction == Direction.BULLISH else 101.0,
        tp_price=103.0 if direction == Direction.BULLISH else 97.0,
        rr_ratio=3.0, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=1000.0,
        leverage=10, required_leverage=10.0,
        num_contracts=5,
        risk_amount_usdt=10.0, max_risk_usdt=10.0, capped=False,
    )


class FakeClient:
    def __init__(self, *, algo_fails: bool = False, leverage_fails: bool = False):
        self.algo_fails = algo_fails
        self.leverage_fails = leverage_fails
        self.calls: list[tuple[str, dict]] = []
        self.close_called = False

    def set_leverage(self, inst_id: str, leverage: int, mgn_mode: str, pos_side=None):
        self.calls.append(("set_leverage", {
            "inst_id": inst_id, "leverage": leverage, "mgn_mode": mgn_mode, "pos_side": pos_side,
        }))
        if self.leverage_fails:
            raise LeverageSetError("boom")
        return {}

    def place_market_order(self, inst_id, side, pos_side, size_contracts, td_mode="isolated"):
        self.calls.append(("place_market_order", {
            "inst_id": inst_id, "side": side, "pos_side": pos_side,
            "size": size_contracts, "td_mode": td_mode,
        }))
        return OrderResult(
            order_id="E1", client_order_id="cliE1",
            status=OrderStatus.PENDING,
        )

    def place_oco_algo(self, inst_id, pos_side, size_contracts, sl_trigger_px, tp_trigger_px,
                       td_mode="isolated", trigger_px_type=""):
        self.calls.append(("place_oco_algo", {
            "inst_id": inst_id, "pos_side": pos_side, "size": size_contracts,
            "sl": sl_trigger_px, "tp": tp_trigger_px,
            "trigger_px_type": trigger_px_type,
        }))
        if self.algo_fails:
            raise RuntimeError("algo rejected")
        return AlgoResult(
            algo_id="A1", client_algo_id="cliA1",
            sl_trigger_px=sl_trigger_px, tp_trigger_px=tp_trigger_px,
        )

    def close_position(self, inst_id, pos_side, td_mode="isolated"):
        self.close_called = True
        return {}


# ── Happy path ──────────────────────────────────────────────────────────────


def test_place_bullish_plan_produces_open_report():
    client = FakeClient()
    router = OrderRouter(client)
    report = router.place(_plan(Direction.BULLISH))
    assert report.state == PositionState.OPEN
    assert report.leverage_set
    assert report.is_protected
    # Order of calls: set_leverage → place_market_order → place_oco_algo
    assert [c[0] for c in client.calls] == [
        "set_leverage", "place_market_order", "place_oco_algo",
    ]


def test_bullish_places_buy_entry_and_sell_algo_side():
    client = FakeClient()
    OrderRouter(client).place(_plan(Direction.BULLISH))
    _, entry = client.calls[1]
    assert entry["side"] == "buy"
    assert entry["pos_side"] == "long"


def test_bearish_places_sell_entry():
    client = FakeClient()
    OrderRouter(client).place(_plan(Direction.BEARISH))
    _, entry = client.calls[1]
    assert entry["side"] == "sell"
    assert entry["pos_side"] == "short"


def test_algo_receives_plan_sl_and_tp():
    client = FakeClient()
    OrderRouter(client).place(_plan(Direction.BULLISH))
    _, algo = client.calls[2]
    assert algo["sl"] == 99.0
    assert algo["tp"] == 103.0
    assert algo["size"] == 5


# ── Failure modes ───────────────────────────────────────────────────────────


def test_leverage_failure_aborts_before_entry():
    client = FakeClient(leverage_fails=True)
    with pytest.raises(LeverageSetError):
        OrderRouter(client).place(_plan())
    # Only the leverage call happened — no order, no algo.
    assert [c[0] for c in client.calls] == ["set_leverage"]


def test_algo_failure_raises_and_auto_closes_position():
    client = FakeClient(algo_fails=True)
    router = OrderRouter(client, RouterConfig(close_on_algo_failure=True))
    with pytest.raises(AlgoOrderError):
        router.place(_plan())
    assert client.close_called is True


def test_algo_failure_without_auto_close_leaves_position():
    client = FakeClient(algo_fails=True)
    router = OrderRouter(client, RouterConfig(close_on_algo_failure=False))
    with pytest.raises(AlgoOrderError):
        router.place(_plan())
    assert client.close_called is False


def test_zero_contracts_refused():
    client = FakeClient()
    bad = _plan()
    bad.num_contracts = 0
    with pytest.raises(ValueError, match="num_contracts"):
        OrderRouter(client).place(bad)


def test_undefined_direction_refused():
    client = FakeClient()
    bad = _plan()
    bad.direction = Direction.UNDEFINED
    with pytest.raises(ValueError):
        OrderRouter(client).place(bad)


# ── Dry run ─────────────────────────────────────────────────────────────────


def test_dry_run_report_mirrors_plan():
    plan = _plan()
    report = dry_run_report(plan)
    assert report.state == PositionState.OPEN
    assert report.entry.avg_price == plan.entry_price
    assert report.algo.sl_trigger_px == plan.sl_price
    assert report.algo.tp_trigger_px == plan.tp_price
    assert report.entry.filled_sz == float(plan.num_contracts)
