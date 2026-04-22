"""Risk-to-Reward (R:R) system — pure math for sizing a trade.

Given an entry, stop-loss, account balance, and risk budget, compute:
  - take-profit price from an R:R ratio
  - position size (USDT notional) so that SL hit = fixed USDT risk
  - required leverage to reach that notional
  - clamp leverage to an account-wide maximum (shrinks notional if needed)
  - round notional to an integer number of OKX contracts

Design rules (CLAUDE.md):
  - USDT risk per trade stays constant; leverage is dynamic, never fixed.
  - Tight SL (0.3%) ⇒ ~15-20x; wide SL (2%) ⇒ ~3-5x.
  - When the leverage/margin ceiling binds (`capped=True`), contracts are
    floor-rounded → actual risk ends up strictly below requested risk.
    Otherwise contracts are ceil-rounded (post-2026-04-19 operator contract)
    so realized loss ≥ max_risk_usdt; overshoot bounded by one per-contract
    cost step (< $3 for current symbol universe). This keeps SL/TP USDT
    amounts equalized across symbols instead of varying $40-$54 by symbol.
  - `risk_amount_usdt_override` (post-2026-04-20 operator contract): when
    provided, bypass `account_balance × risk_pct` and use the override as
    max_risk directly. Gives the operator a flat-dollar $R across all
    symbols and all cycles, immune to unrealized-drawdown balance shimmer
    from concurrent open positions. Safety rail: override ≤ 10% of
    account_balance (mirrors the existing `risk_pct <= 0.1` ceiling).
  - OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC notional. Integer contracts only.

This module is pure: no I/O, no async, safe to import from anywhere.
"""

from __future__ import annotations

import math
from typing import Optional

from src.data.models import Direction
from src.strategy.trade_plan import TradePlan

# Keep a little balance free for fees + mark-price fluctuations between
# set_leverage and place_order. OKX rejects with sCode 51008 when initial
# margin + buffer > available balance.
_MARGIN_SAFETY = 0.95

# When picking leverage we want to MINIMIZE initial margin so multiple
# concurrent positions can coexist. Upper-bound leverage by the distance
# from entry to liquidation: at leverage L, liquidation is ~1/L away, so
# SL must trigger at well under that fraction. LIQ_SAFETY_FACTOR=0.6
# means the SL sits at most 60% of the way to liquidation at the chosen
# leverage (40% buffer for maintenance + mark drift).
_LIQ_SAFETY_FACTOR = 0.6


def _validate(
    direction: Direction,
    entry_price: float,
    sl_price: float,
    account_balance: float,
    risk_pct: float,
    rr_ratio: float,
    max_leverage: int,
    contract_size: float,
) -> None:
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        raise ValueError("direction must be BULLISH or BEARISH")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if sl_price <= 0:
        raise ValueError("sl_price must be positive")
    if direction == Direction.BULLISH and sl_price >= entry_price:
        raise ValueError("BULLISH SL must be strictly below entry")
    if direction == Direction.BEARISH and sl_price <= entry_price:
        raise ValueError("BEARISH SL must be strictly above entry")
    if account_balance <= 0:
        raise ValueError("account_balance must be positive")
    if not (0 < risk_pct <= 0.1):
        raise ValueError("risk_pct must be in (0, 0.1]")
    if rr_ratio <= 0:
        raise ValueError("rr_ratio must be positive")
    if max_leverage < 1:
        raise ValueError("max_leverage must be >= 1")
    if contract_size <= 0:
        raise ValueError("contract_size must be positive")


def calculate_trade_plan(
    direction: Direction,
    entry_price: float,
    sl_price: float,
    account_balance: float,
    risk_pct: float = 0.01,
    rr_ratio: float = 3.0,
    max_leverage: int = 20,
    contract_size: float = 0.01,
    margin_balance: Optional[float] = None,
    fee_reserve_pct: float = 0.0,
    risk_amount_usdt_override: Optional[float] = None,
    sl_source: str = "",
    confluence_score: float = 0.0,
    confluence_factors: list[str] | None = None,
    confluence_pillar_scores: Optional[dict[str, float]] = None,
    reason: str = "",
) -> TradePlan:
    """Compute a fully-sized TradePlan.

    Args:
        direction: BULLISH (long) or BEARISH (short).
        entry_price: intended entry.
        sl_price: stop-loss price (below entry for longs, above for shorts).
        account_balance: USDT equity used for the *risk* budget (R = balance
            × risk_pct). Typically the total account equity — independent of
            how much is currently locked in other positions.
        risk_pct: fraction of `account_balance` to risk (0.01 = 1%).
        rr_ratio: TP distance / SL distance.
        max_leverage: hard cap on leverage (from circuit breakers).
        contract_size: BTC per OKX contract (BTC-USDT-SWAP = 0.01).
        margin_balance: USDT actually available to post as initial margin for
            this trade. When omitted, falls back to `account_balance`. Split
            out so R is sized off total equity while notional/leverage still
            respect the live free-margin ceiling (sCode 51008 avoidance).
        fee_reserve_pct: round-trip taker fee + slippage reserve added to
            sl_pct when computing notional, so the stop-out loss stays inside
            the risk budget *after* fees. Set to 2 × taker_pct (≈0.001) for
            OKX demo taker orders. 0 disables (legacy behavior).
        risk_amount_usdt_override: operator-set flat $R. When provided (and
            > 0), bypasses `account_balance × risk_pct` and uses this number
            as max_risk directly. None = legacy percent mode. Safety rail:
            override must not exceed 10% of account_balance (same ceiling
            as the `risk_pct <= 0.1` rule above), raises ValueError if so.
        sl_source: label for journal/telemetry ("order_block", "fvg", …).
        confluence_score: score that led to this trade (for journal).
        confluence_factors: names of factors that contributed (for journal).
        reason: free-text summary.

    Returns:
        TradePlan with every field populated.
    """
    _validate(direction, entry_price, sl_price, account_balance,
              risk_pct, rr_ratio, max_leverage, contract_size)
    if fee_reserve_pct < 0:
        raise ValueError("fee_reserve_pct must be >= 0")

    effective_margin = (margin_balance
                        if margin_balance is not None else account_balance)
    if effective_margin <= 0:
        raise ValueError("margin_balance must be positive")

    sl_distance = abs(entry_price - sl_price)
    sl_pct = sl_distance / entry_price

    # Operator-set flat $R overrides balance × risk_pct when present. Safety
    # rail: override ≤ 10% of account_balance (mirrors `risk_pct <= 0.1`).
    # Rejecting loudly here keeps a stale/too-high override from silently
    # sizing a position beyond the per-trade loss cap.
    if risk_amount_usdt_override is not None:
        if risk_amount_usdt_override <= 0:
            raise ValueError(
                "risk_amount_usdt_override must be > 0 when set "
                "(pass None to fall back to balance × risk_pct)"
            )
        if risk_amount_usdt_override > account_balance * 0.1:
            raise ValueError(
                f"risk_amount_usdt_override={risk_amount_usdt_override} "
                f"exceeds 10% of account_balance={account_balance}; lower "
                "the override or top up balance before continuing."
            )
        max_risk_usdt = risk_amount_usdt_override
    else:
        max_risk_usdt = account_balance * risk_pct

    if direction == Direction.BULLISH:
        tp_price = entry_price + sl_distance * rr_ratio
    else:
        tp_price = entry_price - sl_distance * rr_ratio

    # Effective loss fraction covers the price move AND round-trip fees; sizing
    # off this shrinks notional enough that a stop-out still lands at ≈R *after*
    # taker fees. TP is unchanged (price-only), so fee compensation comes from
    # size, not from widening TP.
    effective_sl_pct = sl_pct + fee_reserve_pct
    # Ideal notional so that SL hit loses exactly max_risk_usdt, net of fees.
    ideal_notional = max_risk_usdt / effective_sl_pct
    required_leverage = ideal_notional / effective_margin

    # Hard ceiling on notional: leverage cap AND margin safety buffer. Without
    # the buffer, a fully-leveraged order leaves OKX no room for fees and gets
    # rejected with sCode 51008.
    max_notional = effective_margin * max_leverage * _MARGIN_SAFETY
    if ideal_notional > max_notional:
        notional = max_notional
        capped = True
    else:
        notional = ideal_notional
        capped = False

    # Leverage floor — margin (= notional / leverage) must fit inside
    # effective_margin × _MARGIN_SAFETY. Below this OKX rejects with 51008.
    min_lev_for_margin = max(
        1, math.ceil(notional / (effective_margin * _MARGIN_SAFETY))
    )
    # Leverage ceiling — liquidation must sit well past SL. At high
    # leverage, liquidation approaches entry; we require sl_pct to stay
    # within _LIQ_SAFETY_FACTOR of the liq distance (~1/leverage).
    liq_safe_leverage = max(
        1, math.floor(_LIQ_SAFETY_FACTOR / max(sl_pct, 1e-6))
    )
    # Use the MAX feasible leverage: this minimizes initial margin locked
    # per position so max_concurrent_positions > 1 actually fits inside
    # the account. Never drop below the margin floor.
    leverage = min(max_leverage, liq_safe_leverage)
    leverage = max(leverage, min_lev_for_margin, 1)

    # Integer OKX contracts. Operator contract (2026-04-19, post-partial-TP-off):
    # each position must realize AT LEAST max_risk_usdt at SL (and rr_ratio ×
    # that at TP), regardless of per-symbol ctVal/entry quantization. Ceil on
    # per-contract TOTAL cost (price + fee reserve) so realized loss ≈
    # max_risk across symbols; previously floor produced $40-$54 variance on
    # nominal $55. Overshoot bounded by one per_contract_cost step (< $3 for
    # current symbol set). Capped path (leverage/margin ceiling binds) keeps
    # floor — respecting the hard ceiling wins over the equal-risk target.
    contracts_unit_usdt = contract_size * entry_price
    if capped:
        num_contracts = int(notional // contracts_unit_usdt)
    else:
        per_contract_cost = effective_sl_pct * contracts_unit_usdt
        target_contracts = math.ceil(max_risk_usdt / per_contract_cost)
        # Safety: ceil must not breach the leverage/margin ceiling. When the
        # cap can't afford even one contract, propagate 0 — the caller
        # rejects with `zero_contracts`. We never force a minimum of 1 here
        # because that would silently violate the margin buffer.
        max_contracts_by_notional = int(max_notional // contracts_unit_usdt)
        if target_contracts > max_contracts_by_notional:
            num_contracts = max_contracts_by_notional
            capped = True
        else:
            num_contracts = target_contracts
    # actual_risk_usdt stays price-only (sl_pct, not effective) so the journal
    # field represents the price-move loss; fees are covered by the reserved
    # portion of per_contract_cost.
    actual_notional = num_contracts * contracts_unit_usdt
    actual_risk_usdt = actual_notional * sl_pct

    return TradePlan(
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        rr_ratio=rr_ratio,
        sl_distance=sl_distance,
        sl_pct=sl_pct,
        position_size_usdt=actual_notional,
        leverage=leverage,
        required_leverage=required_leverage,
        num_contracts=num_contracts,
        risk_amount_usdt=actual_risk_usdt,
        max_risk_usdt=max_risk_usdt,
        capped=capped,
        fee_reserve_pct=fee_reserve_pct,
        sl_source=sl_source,
        confluence_score=confluence_score,
        confluence_factors=list(confluence_factors or []),
        confluence_pillar_scores=dict(confluence_pillar_scores or {}),
        reason=reason,
    )


def break_even_win_rate(rr_ratio: float) -> float:
    """Minimum win rate (fractional) for an expectancy-neutral R:R ratio.

    rr=1 → 0.5, rr=2 → 0.333…, rr=3 → 0.25. Ignores fees.
    """
    if rr_ratio <= 0:
        raise ValueError("rr_ratio must be positive")
    return 1.0 / (1.0 + rr_ratio)


def expected_value_r(win_rate: float, rr_ratio: float) -> float:
    """Expected value of a trade in R units.

    E[R] = win_rate * rr_ratio - (1 - win_rate). Positive = edge, 0 = coin flip.
    """
    if not (0.0 <= win_rate <= 1.0):
        raise ValueError("win_rate must be in [0, 1]")
    if rr_ratio <= 0:
        raise ValueError("rr_ratio must be positive")
    return win_rate * rr_ratio - (1.0 - win_rate)
