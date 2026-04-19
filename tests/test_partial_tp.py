"""Router-layer partial TP placement (Madde E, router path).

Covers `OrderRouter._place_algos` end-to-end: partial mode places two
OCOs with split sizes and distinct TPs; degenerate size falls back to
single. Also verifies `ExecutionReport.algos` carries both results.
"""

from __future__ import annotations

import pytest

from src.data.models import Direction
from src.execution.models import AlgoResult, OrderResult, OrderStatus
from src.execution.order_router import OrderRouter, RouterConfig
from src.strategy.trade_plan import TradePlan


def _plan(num_contracts: int = 10, direction: Direction = Direction.BULLISH) -> TradePlan:
    entry = 100.0
    sl = 99.0 if direction == Direction.BULLISH else 101.0
    tp = 103.0 if direction == Direction.BULLISH else 97.0
    return TradePlan(
        direction=direction,
        entry_price=entry, sl_price=sl, tp_price=tp,
        rr_ratio=3.0, sl_distance=abs(entry - sl), sl_pct=0.01,
        position_size_usdt=1000.0,
        leverage=10, required_leverage=10.0,
        num_contracts=num_contracts,
        risk_amount_usdt=10.0, max_risk_usdt=10.0, capped=False,
    )


class _FakeClient:
    def __init__(self):
        self.algo_calls: list[dict] = []

    def set_leverage(self, **kw):
        return {}

    def place_market_order(self, **kw):
        return OrderResult(
            order_id="E1", client_order_id="cliE1", status=OrderStatus.PENDING,
        )

    def place_oco_algo(self, *, inst_id, pos_side, size_contracts,
                       sl_trigger_px, tp_trigger_px, td_mode="isolated",
                       trigger_px_type=""):
        self.algo_calls.append({
            "size": size_contracts, "sl": sl_trigger_px, "tp": tp_trigger_px,
            "trigger_px_type": trigger_px_type,
        })
        return AlgoResult(
            algo_id=f"A{len(self.algo_calls)}",
            client_algo_id=f"cliA{len(self.algo_calls)}",
            sl_trigger_px=sl_trigger_px, tp_trigger_px=tp_trigger_px,
        )

    def close_position(self, *a, **kw):
        return {}


# ── Two-algo placement ─────────────────────────────────────────────────────


def test_partial_mode_places_two_algos():
    client = _FakeClient()
    router = OrderRouter(client, RouterConfig(
        partial_tp_enabled=True, partial_tp_ratio=0.5, partial_tp_rr=1.5,
    ))
    report = router.place(_plan(num_contracts=10))
    assert len(client.algo_calls) == 2
    # Sizes split 50/50 and sum to full
    assert client.algo_calls[0]["size"] == 5
    assert client.algo_calls[1]["size"] == 5
    # TP1 at 1.5R = 100 + 1 * 1.5 = 101.5; TP2 at plan.tp = 103.0
    assert client.algo_calls[0]["tp"] == pytest.approx(101.5)
    assert client.algo_calls[1]["tp"] == pytest.approx(103.0)
    # Both algos surfaced on the report
    assert len(report.algos) == 2
    assert report.algo is report.algos[0]


def test_partial_mode_ratio_truncation():
    # 7 × 0.5 = 3 (int floor), remainder 4 — total still 7.
    client = _FakeClient()
    router = OrderRouter(client, RouterConfig(
        partial_tp_enabled=True, partial_tp_ratio=0.5, partial_tp_rr=1.5,
    ))
    router.place(_plan(num_contracts=7))
    assert len(client.algo_calls) == 2
    assert client.algo_calls[0]["size"] == 3
    assert client.algo_calls[1]["size"] == 4


def test_partial_single_contract_raises_loudly():
    # With num_contracts=1, size1 rounds to 0. entry_signals is supposed to
    # reject these plans up-front; if one still reaches the router we fail
    # loud rather than silently placing a single OCO (which would break the
    # TP1/TP2 guarantee the operator expects on every trade).
    from src.execution.errors import AlgoOrderError
    client = _FakeClient()
    router = OrderRouter(client, RouterConfig(
        partial_tp_enabled=True, partial_tp_ratio=0.5, partial_tp_rr=1.5,
        close_on_algo_failure=False,
    ))
    with pytest.raises(AlgoOrderError, match="insufficient_contracts_for_split"):
        router.place(_plan(num_contracts=1))


# ── Disabled path still works ──────────────────────────────────────────────


def test_partial_disabled_places_single_algo():
    client = _FakeClient()
    router = OrderRouter(client, RouterConfig(partial_tp_enabled=False))
    report = router.place(_plan(num_contracts=10))
    assert len(client.algo_calls) == 1
    assert client.algo_calls[0]["size"] == 10
    assert len(report.algos) == 1


# ── Bearish path uses correct TP1 direction ────────────────────────────────


def test_partial_mode_bearish_tp1_direction():
    # Short: entry=100, sl=101, sl_dist=1. TP1 = 100 - 1*1.5 = 98.5.
    client = _FakeClient()
    router = OrderRouter(client, RouterConfig(
        partial_tp_enabled=True, partial_tp_ratio=0.5, partial_tp_rr=1.5,
    ))
    router.place(_plan(num_contracts=10, direction=Direction.BEARISH))
    assert client.algo_calls[0]["tp"] == pytest.approx(98.5)
    assert client.algo_calls[1]["tp"] == pytest.approx(97.0)


# ── Weighted-reward invariant (YAML contract) ──────────────────────────────


def test_default_yaml_runner_tp_is_hard_1_3():
    """Guard: config/default.yaml must enforce a hard 1:3 RR on the runner
    leg via `execution.target_rr_ratio`. The pre-2026-04-19 contract was a
    weighted 3.0R via the partial split (1.5R + 4.5R = avg 3.0R); operator
    log on 2026-04-19 showed the heatmap-cluster TP source drove the runner
    to 8-12R despite the 4.5R config knob, so the contract was rewritten:
    every setup is `entry ± target_rr_ratio × sl_distance`, not a weighted
    average.

    Tune `target_rr_ratio` if the desired runner RR changes; keep
    `default_rr_ratio` (entry_signals path fallback) aligned for non-zone
    code paths.
    """
    import pathlib
    import yaml

    cfg_path = pathlib.Path(__file__).parent.parent / "config" / "default.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    target_rr = float(raw["execution"]["target_rr_ratio"])
    default_rr = float(raw["trading"]["default_rr_ratio"])
    assert target_rr == pytest.approx(3.0), (
        f"execution.target_rr_ratio = {target_rr}, expected 3.0 — runner TP "
        "must enforce hard 1:3 RR per CLAUDE.md changelog 2026-04-19."
    )
    assert default_rr == pytest.approx(target_rr), (
        f"trading.default_rr_ratio ({default_rr}) drifted from "
        f"execution.target_rr_ratio ({target_rr}). Keep them aligned so the "
        "entry_signals fallback path also enforces 1:3 when zones aren't used."
    )
