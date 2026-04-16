"""Monitor-layer TP1 partial-fill detection + SL-to-BE (Madde E).

When the position's live size drops below `initial_size` but stays > 0,
the monitor cancels the TP2 algo and replaces it with an OCO whose SL
sits at the entry price (breakeven) on the remaining contracts.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.execution.models import AlgoResult, CloseFill, PositionSnapshot
from src.execution.position_monitor import PositionMonitor


class _FakeClient:
    """Just the surface PositionMonitor touches."""

    def __init__(self, positions: Optional[list[PositionSnapshot]] = None,
                 cancel_raises: bool = False):
        self.positions = positions or []
        self.cancelled: list[tuple[str, str]] = []
        self.placed: list[dict] = []
        self.cancel_raises = cancel_raises
        self._algo_counter = 0

    def get_positions(self, inst_id: Optional[str] = None) -> list[PositionSnapshot]:
        return list(self.positions)

    def cancel_algo(self, inst_id: str, algo_id: str) -> dict:
        self.cancelled.append((inst_id, algo_id))
        if self.cancel_raises:
            raise RuntimeError("cancel failed")
        return {}

    def place_oco_algo(self, *, inst_id, pos_side, size_contracts,
                       sl_trigger_px, tp_trigger_px, td_mode="isolated") -> AlgoResult:
        self._algo_counter += 1
        self.placed.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "size": size_contracts, "sl": sl_trigger_px, "tp": tp_trigger_px,
        })
        return AlgoResult(
            algo_id=f"NEW{self._algo_counter}", client_algo_id=f"cliNEW{self._algo_counter}",
            sl_trigger_px=sl_trigger_px, tp_trigger_px=tp_trigger_px,
        )


def _snap(size: float, entry: float = 100.0) -> PositionSnapshot:
    return PositionSnapshot(
        inst_id="BTC-USDT-SWAP", pos_side="long",
        size=size, entry_price=entry, mark_price=entry,
        unrealized_pnl=0.0, leverage=10,
    )


# ── TP1 fill detection ──────────────────────────────────────────────────────


def test_tp1_fill_triggers_sl_to_be():
    client = _FakeClient(positions=[_snap(size=5.0)])   # was 10, now 5 → TP1 fill
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )

    fills = monitor.poll()
    assert fills == []
    # TP2 algo was cancelled, new algo placed with SL=entry on 5 contracts
    assert client.cancelled == [("BTC-USDT-SWAP", "ALG2")]
    assert len(client.placed) == 1
    p = client.placed[0]
    assert p["size"] == 5
    assert p["sl"] == pytest.approx(100.0)
    assert p["tp"] == pytest.approx(105.0)


def test_sl_to_be_is_idempotent():
    client = _FakeClient(positions=[_snap(size=5.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    monitor.poll()
    # Only one cancel + one replace even with two polls.
    assert len(client.cancelled) == 1
    assert len(client.placed) == 1


def test_sl_to_be_failure_retries_next_poll():
    client = _FakeClient(positions=[_snap(size=5.0)], cancel_raises=True)
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    # Cancel threw → be_already_moved stayed False → next poll retries.
    client.cancel_raises = False
    monitor.poll()
    assert len(client.cancelled) == 2
    assert len(client.placed) == 1


# ── No-op guards ────────────────────────────────────────────────────────────


def test_disabled_flag_never_moves_sl():
    client = _FakeClient(positions=[_snap(size=5.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=False)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    assert client.cancelled == []
    assert client.placed == []


def test_single_algo_position_is_not_touched():
    # Only one algo (partial TP disabled at open) — no TP2 to cancel.
    client = _FakeClient(positions=[_snap(size=5.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1"], tp2_price=105.0,
    )
    monitor.poll()
    assert client.cancelled == []
    assert client.placed == []


def test_size_unchanged_does_not_trigger():
    # Size still at initial → position hasn't partially filled.
    client = _FakeClient(positions=[_snap(size=10.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    assert client.cancelled == []
    assert client.placed == []


def test_full_close_does_not_trigger_sl_move():
    # Size dropped all the way to 0 → it's a CloseFill, not a partial.
    client = _FakeClient(positions=[])                       # nothing live
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    fills = monitor.poll()
    assert len(fills) == 1
    assert isinstance(fills[0], CloseFill)
    assert client.cancelled == []
    assert client.placed == []
