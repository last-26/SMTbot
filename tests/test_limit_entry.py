"""Phase 7.C2 — limit-entry primitives on OKXClient + OrderRouter.

Adds `place_limit_order` / `cancel_order` to the OKX wrapper and
`place_limit_entry` / `cancel_pending_entry` to the router. The router
preserves the existing market-path leverage set-up before placing, and
handles the post-only-rejected → regular-limit fallback without re-setting
leverage (idempotent at OKX side; no need to spam the call).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.data.models import Direction
from src.execution.errors import OrderRejected
from src.execution.okx_client import OKXClient, OKXCredentials
from src.execution.order_router import OrderRouter, RouterConfig
from src.execution.models import OrderStatus
from src.strategy.trade_plan import TradePlan


# ── Shared fakes ────────────────────────────────────────────────────────────


class FakeTrade:
    def __init__(self):
        self.place_order_resp = {
            "code": "0",
            "data": [{"ordId": "LIM-1", "clOrdId": "cli-lim", "sCode": "0"}],
        }
        self.cancel_order_resp = {
            "code": "0", "data": [{"ordId": "LIM-1", "sCode": "0"}],
        }
        self.calls: list[tuple[str, dict]] = []
        self._post_only_should_fail = False

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        if self._post_only_should_fail and kw.get("ordType") == "post_only":
            return {
                "code": "1",
                "msg": "post_only_would_take_liquidity",
                "data": [{"sCode": "51124", "sMsg": "post-only rejected"}],
            }
        return self.place_order_resp

    def cancel_order(self, **kw):
        self.calls.append(("cancel_order", kw))
        return self.cancel_order_resp


class FakeAccount:
    def __init__(self):
        self.set_lev_resp = {"code": "0", "data": [{}]}
        self.calls: list[tuple[str, dict]] = []

    def set_leverage(self, **kw):
        self.calls.append(("set_leverage", kw))
        return self.set_lev_resp


class FakeMarket:
    pass


def _make_client() -> tuple[OKXClient, FakeTrade, FakeAccount]:
    trade, account, market = FakeTrade(), FakeAccount(), FakeMarket()
    sdk = SimpleNamespace(trade=trade, account=account, market=market, public=market)
    client = OKXClient(
        OKXCredentials(api_key="k", api_secret="s", passphrase="p", demo_flag="1"),
        sdk=sdk,
    )
    return client, trade, account


def _plan(num_contracts: int = 5, direction: Direction = Direction.BULLISH) -> TradePlan:
    return TradePlan(
        direction=direction, entry_price=100.0, sl_price=99.0, tp_price=102.0,
        rr_ratio=2.0, sl_distance=1.0, sl_pct=0.01,
        position_size_usdt=1_000.0, leverage=10, required_leverage=10.0,
        num_contracts=num_contracts, risk_amount_usdt=10.0, max_risk_usdt=10.0,
        capped=False, sl_source="test", confluence_score=3.0,
        confluence_factors=[], reason="test",
    )


# ── OKXClient ───────────────────────────────────────────────────────────────


def test_place_limit_order_sends_px_and_post_only():
    client, trade, _ = _make_client()
    result = client.place_limit_order(
        inst_id="BTC-USDT-SWAP", side="buy", pos_side="long",
        size_contracts=3, px=100.5, ord_type="post_only",
    )
    assert result.status == OrderStatus.PENDING
    assert result.order_id == "LIM-1"
    _, kw = trade.calls[-1]
    assert kw["ordType"] == "post_only"
    assert kw["px"] == "100.5"
    assert kw["sz"] == "3"


def test_place_limit_order_plain_limit_ord_type():
    client, trade, _ = _make_client()
    client.place_limit_order(
        inst_id="BTC-USDT-SWAP", side="sell", pos_side="short",
        size_contracts=2, px=101.0, ord_type="limit",
    )
    _, kw = trade.calls[-1]
    assert kw["ordType"] == "limit"


def test_cancel_order_routes_ord_id():
    client, trade, _ = _make_client()
    client.cancel_order("BTC-USDT-SWAP", "LIM-1")
    kind, kw = trade.calls[-1]
    assert kind == "cancel_order"
    assert kw == {"instId": "BTC-USDT-SWAP", "ordId": "LIM-1"}


# ── OrderRouter — happy path ────────────────────────────────────────────────


def test_place_limit_entry_sets_leverage_then_places_limit():
    client, trade, account = _make_client()
    router = OrderRouter(client, RouterConfig(inst_id="BTC-USDT-SWAP"))

    result = router.place_limit_entry(_plan(), entry_px=100.5)

    assert result.status == OrderStatus.PENDING
    assert len(account.calls) == 1            # leverage set exactly once
    assert len(trade.calls) == 1              # one place_order call
    _, lev_kw = account.calls[-1]
    assert lev_kw["lever"] == "10"
    _, ord_kw = trade.calls[-1]
    assert ord_kw["ordType"] == "post_only"
    assert ord_kw["px"] == "100.5"


def test_place_limit_entry_rejects_zero_contracts():
    client, _, _ = _make_client()
    router = OrderRouter(client, RouterConfig())
    with pytest.raises(ValueError):
        router.place_limit_entry(_plan(num_contracts=0), entry_px=100.0)


# ── OrderRouter — post-only fallback ────────────────────────────────────────


def test_post_only_rejection_falls_back_to_limit():
    """When post-only is rejected (would cross spread), router retries as
    a plain limit — leverage was already set, no second set_leverage call.
    """
    client, trade, account = _make_client()
    trade._post_only_should_fail = True
    router = OrderRouter(client, RouterConfig(inst_id="BTC-USDT-SWAP"))

    result = router.place_limit_entry(_plan(), entry_px=100.5)

    assert result.status == OrderStatus.PENDING
    assert len(account.calls) == 1            # leverage set once, not twice
    # Two place_order calls: first post_only (rejected), second limit (ok)
    ord_types = [kw["ordType"] for k, kw in trade.calls if k == "place_order"]
    assert ord_types == ["post_only", "limit"]


def test_post_only_rejection_can_be_strict():
    """fallback_to_limit=False → OrderRejected propagates (strict maker)."""
    client, trade, _ = _make_client()
    trade._post_only_should_fail = True
    router = OrderRouter(client, RouterConfig())
    with pytest.raises(OrderRejected):
        router.place_limit_entry(
            _plan(), entry_px=100.5, fallback_to_limit=False,
        )


def test_non_post_only_rejection_is_not_retried():
    """If caller asked for ord_type='limit' and OKX rejects, no retry —
    there's nothing to fall back to, so the error propagates as-is."""
    client, trade, _ = _make_client()
    # Make every place_order fail regardless of ordType.
    trade.place_order_resp = {
        "code": "1", "msg": "nope",
        "data": [{"sCode": "51999", "sMsg": "generic rejection"}],
    }
    router = OrderRouter(client, RouterConfig())
    with pytest.raises(OrderRejected):
        router.place_limit_entry(_plan(), entry_px=100.5, ord_type="limit")


# ── OrderRouter — cancel ────────────────────────────────────────────────────


def test_cancel_pending_entry_routes_to_client():
    client, trade, _ = _make_client()
    router = OrderRouter(client, RouterConfig(inst_id="BTC-USDT-SWAP"))
    router.cancel_pending_entry("LIM-1")
    kind, kw = trade.calls[-1]
    assert kind == "cancel_order"
    assert kw["instId"] == "BTC-USDT-SWAP"
    assert kw["ordId"] == "LIM-1"
