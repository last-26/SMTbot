"""Heikin Ashi runtime state manager — per-symbol in-memory history buffer.

3-bar delta yön hesaplaması (MFI/RSI), color flip detection, kısa-vadeli
hafıza. Karar etkisiz; sadece runtime decision mekanizması için. Bot
restart'ta sıfırlanır (in-memory only — DB persistence ayrı katman:
`decision_log` tablosu zengin per-cycle audit trail tutar).

Architecture:
    HAStateRegistry  (singleton in BotContext)
      └── HASymbolState  (one per watched symbol)
            ├── history: deque[HASnapshot]  (maxlen ~10)
            └── derived properties:
                  - mfi_3m_delta_dir / rsi_3m_delta_dir  (UP/DOWN/MIXED)
                  - color_flip_3m / color_flip_1m  (GREEN_TO_RED / RED_TO_GREEN / None)
                  - mfi_3m_delta_value / rsi_3m_delta_value  (raw delta)

Usage:
    state = ha_registry.update(symbol, market_state, timestamp)
    if state.mfi_3m_delta_dir == "UP" and state.color_flip_3m is None:
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

# Per-symbol history buffer maksimum uzunluğu. Operatör 2026-05-04: startup
# backfill için 50+ bar tutmak istiyor (5 yeşil mum görüp short için kırmızı
# beklemek + 30-bar dominant color analizi). Eski default 10'du; 60 ettim
# (3m'de 60 bar = 3 saat, dominant_color_window=30 için iki katı tampon).
DEFAULT_HISTORY_MAXLEN = 60

# Dominant color analizi default penceresi. Operatör 2026-05-04: HA 3m
# kırmızıya dönünce hemen short açma — son 30 bar dominant rengine bak.
# Yeşil baskınsa "ara düzeltme", short skip; mixed/red baskınsa "gerçek
# dönüş", short OK.
DEFAULT_DOMINANT_WINDOW = 30
DEFAULT_DOMINANT_THRESHOLD = 0.6  # %60+ tek renk → "dominant"; aksi → None


@dataclass(frozen=True)
class HASnapshot:
    """Tek bir cycle'ın HA + osilatör değerlerinin immutable snapshot'ı.

    MarketState'ten `from_market_state()` ile inşa edilir. Frozen — hash'lenebilir
    ve test'lerde değer karşılaştırması güvenli.
    """

    timestamp: datetime
    ha_color_1m: str = ""
    ha_color_3m: str = ""
    ha_color_15m: str = ""
    ha_color_4h: str = ""
    ha_streak_1m: int = 0
    ha_streak_3m: int = 0
    ha_streak_15m: int = 0
    ha_streak_4h: int = 0
    ha_no_lower_shadow_3m: bool = False
    ha_no_upper_shadow_3m: bool = False
    ha_body_pct_3m: float = 0.0
    ema200_3m: float = 0.0
    ha_mfi_1m: float = 0.0
    ha_mfi_3m: float = 0.0
    ha_mfi_15m: float = 0.0
    ha_rsi_1m: float = 50.0
    ha_rsi_3m: float = 50.0
    ha_rsi_15m: float = 50.0

    @classmethod
    def from_market_state(
        cls, market_state: MarketState, timestamp: datetime,
    ) -> "HASnapshot":
        """MarketState'in signal_table + oscillator alanlarından HASnapshot inşa et."""
        sig = market_state.signal_table
        osc = market_state.oscillator
        return cls(
            timestamp=timestamp,
            ha_color_1m=sig.ha_color_1m,
            ha_color_3m=sig.ha_color_3m,
            ha_color_15m=sig.ha_color_15m,
            ha_color_4h=sig.ha_color_4h,
            ha_streak_1m=sig.ha_streak_1m,
            ha_streak_3m=sig.ha_streak_3m,
            ha_streak_15m=sig.ha_streak_15m,
            ha_streak_4h=sig.ha_streak_4h,
            ha_no_lower_shadow_3m=sig.ha_no_lower_shadow_3m,
            ha_no_upper_shadow_3m=sig.ha_no_upper_shadow_3m,
            ha_body_pct_3m=sig.ha_body_pct_3m,
            ema200_3m=sig.ema200_3m,
            ha_mfi_1m=osc.ha_mfi_1m,
            ha_mfi_3m=osc.ha_mfi_3m,
            ha_mfi_15m=osc.ha_mfi_15m,
            ha_rsi_1m=osc.ha_rsi_1m,
            ha_rsi_3m=osc.ha_rsi_3m,
            ha_rsi_15m=osc.ha_rsi_15m,
        )


def _delta_dir(values: list[float], min_delta: float = DEFAULT_MIN_DELTA) -> str:
    """3-bar delta yönü: UP / DOWN / MIXED.

    Args:
        values: son 3 değer (eski → yeni).
        min_delta: |last - first| en az bu kadar olmalı; aksi halde MIXED.

    Returns:
        "UP"    — strict monotonic up: a < b < c AND (c - a) >= min_delta
        "DOWN"  — strict monotonic down: a > b > c AND (a - c) >= min_delta
        "MIXED" — diğer tüm durumlar (zigzag, eşit değerler, kısa history)
    """
    if len(values) < 3:
        return "MIXED"
    a, b, c = values[-3], values[-2], values[-1]
    if c > b > a and (c - a) >= min_delta:
        return "UP"
    if c < b < a and (a - c) >= min_delta:
        return "DOWN"
    return "MIXED"


def _color_flip(prev_color: str, curr_color: str) -> Optional[str]:
    """İki ardışık renk arasında flip varsa yön döndür, yoksa None.

    Returns:
        "GREEN_TO_RED" — prev GREEN, curr RED
        "RED_TO_GREEN" — prev RED, curr GREEN
        None — flip yok (aynı renk veya en az biri DOJI / boş)
    """
    if prev_color == "GREEN" and curr_color == "RED":
        return "GREEN_TO_RED"
    if prev_color == "RED" and curr_color == "GREEN":
        return "RED_TO_GREEN"
    return None


@dataclass
class HASymbolState:
    """Tek bir sembolün HA history buffer'ı + türetilmiş metrikler.

    Cycle başında `update(snapshot)` çağrılır. Strateji modülleri
    property'leri okur (lazy hesaplama, side-effect yok).
    """

    symbol: str
    history: deque = field(
        default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_MAXLEN)
    )

    def update(self, snapshot: HASnapshot) -> None:
        """Yeni cycle'ın snapshot'ını history'ye ekle (eski en sondan düşer)."""
        self.history.append(snapshot)

    @property
    def latest(self) -> Optional[HASnapshot]:
        """En son snapshot (None if history empty)."""
        if not self.history:
            return None
        return self.history[-1]

    @property
    def previous(self) -> Optional[HASnapshot]:
        """Bir önceki snapshot (None if history < 2 entries)."""
        if len(self.history) < 2:
            return None
        return self.history[-2]

    # ── Delta direction (UP/DOWN/MIXED) ─────────────────────────────────────

    @property
    def mfi_3m_delta_dir(self) -> str:
        """3-bar HA-MFI 3m delta yönü."""
        if len(self.history) < 3:
            return "MIXED"
        return _delta_dir([s.ha_mfi_3m for s in list(self.history)[-3:]])

    @property
    def rsi_3m_delta_dir(self) -> str:
        """3-bar HA-RSI 3m delta yönü."""
        if len(self.history) < 3:
            return "MIXED"
        return _delta_dir([s.ha_rsi_3m for s in list(self.history)[-3:]])

    @property
    def mfi_3m_delta_value(self) -> Optional[float]:
        """Ham 3-bar HA-MFI 3m delta (last - first); None if insufficient history."""
        if len(self.history) < 3:
            return None
        recent = list(self.history)[-3:]
        return recent[-1].ha_mfi_3m - recent[0].ha_mfi_3m

    @property
    def rsi_3m_delta_value(self) -> Optional[float]:
        """Ham 3-bar HA-RSI 3m delta."""
        if len(self.history) < 3:
            return None
        recent = list(self.history)[-3:]
        return recent[-1].ha_rsi_3m - recent[0].ha_rsi_3m

    # ── Color flip detection (last vs previous bar) ─────────────────────────

    @property
    def color_flip_3m(self) -> Optional[str]:
        """3m HA renk dönüşümü: GREEN_TO_RED / RED_TO_GREEN / None."""
        if len(self.history) < 2:
            return None
        return _color_flip(self.previous.ha_color_3m, self.latest.ha_color_3m)

    @property
    def color_flip_1m(self) -> Optional[str]:
        """1m HA renk dönüşümü (early warning sinyali)."""
        if len(self.history) < 2:
            return None
        return _color_flip(self.previous.ha_color_1m, self.latest.ha_color_1m)

    @property
    def color_flip_15m(self) -> Optional[str]:
        """15m HA renk dönüşümü (HTF yapı dönüşü — flip-side entry için onay)."""
        if len(self.history) < 2:
            return None
        return _color_flip(self.previous.ha_color_15m, self.latest.ha_color_15m)

    # ── Dominant color analysis (window-based baskınlık) ───────────────────

    def dominant_color_3m(
        self,
        window: int = DEFAULT_DOMINANT_WINDOW,
        threshold: float = DEFAULT_DOMINANT_THRESHOLD,
    ) -> Optional[str]:
        """Son `window` snapshot'ta hangi 3m HA rengi baskın.

        Operatör 2026-05-04: "5 yeşil mum varsa kırmızıyı bekle short için —
        düşüş ara düzeltme olabilir, aşağı yön yukarıya ağır basıyorsa shortla."

        Args:
            window: kaç bar geriye bakılır (default 30 = 90 dk on 3m).
            threshold: bir rengin baskın sayılması için minimum oran (0-1).

        Returns:
            "GREEN" / "RED" — son `window` bar içinde >= threshold oranında
              ilgili renk varsa baskın
            None — balanced (ne yeşil ne kırmızı net baskın) veya yetersiz
              history (window'un yarısından az snapshot)
        """
        return self._dominant_color(self._colors_3m(), window, threshold)

    def dominant_color_15m(
        self,
        window: int = DEFAULT_DOMINANT_WINDOW,
        threshold: float = DEFAULT_DOMINANT_THRESHOLD,
    ) -> Optional[str]:
        """15m HA dominant color (HTF baskınlık — operatör destekleyici dedi)."""
        return self._dominant_color(self._colors_15m(), window, threshold)

    def _colors_3m(self) -> list[str]:
        return [s.ha_color_3m for s in self.history]

    def _colors_15m(self) -> list[str]:
        return [s.ha_color_15m for s in self.history]

    @staticmethod
    def _dominant_color(
        colors: list[str], window: int, threshold: float,
    ) -> Optional[str]:
        if not colors:
            return None
        recent = colors[-window:]
        # Need at least half the window for a statistically meaningful read.
        if len(recent) < max(1, window // 2):
            return None
        green_count = sum(1 for c in recent if c == "GREEN")
        red_count = sum(1 for c in recent if c == "RED")
        total = len(recent)
        if green_count / total >= threshold:
            return "GREEN"
        if red_count / total >= threshold:
            return "RED"
        return None


@dataclass
class HAStateRegistry:
    """Per-symbol HA state container — BotContext'e bağlanır.

    Cycle başında bot her watched symbol için `update()` çağırır:

        registry.update(symbol, market_state, timestamp)

    Sonra strateji modülleri sorgular:

        state = registry.get(symbol)
        if state and state.mfi_3m_delta_dir == "UP":
            ...

    Bot restart'ta sıfırlanır (in-memory only).
    """

    states: dict[str, HASymbolState] = field(default_factory=dict)

    def update(
        self, symbol: str, market_state: MarketState, timestamp: datetime,
    ) -> HASymbolState:
        """Symbol'a yeni snapshot ekle. Symbol ilk kezse state oluşturulur.

        Returns:
            Bu sembolün state'i (caller property'leri sorgulayabilir).
        """
        if symbol not in self.states:
            self.states[symbol] = HASymbolState(symbol=symbol)
        snapshot = HASnapshot.from_market_state(market_state, timestamp)
        self.states[symbol].update(snapshot)
        return self.states[symbol]

    def get(self, symbol: str) -> Optional[HASymbolState]:
        """Get state for a symbol; None if no snapshots have been recorded yet."""
        return self.states.get(symbol)

    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset state. If symbol given, just that one; otherwise all symbols."""
        if symbol is None:
            self.states.clear()
        else:
            self.states.pop(symbol, None)
