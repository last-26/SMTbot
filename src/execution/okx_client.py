"""Thin, typed wrapper around python-okx (0.4.x).

Goals:
  - One place where the demo/live flag gets applied (so we can't leak
    live-trading calls from anywhere else in the codebase).
  - Convert OKX's envelope-style responses (`{"code": "0", "msg": "", "data": [...]}`)
    into typed records — or raise OKXError / OrderRejected / InsufficientMargin
    so upstream code never has to pattern-match on magic code strings.
  - Synchronous-facing API. python-okx is sync; the bot's outer loop is
    async. Routers call these inside `asyncio.to_thread(...)` to stay
    non-blocking without re-implementing the SDK.

Out of scope:
  - Websocket streaming (Phase 4.5 if needed; REST polling is fine for MVP).
  - Retry/backoff — callers decide their retry policy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from src.execution.errors import (
    InsufficientMargin,
    LeverageSetError,
    OKXError,
    OrderRejected,
)
from src.execution.models import (
    AlgoResult,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
)

# Codes that map to "your margin/funds aren't enough to place this order".
# Source: OKX V5 error code reference. Kept small on purpose — we expand as
# the bot actually hits other failure modes in demo.
_INSUFFICIENT_MARGIN_CODES = {"51008", "51020", "51200", "51201"}


@dataclass
class OKXCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    demo_flag: str = "1"  # "1" = demo, "0" = live

    def assert_demo(self) -> None:
        if self.demo_flag != "1":
            raise RuntimeError(
                "Live trading flag set; refuse to run unless the caller "
                "explicitly opts in via OKXClient(allow_live=True)."
            )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _check(resp: dict, context: str) -> dict:
    """Validate an OKX envelope and return its `data[0]`.

    Raises OKXError / OrderRejected / InsufficientMargin with the upstream
    code + message so the router can log or branch.
    """
    if not isinstance(resp, dict):
        raise OKXError(f"{context}: unexpected response type {type(resp).__name__}", payload={"raw": resp})
    code = str(resp.get("code", ""))
    if code == "0":
        data = resp.get("data") or [{}]
        return data[0] if data else {}

    msg = resp.get("msg") or "(no message)"
    # Data may carry per-order codes too — bubble them up when present.
    data = resp.get("data") or []
    inner_code = str(data[0].get("sCode", "")) if data else ""
    final_code = inner_code or code

    if final_code in _INSUFFICIENT_MARGIN_CODES:
        raise InsufficientMargin(f"{context}: {msg}", code=final_code, payload=resp)
    if data:
        raise OrderRejected(f"{context}: {msg}", code=final_code, payload=resp)
    raise OKXError(f"{context}: {msg}", code=final_code, payload=resp)


# ── Client ──────────────────────────────────────────────────────────────────


class OKXClient:
    """Concrete OKX REST client. Inject a fake SDK via `sdk=` for tests."""

    def __init__(
        self,
        credentials: OKXCredentials,
        allow_live: bool = False,
        sdk: Any = None,
    ):
        if credentials.demo_flag != "1" and not allow_live:
            raise RuntimeError(
                "Refusing to construct OKXClient with demo_flag != '1' "
                "unless allow_live=True is passed explicitly."
            )
        self.credentials = credentials
        self.demo_flag = credentials.demo_flag

        if sdk is None:
            import okx.Account as Account
            import okx.MarketData as MarketData
            import okx.Trade as Trade
            self.trade = Trade.TradeAPI(
                credentials.api_key, credentials.api_secret, credentials.passphrase,
                False, credentials.demo_flag,
            )
            self.account = Account.AccountAPI(
                credentials.api_key, credentials.api_secret, credentials.passphrase,
                False, credentials.demo_flag,
            )
            self.market = MarketData.MarketAPI(flag=credentials.demo_flag)
        else:
            # Test injection path: sdk.trade / sdk.account / sdk.market
            self.trade = sdk.trade
            self.account = sdk.account
            self.market = sdk.market

    # ── Account ─────────────────────────────────────────────────────────────

    def set_leverage(self, inst_id: str, leverage: int, mgn_mode: str = "isolated",
                     pos_side: Optional[str] = None) -> dict:
        """Call /api/v5/account/set-leverage."""
        kwargs = {"instId": inst_id, "lever": str(leverage), "mgnMode": mgn_mode}
        if pos_side:
            kwargs["posSide"] = pos_side
        resp = self.account.set_leverage(**kwargs)
        try:
            return _check(resp, "set_leverage")
        except OKXError as e:
            raise LeverageSetError(str(e), code=e.code, payload=e.payload) from e

    def get_balance(self, ccy: str = "USDT") -> float:
        resp = self.account.get_account_balance(ccy=ccy)
        data = _check(resp, "get_balance")
        # OKX returns nested {details: [{availEq, eq, ...}]}
        details = data.get("details", [])
        for d in details:
            if d.get("ccy") == ccy:
                return float(d.get("availEq") or d.get("eq") or 0.0)
        return 0.0

    # ── Market ──────────────────────────────────────────────────────────────

    def get_mark_price(self, inst_id: str) -> float:
        resp = self.market.get_mark_price(instType="SWAP", instId=inst_id)
        data = _check(resp, "get_mark_price")
        return float(data.get("markPx") or 0.0)

    # ── Orders ──────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        inst_id: str,
        side: str,                 # "buy" / "sell"
        pos_side: str,             # "long" / "short"
        size_contracts: int,
        td_mode: str = "isolated",
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        cl_ord_id = client_order_id or f"smtbot{uuid.uuid4().hex[:20]}"
        resp = self.trade.place_order(
            instId=inst_id, tdMode=td_mode,
            side=side, posSide=pos_side, ordType="market",
            sz=str(size_contracts), clOrdId=cl_ord_id,
        )
        data = _check(resp, "place_order")
        return OrderResult(
            order_id=str(data.get("ordId", "")),
            client_order_id=str(data.get("clOrdId", cl_ord_id)),
            status=OrderStatus.PENDING,
            raw=resp,
        )

    def place_oco_algo(
        self,
        inst_id: str,
        pos_side: str,
        size_contracts: int,
        sl_trigger_px: float,
        tp_trigger_px: float,
        td_mode: str = "isolated",
        client_algo_id: Optional[str] = None,
    ) -> AlgoResult:
        """Place an OCO SL/TP algo on the position just opened.

        `side` of the algo is the OPPOSITE of the entry side — closing long
        means selling, closing short means buying. OKX uses ordPx=-1 to
        signal "market" when the trigger fires.
        """
        closing_side = "sell" if pos_side == "long" else "buy"
        cl_algo_id = client_algo_id or f"smtalgo{uuid.uuid4().hex[:20]}"
        resp = self.trade.place_algo_order(
            instId=inst_id, tdMode=td_mode,
            side=closing_side, posSide=pos_side, ordType="oco",
            sz=str(size_contracts),
            slTriggerPx=str(sl_trigger_px), slOrdPx="-1",
            tpTriggerPx=str(tp_trigger_px), tpOrdPx="-1",
            algoClOrdId=cl_algo_id,
        )
        data = _check(resp, "place_algo_order")
        return AlgoResult(
            algo_id=str(data.get("algoId", "")),
            client_algo_id=str(data.get("algoClOrdId", cl_algo_id)),
            sl_trigger_px=sl_trigger_px,
            tp_trigger_px=tp_trigger_px,
            raw=resp,
        )

    def cancel_algo(self, inst_id: str, algo_id: str) -> dict:
        resp = self.trade.cancel_algo_order([
            {"instId": inst_id, "algoId": algo_id},
        ])
        return _check(resp, "cancel_algo_order")

    def close_position(self, inst_id: str, pos_side: str, td_mode: str = "isolated") -> dict:
        resp = self.trade.close_positions(
            instId=inst_id, mgnMode=td_mode, posSide=pos_side,
        )
        return _check(resp, "close_positions")

    # ── Positions ───────────────────────────────────────────────────────────

    def get_positions(self, inst_id: Optional[str] = None) -> list[PositionSnapshot]:
        kwargs: dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            kwargs["instId"] = inst_id
        resp = self.account.get_positions(**kwargs)
        if str(resp.get("code", "")) != "0":
            raise OKXError(f"get_positions: {resp.get('msg')}", payload=resp)
        snapshots = []
        for row in resp.get("data", []) or []:
            if not row.get("instId"):
                continue
            pos = float(row.get("pos") or 0.0)
            snapshots.append(PositionSnapshot(
                inst_id=row["instId"],
                pos_side=row.get("posSide", ""),
                size=pos,
                entry_price=float(row.get("avgPx") or 0.0),
                mark_price=float(row.get("markPx") or 0.0),
                unrealized_pnl=float(row.get("upl") or 0.0),
                leverage=int(float(row.get("lever") or 0)),
            ))
        return snapshots
