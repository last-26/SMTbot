"""SL placement helpers for deriving stop-loss from structural levels.

An SL placed at the "natural invalidation" of a setup survives noise better
than an arbitrary percentage stop. We always push the stop PAST the level
by an ATR-scaled buffer so wick-hunting doesn't trigger it.

Supported sources (in preference order used by `entry_signals.py`):
  1. Order Block    — below OB bottom (long) / above OB top (short)
  2. Fair Value Gap — below FVG bottom (long) / above FVG top (short)
  3. Swing point    — below recent swing low (long) / above swing high (short)
  4. ATR fallback   — entry ± atr_multiple * ATR when nothing else is available

Every helper pushes the stop by `buffer_mult * atr` beyond the level.
CLAUDE.md suggests 0.2 ATR as the default buffer.
"""

from __future__ import annotations

from src.analysis.fvg import FVG
from src.analysis.order_blocks import OrderBlock as PyOrderBlock
from src.data.candle_buffer import Candle
from src.data.models import Direction, FVGZone, OrderBlock


def _atr_buffer(atr: float, buffer_mult: float) -> float:
    if atr < 0 or buffer_mult < 0:
        raise ValueError("atr and buffer_mult must be non-negative")
    return atr * buffer_mult


def sl_from_order_block(
    ob: PyOrderBlock | OrderBlock,
    atr: float,
    direction: Direction,
    buffer_mult: float = 0.2,
) -> float:
    """SL just past the OB on the invalidation side."""
    buf = _atr_buffer(atr, buffer_mult)
    if direction == Direction.BULLISH:
        return ob.bottom - buf
    if direction == Direction.BEARISH:
        return ob.top + buf
    raise ValueError("direction must be BULLISH or BEARISH")


def sl_from_fvg(
    fvg: FVG | FVGZone,
    atr: float,
    direction: Direction,
    buffer_mult: float = 0.2,
) -> float:
    """SL just past the FVG on the invalidation side."""
    buf = _atr_buffer(atr, buffer_mult)
    if direction == Direction.BULLISH:
        return fvg.bottom - buf
    if direction == Direction.BEARISH:
        return fvg.top + buf
    raise ValueError("direction must be BULLISH or BEARISH")


def sl_from_swing(
    swing_price: float,
    atr: float,
    direction: Direction,
    buffer_mult: float = 0.2,
) -> float:
    """SL just past a swing high/low."""
    buf = _atr_buffer(atr, buffer_mult)
    if direction == Direction.BULLISH:
        return swing_price - buf
    if direction == Direction.BEARISH:
        return swing_price + buf
    raise ValueError("direction must be BULLISH or BEARISH")


def sl_from_atr(
    entry_price: float,
    atr: float,
    direction: Direction,
    atr_multiple: float = 2.0,
) -> float:
    """Last-resort SL when no structural level is available."""
    if entry_price <= 0 or atr <= 0 or atr_multiple <= 0:
        raise ValueError("entry_price, atr, atr_multiple must be positive")
    if direction == Direction.BULLISH:
        return entry_price - atr * atr_multiple
    if direction == Direction.BEARISH:
        return entry_price + atr * atr_multiple
    raise ValueError("direction must be BULLISH or BEARISH")


def recent_swing_price(
    candles: list[Candle],
    direction: Direction,
    lookback: int = 20,
) -> float | None:
    """Lowest low (bull) or highest high (bear) over last `lookback` candles."""
    if not candles:
        return None
    window = candles[-lookback:] if lookback > 0 else candles
    if direction == Direction.BULLISH:
        return min(c.low for c in window)
    if direction == Direction.BEARISH:
        return max(c.high for c in window)
    raise ValueError("direction must be BULLISH or BEARISH")
