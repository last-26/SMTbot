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
  - When the leverage cap binds, we SHRINK the position. Actual risk then
    ends up below the requested risk — never above.
  - OKX BTC-USDT-SWAP: 1 contract = 0.01 BTC notional. Integer contracts only.

This module is pure: no I/O, no async, safe to import from anywhere.
"""

from __future__ import annotations

from src.data.models import Direction
from src.strategy.trade_plan import TradePlan


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
    sl_source: str = "",
    confluence_score: float = 0.0,
    confluence_factors: list[str] | None = None,
    reason: str = "",
) -> TradePlan:
    """Compute a fully-sized TradePlan.

    Args:
        direction: BULLISH (long) or BEARISH (short).
        entry_price: intended entry.
        sl_price: stop-loss price (below entry for longs, above for shorts).
        account_balance: free USDT available in the trading account.
        risk_pct: fraction of `account_balance` to risk (0.01 = 1%).
        rr_ratio: TP distance / SL distance.
        max_leverage: hard cap on leverage (from circuit breakers).
        contract_size: BTC per OKX contract (BTC-USDT-SWAP = 0.01).
        sl_source: label for journal/telemetry ("order_block", "fvg", …).
        confluence_score: score that led to this trade (for journal).
        confluence_factors: names of factors that contributed (for journal).
        reason: free-text summary.

    Returns:
        TradePlan with every field populated.
    """
    _validate(direction, entry_price, sl_price, account_balance,
              risk_pct, rr_ratio, max_leverage, contract_size)

    sl_distance = abs(entry_price - sl_price)
    sl_pct = sl_distance / entry_price

    max_risk_usdt = account_balance * risk_pct

    if direction == Direction.BULLISH:
        tp_price = entry_price + sl_distance * rr_ratio
    else:
        tp_price = entry_price - sl_distance * rr_ratio

    # Ideal notional so that SL hit loses exactly max_risk_usdt.
    ideal_notional = max_risk_usdt / sl_pct
    required_leverage = ideal_notional / account_balance

    capped = required_leverage > max_leverage
    if capped:
        # Shrink notional to fit the leverage cap. Actual risk drops below target.
        notional = account_balance * max_leverage
    else:
        notional = ideal_notional

    # Round leverage up to at least 1, cap at max_leverage. Using round() here
    # is fine because notional drives actual risk, not the reported leverage.
    leverage = min(max(1, round(required_leverage)), max_leverage)

    # Integer OKX contracts — always round DOWN so we never exceed notional.
    contracts_unit_usdt = contract_size * entry_price
    num_contracts = int(notional // contracts_unit_usdt)
    # Re-derive actual notional from rounded contracts so downstream risk is exact.
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
        sl_source=sl_source,
        confluence_score=confluence_score,
        confluence_factors=list(confluence_factors or []),
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
