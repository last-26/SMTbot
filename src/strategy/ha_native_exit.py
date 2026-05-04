"""HA-native pozisyon exit gate — multi-TF HA renk dönüşü + RCS volume.

HA-native primary mode (2026-05-04 Yol A) entry doctrine'ının simetrik
tarafı: pozisyon AÇILDIKTAN sonra, HA renk yönü pozisyon yönüne ters
döndüğünde — whipsaw guard ile filtreli ve volume ratio ile teyitli —
defensive close tetiklenir.

Operatör spec (CLAUDE.md memory + 2026-05-04 chat):
  * 3m HA color flip TEK BAŞINA exit sinyali değil (tek bar noise olabilir).
    En az `min_opposing_bars_3m` kadar ardışık opposing bar gerekir.
  * 15m HA color opposing → "HTF anchor breakdown" — daha güçlü sinyal,
    tek bar yeterli (ama RCS gate hâlâ çalışır).
  * RCS gate: `volume_3m_ratio` ≥ confirm threshold → reversal teyitli, çık.
    Ratio ≤ noise threshold → "düşük volume = noise", pozisyonda kal.
    Arada → fail-open (yeterince volume yoksa varsayılan davranış).

Architecture:
    evaluate_exit(ctx, config) → ExitDecision
       * Pure function: side-effect yok, MarketState mutasyonu yok.
       * Runner caller (`_maybe_close_on_ha_flip`) decision'ı alıp
         `_defensive_close()` çağırır — burası sadece karar mantığı.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Direction
from src.strategy.ha_state import HASymbolState


@dataclass
class HANativeExitConfig:
    """HA-native exit gate parametreleri.

    Default'lar operatör spec'ine göre ayarlandı; YAML config'e expose
    etmek isterseniz `execution.ha_native_exit_*` knob'larıyla map edin.
    """

    # Master toggle — kapalıyken evaluate_exit her zaman HOLD döner.
    enabled: bool = True

    # 3m HA streak bazlı opposing eşiği (whipsaw guard).
    # 1 = tek opposing bar yeterli (gürültülü), 2 = iki ardışık (default,
    # operatör 2026-05-04: "renk değişir değişmez kapatmak yerine birkaç
    # ek konfirmasyon"), 3+ = daha tutucu.
    min_opposing_bars_3m: int = 2

    # 15m HA color opposing → tek bar yeterli (HTF anchor kırılımı).
    # False olursa sadece 3m streak path'i çalışır.
    enable_15m_opposing: bool = True

    # RCS (Real Confirmed Signal) volume gate eşikleri. Operatör:
    # "ratio ≥ 1.3 → confirm reversal; ≤ 0.8 → noise (pozisyonda kal)".
    # 0.8 < ratio < 1.3 arası → fail-open (default exit fires).
    rcs_volume_ratio_confirm: float = 1.3
    rcs_volume_ratio_noise: float = 0.8

    # Pozisyon açıldıktan sonra exit gate'in fire etmesi için minimum
    # bar sayısı. 0 = anında değerlendirme. Default 1 = ilk cycle'ta
    # whipsaw'a karşı bir bar bekle.
    min_bars_held: int = 1


@dataclass
class ExitDecision:
    """evaluate_exit() çıktısı. action her zaman set; reason her durumda
    insan-okur, journal/dashboard'a damgalanır."""

    action: str  # "CLOSE" | "HOLD"
    reason: str
    gate_results: dict[str, bool] = field(default_factory=dict)

    @property
    def should_close(self) -> bool:
        return self.action == "CLOSE"


@dataclass
class ExitContext:
    """evaluate_exit() input — caller'ın derlediği state snapshot.

    `bars_since_open` 3m bar bazında sayılır (caller zamanı bar süresine
    böler). 0 = pozisyon henüz açıldı.

    `volume_3m_ratio` `signal_table.volume_3m_ratio`tan beslenir; RCS
    gate sınıflandırma input'u. 1.0 nötr varsayılan (Pine emit etmemişse
    default), <0.8 = noise, >=1.3 = confirm.
    """

    position_direction: Direction  # BULLISH (long) / BEARISH (short)
    ha_state: HASymbolState
    bars_since_open: int = 0
    volume_3m_ratio: float = 1.0


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_3m_streak_opposing(
    streak_3m: int, position_direction: Direction, min_bars: int,
) -> bool:
    """3m HA streak pozisyon yönüne ters mi (whipsaw guard sonrası)?

    Streak signed: positive = ardışık GREEN, negative = ardışık RED.
    BULLISH (long) için: streak <= -min_bars → opposing teyitli.
    BEARISH (short) için: streak >= +min_bars → opposing teyitli.
    """
    if min_bars <= 0:
        return False
    if position_direction == Direction.BULLISH:
        return streak_3m <= -min_bars
    if position_direction == Direction.BEARISH:
        return streak_3m >= min_bars
    return False


def _is_15m_opposing(
    color_15m: str, position_direction: Direction,
) -> bool:
    """15m HA color pozisyon yönüne ters mi?

    BULLISH (long) için: 15m RED → opposing.
    BEARISH (short) için: 15m GREEN → opposing.
    DOJI / "" → opposing değil (belirsiz).
    """
    if position_direction == Direction.BULLISH:
        return color_15m == "RED"
    if position_direction == Direction.BEARISH:
        return color_15m == "GREEN"
    return False


def _classify_rcs(
    volume_ratio: float, config: HANativeExitConfig,
) -> str:
    """RCS volume gate sınıflandırma.

    Returns:
        "CONFIRM" — ratio >= confirm threshold (high volume = real reversal)
        "NOISE"   — ratio <= noise threshold (low volume = chop, hold)
        "NEUTRAL" — arada (default exit fires unless other gate suppresses)
    """
    if volume_ratio >= config.rcs_volume_ratio_confirm:
        return "CONFIRM"
    if volume_ratio <= config.rcs_volume_ratio_noise:
        return "NOISE"
    return "NEUTRAL"


# ── Main entry point ──────────────────────────────────────────────────────


def evaluate_exit(
    ctx: ExitContext, config: Optional[HANativeExitConfig] = None,
) -> ExitDecision:
    """HA-native pozisyon için exit kararı.

    Sırayla:
      1. config.enabled toggle.
      2. ha_state boş mu — bos ise HOLD.
      3. min_bars_held threshold — yeterince beklenmedi ise HOLD.
      4. 15m HA opposing? + 3m HA streak opposing?
         İkisi de False → HOLD.
      5. RCS volume gate (ratio sınıflandırma):
         CONFIRM → CLOSE (en güçlü sinyal — reason: ha_15m_or_3m + rcs_confirm)
         NOISE   → HOLD (düşük volume, gürültü — pozisyonda kal)
         NEUTRAL → CLOSE (default exit, gate sadece NOISE'ı bloklar)
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
    is_15m_opp = (
        cfg.enable_15m_opposing
        and _is_15m_opposing(latest.ha_color_15m, pos_dir)
    )
    is_3m_streak_opp = _is_3m_streak_opposing(
        latest.ha_streak_3m, pos_dir, cfg.min_opposing_bars_3m,
    )

    if not is_15m_opp and not is_3m_streak_opp:
        return ExitDecision(
            action="HOLD",
            reason="no_opposing_bars",
            gate_results={
                "15m_opposing": False,
                "3m_streak_opposing": False,
            },
        )

    # En az bir opposing gate açık → RCS gate ile teyit ara.
    rcs_class = _classify_rcs(ctx.volume_3m_ratio, cfg)
    volume_ratio = ctx.volume_3m_ratio

    gate_results = {
        "15m_opposing": is_15m_opp,
        "3m_streak_opposing": is_3m_streak_opp,
        "rcs_confirm": rcs_class == "CONFIRM",
        "rcs_noise": rcs_class == "NOISE",
    }

    if rcs_class == "NOISE":
        return ExitDecision(
            action="HOLD",
            reason=(
                f"rcs_noise(ratio={volume_ratio:.2f}<="
                f"{cfg.rcs_volume_ratio_noise:.2f})"
            ),
            gate_results=gate_results,
        )

    # CONFIRM veya NEUTRAL → close. Reason'ı en güçlü gate'le damgala.
    if is_15m_opp and is_3m_streak_opp:
        primary = "15m_and_3m_opposing"
    elif is_15m_opp:
        primary = "15m_opposing"
    else:
        primary = f"3m_streak_opposing(streak={latest.ha_streak_3m})"

    rcs_tag = "rcs_confirm" if rcs_class == "CONFIRM" else "rcs_neutral"
    reason = f"{primary}+{rcs_tag}(vol_ratio={volume_ratio:.2f})"

    return ExitDecision(
        action="CLOSE", reason=reason, gate_results=gate_results,
    )


