"""Derivatives regime classification (Phase 1.5 Madde 5).

Takes a `DerivativesState` snapshot + per-symbol thresholds and returns
one of five regimes. Ordering is priority-first:

  1. Stale snapshot            → UNKNOWN  (can't trust anything)
  2. Massive 1h liq wash-out   → CAPITULATION
  3. Funding + LS both hot     → LONG_CROWDED  (contrarian short bias)
  4. Funding + LS both cold    → SHORT_CROWDED (contrarian long bias)
  5. Otherwise                 → BALANCED

CAPITULATION wins over crowded regimes on purpose: a washout is the
event that *clears* an imbalance, so once we see it we stop labelling
the market as crowded and wait for the next regime to form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Regime(str, Enum):
    UNKNOWN = "UNKNOWN"
    BALANCED = "BALANCED"
    LONG_CROWDED = "LONG_CROWDED"
    SHORT_CROWDED = "SHORT_CROWDED"
    CAPITULATION = "CAPITULATION"


@dataclass
class RegimeAnalysis:
    regime: Regime
    confidence: float                   # 0.0..1.0
    reasoning: list[str] = field(default_factory=list)


# Safe defaults — calibrated for BTC. ETH / SOL override via config.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "funding_crowded_z": 2.0,
    "ls_crowded_z": 2.0,
    "oi_surge_pct": 8.0,
    "oi_crash_pct": -10.0,
    "capitulation_liq_notional": 10_000_000.0,
    "stale_snapshot_s": 180.0,
}


def classify_regime(
    state: Any,
    *,
    funding_crowded_z: float = 2.0,
    ls_crowded_z: float = 2.0,
    oi_surge_pct: float = 8.0,
    oi_crash_pct: float = -10.0,
    capitulation_liq_notional: float = 10_000_000.0,
    stale_snapshot_s: float = 180.0,
) -> RegimeAnalysis:
    # 1) Stale data → UNKNOWN.
    age = float(getattr(state, "coinalyze_snapshot_age_s", 0.0) or 0.0)
    if age > stale_snapshot_s:
        return RegimeAnalysis(
            Regime.UNKNOWN, 0.0,
            [f"snapshot_age={age:.0f}s>{stale_snapshot_s:.0f}s"],
        )

    # 2) CAPITULATION — combined 1h liq notional breaches the threshold.
    long_liq = float(getattr(state, "long_liq_notional_1h", 0.0) or 0.0)
    short_liq = float(getattr(state, "short_liq_notional_1h", 0.0) or 0.0)
    total_liq = long_liq + short_liq
    if total_liq >= capitulation_liq_notional:
        imbalance = float(getattr(state, "liq_imbalance_1h", 0.0) or 0.0)
        confidence = min(1.0, total_liq / (2.0 * capitulation_liq_notional))
        return RegimeAnalysis(
            Regime.CAPITULATION, confidence,
            [f"liq_1h=${total_liq:,.0f}",
             f"imbalance={imbalance:+.2f}",
             f"threshold=${capitulation_liq_notional:,.0f}"],
        )

    funding_z = float(getattr(state, "funding_rate_zscore_30d", 0.0) or 0.0)
    ls_z = float(getattr(state, "ls_ratio_zscore_14d", 0.0) or 0.0)
    oi_24h = float(getattr(state, "oi_change_24h_pct", 0.0) or 0.0)

    # 3) LONG_CROWDED — funding + LS both historically elevated.
    if funding_z >= funding_crowded_z and ls_z >= ls_crowded_z:
        combined = (funding_z + ls_z) / 2.0
        threshold_avg = (funding_crowded_z + ls_crowded_z) / 2.0
        confidence = min(1.0, combined / (threshold_avg * 2.0))
        reasons = [f"funding_z={funding_z:.2f}", f"ls_z={ls_z:.2f}"]
        if oi_24h >= oi_surge_pct:
            reasons.append(f"oi_24h={oi_24h:+.2f}% surging")
        return RegimeAnalysis(Regime.LONG_CROWDED, confidence, reasons)

    # 4) SHORT_CROWDED — funding + LS both historically depressed.
    if funding_z <= -funding_crowded_z and ls_z <= -ls_crowded_z:
        combined = (abs(funding_z) + abs(ls_z)) / 2.0
        threshold_avg = (funding_crowded_z + ls_crowded_z) / 2.0
        confidence = min(1.0, combined / (threshold_avg * 2.0))
        reasons = [f"funding_z={funding_z:.2f}", f"ls_z={ls_z:.2f}"]
        if oi_24h <= oi_crash_pct:
            reasons.append(f"oi_24h={oi_24h:+.2f}% crashing")
        return RegimeAnalysis(Regime.SHORT_CROWDED, confidence, reasons)

    # 5) Fallback.
    return RegimeAnalysis(
        Regime.BALANCED, 0.5,
        [f"funding_z={funding_z:.2f}", f"ls_z={ls_z:.2f}"],
    )


def resolve_thresholds(
    symbol: str,
    base: dict[str, float],
    overrides: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Layer a per-symbol override dict onto the base thresholds.

    Example:
        base = DEFAULT_THRESHOLDS
        overrides = {"SOL-USDT-SWAP": {"capitulation_liq_notional": 8_000_000}}
    """
    merged = dict(base)
    for override_key, override_val in (overrides.get(symbol) or {}).items():
        merged[override_key] = override_val
    return merged
