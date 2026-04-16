"""Tests for src.execution.position_monitor — poll → CloseFill events."""

from __future__ import annotations

from src.execution.models import PositionSnapshot, PositionState
from src.execution.position_monitor import PositionMonitor


class FakeClient:
    def __init__(self):
        self.snapshots: list[PositionSnapshot] = []

    def get_positions(self, inst_id=None):
        if inst_id is None:
            return list(self.snapshots)
        return [s for s in self.snapshots if s.inst_id == inst_id]


def _snap(inst="BTC-USDT-SWAP", side="long", size=3.0, entry=67000.0) -> PositionSnapshot:
    return PositionSnapshot(
        inst_id=inst, pos_side=side, size=size,
        entry_price=entry, mark_price=entry + 100,
        unrealized_pnl=3.0, leverage=10,
    )


def test_fresh_monitor_has_no_tracked_positions():
    mon = PositionMonitor(FakeClient())
    assert mon.tracked_count == 0
    assert mon.poll() == []


def test_register_open_tracks_position():
    mon = PositionMonitor(FakeClient())
    mon.register_open("BTC-USDT-SWAP", "long", size=3.0, entry_price=67000.0)
    assert mon.tracked_count == 1
    assert mon.state("BTC-USDT-SWAP", "long") == PositionState.OPEN


def test_poll_emits_no_fill_while_position_still_open():
    client = FakeClient()
    client.snapshots = [_snap()]
    mon = PositionMonitor(client)
    mon.register_open("BTC-USDT-SWAP", "long", 3.0, 67000.0)
    assert mon.poll() == []
    assert mon.tracked_count == 1


def test_poll_emits_fill_when_position_disappears():
    client = FakeClient()
    client.snapshots = [_snap()]
    mon = PositionMonitor(client)
    mon.register_open("BTC-USDT-SWAP", "long", 3.0, 67000.0)
    mon.poll()  # still open
    client.snapshots = []  # SL/TP closed the position
    fills = mon.poll()
    assert len(fills) == 1
    assert fills[0].inst_id == "BTC-USDT-SWAP"
    assert fills[0].pos_side == "long"
    assert fills[0].entry_price == 67000.0
    assert mon.tracked_count == 0
    assert mon.state("BTC-USDT-SWAP", "long") == PositionState.CLOSED


def test_poll_updates_cached_entry_price_on_partial_fill():
    """If OKX reports a better avg entry (e.g. after a partial fill that
    completes), the monitor should track the live value."""
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open("BTC-USDT-SWAP", "long", 3.0, 0.0)  # entry unknown at registration
    client.snapshots = [_snap(entry=67250.0)]
    mon.poll()
    client.snapshots = []
    fill = mon.poll()[0]
    assert fill.entry_price == 67250.0


def test_poll_only_closes_tracked_on_matching_inst_id():
    client = FakeClient()
    mon = PositionMonitor(client)
    mon.register_open("BTC-USDT-SWAP", "long", 3.0, 67000.0)
    mon.register_open("ETH-USDT-SWAP", "long", 1.0, 3500.0)
    # BTC poll only — ETH row is absent but should NOT be closed by this poll.
    client.snapshots = [_snap()]
    fills = mon.poll(inst_id="BTC-USDT-SWAP")
    assert fills == []
    assert mon.tracked_count == 2
