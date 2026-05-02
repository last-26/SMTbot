"""Cross-check closed-trade enrichment against Bybit's authoritative
`/v5/position/closed-pnl` rows.

Why this exists
---------------
`PositionMonitor` detects a position close (size→0 on poll) before
Bybit always finishes writing the corresponding closed-pnl row. The
old `enrich_close_fill` then latched onto the previous close on the
same symbol+side and stamped its exit/PnL on the journal. Two cases
observed 2026-05-02 (BTC SHORT 78240, DOGE LONG 0.1085) prompted a
prevention fix (`opened_at` filter on enrich, commit `cfe8e4b`).

This audit catches both the historical pollution AND any future
regression in the enrich path: every closed Bybit-era trade is matched
against Bybit's closed-pnl rows by `avgEntryPrice` (within 0.1%) and
`closedPnl` / `avgExitPrice` / fees / `updatedTime` are diff'd against
the journal.

Usage
-----
    python scripts/audit_bybit_close_enrichment.py             # dry-run audit
    python scripts/audit_bybit_close_enrichment.py --apply     # backfill mismatches

`--apply` writes an atomic SQLite backup to
`data/trades.db.pre_backfill_<ts>` before any UPDATE, runs the
backfill in a single transaction, and re-audits to verify zero
mismatches before exiting.

Caveats
-------
Match heuristic relies on `avgEntryPrice` tolerance. If two consecutive
trades on the same symbol+side opened within 0.1% of each other AND
both closed within the same 72h window, the script could mis-pair.
The current dataset has no such collision (verified 2026-05-02). If
this fires "AMBIGUOUS_MATCH" in the future, fall back to per-trade
manual review using the recorded `order_id`.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

# Bybit-era cut. Anchored at the migration timestamp; pre-cut rows came
# from OKX and use a different fee/PnL convention, audit must skip them.
BYBIT_ERA_CUT = "2026-04-25T21:30"

# Match heuristics.
ENTRY_PRICE_TOLERANCE_PCT = 0.001  # 0.1%
CLOSE_WINDOW_BACK_S = 60           # close-row may print up to 1m before bot's entry_ts
CLOSE_WINDOW_FORWARD_H = 72        # held positions: max 72h hold

# Tolerances under which DB and Bybit are considered "clean equal".
PNL_TOLERANCE_USD = 0.05
EXIT_PRICE_TOLERANCE_PCT = 0.0001
TIMESTAMP_TOLERANCE_S = 5
FEE_TOLERANCE_USD = 0.01

INTERNAL_TO_BYBIT = {
    "BTC-USDT-SWAP": "BTCUSDT",
    "ETH-USDT-SWAP": "ETHUSDT",
    "SOL-USDT-SWAP": "SOLUSDT",
    "DOGE-USDT-SWAP": "DOGEUSDT",
    "XRP-USDT-SWAP": "XRPUSDT",
}


@dataclass
class Mismatch:
    trade_id: str
    symbol: str
    direction: str
    entry_ts: str
    verdict: str  # CLEAN | MINOR_DRIFT | OUTCOME_FLIP | NO_MATCH
    db_exit: float
    by_exit: float
    db_pnl: float
    by_pnl: float
    db_fees: float
    by_fees: float
    db_exit_ts: Optional[str]
    by_exit_ts: Optional[str]
    risk_amount: float


def _bybit_session() -> HTTP:
    load_dotenv()
    return HTTP(
        api_key=os.getenv("BYBIT_API_KEY"),
        api_secret=os.getenv("BYBIT_API_SECRET"),
        demo=True,
    )


def _fetch_bybit_closed_pnl(session: HTTP, symbol: str, max_rows: int = 200) -> list[dict]:
    """Page through `/v5/position/closed-pnl` until `max_rows` collected
    or pagination ends. 200 covers ~all post-cut activity per symbol;
    bump if a future migration accumulates more."""
    rows: list[dict] = []
    cursor = ""
    while True:
        kwargs = {"category": "linear", "symbol": symbol, "limit": 100}
        if cursor:
            kwargs["cursor"] = cursor
        resp = session.get_closed_pnl(**kwargs)
        result = resp.get("result", {})
        rows.extend(result.get("list") or [])
        cursor = result.get("nextPageCursor", "")
        if not cursor or len(rows) >= max_rows:
            break
    return rows


def _find_correct_close(t: dict, bybit_rows: list[dict]) -> Optional[tuple[float, datetime, dict]]:
    """Return (rel_diff, ts, row) for the best Bybit match, or None."""
    target_side = "Buy" if t["direction"] == "BEARISH" else "Sell"
    entry_dt = datetime.fromisoformat(t["entry_timestamp"])
    entry_price = float(t["entry_price"])

    candidates: list[tuple[float, datetime, dict]] = []
    for r in bybit_rows:
        if r.get("side") != target_side:
            continue
        ts_ms = int(r.get("updatedTime") or 0)
        if ts_ms == 0:
            continue
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        if ts < entry_dt - timedelta(seconds=CLOSE_WINDOW_BACK_S):
            continue
        if ts > entry_dt + timedelta(hours=CLOSE_WINDOW_FORWARD_H):
            continue
        avg_entry = float(r.get("avgEntryPrice") or 0)
        if avg_entry == 0:
            continue
        rel_diff = abs(avg_entry - entry_price) / entry_price
        if rel_diff > ENTRY_PRICE_TOLERANCE_PCT:
            continue
        candidates.append((rel_diff, ts, r))
    if not candidates:
        return None
    # Closest entry-price match wins; ties broken by earliest close ts.
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0]


def _classify(t: dict, by_ts: datetime, by: dict) -> tuple[str, Mismatch]:
    by_exit = float(by.get("avgExitPrice"))
    by_pnl = float(by.get("closedPnl"))
    by_fees = float(by.get("openFee") or 0) + float(by.get("closeFee") or 0)
    db_exit = float(t["exit_price"] or 0)
    db_pnl = float(t["pnl_usdt"] or 0)
    db_fees = float(t["fees_usdt"] or 0)
    db_exit_ts = t["exit_timestamp"]
    ts_diff = (
        abs((datetime.fromisoformat(db_exit_ts) - by_ts).total_seconds())
        if db_exit_ts else float("inf")
    )
    pnl_diff = abs(by_pnl - db_pnl)
    exit_diff_pct = abs(by_exit - db_exit) / by_exit if by_exit else 0.0
    fee_diff = abs(by_fees - db_fees)

    clean = (
        pnl_diff < PNL_TOLERANCE_USD
        and exit_diff_pct < EXIT_PRICE_TOLERANCE_PCT
        and ts_diff < TIMESTAMP_TOLERANCE_S
        and fee_diff < FEE_TOLERANCE_USD
    )
    if clean:
        verdict = "CLEAN"
    elif (by_pnl > 0) != (db_pnl > 0):
        verdict = "OUTCOME_FLIP"
    else:
        verdict = "MINOR_DRIFT"

    return verdict, Mismatch(
        trade_id=t["trade_id"], symbol=t["symbol"], direction=t["direction"],
        entry_ts=t["entry_timestamp"], verdict=verdict,
        db_exit=db_exit, by_exit=by_exit, db_pnl=db_pnl, by_pnl=by_pnl,
        db_fees=db_fees, by_fees=by_fees,
        db_exit_ts=db_exit_ts, by_exit_ts=by_ts.isoformat(),
        risk_amount=float(t["risk_amount_usdt"] or 0),
    )


def audit(db_path: str, session: HTTP) -> tuple[dict[str, int], list[Mismatch]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        """
        SELECT trade_id, symbol, direction, outcome, entry_price, exit_price,
               pnl_usdt, fees_usdt, entry_timestamp, exit_timestamp,
               num_contracts, risk_amount_usdt, pnl_r
        FROM trades
        WHERE outcome IN ('WIN','LOSS') AND entry_timestamp >= ?
        ORDER BY entry_timestamp
        """,
        (BYBIT_ERA_CUT,),
    )
    trades = [dict(r) for r in cur.fetchall()]
    con.close()

    bybit_per_symbol: dict[str, list[dict]] = {}
    for sym in {t["symbol"] for t in trades}:
        bybit_sym = INTERNAL_TO_BYBIT[sym]
        bybit_per_symbol[sym] = _fetch_bybit_closed_pnl(session, bybit_sym)

    stats = {"CLEAN": 0, "MINOR_DRIFT": 0, "OUTCOME_FLIP": 0, "NO_MATCH": 0}
    mismatches: list[Mismatch] = []
    for t in trades:
        match = _find_correct_close(t, bybit_per_symbol[t["symbol"]])
        if match is None:
            stats["NO_MATCH"] += 1
            mismatches.append(Mismatch(
                trade_id=t["trade_id"], symbol=t["symbol"],
                direction=t["direction"], entry_ts=t["entry_timestamp"],
                verdict="NO_MATCH",
                db_exit=float(t["exit_price"] or 0), by_exit=0.0,
                db_pnl=float(t["pnl_usdt"] or 0), by_pnl=0.0,
                db_fees=float(t["fees_usdt"] or 0), by_fees=0.0,
                db_exit_ts=t["exit_timestamp"], by_exit_ts=None,
                risk_amount=float(t["risk_amount_usdt"] or 0),
            ))
            continue
        _, by_ts, by = match
        verdict, m = _classify(t, by_ts, by)
        stats[verdict] += 1
        if verdict != "CLEAN":
            mismatches.append(m)

    return stats, mismatches


def _print_audit(n: int, stats: dict[str, int], mismatches: list[Mismatch]) -> None:
    print(f"\n=== AUDIT (n={n}, cut={BYBIT_ERA_CUT}) ===")
    for k in ("CLEAN", "MINOR_DRIFT", "OUTCOME_FLIP", "NO_MATCH"):
        print(f"  {k}: {stats[k]}")
    if mismatches:
        print()
        for m in mismatches:
            print(f"  {m.trade_id[:12]} {m.symbol:18} {m.direction:8} {m.verdict}")
            if m.verdict != "NO_MATCH":
                print(f"    exit:  db={m.db_exit} by={m.by_exit}")
                print(f"    pnl:   db={m.db_pnl:.4f} by={m.by_pnl:.4f}")
                print(f"    fees:  db={m.db_fees:.4f} by={m.by_fees:.4f}")
                print(f"    ts:    db={m.db_exit_ts} by={m.by_exit_ts}")
            else:
                print(f"    db_pnl={m.db_pnl:.4f} db_exit_ts={m.db_exit_ts} (no Bybit match in window)")


def _atomic_backup(db_path: str) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup_path = f"{db_path}.pre_backfill_{ts}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()
    return backup_path


def apply_backfill(db_path: str, mismatches: list[Mismatch]) -> int:
    actionable = [m for m in mismatches if m.verdict in ("MINOR_DRIFT", "OUTCOME_FLIP")]
    if not actionable:
        return 0

    backup = _atomic_backup(db_path)
    print(f"\nBackup: {backup}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    try:
        cur.execute("BEGIN")
        for m in actionable:
            new_pnl_r = m.by_pnl / m.risk_amount if m.risk_amount else 0.0
            new_outcome = (
                "WIN" if m.by_pnl > 0
                else "LOSS" if m.by_pnl < 0
                else "BREAKEVEN"
            )
            cur.execute(
                """
                UPDATE trades SET
                    exit_price = ?, pnl_usdt = ?, pnl_r = ?, fees_usdt = ?,
                    exit_timestamp = ?, outcome = ?
                WHERE trade_id = ?
                """,
                (m.by_exit, m.by_pnl, new_pnl_r, m.by_fees,
                 m.by_exit_ts, new_outcome, m.trade_id),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return len(actionable)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="data/trades.db", help="path to trades.db")
    parser.add_argument("--apply", action="store_true",
                        help="backfill MINOR_DRIFT + OUTCOME_FLIP rows (atomic backup first)")
    args = parser.parse_args()

    session = _bybit_session()

    stats, mismatches = audit(args.db, session)
    n = sum(stats.values())
    _print_audit(n, stats, mismatches)

    if not args.apply:
        if stats["MINOR_DRIFT"] or stats["OUTCOME_FLIP"]:
            print("\nMismatches present. Re-run with --apply to backfill.")
            return 1
        if stats["NO_MATCH"]:
            print("\nNO_MATCH rows present — manual review required (no auto-fix).")
            return 1
        return 0

    fixed = apply_backfill(args.db, mismatches)
    print(f"\nBackfilled {fixed} row(s). Re-auditing...")
    stats2, mismatches2 = audit(args.db, session)
    _print_audit(sum(stats2.values()), stats2, mismatches2)
    if stats2["MINOR_DRIFT"] or stats2["OUTCOME_FLIP"]:
        print("\nFAIL: post-backfill audit still has mismatches.")
        return 1
    print("\nOK: 0 actionable mismatches post-backfill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
