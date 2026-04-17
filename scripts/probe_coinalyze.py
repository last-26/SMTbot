"""Coinalyze API probe — verify live response shapes at runtime.

Usage: .venv/Scripts/python.exe scripts/probe_coinalyze.py

Run this once before trusting the schemas hard-coded in
`src/data/derivatives_api.py`. Prints the first 500 bytes of each endpoint
response so you can spot-check field names (`value`, `c`, `r`, `l`, `s`).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("COINALYZE_API_KEY")
BASE = "https://api.coinalyze.net/v1"


async def probe() -> None:
    if not API_KEY:
        raise SystemExit("COINALYZE_API_KEY missing in .env")

    async with httpx.AsyncClient(
        base_url=BASE,
        headers={"api_key": API_KEY},
        timeout=10.0,
    ) as c:
        # 1) Future markets — can we find BTC/USDT on Binance?
        r = await c.get("/future-markets")
        markets = r.json()
        btc_usdt = [
            m for m in markets
            if m.get("base_asset") == "BTC"
            and m.get("quote_asset") == "USDT"
            and m.get("is_perpetual")
        ]
        print(f"BTC/USDT perpetual markets: {len(btc_usdt)}")
        for m in btc_usdt[:5]:
            print(f"  {m['symbol']:25s} exchange={m.get('exchange','?'):10s} "
                  f"margined={m.get('margined')}")

        binance_btc = next(
            (m["symbol"] for m in btc_usdt if m.get("symbol", "").endswith(".A")),
            None,
        )
        if not binance_btc:
            raise SystemExit("Binance BTCUSDT perp not found in /future-markets")
        print(f"\nChosen: {binance_btc}")

        now = int(time.time())
        endpoints = [
            ("/open-interest",
             {"symbols": binance_btc, "convert_to_usd": "true"}),
            ("/funding-rate",
             {"symbols": binance_btc}),
            ("/predicted-funding-rate",
             {"symbols": binance_btc}),
            ("/long-short-ratio-history",
             {"symbols": binance_btc, "interval": "1hour",
              "from": now - 7200, "to": now}),
            ("/liquidation-history",
             {"symbols": binance_btc, "interval": "1hour",
              "from": now - 3600, "to": now,
              "convert_to_usd": "true"}),
            ("/open-interest-history",
             {"symbols": binance_btc, "interval": "1hour",
              "from": now - 26 * 3600, "to": now,
              "convert_to_usd": "true"}),
            ("/funding-rate-history",
             {"symbols": binance_btc, "interval": "1hour",
              "from": now - 48 * 3600, "to": now}),
        ]
        for path, params in endpoints:
            r = await c.get(path, params=params)
            print(f"\n{path}:  [HTTP {r.status_code}]")
            try:
                print(json.dumps(r.json(), indent=2)[:500])
            except Exception:
                print(r.text[:500])


if __name__ == "__main__":
    asyncio.run(probe())
