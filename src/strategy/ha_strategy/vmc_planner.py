"""VMC entry planner — Yol B (HA Strategy, 2026-05-05) 4-gate evaluator.

Operatör doctrine:
  LONG  = VWAP slope UP (negatiften 0'a) + MFI delta UP + WT2 turning UP +
          5m HA color GREEN
  SHORT = VWAP slope DOWN (pozitiften 0'a) + MFI delta DOWN + WT2 turning DOWN +
          5m HA color RED

Tüm 4 gate mandatory. Soft factor: 15m HA color (mandatory degil; aligned ise
+score, opposing ise -score, neutral 0).

`evaluate_entry(ctx, config)` saf fonksiyon — side-effect yok, runtime decision
mekanizması için. Runner kullanım pattern'i:

    decision = evaluate_entry(EntryContext(...), config.ha_strategy.entry)
    if decision.is_take:
        plan = self._build_vmc_trade_plan(decision, symbol, ...)

Dispatcher YOK — operatör Yol B'de tek path (Major/Continuation/Micro Reversal
ayrımı Yol A'da kalır, flag arkasında frozen).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.data.models import Direction, MarketState
from src.strategy.ha_strategy.vmc_state import VMCSymbolState


# ──────────────────────────────────────────────────────────────────────────────
# Configuration (operatör onaylı default'lar — 2026-05-05)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VMCEntryConfig:
    """Yol B entry knob'ları. Source-level default'lar; YAML override Phase 6+."""

    # Gate 1: VWAP slope (wt_vwap_fast = wt1 - wt2)
    vwap_slope_lookback_bars: int = 2
    vwap_must_be_negative_for_long: bool = True
    vwap_must_be_positive_for_short: bool = True

    # Gate 2: MFI 3-bar delta
    mfi_min_delta_3bar: float = 0.5

    # Gate 3: WT2 turning (lokal dip/tepe + sonraki bar dönüş)
    wt_turn_lookback_bars: int = 2

    # Gate 4: 5m HA color
    ha_color_5m_required: bool = True

    # Soft factor: 15m HA alignment
    ha_15m_alignment: str = "soft"        # off | soft | mandatory
    ha_15m_aligned_bonus: float = 1.0
    ha_15m_opposing_penalty: float = -1.0

    # Entry pricing
    marketable_offset_pct: float = 0.0005  # 5 bps from last close

    # Per-symbol SL pct (operatör 2026-05-05 spec'i)
    sl_pct_per_symbol: dict[str, float] = field(default_factory=lambda: {
        "BTC-USDT-SWAP":      0.005,
        "ETH-USDT-SWAP":      0.008,
        "ZEC-USDT-SWAP":      0.010,
        "HYPE-USDT-SWAP":     0.010,
        "1000PEPE-USDT-SWAP": 0.010,
        "ONDO-USDT-SWAP":     0.010,
        "AVAX-USDT-SWAP":     0.010,
        "DOGE-USDT-SWAP":     0.010,
        "ADA-USDT-SWAP":      0.010,
        "LINK-USDT-SWAP":     0.010,
    })
    sl_pct_default: float = 0.010


def vmc_sl_pct_for(symbol: str, config: VMCEntryConfig) -> float:
    """Per-symbol fixed SL pct — operatör 2026-05-05: %0.5 / %0.8 / %1."""
    return config.sl_pct_per_symbol.get(symbol, config.sl_pct_default)


# ──────────────────────────────────────────────────────────────────────────────
# Context + Decision dataclasses
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntryContext:
    """Bot tarafından evaluate_entry'ye sağlanan input bundle.

    `market_state` Optional — planner şu an doğrudan kullanmıyor (gate'ler
    `vmc_state` üzerinden okur), runner Phase 6 entegrasyonunda confluence
    forward'u veya audit log için faydalı olabilir.
    """

    symbol: str
    vmc_state: VMCSymbolState
    last_close: float                  # marketable limit anchor
    market_state: Optional[MarketState] = None
    has_open_position: bool = False    # no_duplicate gate (cross-pair lock dışı)
    open_position_direction: Optional[Direction] = None  # same-direction lock için


@dataclass
class EntryDecision:
    """evaluate_entry output. is_take True ise plan builder çağrılır.

    Yol A `EntryDecision` ile interface uyumlu: `decision` (action alias),
    `entry_path` / `major_reversal_score` / vb. dispatcher fields None default.
    Runner mode-bağımsız `_run_one_symbol` ortak kod path'lerinde her ikisi
    de değişmeden tüketilir.
    """

    action: str                        # "TAKE" | "NO_SETUP" | "REJECT"
    direction: Optional[Direction] = None
    reason: str = ""
    score: float = 0.0
    gate_results: dict[str, bool] = field(default_factory=dict)
    soft_factors: dict[str, float] = field(default_factory=dict)
    suggested_entry_price: Optional[float] = None
    suggested_sl_price: Optional[float] = None

    # ─── Yol A interface uyumluluğu (backward compat) ────────────────────────
    # _run_one_symbol ortak kod path'leri Yol A EntryDecision'in `decision`
    # field'ını + 3-tip dispatcher field'larını okur. Yol B'de bu field'lar
    # None / 0.0 default — Yol B dispatcher YOK.
    entry_path: Optional[str] = None              # Yol A: major_reversal/continuation/micro_reversal
    target_rr: Optional[float] = None             # Yol A: per-tip RR
    risk_multiplier: Optional[float] = None       # Yol A: per-tip risk scale
    major_reversal_score: Optional[float] = None
    continuation_score: Optional[float] = None
    micro_reversal_score: Optional[float] = None
    mss_break_detected: Optional[bool] = None

    @property
    def decision(self) -> str:
        """Yol A interface alias for `action`."""
        return self.action

    @property
    def is_take(self) -> bool:
        return self.action == "TAKE"

    @property
    def is_no_setup(self) -> bool:
        return self.action == "NO_SETUP"

    @property
    def is_reject(self) -> bool:
        return self.action == "REJECT"


# ──────────────────────────────────────────────────────────────────────────────
# Per-direction gate evaluation
# ──────────────────────────────────────────────────────────────────────────────


def _evaluate_direction(
    direction: Direction,
    ctx: EntryContext,
    config: VMCEntryConfig,
) -> tuple[bool, dict[str, bool], str]:
    """4 mandatory gate'ı tek yön için değerlendir. Returns (passes, results, fail_reason)."""
    state = ctx.vmc_state
    latest = state.latest
    if latest is None:
        return False, {"history_warmup": False}, "no_history"

    is_long = direction == Direction.BULLISH
    expected_color = "GREEN" if is_long else "RED"
    expected_slope = "UP" if is_long else "DOWN"
    expected_turn = "UP" if is_long else "DOWN"
    expected_delta = "UP" if is_long else "DOWN"

    # Gate 1 — VWAP slope (wt_vwap_fast)
    slope = state.vwap_slope_dir(config.vwap_slope_lookback_bars)
    vwap_val = latest.wt_vwap_fast
    if is_long:
        vwap_sign_ok = (vwap_val < 0) if config.vwap_must_be_negative_for_long else True
    else:
        vwap_sign_ok = (vwap_val > 0) if config.vwap_must_be_positive_for_short else True
    vwap_gate = (slope == expected_slope) and vwap_sign_ok

    # Gate 2 — MFI 3-bar delta
    mfi_delta = state.mfi_5m_delta_dir
    mfi_gate = mfi_delta == expected_delta

    # Gate 3 — WT2 turning
    wt_turn = state.wt2_turning_dir(config.wt_turn_lookback_bars)
    wt_gate = wt_turn == expected_turn

    # Gate 4 — 5m HA color
    ha_color = latest.ha_color_5m
    ha_gate = ha_color == expected_color if config.ha_color_5m_required else True

    results = {
        "vwap_slope": vwap_gate,
        "mfi_delta": mfi_gate,
        "wt2_turning": wt_gate,
        "ha_color_5m": ha_gate,
    }
    if not vwap_gate:
        return False, results, f"vwap_slope_fail:slope={slope}/sign_ok={vwap_sign_ok}/value={vwap_val:.2f}"
    if not mfi_gate:
        return False, results, f"mfi_delta_fail:dir={mfi_delta}"
    if not wt_gate:
        return False, results, f"wt2_turning_fail:dir={wt_turn}"
    if not ha_gate:
        return False, results, f"ha_color_5m_fail:color={ha_color}"
    return True, results, ""


def _soft_factors(
    direction: Direction,
    ctx: EntryContext,
    config: VMCEntryConfig,
) -> tuple[dict[str, float], float]:
    """Mandatory dışı destek faktörleri. Returns (factors_dict, total_bonus)."""
    factors: dict[str, float] = {}
    latest = ctx.vmc_state.latest
    if latest is None:
        return factors, 0.0

    expected_color = "GREEN" if direction == Direction.BULLISH else "RED"
    opposing_color = "RED" if direction == Direction.BULLISH else "GREEN"

    # 15m HA alignment — operatör "tam yardımcı değil ama hafif etki"
    if config.ha_15m_alignment != "off":
        if latest.ha_color_15m == expected_color:
            factors["ha_15m_aligned"] = config.ha_15m_aligned_bonus
        elif latest.ha_color_15m == opposing_color:
            factors["ha_15m_opposing"] = config.ha_15m_opposing_penalty
        # DOJI / boş → factor yok

    total = sum(factors.values())
    return factors, total


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def evaluate_entry(
    ctx: EntryContext,
    config: VMCEntryConfig,
) -> EntryDecision:
    """Yol B entry değerlendirmesi.

    Sıralama:
      1) `no_duplicate` gate — same-symbol same-direction zaten açıksa REJECT.
      2) Both directions paralel evaluate. Hangisi 4-gate pass'ediyorsa onu seç.
         Aynı anda iki yön pass'ederse (matematiksel olarak imkansız ama defensive)
         REJECT (veri tutarsızlığı).
      3) 15m soft factor skoru bonus olarak hesaplanır (mandatory'ye etki etmez).
      4) Mandatory `ha_15m_alignment="mandatory"` modu varsa 15m opposing →
         REJECT (default "soft" → etki yok).
      5) Pricing suggestion: entry = last_close ± offset, sl = entry × (1 ∓ sl_pct).
    """
    state = ctx.vmc_state
    if state.latest is None:
        return EntryDecision(
            action="NO_SETUP",
            reason="ha_strategy_no_history",
        )

    if ctx.last_close <= 0:
        return EntryDecision(
            action="NO_SETUP",
            reason="ha_strategy_no_price",
        )

    # Both directions paralel evaluate
    long_pass, long_gates, long_fail = _evaluate_direction(
        Direction.BULLISH, ctx, config,
    )
    short_pass, short_gates, short_fail = _evaluate_direction(
        Direction.BEARISH, ctx, config,
    )

    # Direction selection
    if long_pass and short_pass:
        # Defensive: bu matematiksel olarak imkansız (HA color tek yönde olur)
        return EntryDecision(
            action="REJECT",
            reason="ha_strategy_both_directions_pass",
            gate_results={**{f"long_{k}": v for k, v in long_gates.items()},
                          **{f"short_{k}": v for k, v in short_gates.items()}},
        )
    if not (long_pass or short_pass):
        # Hangisi daha çok gate geçiyor onu rapor et (audit için)
        long_count = sum(1 for v in long_gates.values() if v)
        short_count = sum(1 for v in short_gates.values() if v)
        if long_count >= short_count:
            return EntryDecision(
                action="NO_SETUP",
                reason=f"ha_strategy_long:{long_fail}",
                gate_results=long_gates,
            )
        return EntryDecision(
            action="NO_SETUP",
            reason=f"ha_strategy_short:{short_fail}",
            gate_results=short_gates,
        )

    direction = Direction.BULLISH if long_pass else Direction.BEARISH
    gate_results = long_gates if long_pass else short_gates

    # No-duplicate same-direction lock
    if ctx.has_open_position and ctx.open_position_direction == direction:
        return EntryDecision(
            action="REJECT",
            direction=direction,
            reason="ha_strategy_no_duplicate",
            gate_results=gate_results,
        )

    # 15m soft factor (mandatory mode'da REJECT olabilir)
    soft_factors, soft_total = _soft_factors(direction, ctx, config)
    if (
        config.ha_15m_alignment == "mandatory"
        and "ha_15m_opposing" in soft_factors
    ):
        return EntryDecision(
            action="REJECT",
            direction=direction,
            reason="ha_strategy_15m_opposing_mandatory",
            gate_results=gate_results,
            soft_factors=soft_factors,
        )

    # Pricing suggestion
    is_long = direction == Direction.BULLISH
    offset = ctx.last_close * config.marketable_offset_pct
    entry_px = ctx.last_close + offset if is_long else ctx.last_close - offset
    sl_pct = vmc_sl_pct_for(ctx.symbol, config)
    sl_dist = entry_px * sl_pct
    sl_px = entry_px - sl_dist if is_long else entry_px + sl_dist

    # Score: 4 mandatory gate (each +1) + soft factor
    score = 4.0 + soft_total

    return EntryDecision(
        action="TAKE",
        direction=direction,
        reason=f"ha_strategy:{('long' if is_long else 'short')}",
        score=score,
        gate_results=gate_results,
        soft_factors=soft_factors,
        suggested_entry_price=entry_px,
        suggested_sl_price=sl_px,
    )
