"""Offline factor-audit report — joins `trades` + `rejected_signals`.

Phase 7.B3 — the analysis half of the rejected-signals pipeline. Reads the
journal, produces a plain-text tear sheet that answers the questions the
strategy pivot is supposed to answer:

  * Which factors actually correlate with wins on the clean dataset?
  * Which reject_reasons threw away winners (hypothetical WIN) vs. correctly
    blocked losers (hypothetical LOSS)?
  * Does the derivatives regime classifier (BALANCED/CROWDED/...) matter?
  * Does the ADX trend_regime_at_entry label (Phase 7.D3) discriminate?
  * Does raising the min_confluence threshold leave winners on the table?

Read-only; never writes. Honours `rl.clean_since` from YAML so pre-pivot dirty
data doesn't poison the report unless `--ignore-clean-since` is passed.

Usage::

    .venv/Scripts/python.exe scripts/factor_audit.py --last 14d
    .venv/Scripts/python.exe scripts/factor_audit.py --last all --ignore-clean-since
    .venv/Scripts/python.exe scripts/factor_audit.py --top-combos 20
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.journal.database import TradeJournal
from src.journal.models import RejectedSignal, TradeOutcome, TradeRecord


# ── CLI / config plumbing (mirrors report.py) ───────────────────────────────


def _parse_window(arg: str) -> Optional[datetime]:
    if arg == "all":
        return None
    m = re.fullmatch(r"(\d+)([dh])", arg)
    if not m:
        raise argparse.ArgumentTypeError(
            f"--last must be 'all' or NNd/NNh (got {arg!r})"
        )
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    return datetime.now(tz=timezone.utc) - delta


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_db_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    cfg = _load_cfg()
    return (cfg.get("journal") or {}).get("db_path", "data/trades.db")


def _resolve_clean_since() -> Optional[datetime]:
    cfg = _load_cfg()
    raw = (cfg.get("rl") or {}).get("clean_since")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Core math ───────────────────────────────────────────────────────────────


def _wr(wins: int, losses: int) -> float:
    total = wins + losses
    return (wins / total) if total > 0 else 0.0


def _actual_wr(trades: list[TradeRecord]) -> tuple[int, int, float]:
    wins = sum(1 for t in trades if t.outcome == TradeOutcome.WIN)
    losses = sum(1 for t in trades if t.outcome == TradeOutcome.LOSS)
    return wins, losses, _wr(wins, losses)


def _hypo_wr(rejects: list[RejectedSignal]) -> tuple[int, int, int, float]:
    wins = sum(1 for r in rejects if r.hypothetical_outcome == "WIN")
    losses = sum(1 for r in rejects if r.hypothetical_outcome == "LOSS")
    neither = sum(1 for r in rejects if r.hypothetical_outcome == "NEITHER")
    return wins, losses, neither, _wr(wins, losses)


def _avg_r(trades: list[TradeRecord]) -> float:
    rs = [t.pnl_r for t in trades if t.pnl_r is not None]
    return sum(rs) / len(rs) if rs else 0.0


# ── Grouped breakdowns ──────────────────────────────────────────────────────


def _by_symbol(trades: list[TradeRecord]) -> dict[str, list[TradeRecord]]:
    out: dict[str, list[TradeRecord]] = {}
    for t in trades:
        out.setdefault(t.symbol, []).append(t)
    return out


def _by_session(trades: list[TradeRecord]) -> dict[str, list[TradeRecord]]:
    out: dict[str, list[TradeRecord]] = {}
    for t in trades:
        out.setdefault(t.session or "UNKNOWN", []).append(t)
    return out


# `_by_regime` removed 2026-04-29 — `regime_at_entry` was a 1-distinct
# constant dropped from the schema 2026-04-27. `_by_trend_regime` (ADX
# trend regime) carries the only remaining semantic regime split.


def _by_trend_regime(trades: list[TradeRecord]) -> dict[str, list[TradeRecord]]:
    out: dict[str, list[TradeRecord]] = {}
    for t in trades:
        out.setdefault(t.trend_regime_at_entry or "UNKNOWN", []).append(t)
    return out


def _by_factor(trades: list[TradeRecord]) -> dict[str, list[TradeRecord]]:
    """One trade with N factors lands in N buckets (factor-attribution view)."""
    out: dict[str, list[TradeRecord]] = {}
    for t in trades:
        for f in t.confluence_factors:
            out.setdefault(f, []).append(t)
    return out


def _by_factor_combo(
    trades: list[TradeRecord], *, min_trades: int = 2,
) -> dict[str, list[TradeRecord]]:
    out: dict[str, list[TradeRecord]] = {}
    for t in trades:
        key = ",".join(sorted(t.confluence_factors)) or "NONE"
        out.setdefault(key, []).append(t)
    # Mirror reporter.py: fold thin combos into RARE so long-tail doesn't drown the signal.
    folded: dict[str, list[TradeRecord]] = {}
    rare: list[TradeRecord] = []
    for key, records in out.items():
        if len(records) < min_trades:
            rare.extend(records)
            continue
        folded[key] = records
    if rare:
        folded["RARE"] = rare
    return folded


_DEFAULT_SCORE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 2.0),
    (2.0, 3.0),
    (3.0, 4.0),
    (4.0, 5.0),
    (5.0, float("inf")),
)


def _bucket_label(low: float, high: float) -> str:
    return f"{low:.1f}+" if high == float("inf") else f"{low:.1f}-{high:.1f}"


def _by_score_bucket(
    trades: list[TradeRecord],
    buckets: tuple[tuple[float, float], ...] = _DEFAULT_SCORE_BUCKETS,
) -> dict[str, list[TradeRecord]]:
    out: dict[str, list[TradeRecord]] = {}
    for low, high in buckets:
        out[_bucket_label(low, high)] = [
            t for t in trades if low <= t.confluence_score < high
        ]
    return out


def _rejects_by_reason(
    rejects: list[RejectedSignal],
) -> dict[str, list[RejectedSignal]]:
    out: dict[str, list[RejectedSignal]] = {}
    for r in rejects:
        out.setdefault(r.reject_reason, []).append(r)
    return out


# ── Rendering ───────────────────────────────────────────────────────────────


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+6.2f}%"


def _hline(n: int = 72) -> str:
    return "=" * n


def _render_group(
    title: str,
    buckets: dict[str, list[TradeRecord]],
    *,
    sort_by_count: bool = True,
) -> list[str]:
    if not buckets:
        return []
    lines: list[str] = ["", f"  {title}"]
    rows = sorted(
        buckets.items(),
        key=(lambda kv: len(kv[1])) if sort_by_count else (lambda kv: kv[0]),
        reverse=sort_by_count,
    )
    for name, records in rows:
        if not records:
            lines.append(f"    {name:<28} n=0")
            continue
        w, l, wr = _actual_wr(records)
        ar = _avg_r(records)
        lines.append(
            f"    {name:<28} n={len(records):<3} "
            f"W={w:<3} L={l:<3} wr={_fmt_pct(wr)} avg_r={ar:+6.3f}R"
        )
    return lines


def _render_reject_group(
    rejects_by_reason: dict[str, list[RejectedSignal]],
) -> list[str]:
    if not rejects_by_reason:
        return []
    lines: list[str] = ["", "  Reject reasons (counter-factual outcomes):"]
    rows = sorted(
        rejects_by_reason.items(),
        key=lambda kv: len(kv[1]),
        reverse=True,
    )
    for reason, recs in rows:
        w, l, n_none, wr = _hypo_wr(recs)
        unpegged = sum(1 for r in recs if r.hypothetical_outcome is None)
        lines.append(
            f"    {reason:<34} n={len(recs):<4} "
            f"W={w:<3} L={l:<3} NEITHER={n_none:<3} UNPEGGED={unpegged:<3} "
            f"hypo_wr={_fmt_pct(wr)}"
        )
    return lines


def _render_factor_combos(
    combos: dict[str, list[TradeRecord]],
    *,
    top_n: int,
) -> list[str]:
    if not combos:
        return []
    lines: list[str] = ["", f"  Top factor combos by frequency (top {top_n}):"]
    rows = sorted(combos.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]
    for name, records in rows:
        w, l, wr = _actual_wr(records)
        ar = _avg_r(records)
        display = name if len(name) <= 56 else name[:53] + "..."
        lines.append(
            f"    n={len(records):<3} W={w:<3} L={l:<3} "
            f"wr={_fmt_pct(wr)} avg_r={ar:+6.3f}R  {display}"
        )
    return lines


def _render_factor_actual_vs_hypo(
    by_factor_trade: dict[str, list[TradeRecord]],
    rejects: list[RejectedSignal],
) -> list[str]:
    """Per-factor WR on actual trades side-by-side with hypothetical WR of
    rejects that carried the same factor. Answers "did the reject path throw
    away winners when factor X fired but confluence was short"?"""
    if not by_factor_trade and not rejects:
        return []
    # Build reject factor buckets (only rows pegged with a decisive outcome).
    rej_by_factor: dict[str, list[RejectedSignal]] = {}
    for r in rejects:
        if r.hypothetical_outcome not in ("WIN", "LOSS"):
            continue
        for f in r.confluence_factors:
            rej_by_factor.setdefault(f, []).append(r)

    all_factors = set(by_factor_trade.keys()) | set(rej_by_factor.keys())
    if not all_factors:
        return []
    lines: list[str] = ["", "  Per-factor actual vs hypothetical WR:"]
    lines.append(
        f"    {'factor':<28} "
        f"{'trades n':<10} {'trade wr':<10} "
        f"{'rej n':<8} {'rej wr':<10}"
    )
    # Sort by trade count desc; fall back to hypothetical count.
    def _sort_key(factor: str) -> tuple[int, int]:
        return (
            -len(by_factor_trade.get(factor, [])),
            -len(rej_by_factor.get(factor, [])),
        )
    for factor in sorted(all_factors, key=_sort_key):
        t_records = by_factor_trade.get(factor, [])
        r_records = rej_by_factor.get(factor, [])
        t_w, t_l, t_wr = _actual_wr(t_records)
        r_w, r_l, _, r_wr = _hypo_wr(r_records)
        t_cell = _fmt_pct(t_wr) if (t_w + t_l) else "     -  "
        r_cell = _fmt_pct(r_wr) if (r_w + r_l) else "     -  "
        lines.append(
            f"    {factor:<28} "
            f"n={len(t_records):<7} {t_cell:<10} "
            f"n={len(r_records):<5} {r_cell:<10}"
        )
    return lines


def _render(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    *,
    top_combos: int,
) -> str:
    lines: list[str] = []
    lines.append(_hline())
    lines.append("  Factor audit — actual trades + counter-factual rejects")
    lines.append(_hline())

    # Header stats.
    t_w, t_l, t_wr = _actual_wr(trades)
    decided = t_w + t_l
    be = sum(1 for t in trades if t.outcome == TradeOutcome.BREAKEVEN)
    lines.append(
        f"  Closed trades: {len(trades)}  "
        f"(W={t_w} / L={t_l} / BE={be}) decisive_wr={_fmt_pct(t_wr)}"
    )
    r_w, r_l, r_none, r_wr = _hypo_wr(rejects)
    r_unpegged = sum(1 for r in rejects if r.hypothetical_outcome is None)
    lines.append(
        f"  Rejected signals: {len(rejects)}  "
        f"(W={r_w} / L={r_l} / NEITHER={r_none} / UNPEGGED={r_unpegged}) "
        f"hypo_wr={_fmt_pct(r_wr)}"
    )
    lines.append(f"  Decisive trades: {decided}   "
                 f"Decisive rejects: {r_w + r_l}")

    lines.extend(_render_group("Per-symbol:", _by_symbol(trades)))
    lines.extend(_render_group("Per-session:", _by_session(trades)))
    # `Per-derivatives-regime` section removed 2026-04-29 — column
    # `regime_at_entry` was dropped 2026-04-27 (1-distinct constant).
    lines.extend(_render_group(
        "Per-ADX-trend-regime (trend_regime_at_entry):",
        _by_trend_regime(trades),
    ))
    lines.extend(_render_group(
        "Per-score-bucket (confluence_score):",
        _by_score_bucket(trades),
        sort_by_count=False,
    ))

    lines.extend(_render_factor_combos(
        _by_factor_combo(trades, min_trades=2), top_n=top_combos,
    ))
    lines.extend(_render_factor_actual_vs_hypo(_by_factor(trades), rejects))
    lines.extend(_render_reject_group(_rejects_by_reason(rejects)))

    # Direction split — surfaced last because it's coarse but occasionally
    # tells us the whole stack is biased one way.
    dir_buckets: dict[str, list[TradeRecord]] = {}
    for t in trades:
        dir_buckets.setdefault(t.direction.value, []).append(t)
    lines.extend(_render_group(
        "Per-direction:", dir_buckets, sort_by_count=False,
    ))

    # Reject-by-symbol frequency — fast glance at which pair is getting
    # filtered most. Doesn't carry outcome by itself; outcome lives in
    # the per-reason section above.
    if rejects:
        lines.append("")
        lines.append("  Reject frequency per symbol:")
        by_sym: Counter[str] = Counter(r.symbol for r in rejects)
        for sym, n in by_sym.most_common():
            lines.append(f"    {sym:<20} n={n}")

    lines.append(_hline())
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────


async def _run(
    *,
    db_path: str,
    since: Optional[datetime],
    top_combos: int,
) -> int:
    async with TradeJournal(db_path) as j:
        trades = await j.list_closed_trades(since=since)
        rejects = await j.list_rejected_signals(since=since)
    if not trades and not rejects:
        window = "all time" if since is None else f"since {since.isoformat()}"
        print(f"No trades or rejects in window ({window}).")
        return 0
    print(_render(trades, rejects, top_combos=top_combos))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline factor audit (trades + rejected_signals)",
    )
    parser.add_argument("--db", default=None, help="Path to trades.db")
    parser.add_argument(
        "--last", default="14d",
        help="Window: '7d', '14d', '12h', 'all' (default 14d)",
    )
    parser.add_argument(
        "--top-combos", type=int, default=10,
        help="How many factor-combo rows to print (default 10)",
    )
    parser.add_argument(
        "--ignore-clean-since", action="store_true",
        help="Include rows before `rl.clean_since` (default: honour cutoff)",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    try:
        since = _parse_window(args.last)
    except argparse.ArgumentTypeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if not args.ignore_clean_since:
        clean_since = _resolve_clean_since()
        if clean_since is not None:
            since = clean_since if since is None else max(since, clean_since)

    if not Path(db_path).exists() and db_path != ":memory:":
        print(f"[WARN] DB not found at {db_path} - nothing to audit.")
        return 0

    return asyncio.run(_run(
        db_path=db_path, since=since, top_combos=args.top_combos,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
