"""VMC (Yol B) runtime state manager — per-symbol in-memory history buffer.

Yol B (HA Strategy, 2026-05-05) entry/exit doctrine için 5m + 15m HA + osilatör
zaman serisi. Slope/turn/break helper'ları VWAP, WT2, MFI üzerinde çalışır.

Architecture:
    VMCStateRegistry  (singleton in BotContext)
      └── VMCSymbolState  (one per watched symbol)
            ├── history: deque[VMCSnapshot]  (maxlen ~60)
            └── derived properties:
                  - vwap_slope_dir(lookback)  — UP / DOWN / FLAT
                  - wt2_turning_dir(lookback) — UP / DOWN / FLAT (lokal turn)
                  - mfi_5m_delta_dir / rsi_5m_delta_dir — UP / DOWN / MIXED
                  - color_flip_5m / color_flip_15m
                  - ha_close_break_long(N) / ha_close_break_short(N)
                  - dominant_color_5m / dominant_color_15m

Karar etkisiz; sadece runtime decision mekanizması. Bot restart'ta sıfırlanır
(in-memory only — DB persistence ayrı katman).

Usage:
    state = vmc_registry.update(symbol, market_state, timestamp)
    if state.vwap_slope_dir(2) == "UP" and state.latest.ha_color_5m == "GREEN":
        # entry gate green
        ...
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.data.models import MarketState


# 3-bar delta sinyalleri için minimum mutlak fark eşiği (osilatör birimleri).
# 0.5 → küçük floor; "değer aynı kaldı" durumunu MIXED'e düşürür.
DEFAULT_MIN_DELTA = 0.5

# Per-symbol history buffer maksimum uzunluğu. 5m TF'de 60 bar = 5 saat;
# dominant color 30-bar window + close-break son 5 bar için yeterli tampon.
DEFAULT_HISTORY_MAXLEN = 60

# Dominant color analizi default penceresi.
DEFAULT_DOMINANT_WINDOW = 30
DEFAULT_DOMINANT_THRESHOLD = 0.6

# VWAP slope default lookback — operatör 2026-05-05: "2 ardışık 5m bar".
DEFAULT_VWAP_SLOPE_LOOKBACK = 2

# WT2 turning default lookback — lokal dip/tepe sonrası 2-bar geri dönüş.
DEFAULT_WT_TURN_LOOKBACK = 2

# HA close break default lookback — exit guard "close son 5 bar üst/altını kırma".
DEFAULT_HA_CLOSE_LOOKBACK = 5


@dataclass(frozen=True)
class VMCSnapshot:
    """Tek bir cycle'ın HA + osilatör + VWAP + price değerlerinin immutable snapshot'ı.

    MarketState'ten `from_market_state()` ile inşa edilir. Frozen — hash'lenebilir
    ve test'lerde değer karşılaştırması güvenli.
    """

    timestamp: datetime
    # 5m HA — Yol B primary entry TF
    ha_color_5m: str = ""
    ha_streak_5m: int = 0
    ha_no_lower_shadow_5m: bool = False
    ha_no_upper_shadow_5m: bool = False
    ha_body_pct_5m: float = 0.0
    ema200_5m: float = 0.0
    # 15m HA — Yol B soft anchor (hold-extension)
    ha_color_15m: str = ""
    ha_streak_15m: int = 0
    # HA-MFI / HA-RSI multi-TF (oscillator)
    ha_mfi_5m: float = 0.0
    ha_mfi_15m: float = 0.0
    ha_rsi_5m: float = 50.0
    ha_rsi_15m: float = 50.0
    # Oscillator core (WT/VMC)
    wt1: float = 0.0
    wt2: float = 0.0
    wt_vwap_fast: float = 0.0   # = wt1 - wt2 (Yol B "VWAP" slope sinyali)
    # Pine session-anchored VWAP (5m) — confluence reference
    vwap_5m: float = 0.0
    vwap_5m_upper: float = 0.0
    vwap_5m_lower: float = 0.0
    # Volume pulse — exit confirm hacimli kapanış
    volume_5m: float = 0.0
    volume_5m_ratio: float = 1.0
    # Bar close price — exit "close son N bar üst/altını kırdı" gate'i için
    price: float = 0.0

    @classmethod
    def from_market_state(
        cls, market_state: MarketState, timestamp: datetime,
    ) -> "VMCSnapshot":
        """MarketState'in signal_table + oscillator alanlarından VMCSnapshot inşa et."""
        sig = market_state.signal_table
        osc = market_state.oscillator
        return cls(
            timestamp=timestamp,
            ha_color_5m=sig.ha_color_5m,
            ha_streak_5m=sig.ha_streak_5m,
            ha_no_lower_shadow_5m=sig.ha_no_lower_shadow_5m,
            ha_no_upper_shadow_5m=sig.ha_no_upper_shadow_5m,
            ha_body_pct_5m=sig.ha_body_pct_5m,
            ema200_5m=sig.ema200_5m,
            ha_color_15m=sig.ha_color_15m,
            ha_streak_15m=sig.ha_streak_15m,
            ha_mfi_5m=osc.ha_mfi_5m,
            ha_mfi_15m=osc.ha_mfi_15m,
            ha_rsi_5m=osc.ha_rsi_5m,
            ha_rsi_15m=osc.ha_rsi_15m,
            wt1=osc.wt1,
            wt2=osc.wt2,
            wt_vwap_fast=osc.wt_vwap_fast,
            vwap_5m=sig.vwap_5m,
            vwap_5m_upper=sig.vwap_5m_upper,
            vwap_5m_lower=sig.vwap_5m_lower,
            volume_5m=sig.volume_5m,
            volume_5m_ratio=sig.volume_5m_ratio,
            price=sig.price,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Pure helper functions — slope / turn / delta / break / dominant
# ──────────────────────────────────────────────────────────────────────────────

def _slope_dir(values: list[float], lookback: int) -> str:
    """Son `lookback` bar boyunca monotonic slope yönü.

    Args:
        values: time-series (eski → yeni); en az `lookback` element gerekir.
        lookback: kaç bar üzerinden slope hesaplanır (operatör Yol B = 2).

    Returns:
        "UP"   — son `lookback` bar boyunca strict monotonic up (her step >).
        "DOWN" — strict monotonic down.
        "FLAT" — yetersiz history veya monotonic değil (zigzag/eşit).
    """
    if len(values) < lookback:
        return "FLAT"
    recent = values[-lookback:]
    if all(recent[i] < recent[i + 1] for i in range(len(recent) - 1)):
        return "UP"
    if all(recent[i] > recent[i + 1] for i in range(len(recent) - 1)):
        return "DOWN"
    return "FLAT"


def _turning_dir(values: list[float], lookback: int) -> str:
    """Lokal dip/tepe sonrası dönüş yönü — WT2 turning detection.

    Args:
        values: time-series; en az `lookback + 1` element gerekir.
        lookback: dönüş öncesi inceleme penceresi (operatör Yol B = 2).

    Returns:
        "UP"   — lokal dip yapıp yukarı dönüş: önceki `lookback` bar düşüyordu,
                 son bar yükseldi (LONG entry signal candidate).
        "DOWN" — lokal tepe yapıp aşağı dönüş (SHORT entry signal candidate).
        "FLAT" — yetersiz history veya turn pattern'i yok.
    """
    if len(values) < lookback + 1:
        return "FLAT"
    pre_turn = values[-(lookback + 1):-1]   # önceki lookback bar
    last = values[-1]
    pivot = pre_turn[-1]                    # turn'den hemen önceki bar
    # Lokal dip → up turn: pre-turn azalan, son bar pivot'tan yükseldi
    pre_turn_descending = all(pre_turn[i] >= pre_turn[i + 1] for i in range(len(pre_turn) - 1))
    pre_turn_ascending  = all(pre_turn[i] <= pre_turn[i + 1] for i in range(len(pre_turn) - 1))
    if pre_turn_descending and last > pivot:
        return "UP"
    if pre_turn_ascending and last < pivot:
        return "DOWN"
    return "FLAT"


def _delta_dir(values: list[float], min_delta: float = DEFAULT_MIN_DELTA) -> str:
    """3-bar delta yönü: UP / DOWN / MIXED (HA-native ile aynı semantik)."""
    if len(values) < 3:
        return "MIXED"
    a, b, c = values[-3], values[-2], values[-1]
    if c > b > a and (c - a) >= min_delta:
        return "UP"
    if c < b < a and (a - c) >= min_delta:
        return "DOWN"
    return "MIXED"


def _color_flip(prev_color: str, curr_color: str) -> Optional[str]:
    """İki ardışık renk arasında flip varsa yön döndür, yoksa None."""
    if prev_color == "GREEN" and curr_color == "RED":
        return "GREEN_TO_RED"
    if prev_color == "RED" and curr_color == "GREEN":
        return "RED_TO_GREEN"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-symbol state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class VMCSymbolState:
    """Tek sembolün VMC history buffer'ı + türetilmiş metrikler.

    Cycle başında `update(snapshot)` çağrılır. Strateji modülleri property'leri
    okur (lazy hesaplama, side-effect yok).
    """

    symbol: str
    history: deque = field(
        default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_MAXLEN)
    )

    def update(self, snapshot: VMCSnapshot) -> None:
        self.history.append(snapshot)

    @property
    def latest(self) -> Optional[VMCSnapshot]:
        if not self.history:
            return None
        return self.history[-1]

    @property
    def previous(self) -> Optional[VMCSnapshot]:
        if len(self.history) < 2:
            return None
        return self.history[-2]

    # ── VWAP slope (Yol B entry gate 1: VWAP 0'a yaklaşıyor) ────────────────

    def vwap_slope_dir(
        self, lookback: int = DEFAULT_VWAP_SLOPE_LOOKBACK,
    ) -> str:
        """`wt_vwap_fast` (= wt1 - wt2) son `lookback` bar slope yönü.

        Operatör 2026-05-05: "2 ardışık 5m verisinde negatiften 0'a yaklaşma"
        → LONG candidate. Pozitiften 0'a azalma → SHORT candidate.
        """
        return _slope_dir([s.wt_vwap_fast for s in self.history], lookback)

    @property
    def vwap_value(self) -> Optional[float]:
        """Mevcut wt_vwap_fast değeri (Yol B gate'inde sign check için)."""
        if not self.latest:
            return None
        return self.latest.wt_vwap_fast

    # ── WT2 turning (Yol B entry gate 3: momentum dönüyor) ─────────────────

    def wt2_turning_dir(
        self, lookback: int = DEFAULT_WT_TURN_LOOKBACK,
    ) -> str:
        """WT2 lokal dip/tepe sonrası dönüş yönü.

        Operatör 2026-05-05: "momentum dönüyorsa yukarı + etki" → LONG.
        Lokal dip + sonraki bar yukarı → "UP". Tepe + sonraki bar aşağı → "DOWN".
        """
        return _turning_dir([s.wt2 for s in self.history], lookback)

    # ── MFI / RSI 3-bar delta (Yol B entry gate 2: money flow artıyor) ─────

    @property
    def mfi_5m_delta_dir(self) -> str:
        """5m HA-MFI 3-bar delta yönü (UP/DOWN/MIXED)."""
        return _delta_dir([s.ha_mfi_5m for s in self.history])

    @property
    def rsi_5m_delta_dir(self) -> str:
        """5m HA-RSI 3-bar delta yönü."""
        return _delta_dir([s.ha_rsi_5m for s in self.history])

    @property
    def mfi_5m_delta_value(self) -> Optional[float]:
        if len(self.history) < 3:
            return None
        recent = list(self.history)[-3:]
        return recent[-1].ha_mfi_5m - recent[0].ha_mfi_5m

    @property
    def rsi_5m_delta_value(self) -> Optional[float]:
        if len(self.history) < 3:
            return None
        recent = list(self.history)[-3:]
        return recent[-1].ha_rsi_5m - recent[0].ha_rsi_5m

    # ── HA color flip detection ─────────────────────────────────────────────

    @property
    def color_flip_5m(self) -> Optional[str]:
        if len(self.history) < 2:
            return None
        return _color_flip(self.previous.ha_color_5m, self.latest.ha_color_5m)

    @property
    def color_flip_15m(self) -> Optional[str]:
        if len(self.history) < 2:
            return None
        return _color_flip(self.previous.ha_color_15m, self.latest.ha_color_15m)

    # ── HA close break (Yol B exit guard: close son N bar üst/altını kırdı) ─

    def ha_close_break_long(
        self, lookback: int = DEFAULT_HA_CLOSE_LOOKBACK,
    ) -> bool:
        """LONG pozisyonda exit guard: son bar close < min(önceki N bar close)?

        Operatör 2026-05-05: "mum kendinden önceki 3-5 mumun altına iniyorsa
        anlık sıkıntılı". True → trend bozuluyor (exit confirm candidate).
        """
        if len(self.history) < lookback + 1:
            return False
        last_close = self.latest.price
        prior_closes = [s.price for s in list(self.history)[-(lookback + 1):-1]]
        if not prior_closes or last_close <= 0:
            return False
        return last_close < min(prior_closes)

    def ha_close_break_short(
        self, lookback: int = DEFAULT_HA_CLOSE_LOOKBACK,
    ) -> bool:
        """SHORT pozisyonda exit guard: son bar close > max(önceki N bar close)?"""
        if len(self.history) < lookback + 1:
            return False
        last_close = self.latest.price
        prior_closes = [s.price for s in list(self.history)[-(lookback + 1):-1]]
        if not prior_closes or last_close <= 0:
            return False
        return last_close > max(prior_closes)

    # ── Dominant color analysis (window-based baskınlık) ────────────────────

    def dominant_color_5m(
        self,
        window: int = DEFAULT_DOMINANT_WINDOW,
        threshold: float = DEFAULT_DOMINANT_THRESHOLD,
    ) -> Optional[str]:
        """Son `window` snapshot'ta hangi 5m HA rengi baskın."""
        return self._dominant_color(
            [s.ha_color_5m for s in self.history], window, threshold,
        )

    def dominant_color_15m(
        self,
        window: int = DEFAULT_DOMINANT_WINDOW,
        threshold: float = DEFAULT_DOMINANT_THRESHOLD,
    ) -> Optional[str]:
        return self._dominant_color(
            [s.ha_color_15m for s in self.history], window, threshold,
        )

    @staticmethod
    def _dominant_color(
        colors: list[str], window: int, threshold: float,
    ) -> Optional[str]:
        if not colors:
            return None
        recent = colors[-window:]
        if len(recent) < max(1, window // 2):
            return None
        green = sum(1 for c in recent if c == "GREEN")
        red = sum(1 for c in recent if c == "RED")
        total = len(recent)
        if green / total >= threshold:
            return "GREEN"
        if red / total >= threshold:
            return "RED"
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class VMCStateRegistry:
    """Per-symbol VMC state container — BotContext'e bağlanır.

    Bot restart'ta sıfırlanır (in-memory only). Startup'ta `seed_from_klines()`
    ile Bybit kline'larından backfill edilebilir.
    """

    states: dict[str, VMCSymbolState] = field(default_factory=dict)

    def update(
        self, symbol: str, market_state: MarketState, timestamp: datetime,
    ) -> VMCSymbolState:
        if symbol not in self.states:
            self.states[symbol] = VMCSymbolState(symbol=symbol)
        snapshot = VMCSnapshot.from_market_state(market_state, timestamp)
        self.states[symbol].update(snapshot)
        return self.states[symbol]

    def get(self, symbol: str) -> Optional[VMCSymbolState]:
        return self.states.get(symbol)

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self.states.clear()
        else:
            self.states.pop(symbol, None)
