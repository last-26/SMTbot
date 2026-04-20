"""Position polling → close-fill events.

The OrderRouter places orders and walks away. The PositionMonitor watches
OKX for the effect: entry fills, SL/TP hits, liquidations. It emits:

  - on open→close transition: a CloseFill record, which the caller feeds
    into RiskManager.register_trade_closed().
  - on partial TP1 fill (size shrinks but not to zero), it cancels the
    remaining TP2 algo and places a new SL-at-breakeven OCO for the
    surviving contracts (Madde E).

No websocket — we REST-poll because the bot loop is already polling the
TV MCP every N seconds. Adding a second concurrent connection isn't worth
the complexity for MVP demo flow.

Stateful: the monitor remembers the last snapshot of each (inst_id,
pos_side) key so it can detect the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from loguru import logger

from src.execution.errors import OrderRejected
from src.execution.models import CloseFill, PositionSnapshot, PositionState
from src.execution.okx_client import OKXClient


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)

# OKX V5 error codes where cancel_algo_order can be treated as already-done:
# 51400 = order does not exist, 51401 = already canceled, 51402 = already
# filled. If the TP2 algo vanished (OCO cascade after TP1 fill, prior-poll
# cancel whose place leg failed, operator manual cancel), the monitor's
# cancel is a no-op — proceed to place the BE replacement instead of
# spinning on the cancel every poll.
_ALGO_GONE_CODES = frozenset({"51400", "51401", "51402"})

# Backstop: if cancel keeps failing with an unknown code (e.g. malformed
# OKX response with no sCode), stop after this many polls and surface the
# position as unprotected. Prevents the retry-spin pathology even when
# `_ALGO_GONE_CODES` doesn't match the actual error code.
_CANCEL_MAX_RETRIES = 3


@dataclass
class _Tracked:
    inst_id: str
    pos_side: str
    size: float
    entry_price: float
    initial_size: float = 0.0
    algo_ids: list[str] = field(default_factory=list)
    tp2_price: Optional[float] = None
    be_already_moved: bool = False
    cancel_retry_count: int = 0
    # 2026-04-19 — dynamic TP revision needs the active SL on the runner OCO
    # (so a cancel+place keeps stop discipline) and the runner OCO size (so
    # the replacement OCO covers the right slice in partial-TP mode).
    sl_price: float = 0.0
    runner_size: int = 0
    last_tp_revise_at: Optional[datetime] = None
    # Immutable plan SL, snapshotted at entry. `sl_price` mutates after BE
    # move, which destroys the original risk distance — dynamic TP revision
    # needs the plan SL to compute `entry + target_rr × plan_sl_distance`,
    # else post-BE sl_distance collapses to the BE offset (~0.1%) and the
    # 1:N target becomes unreachably close to mark (OKX rejects as 51277).
    plan_sl_price: float = 0.0
    # 2026-04-20 — MFE-triggered SL lock (Option A). One-shot flag: once the
    # position has crossed sl_lock_mfe_r in favor and the runner OCO has
    # been re-placed with a BE / profit-locked SL, further MFE gains do NOT
    # tighten further. A true trailing mechanic (Phase 12 Option B) would
    # keep moving; this is the simpler "one-time risk removal" contract.
    sl_lock_applied: bool = False
    # 2026-04-20 — resting TP limit (reduce-only post-only) co-placed with
    # the OCO. Bypasses the OCO tpOrdPx=-1 market-on-trigger path for
    # wick-captures. Empty string when disabled or the resting-limit leg
    # failed to place (position still protected by OCO, just no maker-TP).
    tp_limit_order_id: str = ""


@dataclass
class _PendingEntry:
    """A limit entry placed but not yet filled (Phase 7.C3)."""
    inst_id: str
    pos_side: str
    order_id: str
    num_contracts: int
    entry_px: float
    placed_at: datetime
    max_wait_s: float


@dataclass
class PendingEvent:
    """Emitted by `poll_pending` / `cancel_pending` when a limit entry
    transitions out of PENDING.

    - `event_type="FILLED"`: runner should place OCO and call
      `register_open` to move the row into the live-position tracker.
    - `event_type="CANCELED"`: setup died before filling; runner logs,
      may record a `zone_timeout_cancel` reject, and cleans up.
    """
    inst_id: str
    pos_side: str
    order_id: str
    event_type: str            # "FILLED" | "CANCELED"
    reason: str                # "fill" | "timeout" | "external" | "manual" | "invalidated"
    filled_size: float = 0.0
    avg_price: float = 0.0


_TERMINAL_FILLED = frozenset({"filled"})
_TERMINAL_CANCELED = frozenset({"canceled", "mmp_canceled"})


class PositionMonitor:
    """Tracks open positions and emits CloseFill events on closure."""

    def __init__(
        self,
        client: OKXClient,
        *,
        margin_mode: str = "isolated",
        move_sl_to_be_enabled: bool = False,
        sl_be_offset_pct: float = 0.0,
        on_sl_moved: Optional[Callable[[str, str, list[str]], None]] = None,
        algo_trigger_px_type: str = "mark",
    ):
        self.client = client
        self.margin_mode = margin_mode
        self.move_sl_to_be_enabled = move_sl_to_be_enabled
        self.sl_be_offset_pct = sl_be_offset_pct
        self._on_sl_moved = on_sl_moved
        # Replacement OCOs (BE move, dynamic-TP revise) inherit the same
        # trigger-price source as the initial OCO so behavior stays
        # consistent across the position's lifetime.
        self.algo_trigger_px_type = algo_trigger_px_type
        self._tracked: dict[tuple[str, str], _Tracked] = {}
        # Phase 7.C3 — limit entries placed but not yet filled.
        self._pending: dict[tuple[str, str], _PendingEntry] = {}

    # Called by the router after it places an order, so the monitor
    # "knows" to expect this position on the next poll. On restart,
    # `_rehydrate_open_positions` also calls this with `be_already_moved=True`
    # for positions whose SL-to-BE dance completed pre-restart, so the
    # monitor does not try to cancel the (already-replaced) TP2 again.
    def register_open(
        self,
        inst_id: str,
        pos_side: str,
        size: float,
        entry_price: float,
        *,
        algo_ids: Optional[list[str]] = None,
        tp2_price: Optional[float] = None,
        be_already_moved: bool = False,
        sl_price: float = 0.0,
        runner_size: int = 0,
        plan_sl_price: Optional[float] = None,
        tp_limit_order_id: str = "",
    ) -> None:
        # plan_sl_price semantics: None → caller didn't provide one, default to
        # sl_price (correct at fill time). An explicit 0.0 → "unknown, disable
        # dynamic-TP revise" (rehydrate path for post-BE positions).
        resolved_plan_sl = sl_price if plan_sl_price is None else plan_sl_price
        self._tracked[(inst_id, pos_side)] = _Tracked(
            inst_id=inst_id, pos_side=pos_side, size=size, entry_price=entry_price,
            initial_size=size, algo_ids=list(algo_ids or []), tp2_price=tp2_price,
            be_already_moved=be_already_moved,
            sl_price=sl_price, runner_size=runner_size or int(size),
            plan_sl_price=resolved_plan_sl,
            tp_limit_order_id=tp_limit_order_id,
        )

    def poll(self, inst_id: Optional[str] = None) -> list[CloseFill]:
        """Pull current positions from OKX, emit fills for anything that closed.

        As a side-effect on the way, if a partial TP1 fill is detected and
        `move_sl_to_be_enabled`, cancel the remaining TP2 algo and place a
        new SL-at-breakeven OCO for the surviving contracts.
        """
        live_snaps: dict[tuple[str, str], PositionSnapshot] = {}
        for snap in self.client.get_positions(inst_id=inst_id):
            if snap.size != 0.0:
                live_snaps[(snap.inst_id, snap.pos_side)] = snap

        fills: list[CloseFill] = []
        to_remove: list[tuple[str, str]] = []
        for key, tracked in self._tracked.items():
            if inst_id is not None and key[0] != inst_id:
                continue  # skip tracked positions on other instruments this poll
            if key not in live_snaps:
                # Tracked position no longer live → it closed.
                fills.append(self._close_fill_from(tracked))
                # Best-effort cancel the resting TP limit so it doesn't
                # linger as an orphan when the OCO closed us first.
                # Reduce-only keeps it inert even if the cancel races, but
                # leaving resting orders behind trips the next startup's
                # orphan-pending-limit sweep.
                if tracked.tp_limit_order_id:
                    self._cancel_tp_limit_best_effort(
                        tracked.inst_id, tracked.tp_limit_order_id,
                    )
                to_remove.append(key)
            else:
                snap = live_snaps[key]
                self._detect_tp1_and_move_sl(tracked, snap)
                # Refresh cached entry_price/size from the live row
                tracked.size = snap.size
                if snap.entry_price > 0:
                    tracked.entry_price = snap.entry_price

        for key in to_remove:
            self._tracked.pop(key, None)

        return fills

    def _detect_tp1_and_move_sl(
        self, t: _Tracked, snap: PositionSnapshot,
    ) -> None:
        """If the live size shrank (TP1 fill) and we haven't moved SL yet,
        cancel TP2 and replace it with SL=entry on the remaining size.

        Failure handling (every branch exits without spin):

        1. Cancel fails with `_ALGO_GONE_CODES` → treat as idempotent
           success and proceed to place (handles OCO cascade, prior-poll
           cancel whose place leg failed, operator manual cancel).
        2. Cancel fails with an unknown code → increment
           `cancel_retry_count`; next poll retries. After
           `_CANCEL_MAX_RETRIES` attempts, give up and mark
           `be_already_moved=True` so poll stops spinning — live TP2
           algo state is then unknown, operator intervenes.
        3. Place fails after cancel already landed → the surviving leg is
           now *unprotected*. Mark `be_already_moved=True`, drop TP2
           from `algo_ids`, log CRITICAL. Emergency market-close is
           intentionally NOT automated — a winning TP1 runner without a
           stop is better handled by a human than a blind exit."""
        if not self.move_sl_to_be_enabled:
            return
        if t.be_already_moved:
            return
        if t.initial_size <= 0 or len(t.algo_ids) < 2:
            return
        # Only react when size strictly shrank AND something is still open.
        if snap.size >= t.initial_size or snap.size <= 0:
            return
        if t.tp2_price is None:
            return

        tp2_algo_id = t.algo_ids[1]
        sign = 1 if t.pos_side == "long" else -1
        be_price = t.entry_price + (t.entry_price * self.sl_be_offset_pct * sign)

        # Step 1: cancel TP2 (tolerant of already-gone, but verify 51400 —
        # OKX demo has been seen returning 51400 on a still-live algo,
        # and placing a replacement then leaves TWO stops on the book).
        cancel_succeeded_or_idempotent = False
        try:
            self.client.cancel_algo(t.inst_id, tp2_algo_id)
            cancel_succeeded_or_idempotent = True
        except OrderRejected as exc:
            if self._cancel_error_is_already_gone(exc):
                if self._verify_algo_gone(t.inst_id, tp2_algo_id):
                    logger.warning(
                        "sl_to_be_tp2_already_gone inst={} side={} algo_id={} "
                        "code={} verified=true — proceeding with BE placement",
                        t.inst_id, t.pos_side, tp2_algo_id, exc.code,
                    )
                    cancel_succeeded_or_idempotent = True
                else:
                    t.cancel_retry_count += 1
                    logger.warning(
                        "sl_to_be_cancel_51400_but_still_live inst={} side={} "
                        "algo_id={} — not placing replacement (avoid double-SL), "
                        "retry next poll attempt={}/{}",
                        t.inst_id, t.pos_side, tp2_algo_id,
                        t.cancel_retry_count, _CANCEL_MAX_RETRIES,
                    )
            else:
                t.cancel_retry_count += 1
                logger.exception(
                    "sl_to_be_cancel_retry_next_poll inst={} side={} code={} "
                    "attempt={}/{}",
                    t.inst_id, t.pos_side, exc.code,
                    t.cancel_retry_count, _CANCEL_MAX_RETRIES,
                )
        except Exception:
            t.cancel_retry_count += 1
            logger.exception(
                "sl_to_be_cancel_retry_next_poll inst={} side={} attempt={}/{}",
                t.inst_id, t.pos_side,
                t.cancel_retry_count, _CANCEL_MAX_RETRIES,
            )

        if not cancel_succeeded_or_idempotent:
            if t.cancel_retry_count >= _CANCEL_MAX_RETRIES:
                # Backstop: we can't tell if the algo is truly gone or if
                # OKX is returning malformed errors. Either way, stop
                # spinning and surface the state. The TP2 algo *might*
                # still be live on OKX — but after N failed cancels we
                # can't confirm, and the original SL leg from the TP1
                # OCO is already effected (state=effective, sz=partial),
                # so the runner's real risk is that the live TP2 algo
                # may still trigger on its own. Operator decides.
                logger.critical(
                    "sl_to_be_cancel_gave_up inst={} side={} algo_id={} "
                    "after {} attempts — not moving SL to BE; live TP2 "
                    "algo state unknown, manual intervention required",
                    t.inst_id, t.pos_side, tp2_algo_id, t.cancel_retry_count,
                )
                t.be_already_moved = True
            return

        # Step 2: place the BE replacement. Cancel has already landed, so
        # the surviving leg is unprotected until this returns.
        try:
            new_algo = self.client.place_oco_algo(
                inst_id=t.inst_id, pos_side=t.pos_side,
                size_contracts=int(snap.size),
                sl_trigger_px=be_price,
                tp_trigger_px=t.tp2_price,
                td_mode=self.margin_mode,
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.critical(
                "sl_to_be_place_failed_position_unprotected inst={} side={} "
                "remaining_size={} be_price={} err={!r} code={} payload={} — "
                "TP2 cancelled, replacement OCO rejected, manual intervention required",
                t.inst_id, t.pos_side, snap.size, be_price, exc, code, payload,
            )
            # Prevent retry-spin: cancel already succeeded, so a retry
            # would just "cancel a vanished algo" forever. Drop TP2 from
            # algo_ids so the journal reflects that only TP1 remains.
            t.algo_ids = [t.algo_ids[0]]
            t.be_already_moved = True
            if self._on_sl_moved is not None:
                try:
                    self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
                except Exception:
                    logger.exception("on_sl_moved_callback_failed")
            return

        t.algo_ids = [t.algo_ids[0], new_algo.algo_id]
        t.be_already_moved = True
        # Keep dynamic-TP bookkeeping in sync: the runner OCO is now this
        # replacement; revise_runner_tp will read sl_price + runner_size.
        t.sl_price = be_price
        t.runner_size = int(snap.size)
        logger.info(
            "sl_moved_to_be_via_replace inst={} side={} remaining_size={} "
            "be_price={} offset_pct={} new_algo={}",
            t.inst_id, t.pos_side, snap.size,
            be_price, self.sl_be_offset_pct, new_algo.algo_id,
        )
        if self._on_sl_moved is not None:
            try:
                self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
            except Exception:
                logger.exception("on_sl_moved_callback_failed")

    def revise_runner_tp(
        self, inst_id: str, pos_side: str, new_tp: float,
        *, now: Optional[datetime] = None,
    ) -> bool:
        """Cancel the runner OCO and place a replacement with `new_tp`.

        Keeps the active SL untouched (`t.sl_price`, which is plan SL pre-BE
        and the BE-adjusted price post-BE). Used by the dynamic-TP loop in
        the runner: when live conditions move the 1:N target away from the
        original placement, we re-OCO instead of leaving stale TP geometry
        on the book. Returns True on success.

        Failure modes mirror `_detect_tp1_and_move_sl`:
          * cancel returns idempotent-gone code → proceed to place.
          * cancel returns hard error → abort, runner OCO untouched.
          * place fails after cancel → CRITICAL log, runner UNPROTECTED,
            algo_ids trimmed, returns False. No emergency market-close —
            same operator-decides discipline as the BE replacement.
        """
        key = (inst_id, pos_side)
        t = self._tracked.get(key)
        if t is None or not t.algo_ids:
            return False
        if t.tp2_price is not None and abs(t.tp2_price - new_tp) < 1e-9:
            return False
        if t.runner_size <= 0:
            return False

        runner_algo_id = t.algo_ids[-1]

        try:
            self.client.cancel_algo(t.inst_id, runner_algo_id)
        except OrderRejected as exc:
            if not self._cancel_error_is_already_gone(exc):
                logger.warning(
                    "tp_revise_cancel_failed inst={} side={} algo_id={} "
                    "code={} — abort, runner OCO untouched",
                    t.inst_id, t.pos_side, runner_algo_id, exc.code,
                )
                return False
            # Verify 51400 before placing — demo can lie about algo state.
            if not self._verify_algo_gone(t.inst_id, runner_algo_id):
                logger.warning(
                    "tp_revise_cancel_51400_but_still_live inst={} side={} "
                    "algo_id={} — abort to avoid double-OCO, try next cycle",
                    t.inst_id, t.pos_side, runner_algo_id,
                )
                return False
            logger.warning(
                "tp_revise_runner_already_gone inst={} side={} algo_id={} "
                "code={} verified=true — proceeding with replacement",
                t.inst_id, t.pos_side, runner_algo_id, exc.code,
            )
        except Exception:
            logger.exception(
                "tp_revise_cancel_exception inst={} side={} algo_id={}",
                t.inst_id, t.pos_side, runner_algo_id,
            )
            return False

        try:
            new_algo = self.client.place_oco_algo(
                inst_id=t.inst_id, pos_side=t.pos_side,
                size_contracts=int(t.runner_size),
                sl_trigger_px=t.sl_price,
                tp_trigger_px=new_tp,
                td_mode=self.margin_mode,
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.critical(
                "tp_revise_place_failed_position_unprotected inst={} side={} "
                "size={} sl={} new_tp={} err={!r} code={} payload={} — "
                "runner unprotected, manual intervention",
                t.inst_id, t.pos_side, t.runner_size, t.sl_price, new_tp,
                exc, code, payload,
            )
            t.algo_ids = t.algo_ids[:-1]
            return False

        t.algo_ids = t.algo_ids[:-1] + [new_algo.algo_id]
        t.tp2_price = new_tp
        t.last_tp_revise_at = now or _utc_now()
        # 2026-04-20 — if a resting TP limit was co-placed with the old OCO,
        # move it to the new TP price in lockstep. Leave stale on failure
        # rather than double-canceling: old limit still protects at the
        # prior level; the new OCO's market-on-trigger is the safety net.
        if t.tp_limit_order_id:
            old_tp_ord = t.tp_limit_order_id
            self._cancel_tp_limit_best_effort(t.inst_id, old_tp_ord)
            t.tp_limit_order_id = ""
            try:
                new_tp_ord = self.client.place_reduce_only_limit(
                    inst_id=t.inst_id, pos_side=t.pos_side,
                    size_contracts=int(t.runner_size),
                    px=new_tp, td_mode=self.margin_mode,
                    post_only=True,
                )
                t.tp_limit_order_id = new_tp_ord.order_id
                logger.info(
                    "tp_limit_replaced inst={} side={} new_tp={} "
                    "old_ord={} new_ord={}",
                    t.inst_id, t.pos_side, new_tp,
                    old_tp_ord, new_tp_ord.order_id,
                )
            except Exception:
                logger.exception(
                    "tp_limit_replace_failed inst={} side={} new_tp={} — "
                    "OCO market-TP still protects, maker fill lost this cycle",
                    t.inst_id, t.pos_side, new_tp,
                )
        logger.info(
            "tp_revised inst={} side={} new_tp={} sl={} new_algo={}",
            t.inst_id, t.pos_side, new_tp, t.sl_price, new_algo.algo_id,
        )
        # Persist the new runner algo id to the journal so a restart's rehydrate
        # reads the *live* algo, not the pre-revise one. Without this, the next
        # rehydrate reads a stale id and the next revise cancels a ghost while
        # the actually-live replacement keeps running → orphan OCO on OKX.
        # (2026-04-20 DOGE 2-OCO postmortem.)
        if self._on_sl_moved is not None:
            try:
                self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
            except Exception:
                logger.exception("on_sl_moved_callback_failed_after_tp_revise")
        return True

    def get_tracked_runner(
        self, inst_id: str, pos_side: str,
    ) -> Optional[dict]:
        """Snapshot of runner-OCO state for the dynamic-TP gate. Returns
        None when the (inst_id, pos_side) has no live tracked position.
        Read-only — no mutation."""
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return None
        return {
            "entry_price": t.entry_price,
            "sl_price": t.sl_price,
            # 0.0 signals "plan_sl unknown" (post-BE rehydrate) — runner
            # disables dynamic-TP revise when this is not positive.
            "plan_sl_price": t.plan_sl_price,
            "tp2_price": t.tp2_price,
            "runner_size": t.runner_size,
            "be_already_moved": t.be_already_moved,
            "last_tp_revise_at": t.last_tp_revise_at,
            "sl_lock_applied": t.sl_lock_applied,
        }

    def lock_sl_at(
        self, inst_id: str, pos_side: str, new_sl: float,
    ) -> bool:
        """Cancel the runner OCO and place a replacement with `new_sl`
        (keeping the current TP). Used by the MFE-lock gate in the runner:
        once the trade is far enough in favor, the old SL below entry is
        no longer protecting risk — it's protecting a loss that shouldn't
        happen. This pulls the SL up to BE / profit-lock.

        One-shot: `sl_lock_applied` is set to True on success so repeated
        MFE ticks can't re-trigger (subsequent tightening needs a real
        trail, out of scope here).

        Failure modes mirror `revise_runner_tp`:
          * cancel returns idempotent-gone code → proceed (verified).
          * cancel returns hard error → abort, OCO untouched.
          * place fails after cancel → CRITICAL log, UNPROTECTED, return False.
        """
        key = (inst_id, pos_side)
        t = self._tracked.get(key)
        if t is None or not t.algo_ids:
            return False
        if t.sl_lock_applied:
            return False
        if t.runner_size <= 0:
            return False
        if t.tp2_price is None or t.tp2_price <= 0:
            return False
        # Guard against degenerate placements: new SL must still be on the
        # protective side of entry. A long's new SL must be ≤ entry for
        # "risk-free / locked-profit" to make sense; a short's new SL must
        # be ≥ entry. Otherwise we'd be tightening into a worse-than-old
        # stop, which defeats the point.
        if t.pos_side == "long" and new_sl >= t.tp2_price:
            return False
        if t.pos_side == "short" and new_sl <= t.tp2_price:
            return False

        runner_algo_id = t.algo_ids[-1]

        try:
            self.client.cancel_algo(t.inst_id, runner_algo_id)
        except OrderRejected as exc:
            if not self._cancel_error_is_already_gone(exc):
                logger.warning(
                    "sl_lock_cancel_failed inst={} side={} algo_id={} "
                    "code={} — abort, runner OCO untouched",
                    t.inst_id, t.pos_side, runner_algo_id, exc.code,
                )
                return False
            if not self._verify_algo_gone(t.inst_id, runner_algo_id):
                logger.warning(
                    "sl_lock_cancel_51400_but_still_live inst={} side={} "
                    "algo_id={} — abort to avoid double-OCO, try next cycle",
                    t.inst_id, t.pos_side, runner_algo_id,
                )
                return False
            logger.warning(
                "sl_lock_runner_already_gone inst={} side={} algo_id={} "
                "code={} verified=true — proceeding with replacement",
                t.inst_id, t.pos_side, runner_algo_id, exc.code,
            )
        except Exception:
            logger.exception(
                "sl_lock_cancel_exception inst={} side={} algo_id={}",
                t.inst_id, t.pos_side, runner_algo_id,
            )
            return False

        try:
            new_algo = self.client.place_oco_algo(
                inst_id=t.inst_id, pos_side=t.pos_side,
                size_contracts=int(t.runner_size),
                sl_trigger_px=new_sl,
                tp_trigger_px=t.tp2_price,
                td_mode=self.margin_mode,
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.critical(
                "sl_lock_place_failed_position_unprotected inst={} side={} "
                "size={} new_sl={} tp={} err={!r} code={} payload={} — "
                "runner unprotected, manual intervention",
                t.inst_id, t.pos_side, t.runner_size, new_sl, t.tp2_price,
                exc, code, payload,
            )
            t.algo_ids = t.algo_ids[:-1]
            # Mark as applied so we don't loop on another failed cancel+place.
            t.sl_lock_applied = True
            return False

        t.algo_ids = t.algo_ids[:-1] + [new_algo.algo_id]
        t.sl_price = new_sl
        t.sl_lock_applied = True
        logger.info(
            "sl_locked_via_replace inst={} side={} size={} new_sl={} "
            "tp={} new_algo={}",
            t.inst_id, t.pos_side, t.runner_size, new_sl, t.tp2_price,
            new_algo.algo_id,
        )
        if self._on_sl_moved is not None:
            try:
                self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
            except Exception:
                logger.exception("on_sl_moved_callback_failed")
        return True

    @staticmethod
    def _cancel_error_is_already_gone(exc: OrderRejected) -> bool:
        # Known OKX "algo doesn't exist / already cancelled / already
        # filled" codes. Anything else (including empty/None code —
        # generally a wrapping issue, not a semantic 'gone' signal) falls
        # through to the retry path, where the generic retry counter will
        # eventually give up if it truly cannot recover.
        return exc.code in _ALGO_GONE_CODES

    def _verify_algo_gone(self, inst_id: str, algo_id: str) -> bool:
        """Double-check 51400 against the live pending-algos list.

        OKX demo has been seen returning 51400 ("algo does not exist") on
        a cancel while the algo is still on the book — placing a
        replacement OCO then leaves TWO stops on the position, both of
        which fire back-to-back at the next adverse wick. This helper
        queries the live algos and only returns True when the specific
        algoId is truly absent. Network / API failure returns False (be
        conservative — do not proceed to place).
        """
        try:
            pending = self.client.list_pending_algos(inst_id=inst_id)
        except Exception:
            logger.exception(
                "algo_verify_list_failed inst={} algo_id={} — "
                "treating as NOT-gone (conservative)",
                inst_id, algo_id,
            )
            return False
        for row in pending:
            if str(row.get("algoId", "")) == str(algo_id):
                return False
        return True

    def _cancel_tp_limit_best_effort(self, inst_id: str, order_id: str) -> None:
        """Cancel a resting TP limit. Tolerates all non-fatal paths:

        * Already filled / already canceled (`_ALGO_GONE_CODES` family) —
          log-only, that's the expected terminal state on a normal close.
        * Any other exception — log it; the resting limit is reduce-only,
          so in the worst case it sits on the book until the next startup
          `_cancel_orphan_pending_limits` sweep catches it.
        """
        if not order_id:
            return
        try:
            self.client.cancel_order(inst_id, order_id)
            logger.info(
                "tp_limit_canceled inst={} ord={}",
                inst_id, order_id,
            )
        except OrderRejected as exc:
            if self._cancel_error_is_already_gone(exc):
                logger.debug(
                    "tp_limit_cancel_already_gone inst={} ord={} code={}",
                    inst_id, order_id, exc.code,
                )
            else:
                logger.warning(
                    "tp_limit_cancel_failed inst={} ord={} code={} — "
                    "reduce-only will stay inert; next startup sweeps it",
                    inst_id, order_id, exc.code,
                )
        except Exception:
            logger.exception(
                "tp_limit_cancel_exception inst={} ord={}",
                inst_id, order_id,
            )

    def _close_fill_from(self, t: _Tracked) -> CloseFill:
        # At close time, OKX has already removed the position row. Best we
        # can do without a trade-history lookup is mark exit_price=0 and
        # pnl_usdt=0 and let the caller enrich via `get_fills` or the
        # journal (Phase 5) when it comes online.
        return CloseFill(
            inst_id=t.inst_id,
            pos_side=t.pos_side,
            entry_price=t.entry_price,
            exit_price=0.0,
            size=t.size,
            pnl_usdt=0.0,
        )

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def state(self, inst_id: str, pos_side: str) -> PositionState:
        key = (inst_id, pos_side)
        if key in self._tracked:
            return PositionState.OPEN
        if key in self._pending:
            return PositionState.PENDING
        return PositionState.CLOSED

    # ── Pending-entry lifecycle (Phase 7.C3) ────────────────────────────────

    def register_pending(
        self,
        inst_id: str,
        pos_side: str,
        order_id: str,
        *,
        num_contracts: int,
        entry_px: float,
        max_wait_s: float,
        placed_at: Optional[datetime] = None,
    ) -> None:
        """Track a limit entry that the router just placed.

        The runner holds the zone invalidation logic; this monitor only
        knows about (order_id, placed_at, max_wait_s). Timeouts are
        enforced in `poll_pending`; invalidation cancels come through
        `cancel_pending(reason="invalidated")`.
        """
        self._pending[(inst_id, pos_side)] = _PendingEntry(
            inst_id=inst_id, pos_side=pos_side, order_id=order_id,
            num_contracts=num_contracts, entry_px=entry_px,
            placed_at=placed_at or _utc_now(), max_wait_s=max_wait_s,
        )

    def poll_pending(
        self, now: Optional[datetime] = None,
    ) -> list[PendingEvent]:
        """Poll OKX for each tracked pending order and emit transitions.

        For each pending entry:
          * OKX state `filled`            → FILLED event
          * OKX state `canceled`/`mmp_canceled` → CANCELED (reason="external")
          * Still `live`/`partially_filled` beyond `max_wait_s`
              → cancel on OKX, CANCELED (reason="timeout")
          * Transient error fetching state → skip this poll, retry next

        Partial fills that age past max_wait_s are canceled but we still
        emit a FILLED event with the partial size so the runner can
        route whatever did fill into the live-position tracker.
        """
        now = now or _utc_now()
        events: list[PendingEvent] = []
        to_remove: list[tuple[str, str]] = []

        for key, p in self._pending.items():
            try:
                raw = self.client.get_order(p.inst_id, p.order_id)
            except Exception:
                logger.exception(
                    "pending_poll_get_order_failed inst={} ord={}",
                    p.inst_id, p.order_id,
                )
                continue

            state = str(raw.get("state", "")).lower()
            filled_sz = float(raw.get("accFillSz") or 0.0)
            avg_px = float(raw.get("avgPx") or 0.0)

            if state in _TERMINAL_FILLED:
                events.append(PendingEvent(
                    p.inst_id, p.pos_side, p.order_id,
                    event_type="FILLED", reason="fill",
                    filled_size=filled_sz, avg_price=avg_px,
                ))
                to_remove.append(key)
                continue

            if state in _TERMINAL_CANCELED:
                events.append(PendingEvent(
                    p.inst_id, p.pos_side, p.order_id,
                    event_type="CANCELED", reason="external",
                    filled_size=filled_sz, avg_price=avg_px,
                ))
                to_remove.append(key)
                continue

            # Still live or partially filled — check timeout.
            age_s = (now - p.placed_at).total_seconds()
            if age_s < p.max_wait_s:
                continue

            cancel_landed = False
            try:
                self.client.cancel_order(p.inst_id, p.order_id)
                cancel_landed = True
            except OrderRejected as exc:
                # 51400/1/2: order already gone. Treat as success.
                if self._cancel_error_is_already_gone(exc):
                    cancel_landed = True
                else:
                    logger.warning(
                        "pending_timeout_cancel_failed inst={} ord={} "
                        "code={} msg={} — keeping tracking, retry next poll",
                        p.inst_id, p.order_id, exc.code, str(exc),
                    )
            except Exception:
                logger.exception(
                    "pending_timeout_cancel_exception inst={} ord={} "
                    "— keeping tracking, retry next poll",
                    p.inst_id, p.order_id,
                )

            if not cancel_landed:
                # OKX rejected the cancel with a non-idempotent error (e.g.
                # sCode 50001 service-unavailable) or the call raised a
                # generic exception. Previously we emitted CANCELED and
                # dropped tracking — the order stayed live on OKX as a
                # phantom resting limit that could later fill into an
                # unprotected position. Keep the row; next poll retries.
                continue

            # If something filled before the cancel landed, surface it as
            # FILLED so the runner places an OCO on the real remainder.
            if filled_sz > 0:
                events.append(PendingEvent(
                    p.inst_id, p.pos_side, p.order_id,
                    event_type="FILLED", reason="timeout_partial_fill",
                    filled_size=filled_sz, avg_price=avg_px,
                ))
            else:
                events.append(PendingEvent(
                    p.inst_id, p.pos_side, p.order_id,
                    event_type="CANCELED", reason="timeout",
                    filled_size=0.0, avg_price=0.0,
                ))
            to_remove.append(key)

        for key in to_remove:
            self._pending.pop(key, None)
        return events

    def cancel_pending(
        self, inst_id: str, pos_side: str, *, reason: str = "manual",
    ) -> Optional[PendingEvent]:
        """Caller-driven cancel (e.g. runner detects zone invalidation).

        Returns the CANCELED event on success, or None if no pending row
        existed for that (inst_id, pos_side). Idempotent OKX errors
        (51400/1/2) are treated as success — the order is gone either way.
        Transient / non-idempotent errors (e.g. sCode 50001) re-raise to
        the caller; the pending row is kept so the caller can retry. If
        we silently popped on transient failure, the order could remain
        live on OKX as a phantom orphan (see 2026-04-20 changelog).
        """
        key = (inst_id, pos_side)
        p = self._pending.get(key)
        if p is None:
            return None
        try:
            self.client.cancel_order(p.inst_id, p.order_id)
        except OrderRejected as exc:
            if not self._cancel_error_is_already_gone(exc):
                logger.warning(
                    "pending_manual_cancel_failed inst={} ord={} code={} "
                    "msg={} reason={} — keeping tracking, re-raising",
                    p.inst_id, p.order_id, exc.code, str(exc), reason,
                )
                raise
        except Exception:
            logger.exception(
                "pending_manual_cancel_exception inst={} ord={} reason={} "
                "— keeping tracking, re-raising",
                p.inst_id, p.order_id, reason,
            )
            raise
        self._pending.pop(key, None)
        return PendingEvent(
            p.inst_id, p.pos_side, p.order_id,
            event_type="CANCELED", reason=reason,
        )
