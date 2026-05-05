"""VMC history backfill — startup'ta VMCSymbolState buffer'ını geçmiş 5m + 15m
barlarla doldur (Yol B HA Strategy).

Operatör 2026-05-05 Yol B: bot başlatıldığında her sembol için son N 5m bar'ın
HA verilerini hesaplayıp state buffer'a push eder. Avantajlar:
  - `vwap_slope_dir(2)` ve `wt2_turning_dir(2)` ilk cycle'dan itibaren çalışır.
    (Aslında bunlar oscillator-bazlı — ilk Pine cycle'larında dolar.)
  - `dominant_color_5m(30)` ilk cycle'dan itibaren çalışır (30 bar = 2.5 saat).
  - HA streak counter doğru başlangıç değerinde gelir (Pine match).
  - `ha_close_break_long/short(5)` exit guard ilk cycle'dan itibaren aktif.

15m field'ları (`ha_color_15m`, `ha_streak_15m`) her 5m snapshot'a forward-fill
ile eşlenir — her 5m bar timestamp'ine ait 15m bucket'tan okunur.

Runtime Pine'dan dolan field'lar (backfill default'ta 0/50/empty kalır):
  - wt1, wt2, wt_vwap_fast (WaveTrend)
  - ha_mfi_5m, ha_mfi_15m, ha_rsi_5m, ha_rsi_15m (oscillator HA-MFI/RSI)
  - vwap_5m + bands (Pine session-anchored)
  - volume_5m, volume_5m_ratio

Bu trade-off ilk 3 cycle entry üretmez (mfi/rsi 3-bar delta + WT slope için
3 cycle Pine verisi gerekir). 3 cycle = 15 dakika; kabul edilir startup gecikmesi.

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
from typing import Any, Optional, Union

from src.strategy.ha_strategy.vmc_state import (
    VMCSnapshot,
    VMCStateRegistry,
    VMCSymbolState,
)


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
    out: list[tuple[float, float, float, float]] = []
    prev_haO: Optional[float] = None
    prev_haC: Optional[float] = None
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
    body = abs(haC - haO)
    rng = haH - haL
    is_doji = (body / rng) < 0.10 if rng > 0 else True
    return "DOJI" if is_doji else ("GREEN" if haC > haO else "RED")


def _compute_streak_sequence(colors: list[str]) -> list[int]:
    streaks: list[int] = []
    s = 0
    for c in colors:
        if c == "DOJI":
            s = 0
        elif c == "GREEN":
            s = s + 1 if s >= 0 else 1
        else:
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


def kline_to_raw_bar(k: Union[list, dict, Any]) -> RawBar:
    """Convert flexible kline payload to RawBar (mirrors ha_history_backfill)."""
    if isinstance(k, list):
        return RawBar(
            bar_start_ms=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
        )
    if isinstance(k, dict):
        ts_keys = ("t", "bar_start_ms", "timestamp", "ts")
        ts = next((k[key] for key in ts_keys if key in k), None)
        return RawBar(
            bar_start_ms=int(ts) if ts is not None else 0,
            open=float(k.get("o") or k.get("open") or 0),
            high=float(k.get("h") or k.get("high") or 0),
            low=float(k.get("l") or k.get("low") or 0),
            close=float(k.get("c") or k.get("close") or 0),
        )
    return RawBar(
        bar_start_ms=int(getattr(k, "bar_start_ms")),
        open=float(getattr(k, "open")),
        high=float(getattr(k, "high")),
        low=float(getattr(k, "low")),
        close=float(getattr(k, "close")),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5m + 15m alignment helper
# ──────────────────────────────────────────────────────────────────────────────


def _align_15m_to_5m(
    bars_5m: list[RawBar],
    snaps_15m: list[tuple[int, str, int]],
) -> list[tuple[str, int]]:
    """For each 5m bar timestamp, return the most recent <= 15m HA snapshot.

    Args:
        bars_5m: 5m kline list (chronological).
        snaps_15m: list of (bar_start_ms, color, streak) tuples for 15m bars.

    Returns:
        For each 5m bar, a (color, streak) pair from the matching 15m bucket.
        If no 15m bar yet covers that 5m timestamp, returns ("", 0) (default).
    """
    out: list[tuple[str, int]] = []
    if not snaps_15m:
        return [("", 0) for _ in bars_5m]
    # snaps_15m sorted by ts ascending
    sorted_snaps = sorted(snaps_15m, key=lambda x: x[0])
    j = 0  # pointer into sorted_snaps
    last_color = ""
    last_streak = 0
    for bar in bars_5m:
        ts_5m = bar.bar_start_ms
        # Advance j while next 15m bar still <= ts_5m
        while j < len(sorted_snaps) and sorted_snaps[j][0] <= ts_5m:
            _, c, s = sorted_snaps[j]
            last_color = c
            last_streak = s
            j += 1
        out.append((last_color, last_streak))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def compute_vmc_snapshots(
    bars_5m: list[RawBar],
    bars_15m: Optional[list[RawBar]] = None,
) -> list[VMCSnapshot]:
    """5m + 15m kline series → VMCSnapshot series (oldest → newest).

    5m bar'lar için HA OHLC + color + streak + body + shadow + price hesaplanır.
    15m field'ları (color + streak) her 5m snapshot'a forward-fill ile eşlenir.

    Oscillator field'ları (wt1/wt2/MFI/RSI) ve Pine VWAP backfill'da default
    kalır — runtime Pine cycle'larında dolar.
    """
    if not bars_5m:
        return []
    # 5m HA series
    ohlcs_5m = _compute_ha_ohlc_sequence(bars_5m)
    colors_5m = [_compute_color(*o) for o in ohlcs_5m]
    streaks_5m = _compute_streak_sequence(colors_5m)
    # 15m HA series (optional)
    if bars_15m:
        ohlcs_15m = _compute_ha_ohlc_sequence(bars_15m)
        colors_15m = [_compute_color(*o) for o in ohlcs_15m]
        streaks_15m = _compute_streak_sequence(colors_15m)
        snaps_15m = [
            (b.bar_start_ms, c, s)
            for b, c, s in zip(bars_15m, colors_15m, streaks_15m)
        ]
    else:
        snaps_15m = []
    aligned_15m = _align_15m_to_5m(bars_5m, snaps_15m)
    # Build snapshots
    snapshots: list[VMCSnapshot] = []
    for bar, ha, color5m, streak5m, (color15m, streak15m) in zip(
        bars_5m, ohlcs_5m, colors_5m, streaks_5m, aligned_15m,
    ):
        haO, haH, haL, haC = ha
        snapshots.append(VMCSnapshot(
            timestamp=datetime.fromtimestamp(
                bar.bar_start_ms / 1000, tz=timezone.utc,
            ),
            ha_color_5m=color5m,
            ha_streak_5m=streak5m,
            ha_no_lower_shadow_5m=_no_lower_shadow(color5m, haO, haH, haL, haC),
            ha_no_upper_shadow_5m=_no_upper_shadow(color5m, haO, haH, haL, haC),
            ha_body_pct_5m=_body_pct(haO, haH, haL, haC),
            ha_color_15m=color15m,
            ha_streak_15m=streak15m,
            price=bar.close,
            # Oscillator + VWAP defaults (runtime Pine fills):
            #   wt1 = wt2 = wt_vwap_fast = 0.0
            #   ha_mfi_5m = ha_mfi_15m = 0.0
            #   ha_rsi_5m = ha_rsi_15m = 50.0
            #   vwap_5m / bands = 0.0
            #   volume_5m = 0.0, volume_5m_ratio = 1.0
            #   ema200_5m = 0.0
        ))
    return snapshots


def fetch_and_backfill(
    state_registry: VMCStateRegistry,
    symbol: str,
    raw_klines_5m: list,
    raw_klines_15m: Optional[list] = None,
) -> int:
    """Compute VMC snapshots from raw klines and append to symbol's state.

    Caller (runner startup) fetches 5m + 15m kline lists from Bybit (or
    KlineCache) and passes them here. Snapshots appended in chronological
    order to fill the deque for `dominant_color_5m` analysis + streak
    continuity + price-break exit guard.

    Args:
        state_registry: VMCStateRegistry to update.
        symbol: target symbol key.
        raw_klines_5m: list of 5m klines in any supported format.
        raw_klines_15m: list of 15m klines (optional; missing → 15m fields default).

    Returns:
        Number of snapshots appended.
    """
    bars_5m = [kline_to_raw_bar(k) for k in raw_klines_5m]
    bars_15m = (
        [kline_to_raw_bar(k) for k in raw_klines_15m]
        if raw_klines_15m
        else None
    )
    snapshots = compute_vmc_snapshots(bars_5m, bars_15m)
    state = state_registry.get(symbol)
    if state is None:
        state = VMCSymbolState(symbol=symbol)
        state_registry.states[symbol] = state
    for snap in snapshots:
        state.update(snap)
    return len(snapshots)
