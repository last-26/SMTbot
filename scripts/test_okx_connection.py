"""Phase 4 smoke test — verify OKX demo credentials + client wiring.

Reads .env, constructs an OKXClient in demo mode, and performs three
read-only probes:

  1. USDT balance  (account-scope auth check)
  2. Open positions for BTC-USDT-SWAP  (swaps scope)
  3. Mark price for BTC-USDT-SWAP  (market-data only — no auth)

No orders are placed. Run with:

  .venv/Scripts/python.exe scripts/test_okx_connection.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.execution.errors import ExecutionError, OKXError
from src.execution.okx_client import OKXClient, OKXCredentials


INST = "BTC-USDT-SWAP"


def _mask(secret: str) -> str:
    if not secret:
        return "(missing)"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    api_key = os.getenv("OKX_API_KEY", "")
    api_secret = os.getenv("OKX_API_SECRET", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    demo_flag = os.getenv("OKX_DEMO_FLAG", "1")

    print("=" * 60)
    print("OKX connection smoke test")
    print("=" * 60)
    print(f"  API_KEY:    {_mask(api_key)}")
    print(f"  API_SECRET: {_mask(api_secret)}")
    print(f"  PASSPHRASE: {'(set)' if passphrase else '(missing)'}")
    print(f"  DEMO_FLAG:  {demo_flag!r}  ({'DEMO' if demo_flag == '1' else 'LIVE'})")
    print()

    missing = [k for k, v in [
        ("OKX_API_KEY", api_key),
        ("OKX_API_SECRET", api_secret),
        ("OKX_PASSPHRASE", passphrase),
    ] if not v]
    if missing:
        print(f"[FAIL] Missing env vars: {missing}")
        return 1

    if demo_flag != "1":
        print("[FAIL] Refusing to run smoke test against LIVE (demo_flag != '1').")
        return 1

    creds = OKXCredentials(
        api_key=api_key, api_secret=api_secret, passphrase=passphrase,
        demo_flag=demo_flag,
    )

    try:
        client = OKXClient(creds)
    except Exception as e:
        print(f"[FAIL] Could not construct OKXClient: {e}")
        return 1

    ok = True

    # 1. Mark price (no auth)
    print("[1/3] GET mark price (no auth)...")
    try:
        mark = client.get_mark_price(INST)
        print(f"      {INST} mark = {mark:,.2f} USDT")
    except ExecutionError as e:
        print(f"      [FAIL] {e}  (code={e.code})")
        ok = False

    # 2. Balance (needs Read)
    print("[2/3] GET balance (auth: Read)...")
    try:
        bal = client.get_balance("USDT")
        print(f"      USDT avail = {bal:,.2f}")
    except OKXError as e:
        print(f"      [FAIL] {e}  (code={e.code})")
        print("      -> Check API key permissions include Read, key belongs to DEMO env.")
        ok = False
    except ExecutionError as e:
        print(f"      [FAIL] {e}")
        ok = False

    # 3. Positions (needs Read + swap scope)
    print("[3/3] GET positions for BTC-USDT-SWAP...")
    try:
        snaps = client.get_positions(INST)
        if not snaps:
            print("      no open positions (expected on a fresh demo account)")
        else:
            for s in snaps:
                print(f"      {s.inst_id} {s.pos_side} size={s.size} "
                      f"entry={s.entry_price} lev={s.leverage}")
    except ExecutionError as e:
        print(f"      [FAIL] {e}  (code={e.code})")
        ok = False

    print()
    print("=" * 60)
    if ok:
        print("  RESULT: OK — client is wired up, ready for Phase 4 order tests.")
    else:
        print("  RESULT: FAILED — fix the errors above before proceeding.")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
