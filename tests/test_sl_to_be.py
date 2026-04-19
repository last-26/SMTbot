"""Monitor-layer TP1 partial-fill detection + SL-to-BE (Madde E).

When the position's live size drops below `initial_size` but stays > 0,
the monitor cancels the TP2 algo and replaces it with an OCO whose SL
sits at the entry price (breakeven) on the remaining contracts.
"""

from __future__ import annotations

from typing import Optional

import pytest

from src.execution.errors import OrderRejected
from src.execution.models import AlgoResult, CloseFill, PositionSnapshot
from src.execution.position_monitor import PositionMonitor


class _FakeClient:
    """Just the surface PositionMonitor touches."""

    def __init__(self, positions: Optional[list[PositionSnapshot]] = None,
                 cancel_raises: bool = False,
                 cancel_error: Optional[Exception] = None,
                 place_raises: bool = False):
        self.positions = positions or []
        self.cancelled: list[tuple[str, str]] = []
        self.placed: list[dict] = []
        self.cancel_raises = cancel_raises
        self.cancel_error = cancel_error
        self.place_raises = place_raises
        self._algo_counter = 0

    def get_positions(self, inst_id: Optional[str] = None) -> list[PositionSnapshot]:
        return list(self.positions)

    def cancel_algo(self, inst_id: str, algo_id: str) -> dict:
        self.cancelled.append((inst_id, algo_id))
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_raises:
            raise RuntimeError("cancel failed")
        return {}

    def list_pending_algos(self, inst_id: str, ord_type: str = "oco") -> list:
        # Default: algo already gone (matches the common post-fill case).
        return []

    def place_oco_algo(self, *, inst_id, pos_side, size_contracts,
                       sl_trigger_px, tp_trigger_px, td_mode="isolated",
                       trigger_px_type="") -> AlgoResult:
        if self.place_raises:
            raise OrderRejected("place failed", code="51000")
        self._algo_counter += 1
        self.placed.append({
            "inst_id": inst_id, "pos_side": pos_side,
            "size": size_contracts, "sl": sl_trigger_px, "tp": tp_trigger_px,
            "trigger_px_type": trigger_px_type,
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


# ── BE offset (fee buffer past entry) ─────────────────────────────────────


def test_be_offset_long_places_stop_above_entry():
    # Long: BE stop should sit ABOVE entry so a touch-back closes net-zero
    # after the exit taker fee on the surviving leg.
    client = _FakeClient(positions=[_snap(size=5.0, entry=100.0)])
    monitor = PositionMonitor(
        client, move_sl_to_be_enabled=True, sl_be_offset_pct=0.001,
    )
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    assert client.placed[0]["sl"] == pytest.approx(100.0 + 100.0 * 0.001)


def test_be_offset_short_places_stop_below_entry():
    # Short: BE stop sits BELOW entry for the same net-zero-on-touchback
    # invariant (short profits as price falls).
    client = _FakeClient(positions=[PositionSnapshot(
        inst_id="BTC-USDT-SWAP", pos_side="short",
        size=5.0, entry_price=100.0, mark_price=100.0,
        unrealized_pnl=0.0, leverage=10,
    )])
    monitor = PositionMonitor(
        client, move_sl_to_be_enabled=True, sl_be_offset_pct=0.001,
    )
    monitor.register_open(
        "BTC-USDT-SWAP", "short", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=95.0,
    )
    monitor.poll()
    assert client.placed[0]["sl"] == pytest.approx(100.0 - 100.0 * 0.001)


def test_be_offset_zero_preserves_exact_entry_behavior():
    # sl_be_offset_pct=0 (default) keeps the legacy "SL = entry" semantics,
    # so existing tests + pre-change restart paths stay intact.
    client = _FakeClient(positions=[_snap(size=5.0, entry=100.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    assert client.placed[0]["sl"] == pytest.approx(100.0)


# ── Recovery from stale / failed cancel or place ───────────────────────────


def test_cancel_with_algo_gone_code_proceeds_to_place():
    # If TP2 was already cancelled externally (e.g. by a prior poll whose
    # place leg failed), OKX returns code 51400. Cancel should be treated
    # as idempotent and the BE OCO placed anyway, so the position is no
    # longer unprotected.
    client = _FakeClient(
        positions=[_snap(size=5.0)],
        cancel_error=OrderRejected("algo does not exist", code="51400"),
    )
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    assert len(client.placed) == 1
    assert client.placed[0]["size"] == 5
    assert monitor._tracked[("BTC-USDT-SWAP", "long")].be_already_moved is True


def test_cancel_with_unknown_code_retries_up_to_backstop():
    # Generic non-"gone" OrderRejected increments retry counter. After
    # _CANCEL_MAX_RETRIES attempts, monitor gives up and marks
    # be_already_moved=True so poll stops spinning.
    from src.execution.position_monitor import _CANCEL_MAX_RETRIES

    client = _FakeClient(
        positions=[_snap(size=5.0)],
        cancel_error=OrderRejected("weird", code="99999"),
    )
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )

    for _ in range(_CANCEL_MAX_RETRIES):
        monitor.poll()
    # After max retries, monitor gives up — no more cancels on the next poll.
    tracked = monitor._tracked[("BTC-USDT-SWAP", "long")]
    assert tracked.be_already_moved is True
    assert tracked.cancel_retry_count == _CANCEL_MAX_RETRIES
    assert client.placed == []

    prior_cancel_count = len(client.cancelled)
    monitor.poll()
    assert len(client.cancelled) == prior_cancel_count  # no retry-spin


def test_cancel_runtime_error_still_counts_toward_retry_cap():
    # Generic (non-OrderRejected) exceptions go through the fallback
    # `except Exception` branch but still increment the retry counter.
    from src.execution.position_monitor import _CANCEL_MAX_RETRIES

    client = _FakeClient(positions=[_snap(size=5.0)], cancel_raises=True)
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    for _ in range(_CANCEL_MAX_RETRIES):
        monitor.poll()
    tracked = monitor._tracked[("BTC-USDT-SWAP", "long")]
    assert tracked.be_already_moved is True
    assert tracked.cancel_retry_count == _CANCEL_MAX_RETRIES


def test_place_failure_after_cancel_marks_unprotected_no_spin():
    # The real-world bug: cancel succeeded, place OCO failed → remaining
    # leg unprotected. Monitor must mark be_already_moved=True so it
    # doesn't try to re-cancel a vanished TP2 every poll. Journal
    # callback fires with only TP1 in algo_ids.
    client = _FakeClient(positions=[_snap(size=5.0)], place_raises=True)
    callback_calls: list[tuple[str, str, list[str]]] = []

    def on_sl_moved(inst_id, pos_side, algo_ids):
        callback_calls.append((inst_id, pos_side, list(algo_ids)))

    monitor = PositionMonitor(
        client, move_sl_to_be_enabled=True, on_sl_moved=on_sl_moved,
    )
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )

    monitor.poll()
    tracked = monitor._tracked[("BTC-USDT-SWAP", "long")]
    assert tracked.be_already_moved is True
    assert tracked.algo_ids == ["ALG1"]  # TP2 dropped, journal callback reflects it
    assert callback_calls == [("BTC-USDT-SWAP", "long", ["ALG1"])]

    # Subsequent polls must NOT re-attempt cancel or place — that was the
    # original retry-spin bug.
    prior_cancel_count = len(client.cancelled)
    monitor.poll()
    monitor.poll()
    assert len(client.cancelled) == prior_cancel_count


def test_successful_path_resets_retry_counter_via_be_flag():
    # After a successful SL-to-BE move, be_already_moved=True means
    # subsequent polls short-circuit before touching the retry counter,
    # so the counter value is irrelevant post-success. This exercises the
    # happy path to make sure retry-counter code didn't regress it.
    client = _FakeClient(positions=[_snap(size=5.0)])
    monitor = PositionMonitor(client, move_sl_to_be_enabled=True)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", 10.0, 100.0,
        algo_ids=["ALG1", "ALG2"], tp2_price=105.0,
    )
    monitor.poll()
    tracked = monitor._tracked[("BTC-USDT-SWAP", "long")]
    assert tracked.be_already_moved is True
    assert tracked.cancel_retry_count == 0
    monitor.poll()
    assert len(client.cancelled) == 1
    assert len(client.placed) == 1
