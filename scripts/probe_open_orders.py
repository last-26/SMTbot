"""Read-only probe: list live positions + pending limit orders + algos.

For diagnosing the "2 extra limit orders while 5 positions open" report:
shows every resting order / algo on the account so we can tell bot-placed
vs manual vs stale orphans.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.execution.okx_client import OKXClient, OKXCredentials


SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP", "BNB-USDT-SWAP",
]


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    client = OKXClient(
        OKXCredentials(
            api_key=os.getenv("OKX_API_KEY", ""),
            api_secret=os.getenv("OKX_API_SECRET", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            demo_flag=os.getenv("OKX_DEMO_FLAG", "1"),
        ),
    )

    print("=" * 70)
    print("LIVE POSITIONS")
    print("=" * 70)
    positions = client.get_positions()
    for p in positions:
        if p.size == 0:
            continue
        print(f"  {p.inst_id:<18} side={p.pos_side:<5} size={p.size:>8} "
              f"entry={p.entry_price:<12} mark={p.mark_price:<12} "
              f"upl={p.unrealized_pnl:+.2f} lev={p.leverage}x")

    print()
    print("=" * 70)
    print("PENDING LIMIT ORDERS (ordType=post_only/limit, resting)")
    print("=" * 70)
    resp = client.trade.get_order_list(instType="SWAP")
    for row in resp.get("data", []) or []:
        print(f"  inst={row.get('instId'):<18} side={row.get('side'):<5} "
              f"posSide={row.get('posSide'):<5} ordType={row.get('ordType'):<10} "
              f"sz={row.get('sz'):<8} px={row.get('px'):<12} "
              f"state={row.get('state')} ordId={row.get('ordId')} "
              f"clOrdId={row.get('clOrdId')}")

    print()
    print("=" * 70)
    print("PENDING ALGOS (OCO SL/TP + conditional)")
    print("=" * 70)
    for ord_type in ("oco", "conditional", "trigger"):
        try:
            algos = client.list_pending_algos(ord_type=ord_type)
        except Exception as e:
            print(f"  [{ord_type}] list failed: {e!r}")
            continue
        if not algos:
            continue
        for a in algos:
            print(f"  [{ord_type}] inst={a.get('instId'):<18} "
                  f"posSide={a.get('posSide'):<5} sz={a.get('sz'):<8} "
                  f"slTrig={a.get('slTriggerPx'):<12} "
                  f"tpTrig={a.get('tpTriggerPx'):<12} "
                  f"algoId={a.get('algoId')} state={a.get('state')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
