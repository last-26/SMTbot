"""Discover the correct Arkham Intel API shape.

Runtime revealed 405 Method Not Allowed on every ArkhamClient call —
the client's GET assumption is wrong. This script probes the two
endpoints we need (`ws-session`, `entity-balance-changes`) with a
matrix of HTTP methods + URL variants and reports what works.

Usage:
    .venv/Scripts/python.exe scripts/probe_arkham.py

Reads ARKHAM_API_KEY from .env. Prints one line per probe with:
    METHOD PATH -> status (first 120 chars of body)

Look for a 2xx status — that's the correct shape. Share the winning
row with the maintainer so ArkhamClient can be patched.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv


BASE_CANDIDATES = [
    "https://api.arkm.com",
    "https://api.arkm.com/v1",
    "https://api.arkhamintelligence.com",
    "https://api.arkhamintelligence.com/v1",
]

WS_SESSION_PATHS = [
    "/intel/ws-session",
    "/intel/ws/session",
    "/intel/stream/session",
    "/stream/session",
    "/ws/session",
]

BALANCE_CHANGE_PATHS = [
    "/intel/entity-balance-changes",
    "/intel/balance-changes",
    "/intel/entities/balance-changes",
    "/intel/entity/balance-changes",
]

AUTH_HEADER_CANDIDATES = [
    ("API-Key", lambda k: k),
    ("Authorization", lambda k: f"Bearer {k}"),
    ("X-API-Key", lambda k: k),
    ("Arkham-API-Key", lambda k: k),
]


def _trim(body: str, n: int = 120) -> str:
    body = body.replace("\n", " ").replace("\r", " ")
    return body[:n] + ("…" if len(body) > n else "")


async def _probe_one(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
) -> tuple[int, str]:
    try:
        if method == "GET":
            resp = await client.get(url, params=params)
        elif method == "POST":
            resp = await client.post(url, json=json, params=params)
        elif method == "PUT":
            resp = await client.put(url, json=json)
        else:
            return (-1, f"unsupported method {method}")
        body = resp.text or ""
        return (resp.status_code, _trim(body))
    except httpx.HTTPError as e:
        return (-1, f"http error: {e!r}"[:120])
    except Exception as e:
        return (-1, f"exc: {e!r}"[:120])


async def probe_ws_session(api_key: str) -> None:
    print("\n=== /ws-session (create a streaming session) ===")
    print("winning row = any 2xx status\n")
    async with httpx.AsyncClient(timeout=8.0) as client:
        for header_name, header_fmt in AUTH_HEADER_CANDIDATES:
            client.headers = {header_name: header_fmt(api_key)}
            for base in BASE_CANDIDATES:
                for path in WS_SESSION_PATHS:
                    url = base + path
                    for method in ("GET", "POST", "PUT"):
                        status, body = await _probe_one(
                            client, method, url,
                            json={} if method != "GET" else None,
                        )
                        marker = "✓" if 200 <= status < 300 else " "
                        print(
                            f"{marker} {method:4s} {url}"
                            f"   header={header_name}"
                            f"   -> {status} {body}"
                        )


async def probe_balance_changes(api_key: str) -> None:
    print("\n=== /entity-balance-changes (daily CEX flows) ===")
    print("winning row = any 2xx status\n")
    params = {
        "entityIds": "binance,coinbase",
        "pricingIds": "tether,usd-coin",
        "interval": "24h",
    }
    json_body = {
        "entityIds": ["binance", "coinbase"],
        "pricingIds": ["tether", "usd-coin"],
        "interval": "24h",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        for header_name, header_fmt in AUTH_HEADER_CANDIDATES:
            client.headers = {header_name: header_fmt(api_key)}
            for base in BASE_CANDIDATES:
                for path in BALANCE_CHANGE_PATHS:
                    url = base + path
                    for method in ("GET", "POST"):
                        status, body = await _probe_one(
                            client, method, url,
                            params=params if method == "GET" else None,
                            json=json_body if method == "POST" else None,
                        )
                        marker = "✓" if 200 <= status < 300 else " "
                        print(
                            f"{marker} {method:4s} {url}"
                            f"   header={header_name}"
                            f"   -> {status} {body}"
                        )


async def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    api_key = os.environ.get("ARKHAM_API_KEY", "").strip()
    if not api_key:
        print("ARKHAM_API_KEY missing from .env — nothing to probe")
        return
    print(f"probing with api_key prefix={api_key[:8]}... (redacted tail)")
    await probe_ws_session(api_key)
    await probe_balance_changes(api_key)
    print("\nDONE. Share the rows marked ✓ with the maintainer.")


if __name__ == "__main__":
    asyncio.run(main())
