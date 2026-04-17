"""Multi-TF VWAP parsing + confluence factor.

Locks in:
  * `parse_signal_table` extracts vwap_1m / vwap_3m / vwap_15m from cells
    that carry the "<value> (above|below)" suffix.
  * Missing rows → 0.0.
  * `score_direction` fires `vwap_alignment` at full weight when price is on
    the favorable side of all three VWAPs, half weight when 2/3 align,
    and not at all when only 1/3 or 0/3 align.
"""

from __future__ import annotations

from src.analysis.multi_timeframe import DEFAULT_WEIGHTS, score_direction
from src.data.models import Direction, MarketState, SignalTableData
from src.data.structured_reader import parse_signal_table


def _tables(rows: list[str]) -> dict:
    return {
        "success": True,
        "studies": [{
            "name": "SMT Master Overlay",
            "tables": [{"rows": rows}],
        }],
    }


def test_vwap_rows_parsed():
    parsed = parse_signal_table(_tables([
        "=== SMT Signals === | BTCUSDT.P",
        "price    | 70000.0",
        "vwap_1m  | 69850.21 (above)",
        "vwap_3m  | 69800.55 (above)",
        "vwap_15m | 69700.10 (above)",
        "last_bar | 12345",
    ]))
    assert parsed is not None
    assert parsed.vwap_1m == 69850.21
    assert parsed.vwap_3m == 69800.55
    assert parsed.vwap_15m == 69700.10


def test_vwap_missing_rows_default_to_zero():
    parsed = parse_signal_table(_tables([
        "=== SMT Signals === | BTCUSDT.P",
        "price    | 70000.0",
        "vwap_1m  | —",
        "last_bar | 12345",
    ]))
    assert parsed is not None
    assert parsed.vwap_1m == 0.0
    assert parsed.vwap_3m == 0.0
    assert parsed.vwap_15m == 0.0


def _state(price: float, v1: float, v3: float, v15: float) -> MarketState:
    return MarketState(
        symbol="BTC-USDT-SWAP",
        signal_table=SignalTableData(
            price=price, vwap_1m=v1, vwap_3m=v3, vwap_15m=v15,
        ),
    )


def test_vwap_alignment_full_weight_bullish():
    # Price above all three VWAPs → full weight.
    state = _state(price=70_000, v1=69_900, v3=69_800, v15=69_500)
    score = score_direction(state, Direction.BULLISH)
    factor = next((f for f in score.factors if f.name == "vwap_alignment"), None)
    assert factor is not None
    assert factor.weight == DEFAULT_WEIGHTS["vwap_alignment"]


def test_vwap_alignment_half_weight_two_of_three():
    # Two above, one below → half weight.
    state = _state(price=70_000, v1=69_900, v3=70_500, v15=69_500)
    score = score_direction(state, Direction.BULLISH)
    factor = next((f for f in score.factors if f.name == "vwap_alignment"), None)
    assert factor is not None
    assert factor.weight == DEFAULT_WEIGHTS["vwap_alignment"] * 0.5


def test_vwap_alignment_skipped_when_only_one_aligns():
    # Only one above → no factor.
    state = _state(price=70_000, v1=69_900, v3=70_500, v15=70_300)
    score = score_direction(state, Direction.BULLISH)
    assert all(f.name != "vwap_alignment" for f in score.factors)


def test_vwap_alignment_bearish_below_all():
    # Bearish symmetric: price below all three → full weight.
    state = _state(price=70_000, v1=70_100, v3=70_200, v15=70_500)
    score = score_direction(state, Direction.BEARISH)
    factor = next((f for f in score.factors if f.name == "vwap_alignment"), None)
    assert factor is not None
    assert factor.weight == DEFAULT_WEIGHTS["vwap_alignment"]


def test_vwap_alignment_skipped_when_all_missing():
    state = _state(price=70_000, v1=0.0, v3=0.0, v15=0.0)
    score = score_direction(state, Direction.BULLISH)
    assert all(f.name != "vwap_alignment" for f in score.factors)


def test_vwap_alignment_with_partial_availability():
    # Only 2 VWAPs available, both aligned → full weight (n_avail==2, both match).
    state = _state(price=70_000, v1=69_900, v3=69_800, v15=0.0)
    score = score_direction(state, Direction.BULLISH)
    factor = next((f for f in score.factors if f.name == "vwap_alignment"), None)
    assert factor is not None
    assert factor.weight == DEFAULT_WEIGHTS["vwap_alignment"]
