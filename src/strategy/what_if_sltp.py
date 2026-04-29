"""What-if SL/TP target computation for rejected setups (Pass 2.5).

Pure function: takes (symbol, direction, price, atr, reject_reason,
floor_pct, target_rr) and returns the (sl, tp, rr) the strategy would
have planned if the setup were accepted. ``scripts/peg_rejected_outcomes.py``
forward-walks Bybit klines from these targets to flag each row
WIN/LOSS/TIMEOUT (counter-factual outcome).

Two callers:
  - ``src/bot/runner.py::BotRunner._compute_what_if_proposed_sltp`` —
    live reject path; runner pulls floor_pct + target_rr from its
    ``ctx.config`` and forwards them here.
  - ``scripts/backfill_proposed_sl_tp.py`` — retroactive backfill for
    pre-Pass-2.5 reject rows that were never stamped at insert time.

Both must use IDENTICAL math so the live insert path and the backfill
produce the same proposed SL/TP for the same input row — otherwise the
pegger's WIN/LOSS distribution would split spuriously by row vintage.
"""
from __future__ import annotations

from typing import Optional

from src.data.models import Direction


# Reject reasons where the strategy short-circuits BEFORE any SL hierarchy
# fires (no price target ever computed). Pegger has no targets to walk
# against → leave proposed_* NULL and the row is skipped.
NO_PROPOSED_SLTP_REASONS: frozenset[str] = frozenset({
    "no_setup_zone", "no_sl_source", "zero_contracts", "tp_too_tight",
    "session_filter", "macro_event_blackout", "crowded_skip",
    "vwap_reset_blackout",
})


def compute_what_if_proposed_sltp(
    *,
    symbol: str,  # noqa: ARG001 — kept for forward-compat (e.g. per-symbol multipliers)
    direction: Direction,
    price: Optional[float],
    atr: Optional[float],
    reject_reason: str,
    floor_pct: float,
    target_rr: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """ATR-based what-if SL/TP for pre-fill rejects.

    SL distance: ``max(atr × 1.5, price × floor_pct)`` — ATR-aware but
    floored by the same per-symbol minimum SL distance used for live SL
    widening. TP distance: ``sl_distance × target_rr`` (default 1.5 if
    target_rr is None / 0 — matches runtime fallback).

    Returns (None, None, None) when proposed SL/TP cannot be derived:
      - reject_reason in NO_PROPOSED_SLTP_REASONS
      - direction == UNDEFINED
      - price or atr missing / zero
    """
    if reject_reason in NO_PROPOSED_SLTP_REASONS:
        return None, None, None
    if direction == Direction.UNDEFINED:
        return None, None, None
    if not price or not atr:
        return None, None, None
    atr_distance = float(atr) * 1.5
    floor_distance = float(price) * float(floor_pct)
    sl_distance = max(atr_distance, floor_distance)
    rr = float(target_rr or 0.0) or 1.5
    if direction == Direction.BULLISH:
        sl = float(price) - sl_distance
        tp = float(price) + sl_distance * rr
    else:
        sl = float(price) + sl_distance
        tp = float(price) - sl_distance * rr
    return float(sl), float(tp), float(rr)
