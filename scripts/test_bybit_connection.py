"""Smoke-test the Bybit V5 connection end-to-end.

Reads `.env` (BYBIT_API_KEY / BYBIT_API_SECRET / BYBIT_DEMO=1), constructs
a BybitClient against the demo endpoint, and exercises the read-only
surface the bot relies on at startup:

  1. wallet-balance: total equity + collateral-pool balance
  2. instruments-info: contract spec for each symbol
  3. mark price: live tickers
  4. position list: any currently-open positions
  5. open orders: any resting limit / TP-SL on the account

Place no orders. Useful as the first thing to run after populating .env
to confirm the credentials + endpoint are wired correctly before the
bot itself starts trading.

Usage:
    python scripts/test_bybit_connection.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from src.execution.bybit_client import BybitClient, BybitCredentials


SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
]


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(60, "="))


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")

    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    demo_flag = os.environ.get("BYBIT_DEMO", "1").strip()
    if not api_key or not api_secret:
        print("ERR: BYBIT_API_KEY / BYBIT_API_SECRET missing in .env")
        return 1

    creds = BybitCredentials(
        api_key=api_key, api_secret=api_secret,
        demo=demo_flag not in ("0", "false", "False", ""),
    )
    client = BybitClient(creds)
    print(f"[OK] BybitClient constructed demo={creds.demo}")

    # 1. Balance: aggregated UTA collateral pool (USDT+USDC pooled by USD
    # value when "Used as Collateral" is on for both).
    section("wallet")
    avail = client.get_balance("USDT")
    equity = client.get_total_equity("USDT")
    print(f"  totalAvailableBalance USD : {avail:>16,.2f}")
    print(f"  totalMarginBalance    USD : {equity:>16,.2f}  "
          f"(collateral pool, matches UI 'Margin Balance')")

    # 2. Instrument specs.
    section("instrument specs")
    for sym in SYMBOLS:
        try:
            spec = client.get_instrument_spec(sym)
        except Exception as exc:
            print(f"  {sym:<18}  ERR  {exc}")
            continue
        print(f"  {sym:<18}  ct_val={spec['ct_val']:<8}  "
              f"qtyStep={spec['qty_step']:<8}  "
              f"maxLev={spec['max_leverage']}  "
              f"tick={spec['tick_size']}")

    # 3. Mark price.
    section("mark prices")
    for sym in SYMBOLS:
        try:
            mark = client.get_mark_price(sym)
        except Exception as exc:
            print(f"  {sym:<18}  ERR  {exc}")
            continue
        print(f"  {sym:<18}  mark={mark:,.4f}")

    # 4. Open positions.
    section("live positions")
    try:
        positions = client.get_positions()
    except Exception as exc:
        print(f"  ERR  {exc}")
    else:
        if not positions:
            print("  (none)")
        for snap in positions:
            print(f"  {snap.inst_id:<18}  {snap.pos_side:<6}  "
                  f"size={snap.size:<10}  entry={snap.entry_price:<10}  "
                  f"mark={snap.mark_price:<10}  upl={snap.unrealized_pnl:.4f}")

    # 5. Resting orders (entry limits + maker-TP limits).
    section("open orders")
    try:
        orders = client.list_open_orders()
    except Exception as exc:
        print(f"  ERR  {exc}")
        return 2
    if not orders:
        print("  (none)")
    for row in orders:
        print(f"  {row.get('symbol', ''):<18}  "
              f"{row.get('side', ''):<5}  "
              f"qty={row.get('qty', '?'):<10}  "
              f"price={row.get('price', '?'):<10}  "
              f"status={row.get('orderStatus', '?')}  "
              f"link={row.get('orderLinkId', '')}")

    print("\n[OK] smoke test complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
