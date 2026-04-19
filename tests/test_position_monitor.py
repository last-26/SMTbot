"""Tests for src.execution.position_monitor — poll → CloseFill events."""

from __future__ import annotations

import pytest

from src.execution.errors import OrderRejected
from src.execution.models import AlgoResult, PositionSnapshot, PositionState
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


# ── revise_runner_tp ────────────────────────────────────────────────────────


class FakeRevisableClient(FakeClient):
    """Records cancel_algo + place_oco_algo calls for revise tests."""

    def __init__(self, *, cancel_raises=None, place_raises=None,
                 next_algo_id="NEW_ALGO"):
        super().__init__()
        self.cancelled: list[tuple[str, str]] = []
        self.placed: list[dict] = []
        self.cancel_raises = cancel_raises
        self.place_raises = place_raises
        self._next_algo_id = next_algo_id

    def cancel_algo(self, inst_id, algo_id):
        self.cancelled.append((inst_id, algo_id))
        if self.cancel_raises is not None:
            raise self.cancel_raises

    def list_pending_algos(self, inst_id: str, ord_type: str = "oco") -> list:
        # Default: algo already gone — the 51400 is legitimate.
        return []

    def place_oco_algo(self, *, inst_id, pos_side, size_contracts,
                       sl_trigger_px, tp_trigger_px, td_mode,
                       trigger_px_type=""):
        self.placed.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "size_contracts": size_contracts,
            "sl_trigger_px": sl_trigger_px, "tp_trigger_px": tp_trigger_px,
            "td_mode": td_mode,
            "trigger_px_type": trigger_px_type,
        })
        if self.place_raises is not None:
            raise self.place_raises
        return AlgoResult(
            algo_id=self._next_algo_id, client_algo_id=self._next_algo_id,
            sl_trigger_px=sl_trigger_px, tp_trigger_px=tp_trigger_px,
        )


def test_revise_runner_tp_cancels_and_replaces_runner_oco():
    client = FakeRevisableClient(next_algo_id="REPLACEMENT_ID")
    mon = PositionMonitor(client, margin_mode="cross")
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=2,
    )
    ok = mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71500.0)
    assert ok is True
    # Runner OCO (algo_ids[-1]) cancelled, fresh OCO placed at new TP.
    assert client.cancelled == [("BTC-USDT-SWAP", "RUNNER_ID")]
    assert len(client.placed) == 1
    p = client.placed[0]
    assert p["sl_trigger_px"] == 69000.0
    assert p["tp_trigger_px"] == 71500.0
    assert p["size_contracts"] == 2
    assert p["td_mode"] == "cross"
    snap = mon.get_tracked_runner("BTC-USDT-SWAP", "long")
    assert snap is not None
    assert snap["tp2_price"] == 71500.0
    # algo_ids tail rotated to the replacement
    snap_algo = mon._tracked[("BTC-USDT-SWAP", "long")].algo_ids
    assert snap_algo == ["TP1_ID", "REPLACEMENT_ID"]


def test_revise_runner_tp_noop_when_new_tp_equals_current():
    client = FakeRevisableClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=2,
    )
    assert mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=73000.0) is False
    assert client.cancelled == []
    assert client.placed == []


def test_revise_runner_tp_returns_false_when_position_unknown():
    client = FakeRevisableClient()
    mon = PositionMonitor(client)
    assert mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71500.0) is False
    assert client.cancelled == []
    assert client.placed == []


def test_revise_runner_tp_treats_idempotent_cancel_code_as_success():
    """Cancel returning OKX's 'algo already gone' codes is not an abort —
    the replacement OCO must still go up."""
    client = FakeRevisableClient(
        cancel_raises=OrderRejected("algo gone", code="51400"),
        next_algo_id="NEW_AFTER_GONE",
    )
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=2,
    )
    ok = mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71500.0)
    assert ok is True
    assert len(client.placed) == 1


def test_revise_runner_tp_aborts_on_unknown_cancel_error():
    """A non-idempotent cancel error must NOT proceed to place — the live
    runner OCO is still on the book and a re-place would double-protect."""
    client = FakeRevisableClient(
        cancel_raises=OrderRejected("oops", code="51999"),
    )
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=2,
    )
    ok = mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71500.0)
    assert ok is False
    assert client.placed == []
    snap = mon.get_tracked_runner("BTC-USDT-SWAP", "long")
    assert snap["tp2_price"] == 73000.0   # unchanged


def test_revise_runner_tp_unprotects_on_place_failure_after_cancel():
    """Cancel succeeded, place failed → runner unprotected, algo_ids
    trimmed to reflect that no replacement landed."""
    client = FakeRevisableClient(place_raises=RuntimeError("OCO rejected"))
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=2,
    )
    ok = mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71500.0)
    assert ok is False
    assert client.cancelled == [("BTC-USDT-SWAP", "RUNNER_ID")]
    snap_algo = mon._tracked[("BTC-USDT-SWAP", "long")].algo_ids
    assert snap_algo == ["TP1_ID"]


def test_revise_runner_tp_uses_be_price_for_sl_after_be_move():
    """Post-BE state: the active SL is the BE-adjusted price stored on the
    tracked record. The replacement OCO must reuse it, not the original
    plan SL."""
    client = FakeRevisableClient(next_algo_id="POST_BE_REVISE")
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=2.0, entry_price=70000.0,
        algo_ids=["TP1_ID", "BE_RUNNER_ID"], tp2_price=73000.0,
        sl_price=70070.0,             # entry + 0.1% BE buffer (already moved)
        runner_size=2, be_already_moved=True,
    )
    mon.revise_runner_tp("BTC-USDT-SWAP", "long", new_tp=71200.0)
    p = client.placed[0]
    assert p["sl_trigger_px"] == pytest.approx(70070.0)
    assert p["tp_trigger_px"] == 71200.0


def test_get_tracked_runner_returns_none_for_untracked():
    mon = PositionMonitor(FakeClient())
    assert mon.get_tracked_runner("ETH-USDT-SWAP", "long") is None


# ── lock_sl_at (Option A — MFE-triggered SL lock) ──────────────────────────


def test_lock_sl_at_cancels_and_replaces_runner_with_new_sl():
    """Happy path: runner OCO gets cancelled, replacement OCO goes up with
    the new SL + original TP. `sl_lock_applied` flips to True."""
    client = FakeRevisableClient(next_algo_id="LOCKED_OCO")
    mon = PositionMonitor(client, margin_mode="cross")
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    # MFE at 2R → new SL at entry (BE).
    ok = mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0)
    assert ok is True
    assert client.cancelled == [("BTC-USDT-SWAP", "RUNNER_ID")]
    assert len(client.placed) == 1
    p = client.placed[0]
    assert p["sl_trigger_px"] == 70000.0
    assert p["tp_trigger_px"] == 73000.0
    assert p["size_contracts"] == 3
    assert p["td_mode"] == "cross"
    snap = mon.get_tracked_runner("BTC-USDT-SWAP", "long")
    assert snap is not None
    assert snap["sl_lock_applied"] is True
    assert snap["sl_price"] == 70000.0
    assert mon._tracked[("BTC-USDT-SWAP", "long")].algo_ids == ["LOCKED_OCO"]


def test_lock_sl_at_is_one_shot():
    """Second call on the same position is a no-op — no cancel, no place."""
    client = FakeRevisableClient(next_algo_id="LOCKED_OCO")
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    assert mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0) is True
    # Reset the capture so the no-op is easy to see.
    client.cancelled.clear()
    client.placed.clear()
    assert mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70500.0) is False
    assert client.cancelled == []
    assert client.placed == []


def test_lock_sl_at_returns_false_for_untracked():
    client = FakeRevisableClient()
    mon = PositionMonitor(client)
    assert mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0) is False
    assert client.placed == []


def test_lock_sl_at_rejects_new_sl_on_wrong_side_of_tp():
    """For a long, new_sl >= tp is nonsense — guard must abort without
    touching the runner OCO."""
    client = FakeRevisableClient()
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    ok = mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=73500.0)
    assert ok is False
    assert client.cancelled == []
    assert client.placed == []


def test_lock_sl_at_short_direction():
    """Short parity: new SL below original but above entry (BE for short)."""
    client = FakeRevisableClient(next_algo_id="SHORT_LOCKED")
    mon = PositionMonitor(client)
    mon.register_open(
        "ETH-USDT-SWAP", "short", size=2.0, entry_price=2300.0,
        algo_ids=["RUNNER_ID"], tp2_price=2200.0,
        sl_price=2330.0, runner_size=2, plan_sl_price=2330.0,
    )
    ok = mon.lock_sl_at("ETH-USDT-SWAP", "short", new_sl=2300.0)
    assert ok is True
    p = client.placed[0]
    assert p["sl_trigger_px"] == 2300.0
    assert p["tp_trigger_px"] == 2200.0
    assert p["pos_side"] == "short"


def test_lock_sl_at_unprotects_on_place_failure():
    """Cancel succeeded, place failed → algo_ids trimmed, flag still set
    (prevents retry spin)."""
    client = FakeRevisableClient(place_raises=RuntimeError("OCO rejected"))
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    ok = mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0)
    assert ok is False
    assert mon._tracked[("BTC-USDT-SWAP", "long")].algo_ids == []
    # Flag still set so we don't loop forever.
    assert mon._tracked[("BTC-USDT-SWAP", "long")].sl_lock_applied is True


def test_lock_sl_at_aborts_on_unknown_cancel_error():
    client = FakeRevisableClient(
        cancel_raises=OrderRejected("oops", code="51999"),
    )
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    assert mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0) is False
    assert client.placed == []
    # Flag NOT set on a clean abort — runner OCO is untouched, next cycle
    # may retry.
    assert mon._tracked[("BTC-USDT-SWAP", "long")].sl_lock_applied is False


def test_lock_sl_at_idempotent_cancel_proceeds_to_place():
    client = FakeRevisableClient(
        cancel_raises=OrderRejected("algo gone", code="51400"),
        next_algo_id="LOCKED_AFTER_GONE",
    )
    mon = PositionMonitor(client)
    mon.register_open(
        "BTC-USDT-SWAP", "long", size=3.0, entry_price=70000.0,
        algo_ids=["RUNNER_ID"], tp2_price=73000.0,
        sl_price=69000.0, runner_size=3, plan_sl_price=69000.0,
    )
    ok = mon.lock_sl_at("BTC-USDT-SWAP", "long", new_sl=70000.0)
    assert ok is True
    assert len(client.placed) == 1
