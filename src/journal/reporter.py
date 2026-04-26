"""Performance metrics over `TradeRecord` lists — pure, sync, no I/O.

All functions take the list of *closed* trades already filtered for you
(open/canceled rows are a reporting error — the caller should have filtered
them out via `TradeJournal.list_closed_trades`).

Sharpe here is intentionally un-annualized. The RL loop in Phase 6 uses it
as a shape signal, not as a finance-standard risk-adjusted return number.
If we need finance-standard Sharpe later, we add it without breaking this.
"""

from __future__ import annotations

import math
from typing import Iterable

from src.journal.models import TradeOutcome, TradeRecord


# ── Helpers ─────────────────────────────────────────────────────────────────


def _wins(trades: Iterable[TradeRecord]) -> list[TradeRecord]:
    return [t for t in trades if t.outcome == TradeOutcome.WIN]


def _losses(trades: Iterable[TradeRecord]) -> list[TradeRecord]:
    return [t for t in trades if t.outcome == TradeOutcome.LOSS]


# ── Rates ───────────────────────────────────────────────────────────────────


def win_rate(closed: list[TradeRecord]) -> float:
    """Fraction in [0, 1]. Empty list returns 0.0."""
    if not closed:
        return 0.0
    return len(_wins(closed)) / len(closed)


def win_rate_by_session(closed: list[TradeRecord]) -> dict[str, float]:
    """Win rate bucketed by the `session` field (None → 'UNKNOWN')."""
    buckets: dict[str, list[TradeRecord]] = {}
    for t in closed:
        key = t.session or "UNKNOWN"
        buckets.setdefault(key, []).append(t)
    return {k: win_rate(v) for k, v in buckets.items()}


def win_rate_by_factor(closed: list[TradeRecord]) -> dict[str, float]:
    """A trade tagged with N factors counts once per factor.

    Useful for answering "which confluence factors actually correlate with wins"
    — the output dict is keyed by factor label, valued by per-factor win rate.
    """
    buckets: dict[str, list[TradeRecord]] = {}
    for t in closed:
        for factor in t.confluence_factors:
            buckets.setdefault(factor, []).append(t)
    return {k: win_rate(v) for k, v in buckets.items()}


def regime_breakdown(
    closed: list[TradeRecord],
) -> dict[str, dict[str, float]]:
    """Per-derivatives-regime stats. Keys: regime label; values: dict with
    num_trades / win_rate / avg_r / expectancy_r. Trades with no regime tag
    bucket into 'UNKNOWN' so they stay visible rather than silently dropping.

    2026-04-27 — `regime_at_entry` (DerivativesRegime classifier output)
    was dropped from TradeRecord (always returned 'BALANCED' on the
    Bybit dataset, 1-distinct constant). `trend_regime_at_entry` (ADX
    classifier — 3-distinct: RANGING / WEAK_TREND / STRONG_TREND) is
    the live regime signal and replaces the dropped column for this
    bucket-by-regime breakdown.
    """
    buckets: dict[str, list[TradeRecord]] = {}
    for t in closed:
        key = (getattr(t, "trend_regime_at_entry", None) or "UNKNOWN")
        buckets.setdefault(key, []).append(t)
    return {
        regime: {
            "num_trades": len(records),
            "win_rate": win_rate(records),
            "avg_r": avg_r(records),
            "expectancy_r": expectancy_r(records),
        }
        for regime, records in buckets.items()
    }


def win_rate_by_symbol(
    closed: list[TradeRecord],
) -> dict[str, dict[str, float]]:
    """Per-symbol WR + expectancy. Same shape as `regime_breakdown`.

    With 5 pairs running, per-symbol tears are invisible in the global
    win_rate — a 70% BTC paired with a 30% DOGE averages to 'decent' and
    hides the losing leg.
    """
    buckets: dict[str, list[TradeRecord]] = {}
    for t in closed:
        buckets.setdefault(t.symbol, []).append(t)
    return {
        sym: {
            "num_trades": len(records),
            "win_rate": win_rate(records),
            "avg_r": avg_r(records),
            "expectancy_r": expectancy_r(records),
        }
        for sym, records in buckets.items()
    }


def win_rate_by_factor_combo(
    closed: list[TradeRecord], *, min_trades: int = 2,
) -> dict[str, dict[str, float]]:
    """WR keyed by the *set* of factors that fired together.

    Key is a comma-joined, sorted string of factor names — deterministic
    across runs, JSON-safe for the RL feature pipeline. Combos seen fewer
    than `min_trades` times are pooled under 'RARE' so long-tail noise
    doesn't drown the signal. Bumping `min_trades` is the knob to demand
    more evidence per combo.
    """
    buckets: dict[str, list[TradeRecord]] = {}
    for t in closed:
        combo_key = ",".join(sorted(t.confluence_factors)) or "NONE"
        buckets.setdefault(combo_key, []).append(t)
    out: dict[str, dict[str, float]] = {}
    rare: list[TradeRecord] = []
    for combo, records in buckets.items():
        if len(records) < min_trades:
            rare.extend(records)
            continue
        out[combo] = {
            "num_trades": len(records),
            "win_rate": win_rate(records),
            "avg_r": avg_r(records),
        }
    if rare:
        out["RARE"] = {
            "num_trades": len(rare),
            "win_rate": win_rate(rare),
            "avg_r": avg_r(rare),
        }
    return out


_DEFAULT_SCORE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 2.0),
    (2.0, 3.0),
    (3.0, 4.0),
    (4.0, 5.0),
    (5.0, float("inf")),
)


def _bucket_label(low: float, high: float) -> str:
    if high == float("inf"):
        return f"{low:.1f}+"
    return f"{low:.1f}-{high:.1f}"


def win_rate_by_score_bucket(
    closed: list[TradeRecord],
    *,
    buckets: tuple[tuple[float, float], ...] = _DEFAULT_SCORE_BUCKETS,
) -> dict[str, dict[str, float]]:
    """Bucket `confluence_score` into ranges → per-bucket stats.

    Answers the single question "does higher confluence actually produce
    better outcomes?" on the clean dataset. Buckets are half-open
    `[low, high)` so adjacent ranges don't double-count. Empty buckets still
    emit a key with num_trades=0 so the RL loop reads a stable shape every
    window.
    """
    out: dict[str, dict[str, float]] = {}
    for low, high in buckets:
        label = _bucket_label(low, high)
        records = [t for t in closed if low <= t.confluence_score < high]
        out[label] = {
            "num_trades": len(records),
            "win_rate": win_rate(records),
            "avg_r": avg_r(records),
        }
    return out


# ── R-multiples ─────────────────────────────────────────────────────────────


def _r_list(closed: list[TradeRecord]) -> list[float]:
    return [t.pnl_r for t in closed if t.pnl_r is not None]


def avg_r(closed: list[TradeRecord]) -> float:
    rs = _r_list(closed)
    return sum(rs) / len(rs) if rs else 0.0


def expectancy_r(closed: list[TradeRecord]) -> float:
    """Same math as avg_r; kept as a named alias because 'expectancy' is the
    word traders use when talking about per-trade EV in R units."""
    return avg_r(closed)


# ── P/L-weighted ────────────────────────────────────────────────────────────


def profit_factor(closed: list[TradeRecord]) -> float:
    """sum(wins_usdt) / |sum(losses_usdt)|. `inf` if no losses, 0.0 if no wins."""
    wins_sum = sum(t.pnl_usdt or 0.0 for t in _wins(closed))
    losses_sum = sum(t.pnl_usdt or 0.0 for t in _losses(closed))
    if losses_sum == 0:
        return math.inf if wins_sum > 0 else 0.0
    return wins_sum / abs(losses_sum)


# ── Streaks ─────────────────────────────────────────────────────────────────


def _max_streak(closed: list[TradeRecord], predicate) -> int:
    best = cur = 0
    for t in closed:
        if predicate(t):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def max_consecutive_wins(closed: list[TradeRecord]) -> int:
    return _max_streak(closed, lambda t: t.outcome == TradeOutcome.WIN)


def max_consecutive_losses(closed: list[TradeRecord]) -> int:
    return _max_streak(closed, lambda t: t.outcome == TradeOutcome.LOSS)


# ── Equity / drawdown ───────────────────────────────────────────────────────


def equity_curve(closed: list[TradeRecord], starting_balance: float) -> list[float]:
    """Running balance after each trade's PnL (including fees)."""
    curve = [starting_balance]
    balance = starting_balance
    for t in closed:
        balance += (t.pnl_usdt or 0.0) - (t.fees_usdt or 0.0)
        curve.append(balance)
    return curve


def max_drawdown(
    closed: list[TradeRecord], starting_balance: float,
) -> tuple[float, float]:
    """Return (dd_usdt, dd_pct) — the largest peak-to-trough drop in the equity
    curve. Both numbers are non-negative; dd_pct is relative to the peak."""
    if starting_balance <= 0:
        return 0.0, 0.0
    curve = equity_curve(closed, starting_balance)
    peak = curve[0]
    dd_usdt = 0.0
    dd_pct = 0.0
    for val in curve:
        peak = max(peak, val)
        drop = peak - val
        if drop > dd_usdt:
            dd_usdt = drop
            dd_pct = drop / peak * 100.0 if peak > 0 else 0.0
    return dd_usdt, dd_pct


# ── Risk-adjusted ───────────────────────────────────────────────────────────


def sharpe_r(closed: list[TradeRecord]) -> float:
    """Un-annualized per-trade Sharpe on R-multiples.

    Returns 0.0 if <2 trades or std is zero — we don't want the RL reward
    signal to blow up on the first trade.
    """
    rs = _r_list(closed)
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std


def calmar(closed: list[TradeRecord], starting_balance: float) -> float:
    """total_return_pct / max_dd_pct. 0.0 when DD is zero or balance invalid."""
    if starting_balance <= 0 or not closed:
        return 0.0
    ending = equity_curve(closed, starting_balance)[-1]
    total_return_pct = (ending - starting_balance) / starting_balance * 100.0
    _, dd_pct = max_drawdown(closed, starting_balance)
    if dd_pct == 0:
        return 0.0
    return total_return_pct / dd_pct


# ── Summary / formatting ────────────────────────────────────────────────────


def summary(closed: list[TradeRecord], starting_balance: float) -> dict:
    """Bundle every metric into a single dict — what the CLI prints, and what
    the RL training loop will consume as a features/label row."""
    dd_usdt, dd_pct = max_drawdown(closed, starting_balance)
    ending = equity_curve(closed, starting_balance)[-1] if closed else starting_balance
    wins = _wins(closed)
    losses = _losses(closed)
    return {
        "num_trades": len(closed),
        "num_wins": len(wins),
        "num_losses": len(losses),
        "win_rate": win_rate(closed),
        "avg_r": avg_r(closed),
        "expectancy_r": expectancy_r(closed),
        "profit_factor": profit_factor(closed),
        "max_consecutive_wins": max_consecutive_wins(closed),
        "max_consecutive_losses": max_consecutive_losses(closed),
        "max_drawdown_usdt": dd_usdt,
        "max_drawdown_pct": dd_pct,
        "sharpe_r": sharpe_r(closed),
        "calmar": calmar(closed, starting_balance),
        "starting_balance": starting_balance,
        "ending_balance": ending,
        "total_return_pct": (
            (ending - starting_balance) / starting_balance * 100.0
            if starting_balance > 0 else 0.0
        ),
        "win_rate_by_session": win_rate_by_session(closed),
        "win_rate_by_factor": win_rate_by_factor(closed),
        "regime_breakdown": regime_breakdown(closed),
        "win_rate_by_symbol": win_rate_by_symbol(closed),
        "win_rate_by_factor_combo": win_rate_by_factor_combo(closed),
        "win_rate_by_score_bucket": win_rate_by_score_bucket(closed),
    }


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _fmt_pf(x: float) -> str:
    return "inf" if x == math.inf else f"{x:.2f}"


def format_summary(s: dict) -> str:
    """Render `summary()` output as a plain-text report. No Rich dependency."""
    lines = []
    lines.append("=" * 60)
    lines.append("  Trade journal report")
    lines.append("=" * 60)
    lines.append(f"  Trades:               {s['num_trades']}  "
                 f"(W={s['num_wins']} / L={s['num_losses']})")
    lines.append(f"  Win rate:             {_fmt_pct(s['win_rate'])}")
    lines.append(f"  Avg R:                {s['avg_r']:+.3f}R")
    lines.append(f"  Expectancy:           {s['expectancy_r']:+.3f}R")
    lines.append(f"  Profit factor:        {_fmt_pf(s['profit_factor'])}")
    lines.append(f"  Max consec wins:      {s['max_consecutive_wins']}")
    lines.append(f"  Max consec losses:    {s['max_consecutive_losses']}")
    lines.append(f"  Max drawdown:         {s['max_drawdown_usdt']:.2f} USDT  "
                 f"({s['max_drawdown_pct']:.2f}%)")
    lines.append(f"  Sharpe (R, per-trade): {s['sharpe_r']:+.3f}")
    lines.append(f"  Calmar:               {s['calmar']:+.3f}")
    lines.append(f"  Balance:              {s['starting_balance']:.2f} -> "
                 f"{s['ending_balance']:.2f}  "
                 f"({s['total_return_pct']:+.2f}%)")
    if s["win_rate_by_session"]:
        lines.append("")
        lines.append("  Win rate by session:")
        for name, rate in sorted(s["win_rate_by_session"].items()):
            lines.append(f"    {name:<12} {_fmt_pct(rate)}")
    if s["win_rate_by_factor"]:
        lines.append("")
        lines.append("  Win rate by confluence factor:")
        for name, rate in sorted(s["win_rate_by_factor"].items()):
            lines.append(f"    {name:<20} {_fmt_pct(rate)}")
    if s.get("regime_breakdown"):
        lines.append("")
        lines.append("  Derivatives regime breakdown:")
        for name, stats in sorted(s["regime_breakdown"].items()):
            lines.append(
                f"    {name:<14} n={stats['num_trades']:<3}  "
                f"win={_fmt_pct(stats['win_rate'])}  "
                f"avg_r={stats['avg_r']:+.3f}R"
            )
    if s.get("win_rate_by_symbol"):
        lines.append("")
        lines.append("  Win rate by symbol:")
        rows = sorted(
            s["win_rate_by_symbol"].items(),
            key=lambda kv: kv[1]["num_trades"], reverse=True,
        )
        for name, stats in rows:
            lines.append(
                f"    {name:<18} n={stats['num_trades']:<3}  "
                f"win={_fmt_pct(stats['win_rate'])}  "
                f"avg_r={stats['avg_r']:+.3f}R"
            )
    if s.get("win_rate_by_score_bucket"):
        lines.append("")
        lines.append("  Win rate by confluence-score bucket:")
        for name, stats in s["win_rate_by_score_bucket"].items():
            lines.append(
                f"    {name:<10} n={stats['num_trades']:<3}  "
                f"win={_fmt_pct(stats['win_rate'])}  "
                f"avg_r={stats['avg_r']:+.3f}R"
            )
    if s.get("win_rate_by_factor_combo"):
        lines.append("")
        lines.append("  Win rate by factor combo (n >= 2):")
        rows = sorted(
            s["win_rate_by_factor_combo"].items(),
            key=lambda kv: kv[1]["num_trades"], reverse=True,
        )
        for name, stats in rows:
            lines.append(
                f"    n={stats['num_trades']:<3}  "
                f"win={_fmt_pct(stats['win_rate'])}  "
                f"avg_r={stats['avg_r']:+.3f}R  {name}"
            )
    lines.append("=" * 60)
    return "\n".join(lines)
