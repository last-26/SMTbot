"""Pure kline forward-walk helpers shared by pegger + replay tune.

Extracted from ``scripts/peg_rejected_outcomes.py`` 2026-04-30 (Pass 3.2.2)
so ``scripts/replay_decisions.py`` can re-walk Bybit klines against
recomputed proposed_sl/tp targets per Optuna trial without depending on
a sibling script's private helper.

Algorithm (pure function — heavily unit-tested in test_peg_rejected_outcomes):

For LONG:
    For bar in klines[:max_bars]:
        if bar.low  <= sl: return LOSS, bars_to_sl=offset
        if bar.high >= tp: return WIN,  bars_to_tp=offset
    return TIMEOUT

SHORT mirrors. Same-bar SL+TP collision → LOSS (pessimistic worst-case
fill assumption; intra-bar tick order can't be recovered from candles).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.data.kline_cache import Kline


@dataclass(frozen=True)
class PegResult:
    """Walk outcome envelope.

    `outcome` is one of ``WIN`` / ``LOSS`` / ``TIMEOUT`` / ``SKIP``.
    Bar offsets are 0-indexed and only the matching side is set; the
    other stays None — peg's "didn't happen" signal.
    """
    outcome: str
    bars_to_tp: Optional[int] = None
    bars_to_sl: Optional[int] = None
    skip_reason: Optional[str] = None


def walk_klines(
    *,
    direction: str,
    proposed_sl_price: float,
    proposed_tp_price: float,
    klines: list[Kline],
    max_bars: int = 100,
) -> PegResult:
    """Walk klines forward, return WIN/LOSS/TIMEOUT.

    ``klines`` MUST be sorted ASC by ``bar_start_ms`` and start with the
    first bar AFTER ``signal_timestamp`` (caller drops the placement bar).
    Same-bar SL+TP collision resolves pessimistic (SL first).
    """
    if not klines:
        return PegResult(outcome="SKIP", skip_reason="no_klines")
    is_long = direction == "BULLISH"
    walked = 0
    for bar in klines[:max_bars]:
        walked += 1
        if is_long:
            sl_hit = bar.low <= proposed_sl_price
            tp_hit = bar.high >= proposed_tp_price
        else:
            sl_hit = bar.high >= proposed_sl_price
            tp_hit = bar.low <= proposed_tp_price
        if sl_hit:
            return PegResult(outcome="LOSS", bars_to_sl=walked - 1)
        if tp_hit:
            return PegResult(outcome="WIN", bars_to_tp=walked - 1)
    return PegResult(outcome="TIMEOUT")


def signal_ts_to_bar_start_ms(
    signal_ts: datetime, *, interval_minutes: int,
) -> int:
    """Floor `signal_ts` to its bar's open time, then add 1 bar.

    The placement bar (bar containing signal_ts) is excluded — pegger /
    replay walks from the NEXT bar onward to avoid synthetic same-bar
    fill + SL hit attribution.
    """
    bar_ms = interval_minutes * 60 * 1000
    epoch_ms = int(signal_ts.timestamp() * 1000)
    bar_start = (epoch_ms // bar_ms) * bar_ms
    return bar_start + bar_ms


__all__ = ["PegResult", "walk_klines", "signal_ts_to_bar_start_ms"]
