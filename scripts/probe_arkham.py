"""Arkham endpoint probes for FAZ 2-4 capability planning.

Earlier (April 2026) this script was an exploratory matrix to discover the
correct base URL + auth header. Those are now known and bundled into
`ArkhamClient` (`https://api.arkm.com`, header `API-Key`). The legacy probes
are kept at the bottom of this file behind `--legacy` for forensic use.

The DEFAULT run executes four targeted probes that gate FAZ 2-4 of the
2026-04-22 Arkham expansion plan:

  * probe_subscription_usage  — current label-lookup quota burn (start +
                                 end snapshots so each probe's per-call
                                 cost is measurable).
  * probe_entity_flow         — `/flow/entity/{entity}` for binance,
                                 coinbase, bybit. Validates Bybit slug,
                                 reports response shape + time window.
  * probe_token_volume_gran   — `/token/volume/bitcoin?granularity=X`
                                 across 5m, 15m, 30m, 1h. Determines if
                                 sub-hourly aggregation is supported.
  * probe_histogram_subhourly — `/transfers/histogram` with custom
                                 timeGte/timeLte 15m window. Determines
                                 if minute-precision windows are accepted.

Usage:
    .venv/Scripts/python.exe scripts/probe_arkham.py
    .venv/Scripts/python.exe scripts/probe_arkham.py --legacy   # discovery matrix

Reads ARKHAM_API_KEY from .env. Total cost target: <30 credits, ~0 labels.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

ARKHAM_BASE = "https://api.arkm.com"


def _trim(body: str, n: int = 200) -> str:
    body = body.replace("\n", " ").replace("\r", " ")
    return body[:n] + ("..." if len(body) > n else "")


def _usage_from_headers(resp: httpx.Response) -> dict[str, float]:
    try:
        return {
            "usage": float(resp.headers.get("X-Intel-Datapoints-Usage", "0") or 0),
            "limit": float(resp.headers.get("X-Intel-Datapoints-Limit", "0") or 0),
            "remaining": float(resp.headers.get("X-Intel-Datapoints-Remaining", "0") or 0),
        }
    except ValueError:
        return {}


async def _get(
    client: httpx.AsyncClient, path: str, *, params: Optional[dict] = None,
) -> tuple[int, str, dict[str, float], Any]:
    """GET wrapper. Returns (status, body_text, usage_headers, parsed_json_or_None)."""
    try:
        resp = await client.get(ARKHAM_BASE + path, params=params)
        usage = _usage_from_headers(resp)
        try:
            parsed = resp.json()
        except Exception:
            parsed = None
        return (resp.status_code, _trim(resp.text or ""), usage, parsed)
    except httpx.HTTPError as e:
        return (-1, f"http error: {e!r}"[:200], {}, None)


# ── PROBE: subscription usage ──────────────────────────────────────────────


async def probe_subscription_usage(client: httpx.AsyncClient, label: str) -> dict[str, float]:
    """Snapshot current label-lookup usage. Called BEFORE and AFTER probe
    block so per-feature label cost is measurable.

    Confirmed endpoint: `/subscription/intel-usage` (NOT `/user/usage`,
    which 405s on the trial plan). Response shape:
      {totalCount, totalLimit, chainUsage: {chain: {count}}, periodStart}
    """
    print(f"\n=== usage snapshot ({label}) ===")
    status, body, _hdrs, parsed = await _get(client, "/subscription/intel-usage")
    if 200 <= status < 300 and isinstance(parsed, dict):
        total = parsed.get("totalCount")
        limit = parsed.get("totalLimit")
        period = parsed.get("periodStart")
        print(f"  GET /subscription/intel-usage -> {status}")
        print(f"    label_lookups: {total} / {limit}  (period start: {period})")
        chain_usage = parsed.get("chainUsage") or {}
        if isinstance(chain_usage, dict):
            non_zero = {c: d.get("count") for c, d in chain_usage.items()
                        if isinstance(d, dict) and d.get("count")}
            if non_zero:
                print(f"    by chain: {non_zero}")
        return {"usage": float(total or 0), "limit": float(limit or 0)}
    print(f"  GET /subscription/intel-usage -> {status} {body[:120]}")
    return {}


# ── PROBE: /flow/entity/{entity} for Coinbase + Binance + Bybit ────────────


ENTITY_SLUGS_TO_TRY = [
    # Operator-asserted primary slugs first.
    ("coinbase",),
    ("binance",),
    ("bybit", "bybit-exchange", "bybit-global"),  # alternates if 'bybit' 404s
]


async def probe_entity_flow(client: httpx.AsyncClient) -> None:
    print("\n=== /flow/entity/{entity} (Coinbase + Binance + Bybit) ===")
    print("checking: slug acceptance, response shape, time window, label cost\n")
    for slug_alternates in ENTITY_SLUGS_TO_TRY:
        for slug in slug_alternates:
            usage_before = _usage_from_headers
            status, body, usage, parsed = await _get(
                client, f"/flow/entity/{slug}",
            )
            marker = "OK" if 200 <= status < 300 else " "
            print(f"  {marker} GET /flow/entity/{slug} -> {status}")
            if not (200 <= status < 300):
                print(f"      body: {body}")
                # Try next alternate slug.
                await asyncio.sleep(1.1)
                continue
            # Response shape: dict keyed by entity id, value is a list
            # of {time, inflow, outflow, cumulativeInflow, cumulativeOutflow}.
            if isinstance(parsed, dict):
                series = None
                for v in parsed.values():
                    if isinstance(v, list):
                        series = v
                        break
                if series is None:
                    print(f"      unexpected shape: {body}")
                else:
                    n = len(series)
                    if n > 0 and isinstance(series[0], dict):
                        first_t = series[0].get("time")
                        last_t = series[-1].get("time")
                        keys = list(series[0].keys())
                        print(f"      series_len={n} keys={keys}")
                        print(f"      first_time={first_t}")
                        print(f"      last_time={last_t}")
                        # Show the most recent point's flows.
                        last = series[-1]
                        in_, out = last.get("inflow"), last.get("outflow")
                        print(f"      last: inflow={in_} outflow={out} net={(in_ or 0) - (out or 0):.2f}")
                    else:
                        print(f"      series_len={n} (empty or unexpected entries)")
            else:
                print(f"      body: {body}")
            if usage:
                print(
                    f"      usage_after: {usage.get('usage'):.0f}/"
                    f"{usage.get('limit'):.0f}"
                )
            await asyncio.sleep(1.1)
            break  # primary slug worked, skip alternates
        else:
            print(f"  FAIL ALL SLUGS FAILED for {slug_alternates[0]} group")


# ── PROBE: /token/volume/{id} granularity options ─────────────────────────


GRANULARITIES_TO_TRY = ["5m", "15m", "30m", "1h"]


async def probe_token_volume_granularity(client: httpx.AsyncClient) -> None:
    print("\n=== /token/volume/bitcoin granularity options ===")
    print("checking: which sub-hourly granularities (if any) Arkham accepts\n")
    for gran in GRANULARITIES_TO_TRY:
        status, body, usage, parsed = await _get(
            client, "/token/volume/bitcoin",
            params={"timeLast": "24h", "granularity": gran},
        )
        marker = "OK" if 200 <= status < 300 else " "
        print(f"  {marker} granularity={gran:4s} -> {status}")
        if 200 <= status < 300 and isinstance(parsed, list) and parsed:
            n = len(parsed)
            first = parsed[0] if isinstance(parsed[0], dict) else None
            last = parsed[-1] if isinstance(parsed[-1], dict) else None
            if first and last:
                print(f"      buckets={n} first_time={first.get('time')} last_time={last.get('time')}")
                # Compute approximate bucket span if 'time' fields parse.
                try:
                    f_t = datetime.fromisoformat(str(first.get("time")).replace("Z", "+00:00"))
                    l_t = datetime.fromisoformat(str(last.get("time")).replace("Z", "+00:00"))
                    if n > 1:
                        bucket_span_s = (l_t - f_t).total_seconds() / (n - 1)
                        print(f"      avg bucket span ≈ {bucket_span_s:.0f}s")
                except Exception:
                    pass
                print(f"      sample bucket: {json.dumps(last)[:200]}")
        elif not (200 <= status < 300):
            print(f"      body: {body}")
        if usage:
            print(f"      usage: {usage.get('usage'):.0f}/{usage.get('limit'):.0f}")
        await asyncio.sleep(1.1)


# ── PROBE: /transfers/histogram custom sub-hourly window ──────────────────


async def probe_histogram_subhourly(client: httpx.AsyncClient) -> None:
    print("\n=== /transfers/histogram with custom timeGte/timeLte 15m window ===")
    print("checking: does Arkham accept minute-precision absolute windows\n")
    now_ms = int(time.time() * 1000)
    fifteen_min_ago_ms = now_ms - 15 * 60 * 1000
    params = {
        "base": "type:cex",
        "tokens": "tether,usd-coin",
        "flow": "in",
        "timeGte": str(fifteen_min_ago_ms),
        "timeLte": str(now_ms),
    }
    status, body, usage, parsed = await _get(
        client, "/transfers/histogram", params=params,
    )
    marker = "OK" if 200 <= status < 300 else " "
    print(f"  {marker} GET /transfers/histogram (15m window) -> {status}")
    if 200 <= status < 300:
        if isinstance(parsed, list):
            print(f"      buckets={len(parsed)}")
            if parsed:
                print(f"      first: {json.dumps(parsed[0])[:200]}")
                print(f"      last:  {json.dumps(parsed[-1])[:200]}")
            else:
                print("      empty bucket array — endpoint accepts window but no data in 15m")
        elif isinstance(parsed, dict):
            print(f"      response: {json.dumps(parsed, indent=2)[:300]}")
        else:
            print(f"      body: {body}")
    else:
        print(f"      body: {body}")
    if usage:
        print(f"      usage: {usage.get('usage'):.0f}/{usage.get('limit'):.0f}")


# ── Legacy discovery matrix (April 2026) ──────────────────────────────────


BASE_CANDIDATES = [
    "https://api.arkm.com",
    "https://api.arkm.com/v1",
    "https://api.arkhamintelligence.com",
    "https://api.arkhamintelligence.com/v1",
]
WS_SESSION_PATHS = [
    "/intel/ws-session", "/intel/ws/session", "/intel/stream/session",
    "/stream/session", "/ws/session",
]
BALANCE_CHANGE_PATHS = [
    "/intel/entity-balance-changes", "/intel/balance-changes",
    "/intel/entities/balance-changes", "/intel/entity/balance-changes",
]
AUTH_HEADER_CANDIDATES = [
    ("API-Key", lambda k: k),
    ("Authorization", lambda k: f"Bearer {k}"),
    ("X-API-Key", lambda k: k),
    ("Arkham-API-Key", lambda k: k),
]


async def _legacy_probe_one(
    client: httpx.AsyncClient, method: str, url: str, *,
    params: Optional[dict] = None, json_body: Optional[dict] = None,
) -> tuple[int, str]:
    try:
        if method == "GET":
            resp = await client.get(url, params=params)
        elif method == "POST":
            resp = await client.post(url, json=json_body, params=params)
        elif method == "PUT":
            resp = await client.put(url, json=json_body)
        else:
            return (-1, f"unsupported method {method}")
        return (resp.status_code, _trim(resp.text or "", 120))
    except Exception as e:
        return (-1, f"exc: {e!r}"[:120])


async def _legacy_probe_ws_session(api_key: str) -> None:
    print("\n=== [LEGACY] /ws-session matrix ===")
    async with httpx.AsyncClient(timeout=8.0) as client:
        for header_name, header_fmt in AUTH_HEADER_CANDIDATES:
            client.headers = {header_name: header_fmt(api_key)}
            for base in BASE_CANDIDATES:
                for path in WS_SESSION_PATHS:
                    for method in ("GET", "POST", "PUT"):
                        status, body = await _legacy_probe_one(
                            client, method, base + path,
                            json_body={} if method != "GET" else None,
                        )
                        marker = "OK" if 200 <= status < 300 else " "
                        print(f"{marker} {method:4s} {base+path}   header={header_name}   -> {status} {body}")


async def _legacy_probe_balance_changes(api_key: str) -> None:
    print("\n=== [LEGACY] /entity-balance-changes matrix ===")
    params = {"entityIds": "binance,coinbase", "pricingIds": "tether,usd-coin", "interval": "24h"}
    json_body = {"entityIds": ["binance", "coinbase"], "pricingIds": ["tether", "usd-coin"], "interval": "24h"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        for header_name, header_fmt in AUTH_HEADER_CANDIDATES:
            client.headers = {header_name: header_fmt(api_key)}
            for base in BASE_CANDIDATES:
                for path in BALANCE_CHANGE_PATHS:
                    for method in ("GET", "POST"):
                        status, body = await _legacy_probe_one(
                            client, method, base + path,
                            params=params if method == "GET" else None,
                            json_body=json_body if method == "POST" else None,
                        )
                        marker = "OK" if 200 <= status < 300 else " "
                        print(f"{marker} {method:4s} {base+path}   header={header_name}   -> {status} {body}")


# ── main ──────────────────────────────────────────────────────────────────


async def main(legacy: bool = False) -> None:
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    api_key = os.environ.get("ARKHAM_API_KEY", "").strip()
    if not api_key:
        print("ARKHAM_API_KEY missing from .env — nothing to probe")
        return
    print(f"probing with api_key prefix={api_key[:8]}... (redacted tail)")

    if legacy:
        await _legacy_probe_ws_session(api_key)
        await _legacy_probe_balance_changes(api_key)
        return

    headers = {"API-Key": api_key}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        usage_start = await probe_subscription_usage(client, "BEFORE")
        await probe_entity_flow(client)
        await probe_token_volume_granularity(client)
        await probe_histogram_subhourly(client)
        usage_end = await probe_subscription_usage(client, "AFTER")
        if usage_start and usage_end:
            delta = usage_end.get("usage", 0) - usage_start.get("usage", 0)
            print(f"\n>>> TOTAL LABEL DELTA THIS RUN: {delta:.0f} (target <30) <<<")

    print("\nDONE.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true",
                        help="run the original discovery matrix instead")
    args = parser.parse_args()
    asyncio.run(main(legacy=args.legacy))
