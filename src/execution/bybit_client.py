"""Thin, typed wrapper around pybit (unified_trading.HTTP, V5 API).

Goals:
  - One place where the demo/live flag gets applied (so we can't leak
    live-trading calls from anywhere else in the codebase).
  - Convert Bybit's envelope-style responses (`{"retCode": 0, "retMsg":
    "OK", "result": {...}}`) into typed records — or raise BybitError /
    OrderRejected / InsufficientMargin so upstream code never has to
    pattern-match on magic code strings.
  - Synchronous-facing API. pybit is sync; the bot's outer loop is
    async. Routers call these inside `asyncio.to_thread(...)` to stay
    non-blocking without re-implementing the SDK.

Bybit V5 architectural notes (vs the pre-migration wrapper):
  - **TP/SL is a property of the position**, attached at order placement
    via `takeProfit`/`stopLoss` fields on POST /v5/order/create OR
    mutated post-fill via POST /v5/position/trading-stop. There is NO
    separate algo order to cancel/replace. The pre-migration
    `place_oco_algo` / `cancel_algo` / `list_pending_algos` surface
    was replaced by a single `set_position_tpsl()` method (the back-compat
    shims were removed in the 2026-04-26 post-migration cleanup, Phase 7).
  - **Symbol format** is `BTCUSDT` (linear perp). Internally we still
    call the parameter `inst_id` to keep journal column names + dataclass
    field names stable across the migration; the value is just a Bybit
    symbol string now.
  - **Position mode** is hedge (`mode=3`) set once at startup; each
    position carries a `positionIdx` (1=long, 2=short) instead of
    the pre-migration `posSide`.
  - **Account type** is UNIFIED. Margin mode is account-wide
    (REGULAR_MARGIN ≈ cross), not per-call `tdMode`.

Out of scope:
  - Websocket streaming. REST polling is fine for MVP demo flow.
  - Retry/backoff — callers decide their retry policy.
"""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Any, Optional

from loguru import logger

from src.execution.errors import (
    BybitError,
    InsufficientMargin,
    LeverageSetError,
    OrderRejected,
)
from src.execution.models import (
    AlgoResult,
    CloseFill,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
)

# Bybit V5 retCodes that map to "your margin/funds aren't enough".
# Source: https://bybit-exchange.github.io/docs/v5/error
_INSUFFICIENT_MARGIN_CODES = frozenset({"110004", "110007", "110012"})

# Codes returned when the order/leverage call hits a parameter or business-rule
# rejection — surface as OrderRejected so the router's post-only fallback path
# can react. Includes the "post-only would cross" code (170218).
_ORDER_REJECTED_CODES = frozenset({
    "110003",   # Order price exceeds allowable range
    "110017",   # Position mode not allowed (hedge not enabled)
    "110021",   # Exceeded position limits due to Open Interest
    "170218",   # LIMIT-MAKER order rejected (post-only would take liquidity)
})

# Internal trigger-px-type → Bybit equivalent. The strategy/config layer still
# speaks the old vocabulary (`mark` / `last`) for back-compat.
_TRIGGER_PX_TYPE = {
    "mark": "MarkPrice",
    "last": "LastPrice",
    "index": "IndexPrice",
    # Identity passthrough so callers that already speak Bybit-native work.
    "MarkPrice": "MarkPrice",
    "LastPrice": "LastPrice",
    "IndexPrice": "IndexPrice",
}

# ── DNS pin ─────────────────────────────────────────────────────────────────
#
# Some ISPs (observed: TR-mobile / TR-fiber egress) silently drop TCP-443
# SYN packets to specific CloudFront edge IP ranges that Bybit's demo and
# testnet hostnames resolve to (e.g. 13.249.8.0/24). The mainnet
# distribution sits behind a different range (108.157.229.0/24) and
# routes fine, which makes this look like a credentials problem at first
# glance — auth tests "work" for a moment when the local resolver
# happens to return a good edge, then break on the next refresh.
#
# Workaround: at BybitClient construction, resolve api-demo.bybit.com via
# the system DNS, probe each returned IP with a fast TCP-443 connect, and
# pin the first reachable one for the session's lifetime. If every
# system-returned IP is blocked, fall back to a hardcoded shortlist of
# CloudFront edges known to work from the affected networks. The
# selected IP is forwarded into pybit by mounting a custom HTTPSAdapter
# on the underlying requests session — TLS still validates against the
# real hostname (SNI-based), only the destination IP changes.
_DEMO_HOST = "api-demo.bybit.com"
_TESTNET_HOST = "api-testnet.bybit.com"
_MAINNET_HOST = "api.bybit.com"
# Known-working CloudFront edges for api-demo.bybit.com observed
# 2026-04-25; updated as the ISP block list shifts.
_DEMO_FALLBACK_IPS = (
    "13.32.121.84",
    "13.32.121.30",
    "13.32.121.59",
    "13.32.121.94",
)
_PROBE_TIMEOUT_S = 2.0


def _probe_tcp_443(ip: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((ip, 443), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _pick_reachable_ip(host: str, fallbacks: tuple[str, ...] = ()) -> Optional[str]:
    """Resolve `host` via system DNS, return the first 443-reachable IP.

    Falls back to `fallbacks` (hardcoded edge IPs) when none of the
    system-returned IPs respond. Returns None if both lists are exhausted
    — caller should let the request go via the unpinned hostname so the
    failure surfaces with a clear timeout error.
    """
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        ips = [ai[4][0] for ai in infos]
    except socket.gaierror:
        ips = []
    seen: set[str] = set()
    ordered: list[str] = []
    for ip in list(ips) + list(fallbacks):
        if ip not in seen:
            ordered.append(ip)
            seen.add(ip)
    for ip in ordered:
        if _probe_tcp_443(ip):
            return ip
    return None


def _install_dns_pin(session: Any, host: str, ip: str) -> None:
    """Mount a custom HTTPS adapter that resolves `host` to `ip` while
    preserving SNI / certificate validation against the real hostname."""
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.poolmanager import PoolManager
    except ImportError:
        return

    class _PinnedAdapter(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs["server_hostname"] = host
            pool_kwargs["assert_hostname"] = host
            self.poolmanager = PoolManager(
                num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs,
            )

        def send(self, request, **kwargs):
            url = request.url
            if url and f"://{host}" in url:
                request.url = url.replace(f"://{host}", f"://{ip}", 1)
                request.headers["Host"] = host
            return super().send(request, **kwargs)

    session.mount(f"https://{host}", _PinnedAdapter())


# Internal canonical symbol format (`BTC-USDT-SWAP`) is the single string
# every runner / config / journal / test site uses. We translate to
# Bybit-native (`BTCUSDT`) at the API boundary so the rest of the codebase
# doesn't need to know which exchange backs it. The format originated with
# the pre-migration execution layer; it survived the 2026-04-25 Bybit
# migration as canonical because mass-renaming ~50 files + journal rows
# would dwarf the value of the rename.
_INTERNAL_TO_BYBIT_SYMBOL = {
    "BTC-USDT-SWAP": "BTCUSDT",
    "ETH-USDT-SWAP": "ETHUSDT",
    "SOL-USDT-SWAP": "SOLUSDT",
    "DOGE-USDT-SWAP": "DOGEUSDT",
    "BNB-USDT-SWAP": "BNBUSDT",
    "XRP-USDT-SWAP": "XRPUSDT",
    "ADA-USDT-SWAP": "ADAUSDT",
}

# Per-symbol contract-value-per-contract (carried over from the
# pre-migration sizing convention). Sizing math in `rr_system.py` works in
# integer "num_contracts" units against these multipliers (e.g. 10 BTC
# contracts × 0.01 = 0.1 BTC). Bybit linear takes qty in base coin, so we
# multiply at the boundary. Hardcoded because the operator's symbol set is
# fixed; if a 6th pair gets added, extend this map.
_INTERNAL_CT_VAL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
    "SOL-USDT-SWAP": 1.0,
    "DOGE-USDT-SWAP": 1000.0,
    "BNB-USDT-SWAP": 0.01,
    "XRP-USDT-SWAP": 100.0,
    "ADA-USDT-SWAP": 100.0,
}


_BYBIT_TO_INTERNAL_SYMBOL = {v: k for k, v in _INTERNAL_TO_BYBIT_SYMBOL.items()}


def _to_bybit_symbol(inst_id: str) -> str:
    """Internal `BTC-USDT-SWAP` → Bybit-native `BTCUSDT`.

    Idempotent for already-Bybit symbols (test fixtures may pass either
    format). Falls back to the input string when the symbol isn't in the
    map — Bybit will reject with a clear retCode if it's malformed.
    """
    if inst_id in _INTERNAL_TO_BYBIT_SYMBOL:
        return _INTERNAL_TO_BYBIT_SYMBOL[inst_id]
    return inst_id


def _from_bybit_symbol(symbol: str) -> str:
    """Bybit-native `BTCUSDT` → internal `BTC-USDT-SWAP` for journal /
    dict-key consistency with old rows."""
    return _BYBIT_TO_INTERNAL_SYMBOL.get(symbol, symbol)


def _strip_decimal_zeros(d: Decimal) -> str:
    """Format a Decimal without scientific notation, trailing zeros, or
    bare decimal points: `Decimal('21.4000') → '21.4'`, `Decimal('5000') →
    '5000'`. Bybit's order-create endpoint rejects scientific notation
    and is picky about trailing junk, so the qty/price strings we send
    must be plain decimal."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _qty_in_base(inst_id: str, size_contracts: float, qty_step: float = 0.0) -> str:
    """num_contracts (internal-format integer) → qty in base coin (Bybit string).

    Decimal arithmetic prevents the IEEE-754 noise that `214 × 0.1`
    produces in float (`21.400000000000002`); Bybit rejects such qty as
    "Qty invalid" (retCode 10001). When `qty_step` is provided we also
    floor-quantize to the symbol's lot filter so callers don't have to
    pre-round.

    Returns "0" for unmapped symbols so a missing entry surfaces as a
    Bybit-side rejection rather than a silent miscalc.
    """
    ct_val_f = _INTERNAL_CT_VAL.get(inst_id, 0.0)
    if ct_val_f <= 0:
        return "0"
    qty = Decimal(str(size_contracts)) * Decimal(str(ct_val_f))
    if qty_step and qty_step > 0:
        step = Decimal(str(qty_step))
        qty = (qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return _strip_decimal_zeros(qty)


def _format_price(price: float, tick_size: float = 0.0) -> str:
    """Format a float price for Bybit's order-create endpoint.

    Quantizes to `tick_size` when known so zone-shifted prices that
    arrive with arbitrary float precision (`2312.324429423`) become
    tick-aligned (`2312.32`). When `tick_size=0` (caller didn't supply
    or instrument-info hadn't loaded yet) we fall back to noise-strip
    formatting — Bybit is sometimes lenient on tick alignment for
    market orders but always rejects scientific notation / float junk.
    """
    if price <= 0:
        return "0"
    p = Decimal(str(price))
    if tick_size and tick_size > 0:
        tick = Decimal(str(tick_size))
        p = (p / tick).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * tick
    return _strip_decimal_zeros(p)


@dataclass
class BybitCredentials:
    api_key: str
    api_secret: str
    demo: bool = True
    # account_type stays here for completeness; the wallet/positions calls
    # always use UNIFIED on demo. Kept overridable for live deploy.
    account_type: str = "UNIFIED"
    category: str = "linear"

    def assert_demo(self) -> None:
        if not self.demo:
            raise RuntimeError(
                "Live trading flag set; refuse to run unless the caller "
                "explicitly opts in via BybitClient(allow_live=True)."
            )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _check(resp: dict, context: str) -> dict:
    """Validate a Bybit V5 envelope and return its `result` dict.

    Raises BybitError / OrderRejected / InsufficientMargin with the
    upstream retCode + retMsg so the router can log or branch.
    """
    if not isinstance(resp, dict):
        raise BybitError(
            f"{context}: unexpected response type {type(resp).__name__}",
            payload={"raw": resp},
        )
    ret_code = resp.get("retCode")
    if ret_code == 0:
        return resp.get("result") or {}

    msg = resp.get("retMsg") or "(no message)"
    code_str = str(ret_code) if ret_code is not None else ""

    if code_str in _INSUFFICIENT_MARGIN_CODES:
        raise InsufficientMargin(f"{context}: {msg}", code=code_str, payload=resp)
    if code_str in _ORDER_REJECTED_CODES:
        raise OrderRejected(f"{context}: {msg}", code=code_str, payload=resp)
    raise BybitError(f"{context}: {msg}", code=code_str, payload=resp)


def _pybit_ret_code(exc: Exception) -> Optional[str]:
    """Extract Bybit retCode from a pybit exception.

    pybit's `InvalidRequestError` raises on every non-zero retCode (instead
    of returning the dict for our `_check` to inspect), so the structured
    code lands on the exception itself. The class exposes `status_code`
    (which despite the name is the retCode, not the HTTP status). When the
    attribute is missing — e.g. on a network-layer exception — we fall back
    to parsing the message format `(ErrCode: NNNNN)` that pybit embeds.
    """
    code = getattr(exc, "status_code", None)
    if code is not None:
        try:
            return str(int(code))
        except (TypeError, ValueError):
            pass
    import re
    m = re.search(r"ErrCode:\s*(\d+)", str(exc))
    return m.group(1) if m else None


def _pos_idx(pos_side: str) -> int:
    """Internal `pos_side` ("long" / "short") → Bybit `positionIdx` (1 / 2).

    `positionIdx` identifies the POSITION (1=long, 2=short) — not the order
    side. Closing a long is still positionIdx=1 with side='Sell'.
    """
    if pos_side == "long":
        return 1
    if pos_side == "short":
        return 2
    raise ValueError(f"unknown pos_side={pos_side!r}; expected 'long' or 'short'")


def _entry_side(pos_side: str) -> str:
    return "Buy" if pos_side == "long" else "Sell"


def _closing_side(pos_side: str) -> str:
    return "Sell" if pos_side == "long" else "Buy"


def _trigger_px_type(value: str) -> str:
    return _TRIGGER_PX_TYPE.get(value, "MarkPrice")


# ── Client ──────────────────────────────────────────────────────────────────


class BybitClient:
    """Concrete Bybit V5 REST client. Inject a fake SDK via `sdk=` for tests.

    The injected SDK must expose the pybit V5 method names directly: the
    constructor stores it as `self.session` and every method calls
    `self.session.<pybit_method>(...)`.
    """

    def __init__(
        self,
        credentials: BybitCredentials,
        allow_live: bool = False,
        sdk: Any = None,
    ):
        if not credentials.demo and not allow_live:
            raise RuntimeError(
                "Refusing to construct BybitClient with demo=False "
                "unless allow_live=True is passed explicitly."
            )
        self.credentials = credentials
        self.demo = credentials.demo
        self.account_type = credentials.account_type
        self.category = credentials.category

        if sdk is None:
            from pybit.unified_trading import HTTP  # type: ignore[import]
            self.session = HTTP(
                demo=credentials.demo,
                api_key=credentials.api_key,
                api_secret=credentials.api_secret,
                recv_window=5000,
                timeout=30,
            )
            self._maybe_pin_demo_dns()
        else:
            self.session = sdk

        # Per-symbol Bybit instrument filters (qtyStep, tickSize, ...).
        # Populated lazily by `get_instrument_spec`. Used at order placement
        # to quantize qty / price so we never send float-noise values that
        # Bybit rejects with "Qty invalid" / "Price invalid".
        self._specs: dict[str, dict] = {}

    def _maybe_pin_demo_dns(self) -> None:
        """Probe Bybit's demo CloudFront edges at construction; if the
        default-resolved IPs are blocked at TCP-443 (some ISPs filter
        specific CloudFront ranges), pin the requests session to a
        reachable edge. No-op when the host isn't api-demo.bybit.com or
        when every reachable-probe succeeds via system DNS already.
        """
        if not self.demo:
            return
        client = getattr(self.session, "client", None)
        if client is None:
            return
        ip = _pick_reachable_ip(_DEMO_HOST, _DEMO_FALLBACK_IPS)
        if ip is None:
            logger.warning(
                "bybit_demo_dns_pin_failed host={} — leaving default "
                "resolver in place; expect ReadTimeout on next request",
                _DEMO_HOST,
            )
            return
        _install_dns_pin(client, _DEMO_HOST, ip)
        logger.info(
            "bybit_demo_dns_pinned host={} ip={}", _DEMO_HOST, ip,
        )

    # ── Account ─────────────────────────────────────────────────────────────

    def set_leverage(self, inst_id: str, leverage: int, **_: Any) -> dict:
        """Call POST /v5/position/set-leverage for both sides simultaneously.

        Bybit takes `buyLeverage` and `sellLeverage` separately; we set them
        equal to match the pre-migration single-leverage semantics. Extra kwargs are
        accepted and ignored for back-compat with pre-migration call sites that pass
        `mgn_mode=` / `pos_side=`.

        **Idempotent on 110043** ("leverage not modified") — pybit raises
        rather than returns a dict for this code, so we catch the typed
        exception and treat it as success. Without this, the bot's per-
        entry `set_leverage(75)` call rejects the second-and-onward entry
        on every symbol (first call sets the value, subsequent re-sets
        trip 110043).
        """
        try:
            resp = self.session.set_leverage(
                category=self.category,
                symbol=_to_bybit_symbol(inst_id),
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as exc:
            if _pybit_ret_code(exc) == "110043":
                return {}
            raise LeverageSetError(f"set_leverage: {exc}") from exc
        try:
            return _check(resp, "set_leverage")
        except BybitError as e:
            if e.code == "110043":
                return {}
            raise LeverageSetError(str(e), code=e.code, payload=e.payload) from e

    def get_balance(self, ccy: str = "USDT") -> float:
        """Available balance for placing new orders.

        Bybit UNIFIED accounts pool every collateral-toggled asset by USD
        value (USDT + USDC most commonly). `totalAvailableBalance` is the
        account-level USD figure already aggregated across them — the
        equivalent of the pre-migration `availEq`. The `coin=` filter only narrows
        the per-coin breakdown returned in `list[].coin[]`; the
        account-level totals stay the same.
        """
        resp = self.session.get_wallet_balance(
            accountType=self.account_type, coin=ccy,
        )
        result = _check(resp, "get_wallet_balance")
        rows = result.get("list") or []
        if not rows:
            return 0.0
        return float(rows[0].get("totalAvailableBalance") or 0.0)

    def get_total_equity(self, ccy: str = "USDT") -> float:
        """USD value of assets currently used as collateral (UTA pool).

        Used for per-slot sizing so each of `max_concurrent_positions`
        gets a fair share of the *usable* collateral — not the entire
        wallet. We deliberately read `totalMarginBalance` here, NOT
        `totalEquity`:
          * `totalEquity`        — every asset on the account, including
            those with the "Used as Collateral" toggle OFF (e.g. BTC /
            ETH wallet balances that don't back margin). Sizing against
            this would over-allocate.
          * `totalMarginBalance` — USD value of collateral-toggled
            assets only (USDT + USDC by default). Matches the "Margin
            Balance" figure in the Bybit UI and is the correct number
            for per-slot sizing.
        """
        resp = self.session.get_wallet_balance(
            accountType=self.account_type, coin=ccy,
        )
        result = _check(resp, "get_wallet_balance")
        rows = result.get("list") or []
        if not rows:
            return 0.0
        # Fall back to totalEquity if totalMarginBalance is missing
        # (some demo response shapes omit it).
        row = rows[0]
        return float(
            row.get("totalMarginBalance")
            or row.get("totalEquity")
            or 0.0
        )

    def get_wallet_balance_realized(self, ccy: str = "USDT") -> float:
        """USD value of the realized cash position — UPL EXCLUDED.

        Used by the auto-R sizer (`trading.auto_risk_pct_of_wallet`) so
        that per-trade R floats with the operator's true cash bankroll
        rather than with `totalMarginBalance` (which already includes
        unrealized PnL on every open position via mark-to-market).
        Excluding UPL prevents R from inflating during a winning streak
        and over-sizing into the next trade, or from compressing on a
        drawdown such that recovery R falls below the operator's intent.

        Reads `totalWalletBalance` from the account-level totals — Bybit
        V5's documented "Wallet Balance excluding open position UPL"
        field on UNIFIED accounts. Falls back to summing per-coin
        `walletBalance × usdValue / equity` across the `coin[]` array
        when the account-level field is missing on a demo response.
        Final fallback returns 0.0 so the caller treats it as a probe
        failure (skip-trade or env override).
        """
        resp = self.session.get_wallet_balance(
            accountType=self.account_type, coin=ccy,
        )
        result = _check(resp, "get_wallet_balance")
        rows = result.get("list") or []
        if not rows:
            return 0.0
        row = rows[0]
        # Primary: account-level totalWalletBalance (UPL-excluded by Bybit
        # spec). Present on every populated UNIFIED response observed in
        # the wild as of 2026-04-26.
        total_wallet = row.get("totalWalletBalance")
        if total_wallet:
            try:
                return float(total_wallet)
            except (TypeError, ValueError):
                pass
        # Fallback: sum per-coin walletBalance × spot multiplier. For USDT
        # / USDC `walletBalance` and `usdValue` are 1:1; for other
        # collateral coins we approximate via usdValue × walletBalance /
        # max(equity, walletBalance) so any bundled UPL drops out.
        total_usd = 0.0
        for coin_row in row.get("coin") or []:
            try:
                wallet_bal = float(coin_row.get("walletBalance") or 0.0)
                usd_value = float(coin_row.get("usdValue") or 0.0)
                equity = float(coin_row.get("equity") or wallet_bal)
            except (TypeError, ValueError):
                continue
            if wallet_bal <= 0 or usd_value <= 0:
                continue
            # When equity ≈ wallet (no UPL on this coin) usd_value is
            # already the correct realized USD. When equity > wallet
            # (UPL present), prorate by wallet/equity to strip UPL.
            ratio = wallet_bal / equity if equity > 0 else 1.0
            total_usd += usd_value * min(ratio, 1.0)
        return total_usd

    def set_position_mode_hedge(self, settle_coin: str = "USDT") -> dict:
        """Idempotent one-shot at startup: hedge mode for all USDT linear.

        `mode=3` enables Buy/Sell positions simultaneously (positionIdx=1/2).
        Calling on an already-hedged account returns retCode 110025
        ("position mode is not modified") which we treat as success
        (regardless of whether pybit raises or returns the dict).
        """
        try:
            resp = self.session.switch_position_mode(
                category=self.category, coin=settle_coin, mode=3,
            )
        except Exception as exc:
            if _pybit_ret_code(exc) == "110025":
                return {}
            raise BybitError(f"switch_position_mode: {exc}") from exc
        try:
            return _check(resp, "switch_position_mode")
        except BybitError as e:
            if e.code == "110025":
                return {}
            raise

    def set_margin_mode(self, mode: str = "REGULAR_MARGIN") -> dict:
        """Set UNIFIED account margin mode (cross-margin parity = REGULAR_MARGIN).

        The pre-migration per-call `tdMode=cross/isolated` has no Bybit equivalent on
        UNIFIED — margin mode is account-wide. Idempotent: a no-op when the
        mode is already set returns retCode 0.
        """
        try:
            resp = self.session.set_margin_mode(setMarginMode=mode)
        except Exception as exc:
            raise BybitError(f"set_margin_mode: {exc}") from exc
        return _check(resp, "set_margin_mode")

    # ── Market ──────────────────────────────────────────────────────────────

    def get_mark_price(self, inst_id: str) -> float:
        resp = self.session.get_tickers(
            category=self.category, symbol=_to_bybit_symbol(inst_id),
        )
        result = _check(resp, "get_tickers")
        rows = result.get("list") or []
        if not rows:
            return 0.0
        return float(rows[0].get("markPrice") or 0.0)

    def get_top_book(self, inst_id: str) -> tuple[float, float, float]:
        """Return `(bid1, ask1, mark)` from `/v5/market/tickers`.

        Used by the maker-first defensive close (Phase A.10) to place a
        post-only LIMIT just outside the spread on the closing side
        (ask + N*tick for long-close SELL, bid - N*tick for short-close
        BUY) so the post-only validation always passes. Returns
        `(0.0, 0.0, 0.0)` on missing rows / malformed payload — caller
        falls back to market on any zero.
        """
        resp = self.session.get_tickers(
            category=self.category, symbol=_to_bybit_symbol(inst_id),
        )
        result = _check(resp, "get_tickers")
        rows = result.get("list") or []
        if not rows:
            return (0.0, 0.0, 0.0)
        row = rows[0]
        return (
            float(row.get("bid1Price") or 0.0),
            float(row.get("ask1Price") or 0.0),
            float(row.get("markPrice") or 0.0),
        )

    def get_contract_size(self, inst_id: str) -> float:
        """Per-contract base-coin multiplier (internal canonical convention,
        carried over from the pre-migration sizing layer).

        The existing sizing math in `rr_system.py` works in integer
        "num_contracts" units against these multipliers without rewrites:
        BTC=0.01, ETH=0.1, SOL=1, DOGE=1000, BNB=0.01. The Bybit `qtyStep`
        filter is also fetched (in `get_instrument_spec`) but isn't
        returned from this back-compat method.
        """
        return float(_INTERNAL_CT_VAL.get(inst_id, 0.0))

    def get_instrument_spec(self, inst_id: str) -> dict:
        """Return per-symbol filters for sizing + leverage-cap math.

        Returns `{ct_val, max_leverage, qty_step, min_qty, max_qty,
        tick_size}`. `ct_val` is the hardcoded internal canonical
        multiplier (carried over from the pre-migration sizing layer) so
        sizing math works unchanged; the rest comes from Bybit's
        `instruments-info` so leverage caps + qty quantum reflect the
        actual venue.
        """
        resp = self.session.get_instruments_info(
            category=self.category, symbol=_to_bybit_symbol(inst_id),
        )
        result = _check(resp, "get_instruments_info")
        rows = result.get("list") or []
        ct_val = float(_INTERNAL_CT_VAL.get(inst_id, 0.0))
        if not rows:
            spec = {
                "ct_val": ct_val,
                "max_leverage": 0,
                "qty_step": 0.0, "min_qty": 0.0, "max_qty": 0.0,
                "tick_size": 0.0,
            }
            self._specs[inst_id] = spec
            return spec
        row = rows[0]
        lot = row.get("lotSizeFilter") or {}
        price = row.get("priceFilter") or {}
        lev = row.get("leverageFilter") or {}
        spec = {
            "ct_val": ct_val,
            "max_leverage": int(float(lev.get("maxLeverage") or 0)),
            "qty_step": float(lot.get("qtyStep") or 0.0),
            "min_qty": float(lot.get("minOrderQty") or 0.0),
            "max_qty": float(lot.get("maxOrderQty") or 0.0),
            "tick_size": float(price.get("tickSize") or 0.0),
        }
        self._specs[inst_id] = spec
        return spec

    def _qty_step(self, inst_id: str) -> float:
        return float(self._specs.get(inst_id, {}).get("qty_step") or 0.0)

    def _tick_size(self, inst_id: str) -> float:
        return float(self._specs.get(inst_id, {}).get("tick_size") or 0.0)

    # ── Orders ──────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        inst_id: str,
        side: str,                 # "buy" / "sell" (internal vocab) or "Buy"/"Sell"
        pos_side: str,             # "long" / "short"
        size_contracts: float,
        td_mode: str = "isolated", # accepted + ignored (account-wide on Bybit)
        client_order_id: Optional[str] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        trigger_px_type: str = "mark",
    ) -> OrderResult:
        """Place a market entry. TP/SL attach at placement time when provided.

        Optional `take_profit` / `stop_loss` ride along on the same call —
        Bybit V5 attaches them to the resulting position. Skip this on the
        limit-entry zone path where TP/SL get attached after fill via
        `set_position_tpsl()`.
        """
        cl_ord_id = client_order_id or f"smtbot{uuid.uuid4().hex[:20]}"
        tick = self._tick_size(inst_id)
        kwargs: dict[str, Any] = dict(
            category=self.category,
            symbol=_to_bybit_symbol(inst_id),
            side=_normalize_side(side),
            orderType="Market",
            qty=_qty_in_base(inst_id, size_contracts, self._qty_step(inst_id)),
            positionIdx=_pos_idx(pos_side),
            orderLinkId=cl_ord_id,
            timeInForce="IOC",
        )
        if take_profit is not None and take_profit > 0:
            kwargs["takeProfit"] = _format_price(take_profit, tick)
            kwargs["tpTriggerBy"] = _trigger_px_type(trigger_px_type)
        if stop_loss is not None and stop_loss > 0:
            kwargs["stopLoss"] = _format_price(stop_loss, tick)
            kwargs["slTriggerBy"] = _trigger_px_type(trigger_px_type)
        if take_profit or stop_loss:
            kwargs["tpslMode"] = "Full"

        resp = self.session.place_order(**kwargs)
        result = _check(resp, "place_order")
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            client_order_id=str(result.get("orderLinkId", cl_ord_id)),
            status=OrderStatus.PENDING,
            raw=resp,
        )

    def place_limit_order(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        size_contracts: float,
        px: float,
        td_mode: str = "isolated",  # accepted + ignored
        ord_type: str = "post_only",  # "post_only" | "limit"
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Place a limit (or post-only) entry. Returns OrderResult(PENDING).

        On `ord_type="post_only"` Bybit rejects with retCode 170218 if the
        order would take liquidity ("LIMIT-MAKER order rejected"); the
        router wraps the fallback-to-limit decision.
        """
        cl_ord_id = client_order_id or f"smtbot{uuid.uuid4().hex[:20]}"
        time_in_force = "PostOnly" if ord_type == "post_only" else "GTC"
        resp = self.session.place_order(
            category=self.category,
            symbol=_to_bybit_symbol(inst_id),
            side=_normalize_side(side),
            orderType="Limit",
            qty=_qty_in_base(inst_id, size_contracts, self._qty_step(inst_id)),
            price=_format_price(px, self._tick_size(inst_id)),
            positionIdx=_pos_idx(pos_side),
            timeInForce=time_in_force,
            orderLinkId=cl_ord_id,
        )
        result = _check(resp, "place_limit_order")
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            client_order_id=str(result.get("orderLinkId", cl_ord_id)),
            status=OrderStatus.PENDING,
            raw=resp,
        )

    def cancel_order(self, inst_id: str, order_id: str) -> dict:
        """Cancel a resting limit. Bybit retCode 110001/110008/170142/170213
        ("order not found / already filled / does not exist") signals an
        already-gone state. Callers (`PositionMonitor.poll_pending`,
        `cancel_pending`) detect this via `OrderRejected.code in
        _ORDER_GONE_CODES` and treat it as idempotent success.

        Two raise paths possible: pybit may raise its own typed exception
        before our `_check` runs, or it may return the dict and let us
        translate to OrderRejected via `_ORDER_REJECTED_CODES`. We
        normalise the pybit-raise path to `OrderRejected` with the
        structured code so the downstream check stays uniform.
        """
        try:
            resp = self.session.cancel_order(
                category=self.category,
                symbol=_to_bybit_symbol(inst_id),
                orderId=order_id,
            )
        except Exception as exc:
            code = _pybit_ret_code(exc)
            if code:
                raise OrderRejected(
                    f"cancel_order: {exc}", code=code, payload={},
                ) from exc
            raise
        return _check(resp, "cancel_order")

    def place_reduce_only_limit(
        self,
        inst_id: str,
        pos_side: str,
        size_contracts: float,
        px: float,
        td_mode: str = "isolated",   # accepted + ignored
        post_only: bool = True,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Place a reduce-only limit on an open position (maker-TP leg).

        `side` is the closing side of the position — Sell for long, Buy
        for short. `post_only=True` forces maker-only (retCode 170218 on
        rejection); callers can retry as plain limit if the book has
        already reached TP. A `smttp<hash>` orderLinkId tags it as a TP
        limit so the orphan-pending sweep can distinguish it from entries
        (`smtbot<hash>`).
        """
        cl_ord_id = client_order_id or f"smttp{uuid.uuid4().hex[:21]}"
        time_in_force = "PostOnly" if post_only else "GTC"
        resp = self.session.place_order(
            category=self.category,
            symbol=_to_bybit_symbol(inst_id),
            side=_closing_side(pos_side),
            orderType="Limit",
            qty=_qty_in_base(inst_id, size_contracts, self._qty_step(inst_id)),
            price=_format_price(px, self._tick_size(inst_id)),
            positionIdx=_pos_idx(pos_side),
            timeInForce=time_in_force,
            reduceOnly=True,
            orderLinkId=cl_ord_id,
        )
        result = _check(resp, "place_reduce_only_limit")
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            client_order_id=str(result.get("orderLinkId", cl_ord_id)),
            status=OrderStatus.PENDING,
            raw=resp,
        )

    def list_open_orders(  # noqa: C901
        self, inst_id: Optional[str] = None,
        order_filter: str = "Order",
    ) -> list[dict]:
        """List live open orders. Used by the startup orphan-pending-limit
        sweep to find any resting limits whose monitor tracking was lost
        across restart.

        `order_filter` choices: "Order" (regular limit/market), "StopOrder"
        (conditional / TP-SL trigger orders). For our orphan-limit sweep
        we always want "Order" — the position-attached TP/SL on Bybit V5
        is *not* a separate StopOrder, so it doesn't appear here.
        """
        kwargs: dict[str, Any] = {
            "category": self.category,
            "openOnly": 0,
            "orderFilter": order_filter,
        }
        if inst_id:
            kwargs["symbol"] = _to_bybit_symbol(inst_id)
        else:
            kwargs["settleCoin"] = "USDT"
        resp = self.session.get_open_orders(**kwargs)
        result = _check(resp, "list_open_orders")
        # Translate `symbol` field back to internal canonical format so downstream callers
        # (orphan-pending-limit sweep) get the same dict-key vocabulary they
        # used pre-migration.
        rows: list[dict] = []
        for row in result.get("list") or []:
            sym = row.get("symbol")
            if sym:
                row = {**row, "symbol": _from_bybit_symbol(sym)}
            rows.append(row)
        return rows

    def get_order(self, inst_id: str, order_id: str) -> dict:
        """Fetch the current state of one order.

        Returns a dict with at least `orderStatus` (New | PartiallyFilled |
        Filled | Cancelled | Rejected | Untriggered | Triggered |
        Deactivated), `cumExecQty`, and `avgPrice`. Callers should read
        `orderStatus` case-insensitively and translate:
            New / PartiallyFilled / Untriggered → live
            Filled                              → filled
            Cancelled / Rejected / Deactivated  → canceled
        """
        bybit_sym = _to_bybit_symbol(inst_id)
        resp = self.session.get_open_orders(
            category=self.category, symbol=bybit_sym, orderId=order_id,
        )
        result = _check(resp, "get_order")
        rows = result.get("list") or []
        if rows:
            return rows[0]
        # Fallback to history if the order has already filled / been cancelled.
        try:
            hist = self.session.get_order_history(
                category=self.category, symbol=bybit_sym, orderId=order_id,
            )
            hist_result = _check(hist, "get_order_history")
            hist_rows = hist_result.get("list") or []
            if hist_rows:
                return hist_rows[0]
        except (BybitError, AttributeError):
            pass
        return {}

    # ── Position-attached TP/SL (replaces OCO algo flow) ────────────────────

    def set_position_tpsl(
        self,
        inst_id: str,
        pos_side: str,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        tpsl_mode: str = "Full",
        trigger_px_type: str = "mark",
    ) -> dict:
        """POST /v5/position/trading-stop — set or modify TP/SL on an open
        position. Replaces the pre-migration OCO algo placement + cancel+replace dance.

        Pass `take_profit=0.0` or `stop_loss=0.0` to clear that leg
        independently. Per Bybit docs, modifying one side "loses the
        binding relationship" between TP and SL — for our flows (BE move,
        TP revise, SL lock all touch one leg at a time) this is fine.
        """
        tick = self._tick_size(inst_id)
        kwargs: dict[str, Any] = dict(
            category=self.category,
            symbol=_to_bybit_symbol(inst_id),
            tpslMode=tpsl_mode,
            positionIdx=_pos_idx(pos_side),
        )
        if take_profit is not None:
            # Pass "0" as-is (Bybit's "clear that leg" convention); otherwise
            # quantize to tick so dynamic-TP revisions don't send misaligned
            # prices like `2312.324429423`.
            kwargs["takeProfit"] = (
                "0" if take_profit == 0 else _format_price(take_profit, tick)
            )
            kwargs["tpTriggerBy"] = _trigger_px_type(trigger_px_type)
        if stop_loss is not None:
            kwargs["stopLoss"] = (
                "0" if stop_loss == 0 else _format_price(stop_loss, tick)
            )
            kwargs["slTriggerBy"] = _trigger_px_type(trigger_px_type)
        try:
            resp = self.session.set_trading_stop(**kwargs)
        except Exception as exc:
            # Bybit `34040` ("tp/sl not modified") fires when the requested
            # value already matches the on-position value — common during
            # the per-cycle dynamic TP-revise loop when nothing changed
            # since the last tick. Idempotent: treat as success so the
            # monitor's revise gate stops emitting WARNING logs and the
            # one-shot SL-lock flag flips correctly.
            if _pybit_ret_code(exc) == "34040":
                return {}
            raise
        return _check(resp, "set_trading_stop")

    def close_position(
        self, inst_id: str, pos_side: str, td_mode: str = "isolated",
    ) -> dict:
        """Emergency close: market reduce-only for the full position size.

        Used when TP/SL attachment fails and the router can't leave the
        position unprotected. Bybit has no single "close-positions"
        endpoint; we fetch the current size and submit a reduce-only
        market in the closing direction.
        """
        positions = self.get_positions(inst_id=inst_id)
        for snap in positions:
            if snap.pos_side != pos_side:
                continue
            if snap.size <= 0:
                continue
            cl_ord_id = f"smtflat{uuid.uuid4().hex[:19]}"
            # snap.size is already in internal-format contract units (we converted it in
            # `get_positions`); flip back to base coin for the Bybit qty,
            # quantized to the symbol's qtyStep so we never send float noise.
            resp = self.session.place_order(
                category=self.category,
                symbol=_to_bybit_symbol(inst_id),
                side=_closing_side(pos_side),
                orderType="Market",
                qty=_qty_in_base(inst_id, snap.size, self._qty_step(inst_id)),
                positionIdx=_pos_idx(pos_side),
                reduceOnly=True,
                timeInForce="IOC",
                orderLinkId=cl_ord_id,
            )
            return _check(resp, "close_position")
        return {}

    # ── Positions ───────────────────────────────────────────────────────────

    def get_positions(self, inst_id: Optional[str] = None) -> list[PositionSnapshot]:
        kwargs: dict[str, Any] = {"category": self.category}
        if inst_id:
            kwargs["symbol"] = _to_bybit_symbol(inst_id)
        else:
            kwargs["settleCoin"] = "USDT"
        resp = self.session.get_positions(**kwargs)
        result = _check(resp, "get_positions")
        snapshots: list[PositionSnapshot] = []
        for row in result.get("list") or []:
            sym = row.get("symbol")
            if not sym:
                continue
            internal_sym = _from_bybit_symbol(sym)
            size_base = float(row.get("size") or 0.0)
            pos_idx = int(row.get("positionIdx") or 0)
            if pos_idx == 1:
                pos_side = "long"
            elif pos_idx == 2:
                pos_side = "short"
            else:
                # one-way mode: derive from `side`
                side = (row.get("side") or "").lower()
                pos_side = "long" if side == "buy" else "short" if side == "sell" else ""
            # Bybit's `size` is in base coin; flip back to internal-format
            # contract units so the rest of the codebase (monitor, sizing
            # math, journal) works in the integer-contracts vocabulary it
            # was written in. `round()` absorbs IEEE 754 division drift —
            # e.g. ETH 0.7 / 0.1 = 6.999999999999999 must become 7. Without
            # it, `_detect_tp1_and_move_sl` mis-fires "size shrank" on every
            # poll for fractional-ct_val symbols (BTC 0.01 / ETH 0.1) and
            # eventually trips `be_already_moved=True` after retry exhaustion,
            # blocking the legitimate MFE-lock path downstream.
            ct_val = float(_INTERNAL_CT_VAL.get(internal_sym, 0.0))
            size_contracts = round(size_base / ct_val) if ct_val > 0 else size_base
            snapshots.append(PositionSnapshot(
                inst_id=internal_sym,
                pos_side=pos_side,
                size=size_contracts,
                entry_price=float(row.get("avgPrice") or 0.0),
                mark_price=float(row.get("markPrice") or 0.0),
                unrealized_pnl=float(row.get("unrealisedPnl") or 0.0),
                leverage=int(float(row.get("leverage") or 0)),
            ))
        return snapshots

    # Slack subtracted from `fill.opened_at` when filtering closed-pnl rows.
    # Bybit's `createdTime` for the close-row is the time the closing fill
    # printed; the bot's `opened_at` is wall-clock at register_open. A few
    # seconds of clock drift between the two is normal; widen the cutoff
    # slightly so we don't drop a legitimate close that printed a hair before
    # `opened_at`. A genuinely stale row (previous close on the same symbol)
    # will be many minutes / hours older, far past this slack.
    _ENRICH_OPENED_AT_SLACK_S = 5.0
    # Retry budget when no closed-pnl row newer than `opened_at` is returned.
    # Bybit occasionally lags writing the close row by 1-3s after the
    # position size hits 0; without retry the enrich falls through to the
    # previous close on the same symbol+side and stamps wrong PnL/exit. Two
    # short retries keep the loop responsive while clearing realistic lag.
    _ENRICH_RETRY_DELAYS_S: tuple[float, ...] = (1.5, 3.0)

    def enrich_close_fill(self, fill: CloseFill) -> CloseFill:
        """Replace the PositionMonitor's zeroed PnL/exit fields with real
        values from /v5/position/closed-pnl.

        PositionMonitor.poll() emits CloseFill with pnl_usdt=0 / exit_price=0
        because it only knows the position disappeared. Here we fetch the
        most recent closed-pnl rows matching this symbol + posIdx and fill in
        `closedPnl` / `avgExitPrice` / `closeFee + openFee`.

        When `fill.opened_at` is set, we filter out rows whose `createdTime`
        predates the position's open (with a small slack). This guards
        against a Bybit lag in writing the new close row: without the filter
        the function would latch onto the previous close on the same
        symbol+side and stamp wrong values on the journal. If no row newer
        than `opened_at` is visible on the first call we retry briefly
        before giving up — a genuine maker-fill close usually surfaces in
        closed-pnl within ~3s.

        When no matching row is returned (or only stale rows survive the
        filter after retries) we pass the fill through unchanged so the
        caller can still log / decide; zero-PnL closes are never silently
        accepted up the stack.
        """
        bybit_sym = _to_bybit_symbol(fill.inst_id)
        target_idx = _pos_idx(fill.pos_side) if fill.pos_side else 0

        # closed-pnl rows include `side` (the closing side) — for a long
        # position the close is "Sell", for short it's "Buy". Match either via
        # explicit positionIdx (newer Bybit responses) or derive from side.
        def _row_pos_idx(r: dict) -> int:
            pi = r.get("positionIdx")
            if pi is not None:
                try:
                    return int(pi)
                except (TypeError, ValueError):
                    pass
            side = (r.get("side") or "").lower()
            if side == "sell":
                return 1  # closed a long
            if side == "buy":
                return 2  # closed a short
            return 0

        def _ts(r: dict) -> int:
            return int(r.get("updatedTime") or r.get("createdTime") or "0")

        # Compute the cutoff once per call: rows older than this are stale
        # leftovers from previous closes on the same symbol+side and must be
        # rejected. None disables the filter (back-compat for callers that
        # haven't threaded opened_at yet).
        opened_at_cutoff_ms: Optional[int] = None
        if fill.opened_at is not None:
            opened_at_cutoff_ms = int(
                (fill.opened_at.timestamp() - self._ENRICH_OPENED_AT_SLACK_S) * 1000
            )

        def _fetch_and_filter() -> list[dict]:
            try:
                # limit=20 (was 5) leaves headroom: a single symbol with a
                # burst of closes (e.g. multiple defensive-close fills in
                # one session) can otherwise push the latest row off the
                # window, leaving only stale ones to match.
                resp = self.session.get_closed_pnl(
                    category=self.category, symbol=bybit_sym, limit=20,
                )
            except Exception as exc:
                raise BybitError(f"get_closed_pnl: {exc}") from exc
            result = _check(resp, "get_closed_pnl")
            rows = result.get("list") or []
            base = [
                r for r in rows
                if r.get("symbol") == bybit_sym and (
                    target_idx == 0 or _row_pos_idx(r) == target_idx
                )
            ]
            if opened_at_cutoff_ms is None:
                return base
            return [r for r in base if _ts(r) >= opened_at_cutoff_ms]

        matches = _fetch_and_filter()
        for delay_s in self._ENRICH_RETRY_DELAYS_S:
            if matches:
                break
            time.sleep(delay_s)
            matches = _fetch_and_filter()

        if not matches:
            # No row newer than opened_at after retries. Returning the raw
            # fill (zero PnL/exit) is the lesser evil: caller logs it as a
            # missing-enrichment row rather than the journal absorbing a
            # stale previous-close's numbers.
            return fill

        row = max(matches, key=_ts)

        exit_price = float(row.get("avgExitPrice") or fill.exit_price)
        pnl = float(row.get("closedPnl") or fill.pnl_usdt)
        # Bybit splits open + close fees as positive numbers; the bot's
        # convention (carried over from the pre-migration layer) is signed (negative = paid out).
        # Sum and negate to maintain semantics.
        open_fee = float(row.get("openFee") or 0.0)
        close_fee = float(row.get("closeFee") or 0.0)
        fee_signed = -(open_fee + close_fee) if (open_fee or close_fee) else fill.fee_usdt
        ts_ms = _ts(row)
        closed_at = (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            if ts_ms else fill.closed_at
        )

        return CloseFill(
            inst_id=fill.inst_id,
            pos_side=fill.pos_side,
            entry_price=fill.entry_price,
            exit_price=exit_price,
            size=fill.size,
            pnl_usdt=pnl,
            fee_usdt=fee_signed,
            closed_at=closed_at,
            opened_at=fill.opened_at,
        )


def _normalize_side(side: str) -> str:
    """Accept internal vocab ('buy'/'sell') and Bybit-vocab ('Buy'/'Sell')."""
    s = side.strip()
    if s.lower() == "buy":
        return "Buy"
    if s.lower() == "sell":
        return "Sell"
    return s
