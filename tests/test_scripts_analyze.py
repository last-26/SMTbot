"""Smoke test for scripts/analyze.py — end-to-end tiny journal → report.

Seeds a disk-backed journal via `tmp_path` with ~6 trades (mix of
WIN/LOSS/BREAKEVEN, different symbols, filled `confluence_pillar_scores`
dicts) plus 3 rejected signals (with pegged counter-factuals). Calls
`run_analysis` programmatically (not through subprocess) so the test is
fast and gets direct import coverage of the CLI core.

Gracefully skips the GBT assertion if xgboost fails to fit — the fixture
is intentionally small, and GBT behaviour on ~5 decisive rows is allowed
to degrade. The dataset-summary + per-factor-WR sections are the
load-bearing contract.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.models import Direction
from src.execution.models import (
    AlgoResult,
    CloseFill,
    ExecutionReport,
    OrderResult,
    OrderStatus,
    PositionState,
)
from src.journal.database import TradeJournal
from src.strategy.trade_plan import TradePlan


UTC = timezone.utc


# ── Fixture builders (mirror tests/test_journal_database.py style) ──────────


def _plan(
    *,
    direction: Direction = Direction.BULLISH,
    entry: float = 67_000.0,
    sl: float = 66_500.0,
    tp: float = 68_500.0,
    factors: list[str] | None = None,
    pillar_scores: dict[str, float] | None = None,
    confluence: float = 5.0,
) -> TradePlan:
    return TradePlan(
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        rr_ratio=abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 3.0,
        sl_distance=abs(entry - sl),
        sl_pct=abs(entry - sl) / entry,
        position_size_usdt=1_000.0,
        leverage=10,
        required_leverage=10.0,
        num_contracts=5,
        risk_amount_usdt=10.0,
        max_risk_usdt=10.0,
        capped=False,
        sl_source="order_block",
        confluence_score=confluence,
        confluence_factors=list(factors or ["mss_alignment", "vwap_composite"]),
        confluence_pillar_scores=dict(pillar_scores or {
            "mss_alignment": 1.5, "vwap_composite": 1.25,
        }),
        reason="test plan",
    )


def _report() -> ExecutionReport:
    return ExecutionReport(
        entry=OrderResult(
            order_id=f"ORD-{datetime.now().microsecond}",
            client_order_id="cliORD",
            status=OrderStatus.PENDING,
        ),
        algo=AlgoResult(
            algo_id="ALGO-1", client_algo_id="cliALGO-1",
            sl_trigger_px=66_500.0, tp_trigger_px=68_500.0,
        ),
        state=PositionState.OPEN,
        leverage_set=True,
    )


def _close(pnl: float, closed_at: datetime, exit_px: float = 67_500.0) -> CloseFill:
    return CloseFill(
        inst_id="X-USDT-SWAP", pos_side="long",
        entry_price=67_000.0, exit_price=exit_px, size=5.0,
        pnl_usdt=pnl, closed_at=closed_at,
    )


async def _seed_journal(db_path: Path) -> None:
    """Write 6 closed trades (3 WIN / 2 LOSS / 1 BREAKEVEN) + 3 rejects."""
    async with TradeJournal(str(db_path)) as j:
        base = datetime(2026, 4, 21, 10, 0, tzinfo=UTC)

        # 3 BTC wins, 2 BTC losses, 1 ETH breakeven, varied factors + pillars.
        trade_specs = [
            # (symbol, direction, factors, pillars, pnl, hours_offset, session, trend_regime, confluence)
            ("BTC-USDT-SWAP", Direction.BULLISH,
             ["mss_alignment", "vwap_composite", "oscillator_high_conviction_signal"],
             {"mss_alignment": 1.5, "vwap_composite": 1.25, "oscillator_high_conviction_signal": 1.5},
             30.0, 0, "london", "STRONG_TREND", 5.5),
            ("BTC-USDT-SWAP", Direction.BULLISH,
             ["mss_alignment", "vwap_composite"],
             {"mss_alignment": 1.5, "vwap_composite": 1.25},
             25.0, 1, "london", "WEAK_TREND", 4.5),
            ("BTC-USDT-SWAP", Direction.BEARISH,
             ["mss_alignment", "divergence_signal"],
             {"mss_alignment": 1.5, "divergence_signal": 1.25},
             20.0, 2, "new_york", "RANGING", 4.75),
            ("BTC-USDT-SWAP", Direction.BULLISH,
             ["money_flow_alignment"],
             {"money_flow_alignment": 1.0},
             -10.0, 3, "new_york", "RANGING", 3.25),
            ("ETH-USDT-SWAP", Direction.BEARISH,
             ["mss_alignment"],
             {"mss_alignment": 1.5},
             -10.0, 4, "asia", "WEAK_TREND", 3.0),
            ("ETH-USDT-SWAP", Direction.BULLISH,
             ["vwap_composite"],
             {"vwap_composite": 1.25},
             0.0, 5, "london", "UNKNOWN", 3.5),
        ]
        for (symbol, direction, factors, pillars, pnl, h_off, session,
             trend_regime, confluence) in trade_specs:
            signal_ts = base + timedelta(hours=h_off)
            opened = await j.record_open(
                _plan(direction=direction, factors=factors,
                      pillar_scores=pillars, confluence=confluence),
                _report(),
                symbol=symbol,
                signal_timestamp=signal_ts,
                entry_timestamp=signal_ts,
                session=session,
                trend_regime_at_entry=trend_regime,
                regime_at_entry="BALANCED",
                # Tag half of trades with on-chain context to exercise
                # Arkham segmentation.
                on_chain_context=(
                    {"daily_macro_bias": "bullish", "altcoin_index": 55.0,
                     "stablecoin_pulse_1h_usd": 1_000_000.0,
                     "whale_blackout_active": False}
                    if h_off % 2 == 0 else None
                ),
                confluence_pillar_scores=pillars,
            )
            exit_ts = signal_ts + timedelta(minutes=30)
            await j.record_close(
                opened.trade_id,
                _close(pnl, closed_at=exit_ts),
            )

        # 3 rejected_signals — one WIN counter-factual, one LOSS, one NEITHER.
        for i, (reason, outcome) in enumerate([
            ("vwap_misaligned", "WIN"),
            ("ema_momentum_contra", "LOSS"),
            ("below_confluence", "NEITHER"),
        ]):
            rej = await j.record_rejected_signal(
                symbol="BTC-USDT-SWAP",
                direction=Direction.BULLISH,
                reject_reason=reason,
                signal_timestamp=base + timedelta(hours=10 + i),
                confluence_score=4.0,
                confluence_factors=["mss_alignment"],
                confluence_pillar_scores={"mss_alignment": 1.5},
                on_chain_context={"daily_macro_bias": "bearish"},
            )
            await j.update_rejected_outcome(
                rej.rejection_id,
                hypothetical_outcome=outcome,
                bars_to_tp=5 if outcome == "WIN" else None,
                bars_to_sl=5 if outcome == "LOSS" else None,
            )


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_run_analysis_writes_report_with_required_sections(tmp_path):
    """End-to-end: seed → run_analysis → assert file exists and contains
    the load-bearing sections."""
    from scripts.analyze import run_analysis

    db_path = tmp_path / "trades.db"
    await _seed_journal(db_path)

    output_path = tmp_path / "reports" / "analyze_test.md"
    body = await run_analysis(
        db_path=str(db_path),
        output_path=str(output_path),
        since=None,
        ignore_clean_since=True,  # fixture timestamps are in the past
        print_stdout=False,
    )

    # File was written to disk under the requested path.
    assert output_path.exists(), f"output {output_path} not created"
    written = output_path.read_text(encoding="utf-8")
    assert written == body, "returned body diverges from written file"

    # Load-bearing section headers — these are the contract for downstream
    # tooling / changelog entries that reference the report structure.
    assert "# Phase 9 GBT Analysis Report" in body
    assert "## 1. Dataset summary" in body
    assert "## 4. Per-factor WR" in body
    assert "## 5. Per-regime / per-session / per-symbol WR" in body
    assert "## 6. Rejected-signals counter-factual" in body
    assert "## 7. Arkham segmentation" in body
    assert "## 8. Pass 1 tuning recommendations" in body
    assert "## 9. Pass 2 hypotheses" in body

    # The Arkham caveat must land in the report (not-a-tuning-target guard).
    assert "DESCRIPTIVE ONLY" in body

    # Dataset summary must count our 6 trades and name both symbols.
    assert "Closed trades: **6**" in body
    assert "BTC-USDT-SWAP" in body
    assert "ETH-USDT-SWAP" in body


async def test_run_analysis_missing_db_writes_stub(tmp_path):
    """DB path doesn't exist → graceful stub report (no crash)."""
    from scripts.analyze import run_analysis

    missing_db = tmp_path / "does_not_exist.db"
    output_path = tmp_path / "stub.md"
    body = await run_analysis(
        db_path=str(missing_db),
        output_path=str(output_path),
        since=None,
        ignore_clean_since=True,
        print_stdout=False,
    )
    assert output_path.exists()
    assert "DB not found" in body


async def test_run_analysis_empty_journal_returns_dataset_only(tmp_path):
    """Empty journal (schema but no rows) renders the summary section with
    an empty-dataset message rather than crashing the GBT path."""
    from scripts.analyze import run_analysis

    db_path = tmp_path / "empty.db"
    async with TradeJournal(str(db_path)) as j:
        pass  # schema-only init

    output_path = tmp_path / "empty_report.md"
    body = await run_analysis(
        db_path=str(db_path),
        output_path=str(output_path),
        since=None,
        ignore_clean_since=True,
        print_stdout=False,
    )
    assert output_path.exists()
    # Must render the header + dataset summary even when empty. GBT sections
    # are guarded by the <10-trades fallback.
    assert "# Phase 9 GBT Analysis Report" in body
    assert "## 1. Dataset summary" in body
    assert "Insufficient data for GBT" in body
