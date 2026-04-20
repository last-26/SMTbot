"""One-shot cleanup: cancel known orphan pending limits + orphan OCOs.

Orphans identified 2026-04-20 post-postmortem:
  - DOGE OCO 3494715650050920448 (sl=0.09296) — stale from 05:44 revise whose
    new algoId was never written back to the journal, so the 07:07 restart
    rehydrated the pre-revise id and left this one un-tracked on OKX.
  - ETH pending limit 3495274735394340864 (@ 2280.04 sz=29) — pre-restart
    resting limit; startup reconciliation does not rehydrate pending limits.
  - BNB pending limit 3495272138516185088 (@ 620.3  sz=1182) — same class.

Run once, then the permanent fixes (revise_runner_tp journal update +
startup reconciliation for orphans) prevent recurrence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from src.execution.errors import OKXError, OrderRejected
from src.execution.okx_client import OKXClient, OKXCredentials


# (kind, inst_id, id)  — kind in {"limit", "algo"}
TARGETS: list[tuple[str, str, str]] = [
    ("algo", "DOGE-USDT-SWAP", "3494715650050920448"),
    ("limit", "ETH-USDT-SWAP", "3495274735394340864"),
    ("limit", "BNB-USDT-SWAP", "3495272138516185088"),
]

# OKX "already-gone" codes that we treat as idempotent success.
_GONE_CODES = {"51400", "51401", "51402"}


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

    failed = 0
    for kind, inst_id, ident in TARGETS:
        label = f"{kind:<5} {inst_id:<16} id={ident}"
        try:
            if kind == "limit":
                client.cancel_order(inst_id, ident)
            elif kind == "algo":
                client.cancel_algo(inst_id, ident)
            else:
                print(f"  SKIP   {label} — unknown kind {kind!r}")
                continue
            print(f"  OK     {label}")
        except (OKXError, OrderRejected) as exc:
            code = getattr(exc, "code", "") or ""
            if code in _GONE_CODES:
                print(f"  GONE   {label} (already canceled/filled, code={code})")
            else:
                print(f"  FAIL   {label} — code={code} err={exc}")
                failed += 1
        except Exception as exc:
            print(f"  FAIL   {label} — {exc!r}")
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
