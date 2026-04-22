"""One-shot cleanup: cancel known orphan pending limits + orphan OCOs.

Historical run (2026-04-20 post-postmortem) — now archived:
  - DOGE OCO 3494715650050920448 / ETH limit 3495274735394340864 /
    BNB limit 3495272138516185088. Script was run, orphans removed;
    the permanent fixes (revise_runner_tp journal update + startup
    reconciliation for orphans) prevent recurrence of that exact class.

2026-04-23 run — Pass 1 → Pass 2 transition. Operator manually closed
5 open positions on OKX before the DB wipe but did not also cancel
the attached OCO algos and two resting limits. Fresh-DB startup
detected them (`orphan_oco_no_journal_row` ERROR log per algo) but
left them in place per safety policy — an OCO with no journal row
might be protecting an un-tracked position, so the auto-cancel code
deliberately stays hands-off. In this specific case the orphans are
truly un-paired (0 live positions) and safe to sweep.
  - DOGE OCO 3502645596580777984 (sl=0.0972  tp=0.09584 sz=65)
  - BNB  OCO 3502601267250237440 (sl=644.6   tp=638.8   sz=1292)
  - BTC  OCO 3502294562696105984 (sl=79185.1 tp=78238.6 sz=13)
  - SOL  OCO 3502258920440238080 (sl=88.7    tp=86.07   sz=58)
  - ETH  OCO 3502241723558957056 (sl=2416.51 tp=2358.98 sz=26)
  - DOGE limit 3502730384189411328 (short @ 0.09725 sz=103)
  - SOL  limit 3502729003156099072 (short @ 87.81   sz=95)
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
# 2026-04-23 Pass 1 → Pass 2 transition orphans (see module docstring).
TARGETS: list[tuple[str, str, str]] = [
    ("algo",  "DOGE-USDT-SWAP", "3502645596580777984"),
    ("algo",  "BNB-USDT-SWAP",  "3502601267250237440"),
    ("algo",  "BTC-USDT-SWAP",  "3502294562696105984"),
    ("algo",  "SOL-USDT-SWAP",  "3502258920440238080"),
    ("algo",  "ETH-USDT-SWAP",  "3502241723558957056"),
    ("limit", "DOGE-USDT-SWAP", "3502730384189411328"),
    ("limit", "SOL-USDT-SWAP",  "3502729003156099072"),
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
