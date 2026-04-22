"""Tests for the Arkham flow-alignment soft directional signal (2026-04-22).

Covers the two pure helpers in `src.strategy.entry_signals`:

  * `_flow_alignment_score` — combines stablecoin pulse + BTC + ETH 24h
    netflow into a single [-1, +1] directional score.
  * `_flow_alignment_penalty` — additive confluence-threshold bump when
    the score opposes the proposed direction.

Plus one integration test that exercises the penalty via
`generate_entry_intent` — a setup that would normally clear the confluence
threshold should reject once the bump pushes the effective threshold above
the raw score.
"""

from __future__ import annotations

import pytest

from src.data.models import (
    Direction,
    FVGZone,
    MarketState,
    OrderBlock,
    OscillatorTableData,
    SignalTableData,
)
from src.strategy.entry_signals import (
    _flow_alignment_penalty,
    _flow_alignment_score,
    generate_entry_intent,
)


# ── _flow_alignment_score pure function ────────────────────────────────────


def test_score_all_zero_on_none_inputs():
    """All three inputs None → 0.0 (fail-open when Arkham data missing)."""
    assert _flow_alignment_score(None, None, None) == 0.0


def test_score_all_below_noise_floor():
    """Signed values below `noise_floor_usd` contribute 0 regardless of sign."""
    # All below default 1M floor — signed but noise.
    assert _flow_alignment_score(500_000.0, 500_000.0, 500_000.0) == 0.0
    assert _flow_alignment_score(-500_000.0, -500_000.0, -500_000.0) == 0.0
    assert _flow_alignment_score(500_000.0, -500_000.0, 500_000.0) == 0.0


def test_score_strong_bullish():
    """Stables in (+), BTC out (-), ETH out (-) → full +1.0.

    All three signals contribute to the bullish side: sum of weights
    is 0.4 + 0.4 + 0.2 = 1.0 exactly.
    """
    score = _flow_alignment_score(
        stablecoin_pulse_1h_usd=+50_000_000.0,
        btc_netflow_24h_usd=-100_000_000.0,
        eth_netflow_24h_usd=-30_000_000.0,
    )
    assert score == pytest.approx(1.0)


def test_score_strong_bearish():
    """Stables out (-), BTC in (+), ETH in (+) → full -1.0."""
    score = _flow_alignment_score(
        stablecoin_pulse_1h_usd=-50_000_000.0,
        btc_netflow_24h_usd=+100_000_000.0,
        eth_netflow_24h_usd=+30_000_000.0,
    )
    assert score == pytest.approx(-1.0)


def test_score_mixed_neutral():
    """Stables bullish (+0.4), BTC inflow bearish (-0.4), ETH inflow
    bearish (-0.2). Net = -0.2.

    Reminder: BTC/ETH signs are INVERTED (inflow = bearish, outflow = bullish).
    """
    score = _flow_alignment_score(
        stablecoin_pulse_1h_usd=+10_000_000.0,  # bullish (+0.4)
        btc_netflow_24h_usd=+10_000_000.0,      # inverted → bearish (-0.4)
        eth_netflow_24h_usd=+10_000_000.0,      # inverted → bearish (-0.2)
    )
    assert score == pytest.approx(-0.2)


def test_score_clamps_to_unit_range():
    """Score always in [-1, +1].

    With each `_sign()` contributing -1, 0, or +1 and weights summing to
    exactly 1.0, the raw arithmetic already lives in [-1, +1], but the
    helper wraps `max(-1.0, min(1.0, score))` as a safety belt. Exercise
    every sign combination and assert it stays in-range.
    """
    signed_magnitudes = (+10_000_000.0, 0.0, -10_000_000.0)
    # None is treated as 0 too — covered by test_score_all_zero_on_none_inputs.
    for stables in signed_magnitudes:
        for btc in signed_magnitudes:
            for eth in signed_magnitudes:
                score = _flow_alignment_score(stables, btc, eth)
                assert -1.0 <= score <= 1.0, (stables, btc, eth, score)
    # Extremes still clamp.
    assert _flow_alignment_score(
        +50_000_000.0, -100_000_000.0, -30_000_000.0) == pytest.approx(1.0)
    assert _flow_alignment_score(
        -50_000_000.0, +100_000_000.0, +30_000_000.0) == pytest.approx(-1.0)


def test_score_custom_noise_floor():
    """A larger `noise_floor_usd` swallows otherwise-signed inputs."""
    # Default floor 1M → $5M would ordinarily be a bullish signal.
    baseline = _flow_alignment_score(+5_000_000.0, None, None)
    assert baseline == pytest.approx(0.4)  # stables bullish (+0.4 weight)
    # Raise the floor above $5M → input drops out → score 0.
    muted = _flow_alignment_score(
        +5_000_000.0, None, None,
        noise_floor_usd=10_000_000.0,
    )
    assert muted == 0.0


# ── _flow_alignment_penalty pure function ──────────────────────────────────


def test_penalty_aligned_long_returns_zero():
    """Long + bullish score → aligned, no penalty."""
    assert _flow_alignment_penalty(
        direction=Direction.BULLISH, score=+0.8, penalty=0.5,
    ) == 0.0


def test_penalty_aligned_short_returns_zero():
    """Short + bearish score → aligned, no penalty."""
    assert _flow_alignment_penalty(
        direction=Direction.BEARISH, score=-0.8, penalty=0.5,
    ) == 0.0


def test_penalty_misaligned_long_scales_with_score():
    """Long + bearish score → misaligned; penalty scales linearly with |score|."""
    # score=-0.5, penalty=0.5 → 0.5 * 0.5 = 0.25.
    assert _flow_alignment_penalty(
        direction=Direction.BULLISH, score=-0.5, penalty=0.5,
    ) == pytest.approx(0.25)
    # score=-1.0, same penalty → full magnitude.
    assert _flow_alignment_penalty(
        direction=Direction.BULLISH, score=-1.0, penalty=0.5,
    ) == pytest.approx(0.5)


def test_penalty_misaligned_short_scales_with_score():
    """Short + bullish score → misaligned; penalty scales with |score|."""
    # score=+1.0, penalty=0.75 → full 0.75.
    assert _flow_alignment_penalty(
        direction=Direction.BEARISH, score=+1.0, penalty=0.75,
    ) == pytest.approx(0.75)
    # Half strength.
    assert _flow_alignment_penalty(
        direction=Direction.BEARISH, score=+0.5, penalty=0.75,
    ) == pytest.approx(0.375)


def test_penalty_zero_penalty_returns_zero():
    """`penalty=0` short-circuits before any alignment check."""
    for direction in (Direction.BULLISH, Direction.BEARISH):
        for score in (-1.0, -0.5, 0.0, 0.5, 1.0):
            assert _flow_alignment_penalty(
                direction=direction, score=score, penalty=0.0,
            ) == 0.0


def test_penalty_zero_score_returns_zero():
    """Neutral score → nothing to be aligned for / against, no penalty."""
    for direction in (Direction.BULLISH, Direction.BEARISH):
        for penalty in (0.25, 0.5, 1.0):
            assert _flow_alignment_penalty(
                direction=direction, score=0.0, penalty=penalty,
            ) == 0.0


# ── Integration: generate_entry_intent bumps effective threshold ───────────


def _state_with_bullish_ob() -> MarketState:
    """Build a MarketState that clears a standard confluence threshold.

    Mirrors `tests/test_entry_signals.py::_state` for a bullish OB setup —
    `generate_entry_intent` should normally return a tradable intent.
    """
    sig = SignalTableData(
        trend_htf=Direction.BULLISH,
        last_mss="BULLISH@99",
        active_ob="BULL@95-97",
        vmc_ribbon="BULLISH",
        price=100.0,
        atr_14=1.0,
    )
    return MarketState(
        signal_table=sig,
        oscillator=OscillatorTableData(),
        order_blocks=[
            OrderBlock(direction=Direction.BULLISH, bottom=95.0, top=97.0),
        ],
        fvg_zones=[],
    )


def test_generate_entry_intent_applies_flow_alignment_penalty():
    """With flow_alignment off: a baseline-clearing bullish setup produces
    an intent. Turn flow_alignment on with a strongly bearish flow state
    (stables leaving, BTC arriving, ETH arriving) + a long direction; the
    bumped effective threshold should now reject the same setup."""
    state = _state_with_bullish_ob()

    # Sanity: baseline produces a tradable intent.
    baseline = generate_entry_intent(state, min_confluence_score=2.0)
    assert baseline is not None
    assert baseline.direction == Direction.BULLISH
    baseline_score = baseline.confluence.score
    assert baseline_score >= 2.0

    # Pick a min_confluence_score just below the raw score, so without any
    # penalty the intent still clears. Pick a penalty big enough that the
    # bumped threshold (raw + penalty * |score|) rises above the raw score.
    # With score=-1.0 (strong bearish flow) and penalty=1.0, the bump is a
    # full +1.0 — comfortably past any realistic confluence margin.
    min_conf = max(0.1, baseline_score - 0.1)
    bumped = generate_entry_intent(
        state,
        min_confluence_score=min_conf,
        flow_alignment_enabled=True,
        flow_alignment_penalty=1.0,
        flow_alignment_noise_floor_usd=1_000_000.0,
        # Strong bearish alignment: stables out, BTC in, ETH in → score -1.0.
        stablecoin_pulse_enabled=False,  # keep the OTHER gate silent
        stablecoin_pulse_usd=-50_000_000.0,
        flow_alignment_btc_netflow_24h_usd=+100_000_000.0,
        flow_alignment_eth_netflow_24h_usd=+30_000_000.0,
    )
    assert bumped is None, (
        f"expected flow_alignment penalty to push effective threshold above "
        f"raw score {baseline_score} but intent survived"
    )

    # Symmetric sanity check: same setup, penalty disabled → intent returns.
    disabled = generate_entry_intent(
        state,
        min_confluence_score=min_conf,
        flow_alignment_enabled=False,  # gate off
        flow_alignment_penalty=1.0,
        stablecoin_pulse_usd=-50_000_000.0,
        flow_alignment_btc_netflow_24h_usd=+100_000_000.0,
        flow_alignment_eth_netflow_24h_usd=+30_000_000.0,
    )
    assert disabled is not None
    assert disabled.direction == Direction.BULLISH
