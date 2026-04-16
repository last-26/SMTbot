"""LTFReader projection tests (Madde B)."""

from __future__ import annotations

from src.data.ltf_reader import LTFReader, LTFState
from src.data.models import (
    Direction,
    MarketState,
    OscillatorTableData,
    SignalTableData,
)


class _StaticReader:
    def __init__(self, state: MarketState):
        self.state = state

    async def read_market_state(self) -> MarketState:
        return self.state


def _make_state(*, wt_state="NEUTRAL", rsi=50.0, last_signal="—",
                bars_ago=0, wt_cross="—", price=100.0) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP", timeframe="1",
        signal_table=SignalTableData(price=price),
        oscillator=OscillatorTableData(
            wt_state=wt_state, rsi=rsi,
            last_signal=last_signal, last_signal_bars_ago=bars_ago,
            wt_cross=wt_cross,
        ),
    )


async def test_ltf_reader_reads_oscillator():
    reader = _StaticReader(_make_state(
        wt_state="OVERSOLD", rsi=30.0, last_signal="BUY", bars_ago=2,
        wt_cross="UP", price=67_000.0,
    ))
    ltf = LTFReader(bridge=None, reader=reader)
    result = await ltf.read("BTC-USDT-SWAP", "1m")

    assert isinstance(result, LTFState)
    assert result.symbol == "BTC-USDT-SWAP"
    assert result.timeframe == "1m"
    assert result.price == 67_000.0
    assert result.rsi == 30.0
    assert result.wt_state == "OVERSOLD"
    assert result.wt_cross == "UP"
    assert result.last_signal == "BUY"
    assert result.last_signal_bars_ago == 2


async def test_ltf_trend_bearish_when_oversold_and_rsi_low():
    """WT OVERSOLD + RSI<40 → BEARISH (exhausted downside, reversal warning)."""
    reader = _StaticReader(_make_state(wt_state="OVERSOLD", rsi=35.0))
    result = await LTFReader(None, reader).read("BTC-USDT-SWAP")
    assert result.trend == Direction.BEARISH


async def test_ltf_trend_bullish_when_overbought_and_rsi_high():
    reader = _StaticReader(_make_state(wt_state="OVERBOUGHT", rsi=65.0))
    result = await LTFReader(None, reader).read("BTC-USDT-SWAP")
    assert result.trend == Direction.BULLISH


async def test_ltf_trend_ranging_when_ambiguous():
    """OVERSOLD but RSI above threshold → RANGING (ambiguous)."""
    reader = _StaticReader(_make_state(wt_state="OVERSOLD", rsi=45.0))
    result = await LTFReader(None, reader).read("BTC-USDT-SWAP")
    assert result.trend == Direction.RANGING

    reader = _StaticReader(_make_state(wt_state="NEUTRAL", rsi=50.0))
    result = await LTFReader(None, reader).read("BTC-USDT-SWAP")
    assert result.trend == Direction.RANGING
