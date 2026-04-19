"""Counter-factual outcome pegger for `rejected_signals` rows.

Phase 7.B2 — the other half of the rejected_signals pipeline. B1 captures
a row every time `build_trade_plan_with_reason` returns None; this script
walks those rows' candles forward and stamps a hypothetical outcome so we
can ask "would the trade have won?" per reject_reason.

For each un-pegged reject with a `proposed_sl_price` AND `proposed_tp_price`:

  1. Fetch OKX history-candles on the reject's `entry_timeframe`,
     starting at `signal_timestamp` and walking forward up to N bars.
  2. Walk bar-by-bar. First side hit wins:
       bullish: SL = low ≤ sl_price,  TP = high ≥ tp_price
       bearish: SL = high ≥ sl_price, TP = low ≤ tp_price
     Same-bar both-hit → LOSS (conservative; a real fill could be either).
  3. Stamp `hypothetical_outcome` (`WIN` / `LOSS` / `NEITHER`) via
     `TradeJournal.update_rejected_outcome`.

Dry-run by default so you can inspect the stamp plan before it writes.
Pass `--commit` to persist. Rows without both proposed prices (e.g.
`below_confluence` rejects, which short-circuit before SL/TP math) are
skipped with a warning.

Usage::

    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --last 14d
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --last all --commit
    .venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --max-bars 30 \
        --symbol BTC-USDT-SWAP --reason below_confluence
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.journal.database import TradeJournal
from src.journal.models import RejectedSignal

try:
    from okx.MarketData import MarketAPI
except ImportError:  # pragma: no cover — okx lib missing is an env error.
    MarketAPI = None  # type: ignore


DEFAULT_MAX_BARS = 20
DEFAULT_LIMIT = 300  # OKX max per call.


# ── Config / CLI ────────────────────────────────────────────────────────────


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


# ── Candle fetch ────────────────────────────────────────────────────────────


@dataclass
class Bar:
    ts_ms: int
    high: float
    low: float
    close: float


def _tf_to_bar(entry_timeframe: Optional[str]) -> str:
    """Normalize entry_timeframe column to an OKX `bar` parameter.

    Accepts '1', '3', '15', '1m', '3m', '15m', '1H', '4H'. OKX expects
    '1m'/'3m'/'15m'/'1H' case-sensitive for sub-daily; we default to 3m
    when unparseable since it's the entry TF most sprint 3 rejects came on.
    """
    if not entry_timeframe:
        return "3m"
    tf = entry_timeframe.strip()
    if re.fullmatch(r"\d+", tf):
        minutes = int(tf)
        if minutes >= 60:
            return f"{minutes // 60}H"
        return f"{minutes}m"
    return tf


def _fetch_forward_bars(
    api, inst_id: str, since: datetime, bar: str, max_bars: int,
) -> list[Bar]:
    """Pull up to `max_bars` closed bars on `bar` TF starting at `since`.

    OKX history-candlesticks returns bars in *reverse* chronological order
    (newest first). We filter to bars whose timestamp ≥ since (epoch ms),
    then sort ascending so the walker visits them in forward-time order.

    `after`/`before` in OKX terminology are actually page cursors on the
    ts field; we use `before=since_ms` to fetch everything after `since`.
    """
    if MarketAPI is None:
        raise RuntimeError("okx library is required: pip install python-okx")
    since_ms = int(since.timestamp() * 1000)
    out: list[Bar] = []
    cursor: Optional[int] = None
    # Cap iterations at max_bars/limit + 2 pages slack.
    for _ in range((max_bars // DEFAULT_LIMIT) + 2):
        kwargs = {
            "instId": inst_id,
            "bar": bar,
            "limit": str(min(DEFAULT_LIMIT, max_bars - len(out) + DEFAULT_LIMIT)),
        }
        # `before=X` gives bars with ts > X (newer than X). Walking forward
        # from `since_ms` requires repeatedly nudging `before` upward as
        # we consume pages.
        kwargs["before"] = str(cursor or since_ms)
        resp = api.get_history_candlesticks(**kwargs)
        if resp.get("code") != "0":
            raise RuntimeError(f"OKX error: {resp!r}")
        rows = resp.get("data") or []
        if not rows:
            break
        # Newest-first → oldest-first. We only want bars ≥ since_ms.
        parsed = []
        for row in rows:
            ts_ms = int(row[0])
            if ts_ms < since_ms:
                continue
            parsed.append(Bar(
                ts_ms=ts_ms,
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            ))
        parsed.sort(key=lambda b: b.ts_ms)
        out.extend(parsed)
        if len(parsed) < DEFAULT_LIMIT:
            break
        cursor = parsed[-1].ts_ms
        if len(out) >= max_bars:
            break
    # Final sort + trim — pages can overlap when the cursor lines up with a
    # bar boundary, drop duplicates by ts_ms.
    seen: set[int] = set()
    deduped: list[Bar] = []
    for b in sorted(out, key=lambda b: b.ts_ms):
        if b.ts_ms in seen:
            continue
        seen.add(b.ts_ms)
        deduped.append(b)
    return deduped[:max_bars]


# ── Outcome walker ──────────────────────────────────────────────────────────


@dataclass
class WalkResult:
    outcome: str            # "WIN" / "LOSS" / "NEITHER"
    bars_to_tp: Optional[int]
    bars_to_sl: Optional[int]


def _walk_outcome(
    rej: RejectedSignal, bars: list[Bar],
) -> Optional[WalkResult]:
    """Apply the "first SL/TP touch" rule forward. Returns None if we can't
    evaluate (missing proposed prices / no bars / bullish-tp ≤ entry, etc.)."""
    if rej.proposed_sl_price is None or rej.proposed_tp_price is None:
        return None
    if rej.price is None or rej.price <= 0:
        return None
    if not bars:
        return None
    sl = float(rej.proposed_sl_price)
    tp = float(rej.proposed_tp_price)
    is_bull = rej.direction.value == "BULLISH"
    for i, b in enumerate(bars):
        hit_sl = (b.low <= sl) if is_bull else (b.high >= sl)
        hit_tp = (b.high >= tp) if is_bull else (b.low <= tp)
        if hit_sl and hit_tp:
            # Same-bar ambiguity → conservative LOSS.
            return WalkResult("LOSS", bars_to_tp=None, bars_to_sl=i + 1)
        if hit_tp:
            return WalkResult("WIN", bars_to_tp=i + 1, bars_to_sl=None)
        if hit_sl:
            return WalkResult("LOSS", bars_to_tp=None, bars_to_sl=i + 1)
    return WalkResult("NEITHER", bars_to_tp=None, bars_to_sl=None)


# ── Main ────────────────────────────────────────────────────────────────────


async def _run(
    *, db_path: str, since: Optional[datetime], symbol: Optional[str],
    reason: Optional[str], max_bars: int, commit: bool,
) -> int:
    if MarketAPI is None:
        print("[ERROR] python-okx not installed. Run: pip install python-okx",
              file=sys.stderr)
        return 2
    api = MarketAPI(flag="0")  # Public endpoint; prod demo flag irrelevant.

    async with TradeJournal(db_path) as j:
        rows = await j.list_rejected_signals(
            since=since, symbol=symbol, reject_reason=reason,
        )
    if not rows:
        print(
            f"No rejected_signals rows matched (since={since}, "
            f"symbol={symbol!r}, reason={reason!r})."
        )
        return 0

    pending = [r for r in rows if r.hypothetical_outcome is None]
    already = len(rows) - len(pending)
    print(
        f"Found {len(rows)} rejected rows "
        f"({already} already pegged, {len(pending)} pending). "
        f"mode={'COMMIT' if commit else 'dry-run'} "
        f"max_bars={max_bars}"
    )

    win = loss = neither = skipped = 0
    stamp_plan: list[tuple[str, WalkResult]] = []
    for idx, rej in enumerate(pending, 1):
        if rej.proposed_sl_price is None or rej.proposed_tp_price is None:
            skipped += 1
            continue
        bar = _tf_to_bar(rej.entry_timeframe)
        try:
            bars = _fetch_forward_bars(
                api, rej.symbol, rej.signal_timestamp, bar, max_bars,
            )
        except Exception as e:
            print(f"  [WARN] fetch failed for {rej.rejection_id[:8]}: {e}",
                  file=sys.stderr)
            skipped += 1
            continue
        result = _walk_outcome(rej, bars)
        if result is None:
            skipped += 1
            continue
        stamp_plan.append((rej.rejection_id, result))
        if result.outcome == "WIN":
            win += 1
        elif result.outcome == "LOSS":
            loss += 1
        else:
            neither += 1
        if idx % 25 == 0:
            print(f"  ...walked {idx}/{len(pending)}")

    total = win + loss + neither
    wr = (win / total * 100.0) if total > 0 else 0.0
    print(
        f"\nHypothetical outcomes: WIN={win} LOSS={loss} NEITHER={neither} "
        f"(skipped={skipped}, wr={wr:.1f}% of decisive)"
    )

    if not commit:
        print("\n[dry-run] no rows were stamped — re-run with --commit to write.")
        return 0

    async with TradeJournal(db_path) as j:
        for rejection_id, result in stamp_plan:
            await j.update_rejected_outcome(
                rejection_id,
                hypothetical_outcome=result.outcome,
                bars_to_tp=result.bars_to_tp,
                bars_to_sl=result.bars_to_sl,
            )
    print(f"Stamped {len(stamp_plan)} rows.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Peg counter-factual outcomes onto rejected_signals rows.",
    )
    parser.add_argument("--db", default=None, help="Path to trades.db")
    parser.add_argument(
        "--last", default="14d",
        help="Window: '7d', '14d', '12h', 'all' (default 14d)",
    )
    parser.add_argument(
        "--max-bars", type=int, default=DEFAULT_MAX_BARS,
        help=f"Forward-walk horizon in bars (default {DEFAULT_MAX_BARS})",
    )
    parser.add_argument("--symbol", default=None, help="Limit to one symbol")
    parser.add_argument("--reason", default=None, help="Limit to one reject_reason")
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually stamp the outcomes (default: dry-run)",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    try:
        since = _parse_window(args.last)
    except argparse.ArgumentTypeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if not Path(db_path).exists() and db_path != ":memory:":
        print(f"[WARN] DB not found at {db_path} - nothing to peg.")
        return 0

    return asyncio.run(_run(
        db_path=db_path, since=since, symbol=args.symbol,
        reason=args.reason, max_bars=args.max_bars, commit=args.commit,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
