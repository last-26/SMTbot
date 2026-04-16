"""HTF S/R ceiling for SL/TP (Madde D).

Tests the two pure helpers `_push_sl_past_htf_zone` and
`_apply_htf_tp_ceiling` that push an SL past any HTF zone between the
entry and SL, and cap the TP below/above the next HTF zone in the
profit direction. End-to-end behavior (R:R recompute + rejection below
`min_rr_ratio`) is tested through `build_trade_plan_from_state`.
"""

from __future__ import annotations

import pytest

from src.analysis.support_resistance import SRZone
from src.data.models import Direction
from src.strategy.entry_signals import (
    _apply_htf_tp_ceiling,
    _push_sl_past_htf_zone,
)


def _zone(bottom: float, top: float, role: str = "MIXED") -> SRZone:
    return SRZone(
        center=(bottom + top) / 2.0, bottom=bottom, top=top,
        touches=3, role=role, score=1.0,
    )


# ── _push_sl_past_htf_zone ──────────────────────────────────────────────────


def test_push_sl_past_resistance_bearish():
    # Short entry=100, initial sl=103. HTF resistance zone at 101-102 is
    # between entry and SL → push SL past top+buffer (102 + 0.2*1 = 102.2).
    zones = [_zone(101.0, 102.0, role="RESISTANCE")]
    sl = _push_sl_past_htf_zone(
        sl=103.0, entry=100.0, direction=Direction.BEARISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert sl == pytest.approx(102.2)


def test_push_sl_past_support_bullish():
    # Long entry=100, initial sl=96. HTF support zone at 97-98 is
    # between entry and SL → push SL past bottom-buffer (97 - 0.2 = 96.8).
    zones = [_zone(97.0, 98.0, role="SUPPORT")]
    sl = _push_sl_past_htf_zone(
        sl=96.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert sl == pytest.approx(96.8)


def test_push_sl_no_zone_in_path_unchanged():
    # Zones that don't sit between entry and SL must not shift the stop.
    zones = [_zone(90.0, 91.0, role="SUPPORT")]
    sl = _push_sl_past_htf_zone(
        sl=96.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert sl == pytest.approx(96.0)


# ── _apply_htf_tp_ceiling ───────────────────────────────────────────────────


def test_tp_ceiling_long_resistance_ahead():
    # Long entry=100, tp=105, HTF resistance at 103-104 → cap tp at 102.8.
    zones = [_zone(103.0, 104.0, role="RESISTANCE")]
    tp = _apply_htf_tp_ceiling(
        tp=105.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert tp == pytest.approx(102.8)


def test_tp_ceiling_short_support_ahead():
    # Short entry=100, tp=95, HTF support at 96-97 → cap tp at 97.2.
    zones = [_zone(96.0, 97.0, role="SUPPORT")]
    tp = _apply_htf_tp_ceiling(
        tp=95.0, entry=100.0, direction=Direction.BEARISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert tp == pytest.approx(97.2)


def test_tp_beyond_all_zones_unchanged():
    # Long entry=100, tp=102. Resistance at 105-106 is farther than tp.
    zones = [_zone(105.0, 106.0, role="RESISTANCE")]
    tp = _apply_htf_tp_ceiling(
        tp=102.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert tp == pytest.approx(102.0)


def test_tp_ceiling_ignores_wrong_role():
    # Long into SUPPORT zone is nonsensical — our rule only caps against
    # RESISTANCE or MIXED zones for longs.
    zones = [_zone(103.0, 104.0, role="SUPPORT")]
    tp = _apply_htf_tp_ceiling(
        tp=105.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert tp == pytest.approx(105.0)


def test_tp_ceiling_mixed_role_applies():
    # MIXED zones also trigger the cap (same logic as dedicated role).
    zones = [_zone(103.0, 104.0, role="MIXED")]
    tp = _apply_htf_tp_ceiling(
        tp=105.0, entry=100.0, direction=Direction.BULLISH,
        htf_zones=zones, buffer_atr=0.2, atr=1.0,
    )
    assert tp == pytest.approx(102.8)
