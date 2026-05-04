"""HA-native entry decision gate.

Bot her cycle her sembol için entry gate'i evaluate eder. Tüm şartlar
geçerse `EntryDecision(decision="TAKE_LONG"|"TAKE_SHORT")` döner. Aksi halde
REJECT veya NO_SETUP.

Spec (operatör 2026-05-04 revize):
  Ana sinyal: HA 3m current color + 15m alignment (sürekli işleme devam doktrini).
  Renk değişimi → flip; renk devam ediyorsa entry/hold.

Gate'ler (8 adet — ek konfirmasyonlar):
  1. MSS density: son M bar içinde <= K MSS (whipsaw guard)
  2. HA 3m streak abs(>=) min trend yönünde
  3. HA-MFI 3-bar delta dir trend yönünde (UP/DOWN strict monotonic)
  4. HA-RSI 3-bar delta dir trend yönünde
  5. Son 2 HA mum (3m + 1m) trend yönünde aynı renk
  6. HA 15m current color == trend yönü (multi-TF anchor — KRİTİK)
  7. Aynı sembol+yön pending/open yok
  8. Body %3m >= min (momentum candle, doji-like skip)

Yön belirleme: HA 3m current color (GREEN → BULLISH, RED → BEARISH, DOJI → None).
ADX + fresh_mss zorunluluğu KALDIRILDI (operatör revize) — HA renk ana
sinyal, sürekli işleme devam felsefesi. Kaldırılan helper'lar (`_gate_adx`,
`_gate_fresh_mss`) decision_log audit için import edilebilir kalır.

Passive support (gate değil, kayıt + opsiyonel sizing modifier):
  - Confluence skor (mevcut 5-pillar)
  - 4H HA renk (Faz 1 journal-only)
  - EMA200 3m position (Faz 1 journal-only)

Entry params (output):
  - entry_price: marketable limit (best_ask × (1+offset) long, best_bid × (1-offset) short)
  - sl_price: structural swing (last_swing_low/high) + per-symbol floor (caller)
  - tp_price: entry ± 1.0R (scalp doktrin — operatör 2026-05-04)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Direction, MarketState
from src.strategy.ha_state import HASymbolState


# ── Config knobs (operatör seçimi 2026-05-04) ──────────────────────────────


@dataclass(frozen=True)
class HANativeConfig:
    """Default knob values for the HA-native entry gate.

    Operatör güveni 2026-05-04: ben seçtim, operatör ileride değiştirir.

    `adx_threshold` ve `fresh_mss_max_bars` 2026-05-04 revize ile gate'ten
    çıkarıldı (HA renk ana sinyal). Knob'lar audit / decision_log için
    EntryContext'e propagate edilebilir; ileride re-add edilirse default
    None'a düşer.
    """

    # Structure gate
    mss_density_window: int = 6           # Whipsaw guard window
    mss_density_max: int = 2              # 3+ MSS = chop, skip

    # HA continuity gates
    min_streak_3m: int = 2                # 1-mum noise filter
    min_delta_3bar: float = 0.5           # MFI/RSI delta floor (passed to ha_state)
    min_body_pct_3m: float = 30.0         # Doji-like skip (momentum candle gate)

    # Passive support (sizing modifier, gate değil)
    confluence_passive_threshold: float = 5.0

    # Entry execution
    marketable_offset_pct: float = 0.0005  # 5 bps marketable limit slippage cap
    entry_cycle_timeout: int = 1          # 1 cycle fill yoksa cancel
    target_rr_ratio: float = 1.0          # Scalp doktrin (1.0R TP)

    # Deprecated — operatör 2026-05-04 revize ile gate'ten çıktı.
    # Kept for decision_log audit + future re-add. None değeri "use yok".
    adx_threshold: Optional[float] = None
    fresh_mss_max_bars: Optional[int] = None


# ── Entry context + decision dataclasses ───────────────────────────────────


@dataclass(frozen=True)
class EntryContext:
    """All inputs evaluate_entry() needs for one symbol cycle.

    Caller (runner) assembles this from BotContext caches + market_state +
    ha_state. Frozen for safety — mutation breaks gate determinism.
    """

    symbol: str
    market_state: MarketState
    ha_state: HASymbolState

    # ADX triad (computed by runner via Wilder ADX(14) on 3m candles)
    adx_3m: Optional[float] = None
    plus_di_3m: Optional[float] = None
    minus_di_3m: Optional[float] = None

    # Recent MSS history
    last_mss_bar: Optional[int] = None
    last_mss_direction: Optional[Direction] = None
    bars_since_last_mss: Optional[int] = None
    mss_count_recent: int = 0          # MSS count in last `mss_density_window`

    # Order book for marketable limit
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

    # Pending + open pairs for cross-cycle deduplication
    pending_pairs: frozenset = frozenset()  # {(symbol, Direction)}
    open_pairs: frozenset = frozenset()

    # Swing extremes for SL anchor (caller passes from analysis layer)
    last_swing_low: Optional[float] = None
    last_swing_high: Optional[float] = None


@dataclass
class EntryDecision:
    """Output of evaluate_entry(). Always populated; reason explains the path."""

    decision: str                       # "TAKE_LONG" / "TAKE_SHORT" / "REJECT" / "NO_SETUP"
    direction: Optional[Direction] = None
    reason: str = ""
    gate_results: dict[str, bool] = field(default_factory=dict)
    suggested_entry_price: Optional[float] = None
    suggested_sl_price: Optional[float] = None
    suggested_tp_price: Optional[float] = None
    notes: str = ""

    @property
    def is_take(self) -> bool:
        return self.decision in ("TAKE_LONG", "TAKE_SHORT")


# ── Direction inference ────────────────────────────────────────────────────


def _infer_trend_direction(ctx: EntryContext) -> Optional[Direction]:
    """Trend yönü: HA 3m current color (operatör 2026-05-04 revize).

    Returns:
      BULLISH — son 3m HA mum GREEN
      BEARISH — son 3m HA mum RED
      None    — DOJI / empty (no clean direction)

    Diğer gate'ler bu yöne karşı evaluate edilir. MSS yönü artık
    direction kaynağı değil; sadece audit için decision_log'a yazılır.
    """
    if ctx.ha_state.latest is None:
        return None
    color = ctx.ha_state.latest.ha_color_3m
    if color == "GREEN":
        return Direction.BULLISH
    if color == "RED":
        return Direction.BEARISH
    return None  # DOJI / empty


# ── Gate helpers ───────────────────────────────────────────────────────────


def _gate_adx(adx_3m: Optional[float], threshold: float) -> bool:
    """Gate 1: ADX 3m >= threshold."""
    if adx_3m is None:
        return False
    return adx_3m >= threshold


def _gate_fresh_mss(
    bars_since: Optional[int], max_bars: int,
) -> bool:
    """Gate 2: Last MSS within the last `max_bars` bars."""
    if bars_since is None:
        return False
    return 0 <= bars_since <= max_bars


def _gate_mss_density(count: int, max_count: int) -> bool:
    """Gate 3: Recent-window MSS count <= max_count (whipsaw guard)."""
    return count <= max_count


def _gate_streak_3m(
    ha_state: HASymbolState, direction: Direction, min_abs: int,
) -> bool:
    """Gate 4: |HA 3m streak| >= min, signed correctly for the direction.

    LONG → streak > 0 (green run); SHORT → streak < 0 (red run).
    """
    if ha_state.latest is None:
        return False
    streak = ha_state.latest.ha_streak_3m
    if direction == Direction.BULLISH:
        return streak >= min_abs
    if direction == Direction.BEARISH:
        return streak <= -min_abs
    return False


def _gate_mfi_delta(
    ha_state: HASymbolState, direction: Direction, min_abs_value: float,
) -> bool:
    """Gate 5: HA-MFI 3m 3-bar delta direction matches trend.

    LONG needs UP delta; SHORT needs DOWN delta. Strict monotonic with
    abs(delta) >= floor enforced by ha_state._delta_dir().
    """
    direction_str = ha_state.mfi_3m_delta_dir
    if direction == Direction.BULLISH:
        return direction_str == "UP"
    if direction == Direction.BEARISH:
        return direction_str == "DOWN"
    return False


def _gate_rsi_delta(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Gate 6: HA-RSI 3m 3-bar delta direction matches trend."""
    direction_str = ha_state.rsi_3m_delta_dir
    if direction == Direction.BULLISH:
        return direction_str == "UP"
    if direction == Direction.BEARISH:
        return direction_str == "DOWN"
    return False


def _gate_two_bar_color(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Gate: Last 2 HA bars (3m + 1m) trend yönünde aynı renk.

    Doji veya empty color → fail (no clean signal).
    """
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


def _gate_15m_alignment(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Gate: HA 15m current color == trend direction (multi-TF anchor).

    KRİTİK gate (operatör 2026-05-04 revize): 3m yön sinyali HTF onayıyla
    desteklenmeli. 15m DOJI veya ters yön → fail. Bu, scalp tetiğini
    HTF kırılımı yokken bloklar; 15m döndüğünde flip-side alanı açılır.
    """
    if ha_state.latest is None:
        return False
    target = "GREEN" if direction == Direction.BULLISH else "RED"
    return ha_state.latest.ha_color_15m == target


def _gate_dominant_color_alignment(
    ha_state: HASymbolState, direction: Direction,
) -> bool:
    """Gate: 3m HA dominant color son 30 barda trend yönüne ters DEĞİL.

    Operatör 2026-05-04: "5 yeşil mum varsa kırmızıyı bekle short için —
    düşüş ara düzeltme olabilir". 3m HA mum şu an kırmızı (SHORT direction)
    olsa bile, son 30 bar yeşil dominant ise = ara düzeltme = short skip.

    Liberal yorum (default):
      LONG için: dominant_3m != "RED" (yukarı baskın veya balanced OK)
      SHORT için: dominant_3m != "GREEN" (aşağı baskın veya balanced OK)

    Balanced (None) durumunda OK — operatör "biraz daha verilerle destekleniyorsa"
    dedi; balanced = ters baskı yok = entry OK. Strict'e çevirmek istersek:
    dominant_3m == direction'ın renk ekvivalenti zorunlu yaparız.

    15m destekleyici (operatör spec) ayrıca `_gate_15m_alignment` ile zaten
    current color check ediliyor; 15m dominant audit için decision_log'a yazılır.
    """
    dominant_3m = ha_state.dominant_color_3m()
    if dominant_3m is None:
        return True  # balanced = ters baskı yok = OK
    opposite = "RED" if direction == Direction.BULLISH else "GREEN"
    return dominant_3m != opposite


def _gate_no_duplicate(
    symbol: str,
    direction: Direction,
    pending: frozenset,
    open_set: frozenset,
) -> bool:
    """Gate 8: Aynı sembol+yön pending ya da open değil."""
    pair = (symbol, direction)
    return pair not in pending and pair not in open_set


def _gate_body_size(
    ha_state: HASymbolState, min_pct: float,
) -> bool:
    """Auxiliary gate: HA 3m body % >= min (momentum candle, not doji-like)."""
    if ha_state.latest is None:
        return False
    return ha_state.latest.ha_body_pct_3m >= min_pct


# ── Pricing helpers ────────────────────────────────────────────────────────


def _marketable_entry_price(
    direction: Direction, best_bid: float, best_ask: float, offset_pct: float,
) -> float:
    """LONG: ask × (1+offset); SHORT: bid × (1-offset)."""
    if direction == Direction.BULLISH:
        return best_ask * (1.0 + offset_pct)
    return best_bid * (1.0 - offset_pct)


def _structural_sl_price(direction: Direction, ctx: EntryContext) -> Optional[float]:
    """LONG → last_swing_low; SHORT → last_swing_high. None if missing."""
    if direction == Direction.BULLISH:
        return ctx.last_swing_low
    return ctx.last_swing_high


def _tp_price(
    direction: Direction, entry: float, sl: float, rr: float,
) -> float:
    """entry ± rr × sl_distance."""
    sl_distance = abs(entry - sl)
    if direction == Direction.BULLISH:
        return entry + rr * sl_distance
    return entry - rr * sl_distance


# ── Main evaluator ─────────────────────────────────────────────────────────


def evaluate_entry(
    ctx: EntryContext, config: HANativeConfig,
) -> EntryDecision:
    """Run the 8-condition gate; return a decision.

    Hard short-circuit: missing direction signal = NO_SETUP (no MSS).
    Otherwise run every gate, collect results, build the decision.

    Note: gate evaluation is NOT short-circuited mid-list — all gates run so
    the caller sees every result in `gate_results` for decision_log audit.
    """
    direction = _infer_trend_direction(ctx)
    if direction is None:
        return EntryDecision(
            decision="NO_SETUP",
            reason="no_ha_direction",
            gate_results={},
        )

    # Run all 9 gates; collect results. Operatör 2026-05-04: ADX + fresh_mss
    # gate'ten çıkarıldı; 15m_alignment + dominant_color_alignment eklendi.
    results: dict[str, bool] = {
        "mss_density": _gate_mss_density(
            ctx.mss_count_recent, config.mss_density_max,
        ),
        "streak_3m": _gate_streak_3m(
            ctx.ha_state, direction, config.min_streak_3m,
        ),
        "mfi_delta": _gate_mfi_delta(
            ctx.ha_state, direction, config.min_delta_3bar,
        ),
        "rsi_delta": _gate_rsi_delta(ctx.ha_state, direction),
        "two_bar_color": _gate_two_bar_color(ctx.ha_state, direction),
        "fifteen_min_alignment": _gate_15m_alignment(ctx.ha_state, direction),
        "dominant_color_alignment": _gate_dominant_color_alignment(
            ctx.ha_state, direction,
        ),
        "no_duplicate": _gate_no_duplicate(
            ctx.symbol, direction, ctx.pending_pairs, ctx.open_pairs,
        ),
        "body_size": _gate_body_size(ctx.ha_state, config.min_body_pct_3m),
    }

    if not all(results.values()):
        # Find the first failed gate for a human-readable reason.
        first_failed = next(name for name, ok in results.items() if not ok)
        return EntryDecision(
            decision="REJECT",
            direction=direction,
            reason=f"gate_failed:{first_failed}",
            gate_results=results,
        )

    # All gates passed — assemble entry parameters.
    if ctx.best_bid is None or ctx.best_ask is None:
        return EntryDecision(
            decision="REJECT",
            direction=direction,
            reason="missing_orderbook",
            gate_results=results,
        )
    sl_price = _structural_sl_price(direction, ctx)
    if sl_price is None:
        return EntryDecision(
            decision="REJECT",
            direction=direction,
            reason="missing_swing_anchor",
            gate_results=results,
        )

    entry_price = _marketable_entry_price(
        direction, ctx.best_bid, ctx.best_ask, config.marketable_offset_pct,
    )
    tp_price = _tp_price(direction, entry_price, sl_price, config.target_rr_ratio)

    decision_label = "TAKE_LONG" if direction == Direction.BULLISH else "TAKE_SHORT"
    return EntryDecision(
        decision=decision_label,
        direction=direction,
        reason="all_gates_passed",
        gate_results=results,
        suggested_entry_price=entry_price,
        suggested_sl_price=sl_price,
        suggested_tp_price=tp_price,
    )
