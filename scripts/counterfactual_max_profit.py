"""Counterfactual analysis: how would max-profit policies have changed outcomes?

Hypotheses (operator request 2026-05-02):
  H1: Regime-aware RR (RANGING=1.0, WEAK=1.5, STRONG=2.5)
  H2: Trailing SL after MFE 1R (lock at MFE-0.5R, snapped per +0.5R)
  H3: MAE-BE-lock (MAE <= threshold + recovery to entry -> SL=entry)

Data: trades + position_snapshots from data/trades.db
Snap cadence 300s -> fast-TP trades may show mfe < realized_pnl (undersampling).
H1/H2 use max(max_mfe, max(0, actual_pnl)) as effective_mfe to correct this.
For LOSS trades (pnl<0), realized_pnl-based mfe correction not applied.

This is a CONSERVATIVE bound vs. true kline-walk replay.
"""

import argparse
import math
import sqlite3
from collections import defaultdict


def load_data(db_path: str, clean_since: str):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    trades = list(db.execute(
        "SELECT trade_id, symbol, direction, trend_regime_at_entry, entry_price, "
        "sl_price, tp_price, pnl_r, pnl_usdt, close_reason, exit_timestamp "
        "FROM trades WHERE entry_timestamp >= ? AND exit_timestamp IS NOT NULL "
        "ORDER BY entry_timestamp",
        (clean_since,),
    ))

    snaps_by_trade = defaultdict(list)
    for r in db.execute(
        'SELECT trade_id, captured_at, mfe_r_so_far, mae_r_so_far '
        'FROM position_snapshots ORDER BY captured_at'
    ):
        snaps_by_trade[r['trade_id']].append(
            (r['captured_at'], r['mfe_r_so_far'], r['mae_r_so_far'])
        )
    db.close()
    return trades, snaps_by_trade


def effective_mfe_mae(trade, snaps):
    """Return (eff_mfe, eff_mae) corrected for snap-cadence undersampling."""
    actual_pnl = trade['pnl_r'] or 0.0
    raw_mfe = max((s[1] for s in snaps), default=0.0)
    raw_mae = min((s[2] for s in snaps), default=0.0)
    # If trade WON, the realized pnl IS reached -> mfe >= pnl
    eff_mfe = max(raw_mfe, max(0.0, actual_pnl))
    # If trade LOST, the realized pnl IS reached -> mae <= pnl
    eff_mae = min(raw_mae, min(0.0, actual_pnl))
    return eff_mfe, eff_mae


def run_h1(trades, snaps_by_trade, regime_rr):
    """Regime-aware RR. Outcome = capped TP if mfe>=target, else actual."""
    total = 0.0
    by_regime = defaultdict(lambda: {'n': 0, 'sum_a': 0.0, 'sum_h1': 0.0})
    wins = losses = 0
    for t in trades:
        actual = t['pnl_r'] or 0.0
        rg = t['trend_regime_at_entry']
        snaps = snaps_by_trade.get(t['trade_id'], [])
        eff_mfe, eff_mae = effective_mfe_mae(t, snaps)
        target = regime_rr.get(rg)
        if target is None:
            new_pnl = actual
        elif eff_mfe >= target and eff_mae > -1.0:
            new_pnl = target
        elif eff_mae <= -1.0 and eff_mfe < target:
            new_pnl = -1.0
        elif eff_mfe >= target and eff_mae <= -1.0:
            new_pnl = target if actual > 0 else -1.0
        else:
            new_pnl = actual
        total += new_pnl
        if new_pnl > 0:
            wins += 1
        elif new_pnl < 0:
            losses += 1
        by_regime[rg or 'NULL']['n'] += 1
        by_regime[rg or 'NULL']['sum_a'] += actual
        by_regime[rg or 'NULL']['sum_h1'] += new_pnl
    return total, wins, losses, by_regime


def run_h2(trades, snaps_by_trade, mfe_arm: float, trail_distance_r: float):
    """Trailing SL: arm at mfe>=mfe_arm, lock at mfe - trail_distance_r."""
    total = 0.0
    helped = 0
    for t in trades:
        actual = t['pnl_r'] or 0.0
        snaps = snaps_by_trade.get(t['trade_id'], [])
        eff_mfe, _ = effective_mfe_mae(t, snaps)
        if eff_mfe >= mfe_arm:
            locked = eff_mfe - trail_distance_r
            # snap to 0.5R steps (avoids TP-flicker noise)
            locked_snap = math.floor(locked * 2) / 2
            if actual < locked_snap:
                new_pnl = locked_snap
                helped += 1
            else:
                new_pnl = actual
        else:
            new_pnl = actual
        total += new_pnl
    return total, helped


def run_h3(trades, snaps_by_trade, mae_threshold: float):
    """MAE-BE-lock: MAE<=threshold + recovery to entry -> close at 0R.

    Approximation without fine-grained kline data:
      - if eff_mae <= mae_threshold AND mae_threshold < actual_pnl < 0
        -> price went to threshold, recovered partially, would have caught at 0R
      - if eff_mae <= mae_threshold AND actual_pnl <= mae_threshold (close to SL)
        -> not caught (price didn't recover enough)
    """
    total = 0.0
    caught = 0
    for t in trades:
        actual = t['pnl_r'] or 0.0
        snaps = snaps_by_trade.get(t['trade_id'], [])
        _, eff_mae = effective_mfe_mae(t, snaps)
        # Recovery condition: MAE deeper than threshold, but actual closed less negative
        # than threshold (i.e. price came back at least past threshold level)
        # AND actual is still negative (otherwise irrelevant)
        if eff_mae <= mae_threshold and mae_threshold < actual < 0:
            new_pnl = 0.0
            caught += 1
        else:
            new_pnl = actual
        total += new_pnl
    return total, caught


def run_combined(trades, snaps_by_trade, regime_rr, mfe_arm, trail_dist, mae_threshold):
    """Priority: H3 (MAE-BE-lock) > H1 (regime-RR) > H2 (trailing)."""
    total = 0.0
    breakdown = defaultdict(int)
    for t in trades:
        actual = t['pnl_r'] or 0.0
        rg = t['trend_regime_at_entry']
        snaps = snaps_by_trade.get(t['trade_id'], [])
        eff_mfe, eff_mae = effective_mfe_mae(t, snaps)

        if eff_mae <= mae_threshold and mae_threshold < actual < 0:
            new_pnl = 0.0
            breakdown['h3_be'] += 1
        elif rg in regime_rr:
            target = regime_rr[rg]
            if eff_mfe >= target and eff_mae > -1.0:
                new_pnl = target
                breakdown['h1_win'] += 1
            elif eff_mae <= -1.0 and eff_mfe < target:
                new_pnl = -1.0
                breakdown['h1_loss'] += 1
            elif eff_mfe >= target and eff_mae <= -1.0:
                new_pnl = target if actual > 0 else -1.0
                breakdown['h1_win' if actual > 0 else 'h1_loss'] += 1
            elif eff_mfe >= mfe_arm:
                locked = eff_mfe - trail_dist
                locked_snap = math.floor(locked * 2) / 2
                if actual < locked_snap:
                    new_pnl = locked_snap
                    breakdown['h2_trail'] += 1
                else:
                    new_pnl = actual
                    breakdown['unchanged'] += 1
            else:
                new_pnl = actual
                breakdown['unchanged'] += 1
        else:
            new_pnl = actual
            breakdown['unchanged'] += 1
        total += new_pnl
    return total, breakdown


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', default='data/trades.db')
    p.add_argument('--since', default='2026-04-25T21:30:00Z')
    p.add_argument(
        '--regime-rr',
        default='RANGING=1.0,WEAK_TREND=1.5,STRONG_TREND=2.5',
        help='comma-list regime=rr',
    )
    p.add_argument('--mfe-arm', type=float, default=1.0,
                   help='H2 trailing SL arm threshold (R)')
    p.add_argument('--trail-dist', type=float, default=0.5,
                   help='H2 trail distance behind MFE (R)')
    p.add_argument('--mae-threshold', type=float, default=-0.7,
                   help='H3 MAE threshold for BE-lock (R, negative)')
    args = p.parse_args()

    regime_rr = {kv.split('=')[0]: float(kv.split('=')[1])
                 for kv in args.regime_rr.split(',')}

    trades, snaps_by_trade = load_data(args.db, args.since)
    total = len(trades)
    actual_total = sum((t['pnl_r'] or 0) for t in trades)
    wins = sum(1 for t in trades if (t['pnl_r'] or 0) > 0)
    losses = sum(1 for t in trades if (t['pnl_r'] or 0) < 0)

    print('=' * 80)
    print(f"BASELINE (target_rr=1.5, MFE-lock 1.0R->BE, no trailing)")
    print('=' * 80)
    print(
        f"n={total} W={wins} L={losses} sum_R={actual_total:+.2f} "
        f"WR={wins/total*100:.1f}% avg_R={actual_total/total:+.3f}"
    )
    print()

    print('=' * 80)
    print('Effective-MFE/MAE distribution by regime (snap-undersampling corrected)')
    print('=' * 80)
    print(
        f"{'regime':12s} {'n':>3s} {'sum_R':>7s} "
        f"{'avg_eff_mfe':>11s} {'avg_eff_mae':>11s} "
        f"{'mfe>=1':>7s} {'mfe>=1.5':>9s} {'mfe>=2':>7s} {'mfe>=2.5':>9s} "
        f"{'mae<=-0.7':>10s} {'mae<=-1':>8s}"
    )
    by_regime = defaultdict(list)
    for t in trades:
        rg = t['trend_regime_at_entry'] or 'NULL'
        snaps = snaps_by_trade.get(t['trade_id'], [])
        eff_mfe, eff_mae = effective_mfe_mae(t, snaps)
        by_regime[rg].append((t, eff_mfe, eff_mae))

    for rg, items in sorted(by_regime.items()):
        n = len(items)
        sum_r = sum((t['pnl_r'] or 0) for t, _, _ in items)
        avg_mfe = sum(mfe for _, mfe, _ in items) / n
        avg_mae = sum(mae for _, _, mae in items) / n
        c = lambda thr, eff: sum(1 for _, e, _ in items if e >= thr) if eff == 'mfe' \
            else sum(1 for _, _, m in items if m <= thr)
        print(
            f"{rg:12s} {n:>3d} {sum_r:>7.2f} "
            f"{avg_mfe:>11.2f} {avg_mae:>11.2f} "
            f"{c(1.0,'mfe'):>7d} {c(1.5,'mfe'):>9d} {c(2.0,'mfe'):>7d} {c(2.5,'mfe'):>9d} "
            f"{c(-0.7,'mae'):>10d} {c(-1.0,'mae'):>8d}"
        )

    print()
    print('=' * 80)
    h1_str = ', '.join(f'{k}={v}' for k, v in regime_rr.items())
    print(f'H1: Regime-aware RR ({h1_str})')
    print('=' * 80)
    h1_total, h1_w, h1_l, by_regime_h1 = run_h1(trades, snaps_by_trade, regime_rr)
    print(f"BASELINE sum_R={actual_total:+.2f}")
    print(f"H1       sum_R={h1_total:+.2f} W={h1_w} L={h1_l}")
    print(f"DELTA    {h1_total-actual_total:+.2f}R "
          f"({(h1_total-actual_total)*10:+.2f}$ at $10/R)")
    print()
    for rg, d in sorted(by_regime_h1.items()):
        delta = d['sum_h1'] - d['sum_a']
        print(
            f"  {rg:12s} n={d['n']:>3d} actual={d['sum_a']:+7.2f} "
            f"h1={d['sum_h1']:+7.2f} delta={delta:+7.2f}"
        )

    print()
    print('=' * 80)
    print(f'H2: Trailing SL (arm={args.mfe_arm}R, trail={args.trail_dist}R, snap=0.5R)')
    print('=' * 80)
    h2_total, h2_helped = run_h2(trades, snaps_by_trade, args.mfe_arm, args.trail_dist)
    print(f"BASELINE sum_R={actual_total:+.2f}")
    print(f"H2       sum_R={h2_total:+.2f}")
    print(f"DELTA    {h2_total-actual_total:+.2f}R "
          f"({(h2_total-actual_total)*10:+.2f}$ at $10/R)")
    print(f"trades_with_extra_locked_profit={h2_helped}")

    print()
    print('=' * 80)
    print(f'H3: MAE-BE-lock (threshold={args.mae_threshold}R)')
    print('=' * 80)
    h3_total, h3_caught = run_h3(trades, snaps_by_trade, args.mae_threshold)
    print(f"BASELINE sum_R={actual_total:+.2f}")
    print(f"H3       sum_R={h3_total:+.2f}")
    print(f"DELTA    {h3_total-actual_total:+.2f}R "
          f"({(h3_total-actual_total)*10:+.2f}$ at $10/R)")
    print(f"recoveries_caught={h3_caught}")

    print()
    print('=' * 80)
    print('COMBINED H1+H2+H3 (max-profit policy candidate)')
    print('=' * 80)
    print('Priority order: H3 -> H1 -> H2')
    c_total, breakdown = run_combined(
        trades, snaps_by_trade, regime_rr,
        args.mfe_arm, args.trail_dist, args.mae_threshold,
    )
    print(f"BASELINE sum_R={actual_total:+.2f} avg={actual_total/total:+.3f}R/trade")
    print(f"COMBINED sum_R={c_total:+.2f} avg={c_total/total:+.3f}R/trade")
    print(
        f"DELTA    {c_total-actual_total:+.2f}R "
        f"({(c_total-actual_total)/total:+.3f}R/trade) "
        f"({(c_total-actual_total)*10:+.2f}$ at $10/R)"
    )
    print(f"breakdown: {dict(breakdown)}")


if __name__ == '__main__':
    main()
