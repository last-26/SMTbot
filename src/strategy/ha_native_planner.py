"""HA-native entry decision dispatcher — 3 entry tipi + score-based pick.

Operatör 2026-05-05 strateji felsefesi netleşti: bot her zaman trend
DÖNÜŞÜ bekleyerek entry alır. Devam eden trende ortadan dalmaz, dönüş
onayı bekler. 3 farklı entry tipi paralel değerlendirilir:

  Tip 1 — Major Reversal (operatör ana case):
    "Trend değişim noktası — uzun trendin tepesi/dibi"
    Senaryo: 5+ ardışık yeşil → 2+ ardışık kırmızı → SHORT
    Risk: tam R, hedef 1.5R (let it run)

  Tip 2 — Continuation (downtrend kandırıcı yükseliş case):
    "Ana trend devam ediyor, kısa düzeltme bitti"
    Senaryo: 15m+3m downtrend, 1-2 yeşil bar (kandırıcı toparlanma),
    tekrar kırmızıya dönüş → SHORT (ana trend devam)
    Risk: tam R, hedef 1.0R (kısa hareket)
    Faz 2'de tam implementation; bu commit'te score=0 stub.

  Tip 3 — Micro Reversal (1m mss dip/tepe avcılığı):
    2026-05-05 v2 itibarıyla ENABLED (operatör doktrini: bot her cycle
    aynı şekilde çalışsın, 3 tip paralel skorlanır). Mandatory fail
    veya soft threshold altında kalsa da gerçek skor görünür.
    Risk: yarı R ($12.5), hedef 0.7R.

Her cycle 3 tip skor hesaplanır, threshold geçen + en yüksek skor
kazanır. Hiçbiri threshold geçmezse REJECT. decision_log'a 3 skor da
yazılır (Pass 3 GBT segmentasyonu için).

Backward compat: `EntryDecision.decision` eski enum değerleri (TAKE_LONG /
TAKE_SHORT / REJECT / NO_SETUP) kullanır; yeni `entry_path` field
("major_reversal" / "continuation" / "micro_reversal") tip bilgisini
taşır.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.data.models import Direction, MarketState
from src.strategy.ha_state import HASymbolState


# ── Config ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HANativeConfig:
    """Default knob values for the HA-native entry dispatcher.

    Operatör 2026-05-05 spec'i:
      - Önceki ters streak min 3 bar (sık entry için, 5 bar zor)
      - Karma 15m anchor (skor katkısı, hard-block değil)
      - Tip 1 + Tip 2 aktif, Tip 3 DISABLED ilk fazda
    """

    # ── Mandatory gate parametreleri (her tip için ortak) ─────────────────
    mss_density_window: int = 6
    mss_density_max: int = 2
    min_streak_3m: int = 2                    # whipsaw guard (yeni yönde)
    min_body_pct_3m: float = 30.0             # doji-skip
    min_delta_3bar: float = 0.5               # MFI/RSI delta floor
    confluence_passive_threshold: float = 5.0

    # ── Tip 1 (Major Reversal) parametreleri ──────────────────────────────
    # Önceki ters yönde minimum streak — operatör 2026-05-05 v2: 3→2.
    # İlk live cycle gözleminde sürekli `prev_streak_min` mandatory fail
    # ediyordu (bot uzun trend ortasında entry üretemiyordu, dönüş için
    # 3+ önceki ters bar bulamıyordu); 2 bar = 6 dk 3m TF, daha sık entry
    # candidate sağlar. Soft threshold 4.0 hâlâ kalite filtresi olarak
    # devreye giriyor.
    major_reversal_prev_streak_min: int = 2
    major_reversal_threshold: float = 4.0
    major_reversal_target_rr: float = 1.5

    # ── Tip 2 (Continuation) parametreleri ────────────────────────────────
    continuation_threshold: float = 4.5
    continuation_target_rr: float = 1.0
    # Operatör spec: 3m'de karşı yön streak ≤ 2 (kısa toparlanma)
    continuation_max_counter_streak: int = 2
    # Önceki ana-yön streak gücü ≥ 4 (güçlü trend kanıtı)
    continuation_main_trend_min_streak: int = 4

    # ── Tip 3 (Micro Reversal) parametreleri ──────────────────────────────
    # Operatör 2026-05-05 v2: Tip 3 ENABLED. İlk live cycle gözleminde
    # μR=0.00 sürekli görünüyordu (early return DISABLED branch'i score
    # hesabına bile geçmiyordu). Dispatcher tüm cycle'larda 3 tip paralel
    # skor üretsin — bot her zaman aynı şekilde çalışsın doktrini.
    # Yarı R ($12.5) + 0.7R hedef güvenlik kalır.
    micro_reversal_enabled: bool = True
    micro_reversal_threshold: float = 4.5
    micro_reversal_target_rr: float = 0.7
    micro_reversal_risk_multiplier: float = 0.5  # yarı R = $12.5

    # ── Entry execution ───────────────────────────────────────────────────
    marketable_offset_pct: float = 0.0005     # 5 bps slippage cap
    entry_cycle_timeout: int = 1              # 1 cycle fill yoksa cancel

    # ── Backward compat: legacy single-RR knob (Tip 1 default'una map) ────
    # `target_rr_ratio` mevcut runner caller'larında kullanılıyor; eski
    # kod path'inde aynı kalsın diye.
    target_rr_ratio: float = 1.0

    # ── Confluence destek faktörü (operatör 2026-05-05 v4) ────────────────
    # HA-native dispatcher entry kararını tek başına verir; confluence
    # SOFT skoru aktif olarak etkiler (yön teyidi destek). Mandatory
    # gate'lerde yeri yok — sadece soft skor seviyesinde aligned/opposing/
    # strong farkı yaratır.
    confluence_aligned_bonus: float = 2.0       # yön match
    confluence_opposing_penalty: float = -1.5   # yön ters (negative)
    confluence_strong_extra: float = 1.0        # aligned + score >= strong threshold
    confluence_strong_threshold: float = 5.0    # confluence skor "strong" eşiği

    # ── Deprecated knobs (audit için tutuluyor) ───────────────────────────
    adx_threshold: Optional[float] = None
    fresh_mss_max_bars: Optional[int] = None


# ── Entry context ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntryContext:
    """All inputs evaluate_entry() needs for one symbol cycle.

    Caller (runner) assembles this from BotContext caches + market_state +
    ha_state. Frozen for safety.
    """

    symbol: str
    market_state: MarketState
    ha_state: HASymbolState

    # ADX triad
    adx_3m: Optional[float] = None
    plus_di_3m: Optional[float] = None
    minus_di_3m: Optional[float] = None

    # Recent MSS history (Pine'dan parse edilir)
    last_mss_bar: Optional[int] = None
    last_mss_direction: Optional[Direction] = None
    bars_since_last_mss: Optional[int] = None
    mss_count_recent: int = 0

    # Order book for marketable limit
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

    # Cross-cycle deduplication
    pending_pairs: frozenset = frozenset()
    open_pairs: frozenset = frozenset()

    # Swing extremes for SL anchor
    last_swing_low: Optional[float] = None
    last_swing_high: Optional[float] = None

    # 2026-05-05 — Tip 2 Continuation için:
    # "İlk entry kaçırıldı" flag. Runner DB query ile doldurur:
    # current dominant_color trend'i içinde bu sembole `is_ha_native=True`
    # OPEN/CLOSED row var mı? Yoksa flag=True (skor +0.5).
    first_entry_missed: bool = False

    # 2026-05-05 — Önceki dominant trend streak (Tip 1 + Tip 2 input).
    # `ha_state.history` üzerinden hesaplanabilir ama caller tarafında
    # cache'lemek faster. Default 0 → Tip 1 reverse-streak hesabı
    # ha_state'den fallback yapar.
    prev_main_streak: int = 0

    # 2026-05-05 v4 — Confluence destek sinyali. Caller (runner) cycle
    # başına bir kez `calculate_confluence(...)` koşar, sonucu burada
    # plan dispatcher'a pas eder. None → confluence destekli skor +0/-0
    # (tarafsız). HA-native mandatory gate'lerini etkilemez; sadece soft
    # skoru aligned bonus / opposing penalty / strong extra ile değiştirir.
    confluence: Optional[Any] = None


# ── Entry decision (output) ───────────────────────────────────────────────


@dataclass
class EntryTypeScore:
    """Tek bir entry tipinin score + parametre snapshot'ı."""

    score: float = 0.0
    direction: Optional[Direction] = None
    gate_results: dict[str, bool] = field(default_factory=dict)
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    target_rr: float = 1.0
    risk_multiplier: float = 1.0
    # Bir gate fail ettiyse hangi gate (audit için)
    failed_mandatory: Optional[str] = None


@dataclass
class EntryDecision:
    """Output of evaluate_entry()."""

    # decision = "TAKE_LONG" / "TAKE_SHORT" / "REJECT" / "NO_SETUP"
    # (backward compat — runner reject_reason mapping bunu okur)
    decision: str
    direction: Optional[Direction] = None
    # 2026-05-05 — yeni: hangi entry tipi kazandı
    entry_path: Optional[str] = None  # "major_reversal" / "continuation" / "micro_reversal"
    reason: str = ""
    # gate_results artık tip-spesifik nested dict:
    # {"major_reversal": {...}, "continuation": {...}, "micro_reversal": {...}}
    gate_results: dict[str, dict] = field(default_factory=dict)
    # Per-tip skorlar (Pass 3 GBT için)
    major_reversal_score: float = 0.0
    continuation_score: float = 0.0
    micro_reversal_score: float = 0.0
    # Kazanan tipin parametreleri (TAKE durumunda dolu)
    suggested_entry_price: Optional[float] = None
    suggested_sl_price: Optional[float] = None
    suggested_tp_price: Optional[float] = None
    target_rr: float = 1.0
    risk_multiplier: float = 1.0
    notes: str = ""

    @property
    def is_take(self) -> bool:
        return self.decision in ("TAKE_LONG", "TAKE_SHORT")


# ── Backward-compat helpers (eski test API'sini destekler) ───────────────


def _gate_adx(adx_3m: Optional[float], threshold: float) -> bool:
    """DEPRECATED gate (operatör 2026-05-04 revize ile entry gate'ten çıktı).
    Backward-compat — eski testler decision_log audit için kullanır."""
    if adx_3m is None:
        return False
    return adx_3m >= threshold


def _gate_fresh_mss(
    bars_since: Optional[int], max_bars: int,
) -> bool:
    """DEPRECATED gate. Backward-compat helper."""
    if bars_since is None:
        return False
    return 0 <= bars_since <= max_bars


def _gate_two_bar_color(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """DEPRECATED gate (yeni dispatcher kullanmıyor; operatör 2026-05-05:
    `streak_3m` zaten 'art arda mum' çevirir). Backward-compat — eski
    test_ha_native_planner.py tek-tek helper testlerini geçirsin diye."""
    if len(ha_state.history) < 2:
        return False
    target = "GREEN" if direction == Direction.BULLISH else "RED"
    last = ha_state.latest
    prev = ha_state.previous
    return (
        last.ha_color_3m == target
        and prev.ha_color_3m == target
        and last.ha_color_1m == target
        and prev.ha_color_1m == target
    )


def _gate_mfi_delta(
    ha_state: HASymbolState, direction: Direction,
    min_abs_value: float = 0.5,
) -> bool:
    """Backward-compat alias for `_gate_mfi_delta_aligned`. min_abs_value
    parametresi `ha_state._delta_dir`'ün kendi kontrolüyle ortak."""
    return _gate_mfi_delta_aligned(ha_state, direction)


def _gate_rsi_delta(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Backward-compat alias for `_gate_rsi_delta_aligned`."""
    return _gate_rsi_delta_aligned(ha_state, direction)


# ── Direction inference (3m HA color) ─────────────────────────────────────


def _infer_direction_from_3m(ctx: EntryContext) -> Optional[Direction]:
    """3m HA color → Direction. DOJI/empty → None."""
    if ctx.ha_state.latest is None:
        return None
    color = ctx.ha_state.latest.ha_color_3m
    if color == "GREEN":
        return Direction.BULLISH
    if color == "RED":
        return Direction.BEARISH
    return None


# Backward-compat alias for legacy tests
_infer_trend_direction = _infer_direction_from_3m


def _infer_direction_from_15m(ctx: EntryContext) -> Optional[Direction]:
    """15m HA color → Direction. DOJI/empty → None."""
    if ctx.ha_state.latest is None:
        return None
    color = ctx.ha_state.latest.ha_color_15m
    if color == "GREEN":
        return Direction.BULLISH
    if color == "RED":
        return Direction.BEARISH
    return None


# ── Generic gate helpers (her tip kullanabilir) ──────────────────────────


def _gate_mss_density(count: int, max_count: int) -> bool:
    return count <= max_count


def _gate_streak_3m(
    ha_state: HASymbolState, direction: Direction, min_abs: int,
) -> bool:
    if ha_state.latest is None:
        return False
    streak = ha_state.latest.ha_streak_3m
    if direction == Direction.BULLISH:
        return streak >= min_abs
    if direction == Direction.BEARISH:
        return streak <= -min_abs
    return False


def _gate_no_duplicate(
    symbol: str, direction: Direction,
    pending: frozenset, open_set: frozenset,
) -> bool:
    pair = (symbol, direction)
    return pair not in pending and pair not in open_set


def _gate_body_size(
    ha_state: HASymbolState, min_pct: float,
) -> bool:
    if ha_state.latest is None:
        return False
    return ha_state.latest.ha_body_pct_3m >= min_pct


def _gate_dominant_color_alignment(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Liberal: dominant ters baskı yoksa OK."""
    dominant_3m = ha_state.dominant_color_3m()
    if dominant_3m is None:
        return True
    opposite = "RED" if direction == Direction.BULLISH else "GREEN"
    return dominant_3m != opposite


def _gate_15m_alignment(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    if ha_state.latest is None:
        return False
    target = "GREEN" if direction == Direction.BULLISH else "RED"
    return ha_state.latest.ha_color_15m == target


def _gate_mss_direction_alignment(
    last_mss_direction: Optional[Direction], direction: Direction,
) -> bool:
    if last_mss_direction is None:
        return False
    return last_mss_direction == direction


def _gate_mfi_delta_aligned(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    direction_str = ha_state.mfi_3m_delta_dir
    if direction == Direction.BULLISH:
        return direction_str == "UP"
    if direction == Direction.BEARISH:
        return direction_str == "DOWN"
    return False


def _gate_rsi_delta_aligned(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    direction_str = ha_state.rsi_3m_delta_dir
    if direction == Direction.BULLISH:
        return direction_str == "UP"
    if direction == Direction.BEARISH:
        return direction_str == "DOWN"
    return False


def _gate_vwap_alignment(
    state: MarketState, direction: Direction,
) -> bool:
    """Price vs vwap_3m alignment — LONG → price >= vwap, SHORT → tersi."""
    sig = state.signal_table
    if sig is None or sig.vwap_3m <= 0 or sig.price <= 0:
        return False
    if direction == Direction.BULLISH:
        return sig.price >= sig.vwap_3m
    return sig.price <= sig.vwap_3m


def _gate_rcs_volume(state: MarketState, threshold: float = 1.3) -> bool:
    """volume_3m_ratio ≥ confirm threshold."""
    sig = state.signal_table
    if sig is None:
        return False
    return sig.volume_3m_ratio >= threshold


def _confluence_support(
    ctx: EntryContext, direction: Direction, config: HANativeConfig,
) -> tuple[dict[str, bool], float]:
    """Confluence destek faktörü — yön teyidi.

    Operatör 2026-05-05 v4: legacy multi-pillar (mss/vwap/mfi/rsi/divergence/
    structure/liquidity) bel-kemiği onay sinyali. Entry kararı vermez ama
    yön belirlenirken soft skoru aktif olarak etkiler:
      * aligned (confluence.direction == direction): +confluence_aligned_bonus
      * aligned + strong (score >= strong_threshold): ek +confluence_strong_extra
      * opposing (confluence opposite direction): +confluence_opposing_penalty
        (negative — score'u düşürür)
      * UNDEFINED / None: tarafsız 0

    Returns (gates_dict, score_delta).
    """
    gates: dict[str, bool] = {}
    if ctx.confluence is None:
        gates["confluence_aligned"] = False
        gates["confluence_strong"] = False
        gates["confluence_opposing"] = False
        return gates, 0.0
    conf_dir = getattr(ctx.confluence, "direction", None)
    conf_score = float(getattr(ctx.confluence, "score", 0.0) or 0.0)
    aligned = conf_dir == direction
    opposing = (
        conf_dir in (Direction.BULLISH, Direction.BEARISH)
        and conf_dir != direction
    )
    gates["confluence_aligned"] = aligned
    gates["confluence_strong"] = (
        aligned and conf_score >= config.confluence_strong_threshold
    )
    gates["confluence_opposing"] = opposing
    delta = 0.0
    if aligned:
        delta += config.confluence_aligned_bonus
        if conf_score >= config.confluence_strong_threshold:
            delta += config.confluence_strong_extra
    elif opposing:
        delta += config.confluence_opposing_penalty  # negative
    return gates, delta


def _prev_main_streak_from_history(
    ha_state: HASymbolState, current_direction: Direction,
) -> int:
    """Mevcut bardan geriye doğru, current_direction'ın TERSİNDEKİ ardışık
    bar sayısı. Major Reversal "önceki streak ≥ 3" gate'i için kullanılır.

    Örn: ha_state son 5 bar = [GREEN, GREEN, GREEN, GREEN, RED] (oldest→newest)
    current_direction = BEARISH (yeni RED bar)
    → önceki ardışık YEŞİL = 4
    """
    if len(ha_state.history) < 2:
        return 0
    target_prev = "GREEN" if current_direction == Direction.BEARISH else "RED"
    history = list(ha_state.history)
    # Skip current bar (latest), count consecutive prev_target
    count = 0
    for snap in reversed(history[:-1]):
        if snap.ha_color_3m == target_prev:
            count += 1
        else:
            break
    return count


# ── Pricing helpers ────────────────────────────────────────────────────────


def _marketable_entry_price(
    direction: Direction, best_bid: float, best_ask: float, offset_pct: float,
) -> float:
    """Maker-mirror entry price for HA-native limit (Yol A v5, 2026-05-05).

    Operator doctrine: scalp entries pay maker fees; never cross the spread
    on entry. The previous "marketable" formula (BUY at ask + offset / SELL
    at bid - offset) put orders on the wrong side of the book, causing
    Bybit's match engine to async-cancel post-only orders ~200-500ms after
    placement (`reason=external` cancels in 04:33 / 04:34 live cycle).

    New formula rests passively inside the spread:
      - LONG  (BUY):  best_bid - offset → BUY below bid (waits for dip)
      - SHORT (SELL): best_ask + offset → SELL above ask (waits for pop)

    Trade-off: no immediate fill guarantee — if price runs away within the
    1-cycle timeout (3m), the limit cancels and dispatcher re-evaluates next
    cycle. Acceptable for scalp doctrine; maker fee discount > miss cost.

    Note: function name retained for back-compat; `marketable_offset_pct`
    config knob stays the same. Phase 2+ may rename to `_passive_entry_price`
    + `passive_offset_pct` once the dynamic-exit migration ships.
    """
    if direction == Direction.BULLISH:
        return best_bid * (1.0 - offset_pct)
    return best_ask * (1.0 + offset_pct)


def _structural_sl_price(
    direction: Direction, ctx: EntryContext,
) -> Optional[float]:
    if direction == Direction.BULLISH:
        return ctx.last_swing_low
    return ctx.last_swing_high


def _tp_price(
    direction: Direction, entry: float, sl: float, rr: float,
) -> float:
    sl_distance = abs(entry - sl)
    if direction == Direction.BULLISH:
        return entry + rr * sl_distance
    return entry - rr * sl_distance


# ── Tip 1: Major Reversal score ───────────────────────────────────────────


def _score_major_reversal(
    ctx: EntryContext, config: HANativeConfig,
) -> EntryTypeScore:
    """Tip 1 — Major Reversal (büyük trend dönüşü).

    Mandatory gates (fail = score 0, failed_mandatory set):
      - HA color clear (3m DOJI değil) → direction implied
      - Yeni yönde streak ≥ min_streak_3m=2 (whipsaw guard)
      - Önceki ters yönde streak ≥ major_reversal_prev_streak_min=2
        (trend dönüş başlangıcı — 2026-05-05 v2: 3→2)
      - mss_density (chop guard)
      - no_duplicate
      - body_size ≥ 30%

    Soft skor faktörleri (threshold ≥ 4.0):
      - 15m HA pozisyon yönünde aligned: +2.0 (HTF anchor desteği)
      - 15m HA hâlâ eski yönlü (anchor henüz dönmedi): +1.0 (agresif tepeden)
      - MSS direction yeni yönü onaylıyor (yapı kırıldı): +2.0
      - MFI delta yeni yönde: +1.0
      - RSI delta yeni yönde: +1.0
      - VWAP yeni yönle aligned: +0.5
      - RCS volume_3m_ratio ≥ 1.3: +1.0
      - dominant_color hâlâ eski yönlü (büyük resim trend tepesi): +0.5
    """
    direction = _infer_direction_from_3m(ctx)
    score = EntryTypeScore(score=0.0, direction=direction)
    if direction is None:
        score.failed_mandatory = "no_ha_direction"
        return score

    # Mandatory gates
    gates: dict[str, bool] = {}
    gates["mss_density"] = _gate_mss_density(
        ctx.mss_count_recent, config.mss_density_max,
    )
    gates["streak_3m_new_direction"] = _gate_streak_3m(
        ctx.ha_state, direction, config.min_streak_3m,
    )
    prev_streak = (
        ctx.prev_main_streak
        if ctx.prev_main_streak > 0
        else _prev_main_streak_from_history(ctx.ha_state, direction)
    )
    gates["prev_streak_min"] = prev_streak >= config.major_reversal_prev_streak_min
    gates["no_duplicate"] = _gate_no_duplicate(
        ctx.symbol, direction, ctx.pending_pairs, ctx.open_pairs,
    )
    gates["body_size"] = _gate_body_size(ctx.ha_state, config.min_body_pct_3m)

    # 2026-05-05 — operatör spec: skor her zaman hesaplansın (mandatory
    # fail olsa bile soft factors görünür → Pass 3 GBT için "mandatory
    # fail anında soft durum neydi?" sorusu cevap bulur, anlık gözlemde
    # de soft skor 0 yerine gerçek değer görülür). Mandatory fail flag'i
    # tutulur ama erken return YOK — alttaki TAKE check'inde mandatory'ler
    # hâlâ blocking. Yani take logic değişmez, sadece skor zenginleşir.
    mandatory_keys = (
        "mss_density", "streak_3m_new_direction", "prev_streak_min",
        "no_duplicate", "body_size",
    )
    failed_mandatory_key: Optional[str] = None
    for k in mandatory_keys:
        if not gates[k]:
            failed_mandatory_key = k
            break  # ilk fail eden — eski reason taxonomy ile uyumlu

    # Soft factors (threshold contribution) — mandatory fail durumunda
    # bile hesaplanır, sadece TAKE block'unda guard var.
    score_value = 0.0
    gates["15m_aligned_new"] = _gate_15m_alignment(ctx.ha_state, direction)
    if gates["15m_aligned_new"]:
        score_value += 2.0
    else:
        # 15m hâlâ eski yönlü → "tepeden agresif giriş" için ek skor
        prev_dir = (
            Direction.BEARISH if direction == Direction.BULLISH
            else Direction.BULLISH
        )
        gates["15m_aligned_old"] = _gate_15m_alignment(ctx.ha_state, prev_dir)
        if gates["15m_aligned_old"]:
            score_value += 1.0

    gates["mss_direction_aligned"] = _gate_mss_direction_alignment(
        ctx.last_mss_direction, direction,
    )
    if gates["mss_direction_aligned"]:
        score_value += 2.0

    gates["mfi_delta_aligned"] = _gate_mfi_delta_aligned(
        ctx.ha_state, direction,
    )
    if gates["mfi_delta_aligned"]:
        score_value += 1.0

    gates["rsi_delta_aligned"] = _gate_rsi_delta_aligned(
        ctx.ha_state, direction,
    )
    if gates["rsi_delta_aligned"]:
        score_value += 1.0

    gates["vwap_aligned"] = _gate_vwap_alignment(ctx.market_state, direction)
    if gates["vwap_aligned"]:
        score_value += 0.5

    gates["rcs_volume_confirm"] = _gate_rcs_volume(ctx.market_state, 1.3)
    if gates["rcs_volume_confirm"]:
        score_value += 1.0

    # Eski yön dominant_color → büyük resim trend tepesi, reversal candidate güçlü
    prev_dir = (
        Direction.BEARISH if direction == Direction.BULLISH
        else Direction.BULLISH
    )
    gates["dominant_color_old_aligned"] = _gate_15m_alignment(
        ctx.ha_state, prev_dir,
    )  # NB: kullanılmıyor — gate adı dominant değil 15m. fix:
    # dominant_color_alignment'a bak:
    dominant_3m = ctx.ha_state.dominant_color_3m()
    expected_old = "GREEN" if direction == Direction.BEARISH else "RED"
    gates["dominant_old_aligned"] = (dominant_3m == expected_old)
    # Düzeltme: dominant_color_old_aligned key'i ile karışmasın
    del gates["dominant_color_old_aligned"]
    if gates["dominant_old_aligned"]:
        score_value += 0.5

    # 2026-05-05 v4 — Confluence destek faktörü (yön teyidi, aktif)
    conf_gates, conf_delta = _confluence_support(ctx, direction, config)
    gates.update(conf_gates)
    score_value += conf_delta

    score.score = score_value
    score.gate_results = gates

    # 2026-05-05 — Mandatory guard: skor görünür ama mandatory fail varsa
    # TAKE etmiyoruz. Operatör spec: "mandatory'ler bel kemiği, fail =
    # entry yok" — sadece skor görünür kalsın.
    if failed_mandatory_key is not None:
        score.failed_mandatory = failed_mandatory_key
        return score

    # Build entry parameters if soft threshold passes
    if score_value >= config.major_reversal_threshold:
        if ctx.best_bid is None or ctx.best_ask is None:
            score.failed_mandatory = "missing_orderbook"
            return score
        sl = _structural_sl_price(direction, ctx)
        if sl is None:
            score.failed_mandatory = "missing_swing_anchor"
            return score
        entry = _marketable_entry_price(
            direction, ctx.best_bid, ctx.best_ask, config.marketable_offset_pct,
        )
        tp = _tp_price(direction, entry, sl, config.major_reversal_target_rr)
        score.entry_price = entry
        score.sl_price = sl
        score.tp_price = tp
        score.target_rr = config.major_reversal_target_rr
        score.risk_multiplier = 1.0

    return score


# ── Tip 2: Continuation score ─────────────────────────────────────────────


def _count_recent_counter_streak(
    ha_state: HASymbolState, main_direction: Direction,
) -> int:
    """Mevcut bardan geriye doğru, main_direction'ın TERSİNDE ardışık bar
    sayısı (Continuation için 'kandırıcı toparlanma' uzunluğu).

    Operatör 2026-05-05 örneği: "downtrend devam ediyor, 1-2 yeşil bar
    kandırıcı toparlanma sonra tekrar kırmızıya dönüş → SHORT."
    Bu fonksiyon current bar'dan başlayarak geriye doğru "yeşil bar"
    sayısını sayar (downtrend için).

    Bu skor mantığında current bar **main yönde** olmalıdır (yani 3m HA
    color = main_direction). Sayım current bar'dan ÖNCE başlar:

      bars: [..., RED, RED, RED, GREEN, GREEN, RED]
                                              ^current = main (RED)
      counter_streak = 2 (GREEN, GREEN)

    Returns 0 if current bar değil ana yönde veya counter bar yoksa.
    """
    if len(ha_state.history) < 2:
        return 0
    main_color = "GREEN" if main_direction == Direction.BULLISH else "RED"
    counter_color = "RED" if main_direction == Direction.BULLISH else "GREEN"
    history = list(ha_state.history)
    # Current bar must be main color
    if not history or history[-1].ha_color_3m != main_color:
        return 0
    # Count counter bars going backwards from before current
    count = 0
    for snap in reversed(history[:-1]):
        if snap.ha_color_3m == counter_color:
            count += 1
        else:
            break
    return count


def _count_main_trend_streak_before_pullback(
    ha_state: HASymbolState, main_direction: Direction, counter_streak: int,
) -> int:
    """Pullback'ten ÖNCE ana yön streak'inin uzunluğu (önceki ana-yön gücü).

    Continuation skoru için 'önceki ana-yön streak ≥ 4' faktörü:
    bars: [..., RED, RED, RED, RED, RED, GREEN, GREEN, RED]
                                                       ^current
                <-- main_streak_before -->  <pull-back>
    counter_streak = 2 (GREEN, GREEN)
    main_streak_before = 5 (RED × 5)

    Returns 0 if history yetersiz.
    """
    if len(ha_state.history) < counter_streak + 2:
        return 0
    main_color = "GREEN" if main_direction == Direction.BULLISH else "RED"
    history = list(ha_state.history)
    # Skip current bar + counter_streak counter bars, count main color back
    skip = 1 + counter_streak
    if len(history) <= skip:
        return 0
    count = 0
    for snap in reversed(history[:-skip]):
        if snap.ha_color_3m == main_color:
            count += 1
        else:
            break
    return count


def _score_continuation(
    ctx: EntryContext, config: HANativeConfig,
) -> EntryTypeScore:
    """Tip 2 — Trend Continuation (operatör spec: kandırıcı yükseliş sonrası
    ana trende dönüş).

    Senaryo:
      15m + 3m downtrend, dominant_color RED
      3m'de 1-2 GREEN bar (kandırıcı toparlanma)
      Tekrar RED'e dönüş → SHORT (ana trend devam)

    Mandatory gates (fail = score 0, failed_mandatory set):
      - HA color clear (3m DOJI değil) → main direction
      - 15m HA aynı yön (büyük resim onayı şart)
      - dominant_color ana yönü destekliyor (chop değil)
      - Karşı yönde önceki streak ≤ continuation_max_counter_streak=2
        (kısa toparlanma — 3+ bar olsa Major Reversal candidate olur)
      - Ana yönde yeni streak ≥ 1 (toparlanma sonrası ilk dönüş bar)
      - no_duplicate, body_size ≥ 30%

    Soft skor faktörleri (threshold ≥ 4.5 — Major Reversal'dan sıkı):
      - MSS direction main yönü onaylıyor (yapısal devam): +2.0
      - MFI delta ana yönde: +1.0
      - RSI delta ana yönde: +1.0
      - VWAP ana yönle aligned: +1.0
      - Önceki ana-yön streak ≥ 4 (güçlü trend kanıtı): +1.0
      - RCS volume_3m_ratio ≥ 1.3: +0.5
      - first_entry_missed=True (kaçırılmış trend, runner DB query): +0.5
    """
    direction = _infer_direction_from_3m(ctx)
    score = EntryTypeScore(score=0.0, direction=direction)
    if direction is None:
        score.failed_mandatory = "no_ha_direction"
        return score

    if ctx.ha_state.latest is None:
        score.failed_mandatory = "no_ha_data"
        return score

    gates: dict[str, bool] = {}
    counter_streak = _count_recent_counter_streak(ctx.ha_state, direction)

    # 2026-05-05 — Mandatory gate sonuçları (skor görünümü için her zaman
    # değerlendirilir). Erken return YOK — soft factor skoru hesaplanır,
    # sonra mandatory guard'da TAKE blocked.
    gates["15m_aligned"] = _gate_15m_alignment(ctx.ha_state, direction)

    dominant = ctx.ha_state.dominant_color_3m()
    target_color = "GREEN" if direction == Direction.BULLISH else "RED"
    gates["dominant_color_main"] = (dominant == target_color)

    gates["counter_streak_within_limit"] = (
        1 <= counter_streak <= config.continuation_max_counter_streak
    )

    new_streak = ctx.ha_state.latest.ha_streak_3m
    if direction == Direction.BULLISH:
        gates["new_direction_streak"] = new_streak >= 1
    else:
        gates["new_direction_streak"] = new_streak <= -1

    gates["no_duplicate"] = _gate_no_duplicate(
        ctx.symbol, direction, ctx.pending_pairs, ctx.open_pairs,
    )

    gates["body_size"] = _gate_body_size(ctx.ha_state, config.min_body_pct_3m)

    # Mandatory check — fail eden ilk gate'i tut, return etme.
    mandatory_keys_cont = (
        "15m_aligned", "dominant_color_main",
        "counter_streak_within_limit", "new_direction_streak",
        "no_duplicate", "body_size",
    )
    failed_mandatory_key: Optional[str] = None
    for k in mandatory_keys_cont:
        if not gates[k]:
            failed_mandatory_key = k
            break

    # Soft factors — her durumda hesaplanır (mandatory fail olsa bile)
    score_value = 0.0

    gates["mss_direction_main"] = _gate_mss_direction_alignment(
        ctx.last_mss_direction, direction,
    )
    if gates["mss_direction_main"]:
        score_value += 2.0

    gates["mfi_delta_main"] = _gate_mfi_delta_aligned(ctx.ha_state, direction)
    if gates["mfi_delta_main"]:
        score_value += 1.0

    gates["rsi_delta_main"] = _gate_rsi_delta_aligned(ctx.ha_state, direction)
    if gates["rsi_delta_main"]:
        score_value += 1.0

    gates["vwap_aligned"] = _gate_vwap_alignment(ctx.market_state, direction)
    if gates["vwap_aligned"]:
        score_value += 1.0

    main_streak_before = _count_main_trend_streak_before_pullback(
        ctx.ha_state, direction, counter_streak,
    )
    gates["main_trend_strong"] = (
        main_streak_before >= config.continuation_main_trend_min_streak
    )
    if gates["main_trend_strong"]:
        score_value += 1.0

    gates["rcs_volume_confirm"] = _gate_rcs_volume(ctx.market_state, 1.3)
    if gates["rcs_volume_confirm"]:
        score_value += 0.5

    gates["first_entry_missed"] = ctx.first_entry_missed
    if ctx.first_entry_missed:
        score_value += 0.5

    # 2026-05-05 v4 — Confluence destek faktörü (yön teyidi, aktif)
    conf_gates, conf_delta = _confluence_support(ctx, direction, config)
    gates.update(conf_gates)
    score_value += conf_delta

    score.score = score_value
    score.gate_results = gates

    # Mandatory guard: skor görünür ama mandatory fail varsa TAKE yok
    if failed_mandatory_key is not None:
        score.failed_mandatory = failed_mandatory_key
        return score

    # Build entry parameters if soft threshold passes
    if score_value >= config.continuation_threshold:
        if ctx.best_bid is None or ctx.best_ask is None:
            score.failed_mandatory = "missing_orderbook"
            return score
        sl = _structural_sl_price(direction, ctx)
        if sl is None:
            score.failed_mandatory = "missing_swing_anchor"
            return score
        entry = _marketable_entry_price(
            direction, ctx.best_bid, ctx.best_ask, config.marketable_offset_pct,
        )
        tp = _tp_price(direction, entry, sl, config.continuation_target_rr)
        score.entry_price = entry
        score.sl_price = sl
        score.tp_price = tp
        score.target_rr = config.continuation_target_rr
        score.risk_multiplier = 1.0  # Continuation = full R

    return score


# ── Tip 3: Micro Reversal score (FULL kod, DISABLED default) ──────────────


def _gate_1m_streak_new_direction(
    ha_state: HASymbolState, direction: Direction, min_abs: int,
) -> bool:
    """1m HA streak yeni yönde abs(>=) min."""
    if ha_state.latest is None:
        return False
    streak = ha_state.latest.ha_streak_1m
    if direction == Direction.BULLISH:
        return streak >= min_abs
    if direction == Direction.BEARISH:
        return streak <= -min_abs
    return False


def _gate_rsi_extreme(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """3m RSI extreme: LONG için ≤30 (oversold), SHORT için ≥70 (overbought).

    Reversal entry candidate — extreme'den dönüş bekliyor.
    """
    if ha_state.latest is None:
        return False
    rsi = ha_state.latest.ha_rsi_3m
    if direction == Direction.BULLISH:
        return rsi <= 30.0
    if direction == Direction.BEARISH:
        return rsi >= 70.0
    return False


def _gate_divergence_present(state: MarketState, direction: Direction) -> bool:
    """3m oscillator'da divergence sinyali var mı?

    Pine'dan oscillator_table.last_wt_div emit ediliyor: "BULL@<bars_ago>"
    veya "BEAR@<bars_ago>" formatında. LONG için BULL divergence aranır,
    SHORT için BEAR.
    """
    osc = state.oscillator
    if osc is None:
        return False
    div = getattr(osc, "last_wt_div", "") or ""
    if direction == Direction.BULLISH:
        return div.startswith("BULL")
    if direction == Direction.BEARISH:
        return div.startswith("BEAR")
    return False


def _gate_vwap_distant(
    state: MarketState, atr_units: float = 0.5,
) -> bool:
    """Price vs vwap_3m mesafesi ≥ atr_units × ATR.

    Mean reversion candidate: VWAP'tan uzakta = aşırı uzaklaşma
    sinyali, geri dönüş ihtimali yüksek.
    """
    sig = state.signal_table
    if sig is None or sig.vwap_3m <= 0 or sig.atr_14 <= 0 or sig.price <= 0:
        return False
    distance = abs(sig.price - sig.vwap_3m)
    return distance >= atr_units * sig.atr_14


def _score_micro_reversal(
    ctx: EntryContext, config: HANativeConfig,
) -> EntryTypeScore:
    """Tip 3 — Micro Reversal (1m mss dip/tepe avcılığı).

    DISABLED default (operatör profili: WR + perfectionist). Schema +
    kod hazır; `config.micro_reversal_enabled=True` ile aktif olur.
    Dataset birikince + Pass 3 GBT analizinde re-enable kararı.

    Mandatory gates (fail = score 0):
      - HA color clear (3m DOJI değil) → direction
      - 1m streak yeni yönde ≥ 2 (whipsaw guard)
      - 1m MSS direction yeni yön (Pine'dan)
      - 3m RSI extreme VEYA divergence sinyali (en az biri)
      - no_duplicate, body_size ≥ 30%

    Soft skor faktörleri (threshold ≥ 4.5):
      - 1m MSS güçlü break (last_mss == direction): +2.0
      - 3m RSI/MFI divergence: +2.0
      - 3m RSI extreme (≤30 long / ≥70 short): +1.5
      - 1m streak ≥ 3 yeni yön: +1.0
      - VWAP'ten ≥ 0.5 ATR uzakta: +1.0
      - 15m hâlâ eski yönlü (chop guard): +0.5

    Plan: 0.7R RR, 0.5 risk multiplier (yarı R = $12.5).
    """
    direction = _infer_direction_from_3m(ctx)
    score = EntryTypeScore(score=0.0, direction=direction)

    # DISABLED check (operatör profili default)
    if not config.micro_reversal_enabled:
        score.failed_mandatory = "micro_reversal_disabled"
        return score

    if direction is None:
        score.failed_mandatory = "no_ha_direction"
        return score

    gates: dict[str, bool] = {}

    # 2026-05-05 — Mandatory gate sonuçları (skor görünümü için her zaman
    # değerlendirilir). Erken return YOK — soft skor hesabı sonrası
    # mandatory guard ile TAKE blocked.
    gates["1m_streak_new"] = _gate_1m_streak_new_direction(
        ctx.ha_state, direction, 2,
    )
    gates["1m_mss_direction"] = _gate_mss_direction_alignment(
        ctx.last_mss_direction, direction,
    )

    rsi_extreme = _gate_rsi_extreme(ctx.ha_state, direction)
    div_present = _gate_divergence_present(ctx.market_state, direction)
    gates["rsi_extreme_or_divergence"] = rsi_extreme or div_present

    gates["no_duplicate"] = _gate_no_duplicate(
        ctx.symbol, direction, ctx.pending_pairs, ctx.open_pairs,
    )
    gates["body_size"] = _gate_body_size(ctx.ha_state, config.min_body_pct_3m)

    mandatory_keys_micro = (
        "1m_streak_new", "1m_mss_direction", "rsi_extreme_or_divergence",
        "no_duplicate", "body_size",
    )
    failed_mandatory_key: Optional[str] = None
    for k in mandatory_keys_micro:
        if not gates[k]:
            failed_mandatory_key = k
            break

    # Soft factors — her durumda hesaplanır
    score_value = 0.0

    gates["1m_mss_strong"] = (ctx.last_mss_direction == direction)
    if gates["1m_mss_strong"]:
        score_value += 2.0

    gates["divergence"] = div_present
    if div_present:
        score_value += 2.0

    gates["rsi_extreme"] = rsi_extreme
    if rsi_extreme:
        score_value += 1.5

    gates["1m_streak_strong"] = _gate_1m_streak_new_direction(
        ctx.ha_state, direction, 3,
    )
    if gates["1m_streak_strong"]:
        score_value += 1.0

    gates["vwap_distant"] = _gate_vwap_distant(ctx.market_state, 0.5)
    if gates["vwap_distant"]:
        score_value += 1.0

    # 15m hâlâ eski yönlü → reversal candidate güçlü
    prev_dir = (
        Direction.BEARISH if direction == Direction.BULLISH
        else Direction.BULLISH
    )
    gates["15m_aligned_old"] = _gate_15m_alignment(ctx.ha_state, prev_dir)
    if gates["15m_aligned_old"]:
        score_value += 0.5

    # 2026-05-05 v4 — Confluence destek faktörü (yön teyidi, aktif)
    conf_gates, conf_delta = _confluence_support(ctx, direction, config)
    gates.update(conf_gates)
    score_value += conf_delta

    score.score = score_value
    score.gate_results = gates

    # Mandatory guard — skor görünür ama mandatory fail varsa TAKE yok
    if failed_mandatory_key is not None:
        score.failed_mandatory = failed_mandatory_key
        return score

    # Build entry parameters if soft threshold passes
    if score_value >= config.micro_reversal_threshold:
        if ctx.best_bid is None or ctx.best_ask is None:
            score.failed_mandatory = "missing_orderbook"
            return score
        sl = _structural_sl_price(direction, ctx)
        if sl is None:
            score.failed_mandatory = "missing_swing_anchor"
            return score
        entry = _marketable_entry_price(
            direction, ctx.best_bid, ctx.best_ask, config.marketable_offset_pct,
        )
        tp = _tp_price(direction, entry, sl, config.micro_reversal_target_rr)
        score.entry_price = entry
        score.sl_price = sl
        score.tp_price = tp
        score.target_rr = config.micro_reversal_target_rr
        score.risk_multiplier = config.micro_reversal_risk_multiplier  # 0.5

    return score


# ── Main dispatcher ────────────────────────────────────────────────────────


def evaluate_entry(
    ctx: EntryContext, config: HANativeConfig,
) -> EntryDecision:
    """Run 3 entry tipini paralel skor; threshold geçen + en yüksek skoru kazandır.

    Hard short-circuits:
      - 3m HA direction belirsiz (DOJI/empty) → NO_SETUP

    Tüm 3 skor + gate_results her zaman EntryDecision'a yazılır
    (decision_log audit için tam görünürlük).
    """
    direction_3m = _infer_direction_from_3m(ctx)
    if direction_3m is None:
        return EntryDecision(
            decision="NO_SETUP",
            reason="no_ha_direction",
            gate_results={"major_reversal": {}, "continuation": {}, "micro_reversal": {}},
        )

    # Skor 3 tipi
    s_major = _score_major_reversal(ctx, config)
    s_cont = _score_continuation(ctx, config)
    s_micro = _score_micro_reversal(ctx, config)

    all_gate_results = {
        "major_reversal": s_major.gate_results,
        "continuation": s_cont.gate_results,
        "micro_reversal": s_micro.gate_results,
    }

    # Aday filtresi: threshold geçen + entry params hazır
    candidates: list[tuple[str, EntryTypeScore, float]] = [
        ("major_reversal", s_major, config.major_reversal_threshold),
        ("continuation", s_cont, config.continuation_threshold),
        ("micro_reversal", s_micro, config.micro_reversal_threshold),
    ]
    valid = [
        (name, score)
        for name, score, threshold in candidates
        if score.score >= threshold and score.entry_price is not None
    ]

    if not valid:
        # En yüksek skoru olan reject reason için
        top_name, top_score, top_threshold = max(
            candidates, key=lambda c: c[1].score,
        )
        if top_score.failed_mandatory:
            reason = (
                f"{top_name}_mandatory_failed:"
                f"{top_score.failed_mandatory}"
            )
        else:
            reason = (
                f"{top_name}_below_threshold:"
                f"{top_score.score:.2f}<{top_threshold:.2f}"
            )
        return EntryDecision(
            decision="REJECT",
            direction=direction_3m,
            entry_path=None,
            reason=f"all_below_threshold:top={reason}",
            gate_results=all_gate_results,
            major_reversal_score=s_major.score,
            continuation_score=s_cont.score,
            micro_reversal_score=s_micro.score,
        )

    # En yüksek skoru olan kazanır (tie → major_reversal preference; iteration order)
    winner_name, winner_score = max(valid, key=lambda c: c[1].score)
    decision_label = (
        "TAKE_LONG" if winner_score.direction == Direction.BULLISH
        else "TAKE_SHORT"
    )
    return EntryDecision(
        decision=decision_label,
        direction=winner_score.direction,
        entry_path=winner_name,
        reason=f"{winner_name}_passed:score={winner_score.score:.2f}",
        gate_results=all_gate_results,
        major_reversal_score=s_major.score,
        continuation_score=s_cont.score,
        micro_reversal_score=s_micro.score,
        suggested_entry_price=winner_score.entry_price,
        suggested_sl_price=winner_score.sl_price,
        suggested_tp_price=winner_score.tp_price,
        target_rr=winner_score.target_rr,
        risk_multiplier=winner_score.risk_multiplier,
    )
