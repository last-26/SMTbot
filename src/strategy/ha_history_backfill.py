"""HA history backfill — startup'ta state buffer'ını geçmiş 3m barlarla doldur.

Operatör 2026-05-04: bot başlatıldığında her sembol için son N 3m barın HA
verilerini hesaplayıp state buffer'a push eder. Avantajlar:
  - `dominant_color` analizi ilk cycle'dan itibaren çalışır (5+ yeşil mum
    "ara düzeltme" filtresi hemen aktif)
  - HA streak counter doğru başlangıç değerinde gelir (Pine'la match eder)
  - Body % + no-shadow geometry ilk cycle'da hazır

Multi-TF (1m, 15m, 4h) + MFI/RSI runtime Pine'dan dolar — backfill sadece
3m HA color + streak + body + shadow için. `dominant_color_15m`, ilk 15m
Pine cycle'ından sonra çalışmaya başlar.

Usage (runner startup):
    raw_klines = await kline_cache.fetch(symbol, "3", limit=50)
    n = fetch_and_backfill(state_registry, symbol, raw_klines)
    logger.info("ha_backfill symbol={} bars={}", symbol, n)

Pine v6 formula reference (smt_overlay.pine `f_ha_ohlc` / `f_ha_color` /
`f_ha_streak_tf` ile birebir aynı):
  haC = (O + H + L + C) / 4
  haO = (prev_haO + prev_haC) / 2  (or (O+C)/2 first bar)
  haH = max(H, haO, haC)
  haL = min(L, haO, haC)
  color: |body|/range < 0.10 → DOJI; else haC > haO → GREEN, < → RED
  streak: signed counter, +N green / -N red / 0 = doji break
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Union

from src.strategy.ha_state import HASnapshot, HAStateRegistry, HASymbolState


@dataclass(frozen=True)
class RawBar:
    """Minimal OHLC bar (matches kline_cache.Kline shape; subset)."""

    bar_start_ms: int
    open: float
    high: float
    low: float
    close: float


# Pure compute helpers — same formulas as Pine v6 (smt_overlay).


def _compute_ha_ohlc_sequence(
    bars: list[RawBar],
) -> list[tuple[float, float, float, float]]:
    """Pine `f_ha_ohlc` recursion: sequential HA OHLC for each bar."""
    out: list[tuple[float, float, float, float]] = []
    prev_haO: float | None = None
    prev_haC: float | None = None
    for bar in bars:
        haC = (bar.open + bar.high + bar.low + bar.close) / 4
        if prev_haO is None or prev_haC is None:
            haO = (bar.open + bar.close) / 2
        else:
            haO = (prev_haO + prev_haC) / 2
        haH = max(bar.high, haO, haC)
        haL = min(bar.low, haO, haC)
        out.append((haO, haH, haL, haC))
        prev_haO = haO
        prev_haC = haC
    return out


def _compute_color(haO: float, haH: float, haL: float, haC: float) -> str:
    """Pine `f_ha_color`: |body|/range < 0.10 → DOJI; else GREEN/RED."""
    body = abs(haC - haO)
    rng = haH - haL
    is_doji = (body / rng) < 0.10 if rng > 0 else True
    return "DOJI" if is_doji else ("GREEN" if haC > haO else "RED")


def _compute_streak_sequence(colors: list[str]) -> list[int]:
    """Pine `f_ha_streak_tf` recursion: signed running counter."""
    streaks: list[int] = []
    s = 0
    for c in colors:
        if c == "DOJI":
            s = 0
        elif c == "GREEN":
            s = s + 1 if s >= 0 else 1
        else:  # RED
            s = s - 1 if s <= 0 else -1
        streaks.append(s)
    return streaks


def _body_pct(haO: float, haH: float, haL: float, haC: float) -> float:
    rng = haH - haL
    if rng <= 0:
        return 0.0
    return (abs(haC - haO) / rng) * 100.0


def _no_lower_shadow(
    color: str, haO: float, haH: float, haL: float, haC: float,
) -> bool:
    if color != "GREEN":
        return False
    rng = haH - haL
    return rng > 0 and ((haO - haL) / rng) < 0.05


def _no_upper_shadow(
    color: str, haO: float, haH: float, haL: float, haC: float,
) -> bool:
    if color != "RED":
        return False
    rng = haH - haL
    return rng > 0 and ((haH - haO) / rng) < 0.05


# Public API


def compute_ha_snapshots_3m(bars: list[RawBar]) -> list[HASnapshot]:
    """3m kline series → HASnapshot series (oldest → newest).

    Sadece 3m alanları doldurur (color/streak/body/shadow). Diğer TF (1m/15m/
    4h) + MFI/RSI default değerlerinde kalır — runtime Pine cycle'larında dolar.
    """
    if not bars:
        return []
    ohlcs = _compute_ha_ohlc_sequence(bars)
    colors = [_compute_color(*o) for o in ohlcs]
    streaks = _compute_streak_sequence(colors)
    snapshots: list[HASnapshot] = []
    for bar, ha, color, streak in zip(bars, ohlcs, colors, streaks):
        haO, haH, haL, haC = ha
        snapshots.append(HASnapshot(
            timestamp=datetime.fromtimestamp(
                bar.bar_start_ms / 1000, tz=timezone.utc,
            ),
            ha_color_3m=color,
            ha_streak_3m=streak,
            ha_body_pct_3m=_body_pct(haO, haH, haL, haC),
            ha_no_lower_shadow_3m=_no_lower_shadow(color, haO, haH, haL, haC),
            ha_no_upper_shadow_3m=_no_upper_shadow(color, haO, haH, haL, haC),
        ))
    return snapshots


def kline_to_raw_bar(k: Union[list, dict, Any]) -> RawBar:
    """Convert flexible kline payload to RawBar.

    Supports:
      - Bybit V5 list format: [ts, open, high, low, close, ...]
      - kline_cache.Kline serialised dict: {"t": ts, "o": ..., "h": ..., ...}
      - kline_cache.Kline dataclass instance (has .bar_start_ms etc. attrs)
    """
    if isinstance(k, list):
        return RawBar(
            bar_start_ms=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
        )
    if isinstance(k, dict):
        # Compact serialised: {"t":..,"o":..,"h":..,"l":..,"c":..}
        # Or Bybit raw: {"timestamp":..,"open":..,...}
        ts_keys = ("t", "bar_start_ms", "timestamp", "ts")
        ts = next((k[key] for key in ts_keys if key in k), None)
        return RawBar(
            bar_start_ms=int(ts) if ts is not None else 0,
            open=float(k.get("o") or k.get("open") or 0),
            high=float(k.get("h") or k.get("high") or 0),
            low=float(k.get("l") or k.get("low") or 0),
            close=float(k.get("c") or k.get("close") or 0),
        )
    # Dataclass-style with attributes
    return RawBar(
        bar_start_ms=int(getattr(k, "bar_start_ms")),
        open=float(getattr(k, "open")),
        high=float(getattr(k, "high")),
        low=float(getattr(k, "low")),
        close=float(getattr(k, "close")),
    )


def fetch_and_backfill(
    state_registry: HAStateRegistry,
    symbol: str,
    raw_klines: list,
) -> int:
    """Compute HA snapshots from raw klines and append to symbol's state.

    Caller (runner startup) fetches the kline list from Bybit (or KlineCache)
    and passes it here. Snapshots appended in chronological order to fill
    the deque for `dominant_color` analysis + streak continuity.

    Args:
        state_registry: HAStateRegistry to update.
        symbol: target symbol key.
        raw_klines: list of klines in any supported format (list/dict/Kline).

    Returns:
        Number of snapshots appended.
    """
    bars = [kline_to_raw_bar(k) for k in raw_klines]
    snapshots = compute_ha_snapshots_3m(bars)
    state = state_registry.get(symbol)
    if state is None:
        state = HASymbolState(symbol=symbol)
        state_registry.states[symbol] = state
    for snap in snapshots:
        state.update(snap)
    return len(snapshots)
