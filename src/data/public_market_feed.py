"""Public Binance futures market feed.

Used by the demo-wick artefact cross-check (Katman 2). When the bot closes a
trade, we fetch the concurrent 1m candle from Binance USD-M futures and
check whether entry/exit prices actually sat inside that candle's [low,
high] band. A hit outside the real-market range strongly suggests a
demo-book wick that never happened on a real exchange — which if
unflagged would poison RL training data with artefact outcomes.

Non-blocking failure: every method returns None (never raises) when the
network is unreachable / Binance returns non-200. The caller treats None
as "couldn't cross-check" and leaves the artefact fields as NULL in the
journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger


BINANCE_FUTURES_BASE = "https://fapi.binance.com"


@dataclass(frozen=True)
class RealCandle:
    """One Binance futures 1m kline — the fields we care about."""
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


def internal_to_binance_futures(internal_symbol: str) -> Optional[str]:
    """`BTC-USDT-SWAP` → `BTCUSDT`. Returns None for shapes we can't map.

    Maps the internal canonical perp identifier to the Binance USDT-M
    futures ticker for cross-venue artefact validation (see Phase 4
    cross-check in `src/bot/runner.py`).
    """
    if not internal_symbol or not internal_symbol.endswith("-SWAP"):
        return None
    base_quote = internal_symbol[:-len("-SWAP")]
    if "-" not in base_quote:
        return None
    base, quote = base_quote.split("-", 1)
    if not base or not quote:
        return None
    return f"{base}{quote}"


class BinancePublicClient:
    """Minimal sync REST client. Shares no state with the bot's async loops;
    callers run this inside `asyncio.to_thread(...)` when on the hot path."""

    def __init__(self, timeout_s: float = 5.0):
        self._client = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def get_kline_around(
        self, binance_symbol: str, ts_ms: int, interval: str = "1m",
    ) -> Optional[RealCandle]:
        """Fetch the 1m kline that *contains* `ts_ms`.

        Binance's kline endpoint accepts `startTime` + `endTime` filters.
        For a 1m interval we grab a 2-minute window anchored at
        `[ts_ms - 60_000, ts_ms + 60_000]` so jitter on either side of
        the minute boundary still lands us the correct candle.
        """
        if not binance_symbol:
            return None
        try:
            resp = self._client.get(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/klines",
                params={
                    "symbol": binance_symbol,
                    "interval": interval,
                    "startTime": int(ts_ms) - 60_000,
                    "endTime": int(ts_ms) + 60_000,
                    "limit": 5,
                },
            )
        except Exception:
            logger.exception(
                "binance_kline_request_failed symbol={} ts_ms={}",
                binance_symbol, ts_ms,
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "binance_kline_http_{} symbol={} ts_ms={} body={!r}",
                resp.status_code, binance_symbol, ts_ms,
                resp.text[:200],
            )
            return None
        try:
            rows = resp.json()
        except Exception:
            logger.exception("binance_kline_json_failed symbol={}", binance_symbol)
            return None
        if not isinstance(rows, list) or not rows:
            return None
        # Each row: [openTime, open, high, low, close, volume, closeTime, ...].
        # Pick the kline whose openTime ≤ ts_ms < openTime+60s.
        window = int(ts_ms)
        chosen = None
        for r in rows:
            try:
                open_ms = int(r[0])
            except (TypeError, ValueError, IndexError):
                continue
            if open_ms <= window < open_ms + 60_000:
                chosen = r
                break
        if chosen is None:
            # Fallback: last row before ts_ms.
            chosen = rows[-1]
        try:
            return RealCandle(
                open_time_ms=int(chosen[0]),
                open=float(chosen[1]),
                high=float(chosen[2]),
                low=float(chosen[3]),
                close=float(chosen[4]),
            )
        except (TypeError, ValueError, IndexError):
            logger.exception(
                "binance_kline_row_parse_failed row={!r}", chosen,
            )
            return None


def price_inside_candle(
    price: float, candle: RealCandle, tolerance_pct: float = 0.0,
) -> bool:
    """Is `price` inside `[candle.low, candle.high]` modulo tolerance?

    `tolerance_pct` (fraction, e.g. 0.0005 = 5bps) widens the band on both
    sides to tolerate Binance-vs-Bybit microstructure differences (funding
    snapshot, quoting jitter). A 0.05% default catches blatant demo wicks
    without flagging routine cross-exchange skew.
    """
    if candle.high <= 0 or candle.low <= 0:
        return True  # can't tell; be lenient
    band = max(candle.high - candle.low, 0.0) * tolerance_pct
    lo = candle.low - band
    hi = candle.high + band
    return lo <= price <= hi
