"""Tests for src.execution.okx_client — envelope parsing + demo guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.execution.errors import (
    InsufficientMargin,
    LeverageSetError,
    OKXError,
    OrderRejected,
)
from src.execution.okx_client import OKXClient, OKXCredentials
from src.execution.models import OrderStatus


def _creds(demo: str = "1") -> OKXCredentials:
    return OKXCredentials(
        api_key="k", api_secret="s", passphrase="p", demo_flag=demo,
    )


class FakeTrade:
    def __init__(self):
        self.place_order_resp = {"code": "0", "data": [{"ordId": "42", "clOrdId": "cli42", "sCode": "0"}]}
        self.place_algo_resp = {"code": "0", "data": [{"algoId": "99", "algoClOrdId": "clalgo99"}]}
        self.cancel_algo_resp = {"code": "0", "data": [{}]}
        self.close_resp = {"code": "0", "data": [{}]}
        self.calls: list[tuple[str, dict]] = []

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        return self.place_order_resp

    def place_algo_order(self, **kw):
        self.calls.append(("place_algo_order", kw))
        return self.place_algo_resp

    def cancel_algo_order(self, orders):
        self.calls.append(("cancel_algo_order", {"orders": orders}))
        return self.cancel_algo_resp

    def close_positions(self, **kw):
        self.calls.append(("close_positions", kw))
        return self.close_resp


class FakeAccount:
    def __init__(self):
        self.set_lev_resp = {"code": "0", "data": [{}]}
        self.balance_resp = {"code": "0", "data": [
            {"details": [{"ccy": "USDT", "availEq": "1234.5", "eq": "1250.0"}]},
        ]}
        self.positions_resp = {"code": "0", "data": []}
        self.calls: list[tuple[str, dict]] = []

    def set_leverage(self, **kw):
        self.calls.append(("set_leverage", kw))
        return self.set_lev_resp

    def get_account_balance(self, **kw):
        self.calls.append(("get_balance", kw))
        return self.balance_resp

    def get_positions(self, **kw):
        self.calls.append(("get_positions", kw))
        return self.positions_resp


class FakeMarket:
    def __init__(self):
        self.mark_resp = {"code": "0", "data": [{"markPx": "67250.5"}]}

    def get_mark_price(self, **kw):
        return self.mark_resp


def _make_client(demo: str = "1", allow_live: bool = False):
    sdk = SimpleNamespace(trade=FakeTrade(), account=FakeAccount(), market=FakeMarket())
    return OKXClient(_creds(demo), allow_live=allow_live, sdk=sdk), sdk


# ── Demo guard ──────────────────────────────────────────────────────────────


def test_refuses_live_without_opt_in():
    with pytest.raises(RuntimeError, match="allow_live"):
        OKXClient(_creds(demo="0"), sdk=SimpleNamespace(
            trade=FakeTrade(), account=FakeAccount(), market=FakeMarket(),
        ))


def test_accepts_live_with_opt_in():
    client = OKXClient(_creds(demo="0"), allow_live=True, sdk=SimpleNamespace(
        trade=FakeTrade(), account=FakeAccount(), market=FakeMarket(),
    ))
    assert client.demo_flag == "0"


# ── Leverage ────────────────────────────────────────────────────────────────


def test_set_leverage_passes_through():
    client, sdk = _make_client()
    client.set_leverage("BTC-USDT-SWAP", 10, pos_side="long")
    name, kw = sdk.account.calls[-1]
    assert name == "set_leverage"
    assert kw["instId"] == "BTC-USDT-SWAP"
    assert kw["lever"] == "10"
    assert kw["mgnMode"] == "isolated"
    assert kw["posSide"] == "long"


def test_set_leverage_raises_typed_error_on_failure():
    client, sdk = _make_client()
    sdk.account.set_lev_resp = {"code": "51000", "msg": "bad param", "data": []}
    with pytest.raises(LeverageSetError):
        client.set_leverage("BTC-USDT-SWAP", 10)


# ── Place order ─────────────────────────────────────────────────────────────


def test_place_market_order_returns_pending_result():
    client, sdk = _make_client()
    res = client.place_market_order(
        "BTC-USDT-SWAP", side="buy", pos_side="long", size_contracts=3,
    )
    assert res.status == OrderStatus.PENDING
    assert res.order_id == "42"
    name, kw = sdk.trade.calls[-1]
    assert name == "place_order"
    assert kw["side"] == "buy"
    assert kw["posSide"] == "long"
    assert kw["ordType"] == "market"
    assert kw["sz"] == "3"
    assert kw["clOrdId"].startswith("smtbot")


def test_place_market_order_raises_insufficient_margin():
    client, sdk = _make_client()
    sdk.trade.place_order_resp = {
        "code": "1", "msg": "insufficient",
        "data": [{"sCode": "51008", "sMsg": "insufficient balance"}],
    }
    with pytest.raises(InsufficientMargin):
        client.place_market_order("BTC-USDT-SWAP", "buy", "long", 1)


def test_place_market_order_raises_rejected_on_other_error():
    client, sdk = _make_client()
    sdk.trade.place_order_resp = {
        "code": "1", "msg": "bad",
        "data": [{"sCode": "51400", "sMsg": "price out of range"}],
    }
    with pytest.raises(OrderRejected):
        client.place_market_order("BTC-USDT-SWAP", "buy", "long", 1)


# ── OCO algo ────────────────────────────────────────────────────────────────


def test_place_oco_algo_long_sends_sell_side():
    client, sdk = _make_client()
    res = client.place_oco_algo(
        "BTC-USDT-SWAP", pos_side="long", size_contracts=2,
        sl_trigger_px=60000.0, tp_trigger_px=70000.0,
    )
    assert res.algo_id == "99"
    name, kw = sdk.trade.calls[-1]
    assert name == "place_algo_order"
    assert kw["side"] == "sell"
    assert kw["ordType"] == "oco"
    assert kw["slTriggerPx"] == "60000.0"
    assert kw["tpTriggerPx"] == "70000.0"
    assert kw["slOrdPx"] == "-1" and kw["tpOrdPx"] == "-1"


def test_place_oco_algo_short_sends_buy_side():
    client, sdk = _make_client()
    client.place_oco_algo(
        "BTC-USDT-SWAP", pos_side="short", size_contracts=1,
        sl_trigger_px=71000.0, tp_trigger_px=65000.0,
    )
    _, kw = sdk.trade.calls[-1]
    assert kw["side"] == "buy"


# ── Balance & positions ─────────────────────────────────────────────────────


def test_balance_extracts_usdt():
    client, _ = _make_client()
    assert client.get_balance("USDT") == pytest.approx(1234.5)


def test_get_positions_filters_empty_rows():
    client, sdk = _make_client()
    sdk.account.positions_resp = {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": "3",
         "avgPx": "67000", "markPx": "67100", "upl": "3.0", "lever": "10"},
        {"instId": "", "pos": "0"},   # empty row → skipped
    ]}
    snaps = client.get_positions("BTC-USDT-SWAP")
    assert len(snaps) == 1
    assert snaps[0].size == 3.0
    assert snaps[0].leverage == 10
    assert snaps[0].is_closed is False


def test_get_positions_raises_on_envelope_error():
    client, sdk = _make_client()
    sdk.account.positions_resp = {"code": "1", "msg": "boom", "data": []}
    with pytest.raises(OKXError):
        client.get_positions()


# ── Mark price ──────────────────────────────────────────────────────────────


def test_get_mark_price_parses_float():
    client, _ = _make_client()
    assert client.get_mark_price("BTC-USDT-SWAP") == pytest.approx(67250.5)


# ── TP resting limit (2026-04-20 maker-TP alongside OCO) ───────────────────


def test_place_reduce_only_limit_long_sends_sell_side_post_only():
    client, sdk = _make_client()
    res = client.place_reduce_only_limit(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        size_contracts=3, px=73000.0, td_mode="cross",
    )
    assert res.order_id == "42"
    name, kw = sdk.trade.calls[-1]
    assert name == "place_order"
    assert kw["instId"] == "BTC-USDT-SWAP"
    assert kw["side"] == "sell"
    assert kw["posSide"] == "long"
    assert kw["ordType"] == "post_only"
    assert kw["reduceOnly"] is True
    assert kw["sz"] == "3"
    assert kw["px"] == "73000.0"
    assert kw["tdMode"] == "cross"
    assert kw["clOrdId"].startswith("smttp")


def test_place_reduce_only_limit_short_sends_buy_side():
    client, sdk = _make_client()
    client.place_reduce_only_limit(
        inst_id="ETH-USDT-SWAP", pos_side="short",
        size_contracts=10, px=2200.5,
    )
    _, kw = sdk.trade.calls[-1]
    assert kw["side"] == "buy"
    assert kw["posSide"] == "short"


def test_place_reduce_only_limit_plain_limit_when_post_only_false():
    client, sdk = _make_client()
    client.place_reduce_only_limit(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        size_contracts=1, px=73000.0, post_only=False,
    )
    _, kw = sdk.trade.calls[-1]
    assert kw["ordType"] == "limit"


def test_place_reduce_only_limit_uses_caller_client_order_id():
    client, sdk = _make_client()
    client.place_reduce_only_limit(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        size_contracts=1, px=73000.0,
        client_order_id="custom_cli_123",
    )
    _, kw = sdk.trade.calls[-1]
    assert kw["clOrdId"] == "custom_cli_123"


def test_place_reduce_only_limit_raises_on_envelope_error():
    client, sdk = _make_client()
    sdk.trade.place_order_resp = {
        "code": "1",
        "msg": "post_only would take liquidity",
        "data": [{"sCode": "51124", "sMsg": "post_only reject", "ordId": ""}],
    }
    with pytest.raises(OrderRejected) as exc:
        client.place_reduce_only_limit(
            inst_id="BTC-USDT-SWAP", pos_side="long",
            size_contracts=1, px=73000.0,
        )
    assert exc.value.code == "51124"
