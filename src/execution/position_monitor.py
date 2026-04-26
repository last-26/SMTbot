"""Position polling → close-fill events.

The OrderRouter places orders and walks away. The PositionMonitor watches
Bybit for the effect: entry fills, SL/TP hits, liquidations. It emits:

  - on open→close transition: a CloseFill record, which the caller feeds
    into RiskManager.register_trade_closed().
  - on MFE / TP1 / dynamic-TP triggers: mutates the position-attached
    TP/SL via /v5/position/trading-stop (single atomic call — no
    cancel+place dance, since TP/SL is part of the position itself
    on Bybit V5, not a separate algo order).

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

from src.execution.bybit_client import BybitClient
from src.execution.errors import OrderRejected
from src.execution.models import CloseFill, PositionSnapshot, PositionState


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# Bybit V5 error codes where cancel_order can be treated as already-done:
# 110001 = order does not exist, 110008 = order has been completed or
# cancelled, 110010 = order has been cancelled, 170142/170213 = order
# does not exist (variant codes for spot/linear). If a pending limit
# vanished between polls (filled, prior-poll cancel raced, operator
# manual cancel), we treat the cancel as idempotent success.
_ORDER_GONE_CODES = frozenset({"110001", "110008", "110010", "170142", "170213"})

# Legacy alias kept while the rest of the codebase migrates off the OKX-era
# name. Both names point at the same set on Bybit.
_ALGO_GONE_CODES = _ORDER_GONE_CODES

# Backstop: if cancel keeps failing with an unknown code, stop after this
# many polls and surface the position as unprotected. Prevents the
# retry-spin pathology even when `_ORDER_GONE_CODES` doesn't match the
# actual error code.
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
    # 1:N target becomes unreachably close to mark (Bybit rejects with 110012).
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
    # 2026-04-26 — running MFE / MAE in R units, updated on every poll
    # (every 5s) so peaks aren't missed between cadence-gated journal
    # snapshot writes (default 5min). Memory-only; rehydrated positions
    # restart from 0/0 — see CLAUDE.md restart caveat. Sign convention:
    # mfe_r_high is the most-positive R reached (peak favourable),
    # mae_r_low is the most-negative R reached (deepest adverse).
    mfe_r_high: float = 0.0
    mae_r_low: float = 0.0


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


# Bybit V5 `orderStatus` values (CamelCase). We compare lowercased so the
# reader is tolerant of any case mishaps in mocks / SDK quirks. Filled is
# the only success terminal state; Cancelled / Rejected / Deactivated are
# all terminal-cancel states that the runner should surface as such.
# `canceled` (American spelling) and `mmp_canceled` are kept for
# back-compat with pre-migration test fixtures — the live Bybit API
# never emits these, but tests that haven't been touched still work.
_TERMINAL_FILLED = frozenset({"filled"})
_TERMINAL_CANCELED = frozenset({
    "cancelled", "rejected", "deactivated",
    "canceled", "mmp_canceled",
})


class PositionMonitor:
    """Tracks open positions and emits CloseFill events on closure."""

    def __init__(
        self,
        client: BybitClient,
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

    def poll(
        self, inst_id: Optional[str] = None,
    ) -> tuple[list[CloseFill], list[PositionSnapshot]]:
        """Pull current positions from Bybit, emit fills for anything that
        closed, and return the live snapshots so the runner can write
        intra-trade journal rows without a second API call.

        As a side-effect on the way, if a partial TP1 fill is detected and
        `move_sl_to_be_enabled`, cancel the remaining TP2 algo and place a
        new SL-at-breakeven OCO for the surviving contracts.

        Also updates running `mfe_r_high` / `mae_r_low` on each tracked
        position so the cadence-gated `position_snapshots` writer sees
        peaks captured between snapshot ticks.

        Return shape: `(fills, live_snaps)`. `live_snaps` is the list of
        currently-OPEN PositionSnapshot rows for tracked positions only —
        a closed position appears in `fills` (one CloseFill) but NOT in
        `live_snaps` (it has no current state to snapshot).
        """
        live_snaps_map: dict[tuple[str, str], PositionSnapshot] = {}
        for snap in self.client.get_positions(inst_id=inst_id):
            if snap.size != 0.0:
                live_snaps_map[(snap.inst_id, snap.pos_side)] = snap

        fills: list[CloseFill] = []
        to_remove: list[tuple[str, str]] = []
        for key, tracked in self._tracked.items():
            if inst_id is not None and key[0] != inst_id:
                continue  # skip tracked positions on other instruments this poll
            if key not in live_snaps_map:
                # Tracked position no longer live → it closed.
                fills.append(self._close_fill_from(tracked))
                # Best-effort cancel the resting TP limit so it doesn't
                # linger as an orphan when the position closed via SL/TP
                # first. Reduce-only keeps it inert even if the cancel
                # races, but leaving resting orders behind trips the next
                # startup's orphan-pending-limit sweep.
                if tracked.tp_limit_order_id:
                    self._cancel_tp_limit_best_effort(
                        tracked.inst_id, tracked.tp_limit_order_id,
                    )
                # On Bybit V5 the position-attached TP/SL clears
                # automatically when the position size hits zero — no
                # algo-orphan sweep needed (compare the OKX-era
                # `_cancel_algos_best_effort` call that lived here).
                to_remove.append(key)
            else:
                snap = live_snaps_map[key]
                self._detect_tp1_and_move_sl(tracked, snap)
                self._update_excursion(tracked, snap)
                # Refresh cached entry_price/size from the live row
                tracked.size = snap.size
                if snap.entry_price > 0:
                    tracked.entry_price = snap.entry_price

        for key in to_remove:
            self._tracked.pop(key, None)

        # Live snaps for the runner snapshot writer — only positions still
        # tracked AND still in the live map (closed positions stay in
        # `fills`, gone from this list).
        live_snaps_out = [
            live_snaps_map[k] for k in self._tracked.keys()
            if k in live_snaps_map
        ]
        return fills, live_snaps_out

    def _update_excursion(
        self, t: _Tracked, snap: PositionSnapshot,
    ) -> None:
        """Update running MFE / MAE in R units on the tracked position.

        Skips silently when plan_sl_price is unset (rehydrate sentinel
        0.0) or zero-distance (fill price == plan SL, malformed).
        Direction-aware: for shorts, "favorable" means mark below entry.
        """
        if t.plan_sl_price <= 0:
            return
        sl_dist = abs(t.entry_price - t.plan_sl_price)
        if sl_dist <= 0:
            return
        sign = 1.0 if t.pos_side == "long" else -1.0
        r_now = sign * (snap.mark_price - t.entry_price) / sl_dist
        if r_now > t.mfe_r_high:
            t.mfe_r_high = r_now
        if r_now < t.mae_r_low:
            t.mae_r_low = r_now

    def _detect_tp1_and_move_sl(
        self, t: _Tracked, snap: PositionSnapshot,
    ) -> None:
        """If the live size shrank (TP1 fill) and we haven't moved SL yet,
        update the position's SL to breakeven (+ fee buffer) on the
        surviving contracts.

        On Bybit V5 this is a single atomic /v5/position/trading-stop call
        — there's no separate algo to cancel and replace. If the call
        fails the position keeps its existing (pre-BE) SL, so there's no
        "unprotected window" to worry about. We still cap retries to avoid
        spin and log CRITICAL on a hard failure so operator can intervene.
        """
        if not self.move_sl_to_be_enabled:
            return
        if t.be_already_moved:
            return
        if t.initial_size <= 0:
            return
        # Only react when size strictly shrank AND something is still open.
        if snap.size >= t.initial_size or snap.size <= 0:
            return
        if t.tp2_price is None:
            return

        sign = 1 if t.pos_side == "long" else -1
        be_price = t.entry_price + (t.entry_price * self.sl_be_offset_pct * sign)

        try:
            self.client.set_position_tpsl(
                inst_id=t.inst_id,
                pos_side=t.pos_side,
                stop_loss=be_price,
                # Leave TP unchanged — this is SL-only.
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            t.cancel_retry_count += 1
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.warning(
                "sl_to_be_trading_stop_failed inst={} side={} be_price={} "
                "err={!r} code={} payload={} attempt={}/{} — old SL still "
                "protects, retry next poll",
                t.inst_id, t.pos_side, be_price, exc, code, payload,
                t.cancel_retry_count, _CANCEL_MAX_RETRIES,
            )
            if t.cancel_retry_count >= _CANCEL_MAX_RETRIES:
                logger.critical(
                    "sl_to_be_gave_up inst={} side={} after {} attempts — "
                    "position retains pre-BE SL, manual intervention to "
                    "tighten if desired",
                    t.inst_id, t.pos_side, t.cancel_retry_count,
                )
                t.be_already_moved = True
            return

        t.be_already_moved = True
        t.sl_price = be_price
        t.runner_size = int(snap.size)
        logger.info(
            "sl_moved_to_be inst={} side={} remaining_size={} "
            "be_price={} offset_pct={}",
            t.inst_id, t.pos_side, snap.size,
            be_price, self.sl_be_offset_pct,
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
        """Update the position's TP via /v5/position/trading-stop.

        Keeps the active SL untouched (Bybit's trading-stop accepts each
        leg independently — passing only `take_profit` leaves `stop_loss`
        on the position alone). Used by the dynamic-TP loop in the runner:
        when live conditions move the 1:N target away from the original
        placement, we mutate the TP in place. Returns True on success.

        Failure mode: trading-stop call rejects → existing TP stays in
        place, return False. The position never goes unprotected because
        we never cancel before placing — Bybit handles the swap atomically.
        """
        key = (inst_id, pos_side)
        t = self._tracked.get(key)
        if t is None:
            return False
        if t.tp2_price is not None and abs(t.tp2_price - new_tp) < 1e-9:
            return False
        if t.runner_size <= 0:
            return False

        try:
            self.client.set_position_tpsl(
                inst_id=t.inst_id,
                pos_side=t.pos_side,
                take_profit=new_tp,
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.warning(
                "tp_revise_trading_stop_failed inst={} side={} new_tp={} "
                "err={!r} code={} payload={} — existing TP still protects",
                t.inst_id, t.pos_side, new_tp, exc, code, payload,
            )
            return False

        t.tp2_price = new_tp
        t.last_tp_revise_at = now or _utc_now()
        # 2026-04-20 — if a resting TP limit (maker-TP) was co-placed,
        # move it to the new TP price in lockstep. Leave stale on failure
        # rather than double-canceling: old limit still protects at the
        # prior level; the new trading-stop TP is the safety net.
        if t.tp_limit_order_id:
            old_tp_ord = t.tp_limit_order_id
            self._cancel_tp_limit_best_effort(t.inst_id, old_tp_ord)
            t.tp_limit_order_id = ""
            try:
                new_tp_ord = self.client.place_reduce_only_limit(
                    inst_id=t.inst_id, pos_side=t.pos_side,
                    size_contracts=int(t.runner_size),
                    px=new_tp, post_only=True,
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
                    "trading-stop TP still protects, maker fill lost this cycle",
                    t.inst_id, t.pos_side, new_tp,
                )
        logger.info(
            "tp_revised inst={} side={} new_tp={} sl={}",
            t.inst_id, t.pos_side, new_tp, t.sl_price,
        )
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

    def get_tracked(
        self, inst_id: str, pos_side: str,
    ) -> Optional[_Tracked]:
        """Read-only handle to the full _Tracked record for the journal
        snapshot writer. Returns None when no live position exists.

        Returns the live dataclass — caller must not mutate. Used by the
        runner's `_maybe_write_position_snapshots` to read entry_price,
        plan_sl_price, sl_price, tp2_price, mfe_r_high/mae_r_low, and
        the BE / lock flags in one shot.
        """
        return self._tracked.get((inst_id, pos_side))

    def lock_sl_at(
        self, inst_id: str, pos_side: str, new_sl: float,
    ) -> bool:
        """Update the position's SL to `new_sl` (keeping current TP).

        Used by the MFE-lock gate in the runner: once the trade is far
        enough in favor, the old SL below entry is no longer protecting
        risk — it's protecting a loss that shouldn't happen. This pulls
        the SL up to BE / profit-lock via /v5/position/trading-stop.

        One-shot: `sl_lock_applied` is set to True on success so repeated
        MFE ticks can't re-trigger (subsequent tightening needs a real
        trail, out of scope here).

        Failure mode: trading-stop call rejects → existing SL stays in
        place, return False, set `sl_lock_applied=True` so we don't loop.
        """
        key = (inst_id, pos_side)
        t = self._tracked.get(key)
        if t is None:
            return False
        if t.sl_lock_applied:
            return False
        if t.runner_size <= 0:
            return False
        if t.tp2_price is None or t.tp2_price <= 0:
            return False
        # Guard against degenerate placements: new SL must still be on the
        # protective side of TP. A long's new SL must be < TP for the lock
        # to make sense; a short's new SL must be > TP. Otherwise we'd be
        # tightening into a worse-than-old stop, which defeats the point.
        if t.pos_side == "long" and new_sl >= t.tp2_price:
            return False
        if t.pos_side == "short" and new_sl <= t.tp2_price:
            return False

        try:
            self.client.set_position_tpsl(
                inst_id=t.inst_id,
                pos_side=t.pos_side,
                stop_loss=new_sl,
                trigger_px_type=self.algo_trigger_px_type,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.warning(
                "sl_lock_trading_stop_failed inst={} side={} new_sl={} "
                "tp={} err={!r} code={} payload={} — existing SL still "
                "protects, marking lock_applied to prevent retry-spin",
                t.inst_id, t.pos_side, new_sl, t.tp2_price, exc, code, payload,
            )
            t.sl_lock_applied = True
            return False

        t.sl_price = new_sl
        t.sl_lock_applied = True
        logger.info(
            "sl_locked inst={} side={} size={} new_sl={} tp={}",
            t.inst_id, t.pos_side, t.runner_size, new_sl, t.tp2_price,
        )
        if self._on_sl_moved is not None:
            try:
                self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))
            except Exception:
                logger.exception("on_sl_moved_callback_failed")
        return True

    @staticmethod
    def _cancel_error_is_already_gone(exc: OrderRejected) -> bool:
        # Bybit "order doesn't exist / already cancelled / already filled"
        # codes (110001/110008/110010/170142/170213). Anything else falls
        # through to the retry path, where the generic retry counter will
        # eventually give up if it truly cannot recover.
        return exc.code in _ORDER_GONE_CODES

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
        """Poll Bybit for each tracked pending order and emit transitions.

        For each pending entry:
          * Bybit `orderStatus=Filled`                       → FILLED event
          * `Cancelled` / `Rejected` / `Deactivated`         → CANCELED (reason="external")
          * Still `New`/`PartiallyFilled` beyond `max_wait_s` → cancel on Bybit, CANCELED (reason="timeout")
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

            # Bybit V5: `orderStatus` field, CamelCase. Accept the OKX-era
            # `state` field too so test mocks can transition gradually.
            state = str(raw.get("orderStatus") or raw.get("state") or "").lower()
            filled_sz = float(raw.get("cumExecQty") or raw.get("accFillSz") or 0.0)
            avg_px = float(raw.get("avgPrice") or raw.get("avgPx") or 0.0)

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
                # Bybit "order gone" codes (110001/110008/110010/170142/
                # 170213): treat cancel as success.
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
                # The exchange rejected the cancel with a non-idempotent
                # error (e.g. transient service-unavailable) or the call
                # raised a generic exception. Previously we emitted CANCELED
                # and dropped tracking — the order stayed live on the
                # exchange as a phantom resting limit that could later fill
                # into an unprotected position. Keep the row; next poll
                # retries.
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
        existed for that (inst_id, pos_side). Idempotent "order gone"
        errors are treated as success — the order is gone either way.
        Transient / non-idempotent errors re-raise to the caller; the
        pending row is kept so the caller can retry. If we silently
        popped on transient failure, the order could remain live on the
        exchange as a phantom orphan (see 2026-04-20 changelog).
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
