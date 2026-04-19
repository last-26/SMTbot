"""ADX-based trend-regime classifier (Phase 7.D3).

Classifies the entry-TF market into one of three labels:

* ``RANGING`` — ADX below the ranging threshold; directional signals have
  poor edge; mean-reversion / sweep-reversal setups work best.
* ``WEAK_TREND`` — ADX between ranging and strong thresholds; trend-
  continuation factors gain edge but runners should be quicker to exit.
* ``STRONG_TREND`` — ADX above the strong threshold; trend-continuation
  factors (HTF alignment, VWAP in trend direction) should be rewarded;
  counter-trend reversal setups should be discounted.
* ``UNKNOWN`` — not enough bars or degenerate input; callers treat this
  the same as "no gate" (factor weights unchanged).

This is a pragmatic Wilder-smoothed ADX implementation in pure Python so
we stay pandas-free. `classify_trend_regime` is the one public entry;
unit tests cover the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.data.candle_buffer import Candle


class TrendRegime(str, Enum):
    """Trend-strength classification used by the scoring engine."""
    RANGING = "RANGING"
    WEAK_TREND = "WEAK_TREND"
    STRONG_TREND = "STRONG_TREND"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TrendRegimeResult:
    regime: TrendRegime
    adx: float
    plus_di: float
    minus_di: float
    bars_used: int


# Wilder defaults — the same 14-period ADX used on most charting platforms.
DEFAULT_ADX_PERIOD: int = 14
DEFAULT_RANGING_THRESHOLD: float = 20.0
DEFAULT_STRONG_THRESHOLD: float = 30.0


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder's RMA: first value = simple sum of `period`, then running sum."""
    if period <= 0 or len(values) < period:
        return []
    out: list[float] = []
    seed = sum(values[:period])
    out.append(seed)
    for v in values[period:]:
        prev = out[-1]
        out.append(prev - (prev / period) + v)
    return out


def compute_adx(
    candles: list[Candle],
    period: int = DEFAULT_ADX_PERIOD,
) -> Optional[tuple[float, float, float]]:
    """Return (adx, +di, -di) using Wilder's smoothing.

    Needs at least 2*period closed bars; fewer → None. Degenerate input
    (flat prices for the window) returns None as well — a zero ADX would
    be misleading.
    """
    if period <= 0 or len(candles) < 2 * period + 1:
        return None

    trs: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []

    for i in range(1, len(candles)):
        curr = candles[i]
        prev = candles[i - 1]
        high = curr.high
        low = curr.low
        prev_close = prev.close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up_move = high - prev.high
        down_move = prev.low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    tr_smooth = _wilder_smooth(trs, period)
    plus_dm_smooth = _wilder_smooth(plus_dms, period)
    minus_dm_smooth = _wilder_smooth(minus_dms, period)
    if not tr_smooth or not plus_dm_smooth or not minus_dm_smooth:
        return None

    dx_series: list[float] = []
    for tr, pdm, mdm in zip(tr_smooth, plus_dm_smooth, minus_dm_smooth):
        if tr <= 0:
            continue
        plus_di = 100.0 * pdm / tr
        minus_di = 100.0 * mdm / tr
        denom = plus_di + minus_di
        if denom <= 0:
            continue
        dx = 100.0 * abs(plus_di - minus_di) / denom
        dx_series.append(dx)

    if len(dx_series) < period:
        return None

    adx_smooth = _wilder_smooth(dx_series, period)
    if not adx_smooth:
        return None
    adx = adx_smooth[-1] / period
    # Scale back: _wilder_smooth returns a running-sum-style accumulator. For
    # the public (+DI / -DI / ADX) we want the average, so divide by period.
    tr_last = tr_smooth[-1]
    plus_di_last = 100.0 * plus_dm_smooth[-1] / tr_last if tr_last > 0 else 0.0
    minus_di_last = 100.0 * minus_dm_smooth[-1] / tr_last if tr_last > 0 else 0.0
    return adx, plus_di_last, minus_di_last


def classify_trend_regime(
    candles: Optional[list[Candle]],
    period: int = DEFAULT_ADX_PERIOD,
    ranging_threshold: float = DEFAULT_RANGING_THRESHOLD,
    strong_threshold: float = DEFAULT_STRONG_THRESHOLD,
) -> TrendRegimeResult:
    """Classify the entry-TF market into RANGING / WEAK_TREND / STRONG_TREND.

    ``UNKNOWN`` is returned when the buffer is too short or the ADX math
    degenerates (flat prices). Callers gate conditional scoring on
    `regime != UNKNOWN` — a missing classification is fail-open.
    """
    if not candles:
        return TrendRegimeResult(TrendRegime.UNKNOWN, 0.0, 0.0, 0.0, 0)
    if ranging_threshold >= strong_threshold:
        raise ValueError(
            f"ranging_threshold={ranging_threshold} must be < "
            f"strong_threshold={strong_threshold}"
        )
    result = compute_adx(candles, period=period)
    if result is None:
        return TrendRegimeResult(TrendRegime.UNKNOWN, 0.0, 0.0, 0.0, len(candles))
    adx, plus_di, minus_di = result
    if adx < ranging_threshold:
        regime = TrendRegime.RANGING
    elif adx < strong_threshold:
        regime = TrendRegime.WEAK_TREND
    else:
        regime = TrendRegime.STRONG_TREND
    return TrendRegimeResult(regime, adx, plus_di, minus_di, len(candles))
