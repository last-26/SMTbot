"""Read-only probe: list live positions + pending limit orders on Bybit.

For diagnosing orphan / unexpected order state. Shows every resting order
on the account plus the position-attached TP/SL pair (which on Bybit V5
is part of the position itself, not a separate algo). Useful for telling
bot-placed (`smtbot*` / `smttp*` orderLinkId prefixes) vs manual vs
stale orphans.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.execution.bybit_client import BybitClient, BybitCredentials


SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP", "XRP-USDT-SWAP",
]


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    demo_flag = os.getenv("BYBIT_DEMO", "1").strip()
    client = BybitClient(
        BybitCredentials(
            api_key=os.getenv("BYBIT_API_KEY", ""),
            api_secret=os.getenv("BYBIT_API_SECRET", ""),
            demo=demo_flag not in ("0", "false", "False", ""),
        ),
    )

    print("=" * 78)
    print("LIVE POSITIONS  (with attached TP/SL fields)")
    print("=" * 78)
    positions = client.get_positions()
    if not positions:
        print("  (none)")
    for p in positions:
        if p.size == 0:
            continue
        print(f"  {p.inst_id:<18} side={p.pos_side:<5} size={p.size:>10} "
              f"entry={p.entry_price:<12} mark={p.mark_price:<12} "
              f"upl={p.unrealized_pnl:+.2f} lev={p.leverage}x")

    # Position-attached TP/SL pair (visible in /v5/position/list, not a
    # separate order). We re-query via list_pending_algos which on Bybit
    # is a back-compat shim that surfaces TP/SL from positions.
    print()
    print("=" * 78)
    print("POSITION-ATTACHED TP/SL  (Bybit V5: part of position, not a separate algo)")
    print("=" * 78)
    try:
        tpsl_rows = client.list_pending_algos()
    except Exception as exc:
        print(f"  list failed: {exc!r}")
    else:
        if not tpsl_rows:
            print("  (none)")
        for row in tpsl_rows:
            print(f"  inst={row.get('instId'):<18} "
                  f"posSide={row.get('posSide'):<5} "
                  f"slTrig={row.get('slTriggerPx'):<12} "
                  f"tpTrig={row.get('tpTriggerPx'):<12}")

    print()
    print("=" * 78)
    print("OPEN ORDERS  (resting limits: smtbot* = entry, smttp* = maker-TP)")
    print("=" * 78)
    try:
        orders = client.list_open_orders()
    except Exception as exc:
        print(f"  list failed: {exc!r}")
        return 2
    if not orders:
        print("  (none)")
    for row in orders:
        link = row.get("orderLinkId", "")
        kind = ("entry" if link.startswith("smtbot")
                else "maker-TP" if link.startswith("smttp")
                else "flat" if link.startswith("smtflat")
                else "manual?")
        print(f"  inst={row.get('symbol'):<18} side={row.get('side'):<5} "
              f"posIdx={row.get('positionIdx', '?'):<3} "
              f"qty={row.get('qty', '?'):<10} price={row.get('price', '?'):<12} "
              f"reduceOnly={row.get('reduceOnly')} status={row.get('orderStatus', '?'):<14} "
              f"link={link:<28} kind={kind}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
