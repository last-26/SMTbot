"""TradePlan: the fully-sized, risk-bounded trade the execution layer will place.

A TradePlan is the output of the strategy engine and the input to the
execution layer (Phase 4). It contains every number the exchange client needs
to place an entry order plus its position-attached TP/SL, along with the
accounting fields that go into the trade journal (Phase 5).

Produced by:
  - `calculate_trade_plan` in `rr_system.py` (pure math)
  - `build_trade_plan_from_state` in `entry_signals.py` (state orchestration)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data.models import Direction


@dataclass
class TradePlan:
    """Fully-sized trade ready for execution."""
    direction: Direction
    entry_price: float
    sl_price: float
    tp_price: float

    rr_ratio: float
    sl_distance: float            # |entry - sl| in price units
    sl_pct: float                 # sl_distance / entry, in [0, 1]

    position_size_usdt: float     # notional exposure
    leverage: int                 # rounded, clamped to [1, max_leverage]
    required_leverage: float      # pre-cap, for diagnostics
    num_contracts: int            # Integer contracts (internal canonical: BTC-USDT-SWAP → 0.01 BTC each)

    risk_amount_usdt: float       # actual USDT at risk on SL hit (notional * sl_pct)
    max_risk_usdt: float          # USDT risk target before any capping
    capped: bool                  # True when required_leverage > max_leverage

    fee_reserve_pct: float = 0.0  # round-trip taker reserve baked into sizing
    sl_source: str = ""           # "order_block" / "fvg" / "swing" / "atr_fallback"
    confluence_score: float = 0.0
    confluence_factors: list[str] = field(default_factory=list)
    # 2026-04-22 — per-pillar raw score dict (ConfluenceFactor.name → weight).
    # Mirrors `confluence_factors` but preserves contribution magnitude so
    # Pass 2 can Bayesian-tune per-pillar weights against closed-trade
    # outcomes. Empty dict when no factors fired (e.g., direction UNDEFINED).
    confluence_pillar_scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""              # human-readable summary
    # Partial-TP ladder from the zone builder: list of (price, share_fraction)
    # ordered near→far, summing to 1.0. Default `[(tp_price, 1.0)]` preserves
    # single-leg behaviour for legacy callers. Populated by `apply_zone_to_plan`
    # from `ZoneSetup.tp_ladder`. Consumers (partial-TP executor, journal,
    # RL feature builder) can iterate safely without a None guard.
    tp_ladder: list[tuple[float, float]] = field(default_factory=list)

    @property
    def tp_distance(self) -> float:
        return abs(self.tp_price - self.entry_price)

    @property
    def expected_win_usdt(self) -> float:
        """USDT profit if TP is hit (notional * tp_pct)."""
        if self.entry_price <= 0:
            return 0.0
        return self.position_size_usdt * (self.tp_distance / self.entry_price)

    @property
    def is_long(self) -> bool:
        return self.direction == Direction.BULLISH

    @property
    def is_short(self) -> bool:
        return self.direction == Direction.BEARISH
