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
from datetime import datetime, timedelta, timezone
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
    # 2026-05-02 — Phase A regime-aware exit policy. The ADX regime label
    # captured at entry-time, snapshotted here so SL/TP mutations during
    # the position's lifetime can look up regime-specific knobs (e.g.
    # `target_rr_ratio_per_regime`) without re-classifying ADX or threading
    # the value through every callback. None = unknown / pre-Phase-A
    # rehydrate row → callers fall back to the global value.
    regime_at_entry: Optional[str] = None
    # 2026-05-02 — Phase A.5 multi-step trailing SL state. Tracks the
    # highest R-level SL has been pulled to so the trailing gate can guard
    # monotonic-only updates (a mark dip must NEVER widen SL backward).
    # 0.0 = trailing has not fired yet on this position.
    last_trail_lock_r: float = 0.0
    # 2026-05-02 — Phase A.6 MAE-BE-lock state. Two-stage gate:
    #   armed:    MAE crossed threshold; waiting for recovery
    #   applied:  recovery + adverse-cycle confirmed → reduce-only post-only
    #             limit placed at entry+fee_buffer (long) or entry-fee_buffer
    #             (short). One-shot — applied=True blocks repeats.
    # `recovery_limit_order_id` = the placed limit's Bybit order id, used
    # for orphan cleanup on close.
    mae_be_lock_armed: bool = False
    mae_be_lock_applied: bool = False
    mae_be_recovery_limit_order_id: str = ""
    # 2026-05-02 — Phase A.8 cycle-on-cycle directional confluence history.
    # Each runner cycle that visits the OPEN-position leg appends the
    # score returned by `score_direction(state, position_direction)`.
    # Truncated to `weakening_max_history` (config) on append. Used by
    # `_maybe_close_on_momentum_fade` to detect declining-signal exit
    # patterns. Memory-only; rehydrated positions restart with empty
    # history — same caveat as MFE/MAE.
    recent_confluence_history: list[float] = field(default_factory=list)
    # 2026-05-02 — Phase A.10 maker-first defensive close in-flight state.
    # Set when `_defensive_close()` places a post-only reduce-only LIMIT
    # instead of a market reduce. The runner re-checks `deadline` each
    # cycle and falls back to market if the limit hasn't filled. On
    # successful maker fill, the existing close-detection path emits
    # CloseFill normally; orphan cleanup cancels the limit if SL/TP fired
    # first. Empty string / None when no maker-first close is pending.
    defensive_close_limit_order_id: str = ""
    defensive_close_deadline: Optional[datetime] = None
    # 2026-05-02 — Position open timestamp. Threaded into CloseFill so
    # `bybit_client.enrich_close_fill` can reject `/v5/position/closed-pnl`
    # rows whose `createdTime` predates this open (those are from previous
    # closes on the same symbol+side and would mis-stamp the journal).
    opened_at: datetime = field(default_factory=_utc_now)
    # 2026-05-04 — HA-native primary mode (Yol A) flag. True only when the
    # entry came from `_build_ha_native_trade_plan` (HA-native planner
    # produced the plan via OVERRIDE block); False for legacy 5-pillar
    # entries. The HA-flip exit gate (`_maybe_close_on_ha_flip`) only
    # fires for HA-native positions — legacy positions retain their
    # pre-existing exit suite (momentum_fade, MAE-BE-recovery, etc.).
    is_ha_native: bool = False
    # 2026-05-05 Phase 3a — Yol A v5 dynamic exit Layer 2 state stamp.
    # Set True when evaluate_exit returns action="WARN" (MSS direction
    # reversed against position). Layer 3 close path requires this latch
    # plus supporting MFI/RSI delta + RCS volume confirm. Persists across
    # cycles — once warned, stays warned until position closes.
    structural_warning: bool = False
    # 2026-05-05 — Yol B (HA Strategy) per-position fields. Set when the
    # entry came from `_build_vmc_trade_plan` (Yol B planner produced the
    # plan via OVERRIDE block); False for Yol A / legacy positions. The
    # VMC exit gate (`_maybe_close_on_vmc_exit`) only fires for Yol B
    # positions — Yol A positions retain HA-native 3-layer exit suite.
    is_vmc: bool = False
    # WT2 peak (LONG max) / trough (SHORT min) seen since position opened.
    # Updated each cycle by `_maybe_close_on_vmc_exit`. 0.0 sentinel = first
    # cycle, will be set to current wt2 on initial peak update. In-memory
    # only; restart sonrası 0'dan başlar (acceptable, ilk cycle peak update'i
    # current wt2'ye set eder).
    wt2_peak_during_position: float = 0.0
    # WT2 at entry — sadece audit için, exit doctrine'ında kullanılmaz.
    wt2_at_entry: float = 0.0
    # 15m hold-extension counter — `evaluate_exit` WARN action her cycle
    # ++. Threshold (`hold_extension_max_cycles`, default 2) aşılınca
    # CLOSE'a düşer. Restart sonrası 0'dan başlar.
    hold_extension_count: int = 0


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
        regime_at_entry: Optional[str] = None,
        opened_at: Optional[datetime] = None,
        is_ha_native: bool = False,
        is_vmc: bool = False,
        wt2_at_entry: float = 0.0,
    ) -> None:
        # plan_sl_price semantics: None → caller didn't provide one, default to
        # sl_price (correct at fill time). An explicit 0.0 → "unknown, disable
        # dynamic-TP revise" (rehydrate path for post-BE positions).
        resolved_plan_sl = sl_price if plan_sl_price is None else plan_sl_price
        resolved_opened_at = opened_at if opened_at is not None else _utc_now()
        self._tracked[(inst_id, pos_side)] = _Tracked(
            inst_id=inst_id, pos_side=pos_side, size=size, entry_price=entry_price,
            initial_size=size, algo_ids=list(algo_ids or []), tp2_price=tp2_price,
            be_already_moved=be_already_moved,
            sl_price=sl_price, runner_size=runner_size or int(size),
            plan_sl_price=resolved_plan_sl,
            tp_limit_order_id=tp_limit_order_id,
            regime_at_entry=regime_at_entry,
            opened_at=resolved_opened_at,
            is_ha_native=is_ha_native,
            is_vmc=is_vmc,
            wt2_at_entry=wt2_at_entry,
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
                # 2026-05-02 — Phase A.6 MAE-BE-lock leaves a reduce-only
                # post-only limit resting at entry+fee. If the position
                # closed via something else (SL fire, manual close, TP
                # limit fill), that BE-recovery limit is now an orphan —
                # cancel best-effort. Reuses `_cancel_tp_limit_best_effort`
                # since both are reduce-only limits cancelled by orderId.
                if tracked.mae_be_recovery_limit_order_id:
                    self._cancel_tp_limit_best_effort(
                        tracked.inst_id,
                        tracked.mae_be_recovery_limit_order_id,
                    )
                # 2026-05-02 — Phase A.10 maker-first defensive close. If
                # the position closed via another path (SL hit, TP limit
                # fill, manual close, etc.) while a defensive maker LIMIT
                # was still resting, cancel it best-effort. The limit was
                # reduce-only so it stays inert post-close, but leaving
                # it behind trips the next startup's orphan-pending-limit
                # sweep.
                if tracked.defensive_close_limit_order_id:
                    self._cancel_tp_limit_best_effort(
                        tracked.inst_id,
                        tracked.defensive_close_limit_order_id,
                    )
                # On Bybit V5 the position-attached TP/SL clears
                # automatically when the position size hits zero — no
                # algo-orphan sweep needed.
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
            # 2026-05-02 — Phase A regime-aware exit policy. Lets the
            # dynamic-TP gate look up `target_rr_ratio_per_regime` via
            # `cfg.execution.effective_target_rr_ratio(regime)`.
            "regime_at_entry": t.regime_at_entry,
            "last_trail_lock_r": t.last_trail_lock_r,
            "mfe_r_high": t.mfe_r_high,
            "mae_r_low": t.mae_r_low,
            "mae_be_lock_armed": t.mae_be_lock_armed,
            "mae_be_lock_applied": t.mae_be_lock_applied,
            # Tuple to give the runner an immutable snapshot — the gate
            # mustn't mutate the canonical _Tracked list via this view.
            "recent_confluence_history": tuple(t.recent_confluence_history),
            # 2026-05-05 Phase 2 — Yol A v5 dynamic-exit doctrine. Dynamic
            # TP revision must skip HA-native positions (plan.tp_price=0
            # sentinel; exit driven by HA flip + momentum fade + MFE-lock +
            # trailing + MAE-BE recovery + defensive close). Legacy 5-pillar
            # positions still get the regime-aware fixed-RR TP revise.
            "is_ha_native": t.is_ha_native,
            # 2026-05-05 — Yol B (HA Strategy) snapshot fields. Dynamic-TP
            # revise also skipped when is_vmc=True (Yol B has no fixed TP).
            "is_vmc": t.is_vmc,
            "wt2_peak_during_position": t.wt2_peak_during_position,
            "wt2_at_entry": t.wt2_at_entry,
            "hold_extension_count": t.hold_extension_count,
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

    def append_confluence_score(
        self, inst_id: str, pos_side: str, score: float, max_history: int,
    ) -> bool:
        """Append a directional confluence score to the position's history
        (Phase A.8, 2026-05-02). Truncates to `max_history` from the front
        on overflow so the deque keeps the most recent N scores.

        Returns True on append, False when the position isn't tracked
        (closed mid-cycle, etc.) or `max_history <= 0`.
        """
        if max_history <= 0:
            return False
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return False
        t.recent_confluence_history.append(float(score))
        # Truncate from the front to keep last `max_history` entries.
        overflow = len(t.recent_confluence_history) - max_history
        if overflow > 0:
            del t.recent_confluence_history[:overflow]
        return True

    def arm_mae_be_lock(self, inst_id: str, pos_side: str) -> bool:
        """Set the MAE-BE-lock armed flag (Phase A.6, 2026-05-02).

        Two-stage gate: this is stage 1 (MAE crossed threshold). Stage 2
        is `place_be_recovery_limit` (recovery + adverse cycle). Idempotent
        — calling repeatedly while already armed is a no-op.
        """
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return False
        if t.mae_be_lock_applied:
            return False  # already past stage 2; arming is moot
        t.mae_be_lock_armed = True
        return True

    def place_be_recovery_limit(
        self,
        inst_id: str,
        pos_side: str,
        limit_px: float,
        margin_mode: str = "cross",
    ) -> Optional[str]:
        """Place a reduce-only post-only LIMIT at the BE-recovery price
        (Phase A.6 stage 2, 2026-05-02).

        Operator-described mechanic: when MAE went deep then recovered AND
        the cycle's LTF direction is still adverse, place a maker exit at
        a fee-positive level (slightly above entry for long, slightly below
        for short). When mark touches that level the limit fills as a
        maker, position closes at micro-profit covering the round-trip
        taker on entry. The position-attached SL at -1R stays as backup.

        One-shot per position. On success records the limit's order id on
        the tracked row so the close path can clean it up if the SL fires
        first.
        """
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return None
        if t.mae_be_lock_applied:
            return None
        if t.runner_size <= 0:
            return None
        try:
            res = self.client.place_reduce_only_limit(
                inst_id=t.inst_id,
                pos_side=t.pos_side,
                size_contracts=int(t.runner_size),
                px=limit_px,
                td_mode=margin_mode,
                post_only=True,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.warning(
                "mae_be_lock_limit_place_failed inst={} side={} px={} "
                "size={} err={!r} code={} payload={} — position-attached "
                "SL still protects, will retry next cycle",
                t.inst_id, t.pos_side, limit_px, t.runner_size,
                exc, code, payload,
            )
            return None

        t.mae_be_lock_applied = True
        t.mae_be_recovery_limit_order_id = res.order_id
        logger.info(
            "mae_be_lock_limit_placed inst={} side={} px={} size={} ord={}",
            t.inst_id, t.pos_side, limit_px, t.runner_size, res.order_id,
        )
        return res.order_id

    def place_defensive_close_maker_limit(
        self,
        inst_id: str,
        pos_side: str,
        limit_px: float,
        timeout_s: int,
        margin_mode: str = "cross",
    ) -> Optional[str]:
        """Place a reduce-only post-only LIMIT to close the position as maker
        (Phase A.10, 2026-05-02).

        Used by `_defensive_close()` to capture maker fee on momentum_fade /
        ltf_reversal exits instead of paying taker via market reduce. The
        runner sets `defensive_close_deadline` to `now + timeout_s`; if the
        limit hasn't filled by then a per-cycle finalisation step cancels
        the limit and falls back to `close_position()` market.

        Returns the order_id on success, or None on placement failure
        (post-only would-cross / Bybit reject / position untracked). On
        None, the caller MUST fall back to market close — the defensive
        intent is to exit ASAP, so no retries here.
        """
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return None
        if t.runner_size <= 0:
            return None
        if t.defensive_close_limit_order_id:
            # Already in flight — caller raced, idempotent no-op.
            return t.defensive_close_limit_order_id
        try:
            res = self.client.place_reduce_only_limit(
                inst_id=t.inst_id,
                pos_side=t.pos_side,
                size_contracts=int(t.runner_size),
                px=limit_px,
                td_mode=margin_mode,
                post_only=True,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            payload = getattr(exc, "payload", None)
            logger.warning(
                "defensive_close_maker_limit_place_failed inst={} side={} "
                "px={} size={} err={!r} code={} payload={} — caller will "
                "fall back to market reduce",
                t.inst_id, t.pos_side, limit_px, t.runner_size,
                exc, code, payload,
            )
            return None

        t.defensive_close_limit_order_id = res.order_id
        t.defensive_close_deadline = _utc_now() + timedelta(seconds=timeout_s)
        logger.info(
            "defensive_close_maker_limit_placed inst={} side={} px={} "
            "size={} ord={} deadline_s={}",
            t.inst_id, t.pos_side, limit_px, t.runner_size, res.order_id,
            timeout_s,
        )
        return res.order_id

    def iter_expired_defensive_close_limits(
        self, now: Optional[datetime] = None,
    ) -> list[tuple[str, str, str]]:
        """Yield `(inst_id, pos_side, order_id)` triples for tracked
        positions whose maker defensive-close LIMIT has passed its
        deadline without filling.

        Caller (runner) cancels the limit + fires `close_position()` market,
        then calls `clear_defensive_close_state()` to reset tracking.
        """
        cutoff = now or _utc_now()
        expired: list[tuple[str, str, str]] = []
        for (inst_id, pos_side), t in self._tracked.items():
            if not t.defensive_close_limit_order_id:
                continue
            if t.defensive_close_deadline is None:
                continue
            if cutoff >= t.defensive_close_deadline:
                expired.append((inst_id, pos_side, t.defensive_close_limit_order_id))
        return expired

    def clear_defensive_close_state(
        self, inst_id: str, pos_side: str,
    ) -> None:
        """Reset the maker defensive-close in-flight fields after the
        runner has cancelled the limit and fired the market fallback (or
        the limit filled and close-detection emitted CloseFill)."""
        t = self._tracked.get((inst_id, pos_side))
        if t is None:
            return
        t.defensive_close_limit_order_id = ""
        t.defensive_close_deadline = None

    def trail_sl_to(
        self, inst_id: str, pos_side: str, new_sl: float, lock_r: float,
    ) -> bool:
        """Multi-step trailing-SL update (Phase A.5, 2026-05-02).

        Distinct from `lock_sl_at` (BE-lock one-shot): this is called
        repeatedly as MFE keeps growing, each call pulling SL forward by
        `trail_step_r`. Monotonic-only — the new SL must be more
        conservative (higher for long, lower for short) than the current
        cached SL, otherwise this is a no-op.

        Returns True when trading-stop was successfully updated;
        False when the call was skipped (degenerate state, monotonic
        violation) or rejected (Bybit error). On rejection the cached
        SL stays at its prior value and `last_trail_lock_r` is NOT
        bumped, so the next cycle retries.
        """
        key = (inst_id, pos_side)
        t = self._tracked.get(key)
        if t is None:
            return False
        if t.runner_size <= 0:
            return False
        if t.tp2_price is None or t.tp2_price <= 0:
            return False
        # Same TP-side guard as lock_sl_at — never tighten past TP.
        if t.pos_side == "long" and new_sl >= t.tp2_price:
            return False
        if t.pos_side == "short" and new_sl <= t.tp2_price:
            return False
        # Monotonic SL guard — must be MORE protective than current SL.
        cur_sl = float(t.sl_price or 0.0)
        if cur_sl > 0:
            if t.pos_side == "long" and new_sl <= cur_sl:
                return False
            if t.pos_side == "short" and new_sl >= cur_sl:
                return False
        # Monotonic R-level guard (caller passes the proposed lock_r;
        # we re-check here as a defensive belt against drift).
        if lock_r <= t.last_trail_lock_r:
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
                "sl_trail_trading_stop_failed inst={} side={} new_sl={} "
                "lock_r={} tp={} err={!r} code={} payload={} — keeping "
                "previous SL, will retry next cycle",
                t.inst_id, t.pos_side, new_sl, lock_r, t.tp2_price,
                exc, code, payload,
            )
            return False

        t.sl_price = new_sl
        t.last_trail_lock_r = lock_r
        logger.info(
            "sl_trailed inst={} side={} size={} new_sl={} lock_r={} tp={}",
            t.inst_id, t.pos_side, t.runner_size, new_sl, lock_r, t.tp2_price,
        )
        return True

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

        * Already filled / already canceled (`_ORDER_GONE_CODES` family) —
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
        # At close time, Bybit has already removed the position row. Best we
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
            opened_at=t.opened_at,
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

            # Bybit V5: `orderStatus` field, CamelCase. Accept the pre-migration
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

        2026-04-27 (F6) — `_ORDER_GONE_CODES` covers BOTH "already
        cancelled" and "already filled" on Bybit V5. If the order
        actually filled between our cancel-cmd dispatch and Bybit's
        rejection, blindly accepting the idempotent code as "cancel
        success" loses the fill event (the position stays live without
        SL/TP attached, see SOL short phantom-cancel 2026-04-27 changelog
        entry). Now we verify the real terminal state via `get_order`
        before deciding event type.
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
            # 2026-04-27 (F6) — order is "gone" per Bybit, but that could
            # mean either Cancelled OR Filled. Verify the real terminal
            # state to avoid silent fill loss.
            verified = self._verify_cancel_terminal_state(p, reason)
            if verified is not None:
                self._pending.pop(key, None)
                return verified
            # get_order itself failed — fall through to legacy behavior
            # (assume cancel landed, log a warning so this case is visible)
            logger.warning(
                "pending_manual_cancel_unverified inst={} ord={} reason={} "
                "— treating as CANCELED but real state unknown",
                p.inst_id, p.order_id, reason,
            )
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

    def _verify_cancel_terminal_state(
        self, p: "_PendingEntry", cancel_reason: str,
    ) -> Optional[PendingEvent]:
        """Inspect the real order status when Bybit returned an
        `_ORDER_GONE_CODES` error from a cancel attempt. Three outcomes:

        - status `Filled` (or partial fill that completed) → return a
          FILLED event so the caller routes through the fill flow
          (record_open + position-attached TP/SL). Logs
          `phantom_cancel_detected` so audits can spot the race.
        - status `Cancelled` / `Rejected` / `Deactivated` → return a
          CANCELED event with the caller's reason (legacy behaviour).
        - get_order fails or returns an inconclusive status → return
          None; the caller falls back to assuming cancel landed (legacy
          best-effort).
        """
        try:
            raw = self.client.get_order(p.inst_id, p.order_id)
        except Exception:
            logger.exception(
                "cancel_verify_get_order_failed inst={} ord={} reason={}",
                p.inst_id, p.order_id, cancel_reason,
            )
            return None
        state = str(raw.get("orderStatus") or raw.get("state") or "").lower()
        filled_sz = float(raw.get("cumExecQty") or raw.get("accFillSz") or 0.0)
        avg_px = float(raw.get("avgPrice") or raw.get("avgPx") or 0.0)
        if state in _TERMINAL_FILLED:
            logger.warning(
                "phantom_cancel_detected inst={} ord={} actual_status=Filled "
                "filled_sz={} avg_px={} cancel_reason={} — routing to FILLED",
                p.inst_id, p.order_id, filled_sz, avg_px, cancel_reason,
            )
            return PendingEvent(
                p.inst_id, p.pos_side, p.order_id,
                event_type="FILLED", reason="phantom_cancel_recovery",
                filled_size=filled_sz, avg_price=avg_px,
            )
        if state in _TERMINAL_CANCELED:
            return PendingEvent(
                p.inst_id, p.pos_side, p.order_id,
                event_type="CANCELED", reason=cancel_reason,
            )
        # Inconclusive state (e.g. New, PartiallyFilled while Bybit said
        # "gone") — let the caller fall back to its default.
        logger.warning(
            "cancel_verify_inconclusive inst={} ord={} status={} reason={}",
            p.inst_id, p.order_id, state, cancel_reason,
        )
        return None
