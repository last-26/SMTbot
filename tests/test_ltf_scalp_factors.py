"""Tests for the 2026-04-28 scalp-confirmation soft factors:
`ltf_ribbon_alignment` (1m EMA21-55 ribbon bias),
`ltf_mss_alignment` (1m last MSS direction prefix), and
`htf_mss_alignment` (15m last MSS, journal-only weight=0 by default).

The 1m factors fire on an opt-in basis when the 1m signal aligns with
the proposed trade direction. They draw from `LTFState` populated by
`LTFReader.read()` — the same MarketState the bot already fetches for
the defensive-close gate, so no extra TV round-trip.

The 15m factor reads from a stripped-down HTF MarketState passed via
the `htf_state` keyword on `score_direction` / `calculate_confluence`.
The runner threads it from `ctx.htf_state_cache[symbol]` populated on
the HTF settle pass. Default weight 0.0 — the factor name lands in the
`confluence_factors` JSON column for Pass 3 GBT to train on without
tilting the live confluence score.
"""

from __future__ import annotations

from src.analysis.multi_timeframe import DEFAULT_WEIGHTS, score_direction
from src.data.ltf_reader import LTFState
from src.data.models import (
    Direction,
    MarketState,
    OscillatorTableData,
    SignalTableData,
)


def _ltf(*, vmc_ribbon: str = "", last_mss: str | None = None) -> LTFState:
    """Build an LTFState with only the fields these factors read."""
    return LTFState(
        symbol="BTC-USDT-SWAP",
        timeframe="1m",
        price=70_000.0,
        rsi=55.0,
        wt_state="NEUTRAL",
        wt_cross="—",
        last_signal="",
        last_signal_bars_ago=99,
        trend=Direction.RANGING,  # silent on ltf_momentum_alignment
        vmc_ribbon=vmc_ribbon,
        last_mss=last_mss,
    )


def _state() -> MarketState:
    """MarketState with no factors firing — only the LTF inputs vary."""
    sig = SignalTableData(price=70_000.0, atr_14=1.0, vmc_ribbon="")
    return MarketState(signal_table=sig, oscillator=OscillatorTableData())


# ── ltf_ribbon_alignment ────────────────────────────────────────────────────


def test_ribbon_aligned_long_fires():
    result = score_direction(
        _state(), Direction.BULLISH, ltf_state=_ltf(vmc_ribbon="BULLISH"),
    )
    assert "ltf_ribbon_alignment" in result.factor_names
    assert result.score >= DEFAULT_WEIGHTS["ltf_ribbon_alignment"]


def test_ribbon_aligned_short_fires():
    result = score_direction(
        _state(), Direction.BEARISH, ltf_state=_ltf(vmc_ribbon="BEARISH"),
    )
    assert "ltf_ribbon_alignment" in result.factor_names


def test_ribbon_misaligned_does_not_fire():
    # 1m ribbon BULLISH, proposed direction BEARISH → factor stays silent
    result = score_direction(
        _state(), Direction.BEARISH, ltf_state=_ltf(vmc_ribbon="BULLISH"),
    )
    assert "ltf_ribbon_alignment" not in result.factor_names


def test_ribbon_empty_string_does_not_fire():
    result = score_direction(
        _state(), Direction.BULLISH, ltf_state=_ltf(vmc_ribbon=""),
    )
    assert "ltf_ribbon_alignment" not in result.factor_names


def test_ribbon_silent_when_ltf_state_none():
    result = score_direction(_state(), Direction.BULLISH, ltf_state=None)
    assert "ltf_ribbon_alignment" not in result.factor_names


# ── ltf_mss_alignment ───────────────────────────────────────────────────────


def test_mss_aligned_long_fires_via_prefix():
    # `_parse_direction_prefix` handles "BULL" or "BULLISH" prefix.
    result = score_direction(
        _state(), Direction.BULLISH,
        ltf_state=_ltf(last_mss="BULLISH@69500"),
    )
    assert "ltf_mss_alignment" in result.factor_names


def test_mss_aligned_short_fires():
    result = score_direction(
        _state(), Direction.BEARISH,
        ltf_state=_ltf(last_mss="BEARISH@70500"),
    )
    assert "ltf_mss_alignment" in result.factor_names


def test_mss_short_prefix_also_fires():
    # The parser accepts "BULL@..." / "BEAR@..." short forms too.
    result = score_direction(
        _state(), Direction.BULLISH,
        ltf_state=_ltf(last_mss="BULL@69500"),
    )
    assert "ltf_mss_alignment" in result.factor_names


def test_mss_misaligned_does_not_fire():
    result = score_direction(
        _state(), Direction.BEARISH,
        ltf_state=_ltf(last_mss="BULLISH@69500"),
    )
    assert "ltf_mss_alignment" not in result.factor_names


def test_mss_none_does_not_fire():
    result = score_direction(
        _state(), Direction.BULLISH,
        ltf_state=_ltf(last_mss=None),
    )
    assert "ltf_mss_alignment" not in result.factor_names


# ── Combined: both factors fire together ────────────────────────────────────


def test_both_factors_stack_on_full_alignment():
    """A trade with 1m ribbon + 1m MSS both aligned picks up roughly
    +0.5 (2 × 0.25) on top of the entry-TF score — exactly the 'scalp
    confirmation' bonus the operator asked for in the 2026-04-28 tune.
    """
    result = score_direction(
        _state(),
        Direction.BULLISH,
        ltf_state=_ltf(vmc_ribbon="BULLISH", last_mss="BULLISH@69500"),
    )
    names = result.factor_names
    assert "ltf_ribbon_alignment" in names
    assert "ltf_mss_alignment" in names
    expected = (
        DEFAULT_WEIGHTS["ltf_ribbon_alignment"]
        + DEFAULT_WEIGHTS["ltf_mss_alignment"]
    )
    assert result.score >= expected


def test_factors_only_in_yaml_default_weights():
    """Belt-and-suspenders: both new factor weights live in DEFAULT_WEIGHTS
    so an empty config/default.yaml `confluence_weights` block still gets
    them. Guards against a future YAML edit that drops the entries."""
    assert DEFAULT_WEIGHTS["ltf_ribbon_alignment"] == 0.25
    assert DEFAULT_WEIGHTS["ltf_mss_alignment"] == 0.25
    # 15m MSS is journal-only by default — weight 0 so the factor lands
    # in confluence_factors for Pass 3 GBT without affecting the live
    # min_confluence_score gate.
    assert DEFAULT_WEIGHTS["htf_mss_alignment"] == 0.0


# ── htf_mss_alignment (15m, journal-only by default) ───────────────────────


class _FakeHTFState:
    """Minimal stand-in for the HTF MarketState slice the factor reads."""

    def __init__(self, last_mss: str | None):
        self.signal_table = SignalTableData(
            price=70_000.0, atr_14=1.0, vmc_ribbon="",
            last_mss=last_mss,
        )


def test_htf_mss_aligned_long_appears_in_factors_with_zero_weight():
    """Default weight 0.0 — factor should still appear in factors list
    so confluence_factors JSON captures it for Pass 3 training, but
    score doesn't change."""
    htf = _FakeHTFState("BULLISH@69500")
    result = score_direction(_state(), Direction.BULLISH, htf_state=htf)
    assert "htf_mss_alignment" in result.factor_names
    # Score contribution from this factor is zero by default — total
    # score stays at 0 because no other factors fire in _state().
    assert result.score == 0.0


def test_htf_mss_aligned_short_appears():
    htf = _FakeHTFState("BEARISH@70500")
    result = score_direction(_state(), Direction.BEARISH, htf_state=htf)
    assert "htf_mss_alignment" in result.factor_names


def test_htf_mss_misaligned_does_not_fire():
    htf = _FakeHTFState("BULLISH@69500")
    result = score_direction(_state(), Direction.BEARISH, htf_state=htf)
    assert "htf_mss_alignment" not in result.factor_names


def test_htf_mss_none_does_not_fire():
    htf = _FakeHTFState(None)
    result = score_direction(_state(), Direction.BULLISH, htf_state=htf)
    assert "htf_mss_alignment" not in result.factor_names


def test_htf_state_none_does_not_fire():
    result = score_direction(_state(), Direction.BULLISH, htf_state=None)
    assert "htf_mss_alignment" not in result.factor_names


def test_htf_mss_weight_can_be_overridden_to_active():
    """If the operator flips the YAML weight to 0.25, the factor starts
    contributing — guards the 0.0-default doesn't block future Pass 3
    activation."""
    htf = _FakeHTFState("BULLISH@69500")
    result = score_direction(
        _state(), Direction.BULLISH, htf_state=htf,
        weights={"htf_mss_alignment": 0.25},
    )
    assert "htf_mss_alignment" in result.factor_names
    assert result.score >= 0.25
