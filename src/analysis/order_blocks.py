"""Order Block (OB) detection.

An Order Block is the last opposing candle immediately before an impulsive
move. It marks the zone where smart money likely placed orders to drive the
move. OBs often act as support/resistance when price returns.

- Bullish OB: last bearish candle before a strong bullish impulse.
  Zone = [ob_candle.low, ob_candle.high] (or body only, configurable).
- Bearish OB: last bullish candle before a strong bearish impulse.

An OB is "BROKEN" once price closes beyond the OB in the opposite direction
of its intent (bullish OB broken when close < ob.low).

Supplements smt_overlay.pine for HTF analysis without chart switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data.candle_buffer import Candle
from src.data.models import Direction


@dataclass
class OrderBlock:
    """An Order Block zone."""
    direction: Direction          # BULLISH (demand) or BEARISH (supply)
    bottom: float                 # lower bound
    top: float                    # upper bound
    origin_bar: int               # bar index of the OB candle
    status: str = "ACTIVE"        # ACTIVE or BROKEN
    tests: int = 0                # number of times price has returned to the zone
    impulse_strength: float = 0.0 # size of the impulse leg relative to ATR

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


# ── Detection ───────────────────────────────────────────────────────────────


def _avg_body(candles: list[Candle]) -> float:
    """Average candle body size — used as the impulse threshold baseline."""
    if not candles:
        return 0.0
    bodies = [c.body_size for c in candles if c.body_size > 0]
    return sum(bodies) / len(bodies) if bodies else 0.0


def detect_order_blocks(
    candles: list[Candle],
    impulse_multiplier: float = 1.5,
    lookback: int = 20,
    use_body_only: bool = False,
) -> list[OrderBlock]:
    """Detect bullish and bearish Order Blocks.

    Algorithm:
      1. Compute average body size over the last `lookback` candles.
      2. Walk forward. For each candle i, if its body > avg * multiplier and
         it is bullish, search backwards for the most recent bearish candle
         (within a short window) — that's the bullish OB.
      3. Symmetric for bearish impulses.
      4. After detection, mark broken status and count tests.

    Args:
        candles: OHLCV buffer.
        impulse_multiplier: how many x average body size qualifies as impulse.
        lookback: window size for average body baseline.
        use_body_only: if True, OB zone is [body_bottom, body_top]
            instead of full candle [low, high].
    """
    obs: list[OrderBlock] = []
    n = len(candles)
    if n < 5:
        return obs

    # Build a rolling average body (keep it simple; recompute per scan window)
    avg_body = _avg_body(candles[-min(lookback, n):])
    if avg_body == 0:
        return obs
    impulse_threshold = avg_body * impulse_multiplier

    # Find impulse candles and map back to OB
    MAX_SEARCH = 5  # search up to 5 bars back for the opposing candle
    for i in range(1, n):
        cand = candles[i]
        if cand.body_size < impulse_threshold:
            continue

        if cand.is_bullish:
            # Look back for most recent bearish candle
            for j in range(i - 1, max(i - MAX_SEARCH - 1, -1), -1):
                ob_cand = candles[j]
                if ob_cand.is_bearish:
                    if use_body_only:
                        bottom = min(ob_cand.open, ob_cand.close)
                        top = max(ob_cand.open, ob_cand.close)
                    else:
                        bottom = ob_cand.low
                        top = ob_cand.high
                    obs.append(OrderBlock(
                        direction=Direction.BULLISH,
                        bottom=bottom,
                        top=top,
                        origin_bar=j,
                        impulse_strength=cand.body_size / avg_body,
                    ))
                    break
        elif cand.is_bearish:
            for j in range(i - 1, max(i - MAX_SEARCH - 1, -1), -1):
                ob_cand = candles[j]
                if ob_cand.is_bullish:
                    if use_body_only:
                        bottom = min(ob_cand.open, ob_cand.close)
                        top = max(ob_cand.open, ob_cand.close)
                    else:
                        bottom = ob_cand.low
                        top = ob_cand.high
                    obs.append(OrderBlock(
                        direction=Direction.BEARISH,
                        bottom=bottom,
                        top=top,
                        origin_bar=j,
                        impulse_strength=cand.body_size / avg_body,
                    ))
                    break

    # Deduplicate by origin_bar (keep strongest impulse)
    by_bar: dict[tuple[int, str], OrderBlock] = {}
    for ob in obs:
        key = (ob.origin_bar, ob.direction.value)
        existing = by_bar.get(key)
        if existing is None or ob.impulse_strength > existing.impulse_strength:
            by_bar[key] = ob
    obs = sorted(by_bar.values(), key=lambda o: o.origin_bar)

    # Mark broken & count tests
    for ob in obs:
        for k in range(ob.origin_bar + 1, n):
            later = candles[k]
            if ob.direction == Direction.BULLISH:
                if later.close < ob.bottom:
                    ob.status = "BROKEN"
                    break
                if later.low <= ob.top and later.high >= ob.bottom:
                    ob.tests += 1
            else:
                if later.close > ob.top:
                    ob.status = "BROKEN"
                    break
                if later.high >= ob.bottom and later.low <= ob.top:
                    ob.tests += 1

    return obs


def active_order_blocks(obs: list[OrderBlock]) -> list[OrderBlock]:
    return [o for o in obs if o.status == "ACTIVE"]


def nearest_order_block(
    obs: list[OrderBlock],
    price: float,
    direction: Optional[Direction] = None,
) -> Optional[OrderBlock]:
    """Nearest ACTIVE order block to `price`, optionally filtered by direction."""
    candidates = [o for o in obs if o.status == "ACTIVE"]
    if direction is not None:
        candidates = [o for o in candidates if o.direction == direction]
    if not candidates:
        return None
    return min(candidates, key=lambda o: abs(o.midpoint - price))


def price_in_order_block(
    obs: list[OrderBlock],
    price: float,
    direction: Optional[Direction] = None,
) -> Optional[OrderBlock]:
    """Return the OB whose zone contains `price`, if any."""
    for ob in obs:
        if ob.status != "ACTIVE":
            continue
        if direction is not None and ob.direction != direction:
            continue
        if ob.contains(price):
            return ob
    return None
