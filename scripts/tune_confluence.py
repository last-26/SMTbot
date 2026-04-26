"""Pass 1 Bayesian optimisation — non-Arkham knobs via Optuna + replay.

Phase 9 pre-work. Replays the journal through
``scripts.replay_decisions.replay_config`` under candidate
``ConfigOverride`` values suggested by Optuna, scores each on net-R +
Sharpe - max-DD, and reports the best config with a train/validate
walk-forward split.

Pass 1 scope: confluence threshold (global + per-symbol) and three hard
gate toggles (VWAP, EMA-momentum, cross-asset-opposition). Pass 2
(per-pillar weights + Arkham knobs) bolts on via the scaffold already in
``replay_decisions.py``.

Walk-forward split:
    Closed trades sorted by entry_timestamp; first ``train_frac`` used
    for Optuna search, remainder held out as validate. Rejects split by
    signal_timestamp using the same fraction. Report flags overfit when
    validate.net_r < 0.5 * train.net_r or win_rate drops >10pp.

Usage::

    .venv/Scripts/python.exe scripts/tune_confluence.py
    .venv/Scripts/python.exe scripts/tune_confluence.py --n-trials 500 \\
        --train-frac 0.73 --output reports/tune_$(date +%Y%m%d).md
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.journal.database import TradeJournal
from src.journal.models import RejectedSignal, TradeRecord
from scripts.replay_decisions import (
    ConfigOverride,
    DatasetMetrics,
    replay_config,
)


# Optuna is optional for smoke-testing the library path; wire it lazily
# so unit-tests that only exercise replay_decisions pass even if the
# environment is missing the package. run_tune will raise clearly if
# the import is missing.
try:  # pragma: no cover — presence depends on env
    import optuna
    from optuna.trial import Trial
    _HAS_OPTUNA = True
except ImportError:  # pragma: no cover
    optuna = None  # type: ignore
    Trial = None  # type: ignore
    _HAS_OPTUNA = False


_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "DOGE-USDT-SWAP", "XRP-USDT-SWAP")


# ── CLI / config plumbing (mirrors factor_audit.py) ─────────────────────────


def _parse_window(arg: str) -> Optional[datetime]:
    if arg == "all":
        return None
    m = re.fullmatch(r"(\d+)([dh])", arg)
    if not m:
        raise argparse.ArgumentTypeError(
            f"--last must be 'all' or NNd/NNh (got {arg!r})"
        )
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(days=n) if unit == "d" else timedelta(hours=n)
    return datetime.now(tz=timezone.utc) - delta


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_db_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    cfg = _load_cfg()
    return (cfg.get("journal") or {}).get("db_path", "data/trades.db")


def _resolve_clean_since() -> Optional[datetime]:
    cfg = _load_cfg()
    raw = (cfg.get("rl") or {}).get("clean_since")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Walk-forward split ──────────────────────────────────────────────────────


def walk_forward_split(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    train_frac: float,
) -> tuple[list[TradeRecord], list[TradeRecord],
           list[RejectedSignal], list[RejectedSignal]]:
    """Split both series by the first ``train_frac`` fraction of rows.

    Returns (train_trades, validate_trades, train_rejects, validate_rejects).
    Trades are sorted by entry_timestamp, rejects by signal_timestamp.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1); got {train_frac!r}")

    # Sort defensively — upstream queries already ORDER BY, but be safe.
    sorted_trades = sorted(
        trades,
        key=lambda t: t.entry_timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc),
    )
    sorted_rejects = sorted(
        rejects,
        key=lambda r: r.signal_timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc),
    )

    n_trade_train = max(1, int(len(sorted_trades) * train_frac)) if sorted_trades else 0
    n_reject_train = int(len(sorted_rejects) * train_frac) if sorted_rejects else 0

    train_trades = sorted_trades[:n_trade_train]
    validate_trades = sorted_trades[n_trade_train:]
    train_rejects = sorted_rejects[:n_reject_train]
    validate_rejects = sorted_rejects[n_reject_train:]
    return train_trades, validate_trades, train_rejects, validate_rejects


# ── Suggest / objective ─────────────────────────────────────────────────────


def suggest_config(trial, symbols: tuple[str, ...] = _SYMBOLS) -> ConfigOverride:
    """Draw one ConfigOverride from the Optuna trial.

    Search space:
      * ``confluence_threshold_global`` — continuous [2.0, 5.0]
      * ``confluence_threshold_per_symbol[S]`` — continuous [2.0, 5.0]
        drawn only when ``use_per_symbol`` flag is True; else left empty.
      * 3x bool gate toggles, independent categoricals.
    """
    thr_global = trial.suggest_float("confluence_threshold_global", 2.0, 5.0)
    use_per_symbol = trial.suggest_categorical("use_per_symbol", [False, True])
    per_symbol: dict[str, float] = {}
    if use_per_symbol:
        for sym in symbols:
            per_symbol[sym] = trial.suggest_float(
                f"threshold_{sym}", 2.0, 5.0,
            )
    vwap = trial.suggest_categorical("vwap_hard_veto_enabled", [False, True])
    ema = trial.suggest_categorical("ema_veto_enabled", [False, True])
    xopp = trial.suggest_categorical("cross_asset_opposition_enabled", [False, True])
    return ConfigOverride(
        confluence_threshold_global=thr_global,
        confluence_threshold_per_symbol=per_symbol,
        vwap_hard_veto_enabled=vwap,
        ema_veto_enabled=ema,
        cross_asset_opposition_enabled=xopp,
    )


def score_metrics(m: DatasetMetrics) -> float:
    """The Optuna objective. Higher is better.

    Weighting rationale: net_r dominates (0.6) because our North Star is
    cumulative edge; Sharpe (0.3) rewards consistency; max-DD (0.1)
    discourages high-variance win streaks. Sharpe is scaled x2 so a
    1.0 Sharpe contributes 0.6 toward the sum — in-range with net_r.
    max_dd is subtracted, also x2 so a 2.0 DD costs 0.4.
    """
    return (
        0.6 * m.net_r
        + 0.3 * m.sharpe_r * 2.0
        - 0.1 * max(0.0, m.max_dd_r * 2.0)
    )


def objective(
    trial,
    train_trades: list[TradeRecord],
    train_rejects: list[RejectedSignal],
    *,
    min_trades: int = 5,
) -> float:
    cfg = suggest_config(trial)
    m = replay_config(train_trades, train_rejects, cfg)
    if m.n_trades_accepted < min_trades:
        return -1e6
    return score_metrics(m)


# ── Reporting ───────────────────────────────────────────────────────────────


_DEFAULT_CURRENT_CFG = ConfigOverride(
    confluence_threshold_global=2.0,
    vwap_hard_veto_enabled=True,
    ema_veto_enabled=True,
    cross_asset_opposition_enabled=True,
)


def _metrics_row(label: str, m: DatasetMetrics) -> str:
    return (
        f"| {label} | {m.n_trades_accepted} | {m.n_wins} | {m.n_losses} | "
        f"{m.win_rate*100:+.2f}% | {m.avg_r:+.3f}R | {m.net_r:+.3f}R | "
        f"{m.sharpe_r:+.3f} | {m.max_dd_r:.3f}R |"
    )


def _yaml_diff_block(best: ConfigOverride, current: ConfigOverride) -> str:
    lines: list[str] = ["```yaml"]
    lines.append(f"# Current vs. best tuned config (Pass 1)")
    lines.append(f"confluence_threshold_global: {best.confluence_threshold_global:.3f}"
                 f"    # current: {current.confluence_threshold_global:.3f}")
    if best.confluence_threshold_per_symbol:
        lines.append("confluence_threshold_per_symbol:")
        for sym, thr in sorted(best.confluence_threshold_per_symbol.items()):
            lines.append(f"  {sym}: {thr:.3f}")
    else:
        lines.append("confluence_threshold_per_symbol: {}   # global applies")
    lines.append(f"vwap_hard_veto_enabled: {best.vwap_hard_veto_enabled}"
                 f"    # current: {current.vwap_hard_veto_enabled}")
    lines.append(f"ema_veto_enabled: {best.ema_veto_enabled}"
                 f"    # current: {current.ema_veto_enabled}")
    lines.append(f"cross_asset_opposition_enabled: {best.cross_asset_opposition_enabled}"
                 f"    # current: {current.cross_asset_opposition_enabled}")
    lines.append("```")
    return "\n".join(lines)


def _overfit_warning(train: DatasetMetrics, validate: DatasetMetrics) -> list[str]:
    warnings: list[str] = []
    if train.net_r > 0 and validate.net_r < 0.5 * train.net_r:
        warnings.append(
            f"- **WARN: validate.net_r ({validate.net_r:+.3f}R) < 0.5 * "
            f"train.net_r ({train.net_r:+.3f}R)** — likely overfit."
        )
    wr_delta_pp = (train.win_rate - validate.win_rate) * 100.0
    if wr_delta_pp > 10.0:
        warnings.append(
            f"- **WARN: win_rate dropped {wr_delta_pp:.2f}pp** "
            f"(train {train.win_rate*100:.2f}% → validate {validate.win_rate*100:.2f}%) — "
            "parameter set learned train noise."
        )
    dd_delta = validate.max_dd_r - train.max_dd_r
    if dd_delta > 1.5:
        warnings.append(
            f"- Note: validate max_dd_r {validate.max_dd_r:.3f}R exceeds "
            f"train {train.max_dd_r:.3f}R by {dd_delta:+.3f}R — watch live."
        )
    if not warnings:
        warnings.append("- No overfit red flags (train vs validate within bounds).")
    return warnings


def _format_trial_leaderboard(study, top_n: int = 10) -> list[str]:
    lines = ["", f"## Top {top_n} trials (by train objective)", ""]
    lines.append("| Rank | Value | Threshold | VWAP | EMA | X-opp | Per-sym |")
    lines.append("|------|-------|-----------|------|-----|-------|---------|")
    try:
        done_trials = [t for t in study.trials if t.value is not None]
    except Exception:
        done_trials = []
    done_trials.sort(key=lambda t: t.value if t.value is not None else -1e18,
                     reverse=True)
    for i, t in enumerate(done_trials[:top_n], start=1):
        p = t.params
        thr = p.get("confluence_threshold_global", float("nan"))
        vwap = p.get("vwap_hard_veto_enabled", "-")
        ema = p.get("ema_veto_enabled", "-")
        xopp = p.get("cross_asset_opposition_enabled", "-")
        per_sym = p.get("use_per_symbol", "-")
        lines.append(
            f"| {i} | {t.value:+.3f} | {thr:.3f} | {vwap} | {ema} | {xopp} | {per_sym} |"
        )
    return lines


def render_report(
    *,
    trades_train: list[TradeRecord],
    trades_validate: list[TradeRecord],
    rejects_train: list[RejectedSignal],
    rejects_validate: list[RejectedSignal],
    best_cfg: ConfigOverride,
    train_metrics: DatasetMetrics,
    validate_metrics: DatasetMetrics,
    study=None,
    n_trials: int = 0,
    seed: Optional[int] = None,
) -> str:
    """Emit a markdown report. Keep the shape plain — factor_audit.py
    leans text, this leans markdown because the operator pastes it into
    the changelog."""
    lines: list[str] = []
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines.append("# Pass 1 Confluence + Gate Tune Report")
    lines.append("")
    lines.append(f"_Generated {now}_")
    lines.append(f"_n_trials={n_trials}, seed={seed}_")
    lines.append("")
    # 1. Dataset summary
    lines.append("## 1. Dataset summary")
    lines.append("")
    lines.append(
        f"- Trades: {len(trades_train)} train / {len(trades_validate)} validate"
    )
    lines.append(
        f"- Rejects: {len(rejects_train)} train / {len(rejects_validate)} validate"
    )
    lines.append("")
    # 2. Best config vs current
    lines.append("## 2. Best config (diff vs current defaults)")
    lines.append("")
    lines.append(_yaml_diff_block(best_cfg, _DEFAULT_CURRENT_CFG))
    lines.append("")
    # 3. Metrics table
    lines.append("## 3. Metrics — train vs validate")
    lines.append("")
    lines.append("| Split | N | W | L | WR | avg_R | net_R | Sharpe | max_DD |")
    lines.append("|-------|---|---|---|----|-------|-------|--------|--------|")
    lines.append(_metrics_row("Train", train_metrics))
    lines.append(_metrics_row("Validate", validate_metrics))
    lines.append("")
    # 4. Overfit warning
    lines.append("## 4. Overfit checks")
    lines.append("")
    lines.extend(_overfit_warning(train_metrics, validate_metrics))
    lines.append("")
    # 5. Leaderboard
    if study is not None:
        lines.extend(_format_trial_leaderboard(study, top_n=10))
        lines.append("")
    # 6. Pass 2 scaffold note
    lines.append("## 6. Pass 2 extension note")
    lines.append("")
    lines.append(
        "Arkham knobs (daily_bias_delta, stablecoin_pulse_penalty, "
        "altcoin_index_penalty, flow_alignment_penalty) + per-pillar "
        "weights will be added in Pass 2. Scaffold present in "
        "`scripts/replay_decisions.py` "
        "(`replay_with_pillar_reweight` + `ConfigOverride.pillar_weights`); "
        "extension should only require adding Optuna `suggest_float` "
        "calls to `suggest_config` and wiring the richer replay entry."
    )
    lines.append("")
    return "\n".join(lines)


# ── Main runner ─────────────────────────────────────────────────────────────


def run_tune(
    trades: list[TradeRecord],
    rejects: list[RejectedSignal],
    *,
    n_trials: int = 300,
    train_frac: float = 0.73,
    seed: Optional[int] = 42,
) -> dict:
    """Run Optuna search end-to-end on an in-memory dataset.

    Returns a dict with keys: ``best_config``, ``best_params``,
    ``train_metrics``, ``validate_metrics``, ``study``, ``train_trades``,
    ``validate_trades``, ``train_rejects``, ``validate_rejects``. The
    dict shape is what the smoke test asserts against.
    """
    if not _HAS_OPTUNA:
        raise ImportError(
            "optuna is required for tune_confluence but is not installed. "
            "Activate .venv and `pip install optuna` before running."
        )

    train_trades, validate_trades, train_rejects, validate_rejects = (
        walk_forward_split(trades, rejects, train_frac)
    )

    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(
        lambda trial: objective(trial, train_trades, train_rejects),
        n_trials=n_trials,
        show_progress_bar=False,
    )

    # Rebuild ConfigOverride from the best trial's params. suggest_config
    # does this inside the objective but Optuna doesn't cache the returned
    # object — so we recreate deterministically from params.
    best = study.best_trial
    params = best.params
    best_cfg = ConfigOverride(
        confluence_threshold_global=params.get("confluence_threshold_global", 2.0),
        vwap_hard_veto_enabled=params.get("vwap_hard_veto_enabled", True),
        ema_veto_enabled=params.get("ema_veto_enabled", True),
        cross_asset_opposition_enabled=params.get(
            "cross_asset_opposition_enabled", True,
        ),
    )
    if params.get("use_per_symbol"):
        for sym in _SYMBOLS:
            key = f"threshold_{sym}"
            if key in params:
                best_cfg.confluence_threshold_per_symbol[sym] = params[key]

    train_metrics = replay_config(train_trades, train_rejects, best_cfg)
    validate_metrics = replay_config(validate_trades, validate_rejects, best_cfg)

    return {
        "best_config": best_cfg,
        "best_params": params,
        "train_metrics": train_metrics,
        "validate_metrics": validate_metrics,
        "study": study,
        "train_trades": train_trades,
        "validate_trades": validate_trades,
        "train_rejects": train_rejects,
        "validate_rejects": validate_rejects,
        "n_trials": n_trials,
        "seed": seed,
    }


async def _fetch_dataset(
    db_path: str,
    since: Optional[datetime],
) -> tuple[list[TradeRecord], list[RejectedSignal]]:
    async with TradeJournal(db_path) as j:
        trades = await j.list_closed_trades(since=since)
        rejects = await j.list_rejected_signals(since=since)
    return trades, rejects


def _default_output_path() -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"reports/tune_{stamp}.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pass 1 Optuna tune — confluence threshold + hard gates",
    )
    parser.add_argument("--db", default=None, help="Path to trades.db")
    parser.add_argument(
        "--last", default="30d",
        help="Window: '7d', '14d', '30d', '12h', 'all' (default 30d)",
    )
    parser.add_argument("--n-trials", type=int, default=300,
                        help="Optuna trials (default 300)")
    parser.add_argument("--train-frac", type=float, default=0.73,
                        help="Walk-forward train fraction (default 0.73)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Optuna TPE sampler seed (default 42)")
    parser.add_argument("--output", default=None,
                        help="Report output path (default reports/tune_{TIMESTAMP}.md)")
    parser.add_argument(
        "--ignore-clean-since", action="store_true",
        help="Include rows before `rl.clean_since` (default: honour cutoff)",
    )
    args = parser.parse_args()

    if not _HAS_OPTUNA:
        print("[ERROR] optuna is not installed. Run `pip install optuna` and retry.",
              file=sys.stderr)
        return 2

    db_path = _resolve_db_path(args.db)
    try:
        since = _parse_window(args.last)
    except argparse.ArgumentTypeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    if not args.ignore_clean_since:
        cs = _resolve_clean_since()
        if cs is not None:
            since = cs if since is None else max(since, cs)

    if not Path(db_path).exists() and db_path != ":memory:":
        print(f"[WARN] DB not found at {db_path} — nothing to tune.")
        return 0

    trades, rejects = asyncio.run(_fetch_dataset(db_path, since))
    if not trades and not rejects:
        window = "all time" if since is None else f"since {since.isoformat()}"
        print(f"No trades or rejects in window ({window}).")
        return 0

    result = run_tune(
        trades, rejects,
        n_trials=args.n_trials,
        train_frac=args.train_frac,
        seed=args.seed,
    )

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

    out_path = args.output or _default_output_path()
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(report, encoding="utf-8")
    print(f"Wrote {out_file}")
    print(f"Best objective: {result['study'].best_value:+.4f}")
    tm = result["train_metrics"]
    vm = result["validate_metrics"]
    print(f"Train : n={tm.n_trades_accepted} net_r={tm.net_r:+.3f}R "
          f"wr={tm.win_rate*100:.2f}% sharpe={tm.sharpe_r:+.3f}")
    print(f"Valid.: n={vm.n_trades_accepted} net_r={vm.net_r:+.3f}R "
          f"wr={vm.win_rate*100:.2f}% sharpe={vm.sharpe_r:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
