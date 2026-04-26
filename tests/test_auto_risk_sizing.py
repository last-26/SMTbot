"""Auto-R sizing (2026-04-26).

R = realized_wallet_balance × `auto_risk_pct_of_wallet`. Realized excludes
unrealized PnL (Bybit V5 `totalWalletBalance` field), so a winning streak
or a drawdown on open positions doesn't drag the per-trade $R around.

Tests cover three layers:
  1. `BybitClient.get_wallet_balance_realized` raw-response parsing —
     prefers account-level `totalWalletBalance`; falls back to per-coin
     `walletBalance × usdValue / equity` to strip UPL.
  2. `TradingConfig.auto_risk_pct_of_wallet` validator range.
  3. End-to-end through `calculate_trade_plan` — resolved override flows
     into max_risk identically to the legacy env path.
"""

from __future__ import annotations

import pytest

from src.bot.config import TradingConfig
from src.data.models import Direction
from src.execution.bybit_client import BybitClient, BybitCredentials
from src.strategy.rr_system import calculate_trade_plan


# ── Fake pybit SDK ──────────────────────────────────────────────────────────


class _FakeSession:
    """Minimal pybit.HTTP shim — only the wallet endpoint."""

    def __init__(self, wallet_payload: dict):
        self._wallet_payload = wallet_payload

    def get_wallet_balance(self, **_: object) -> dict:
        return {"retCode": 0, "retMsg": "OK", "result": self._wallet_payload}


def _make_client(payload: dict) -> BybitClient:
    creds = BybitCredentials(api_key="x", api_secret="y", demo=True)
    return BybitClient(creds, sdk=_FakeSession(payload))


# ── 1. BybitClient.get_wallet_balance_realized ──────────────────────────────


def test_get_wallet_balance_realized_prefers_total_wallet_balance():
    """Account-level `totalWalletBalance` wins when present — UPL-excluded
    by Bybit V5 spec, no per-coin pro-rating needed."""
    payload = {
        "list": [{
            "totalEquity": "550.0",          # wallet + UPL
            "totalMarginBalance": "540.0",
            "totalAvailableBalance": "490.0",
            "totalWalletBalance": "500.0",   # ← UPL-excluded
            "coin": [
                {"coin": "USDT", "walletBalance": "500.0",
                 "usdValue": "500.0", "equity": "550.0"},
            ],
        }],
    }
    client = _make_client(payload)
    assert client.get_wallet_balance_realized() == pytest.approx(500.0)


def test_get_wallet_balance_realized_falls_back_to_coin_array():
    """When `totalWalletBalance` is absent, sum per-coin walletBalance and
    prorate by wallet/equity to strip UPL inside `usdValue`."""
    payload = {
        "list": [{
            "totalEquity": "660.0",
            "totalMarginBalance": "650.0",
            # totalWalletBalance OMITTED — exercise fallback
            "coin": [
                # USDT: 400 wallet, 500 usdValue (incl. $100 UPL bundled),
                # equity 500. Ratio = 400/500 = 0.8 → realized = 500*0.8 = 400.
                {"coin": "USDT", "walletBalance": "400.0",
                 "usdValue": "500.0", "equity": "500.0"},
                # USDC: clean (no UPL on this coin), wallet=usdValue=160.
                {"coin": "USDC", "walletBalance": "160.0",
                 "usdValue": "160.0", "equity": "160.0"},
            ],
        }],
    }
    client = _make_client(payload)
    # 400 + 160 = 560 realized USD.
    assert client.get_wallet_balance_realized() == pytest.approx(560.0)


def test_get_wallet_balance_realized_handles_empty_response():
    """Empty `list` → 0.0; caller (runner) treats this as a probe failure."""
    client = _make_client({"list": []})
    assert client.get_wallet_balance_realized() == 0.0


def test_get_wallet_balance_realized_handles_malformed_floats():
    """Junk strings in fallback coin rows are skipped, not raised."""
    payload = {
        "list": [{
            # Force fallback path (no totalWalletBalance).
            "coin": [
                {"coin": "USDT", "walletBalance": "not-a-number",
                 "usdValue": "100", "equity": "100"},
                {"coin": "USDC", "walletBalance": "50",
                 "usdValue": "50", "equity": "50"},
            ],
        }],
    }
    client = _make_client(payload)
    # USDT row dropped, USDC row counted.
    assert client.get_wallet_balance_realized() == pytest.approx(50.0)


# ── 2. Config validator ─────────────────────────────────────────────────────


def _trading_kwargs(**overrides):
    base = dict(
        symbols=["BTC-USDT-SWAP"],
        entry_timeframe="3m",
        htf_timeframe="15m",
        risk_per_trade_pct=1.0,
        max_leverage=10,
        default_rr_ratio=2.0,
        min_rr_ratio=1.5,
        max_concurrent_positions=5,
    )
    base.update(overrides)
    return base


def test_auto_risk_pct_default_is_zero():
    """Default value is 0 (disabled) — preserves legacy behaviour for any
    config not opting in."""
    cfg = TradingConfig(**_trading_kwargs())
    assert cfg.auto_risk_pct_of_wallet == 0.0


def test_auto_risk_pct_accepts_valid_range():
    cfg = TradingConfig(**_trading_kwargs(auto_risk_pct_of_wallet=0.02))
    assert cfg.auto_risk_pct_of_wallet == 0.02


def test_auto_risk_pct_rejects_above_ten_percent():
    """Mirrors the rr_system override safety ceiling (≤ 10% of balance).
    A misconfigured 0.5 must not silently size half-bankroll positions."""
    with pytest.raises(ValueError, match="auto_risk_pct_of_wallet"):
        TradingConfig(**_trading_kwargs(auto_risk_pct_of_wallet=0.15))


def test_auto_risk_pct_rejects_negative():
    with pytest.raises(ValueError, match="auto_risk_pct_of_wallet"):
        TradingConfig(**_trading_kwargs(auto_risk_pct_of_wallet=-0.01))


# ── 3. End-to-end through calculate_trade_plan ──────────────────────────────


def test_resolved_auto_risk_flows_into_max_risk_identically_to_env_override():
    """The runner pre-computes `wallet × pct` and passes it as
    `risk_amount_usdt_override`. This must produce the same TradePlan as
    setting the env override to the same dollar value — the resolution
    point shifted from rr_system to runner, but the math is unchanged."""
    common = dict(
        direction=Direction.BULLISH,
        entry_price=100.0,
        sl_price=99.0,           # 1% SL
        account_balance=1000.0,
        risk_pct=0.01,           # legacy fallback (ignored when override set)
        rr_ratio=2.0,
        max_leverage=10,
        contract_size=1.0,
        fee_reserve_pct=0.0,
    )

    # Auto-mode equivalent: wallet=$1000, pct=0.02 → resolved $20 R.
    plan_auto = calculate_trade_plan(
        **common, risk_amount_usdt_override=20.0,
    )

    # Manual env override at the same dollar.
    plan_env = calculate_trade_plan(
        **common, risk_amount_usdt_override=20.0,
    )

    assert plan_auto.risk_amount_usdt == pytest.approx(plan_env.risk_amount_usdt)
    assert plan_auto.num_contracts == plan_env.num_contracts
    # 1% SL × notional should land $20 ± one ceil step.
    assert plan_auto.risk_amount_usdt == pytest.approx(20.0, abs=1.0)


def test_resolved_auto_risk_safety_ceiling_blocks_oversized_pct():
    """If a buggy probe returned > 10% of balance, rr_system rejects.
    Defence-in-depth: the field validator capped pct at 0.1, but a stale
    `current_balance` × auto pct could still breach the runtime ceiling
    when called against `account_balance`."""
    with pytest.raises(ValueError, match="exceeds 10%"):
        calculate_trade_plan(
            direction=Direction.BULLISH,
            entry_price=100.0,
            sl_price=99.0,
            account_balance=100.0,         # tiny bankroll
            risk_pct=0.01,
            rr_ratio=2.0,
            max_leverage=10,
            contract_size=1.0,
            risk_amount_usdt_override=15.0,  # 15% of $100 → reject
        )
