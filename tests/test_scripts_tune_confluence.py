"""Tests for ``scripts.replay_decisions`` + ``scripts.tune_confluence``.

Pass 1 smoke + correctness. Fabricates TradeRecord and RejectedSignal
instances directly (does NOT round-trip through SQLite writers) so the
tests don't depend on the runner or TradePlan/ExecutionReport shape —
which keeps these tests stable across unrelated refactors.

When Optuna is not installed, the smoke test skips with a clear reason
so CI still passes in a stripped environment. Core replay-logic tests
never need Optuna.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 2026-04-29 — Pass 2.5.B re-added hypothetical_outcome columns; 2026-04-30
# (Pass 3.2.2.b) replay engine gained pillar_weights / target_rr_ratio /
# zone_max_wait_bars knobs. 2026-04-27 skip pin removed. Tests fabricate
# rejects with `hypothetical_outcome=` and exercise the new knob paths.

# Scripts directory isn't on sys.path by default — inject so we can import
# the library module. Mirrors what the scripts themselves do for src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay_decisions import (
    ConfigOverride,
    DatasetMetrics,
    replay_config,
    replay_with_pillar_reweight,
    simulate_reject_outcome,
    simulate_trade_outcome,
)
from src.data.kline_cache import Kline, KlineCache
from src.data.models import Direction
from src.journal.models import (
    RejectedSignal,
    TradeOutcome,
    TradeRecord,
)


UTC = timezone.utc


def _mk_trade(
    *,
    symbol: str = "BTC-USDT-SWAP",
    confluence: float = 4.0,
    outcome: TradeOutcome = TradeOutcome.WIN,
    pnl_r: float = 2.0,
    entry_ts: datetime = None,
) -> TradeRecord:
    """Fabricate a minimal closed TradeRecord. Fields the replay reads
    are the only ones that need sensible values; the rest sit on
    pydantic defaults."""
    entry = entry_ts or datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    return TradeRecord(
        trade_id=f"t_{symbol}_{entry.isoformat()}",
        symbol=symbol,
        direction=Direction.BULLISH,
        outcome=outcome,
        signal_timestamp=entry - timedelta(minutes=3),
        entry_timestamp=entry,
        exit_timestamp=entry + timedelta(minutes=15),
        entry_price=67_000.0,
        sl_price=66_500.0,
        tp_price=68_000.0,
        rr_ratio=2.0,
        leverage=10,
        num_contracts=5,
        position_size_usdt=1_000.0,
        risk_amount_usdt=50.0,
        confluence_score=confluence,
        confluence_factors=["mss_alignment", "vwap_composite"],
        pnl_usdt=pnl_r * 50.0,
        pnl_r=pnl_r,
    )


def _mk_reject(
    *,
    symbol: str = "BTC-USDT-SWAP",
    reject_reason: str = "vwap_misaligned",
    confluence: float = 3.5,
    hypothetical: str = "WIN",
    signal_ts: datetime = None,
) -> RejectedSignal:
    ts = signal_ts or datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    return RejectedSignal(
        rejection_id=f"r_{symbol}_{ts.isoformat()}_{reject_reason}",
        symbol=symbol,
        direction=Direction.BEARISH,
        reject_reason=reject_reason,
        signal_timestamp=ts,
        confluence_score=confluence,
        confluence_factors=["divergence_signal"],
        hypothetical_outcome=hypothetical,
    )


# ── Core replay-logic tests (no Optuna needed) ──────────────────────────────


def test_simulate_trade_outcome_threshold_accepts_and_rejects() -> None:
    """Trade with confluence=4.0 must pass threshold 2.0, fail at 5.0."""
    trade = _mk_trade(confluence=4.0, pnl_r=2.0)
    loose = ConfigOverride(confluence_threshold_global=2.0)
    tight = ConfigOverride(confluence_threshold_global=5.0)

    ok_loose, outcome_loose, r_loose = simulate_trade_outcome(trade, loose)
    ok_tight, outcome_tight, r_tight = simulate_trade_outcome(trade, tight)

    assert ok_loose is True
    assert outcome_loose == "WIN"
    assert r_loose == pytest.approx(2.0)

    assert ok_tight is False
    assert outcome_tight == "FILTERED"
    assert r_tight == 0.0


def test_simulate_trade_outcome_respects_per_symbol_threshold() -> None:
    """Per-symbol override beats the global threshold."""
    trade = _mk_trade(symbol="ETH-USDT-SWAP", confluence=3.0, pnl_r=1.5)
    cfg = ConfigOverride(
        confluence_threshold_global=2.0,
        confluence_threshold_per_symbol={"ETH-USDT-SWAP": 4.0},
    )
    ok, outcome, _ = simulate_trade_outcome(trade, cfg)
    assert ok is False
    assert outcome == "FILTERED"


def test_simulate_reject_outcome_gate_flip_unblocks_winner() -> None:
    """Reject with reason=vwap_misaligned becomes accepted when vwap
    gate is disabled, and the +R flows from hypothetical_outcome=WIN."""
    reject = _mk_reject(reject_reason="vwap_misaligned", hypothetical="WIN")
    gate_off = ConfigOverride(vwap_hard_veto_enabled=False)
    gate_on = ConfigOverride(vwap_hard_veto_enabled=True)

    ok_off, outcome_off, r_off = simulate_reject_outcome(reject, gate_off)
    ok_on, outcome_on, r_on = simulate_reject_outcome(reject, gate_on)

    assert ok_off is True
    assert outcome_off == "WIN"
    assert r_off == pytest.approx(1.5)  # default WIN estimate

    assert ok_on is False
    assert outcome_on == "STILL_REJECTED"
    assert r_on == 0.0


def test_simulate_reject_outcome_below_confluence_threshold_path() -> None:
    """below_confluence reject with score 4.0 accepts under threshold 3.5."""
    reject = _mk_reject(
        reject_reason="below_confluence",
        confluence=4.0,
        hypothetical="LOSS",
    )
    loose = ConfigOverride(confluence_threshold_global=3.5)
    tight = ConfigOverride(confluence_threshold_global=4.5)

    ok_loose, outcome_loose, r_loose = simulate_reject_outcome(reject, loose)
    ok_tight, _, _ = simulate_reject_outcome(reject, tight)

    assert ok_loose is True
    assert outcome_loose == "LOSS"
    assert r_loose == pytest.approx(-1.0)

    assert ok_tight is False


def test_simulate_reject_outcome_neither_books_zero_r() -> None:
    """Accepting an unpegged/NEITHER reject books 0 R — no counter-factual."""
    reject_neither = _mk_reject(hypothetical="NEITHER")
    reject_unpegged = _mk_reject(hypothetical=None)
    cfg = ConfigOverride(vwap_hard_veto_enabled=False)

    for r in (reject_neither, reject_unpegged):
        ok, outcome, pnl = simulate_reject_outcome(r, cfg)
        assert ok is True
        assert outcome == "NEITHER"
        assert pnl == 0.0


def test_replay_config_aggregates_trades_and_rejects() -> None:
    """Two wins + one loss on trades, one gate-flip win on rejects."""
    trades = [
        _mk_trade(outcome=TradeOutcome.WIN, pnl_r=2.0, confluence=4.0),
        _mk_trade(outcome=TradeOutcome.WIN, pnl_r=1.8, confluence=3.5),
        _mk_trade(outcome=TradeOutcome.LOSS, pnl_r=-1.0, confluence=4.5),
    ]
    rejects = [
        _mk_reject(reject_reason="ema_momentum_contra", hypothetical="WIN"),
    ]
    cfg = ConfigOverride(
        confluence_threshold_global=2.0,
        ema_veto_enabled=False,
    )
    m = replay_config(trades, rejects, cfg)
    assert isinstance(m, DatasetMetrics)
    assert m.n_trades_accepted == 4
    assert m.n_wins == 3
    assert m.n_losses == 1
    assert m.net_r == pytest.approx(2.0 + 1.8 - 1.0 + 1.5)
    assert m.win_rate == pytest.approx(0.75)


# ── Pass 3 Faz-A: pillar_weights ───────────────────────────────────────────


def _mk_trade_with_pillars(
    *, pillar_scores: dict[str, float], confluence: float = 4.0,
    outcome: TradeOutcome = TradeOutcome.WIN, pnl_r: float = 1.5,
) -> TradeRecord:
    t = _mk_trade(confluence=confluence, outcome=outcome, pnl_r=pnl_r)
    return t.model_copy(update={"confluence_pillar_scores": pillar_scores})


def _mk_reject_with_pillars(
    *, pillar_scores: dict[str, float], confluence: float = 3.5,
    reject_reason: str = "below_confluence", hypothetical: str = "WIN",
) -> RejectedSignal:
    r = _mk_reject(
        reject_reason=reject_reason, confluence=confluence,
        hypothetical=hypothetical,
    )
    return r.model_copy(update={"confluence_pillar_scores": pillar_scores})


def test_pillar_weights_empty_falls_back_to_stored_score() -> None:
    """No pillar_weights in cfg → score = stored confluence_score."""
    trade = _mk_trade_with_pillars(
        pillar_scores={"mss_alignment": 1.0, "vwap_composite": 1.0},
        confluence=4.0,
    )
    cfg = ConfigOverride(confluence_threshold_global=3.5)  # 4.0 >= 3.5 → pass
    ok, _, _ = simulate_trade_outcome(trade, cfg)
    assert ok is True


def test_pillar_weights_zero_disables_factor_filters_trade() -> None:
    """pillar_weights[factor]=0 disables that factor; effective score
    drops below threshold → FILTERED."""
    trade = _mk_trade_with_pillars(
        pillar_scores={"mss_alignment": 2.0, "vwap_composite": 2.0},
        confluence=4.0,  # stored score
    )
    cfg = ConfigOverride(
        confluence_threshold_global=3.5,
        pillar_weights={"mss_alignment": 0.0, "vwap_composite": 0.0},
    )
    # Effective: 2.0*0 + 2.0*0 = 0.0 < 3.5 → FILTERED
    ok, outcome, _ = simulate_trade_outcome(trade, cfg)
    assert ok is False
    assert outcome == "FILTERED"


def test_pillar_weights_amplifier_lifts_below_confluence_reject() -> None:
    """A reject with confluence_score=3.0 (below threshold 3.5) becomes
    accepted when pillar_weights amplify pillar contributions to >= 3.5."""
    reject = _mk_reject_with_pillars(
        pillar_scores={"mss_alignment": 1.0, "divergence_signal": 1.0},
        confluence=3.0,
        reject_reason="below_confluence",
        hypothetical="WIN",
    )
    cfg = ConfigOverride(
        confluence_threshold_global=3.5,
        pillar_weights={"mss_alignment": 2.0, "divergence_signal": 2.0},
    )
    # Effective: 1.0*2 + 1.0*2 = 4.0 >= 3.5 → accept
    ok, outcome, r = simulate_reject_outcome(reject, cfg)
    assert ok is True
    assert outcome == "WIN"
    assert r == pytest.approx(1.5)


def test_pillar_weights_with_empty_pillar_scores_falls_back() -> None:
    """Row with empty pillar_scores dict — pillar_weights ignored,
    falls back to stored confluence_score."""
    trade = _mk_trade_with_pillars(pillar_scores={}, confluence=4.0)
    cfg = ConfigOverride(
        confluence_threshold_global=3.5,
        pillar_weights={"mss_alignment": 2.0},
    )
    ok, _, _ = simulate_trade_outcome(trade, cfg)
    assert ok is True  # stored 4.0 >= 3.5


# ── Pass 3 Faz-A: zone_max_wait_bars ───────────────────────────────────────


def test_zone_max_wait_bars_extended_unblocks_zone_timeout_cancel() -> None:
    """zone_max_wait_bars > baseline → zone_timeout_cancel reject ACCEPTED;
    outcome from stored hypothetical_outcome."""
    reject = _mk_reject(
        reject_reason="zone_timeout_cancel", hypothetical="WIN",
    )
    cfg = ConfigOverride(
        zone_max_wait_bars=5,  # baseline default 2
    )
    ok, outcome, r = simulate_reject_outcome(reject, cfg)
    assert ok is True
    assert outcome == "WIN"
    assert r == pytest.approx(1.5)


def test_zone_max_wait_bars_at_baseline_keeps_reject() -> None:
    """zone_max_wait_bars == baseline → no unblock; STILL_REJECTED."""
    reject = _mk_reject(
        reject_reason="zone_timeout_cancel", hypothetical="WIN",
    )
    cfg = ConfigOverride(zone_max_wait_bars=2)  # == baseline
    ok, outcome, _ = simulate_reject_outcome(reject, cfg)
    assert ok is False
    assert outcome == "STILL_REJECTED"


def test_zone_max_wait_bars_none_keeps_reject() -> None:
    """zone_max_wait_bars=None (Pass 1 default) → no unblock."""
    reject = _mk_reject(
        reject_reason="zone_timeout_cancel", hypothetical="WIN",
    )
    cfg = ConfigOverride()  # all defaults
    ok, _, _ = simulate_reject_outcome(reject, cfg)
    assert ok is False


def test_zone_max_wait_bars_does_not_affect_other_reject_reasons() -> None:
    """zone_max_wait_bars knob ONLY targets zone_timeout_cancel rows."""
    reject = _mk_reject(
        reject_reason="ema_momentum_contra", hypothetical="WIN",
    )
    cfg = ConfigOverride(zone_max_wait_bars=10)  # large value
    ok, _, _ = simulate_reject_outcome(reject, cfg)
    assert ok is False  # ema gate still ON, not unblocked


# ── Pass 3 Faz-A: target_rr_ratio + kline_cache re-walk ────────────────────


def _mk_reject_for_rewalk(
    *, symbol: str = "BTC-USDT-SWAP",
    direction: Direction = Direction.BULLISH,
    price: float = 100.0,
    proposed_sl: float = 99.0,
    signal_ts: datetime = None,
    reject_reason: str = "vwap_misaligned",
    hypothetical: str = "LOSS",  # original peg outcome
) -> RejectedSignal:
    ts = signal_ts or datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    return RejectedSignal(
        rejection_id=f"rw_{symbol}_{ts.isoformat()}",
        symbol=symbol,
        direction=direction,
        reject_reason=reject_reason,
        signal_timestamp=ts,
        price=price,
        atr=0.5,
        proposed_sl_price=proposed_sl,
        proposed_tp_price=price + (price - proposed_sl) * 1.5,  # baseline 1:1.5
        proposed_rr_ratio=1.5,
        hypothetical_outcome=hypothetical,
    )


def _seed_kline_cache(
    cache: KlineCache, *, signal_ts: datetime, klines: list[Kline],
    bybit_symbol: str = "BTCUSDT",
    interval_minutes: int = 3, max_bars: int = 100,
) -> None:
    from src.strategy.kline_walk import signal_ts_to_bar_start_ms
    start_ms = signal_ts_to_bar_start_ms(
        signal_ts, interval_minutes=interval_minutes,
    )
    cache.put(
        bybit_symbol=bybit_symbol, interval_minutes=interval_minutes,
        start_ms=start_ms, max_bars=max_bars, klines=klines,
    )


def test_target_rr_ratio_no_kline_cache_falls_back_to_stored_outcome(tmp_path) -> None:
    """target_rr_ratio set but no kline_cache → stored hypothetical_outcome."""
    reject = _mk_reject_for_rewalk(hypothetical="LOSS")
    cfg = ConfigOverride(
        vwap_hard_veto_enabled=False,  # unblock via gate toggle
        target_rr_ratio=2.0,  # set but cache absent → fallback
    )
    ok, outcome, r = simulate_reject_outcome(reject, cfg, kline_cache=None)
    assert ok is True
    assert outcome == "LOSS"  # from stored hypothetical
    assert r == pytest.approx(-1.0)


def test_target_rr_ratio_with_cache_rewalks_to_fresh_outcome(tmp_path) -> None:
    """target_rr_ratio set + kline_cache primed → fresh walk overrides
    stored outcome. Test: original LOSS, but with extended target_rr=3.0
    the new TP is far enough away that the SL hits first → still LOSS
    BUT bars_to_sl semantics confirmed via fresh walk."""
    cache = KlineCache(tmp_path / "kc.db")
    sig_ts = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    # Klines: 100 bars, low gradually drops to 98 (hits sl=99 at bar 5),
    # high never reaches the new 3.0R TP (103)
    klines = []
    for i in range(100):
        # Bar i (0-indexed walk position): low = 100-0.5*i, high=100+0.1*i
        low = 100.0 - 0.5 * i
        high = 100.0 + 0.1 * i
        klines.append(Kline(
            bar_start_ms=int(sig_ts.timestamp() * 1000)
            + (i + 1) * 3 * 60 * 1000,
            open=low, high=high, low=low, close=high,
        ))
    _seed_kline_cache(cache, signal_ts=sig_ts, klines=klines)

    reject = _mk_reject_for_rewalk(
        signal_ts=sig_ts,
        price=100.0, proposed_sl=99.0,
        hypothetical="WIN",  # original peg said WIN, but fresh walk says LOSS
    )
    cfg = ConfigOverride(
        vwap_hard_veto_enabled=False,  # unblock
        target_rr_ratio=3.0,  # extended TP far away
    )
    ok, outcome, r = simulate_reject_outcome(
        reject, cfg, kline_cache=cache,
    )
    assert ok is True
    assert outcome == "LOSS"  # fresh walk overrode stored WIN
    assert r == pytest.approx(-1.0)


def test_target_rr_ratio_with_cache_miss_falls_back_to_stored(tmp_path) -> None:
    """target_rr_ratio set + cache provided but cache MISS for this row
    → fallback to stored hypothetical_outcome (no walk)."""
    cache = KlineCache(tmp_path / "kc.db")  # empty cache
    reject = _mk_reject_for_rewalk(hypothetical="WIN")
    cfg = ConfigOverride(
        vwap_hard_veto_enabled=False,
        target_rr_ratio=2.0,
    )
    ok, outcome, r = simulate_reject_outcome(reject, cfg, kline_cache=cache)
    assert ok is True
    assert outcome == "WIN"  # stored hypothetical wins (no fresh walk possible)
    assert r == pytest.approx(1.5)


def test_replay_with_pillar_reweight_alias_delegates_to_replay_config() -> None:
    """Backward-compat alias test — same dataset, both functions yield
    identical metrics."""
    trades = [
        _mk_trade(outcome=TradeOutcome.WIN, pnl_r=2.0, confluence=4.0),
        _mk_trade(outcome=TradeOutcome.LOSS, pnl_r=-1.0, confluence=3.5),
    ]
    rejects = [_mk_reject(hypothetical="WIN")]
    cfg = ConfigOverride(
        confluence_threshold_global=2.0,
        vwap_hard_veto_enabled=False,
        pillar_weights={"mss_alignment": 1.5},
    )
    m_a = replay_config(trades, rejects, cfg)
    m_b = replay_with_pillar_reweight(trades, rejects, cfg)
    assert m_a.net_r == m_b.net_r
    assert m_a.n_trades_accepted == m_b.n_trades_accepted
    assert m_a.win_rate == m_b.win_rate


# ── Smoke test — run_tune end-to-end ────────────────────────────────────────


try:
    import optuna  # noqa: F401
    _HAS_OPTUNA = True
except ImportError:
    _HAS_OPTUNA = False


@pytest.mark.skipif(not _HAS_OPTUNA, reason="optuna not installed")
def test_tune_smoke_runs_ten_trials() -> None:
    """run_tune returns the expected dict shape and positive stats."""
    from scripts.tune_confluence import run_tune

    base = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    trades: list[TradeRecord] = []
    # 8 trades, mix of wins and losses at varying confluence to give
    # the TPE sampler something non-degenerate.
    for i in range(8):
        outcome = TradeOutcome.WIN if i % 3 != 0 else TradeOutcome.LOSS
        pnl_r = 1.8 if outcome == TradeOutcome.WIN else -1.0
        trades.append(_mk_trade(
            outcome=outcome,
            pnl_r=pnl_r,
            confluence=2.5 + 0.3 * i,
            entry_ts=base + timedelta(hours=i),
        ))
    rejects = [
        _mk_reject(
            reject_reason="vwap_misaligned",
            hypothetical="WIN",
            signal_ts=base + timedelta(hours=9),
        ),
        _mk_reject(
            reject_reason="below_confluence",
            confluence=3.2,
            hypothetical="LOSS",
            signal_ts=base + timedelta(hours=10),
        ),
        _mk_reject(
            reject_reason="ema_momentum_contra",
            hypothetical="WIN",
            signal_ts=base + timedelta(hours=11),
        ),
    ]

    result = run_tune(
        trades, rejects,
        n_trials=10,
        train_frac=0.75,
        seed=7,
    )

    # Key shape — docstring contract.
    for key in (
        "best_config", "best_params", "train_metrics", "validate_metrics",
        "study", "train_trades", "validate_trades",
        "train_rejects", "validate_rejects",
    ):
        assert key in result, f"run_tune missing key {key!r}"

    assert isinstance(result["best_config"], ConfigOverride)
    assert isinstance(result["train_metrics"], DatasetMetrics)
    assert isinstance(result["validate_metrics"], DatasetMetrics)
    assert result["train_metrics"].n_trades_accepted >= 0
    assert result["validate_metrics"].n_trades_accepted >= 0

    # Train set should be non-empty (75% of 8 = 6 trades).
    assert len(result["train_trades"]) == 6
    assert len(result["validate_trades"]) == 2


@pytest.mark.skipif(not _HAS_OPTUNA, reason="optuna not installed")
def test_tune_smoke_renders_report_without_error() -> None:
    """Report rendering must tolerate a real tune-run output without
    raising — the operator runs this via CLI directly."""
    from scripts.tune_confluence import render_report, run_tune

    base = datetime(2026, 4, 5, 12, 0, tzinfo=UTC)
    trades = [
        _mk_trade(outcome=TradeOutcome.WIN, pnl_r=2.0,
                  confluence=3.5, entry_ts=base + timedelta(hours=i))
        for i in range(6)
    ]
    rejects: list[RejectedSignal] = []

    result = run_tune(trades, rejects, n_trials=5, train_frac=0.73, seed=1)
    report = render_report(
        trades_train=result["train_trades"],
        trades_validate=result["validate_trades"],
        rejects_train=result["train_rejects"],
        rejects_validate=result["validate_rejects"],
        best_cfg=result["best_config"],
        train_metrics=result["train_metrics"],
        validate_metrics=result["validate_metrics"],
        study=result["study"],
        n_trials=result["n_trials"],
        seed=result["seed"],
    )
    # Essential section headers present.
    assert "# Pass 1 Confluence + Gate Tune Report" in report
    assert "## 1. Dataset summary" in report
    assert "## 2. Best config" in report
    assert "## 3. Metrics" in report
    assert "## 4. Overfit checks" in report
    assert "## 6. Pass 2 extension note" in report


def test_walk_forward_split_preserves_order() -> None:
    """Sanity check — earlier rows land in train, later in validate."""
    from scripts.tune_confluence import walk_forward_split

    base = datetime(2026, 4, 1, tzinfo=UTC)
    trades = [
        _mk_trade(entry_ts=base + timedelta(hours=i))
        for i in range(10)
    ]
    tr, va, rej_tr, rej_va = walk_forward_split(trades, [], 0.7)
    assert len(tr) == 7
    assert len(va) == 3
    # Train must end strictly before validate begins (time-ordered).
    assert tr[-1].entry_timestamp < va[0].entry_timestamp
    assert rej_tr == []
    assert rej_va == []
