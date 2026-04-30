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
  * ``hypothetical_outcome`` on rejects (legacy column, stamped by the
    pre-migration counter-factual pegger which depended on a
    pre-migration candle endpoint and was removed 2026-04-26) — the
    counter-factual label used when a row is now accepted. Pre-migration
    rows still carry these stamps; post-migration rows are NULL until
    a Bybit-native pegger is written.

Replay CANNOT:
  * Re-weight pillars (Pass 2 — when pillar-score column has data).
  * Recompute confluence from new weights.
  * Re-simulate SL/TP fill geometry — the peg-outcome column is a
    conservative "would have hit TP first" flag, not a simulated fill.

R-estimate constants for accepted-from-reject rows:
  * ``WIN`` → +1.5R. Deliberately below the bot's 1:2 hard TP because
    the legacy pegger only tells us "TP would have hit first",
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

from src.data.kline_cache import KlineCache
from src.execution.bybit_client import _INTERNAL_TO_BYBIT_SYMBOL
from src.journal.models import RejectedSignal, TradeOutcome, TradeRecord
from src.strategy.kline_walk import (
    PegResult,
    signal_ts_to_bar_start_ms,
    walk_klines,
)


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

    # 2026-04-22 hook, 2026-04-30 (Pass 3 Faz-A) ACTIVE: per-pillar score
    # multiplier applied to `confluence_pillar_scores` JSON dict at replay
    # time. cfg.pillar_weights[factor_name] = multiplier (0.0=disable,
    # 1.0=baseline, 2.0=2x). Empty dict → fall back to stored
    # confluence_score (Pass 1 behavior).
    pillar_weights: dict[str, float] = field(default_factory=dict)

    # 2026-04-30 (Pass 3 Faz-A) — replay re-walk knobs.
    # ``target_rr_ratio`` set → recompute proposed_tp from each reject's
    # proposed_sl_price + reject.price using new RR, then re-walk Bybit
    # klines via ``kline_cache`` to derive a fresh hypothetical_outcome.
    # None → use the row's stored hypothetical_outcome (Pass 1 behavior).
    target_rr_ratio: Optional[float] = None
    # ``zone_max_wait_bars`` set + > zone_max_wait_baseline → reject_reason
    # == "zone_timeout_cancel" rows ACCEPTED (treated like a gate-toggle:
    # extended wait would have fired the fill before cancel). hypothetical
    # outcome already pegged from signal_ts forward-walk; we just unblock.
    zone_max_wait_bars: Optional[int] = None
    zone_max_wait_baseline: int = 2

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


# ── Pillar reweighting helper ──────────────────────────────────────────────


def _effective_score(
    *,
    stored_score: float,
    pillar_scores: Optional[dict[str, float]],
    pillar_weights: dict[str, float],
) -> float:
    """Apply per-pillar multiplier to a row's score.

    pillar_weights[factor_name] = multiplier (0.0 = disable, 1.0 =
    baseline, 2.0 = 2x). Factors NOT in pillar_weights keep their stored
    weight (multiplier=1.0). Empty pillar_weights → return stored_score
    unchanged (Pass 1 fallback).

    pillar_scores dict comes from `confluence_pillar_scores` JSON
    column. Empty / None → fall back to stored_score even if
    pillar_weights set (the row was written before Pass 2
    instrumentation).
    """
    if not pillar_weights:
        return stored_score
    if not pillar_scores:
        return stored_score  # nothing to reweight
    return sum(
        weight * pillar_weights.get(factor, 1.0)
        for factor, weight in pillar_scores.items()
    )


# ── Bybit-symbol helper for re-walk ────────────────────────────────────────


def _to_bybit_symbol(internal: str) -> str:
    return _INTERNAL_TO_BYBIT_SYMBOL.get(internal, internal)


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
        * Pass 3 Faz-A: pillar reweighting via ``cfg.pillar_weights``.
          Empty dict → falls back to stored ``confluence_score``.
    """
    thr = cfg.threshold_for(trade.symbol)
    score = _effective_score(
        stored_score=float(trade.confluence_score or 0.0),
        pillar_scores=trade.confluence_pillar_scores,
        pillar_weights=cfg.pillar_weights,
    )
    if score < thr:
        return (False, "FILTERED", 0.0)
    outcome_str = trade.outcome.value if isinstance(trade.outcome, TradeOutcome) else str(trade.outcome)
    pnl_r = trade.pnl_r if trade.pnl_r is not None else 0.0
    return (True, outcome_str, pnl_r)


def _rewalk_with_new_target_rr(
    reject: RejectedSignal,
    cfg: ConfigOverride,
    *,
    kline_cache: KlineCache,
    interval_minutes: int,
    walk_max_bars: int,
) -> Optional[PegResult]:
    """Recompute proposed_tp from cfg.target_rr_ratio and re-walk klines.

    Returns None when the rewalk cannot run (missing inputs); caller
    falls back to stored ``hypothetical_outcome``.
    """
    if (cfg.target_rr_ratio is None
            or reject.proposed_sl_price is None
            or reject.price is None
            or reject.signal_timestamp is None):
        return None
    direction = reject.direction.value if hasattr(reject.direction, "value") else str(reject.direction)
    price = float(reject.price)
    sl = float(reject.proposed_sl_price)
    if direction == "BULLISH":
        sl_distance = price - sl
        if sl_distance <= 0:
            return None
        tp = price + sl_distance * cfg.target_rr_ratio
    elif direction == "BEARISH":
        sl_distance = sl - price
        if sl_distance <= 0:
            return None
        tp = price - sl_distance * cfg.target_rr_ratio
    else:
        return None
    bybit_symbol = _to_bybit_symbol(reject.symbol)
    start_ms = signal_ts_to_bar_start_ms(
        reject.signal_timestamp, interval_minutes=interval_minutes,
    )
    klines = kline_cache.get(
        bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
        start_ms=start_ms, max_bars=walk_max_bars,
    )
    if klines is None:
        return None  # cache miss — caller falls back to stored outcome
    return walk_klines(
        direction=direction, proposed_sl_price=sl,
        proposed_tp_price=tp, klines=klines, max_bars=walk_max_bars,
    )


def simulate_reject_outcome(
    reject: RejectedSignal,
    cfg: ConfigOverride,
    *,
    kline_cache: Optional[KlineCache] = None,
    interval_minutes: int = 3,
    walk_max_bars: int = 100,
) -> tuple[bool, str, float]:
    """Replay one historical rejected signal under a hypothetical config.

    Acceptance paths:
      1. ``reject_reason`` is a gate name and that gate is now disabled.
      2. ``reject_reason == 'below_confluence'`` and the new (optionally
         pillar-reweighted) score >= threshold for this symbol.
      3. ``reject_reason == 'zone_timeout_cancel'`` and
         ``cfg.zone_max_wait_bars > cfg.zone_max_wait_baseline`` —
         extended wait would have fired the fill before cancel; we
         unblock and apply the stored hypothetical_outcome (already
         pegged from signal_ts forward-walk).
      4. None of the above → still rejected.

    Outcome R:
      * If ``cfg.target_rr_ratio`` set + ``kline_cache`` provided +
        reject has proposed_sl_price/price/signal_timestamp → re-walk
        Bybit klines with recomputed proposed_tp → fresh outcome.
      * Otherwise → row's stored ``hypothetical_outcome`` (Pass 2.5 peg).
      * Missing / NEITHER → 0R.
    """
    now_accepted = False

    # Path 1 — gate toggle.
    gate_attr = _GATE_OVERRIDE_MAP.get(reject.reject_reason)
    if gate_attr is not None and not getattr(cfg, gate_attr):
        now_accepted = True

    # Path 2 — confluence threshold loosening (with optional pillar reweight).
    if reject.reject_reason == "below_confluence":
        thr = cfg.threshold_for(reject.symbol)
        score = _effective_score(
            stored_score=float(reject.confluence_score or 0.0),
            pillar_scores=reject.confluence_pillar_scores,
            pillar_weights=cfg.pillar_weights,
        )
        if score >= thr:
            now_accepted = True

    # Path 3 — zone_max_wait_bars extended unblocks zone_timeout_cancel.
    if (reject.reject_reason == "zone_timeout_cancel"
            and cfg.zone_max_wait_bars is not None
            and cfg.zone_max_wait_bars > cfg.zone_max_wait_baseline):
        now_accepted = True

    if not now_accepted:
        return (False, "STILL_REJECTED", 0.0)

    # Outcome resolution: prefer fresh re-walk if cfg.target_rr_ratio +
    # kline_cache available; else fall back to stored hypothetical_outcome.
    rewalk: Optional[PegResult] = None
    if cfg.target_rr_ratio is not None and kline_cache is not None:
        rewalk = _rewalk_with_new_target_rr(
            reject, cfg,
            kline_cache=kline_cache,
            interval_minutes=interval_minutes,
            walk_max_bars=walk_max_bars,
        )
    if rewalk is not None and rewalk.outcome in ("WIN", "LOSS", "TIMEOUT"):
        if rewalk.outcome == "WIN":
            return (True, "WIN", cfg.win_r_estimate)
        if rewalk.outcome == "LOSS":
            return (True, "LOSS", cfg.loss_r_estimate)
        return (True, "NEITHER", cfg.neither_r_estimate)  # TIMEOUT → no R booked

    # Fallback: stored peg outcome
    hypo = reject.hypothetical_outcome
    if hypo == "WIN":
        return (True, "WIN", cfg.win_r_estimate)
    if hypo == "LOSS":
        return (True, "LOSS", cfg.loss_r_estimate)
    return (True, "NEITHER", cfg.neither_r_estimate)


def replay_config(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    cfg: ConfigOverride,
    *,
    kline_cache: Optional[KlineCache] = None,
    interval_minutes: int = 3,
    walk_max_bars: int = 100,
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

    Pass 3 Faz-A knobs (cfg.pillar_weights / cfg.target_rr_ratio /
    cfg.zone_max_wait_bars) are applied automatically inside the
    per-row simulate_*. ``kline_cache`` is forwarded to reject re-walk
    when ``cfg.target_rr_ratio`` is set; if the cache is absent the
    reject falls back to its stored ``hypothetical_outcome``.
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
        ok, outcome, pnl_r = simulate_reject_outcome(
            reject, cfg,
            kline_cache=kline_cache,
            interval_minutes=interval_minutes,
            walk_max_bars=walk_max_bars,
        )
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


# ── Pass 2 scaffold (Pass 3 Faz-A: now lives inside replay_config) ─────────


def replay_with_pillar_reweight(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    cfg: ConfigOverride,
    *,
    kline_cache: Optional[KlineCache] = None,
    interval_minutes: int = 3,
    walk_max_bars: int = 100,
) -> DatasetMetrics:
    """Backward-compat alias for replay_config (Pass 3 Faz-A).

    Pass 2 hook scaffolding; pillar reweighting is now applied
    transparently by ``replay_config`` itself via the active
    ``cfg.pillar_weights`` field. This thin wrapper preserves the
    public API for any caller that imported the Pass 2 entry point
    directly. Prefer ``replay_config`` in new code.
    """
    return replay_config(
        trades, rejects, cfg,
        kline_cache=kline_cache,
        interval_minutes=interval_minutes,
        walk_max_bars=walk_max_bars,
    )


__all__ = [
    "ConfigOverride",
    "DatasetMetrics",
    "simulate_trade_outcome",
    "simulate_reject_outcome",
    "replay_config",
    "replay_with_pillar_reweight",
]
