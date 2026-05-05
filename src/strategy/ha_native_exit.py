"""HA-native pozisyon exit gate — Yol A v5 dynamic 3-layer doctrine.

Operatör doctrine (2026-05-05): "Yükseliş trendi başladıysa negatif yön
gelene kadar pozisyon devam etmeli. Arada farklı renk mumlar gelse de
kısa vadeden ters yöne MSS oluşmuyorsa pozisyon tutulmalı. Yan
destekleyici veriler de negatife dönüyorsa dinamik kapatılıp olduğu
yerden terse pozisyon düşünmeli."

Pre-2026-05-05 doctrine: 3m HA streak ≥ 2 opposing OR 15m HA opposing →
defensive close (RCS volume gate ile filtreli). Bu "ufak ters mumlarda
kapat" davranışıdır — operatör reddetti.

3-Layer architecture:

  **Layer 1 (HOLD):** 3m HA color flip TEK BAŞINA → KAPATMA. Trend
  devam ediyor varsayımı; küçük ters mumlar tolerans. 15m HA opposing
  de tek başına kapatma için yetmez.

  **Layer 2 (WARN):** Pozisyon yönüne ters bir MSS direction sinyali
  gelirse (caller `last_mss_direction` field'ından okur — Pine emitting
  multi-TF agnostic MSS; gelecek Phase 3b Pine multi-TF MSS ile 1m'e
  daraltılır) → state'e `structural_warning=True` damgası. Bu sefer hâlâ
  CLOSE değil — supporting confirm beklenir.

  **Layer 3 (CLOSE):** Layer 2'den itibaren `structural_warning_active`
  AND (MFI 3-bar delta opposing OR RSI 3-bar delta opposing) AND
  `volume_3m_ratio ≥ rcs_confirm` → defensive close. Caller close
  sonrası `_pending_reverse_candidate` flag'i kullanarak Phase 4'te
  reverse-on-flip değerlendirebilir.

Architecture:
    evaluate_exit(ctx, config) → ExitDecision
       * Pure function: side-effect yok.
       * action ∈ {"CLOSE", "WARN", "HOLD"}.
       * Caller (`_maybe_close_on_ha_flip` runner method):
         - WARN → tracked.structural_warning=True stamp
         - CLOSE → defensive_close + reverse-candidate flag
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Direction
from src.strategy.ha_state import HASymbolState


@dataclass
class HANativeExitConfig:
    """HA-native exit gate parametreleri (Yol A v5 3-layer doctrine).

    Default'lar operatör spec'ine göre ayarlandı; YAML config'e expose
    etmek isterseniz `execution.ha_native_exit_*` knob'larıyla map edin.
    """

    # Master toggle — kapalıyken evaluate_exit her zaman HOLD döner.
    enabled: bool = True

    # Layer 2 trigger — last_mss_direction pozisyon yönüne ters çıkarsa
    # WARN damgası. False ise Layer 2 atla (Layer 3 fire edemez).
    enable_mss_layer2: bool = True

    # Layer 3 supporting-signals confirm: kaç MFI/RSI 3-bar delta gate
    # opposing yönde olmalı? 1 = "MFI VEYA RSI ters" yeterli (default,
    # operatör doctrine "yan destekleyici veriler" çoğul ama 1 yeterli
    # daha responsif), 2 = "her ikisi de ters" (daha tutucu).
    layer3_supporting_min_count: int = 1

    # Layer 3 RCS volume gate eşiği. >= confirm = "real reversal teyitli";
    # < confirm = volume yetersiz, hâlâ tut (warning persist).
    rcs_volume_ratio_confirm: float = 1.3

    # Pozisyon açıldıktan sonra exit gate fire etmesi için minimum
    # bar sayısı. 0 = anında değerlendirme. Default 1 = ilk cycle'ta
    # whipsaw'a karşı bir bar bekle.
    min_bars_held: int = 1


@dataclass
class ExitDecision:
    """evaluate_exit() çıktısı.

    action ∈ {"CLOSE", "WARN", "HOLD"}.
        CLOSE → caller defensive_close çağırır + reverse-candidate set
        WARN → caller tracked.structural_warning=True stamp eder
        HOLD → no-op
    reason her durumda insan-okur, journal/dashboard'a damgalanır.
    """

    action: str  # "CLOSE" | "WARN" | "HOLD"
    reason: str
    gate_results: dict[str, bool] = field(default_factory=dict)

    @property
    def should_close(self) -> bool:
        return self.action == "CLOSE"

    @property
    def should_warn(self) -> bool:
        return self.action == "WARN"


@dataclass
class ExitContext:
    """evaluate_exit() input — caller'ın derlediği state snapshot.

    `bars_since_open` 3m bar bazında sayılır (caller zamanı bar süresine
    böler). 0 = pozisyon henüz açıldı.

    `volume_3m_ratio` `signal_table.volume_3m_ratio`tan beslenir.

    `last_mss_direction` Pine emit `last_mss` field'ından parse edilir
    (caller HAEntryContext.last_mss_direction ile aynı formdan kullanır).
    Phase 3b'de Pine multi-TF MSS aktivasyonu sonrası `mss_direction_1m`
    ile değiştirilir; mevcut "single MSS proxy" 1m yaklaşıklığı.

    `mfi_delta_dir` / `rsi_delta_dir` HASymbolState.{mfi,rsi}_3m_delta_dir
    property'lerinden gelir. Değerler: "UP" / "DOWN" / "MIXED".

    `structural_warning_active` _Tracked.structural_warning state'idir;
    Layer 2 önceki cycle'da WARN basmışsa True. Layer 3 sadece bu True
    iken fire eder (latch behavior — bir kez warn, sonra confirm bekle).
    """

    position_direction: Direction  # BULLISH (long) / BEARISH (short)
    ha_state: HASymbolState
    bars_since_open: int = 0
    volume_3m_ratio: float = 1.0
    last_mss_direction: Optional[Direction] = None
    mfi_delta_dir: str = "MIXED"  # "UP" | "DOWN" | "MIXED"
    rsi_delta_dir: str = "MIXED"
    structural_warning_active: bool = False


# ── Helpers ────────────────────────────────────────────────────────────────


def _opposing_dir(position_direction: Direction) -> Direction:
    if position_direction == Direction.BULLISH:
        return Direction.BEARISH
    return Direction.BULLISH


def _mss_opposes_position(
    mss_dir: Optional[Direction], position_direction: Direction,
) -> bool:
    """MSS direction pozisyon yönüne ters mi?

    BULLISH (long) için mss=BEARISH → opposing (structural break aşağı).
    BEARISH (short) için mss=BULLISH → opposing.
    None / UNDEFINED → False (no signal).
    """
    if mss_dir is None:
        return False
    return mss_dir == _opposing_dir(position_direction)


def _delta_opposes_position(
    delta_dir: str, position_direction: Direction,
) -> bool:
    """3-bar delta direction pozisyon yönüne ters mi?

    BULLISH (long) için delta=DOWN → opposing.
    BEARISH (short) için delta=UP → opposing.
    MIXED / boş → False.
    """
    if position_direction == Direction.BULLISH:
        return delta_dir == "DOWN"
    if position_direction == Direction.BEARISH:
        return delta_dir == "UP"
    return False


# ── Main entry point ──────────────────────────────────────────────────────


def evaluate_exit(
    ctx: ExitContext, config: Optional[HANativeExitConfig] = None,
) -> ExitDecision:
    """HA-native pozisyon için 3-layer exit kararı (Yol A v5).

    Sırayla:
      1. config.enabled toggle.
      2. ha_state boş mu — bos ise HOLD.
      3. min_bars_held threshold — yeterince beklenmedi ise HOLD.
      4. Layer 3 öncelik: structural_warning_active AND supporting confirm
         AND RCS confirm → CLOSE.
      5. Layer 2: last_mss_direction opposing → WARN (state stamp).
         Önceden warn aktifse de "warn_persist" döner (HOLD davranışı,
         caller flag'i sıfırlamaz).
      6. Layer 1 (default): HOLD — 3m HA renk dönüşü tek başına yetmez.
    """
    cfg = config if config is not None else HANativeExitConfig()

    if not cfg.enabled:
        return ExitDecision(action="HOLD", reason="ha_native_exit_disabled")

    latest = ctx.ha_state.latest
    if latest is None:
        return ExitDecision(action="HOLD", reason="no_ha_data")

    if ctx.bars_since_open < cfg.min_bars_held:
        return ExitDecision(
            action="HOLD",
            reason=f"bars_too_few:{ctx.bars_since_open}/{cfg.min_bars_held}",
        )

    pos_dir = ctx.position_direction
    mss_opp = _mss_opposes_position(ctx.last_mss_direction, pos_dir)
    mfi_opp = _delta_opposes_position(ctx.mfi_delta_dir, pos_dir)
    rsi_opp = _delta_opposes_position(ctx.rsi_delta_dir, pos_dir)
    supporting_count = int(mfi_opp) + int(rsi_opp)
    rcs_confirm = ctx.volume_3m_ratio >= cfg.rcs_volume_ratio_confirm

    gate_results = {
        "mss_opposing": mss_opp,
        "mfi_delta_opposing": mfi_opp,
        "rsi_delta_opposing": rsi_opp,
        "rcs_confirm": rcs_confirm,
        "structural_warning_active": ctx.structural_warning_active,
    }

    # Layer 3 — structural warning + supporting confirm + RCS confirm.
    if (
        ctx.structural_warning_active
        and supporting_count >= cfg.layer3_supporting_min_count
        and rcs_confirm
    ):
        reason = (
            f"layer3_close:warning_active+supporting={supporting_count}/2"
            f"+rcs_confirm(vol_ratio={ctx.volume_3m_ratio:.2f})"
        )
        return ExitDecision(
            action="CLOSE", reason=reason, gate_results=gate_results,
        )

    # Layer 2 — MSS direction reversed (toggle off → skip).
    if cfg.enable_mss_layer2 and mss_opp:
        if ctx.structural_warning_active:
            # Already in warn state; persist (no new stamp needed). HOLD
            # action so caller knows nothing to fire, but reason explains.
            reason = "layer2_warning_persist"
        else:
            reason = "layer2_warn:mss_opposing"
        return ExitDecision(
            action="WARN" if not ctx.structural_warning_active else "HOLD",
            reason=reason,
            gate_results=gate_results,
        )

    # Layer 1 — default HOLD. 3m HA flip tek başına trigger değil.
    if ctx.structural_warning_active:
        # Warn aktif ama Layer 3 supporting/rcs confirm yok → tut, bekle.
        return ExitDecision(
            action="HOLD",
            reason=(
                f"layer3_pending:supporting={supporting_count}/2,"
                f"rcs_confirm={rcs_confirm}"
            ),
            gate_results=gate_results,
        )
    return ExitDecision(
        action="HOLD",
        reason="layer1_hold:no_structural_signal",
        gate_results=gate_results,
    )
