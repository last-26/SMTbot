"""LTF (low-timeframe) reversal reader.

Derives a compact `LTFState` from the SMT Oscillator table so the bot can
detect LTF reversals against an open position (Madde F). The SMT Oscillator
Pine already parses WaveTrend / RSI / last-signal, so this module is a thin
projection — no new data fetch, just a shape better suited to the defensive
close gate.

The caller is expected to have already switched the TV chart to the LTF
timeframe before calling `read()`. Pairing with `_wait_for_pine_settle` in
the runner is what guarantees the oscillator we read here reflects the LTF,
not the previous chart state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.data.models import Direction, OscillatorTableData


@dataclass
class LTFState:
    """Compact view of the LTF oscillator for the defensive-close gate.

    Core fields (rsi / wt_state / wt_cross / last_signal / trend) feed the
    defensive-close logic in Madde F. 2026-04-22 (gece, late) added the
    full `oscillator` attachment so the runner can journal per-TF raw
    oscillator numerics (wt1/wt2/rsi_mfi/stoch_k/d/momentum/divergence
    flags) at entry / pending-placement time for Pass 2 GBT features.

    `oscillator` is Optional to preserve backward compatibility with
    existing LTFState constructors in tests — legacy callers see no
    change, new callers attach the full snapshot.
    """
    symbol: str
    timeframe: str
    price: float
    rsi: float
    wt_state: str            # "OVERBOUGHT" / "OVERSOLD" / "NEUTRAL"
    wt_cross: str            # "UP" / "DOWN" / "—"
    last_signal: str         # "BUY" / "SELL" / "GOLD_BUY" / …
    last_signal_bars_ago: int
    trend: Direction         # BULLISH / BEARISH / RANGING — heuristic
    oscillator: Optional[OscillatorTableData] = None


def _trend_from_oscillator(wt_state: str, rsi: float) -> Direction:
    """wt OVERSOLD + rsi<40 → BEARISH; wt OVERBOUGHT + rsi>60 → BULLISH.

    We're after a reversal signal against the currently open side, so the
    heuristic deliberately collapses anything ambiguous into RANGING.
    """
    ws = (wt_state or "").upper()
    if ws == "OVERSOLD" and rsi < 40:
        return Direction.BEARISH
    if ws == "OVERBOUGHT" and rsi > 60:
        return Direction.BULLISH
    return Direction.RANGING


class LTFReader:
    """Read the SMT Oscillator into an LTFState snapshot.

    Reads via the existing structured reader (no direct TV CLI call). The
    caller must ensure the chart is on the LTF timeframe when `read()` is
    invoked — this class does not switch timeframes.
    """

    def __init__(self, bridge: Any, reader: Any):
        self.bridge = bridge          # TVBridge (unused today; reserved for direct CLI)
        self.reader = reader          # StructuredReader — has .read_market_state()

    async def read(self, symbol: str, timeframe: str = "1m") -> LTFState:
        state = await self.reader.read_market_state()
        osc = state.oscillator
        sig = state.signal_table
        return LTFState(
            symbol=symbol,
            timeframe=timeframe,
            price=float(sig.price or 0.0),
            rsi=float(osc.rsi),
            wt_state=osc.wt_state,
            wt_cross=osc.wt_cross,
            last_signal=osc.last_signal,
            last_signal_bars_ago=osc.last_signal_bars_ago,
            trend=_trend_from_oscillator(osc.wt_state, osc.rsi),
            # 2026-04-22 (gece, late) — full oscillator snapshot attached so
            # the runner can journal per-TF raw numerics at entry time.
            # Defensive-close logic only reads the flat fields above; this
            # attachment is additive and never mutates.
            oscillator=osc.model_copy(deep=True),
        )
