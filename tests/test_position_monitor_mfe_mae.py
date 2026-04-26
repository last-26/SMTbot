"""Tests for running MFE/MAE excursion tracking on _Tracked positions.

The position monitor updates `mfe_r_high` (max favorable excursion in R units)
and `mae_r_low` (max adverse excursion, also in R units, expressed as negative)
on every poll. These counters feed the position_snapshots journal table for
post-hoc RL trajectory analysis.
"""

from __future__ import annotations

from src.execution.models import PositionSnapshot
from src.execution.position_monitor import PositionMonitor


class FakeClient:
    def __init__(self):
        self.snapshots: list[PositionSnapshot] = []

    def get_positions(self, inst_id=None):
        if inst_id is None:
            return list(self.snapshots)
        return [s for s in self.snapshots if s.inst_id == inst_id]


def _snap(
    inst="BTC-USDT-SWAP", side="long", size=3.0,
    entry=67000.0, mark=67000.0,
) -> PositionSnapshot:
    return PositionSnapshot(
        inst_id=inst, pos_side=side, size=size,
        entry_price=entry, mark_price=mark,
        unrealized_pnl=0.0, leverage=10,
    )


def test_tracked_defaults_excursion_to_zero():
    mon = PositionMonitor(FakeClient())
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=66800.0,
    )
    t = mon.get_tracked("BTC-USDT-SWAP", "long")
    assert t is not None
    assert t.mfe_r_high == 0.0
    assert t.mae_r_low == 0.0


def test_favorable_long_move_updates_mfe_only():
    """SL distance = 200 → +400 favorable move = +2.0R MFE; MAE untouched."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=66800.0,
    )
    client.snapshots = [_snap(mark=67400.0)]
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "long")
    assert t.mfe_r_high == 2.0
    assert t.mae_r_low == 0.0


def test_adverse_long_move_updates_mae_only():
    """SL distance = 200 → -100 adverse move = -0.5R MAE; MFE untouched."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=66800.0,
    )
    client.snapshots = [_snap(mark=66900.0)]
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "long")
    assert t.mfe_r_high == 0.0
    assert t.mae_r_low == -0.5


def test_running_extremes_persist_across_polls():
    """First poll +1.5R favorable, second poll -1.0R adverse — both peaks held."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=66800.0,  # SL distance 200
    )
    client.snapshots = [_snap(mark=67300.0)]  # +1.5R
    mon.poll()
    client.snapshots = [_snap(mark=66800.0)]  # back to entry-1R adverse
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "long")
    assert t.mfe_r_high == 1.5  # held
    assert t.mae_r_low == -1.0


def test_short_position_excursion_is_sign_inverted():
    """For shorts, mark BELOW entry is favorable. SL distance = 200,
    mark drops 300 → +1.5R MFE; mark rises 100 → -0.5R MAE."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "short", 3.0, 67000.0,
        plan_sl_price=67200.0,  # SL ABOVE entry for short, distance 200
    )
    client.snapshots = [_snap(side="short", mark=66700.0)]  # short favorable
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "short")
    assert t.mfe_r_high == 1.5
    assert t.mae_r_low == 0.0

    client.snapshots = [_snap(side="short", mark=67100.0)]  # short adverse
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "short")
    assert t.mfe_r_high == 1.5  # held
    assert t.mae_r_low == -0.5


def test_excursion_skipped_when_plan_sl_unknown():
    """plan_sl_price=0.0 (rehydrate sentinel) → MFE/MAE stay at 0.0."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=0.0,
    )
    client.snapshots = [_snap(mark=68000.0)]  # would be +5.0R if SL set
    mon.poll()
    t = mon.get_tracked("BTC-USDT-SWAP", "long")
    assert t.mfe_r_high == 0.0
    assert t.mae_r_low == 0.0


def test_poll_returns_tuple_of_fills_and_live_snaps():
    """poll() now returns (fills, live_snaps) — runner needs both without
    issuing a second get_positions() call."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", 3.0, 67000.0,
        plan_sl_price=66800.0,
    )
    snap = _snap(mark=67400.0)
    client.snapshots = [snap]
    fills, live_snaps = mon.poll()
    assert fills == []
    assert len(live_snaps) == 1
    assert live_snaps[0].inst_id == "BTC-USDT-SWAP"
    assert live_snaps[0].mark_price == 67400.0


def test_get_tracked_returns_none_for_unknown_position():
    mon = PositionMonitor(FakeClient())
    assert mon.get_tracked("BTC-USDT-SWAP", "long") is None
