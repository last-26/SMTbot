"""Multi-timeframe rolling candle buffer.

Stores recent OHLCV bars fetched from TradingView MCP for analysis.
Supports multiple timeframes (e.g. 15m for entry, 4H for HTF bias).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from .tv_bridge import TVBridge


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: Optional[datetime] = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def total_range(self) -> float:
        return self.high - self.low


class CandleBuffer:
    """Rolling buffer of OHLCV candles for a single timeframe.

    Usage::

        buffer = CandleBuffer(max_size=500)
        buffer.update_from_ohlcv(ohlcv_data)
        last_20 = buffer.last(20)
    """

    def __init__(self, max_size: int = 500):
        self.max_size = max_size
        self._candles: deque[Candle] = deque(maxlen=max_size)

    def update_from_ohlcv(self, ohlcv_data: dict[str, Any]) -> int:
        """Update buffer from TV CLI ohlcv response.

        Returns the number of new candles added.
        """
        if not ohlcv_data.get("success"):
            return 0

        bars = ohlcv_data.get("bars", [])
        if not bars:
            return 0

        added = 0
        for bar in bars:
            candle = Candle(
                open=float(bar.get("open", 0)),
                high=float(bar.get("high", 0)),
                low=float(bar.get("low", 0)),
                close=float(bar.get("close", 0)),
                volume=float(bar.get("volume", 0)),
            )
            # Parse timestamp if available
            ts = bar.get("time") or bar.get("timestamp")
            if ts is not None:
                try:
                    if isinstance(ts, (int, float)):
                        candle.timestamp = datetime.utcfromtimestamp(ts)
                    elif isinstance(ts, str):
                        candle.timestamp = datetime.fromisoformat(ts)
                except (ValueError, OSError):
                    pass

            self._candles.append(candle)
            added += 1

        return added

    def last(self, n: int = 1) -> list[Candle]:
        """Return the last N candles (most recent last)."""
        if n >= len(self._candles):
            return list(self._candles)
        return list(self._candles)[-n:]

    @property
    def latest(self) -> Optional[Candle]:
        """Return the most recent candle, or None."""
        return self._candles[-1] if self._candles else None

    def __len__(self) -> int:
        return len(self._candles)

    def is_empty(self) -> bool:
        return len(self._candles) == 0


class MultiTFBuffer:
    """Manages candle buffers for multiple timeframes.

    Usage::

        mtf = MultiTFBuffer(bridge)
        await mtf.refresh("15")
        candles_15m = mtf.get_buffer("15").last(20)
    """

    def __init__(self, bridge: Optional[TVBridge] = None, max_size: int = 500):
        self.bridge = bridge or TVBridge()
        self.max_size = max_size
        self._buffers: dict[str, CandleBuffer] = {}

    def get_buffer(self, timeframe: str) -> CandleBuffer:
        """Get or create a buffer for a timeframe."""
        if timeframe not in self._buffers:
            self._buffers[timeframe] = CandleBuffer(max_size=self.max_size)
        return self._buffers[timeframe]

    async def refresh(self, timeframe: str, count: int = 100) -> int:
        """Fetch latest candles for a timeframe from TradingView.

        Note: This fetches candles for whatever timeframe is currently
        active on the chart. Caller should ensure the chart is on the
        correct timeframe before calling.

        Returns number of candles loaded.
        """
        ohlcv = await self.bridge.get_ohlcv(count=count)
        buf = self.get_buffer(timeframe)
        added = buf.update_from_ohlcv(ohlcv)
        logger.debug("CandleBuffer[{}]: loaded {} candles (total {})",
                      timeframe, added, len(buf))
        return added
