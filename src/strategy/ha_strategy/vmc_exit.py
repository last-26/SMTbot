"""VMC exit doctrine — Yol B (HA Strategy, 2026-05-05) dynamic exit evaluator.

Operatör doctrine (özet):

  TRIGGER A — momentum drawdown:
    LONG: WT2 peak'ten %20 düşüş (config.momentum_drawdown_pct).
    SHORT: WT2 trough'tan %20 yükseliş.

  TRIGGER B — oscillator dot + HA close break + volume confirm:
    LONG: WT cross DOWN ∧ HA close < min(last 5 bar close) ∧
          body_pct_5m ≥ 30 ∧ volume_5m_ratio ≥ 1.2 → "hacimli kırmızı kapanış"
    SHORT: mirror.

  HOLD GUARD A — momentum hâlâ "havada":
    LONG: WT2 ≥ momentum_hold_zone_long (default 70) → drawdown bypass.
    SHORT: WT2 ≤ -70.
    Operatör: "momentum havadayken tutulabilir".

  HOLD GUARD B — 15m hold-extension:
    Drawdown fired ama 15m HA hâlâ aligned → max 2 cycle daha tut (WARN state).
    Sonra force exit. Operatör: "5m'de 2 kırmızı atmıştır ama 15m'de hala renk
    değişmemiştir, poz biraz daha tutulabilir".

Peak/trough tracking position lifecycle'a bağlı (runtime in-memory). Bu modül
pure function — caller (`runner._maybe_close_on_vmc_exit`) per-position
`_Tracked.wt2_peak_during_position` field'ını update eder ve `evaluate_exit`'e
ExitContext üzerinden geçirir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Direction
from src.strategy.ha_strategy.vmc_state import VMCSymbolState


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VMCExitConfig:
    """Yol B exit knob'ları (operatör onaylı default'lar — 2026-05-05)."""

    # Trigger A — momentum drawdown
    momentum_drawdown_pct: float = 0.20         # peak'ten %20 düşüş
    momentum_hold_zone_long: float = 70.0       # WT2 ≥ 70 → bypass
    momentum_hold_zone_short: float = -70.0     # WT2 ≤ -70 → bypass

    # Trigger B — oscillator dot + HA close break + volume
    osc_dot_required: bool = True               # WT cross OB/OS bölgesinde mi
    ha_close_lookback_bars: int = 5             # close son N bar üst/altını kırma
    ha_close_break_required: bool = True
    ha_min_body_pct_5m: float = 30.0            # doji guard (zayıf bar exit etmez)
    ha_volume_ratio_confirm: float = 1.2        # hacimli kapanış teyidi

    # Hold-extension (15m HA aligned ise uzat)
    hold_extension_15m_aligned: bool = True
    hold_extension_max_cycles: int = 2          # max 2 cycle (10dk on 5m)

    # Whipsaw guard — pozisyon ilk N bar exit gate'i çalışmasın
    min_bars_held: int = 1


# ──────────────────────────────────────────────────────────────────────────────
# Context + Decision
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExitContext:
    """evaluate_exit'e sağlanan input bundle (pure function input)."""

    direction: Direction                        # pozisyon yönü (BULLISH/BEARISH)
    vmc_state: VMCSymbolState                   # 5m + 15m history + helpers
    wt2_peak_during_position: float             # LONG: max WT2; SHORT: min WT2
    wt2_at_entry: float                         # entry anındaki WT2
    bars_held: int                              # cycle sayısı (whipsaw guard)
    hold_extension_count: int = 0               # kaç cycle 15m extension'da kaldı
    wt_cross: str = "—"                    # WT cross signal: UP / DOWN / —
    wt_state: str = "NEUTRAL"                   # OVERBOUGHT / OVERSOLD / NEUTRAL


@dataclass
class ExitDecision:
    """evaluate_exit output. action ∈ {HOLD, WARN, CLOSE}."""

    action: str                                 # HOLD | WARN | CLOSE
    reason: str = ""
    drawdown_pct: Optional[float] = None
    in_hold_zone: bool = False
    osc_dot_fired: bool = False
    ha_close_break_fired: bool = False
    volume_confirmed: bool = False
    fifteen_min_aligned: bool = False
    triggers: dict[str, bool] = field(default_factory=dict)

    @property
    def should_close(self) -> bool:
        return self.action == "CLOSE"

    @property
    def should_warn(self) -> bool:
        return self.action == "WARN"

    @property
    def should_hold(self) -> bool:
        return self.action == "HOLD"


# ──────────────────────────────────────────────────────────────────────────────
# Sub-gate helpers
# ──────────────────────────────────────────────────────────────────────────────


def _drawdown_pct(
    direction: Direction,
    current_wt2: float,
    peak: float,
) -> float:
    """Compute drawdown as fraction of |peak|.

    LONG: (peak - current) / |peak| — pozitif drawdown = aşağı düşüş.
    SHORT: (current - trough) / |trough| — pozitif drawdown = yukarı yükseliş.
    Peak ~ 0 ise edge case → 0 döner (drawdown anlamsız).
    """
    if abs(peak) < 1e-9:
        return 0.0
    if direction == Direction.BULLISH:
        return (peak - current_wt2) / abs(peak)
    return (current_wt2 - peak) / abs(peak)


def _in_hold_zone(
    direction: Direction,
    current_wt2: float,
    config: VMCExitConfig,
) -> bool:
    """Momentum hâlâ "havada" mı? Operatör "70-75'lerde kapanmamalı"."""
    if direction == Direction.BULLISH:
        return current_wt2 >= config.momentum_hold_zone_long
    return current_wt2 <= config.momentum_hold_zone_short


def _osc_dot_fired(direction: Direction, ctx: ExitContext) -> bool:
    """WT cross + bölge teyidi (sell dot for LONG, buy dot for SHORT).

    LONG: wt_cross == DOWN AND wt_state == OVERBOUGHT (Pine "sell circle").
    SHORT: wt_cross == UP AND wt_state == OVERSOLD (Pine "buy circle").
    """
    if direction == Direction.BULLISH:
        return ctx.wt_cross == "DOWN" and ctx.wt_state == "OVERBOUGHT"
    return ctx.wt_cross == "UP" and ctx.wt_state == "OVERSOLD"


def _ha_close_break_fired(
    direction: Direction,
    state: VMCSymbolState,
    config: VMCExitConfig,
) -> bool:
    """close son N bar üst/altını kırdı + body_pct ≥ threshold (doji guard).

    Operatör: "mum kendinden önceki 3-5 mumun altına iniyorsa anlık sıkıntılı.
    Ama zayıf doji şeklinde atıyorsa renk değişse bile önceki trend devam edebilir."
    """
    latest = state.latest
    if latest is None:
        return False
    if latest.ha_body_pct_5m < config.ha_min_body_pct_5m:
        return False  # zayıf doji — exit fire etmesin
    if direction == Direction.BULLISH:
        return state.ha_close_break_long(config.ha_close_lookback_bars)
    return state.ha_close_break_short(config.ha_close_lookback_bars)


def _volume_confirmed(
    state: VMCSymbolState, config: VMCExitConfig,
) -> bool:
    """volume_5m_ratio ≥ confirm threshold (1.2 default)."""
    latest = state.latest
    if latest is None:
        return False
    return latest.volume_5m_ratio >= config.ha_volume_ratio_confirm


def _fifteen_min_aligned(
    direction: Direction, state: VMCSymbolState,
) -> bool:
    """15m HA color hâlâ pozisyonla aligned mi (hold-extension için)."""
    latest = state.latest
    if latest is None:
        return False
    expected = "GREEN" if direction == Direction.BULLISH else "RED"
    return latest.ha_color_15m == expected


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_exit(
    ctx: ExitContext,
    config: VMCExitConfig,
) -> ExitDecision:
    """Yol B exit değerlendirmesi.

    Sıralama:
      1) Whipsaw guard — `bars_held < min_bars_held` → HOLD.
      2) Trigger A: drawdown gate — peak'ten %drawdown_pct düşüş?
      3) Trigger B: oscillator dot + HA close break + volume confirm.
      4) Hold-zone guard A: WT2 ≥ hold_zone (LONG) ise drawdown bypass
         (trigger B yine fire edebilir — operatör "trend bitince çık").
      5) Hold-extension guard B: drawdown fired ama 15m aligned + count <
         max_cycles → WARN (count++; bir sonraki cycle'da yeniden değerlendir).
      6) Else → CLOSE.
    """
    state = ctx.vmc_state
    latest = state.latest
    if latest is None:
        return ExitDecision(action="HOLD", reason="vmc_exit_no_state")

    # 1) Whipsaw guard
    if ctx.bars_held < config.min_bars_held:
        return ExitDecision(action="HOLD", reason="whipsaw_guard_min_bars")

    current_wt2 = latest.wt2

    # 2) Drawdown gate
    drawdown = _drawdown_pct(ctx.direction, current_wt2, ctx.wt2_peak_during_position)
    drawdown_fired = drawdown >= config.momentum_drawdown_pct
    in_hold_zone = _in_hold_zone(ctx.direction, current_wt2, config)

    # 3) Trigger B — confirmed reversal
    osc_fired = _osc_dot_fired(ctx.direction, ctx) if config.osc_dot_required else False
    ha_break = _ha_close_break_fired(ctx.direction, state, config) if config.ha_close_break_required else True
    volume_ok = _volume_confirmed(state, config)
    trigger_b_fired = osc_fired and ha_break and volume_ok

    # No trigger → HOLD
    if not (drawdown_fired or trigger_b_fired):
        return ExitDecision(
            action="HOLD",
            reason="vmc_exit_no_trigger",
            drawdown_pct=drawdown,
            in_hold_zone=in_hold_zone,
            triggers={"drawdown": drawdown_fired, "trigger_b": trigger_b_fired},
        )

    # 4) Hold-zone guard — momentum hâlâ havada
    # Operatör "havadayken tutulabilir" → drawdown bypass. Ama trigger B (kesin
    # reversal: dot + ha break + volume) yine fire ederse hold-zone'u override
    # eder (operatör "aşağı trend devam ediyorsa pozdan çıkış alınmalı").
    if in_hold_zone and drawdown_fired and not trigger_b_fired:
        return ExitDecision(
            action="HOLD",
            reason="vmc_exit_momentum_in_hold_zone",
            drawdown_pct=drawdown,
            in_hold_zone=True,
            triggers={"drawdown": True, "trigger_b": False},
        )

    # 5) 15m hold-extension
    fifteen_aligned = _fifteen_min_aligned(ctx.direction, state)
    if (
        config.hold_extension_15m_aligned
        and fifteen_aligned
        and ctx.hold_extension_count < config.hold_extension_max_cycles
    ):
        # Operatör: "5m'de 2 kırmızı atmıştır ama 15m'de hala renk değişmemiştir,
        # poz biraz daha tutulabilir." Caller hold_extension_count++ yapar.
        return ExitDecision(
            action="WARN",
            reason=f"vmc_exit_15m_hold_extension:count={ctx.hold_extension_count}/{config.hold_extension_max_cycles}",
            drawdown_pct=drawdown,
            in_hold_zone=in_hold_zone,
            osc_dot_fired=osc_fired,
            ha_close_break_fired=ha_break,
            volume_confirmed=volume_ok,
            fifteen_min_aligned=True,
            triggers={"drawdown": drawdown_fired, "trigger_b": trigger_b_fired},
        )

    # 6) Final close
    if drawdown_fired and trigger_b_fired:
        reason = "vmc_exit_drawdown_and_reversal"
    elif drawdown_fired:
        reason = f"vmc_exit_drawdown_pct={drawdown:.2%}"
    else:
        reason = "vmc_exit_oscillator_reversal"

    return ExitDecision(
        action="CLOSE",
        reason=reason,
        drawdown_pct=drawdown,
        in_hold_zone=in_hold_zone,
        osc_dot_fired=osc_fired,
        ha_close_break_fired=ha_break,
        volume_confirmed=volume_ok,
        fifteen_min_aligned=fifteen_aligned,
        triggers={"drawdown": drawdown_fired, "trigger_b": trigger_b_fired},
    )
