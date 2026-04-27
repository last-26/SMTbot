"""Phase 7.C3 — PENDING state lifecycle in PositionMonitor.

The monitor tracks pending limit entries alongside open positions. Each
poll asks the exchange for the current order state and emits one `PendingEvent`
per transition:
  - filled                          → FILLED (reason="fill")
  - canceled / mmp_canceled         → CANCELED (reason="external")
  - live / partially_filled, aged   → cancel then CANCELED (reason="timeout")
  - partially filled AND aged       → FILLED with the partial size
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.execution.errors import OrderRejected
from src.execution.models import PositionState
from src.execution.position_monitor import PositionMonitor


UTC = timezone.utc


class _FakeBybit:
    """Matches the subset of BybitClient that PositionMonitor consumes."""

    def __init__(self):
        self.order_states: dict[str, dict] = {}     # order_id → dict
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_raises: Optional[Exception] = None
        self.get_order_raises: Optional[Exception] = None

    def get_order(self, inst_id: str, order_id: str) -> dict:
        if self.get_order_raises is not None:
            raise self.get_order_raises
        return self.order_states.get(
            order_id,
            {"state": "live", "accFillSz": "0", "avgPx": "0"},
        )

    def cancel_order(self, inst_id: str, order_id: str) -> dict:
        self.cancel_calls.append((inst_id, order_id))
        if self.cancel_raises is not None:
            raise self.cancel_raises
        return {}

    # Unused by the pending path but present so _tracked features still work.
    def get_positions(self, inst_id=None):
        return []


def _mk(monitor_kwargs: Optional[dict] = None):
    fake = _FakeBybit()
    monitor = PositionMonitor(fake, **(monitor_kwargs or {}))
    return monitor, fake


def _register(monitor, inst="BTC-USDT-SWAP", side="long", order_id="LIM-1",
              *, num_contracts=5, entry_px=100.0, max_wait_s=180.0,
              placed_at: Optional[datetime] = None):
    monitor.register_pending(
        inst_id=inst, pos_side=side, order_id=order_id,
        num_contracts=num_contracts, entry_px=entry_px,
        max_wait_s=max_wait_s,
        placed_at=placed_at or datetime.now(UTC),
    )


# ── Registration + state ────────────────────────────────────────────────────


def test_register_pending_reports_pending_state():
    monitor, _ = _mk()
    _register(monitor)
    assert monitor.pending_count == 1
    assert monitor.state("BTC-USDT-SWAP", "long") == PositionState.PENDING


def test_state_prefers_open_over_pending():
    """If the same key is in both dicts, OPEN wins (the live position is
    the authoritative state; pending should have been cleared on fill)."""
    monitor, _ = _mk()
    _register(monitor)
    monitor.register_open(
        "BTC-USDT-SWAP", "long", size=5.0, entry_price=100.0,
    )
    assert monitor.state("BTC-USDT-SWAP", "long") == PositionState.OPEN


# ── poll_pending — happy paths ─────────────────────────────────────────────


def test_poll_pending_emits_filled_when_order_filled():
    monitor, fake = _mk()
    _register(monitor)
    fake.order_states["LIM-1"] = {
        "state": "filled", "accFillSz": "5", "avgPx": "100.25",
    }
    events = monitor.poll_pending()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "FILLED"
    assert ev.reason == "fill"
    assert ev.filled_size == 5.0
    assert ev.avg_price == 100.25
    # Pending row is consumed; state returns to CLOSED.
    assert monitor.pending_count == 0
    assert monitor.state("BTC-USDT-SWAP", "long") == PositionState.CLOSED


def test_poll_pending_emits_canceled_when_externally_canceled():
    monitor, fake = _mk()
    _register(monitor)
    fake.order_states["LIM-1"] = {
        "state": "canceled", "accFillSz": "0", "avgPx": "0",
    }
    events = monitor.poll_pending()
    assert len(events) == 1
    assert events[0].event_type == "CANCELED"
    assert events[0].reason == "external"
    assert monitor.pending_count == 0


def test_poll_pending_mmp_canceled_treated_as_external_cancel():
    monitor, fake = _mk()
    _register(monitor)
    fake.order_states["LIM-1"] = {"state": "mmp_canceled"}
    events = monitor.poll_pending()
    assert events[0].reason == "external"


# ── poll_pending — timeout ─────────────────────────────────────────────────


def test_poll_pending_times_out_and_cancels():
    """Live order older than max_wait_s → the exchange cancel + CANCELED event."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {"state": "live"}

    events = monitor.poll_pending()

    assert len(events) == 1
    assert events[0].event_type == "CANCELED"
    assert events[0].reason == "timeout"
    assert fake.cancel_calls == [("BTC-USDT-SWAP", "LIM-1")]
    assert monitor.pending_count == 0


def test_poll_pending_fresh_order_is_left_alone():
    """Still within max_wait_s → no event, no cancel call, row stays."""
    monitor, fake = _mk()
    _register(monitor, max_wait_s=600.0)
    fake.order_states["LIM-1"] = {"state": "live"}

    events = monitor.poll_pending()
    assert events == []
    assert fake.cancel_calls == []
    assert monitor.pending_count == 1


def test_poll_pending_partial_fill_on_timeout_emits_filled():
    """An order that partially filled before hitting timeout — cancel the
    rest, but surface the filled fraction as FILLED so the runner can
    move what did fill into the open-position tracker."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, num_contracts=5, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {
        "state": "partially_filled", "accFillSz": "2", "avgPx": "100.1",
    }

    events = monitor.poll_pending()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "FILLED"
    assert ev.reason == "timeout_partial_fill"
    assert ev.filled_size == 2.0
    # Cancel attempted to kill the remainder.
    assert fake.cancel_calls == [("BTC-USDT-SWAP", "LIM-1")]


# ── poll_pending — error handling ──────────────────────────────────────────


def test_poll_pending_skips_row_on_get_order_exception():
    """Transient `get_order` failure: log it, keep the row for the next
    poll. No event, no cancel."""
    monitor, fake = _mk()
    _register(monitor)
    fake.get_order_raises = RuntimeError("network blip")

    events = monitor.poll_pending()
    assert events == []
    assert monitor.pending_count == 1


def test_poll_pending_cancel_already_gone_is_idempotent():
    """Order vanished between get_order (returned live) and cancel (51400);
    still emit CANCELED — the state snapshot was stale."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {"state": "live"}
    fake.cancel_raises = OrderRejected("gone", code="51400", payload={})

    events = monitor.poll_pending()
    assert len(events) == 1
    assert events[0].event_type == "CANCELED"
    assert monitor.pending_count == 0


# ── cancel_pending ─────────────────────────────────────────────────────────


def test_cancel_pending_returns_none_when_not_tracked():
    monitor, _ = _mk()
    assert monitor.cancel_pending("BTC-USDT-SWAP", "long") is None


def test_cancel_pending_emits_event_and_clears_row():
    monitor, fake = _mk()
    _register(monitor)
    ev = monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="invalidated")
    assert ev is not None
    assert ev.event_type == "CANCELED"
    assert ev.reason == "invalidated"
    assert fake.cancel_calls == [("BTC-USDT-SWAP", "LIM-1")]
    assert monitor.pending_count == 0


def test_cancel_pending_idempotent_when_exchange_reports_gone():
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = OrderRejected("gone", code="170142", payload={})
    # Default get_order returns {"state": "live"} — inconclusive, falls
    # back to legacy CANCELED behavior.
    ev = monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="manual")
    assert ev is not None
    assert ev.event_type == "CANCELED"
    assert monitor.pending_count == 0


# ── Regression: 2026-04-27 phantom-cancel-vs-fill race (F6) ────────────────
#
# Bybit V5's `_ORDER_GONE_CODES` (110001/110008/110010/170142/170213) cover
# both "already cancelled" and "already filled". If the order actually
# filled in the millisecond window between our cancel-cmd dispatch and
# Bybit's rejection, blindly accepting the idempotent code as "cancel
# success" loses the fill event — the position lives on without SL/TP.
#
# Fix (F6): when cancel returns a gone-code, verify the real terminal
# state via get_order and route Filled to FILLED instead of CANCELED.


def test_cancel_pending_phantom_fill_routes_to_FILLED():
    """If cancel hits gone-code AND get_order returns Filled, surface the
    fill event so the caller can route through fill flow (record_open +
    SL/TP attach). Loses the cancel reason — caller treats as a normal
    fill instead."""
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = OrderRejected("gone", code="170142", payload={})
    # The order actually filled before our cancel arrived.
    fake.order_states["LIM-1"] = {
        "orderStatus": "Filled", "cumExecQty": "5", "avgPrice": "100.25",
    }

    ev = monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="invalidated")

    assert ev is not None
    assert ev.event_type == "FILLED"
    assert ev.reason == "phantom_cancel_recovery"
    assert ev.filled_size == 5.0
    assert ev.avg_price == 100.25
    assert monitor.pending_count == 0


def test_cancel_pending_verified_cancelled_keeps_caller_reason():
    """If cancel hits gone-code AND get_order confirms Cancelled status,
    surface CANCELED with the caller's reason — same as the legacy
    happy path."""
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = OrderRejected("gone", code="170142", payload={})
    fake.order_states["LIM-1"] = {
        "orderStatus": "Cancelled", "cumExecQty": "0", "avgPrice": "0",
    }

    ev = monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="invalidated")

    assert ev is not None
    assert ev.event_type == "CANCELED"
    assert ev.reason == "invalidated"
    assert monitor.pending_count == 0


def test_cancel_pending_verify_get_order_failure_falls_back_to_CANCELED():
    """If get_order itself fails after a gone-code cancel, fall back to
    legacy idempotent-cancel behavior (don't lose the row, but accept
    cancel best-effort). Logs a warning so audits notice."""
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = OrderRejected("gone", code="170142", payload={})
    fake.get_order_raises = RuntimeError("tcp reset on verify")

    ev = monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="manual")

    assert ev is not None
    assert ev.event_type == "CANCELED"
    assert ev.reason == "manual"
    assert monitor.pending_count == 0


# ── Regression: 2026-04-20 phantom-cancel bug ──────────────────────────────
#
# the exchange sCode 50001 ("service temporarily unavailable") hit the cancel path
# during a transient outage. The monitor previously logged the failure and
# emitted CANCELED anyway — dropping tracking while the order stayed live
# on the exchange as a phantom orphan. When the next cycle placed a new limit at a
# similar price, the account ended up with two resting longs on the same
# symbol that could fill into unprotected positions.
#
# Fix: cancel failures that are NOT idempotent-gone (51400/1/2) must keep
# the pending row tracked so the next poll retries.


def test_poll_pending_keeps_row_when_timeout_cancel_fails_transient():
    """sCode 50001 on cancel → keep the row, no event, next poll retries."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {"state": "live"}
    fake.cancel_raises = OrderRejected(
        "service temporarily unavailable", code="50001", payload={},
    )

    events = monitor.poll_pending()

    assert events == []
    assert monitor.pending_count == 1
    # Cancel was attempted once; row is preserved for retry.
    assert fake.cancel_calls == [("BTC-USDT-SWAP", "LIM-1")]


def test_poll_pending_keeps_row_when_timeout_cancel_raises_generic():
    """Non-OrderRejected exception (network-level) — same behavior as 50001."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {"state": "live"}
    fake.cancel_raises = RuntimeError("tcp reset")

    events = monitor.poll_pending()

    assert events == []
    assert monitor.pending_count == 1


def test_poll_pending_retries_cancel_on_next_poll_after_transient_failure():
    """Cancel fails once, succeeds on the next poll — row finally clears."""
    monitor, fake = _mk()
    old = datetime.now(UTC) - timedelta(seconds=300)
    _register(monitor, max_wait_s=60.0, placed_at=old)
    fake.order_states["LIM-1"] = {"state": "live"}

    # First poll: transient failure.
    fake.cancel_raises = OrderRejected("busy", code="50001", payload={})
    events1 = monitor.poll_pending()
    assert events1 == []
    assert monitor.pending_count == 1

    # Second poll: the exchange recovered.
    fake.cancel_raises = None
    events2 = monitor.poll_pending()
    assert len(events2) == 1
    assert events2[0].event_type == "CANCELED"
    assert events2[0].reason == "timeout"
    assert monitor.pending_count == 0
    # Cancel was attempted twice (one failed, one succeeded).
    assert fake.cancel_calls == [
        ("BTC-USDT-SWAP", "LIM-1"),
        ("BTC-USDT-SWAP", "LIM-1"),
    ]


def test_cancel_pending_reraises_on_non_gone_rejection():
    """Caller-driven cancel + sCode 50001 → re-raise, keep tracking. The
    caller needs to know the cancel didn't land so they can retry or alert."""
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = OrderRejected("busy", code="50001", payload={})

    with pytest.raises(OrderRejected):
        monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="invalidated")

    assert monitor.pending_count == 1


def test_cancel_pending_reraises_on_generic_exception():
    monitor, fake = _mk()
    _register(monitor)
    fake.cancel_raises = RuntimeError("tcp reset")

    with pytest.raises(RuntimeError):
        monitor.cancel_pending("BTC-USDT-SWAP", "long", reason="manual")

    assert monitor.pending_count == 1
