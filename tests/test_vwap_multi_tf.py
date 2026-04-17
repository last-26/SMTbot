"""Multi-TF VWAP parsing + per-TF confluence factors.

Locks in:
  * `parse_signal_table` extracts vwap_1m / vwap_3m / vwap_15m from cells
    that carry the "<value> (above|below)" suffix.
  * Missing rows → 0.0.
  * `score_direction` fires three independent factors — `vwap_1m_alignment`,
    `vwap_3m_alignment`, `vwap_15m_alignment` — one per TF where the price
    sits on the favorable side. Missing (0.0) VWAPs are skipped silently.
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


def _vwap_factors(factors) -> dict[str, float]:
    return {
        f.name: f.weight for f in factors
        if f.name in ("vwap_1m_alignment", "vwap_3m_alignment", "vwap_15m_alignment")
    }


def test_all_three_vwap_factors_fire_when_above_all_bullish():
    # Price above all three VWAPs → three independent factors at their weights.
    state = _state(price=70_000, v1=69_900, v3=69_800, v15=69_500)
    score = score_direction(state, Direction.BULLISH)
    got = _vwap_factors(score.factors)
    assert got == {
        "vwap_1m_alignment":  DEFAULT_WEIGHTS["vwap_1m_alignment"],
        "vwap_3m_alignment":  DEFAULT_WEIGHTS["vwap_3m_alignment"],
        "vwap_15m_alignment": DEFAULT_WEIGHTS["vwap_15m_alignment"],
    }


def test_only_aligned_vwap_factors_fire():
    # Price above 1m + 15m but below 3m → only 1m and 15m factors fire.
    state = _state(price=70_000, v1=69_900, v3=70_500, v15=69_500)
    score = score_direction(state, Direction.BULLISH)
    got = _vwap_factors(score.factors)
    assert got == {
        "vwap_1m_alignment":  DEFAULT_WEIGHTS["vwap_1m_alignment"],
        "vwap_15m_alignment": DEFAULT_WEIGHTS["vwap_15m_alignment"],
    }


def test_vwap_per_tf_split_independent():
    # The core gradient-flip test: 1m flipped bearish while 15m still above.
    # For a BULLISH candidate, only 15m should fire (1m + 3m below price means
    # price > those, but if we flip to "price below 1m" we drop vwap_1m).
    # Here: price BELOW 1m, ABOVE 3m, ABOVE 15m for BULLISH direction.
    state = _state(price=70_000, v1=70_100, v3=69_800, v15=69_500)
    score = score_direction(state, Direction.BULLISH)
    got = _vwap_factors(score.factors)
    # vwap_1m_alignment should NOT fire (price < 1m VWAP = bearish-aligned).
    # vwap_3m_alignment and vwap_15m_alignment SHOULD fire.
    assert "vwap_1m_alignment" not in got
    assert got["vwap_3m_alignment"] == DEFAULT_WEIGHTS["vwap_3m_alignment"]
    assert got["vwap_15m_alignment"] == DEFAULT_WEIGHTS["vwap_15m_alignment"]


def test_bearish_three_factors_fire_below_all():
    # Bearish symmetric: price below all three → three factors fire.
    state = _state(price=70_000, v1=70_100, v3=70_200, v15=70_500)
    score = score_direction(state, Direction.BEARISH)
    got = _vwap_factors(score.factors)
    assert got == {
        "vwap_1m_alignment":  DEFAULT_WEIGHTS["vwap_1m_alignment"],
        "vwap_3m_alignment":  DEFAULT_WEIGHTS["vwap_3m_alignment"],
        "vwap_15m_alignment": DEFAULT_WEIGHTS["vwap_15m_alignment"],
    }


def test_no_factors_when_all_vwaps_missing():
    state = _state(price=70_000, v1=0.0, v3=0.0, v15=0.0)
    score = score_direction(state, Direction.BULLISH)
    assert _vwap_factors(score.factors) == {}


def test_partial_availability_fires_for_present_and_aligned_only():
    # 1m + 3m present and aligned, 15m missing → two factors fire.
    state = _state(price=70_000, v1=69_900, v3=69_800, v15=0.0)
    score = score_direction(state, Direction.BULLISH)
    got = _vwap_factors(score.factors)
    assert got == {
        "vwap_1m_alignment": DEFAULT_WEIGHTS["vwap_1m_alignment"],
        "vwap_3m_alignment": DEFAULT_WEIGHTS["vwap_3m_alignment"],
    }


def test_single_aligned_tf_fires_alone():
    # Previously skipped (1/3 alignment) — now fires as a single factor.
    # Price above 1m only; below 3m + 15m.
    state = _state(price=70_000, v1=69_900, v3=70_500, v15=70_300)
    score = score_direction(state, Direction.BULLISH)
    got = _vwap_factors(score.factors)
    assert got == {"vwap_1m_alignment": DEFAULT_WEIGHTS["vwap_1m_alignment"]}


# ── Weights override + validation ─────────────────────────────────────────


def test_confluence_weights_override_from_kwarg():
    """Explicit weights kwarg overrides DEFAULT_WEIGHTS per-key."""
    state = _state(price=70_000, v1=69_900, v3=69_800, v15=69_500)
    # Halve the 1m weight, leave others at default.
    overrides = {"vwap_1m_alignment": 0.05}
    score = score_direction(state, Direction.BULLISH, weights=overrides)
    got = _vwap_factors(score.factors)
    assert got["vwap_1m_alignment"] == 0.05
    # Others come from DEFAULT_WEIGHTS (shallow merge).
    assert got["vwap_3m_alignment"] == DEFAULT_WEIGHTS["vwap_3m_alignment"]
    assert got["vwap_15m_alignment"] == DEFAULT_WEIGHTS["vwap_15m_alignment"]


def test_unknown_confluence_weight_key_warns_at_config_load():
    """AnalysisConfig emits UserWarning for keys that don't exist in
    DEFAULT_WEIGHTS — typo guard, not a hard fail."""
    import warnings
    from src.bot.config import AnalysisConfig
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        AnalysisConfig(confluence_weights={"totally_made_up_key": 0.5})
    msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("totally_made_up_key" in m for m in msgs), msgs
