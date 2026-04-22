"""Pure replay library — apply a hypothetical config to a historical dataset.

Pass 1 scope (2026-04-22): non-Arkham knob tuning. Given a closed-trade or
rejected-signal row plus a ``ConfigOverride`` dict, return whether the row
would have been accepted under the new config and what R it would have
booked.

Replay is DATA-LIMITED. The journal stores:
  * ``confluence_score`` (float) + ``confluence_factors`` (list[str] names)
    — enough to apply a new confluence threshold.
  * ``confluence_pillar_scores`` (JSON dict, new 2026-04-22 column) —
    empty on pre-migration rows, so per-pillar re-weighting is a Pass 2
    problem. We keep the field in ``ConfigOverride`` as a hook, but Pass 1
    never reads it.
  * ``reject_reason`` on rejects — enough to flip a gate off and accept
    rows previously rejected for that reason.
  * ``hypothetical_outcome`` on rejects (stamped by
    ``peg_rejected_outcomes.py``) — the counter-factual label used when
    a row is now accepted.

Replay CANNOT:
  * Re-weight pillars (Pass 2 — when pillar-score column has data).
  * Recompute confluence from new weights.
  * Re-simulate SL/TP fill geometry — the peg-outcome column is a
    conservative "would have hit TP first" flag, not a simulated fill.

R-estimate constants for accepted-from-reject rows:
  * ``WIN`` → +1.5R. Deliberately below the bot's 1:2 hard TP because
    peg_rejected_outcomes.py only tells us "TP would have hit first",
    not "with what slippage / TP1 partial / early-close on
    ltf_reversal". Operator can tune via ``win_r_estimate`` override
    if Phase 9 suggests otherwise.
  * ``LOSS`` → -1.0R. Full stop-out assumption — the peg flag already
    told us SL hit first, so slippage only makes it worse, not better.
  * ``NEITHER`` / ``None`` → 0.0R. No R booked; treated as a filtered
    trade for Sharpe / DD purposes (it wouldn't have closed within
    the N-bar counter-factual window).

These constants were chosen conservatively — an aggressive WIN=+1.8R
would inflate net_r on configs that unblock many rejects. If Pass 1's
best config turns out to lean heavily on gate-flips, re-audit with
``--win-r 1.2`` to sanity-check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from src.journal.models import RejectedSignal, TradeOutcome, TradeRecord


# ── Config override shape ───────────────────────────────────────────────────


@dataclass
class ConfigOverride:
    """Non-Arkham tunable knobs for Pass 1 replay.

    All fields are optional with replay-safe defaults that mirror the
    current ``config/default.yaml`` state:
      * global threshold 2.0 (data-collection loose gate)
      * every hard gate enabled (match today's runtime)
      * WIN=+1.5R / LOSS=-1.0R / NEITHER=0.0 R-estimate constants
    """

    # Primary knob — confluence threshold. Per-symbol override wins when
    # present; otherwise the global value applies.
    confluence_threshold_global: float = 2.0
    confluence_threshold_per_symbol: dict[str, float] = field(default_factory=dict)

    # Hard-gate toggles. Flip to False and rejected_signals rows whose
    # reject_reason maps to that gate become ACCEPTED with their
    # hypothetical_outcome applied.
    vwap_hard_veto_enabled: bool = True
    ema_veto_enabled: bool = True
    cross_asset_opposition_enabled: bool = True

    # R-estimate constants for rejected rows that now pass. See module
    # docstring for conservative-default rationale.
    win_r_estimate: float = 1.5
    loss_r_estimate: float = -1.0
    neither_r_estimate: float = 0.0

    # Pass 2 hook: empty dict today (journal column is almost entirely
    # empty on the 41-trade dataset). Present so tune_confluence.py can
    # plumb values in without changing the replay signature.
    pillar_weights: dict[str, float] = field(default_factory=dict)

    def threshold_for(self, symbol: str) -> float:
        """Return the effective threshold for a symbol, falling back to
        the global value. Keeps the call-site readable in the hot path."""
        if symbol in self.confluence_threshold_per_symbol:
            return self.confluence_threshold_per_symbol[symbol]
        return self.confluence_threshold_global


# ── Gate-name → override-flag mapping ───────────────────────────────────────

# Map the rejected_signals.reject_reason string to the ``ConfigOverride``
# attribute that controls it. Flipping the attribute to False converts a
# row carrying that reject_reason into an acceptance.
_GATE_OVERRIDE_MAP: dict[str, str] = {
    "vwap_misaligned": "vwap_hard_veto_enabled",
    "ema_momentum_contra": "ema_veto_enabled",
    "cross_asset_opposition": "cross_asset_opposition_enabled",
}


# ── Aggregated metrics ──────────────────────────────────────────────────────


@dataclass
class DatasetMetrics:
    """Scalar summary of a replay run.

    Attributes are deliberately flat so tune_confluence.py can toss them
    at the Optuna objective without marshalling. ``sharpe_r`` is the
    per-trade Sharpe-like ratio (``mean / stdev``); NaN-safe but guarded
    on very small samples.
    """

    n_trades_accepted: int
    n_wins: int
    n_losses: int
    net_r: float
    sharpe_r: float
    max_dd_r: float
    win_rate: float
    avg_r: float


def _sharpe(returns: list[float]) -> float:
    """Per-trade Sharpe surrogate. Guards on < 3 samples and zero stdev."""
    if len(returns) < 3:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(var)
    if stdev == 0.0:
        return 0.0
    return mean / stdev


def _max_drawdown_r(returns: list[float]) -> float:
    """Sequential R-level peak-to-trough drawdown. Returns non-negative R."""
    if not returns:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ── Replay primitives ───────────────────────────────────────────────────────


def simulate_trade_outcome(
    trade: TradeRecord,
    cfg: ConfigOverride,
) -> tuple[bool, str, float]:
    """Replay one historical closed trade under a hypothetical config.

    Contract:
        The trade was ACCEPTED at runtime. Under ``cfg``, does it still
        pass the confluence gate? If not, return (False, 'FILTERED', 0.0);
        if yes, return the actual outcome + actual pnl_r.

    Notes:
        * Hard-gate flips don't change accepted trades — if the runtime
          gates accepted it, the runtime already ran those gates. Only
          the confluence threshold can retroactively filter an accepted
          trade.
        * BREAKEVEN / CANCELED / OPEN rows are handled gracefully —
          OPEN is filtered by journal queries normally, but be defensive.
        * ``pnl_r is None`` on a row (shouldn't happen on a closed row
          but defensive) → treated as 0.0.
    """
    thr = cfg.threshold_for(trade.symbol)
    if trade.confluence_score < thr:
        return (False, "FILTERED", 0.0)
    outcome_str = trade.outcome.value if isinstance(trade.outcome, TradeOutcome) else str(trade.outcome)
    pnl_r = trade.pnl_r if trade.pnl_r is not None else 0.0
    return (True, outcome_str, pnl_r)


def simulate_reject_outcome(
    reject: RejectedSignal,
    cfg: ConfigOverride,
) -> tuple[bool, str, float]:
    """Replay one historical rejected signal under a hypothetical config.

    Three ways a reject can now accept:
      1. ``reject_reason`` is a gate name and that gate is now disabled.
      2. ``reject_reason == 'below_confluence'`` and the new threshold
         for this symbol is <= the reject's recorded confluence_score.
      3. None of the above → still rejected, outcome 'STILL_REJECTED', 0R.

    When accepted, R comes from the row's ``hypothetical_outcome`` field
    (stamped by ``peg_rejected_outcomes.py``). Missing / NEITHER → 0R.
    """
    now_accepted = False

    # Path 1 — gate toggle.
    gate_attr = _GATE_OVERRIDE_MAP.get(reject.reject_reason)
    if gate_attr is not None and not getattr(cfg, gate_attr):
        now_accepted = True

    # Path 2 — confluence threshold loosening.
    if reject.reject_reason == "below_confluence":
        thr = cfg.threshold_for(reject.symbol)
        if reject.confluence_score >= thr:
            now_accepted = True

    if not now_accepted:
        return (False, "STILL_REJECTED", 0.0)

    # Translate hypothetical counter-factual → R.
    hypo = reject.hypothetical_outcome
    if hypo == "WIN":
        return (True, "WIN", cfg.win_r_estimate)
    if hypo == "LOSS":
        return (True, "LOSS", cfg.loss_r_estimate)
    # NEITHER or None (unpegged) — accepted but no R booked.
    return (True, "NEITHER", cfg.neither_r_estimate)


def replay_config(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    cfg: ConfigOverride,
) -> DatasetMetrics:
    """Aggregate replay metrics across every row in the dataset.

    Trades and rejects are replayed independently and their accepted
    returns are concatenated for Sharpe / max-DD. The time-ordering of
    the returns matters for max-DD so we preserve the caller's row
    ordering (typically entry_timestamp ASC — see
    TradeJournal.list_closed_trades and list_rejected_signals).

    Empty input → all-zero metrics (no trades accepted). The objective
    function in tune_confluence.py penalises tiny samples explicitly
    so this is safe.
    """
    returns: list[float] = []
    wins = 0
    losses = 0
    accepted = 0

    for trade in trades:
        ok, outcome, pnl_r = simulate_trade_outcome(trade, cfg)
        if not ok:
            continue
        accepted += 1
        returns.append(pnl_r)
        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1

    for reject in rejects:
        ok, outcome, pnl_r = simulate_reject_outcome(reject, cfg)
        if not ok:
            continue
        accepted += 1
        returns.append(pnl_r)
        if outcome == "WIN":
            wins += 1
        elif outcome == "LOSS":
            losses += 1

    net_r = sum(returns)
    avg_r = (net_r / len(returns)) if returns else 0.0
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
    return DatasetMetrics(
        n_trades_accepted=accepted,
        n_wins=wins,
        n_losses=losses,
        net_r=net_r,
        sharpe_r=_sharpe(returns),
        max_dd_r=_max_drawdown_r(returns),
        win_rate=win_rate,
        avg_r=avg_r,
    )


# ── Pass 2 scaffold (stub — not wired in Pass 1 CLI) ───────────────────────


def replay_with_pillar_reweight(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    cfg: ConfigOverride,
) -> DatasetMetrics:
    """Pass 2 entry point — re-weight per-pillar scores before thresholding.

    Today this is a thin wrapper around ``replay_config``. Pass 2 fills
    the body: iterate rows, consult ``trade.confluence_pillar_scores``
    (now populated), multiply each pillar name by
    ``cfg.pillar_weights[name]``, recompute the total, and use that
    rescaled total as the thresholding input instead of the stored
    ``confluence_score``. Zero-risk to land today because
    ``cfg.pillar_weights`` defaults to empty dict.
    """
    if not cfg.pillar_weights:
        return replay_config(trades, rejects, cfg)
    # Pass 2 lives here. For now, behave identically to replay_config
    # so callers can plumb `pillar_weights` without breaking Pass 1.
    return replay_config(trades, rejects, cfg)


__all__ = [
    "ConfigOverride",
    "DatasetMetrics",
    "simulate_trade_outcome",
    "simulate_reject_outcome",
    "replay_config",
    "replay_with_pillar_reweight",
]
