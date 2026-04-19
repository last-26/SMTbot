"""Tests for src.journal.reporter — pure metric functions."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from src.data.models import Direction
from src.journal.models import TradeOutcome, TradeRecord
from src.journal.reporter import (
    avg_r,
    calmar,
    equity_curve,
    expectancy_r,
    format_summary,
    max_consecutive_losses,
    max_consecutive_wins,
    max_drawdown,
    profit_factor,
    sharpe_r,
    summary,
    win_rate,
    win_rate_by_factor,
    win_rate_by_factor_combo,
    win_rate_by_score_bucket,
    win_rate_by_session,
    win_rate_by_symbol,
)


UTC = timezone.utc
_T = datetime(2026, 4, 16, 12, tzinfo=UTC)


def _rec(
    *,
    outcome: TradeOutcome,
    pnl_usdt: float,
    pnl_r: float,
    session: str | None = None,
    factors: list[str] | None = None,
    fees: float = 0.0,
    symbol: str = "BTC-USDT-SWAP",
    confluence_score: float = 0.0,
) -> TradeRecord:
    return TradeRecord(
        trade_id=f"t-{id(object())}",
        symbol=symbol,
        direction=Direction.BULLISH,
        outcome=outcome,
        signal_timestamp=_T,
        entry_timestamp=_T,
        exit_timestamp=_T,
        entry_price=67_000.0, sl_price=66_500.0, tp_price=68_500.0,
        rr_ratio=3.0, leverage=10, num_contracts=5,
        position_size_usdt=1_000.0, risk_amount_usdt=10.0,
        confluence_factors=factors or [],
        confluence_score=confluence_score,
        session=session,
        pnl_usdt=pnl_usdt, pnl_r=pnl_r, fees_usdt=fees,
    )


def _win(r: float = 1.0, **kw) -> TradeRecord:
    return _rec(outcome=TradeOutcome.WIN, pnl_usdt=r * 10.0, pnl_r=r, **kw)


def _loss(r: float = 1.0, **kw) -> TradeRecord:
    return _rec(outcome=TradeOutcome.LOSS, pnl_usdt=-r * 10.0, pnl_r=-r, **kw)


# ── win_rate ────────────────────────────────────────────────────────────────


def test_win_rate_all_wins():
    assert win_rate([_win(), _win(), _win()]) == 1.0


def test_win_rate_mixed():
    # 3W, 2L → 0.6
    trades = [_win(), _win(), _win(), _loss(), _loss()]
    assert win_rate(trades) == 0.6


def test_win_rate_empty_is_zero():
    assert win_rate([]) == 0.0


# ── profit_factor ───────────────────────────────────────────────────────────


def test_profit_factor_basic():
    trades = [_win(r=1.0), _win(r=1.0), _win(r=1.0), _loss(r=1.0)]
    # sum(wins)=30, |sum(losses)|=10 → 3.0
    assert profit_factor(trades) == 3.0


def test_profit_factor_no_losses_is_inf():
    assert profit_factor([_win(), _win()]) == math.inf


def test_profit_factor_no_wins_is_zero():
    assert profit_factor([_loss(), _loss()]) == 0.0


# ── streaks ─────────────────────────────────────────────────────────────────


def test_max_consecutive_losses():
    # W L L L W → 3
    trades = [_win(), _loss(), _loss(), _loss(), _win()]
    assert max_consecutive_losses(trades) == 3
    assert max_consecutive_wins(trades) == 1


# ── drawdown / equity ───────────────────────────────────────────────────────


def test_max_drawdown_known_curve():
    # +10, +10, -30 on $100 balance → curve: 100, 110, 120, 90. DD = 30, 25%.
    trades = [_win(r=1.0), _win(r=1.0), _loss(r=3.0)]
    dd_usdt, dd_pct = max_drawdown(trades, starting_balance=100.0)
    assert dd_usdt == 30.0
    assert dd_pct == 25.0


def test_equity_curve_tracks_fees():
    trades = [_win(r=1.0, fees=1.0)]  # pnl=10, fees=1 → +9
    curve = equity_curve(trades, starting_balance=100.0)
    assert curve == [100.0, 109.0]


# ── bucketing ───────────────────────────────────────────────────────────────


def test_win_rate_by_session():
    trades = [
        _win(session="LONDON"),
        _loss(session="LONDON"),
        _win(session="NEW_YORK"),
        _win(session="NEW_YORK"),
    ]
    result = win_rate_by_session(trades)
    assert result["LONDON"] == 0.5
    assert result["NEW_YORK"] == 1.0


def test_win_rate_by_factor_counts_trade_per_factor():
    trades = [
        _win(factors=["OB_test", "FVG_active"]),
        _loss(factors=["OB_test"]),
    ]
    result = win_rate_by_factor(trades)
    # OB_test: 1W/2 = 0.5, FVG_active: 1W/1 = 1.0
    assert result["OB_test"] == 0.5
    assert result["FVG_active"] == 1.0


# ── sharpe / calmar ─────────────────────────────────────────────────────────


def test_sharpe_positive_when_mean_positive():
    trades = [_win(r=1.0), _win(r=2.0), _loss(r=0.5)]
    assert sharpe_r(trades) > 0


def test_sharpe_zero_when_constant_returns():
    trades = [_win(r=1.0), _win(r=1.0), _win(r=1.0)]
    # std == 0 → short-circuit to 0.0, not NaN
    assert sharpe_r(trades) == 0.0


def test_calmar_known_curve():
    # +10, +10, -30 on $100 → ending 90, total_return=-10%, DD=25%
    trades = [_win(r=1.0), _win(r=1.0), _loss(r=3.0)]
    result = calmar(trades, starting_balance=100.0)
    # -10 / 25 = -0.4
    assert result == -0.4


# ── win_rate_by_symbol ──────────────────────────────────────────────────────


def test_win_rate_by_symbol_buckets_correctly():
    trades = [
        _win(symbol="BTC-USDT-SWAP"),
        _win(symbol="BTC-USDT-SWAP"),
        _loss(symbol="ETH-USDT-SWAP"),
        _loss(symbol="ETH-USDT-SWAP"),
    ]
    result = win_rate_by_symbol(trades)
    assert result["BTC-USDT-SWAP"]["num_trades"] == 2
    assert result["BTC-USDT-SWAP"]["win_rate"] == 1.0
    assert result["ETH-USDT-SWAP"]["win_rate"] == 0.0
    assert result["ETH-USDT-SWAP"]["avg_r"] == -1.0


def test_win_rate_by_symbol_empty_returns_empty():
    assert win_rate_by_symbol([]) == {}


# ── win_rate_by_factor_combo ────────────────────────────────────────────────


def test_factor_combo_key_is_sorted_and_joined():
    trades = [
        _win(factors=["B", "A"]),
        _loss(factors=["A", "B"]),
    ]
    result = win_rate_by_factor_combo(trades, min_trades=1)
    # keys deterministic regardless of input order — alphabetical
    assert "A,B" in result
    assert result["A,B"]["num_trades"] == 2
    assert result["A,B"]["win_rate"] == 0.5


def test_factor_combo_pools_rare_under_threshold():
    trades = [
        _win(factors=["A", "B"]),
        _win(factors=["A", "B"]),  # combo seen twice → included
        _loss(factors=["C"]),  # singleton → pooled under RARE
    ]
    result = win_rate_by_factor_combo(trades, min_trades=2)
    assert "A,B" in result
    assert result["A,B"]["num_trades"] == 2
    assert result["A,B"]["win_rate"] == 1.0
    assert "C" not in result
    assert result["RARE"]["num_trades"] == 1
    assert result["RARE"]["win_rate"] == 0.0


def test_factor_combo_empty_factors_bucket_none():
    trades = [_win(factors=[]), _win(factors=[])]
    result = win_rate_by_factor_combo(trades)
    assert "NONE" in result
    assert result["NONE"]["num_trades"] == 2


# ── win_rate_by_score_bucket ────────────────────────────────────────────────


def test_score_bucket_assigns_by_range():
    trades = [
        _win(confluence_score=2.5),  # falls in 2.0-3.0
        _win(confluence_score=2.5),
        _loss(confluence_score=4.5),  # falls in 4.0-5.0
    ]
    result = win_rate_by_score_bucket(trades)
    assert result["2.0-3.0"]["num_trades"] == 2
    assert result["2.0-3.0"]["win_rate"] == 1.0
    assert result["4.0-5.0"]["num_trades"] == 1
    assert result["4.0-5.0"]["win_rate"] == 0.0
    # unvisited buckets still emit stable shape with num_trades=0
    assert result["3.0-4.0"]["num_trades"] == 0


def test_score_bucket_half_open_boundary():
    # upper bound is EXCLUSIVE — 3.0 belongs to 3.0-4.0, not 2.0-3.0
    trades = [_win(confluence_score=3.0)]
    result = win_rate_by_score_bucket(trades)
    assert result["2.0-3.0"]["num_trades"] == 0
    assert result["3.0-4.0"]["num_trades"] == 1


def test_score_bucket_open_top_bucket_captures_high_scores():
    trades = [_win(confluence_score=7.5)]
    result = win_rate_by_score_bucket(trades)
    assert result["5.0+"]["num_trades"] == 1


# ── summary / format ────────────────────────────────────────────────────────


def test_summary_contains_expected_keys():
    trades = [_win(session="LONDON", factors=["OB"]),
              _loss(session="LONDON", factors=["FVG"])]
    s = summary(trades, starting_balance=1_000.0)
    expected = {
        "num_trades", "num_wins", "num_losses", "win_rate", "avg_r",
        "expectancy_r", "profit_factor", "max_consecutive_wins",
        "max_consecutive_losses", "max_drawdown_usdt", "max_drawdown_pct",
        "sharpe_r", "calmar", "starting_balance", "ending_balance",
        "total_return_pct", "win_rate_by_session", "win_rate_by_factor",
        "win_rate_by_symbol", "win_rate_by_factor_combo",
        "win_rate_by_score_bucket",
    }
    assert expected.issubset(s.keys())
    assert s["num_trades"] == 2
    assert s["expectancy_r"] == avg_r(trades)


def test_format_summary_renders_nonempty_report():
    trades = [_win(session="LONDON", factors=["OB_test"])]
    out = format_summary(summary(trades, starting_balance=1_000.0))
    assert "Trade journal report" in out
    assert "Win rate" in out
    assert "LONDON" in out
    assert "OB_test" in out


def test_format_summary_renders_new_breakdowns():
    trades = [
        _win(symbol="BTC-USDT-SWAP", confluence_score=3.5, factors=["OB", "FVG"]),
        _win(symbol="BTC-USDT-SWAP", confluence_score=3.5, factors=["OB", "FVG"]),
        _loss(symbol="ETH-USDT-SWAP", confluence_score=2.5, factors=["Sweep"]),
    ]
    out = format_summary(summary(trades, starting_balance=5_000.0))
    assert "Win rate by symbol" in out
    assert "BTC-USDT-SWAP" in out
    assert "ETH-USDT-SWAP" in out
    assert "Win rate by confluence-score bucket" in out
    assert "3.0-4.0" in out
    assert "Win rate by factor combo" in out
    assert "FVG,OB" in out  # sorted combo key
