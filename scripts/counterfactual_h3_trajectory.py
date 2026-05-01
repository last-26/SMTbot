"""H3 (MAE-BE-lock) trajectory-aware counterfactual.

Problem with naive H3: SL is at -1R, so trades hitting MAE -1R already exited.
Operator intent: at MAE threshold (e.g. -0.5R), if price returns to entry AND
cycle signal still adverse -> lock SL at entry. Position closes at 0R when
price touches entry from above (long) or below (short).

This script walks per-snapshot trajectory to detect:
  1. Did MAE reach threshold at any point?
  2. After that, did MAE recover to >= recovery_band (e.g. -0.1R, near entry)?
  3. After recovery, did position close negative (BE-lock would have caught)
     OR positive (BE-lock would have prematurely closed at 0R)?

Estimates two scenarios:
  A. "Best case": every MAE-deep loss is rescued at 0R (signal-flip catches)
  B. "Worst case": every MAE-deep recovery is caught (incl. wins prematurely)
  C. "Selective": only losses caught (operator-described "still negative" check)
"""

import argparse
import sqlite3
from collections import defaultdict


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default='data/trades.db')
    p.add_argument('--since', default='2026-04-25T21:30:00Z')
    p.add_argument('--mae-thresholds', default='-0.3,-0.5,-0.7,-0.9',
                   help='comma-list of MAE thresholds to sweep')
    p.add_argument('--recovery-band', type=float, default=-0.1,
                   help='MAE level considered "recovered to entry"')
    args = p.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    trades = list(db.execute(
        "SELECT trade_id, pnl_r, trend_regime_at_entry, close_reason "
        "FROM trades WHERE entry_timestamp >= ? AND exit_timestamp IS NOT NULL "
        "ORDER BY entry_timestamp",
        (args.since,),
    ))

    snaps_by_trade = defaultdict(list)
    for r in db.execute(
        'SELECT trade_id, captured_at, mfe_r_so_far, mae_r_so_far '
        'FROM position_snapshots ORDER BY trade_id, captured_at'
    ):
        snaps_by_trade[r['trade_id']].append(
            (r['captured_at'], r['mfe_r_so_far'], r['mae_r_so_far'])
        )
    db.close()

    total = len(trades)
    actual_total = sum((t['pnl_r'] or 0) for t in trades)

    print('=' * 80)
    print('H3 trajectory-aware counterfactual')
    print('=' * 80)
    print(f"baseline n={total} sum_R={actual_total:+.2f}")
    print(f"recovery_band MAE >= {args.recovery_band}R counts as 'returned to entry'")
    print()

    print(
        f"{'thresh':>6s} {'mae_deep':>8s} {'recovered':>9s} "
        f"{'rec_won':>7s} {'rec_lost':>8s} "
        f"{'best':>7s} {'worst':>7s} {'selective':>9s}"
    )

    for threshold_str in args.mae_thresholds.split(','):
        threshold = float(threshold_str)
        mae_deep = 0
        recovered = 0
        rec_won = []
        rec_lost = []
        for t in trades:
            actual = t['pnl_r'] or 0.0
            snaps = snaps_by_trade.get(t['trade_id'], [])
            if not snaps:
                continue
            # walk trajectory: find first snap where mae <= threshold,
            # then check if subsequent snaps have mae >= recovery_band
            hit_threshold = False
            recovered_after = False
            for _, _, mae in snaps:
                if not hit_threshold and mae <= threshold:
                    hit_threshold = True
                elif hit_threshold and mae >= args.recovery_band:
                    recovered_after = True
                    break
            if hit_threshold:
                mae_deep += 1
            if recovered_after:
                recovered += 1
                if actual > 0:
                    rec_won.append(t)
                else:
                    rec_lost.append(t)

        # Best-case: rescue all losses to 0R, leave wins untouched
        best_delta = sum(-actual_or_0(t) for t in rec_lost)
        # Worst-case: catch wins prematurely too
        worst_delta = best_delta + sum(-actual_or_0(t) for t in rec_won)
        # Selective: only losses (cycle-still-negative check)
        selective_delta = best_delta

        print(
            f"{threshold:>6.2f} {mae_deep:>8d} {recovered:>9d} "
            f"{len(rec_won):>7d} {len(rec_lost):>8d} "
            f"{best_delta:>+7.2f} {worst_delta:>+7.2f} {selective_delta:>+9.2f}"
        )

    print()
    print('Interpretation:')
    print('  best     = rescue all recovered LOSSES to 0R (assumes perfect signal-flip detection)')
    print('  worst    = also catch RECOVERED WINS prematurely at 0R (worst case w/ false flips)')
    print('  selective= same as best (operator-described "cycle still negative" filter)')


def actual_or_0(t):
    return t['pnl_r'] or 0.0


if __name__ == '__main__':
    main()
