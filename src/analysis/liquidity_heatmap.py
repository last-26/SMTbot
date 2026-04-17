"""Estimated + historical liquidity heatmap (Phase 1.5 Madde 4).

Pure functions, no I/O. Consumers pass:
  * `current_price`          — where the symbol is trading right now
  * `DerivativesState`       — for `open_interest_usd` + `long_short_ratio`
  * `LiquidationStream`      — to pull the last 24-48h of realized liq events

We split the total OI into long / short halves using the LS ratio, spread
each half across an assumed leverage distribution, and price the liq level
for each bucket with the simplified Coinglass formula:

    long_liq_price  = entry * (1 - 1/lev + fee_buffer)
    short_liq_price = entry * (1 + 1/lev - fee_buffer)

**Hard assumption:** entry == current_price. Real positions were opened at
a mix of prices; a better model would reconstruct weighted-avg entry from
OI-change history. That is a Phase 7+ refinement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

LEVERAGE_BUCKETS: list[tuple[int, float]] = [
    (10, 0.30),
    (25, 0.35),
    (50, 0.20),
    (100, 0.15),
]
FEE_BUFFER = 0.005   # +/- 0.5% fees + mark drift


@dataclass
class EstimatedLiqLevel:
    price: float
    notional_usd: float
    side: str            # 'LONG_LIQ' | 'SHORT_LIQ'
    leverage: int
    kind: str = "estimated"


@dataclass
class Cluster:
    price: float
    notional_usd: float
    side: str            # 'LONG_LIQ' | 'SHORT_LIQ' | 'MIXED'
    sources: list[str] = field(default_factory=list)  # ['estimated','historical']


@dataclass
class LiquidityHeatmap:
    symbol: str
    current_price: float
    clusters_above: list[Cluster]
    clusters_below: list[Cluster]
    nearest_above: Optional[Cluster]
    nearest_below: Optional[Cluster]
    largest_above_notional: float
    largest_below_notional: float


# ── Estimated levels ──────────────────────────────────────────────────────


def estimate_liquidation_levels(
    current_price: float,
    long_short_ratio: float,
    total_oi_usd: float,
    leverage_buckets: list[tuple[int, float]] = LEVERAGE_BUCKETS,
) -> list[EstimatedLiqLevel]:
    if total_oi_usd <= 0 or current_price <= 0:
        return []
    ls = max(long_short_ratio, 1e-9)
    denom = ls + 1.0
    long_oi = total_oi_usd * (ls / denom)
    short_oi = total_oi_usd * (1.0 / denom)

    levels: list[EstimatedLiqLevel] = []
    for lev, share in leverage_buckets:
        long_notional = long_oi * share
        short_notional = short_oi * share
        long_liq_price = current_price * (1.0 - 1.0 / lev + FEE_BUFFER)
        short_liq_price = current_price * (1.0 + 1.0 / lev - FEE_BUFFER)
        levels.append(EstimatedLiqLevel(
            price=long_liq_price, notional_usd=long_notional,
            side="LONG_LIQ", leverage=lev,
        ))
        levels.append(EstimatedLiqLevel(
            price=short_liq_price, notional_usd=short_notional,
            side="SHORT_LIQ", leverage=lev,
        ))
    return levels


# ── Historical levels (from LiquidationStream buffer) ─────────────────────


def historical_liq_levels(
    liq_stream: Any,
    symbol: str,
    lookback_ms: int,
) -> list[EstimatedLiqLevel]:
    if liq_stream is None:
        return []
    try:
        events = liq_stream.recent(symbol, lookback_ms)
    except Exception:
        return []
    out: list[EstimatedLiqLevel] = []
    for ev in events:
        out.append(EstimatedLiqLevel(
            price=ev.price,
            notional_usd=ev.notional_usd,
            side=ev.side,
            leverage=0,
            kind="historical",
        ))
    return out


# ── Clustering ────────────────────────────────────────────────────────────


def cluster_levels(
    levels: list[EstimatedLiqLevel],
    bucket_pct: float = 0.002,
) -> list[Cluster]:
    """Merge nearby price levels (same side), summing notional."""
    if not levels:
        return []

    def _bucket_key(lvl: EstimatedLiqLevel) -> tuple[str, int]:
        # Quantize price onto a multiplicative grid so nearby levels map
        # to the same integer bucket. `int(log2(p)/log2(1+pct))` is overkill —
        # `round(p / step)` with step = price * pct is stable enough.
        step = max(lvl.price * bucket_pct, 1e-9)
        return (lvl.side, int(round(lvl.price / step)))

    groups: dict[tuple[str, int], list[EstimatedLiqLevel]] = {}
    for lvl in levels:
        groups.setdefault(_bucket_key(lvl), []).append(lvl)

    clusters: list[Cluster] = []
    for (side, _), members in groups.items():
        total = sum(m.notional_usd for m in members)
        if total <= 0:
            continue
        weighted_price = sum(m.price * m.notional_usd for m in members) / total
        sources = sorted({m.kind for m in members})
        clusters.append(Cluster(
            price=weighted_price,
            notional_usd=total,
            side=side,
            sources=sources,
        ))
    return clusters


# ── Top-level builder ─────────────────────────────────────────────────────


def build_heatmap(
    symbol: str,
    current_price: float,
    deriv_state: Any,
    liq_stream: Any,
    bucket_pct: float = 0.002,
    historical_lookback_ms: int = 48 * 60 * 60 * 1000,
    max_clusters_each_side: int = 10,
    leverage_buckets: list[tuple[int, float]] = LEVERAGE_BUCKETS,
) -> LiquidityHeatmap:
    oi = float(getattr(deriv_state, "open_interest_usd", 0.0) or 0.0)
    ls = float(getattr(deriv_state, "long_short_ratio", 1.0) or 1.0)

    levels = estimate_liquidation_levels(
        current_price=current_price,
        long_short_ratio=ls,
        total_oi_usd=oi,
        leverage_buckets=leverage_buckets,
    )
    levels.extend(historical_liq_levels(
        liq_stream, symbol, historical_lookback_ms,
    ))

    clusters = cluster_levels(levels, bucket_pct=bucket_pct)

    above = sorted(
        [c for c in clusters if c.price > current_price],
        key=lambda c: c.price,
    )[:max_clusters_each_side]
    below = sorted(
        [c for c in clusters if c.price < current_price],
        key=lambda c: c.price,
        reverse=True,
    )[:max_clusters_each_side]

    nearest_above = above[0] if above else None
    nearest_below = below[0] if below else None
    largest_above = max((c.notional_usd for c in above), default=0.0)
    largest_below = max((c.notional_usd for c in below), default=0.0)

    return LiquidityHeatmap(
        symbol=symbol,
        current_price=current_price,
        clusters_above=above,
        clusters_below=below,
        nearest_above=nearest_above,
        nearest_below=nearest_below,
        largest_above_notional=largest_above,
        largest_below_notional=largest_below,
    )
