"""Phase 9 GBT analysis — xgboost + SHAP over the trade journal.

The analysis half of Phase 9. Reads `trades` + `rejected_signals` from the
journal, builds a feature matrix, trains WIN-vs-LOSS classifier and
`pnl_r` regressor, surfaces feature importance + SHAP direction-of-effect
for the top features, and lays out a markdown report with Pass 1 tuning
recommendations plus Pass 2 hypotheses (deferred).

Read-only; never writes to the journal. Honours `rl.clean_since` from
config unless `--ignore-clean-since` is passed. Descriptive on Arkham
segments (coverage still inconsistent on early post-pivot data); Pass 2
re-runs on a uniform-coverage slice once coverage stabilises.

Usage::

    .venv/Scripts/python.exe scripts/analyze.py --last 30d
    .venv/Scripts/python.exe scripts/analyze.py --last all --ignore-clean-since
    .venv/Scripts/python.exe scripts/analyze.py --output reports/my_analyze.md

Programmatic (used by the smoke test)::

    await run_analysis(db_path=..., output_path=..., since=None,
                       ignore_clean_since=True)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.journal.database import TradeJournal
from src.journal.models import RejectedSignal, TradeOutcome, TradeRecord


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


# ── Core math helpers ───────────────────────────────────────────────────────


def _wr(wins: int, losses: int) -> float:
    total = wins + losses
    return (wins / total) if total > 0 else 0.0


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+6.2f}%"


def _actual_wr(trades: list[TradeRecord]) -> tuple[int, int, float]:
    wins = sum(1 for t in trades if t.outcome == TradeOutcome.WIN)
    losses = sum(1 for t in trades if t.outcome == TradeOutcome.LOSS)
    return wins, losses, _wr(wins, losses)


def _avg_r(trades: list[TradeRecord]) -> float:
    rs = [t.pnl_r for t in trades if t.pnl_r is not None]
    return sum(rs) / len(rs) if rs else 0.0


def _sharpe_r(trades: list[TradeRecord]) -> float:
    """Sharpe on per-trade R. Degenerate for very small samples — returns 0
    when we have <2 datapoints or stdev of 0 (avoids div-by-zero)."""
    rs = [t.pnl_r for t in trades if t.pnl_r is not None]
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return mean / std


def _max_drawdown_r(trades: list[TradeRecord]) -> float:
    """Max cumulative-R drawdown across trades in entry order. Returns a
    non-positive number (0 when the equity-in-R curve never dips)."""
    running = 0.0
    peak = 0.0
    mdd = 0.0
    ordered = sorted(
        (t for t in trades if t.pnl_r is not None),
        key=lambda t: t.entry_timestamp,
    )
    for t in ordered:
        running += t.pnl_r or 0.0
        peak = max(peak, running)
        mdd = min(mdd, running - peak)
    return mdd


# ── Feature extraction ──────────────────────────────────────────────────────


def _trade_to_feature_row(t: TradeRecord, all_factors: list[str],
                          all_pillars: list[str]) -> dict:
    """Flatten a TradeRecord into a flat dict row for pandas.

    Keeps categorical strings in raw form — one-hot encoding happens
    downstream via `pd.get_dummies` on the assembled DataFrame.
    """
    row: dict = {
        "confluence_score": float(t.confluence_score or 0.0),
        "funding_z_at_entry": t.funding_z_at_entry,
        "ls_ratio_at_entry": t.ls_ratio_at_entry,
        "oi_change_24h_at_entry": t.oi_change_24h_at_entry,
        "liq_imbalance_1h_at_entry": t.liq_imbalance_1h_at_entry,
        "hour_of_day": t.entry_timestamp.hour if t.entry_timestamp else None,
        "symbol": t.symbol,
        "direction": t.direction.value,
        "session": t.session or "UNKNOWN",
        "trend_regime": t.trend_regime_at_entry or "UNKNOWN",
        "regime": t.regime_at_entry or "UNKNOWN",
        "sl_source": t.sl_source or "UNKNOWN",
        "setup_zone_source": t.setup_zone_source or "UNKNOWN",
    }
    # Factor presence — one-hot over all_factors so the feature space is stable.
    factor_set = set(t.confluence_factors or [])
    for factor in all_factors:
        row[f"f_{factor}"] = 1 if factor in factor_set else 0
    # Per-pillar raw scores — one column per pillar seen across the dataset.
    pillars = t.confluence_pillar_scores or {}
    for pname in all_pillars:
        row[f"p_{pname}"] = float(pillars.get(pname, 0.0))
    # Target variables.
    row["_target_win"] = 1 if t.outcome == TradeOutcome.WIN else 0
    row["_target_r"] = float(t.pnl_r or 0.0)
    row["_outcome"] = t.outcome.value
    return row


def _collect_factor_universe(trades: list[TradeRecord]) -> list[str]:
    """Unique factor names seen across all trades, sorted for stability."""
    seen: set[str] = set()
    for t in trades:
        for f in t.confluence_factors or []:
            seen.add(f)
    return sorted(seen)


def _collect_pillar_universe(trades: list[TradeRecord]) -> list[str]:
    """Unique pillar-score keys seen across all trades, sorted."""
    seen: set[str] = set()
    for t in trades:
        for k in (t.confluence_pillar_scores or {}).keys():
            seen.add(k)
    return sorted(seen)


# ── Report sections (stdout / markdown) ─────────────────────────────────────


def _render_dataset_summary(trades: list[TradeRecord]) -> list[str]:
    lines = ["## 1. Dataset summary", ""]
    if not trades:
        lines.append("_No closed trades in window._")
        return lines
    w, l, wr = _actual_wr(trades)
    be = sum(1 for t in trades if t.outcome == TradeOutcome.BREAKEVEN)
    avg_r = _avg_r(trades)
    sharpe = _sharpe_r(trades)
    mdd = _max_drawdown_r(trades)
    earliest = min(t.entry_timestamp for t in trades)
    latest = max(t.entry_timestamp for t in trades)
    lines.append(
        f"- Closed trades: **{len(trades)}** (W={w} / L={l} / BE={be})"
    )
    lines.append(f"- Date range: {earliest.isoformat()} → {latest.isoformat()}")
    lines.append(f"- Decisive WR: {_fmt_pct(wr)}")
    lines.append(f"- Avg R: {avg_r:+.3f}R  |  Sharpe-R: {sharpe:+.3f}  |  Max DD (cum-R): {mdd:+.3f}R")
    lines.append("")
    lines.append("### Per-symbol breakdown")
    lines.append("")
    lines.append("| symbol | n | W | L | WR | avg_R |")
    lines.append("|---|---|---|---|---|---|")
    by_symbol: dict[str, list[TradeRecord]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    for sym, recs in sorted(by_symbol.items(), key=lambda kv: -len(kv[1])):
        sw, sl, swr = _actual_wr(recs)
        sar = _avg_r(recs)
        lines.append(
            f"| {sym} | {len(recs)} | {sw} | {sl} | {_fmt_pct(swr)} | {sar:+.3f}R |"
        )
    return lines


def _render_gbt_importance(
    trades: list[TradeRecord],
    all_factors: list[str],
    all_pillars: list[str],
) -> tuple[list[str], Optional[object], Optional[object], Optional[object]]:
    """Trains XGBClassifier (WIN v LOSS) and XGBRegressor (pnl_r). Returns
    the markdown lines plus the trained models + DataFrame so the SHAP step
    can reuse them. Models are None when we fall back."""
    lines = ["## 2. GBT feature importance (primary features)", ""]
    try:
        import pandas as pd
        import xgboost as xgb
    except ImportError as e:
        lines.append(
            f"_xgboost / pandas not installed ({e}); skipping GBT step._"
        )
        return lines, None, None, None

    # Build frame. Drop BREAKEVEN rows from classifier (they're not in {WIN, LOSS}).
    rows = [_trade_to_feature_row(t, all_factors, all_pillars) for t in trades]
    df = pd.DataFrame(rows)
    decisive = df[df["_outcome"].isin([TradeOutcome.WIN.value,
                                       TradeOutcome.LOSS.value])].copy()

    if len(decisive) < 6:
        lines.append(
            "_< 6 decisive (WIN/LOSS) trades — not enough to fit a GBT. "
            "Skipping classifier & regressor._"
        )
        return lines, None, None, df

    # One-hot the categoricals. Drop target + outcome columns before dummies.
    categorical = [
        "symbol", "direction", "session", "trend_regime", "regime",
        "sl_source", "setup_zone_source",
    ]
    X = pd.get_dummies(
        decisive.drop(columns=["_target_win", "_target_r", "_outcome"]),
        columns=categorical,
        dummy_na=False,
    )
    # Coerce every column to numeric (floats); bools from get_dummies OK.
    X = X.apply(lambda c: c.astype(float), axis=0)
    y_win = decisive["_target_win"].astype(int)
    y_r = decisive["_target_r"].astype(float)

    # Classifier — WIN vs LOSS.
    try:
        clf = xgb.XGBClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            objective="binary:logistic", eval_metric="logloss",
            tree_method="hist", n_jobs=1, random_state=42,
            use_label_encoder=False,
            verbosity=0,
        )
        clf.fit(X, y_win)
    except Exception as e:  # noqa: BLE001 — broad by design, report and move on
        lines.append(f"_Classifier fit failed: {e.__class__.__name__}: {e}_")
        clf = None

    # Regressor — pnl_r.
    try:
        reg = xgb.XGBRegressor(
            n_estimators=100, max_depth=3, learning_rate=0.1,
            objective="reg:squarederror", tree_method="hist",
            n_jobs=1, random_state=42, verbosity=0,
        )
        reg.fit(X, y_r)
    except Exception as e:  # noqa: BLE001
        lines.append(f"_Regressor fit failed: {e.__class__.__name__}: {e}_")
        reg = None

    # Top-20 importances by 'gain' (model-intrinsic). Fallback to default if
    # the booster doesn't expose a gain map for some reason (very small data).
    def _rank_importance(model) -> list[tuple[str, float]]:
        if model is None:
            return []
        try:
            booster = model.get_booster()
            gain = booster.get_score(importance_type="gain")
        except Exception:  # noqa: BLE001
            return []
        # Map f0/f1/... → real names. On sklearn wrapper, feature_names is set.
        names = list(X.columns)
        out: list[tuple[str, float]] = []
        for k, v in gain.items():
            if k.startswith("f") and k[1:].isdigit():
                idx = int(k[1:])
                name = names[idx] if 0 <= idx < len(names) else k
            else:
                name = k
            out.append((name, float(v)))
        return sorted(out, key=lambda kv: kv[1], reverse=True)

    if clf is not None:
        lines.append("### Classifier (WIN vs LOSS) — top 20 by gain")
        lines.append("")
        lines.append("| rank | feature | gain |")
        lines.append("|---|---|---|")
        for i, (name, gain) in enumerate(_rank_importance(clf)[:20], start=1):
            lines.append(f"| {i} | `{name}` | {gain:.3f} |")
        lines.append("")

    if reg is not None:
        lines.append("### Regressor (pnl_r) — top 20 by gain")
        lines.append("")
        lines.append("| rank | feature | gain |")
        lines.append("|---|---|---|")
        for i, (name, gain) in enumerate(_rank_importance(reg)[:20], start=1):
            lines.append(f"| {i} | `{name}` | {gain:.3f} |")
        lines.append("")

    return lines, clf, reg, X


def _render_shap_summary(
    clf, X, all_pillars: list[str], all_factors: list[str],
) -> list[str]:
    """Top-5 SHAP mean |value| with sign-of-effect narrative on primary
    features. Primary = pillar-score columns + confluence_score +
    funding/ls/oi/liq numerics. Factor one-hots are secondary and dropped
    from this view to avoid noise."""
    lines = ["## 3. SHAP summary (primary features)", ""]
    if clf is None or X is None or len(X) < 6:
        lines.append("_Skipped — classifier not trained._")
        return lines
    try:
        import numpy as np
        import shap
    except ImportError as e:
        lines.append(f"_shap not installed ({e}); skipping._")
        return lines

    primary_prefixes = ("p_",)
    primary_explicit = {
        "confluence_score", "funding_z_at_entry", "ls_ratio_at_entry",
        "oi_change_24h_at_entry", "liq_imbalance_1h_at_entry", "hour_of_day",
    }
    primary_cols = [
        c for c in X.columns
        if c.startswith(primary_prefixes) or c in primary_explicit
    ]
    if not primary_cols:
        lines.append("_No primary features in feature matrix._")
        return lines

    try:
        explainer = shap.TreeExplainer(clf)
        values = explainer.shap_values(X)
    except Exception as e:  # noqa: BLE001
        lines.append(
            f"_SHAP explain failed ({e.__class__.__name__}: {e}); skipping._"
        )
        return lines

    # For binary classifier, shap_values may come back as (n_samples, n_features)
    # or as a list of two arrays. Normalise to (n_samples, n_features).
    if isinstance(values, list):
        values = values[-1]
    try:
        values = np.asarray(values)
    except Exception:  # noqa: BLE001
        lines.append("_SHAP returned unexpected shape; skipping._")
        return lines
    if values.ndim != 2:
        lines.append(
            f"_SHAP returned {values.ndim}-D array; expected 2. Skipping._"
        )
        return lines

    col_index = {c: i for i, c in enumerate(X.columns)}
    mean_abs: list[tuple[str, float, float]] = []
    for col in primary_cols:
        idx = col_index.get(col)
        if idx is None or idx >= values.shape[1]:
            continue
        shap_col = values[:, idx]
        mean_abs.append((
            col, float(np.mean(np.abs(shap_col))), float(np.mean(shap_col)),
        ))
    mean_abs.sort(key=lambda t: t[1], reverse=True)

    lines.append(
        "Top 5 primary features by mean |SHAP|. A **positive** mean SHAP "
        "pushes predictions toward WIN; negative pushes toward LOSS."
    )
    lines.append("")
    lines.append("| rank | feature | mean |SHAP| | mean SHAP | direction |")
    lines.append("|---|---|---|---|---|")
    for i, (name, mabs, mean) in enumerate(mean_abs[:5], start=1):
        direction = "→ WIN" if mean > 0 else ("→ LOSS" if mean < 0 else "neutral")
        lines.append(
            f"| {i} | `{name}` | {mabs:.4f} | {mean:+.4f} | {direction} |"
        )
    lines.append("")
    return lines


def _render_per_factor_wr(trades: list[TradeRecord]) -> list[str]:
    lines = ["## 4. Per-factor WR (top 15 by n_trades)", ""]
    if not trades:
        lines.append("_No trades._")
        return lines
    by_factor: dict[str, list[TradeRecord]] = {}
    for t in trades:
        for f in t.confluence_factors or []:
            by_factor.setdefault(f, []).append(t)
    if not by_factor:
        lines.append("_No factors in trade rows._")
        return lines
    lines.append("| factor | n | WR | avg_R |")
    lines.append("|---|---|---|---|")
    rows = sorted(by_factor.items(), key=lambda kv: -len(kv[1]))[:15]
    for factor, recs in rows:
        w, l, wr = _actual_wr(recs)
        lines.append(
            f"| `{factor}` | {len(recs)} | {_fmt_pct(wr)} | {_avg_r(recs):+.3f}R |"
        )
    return lines


def _render_slicing_tables(trades: list[TradeRecord]) -> list[str]:
    lines = ["## 5. Per-regime / per-session / per-symbol WR", ""]
    if not trades:
        lines.append("_No trades._")
        return lines

    def _render_slice(title: str, attr: str) -> list[str]:
        out = [f"### {title}", "", "| bucket | n | WR | avg_R |", "|---|---|---|---|"]
        groups: dict[str, list[TradeRecord]] = {}
        for t in trades:
            key = getattr(t, attr, None) or "UNKNOWN"
            if hasattr(key, "value"):
                key = key.value
            groups.setdefault(str(key), []).append(t)
        for k, recs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            w, l, wr = _actual_wr(recs)
            out.append(f"| {k} | {len(recs)} | {_fmt_pct(wr)} | {_avg_r(recs):+.3f}R |")
        out.append("")
        return out

    lines.extend(_render_slice("ADX trend regime (trend_regime_at_entry)",
                               "trend_regime_at_entry"))
    lines.extend(_render_slice("Session", "session"))
    lines.extend(_render_slice("Symbol", "symbol"))
    return lines


def _render_rejected_counterfactual(
    rejects: list[RejectedSignal],
) -> list[str]:
    lines = ["## 6. Rejected-signals counter-factual", ""]
    if not rejects:
        lines.append("_No rejected signals in window._")
        return lines
    pegged = [r for r in rejects if r.hypothetical_outcome in ("WIN", "LOSS")]
    if not pegged:
        lines.append("_No pegged (WIN/LOSS) rejects — run `peg_rejected_outcomes.py --commit` first._")
        return lines
    by_reason: dict[str, list[RejectedSignal]] = {}
    for r in pegged:
        by_reason.setdefault(r.reject_reason, []).append(r)
    lines.append(
        "Per reject_reason: how many counter-factual wins / losses were "
        "blocked? High n + high counterfactual_WR = over-blocking indicator."
    )
    lines.append("")
    lines.append("| reject_reason | n | n_blocked_winners | n_blocked_losers | counterfactual_WR |")
    lines.append("|---|---|---|---|---|")
    rows = sorted(by_reason.items(), key=lambda kv: -len(kv[1]))
    for reason, recs in rows:
        wins = sum(1 for r in recs if r.hypothetical_outcome == "WIN")
        losses = sum(1 for r in recs if r.hypothetical_outcome == "LOSS")
        wr = _wr(wins, losses)
        lines.append(
            f"| `{reason}` | {len(recs)} | {wins} | {losses} | {_fmt_pct(wr)} |"
        )
    return lines


_ARKHAM_CAVEAT = (
    "> **Caveat:** Arkham coverage is inconsistent across the current "
    "dataset (Arkham activated mid-way, some rows have `on_chain_context` "
    "populated while others do not). These segments are DESCRIPTIVE ONLY "
    "and must NOT be used as tuning targets. Pass 2 re-runs this section "
    "on a uniform-coverage slice (all rows post-activation)."
)


def _render_arkham_segmentation(trades: list[TradeRecord]) -> list[str]:
    lines = ["## 7. Arkham segmentation (descriptive only — Pass 1 scope)", ""]
    lines.append(_ARKHAM_CAVEAT)
    lines.append("")
    if not trades:
        lines.append("_No trades._")
        return lines

    active = [t for t in trades if t.on_chain_context is not None]
    inactive = [t for t in trades if t.on_chain_context is None]
    lines.append("### Arkham active vs inactive")
    lines.append("")
    lines.append("| bucket | n | WR | avg_R |")
    lines.append("|---|---|---|---|")
    for label, recs in [("arkham_active", active), ("arkham_inactive", inactive)]:
        w, l, wr = _actual_wr(recs)
        lines.append(
            f"| {label} | {len(recs)} | {_fmt_pct(wr)} | {_avg_r(recs):+.3f}R |"
        )
    lines.append("")

    # Bucket by daily_macro_bias (bullish / bearish / neutral / null).
    bias_buckets: dict[str, list[TradeRecord]] = {}
    for t in active:
        ctx = t.on_chain_context or {}
        bias = ctx.get("daily_macro_bias") or "UNKNOWN"
        bias_buckets.setdefault(str(bias), []).append(t)
    if bias_buckets:
        lines.append("### by daily_macro_bias (arkham_active only)")
        lines.append("")
        lines.append("| bias | n | WR | avg_R |")
        lines.append("|---|---|---|---|")
        for bias, recs in sorted(bias_buckets.items(), key=lambda kv: -len(kv[1])):
            w, l, wr = _actual_wr(recs)
            lines.append(
                f"| {bias} | {len(recs)} | {_fmt_pct(wr)} | {_avg_r(recs):+.3f}R |"
            )
        lines.append("")

    # Bucket by altcoin_index (tertiles 0-33 / 33-66 / 66-100). BTC/ETH
    # exempt per existing rules, but bucket them in too for visibility.
    alt_buckets: dict[str, list[TradeRecord]] = {
        "bitcoin_dominance(0-33)": [],
        "neutral(33-66)": [],
        "altseason(66-100)": [],
        "null": [],
    }
    for t in active:
        ctx = t.on_chain_context or {}
        idx = ctx.get("altcoin_index")
        if idx is None:
            alt_buckets["null"].append(t)
        elif idx < 33:
            alt_buckets["bitcoin_dominance(0-33)"].append(t)
        elif idx < 66:
            alt_buckets["neutral(33-66)"].append(t)
        else:
            alt_buckets["altseason(66-100)"].append(t)
    if any(alt_buckets.values()):
        lines.append("### by altcoin_index tertile (arkham_active only)")
        lines.append("")
        lines.append("| bucket | n | WR | avg_R |")
        lines.append("|---|---|---|---|")
        for bucket, recs in alt_buckets.items():
            w, l, wr = _actual_wr(recs)
            lines.append(
                f"| {bucket} | {len(recs)} | {_fmt_pct(wr)} | {_avg_r(recs):+.3f}R |"
            )
        lines.append("")
    return lines


def _render_pass1_recommendations(
    trades: list[TradeRecord], rejects: list[RejectedSignal],
) -> list[str]:
    """Concrete YAML-delta suggestions for NON-Arkham knobs.

    Deliberately conservative — these are observations, not prescriptions,
    and every suggestion is gated on a sample-size threshold."""
    lines = ["## 8. Pass 1 tuning recommendations (non-Arkham only)", ""]
    if len(trades) < 10:
        lines.append(
            "_< 10 trades; recommendations require more data. Dataset summary only._"
        )
        return lines

    recs: list[str] = []

    # (a) Global confluence threshold — look at the mean score of wins vs losses.
    wins = [t for t in trades if t.outcome == TradeOutcome.WIN]
    losses = [t for t in trades if t.outcome == TradeOutcome.LOSS]
    if wins and losses:
        mean_win = sum(t.confluence_score for t in wins) / len(wins)
        mean_loss = sum(t.confluence_score for t in losses) / len(losses)
        delta = mean_win - mean_loss
        if delta > 0.5 and mean_loss > 0:
            recs.append(
                f"- Consider bumping `analysis.min_confluence_score` — "
                f"losing trades average {mean_loss:.2f} vs wins {mean_win:.2f} "
                f"(Δ={delta:+.2f}). Raising the floor near {mean_loss + delta/2:.2f} "
                f"would cull the lower-score loss cluster."
            )
        elif delta < -0.1:
            recs.append(
                f"- WARN: losses score HIGHER than wins on average "
                f"(loss={mean_loss:.2f}, win={mean_win:.2f}). Suggests the scoring "
                f"is anti-correlated on this slice — re-examine weights per pillar."
            )

    # (b) Per-symbol — if a symbol has ≥6 trades with WR <20%, propose raising
    # its per-symbol threshold if one exists (implicit raise of the global floor
    # for that symbol via per-symbol override).
    per_symbol: dict[str, list[TradeRecord]] = {}
    for t in trades:
        per_symbol.setdefault(t.symbol, []).append(t)
    for sym, tlist in per_symbol.items():
        if len(tlist) < 6:
            continue
        w, l, wr = _actual_wr(tlist)
        if (w + l) >= 6 and wr < 0.20:
            recs.append(
                f"- `{sym}`: WR={_fmt_pct(wr)} across {len(tlist)} trades — consider "
                f"per-symbol confluence raise or temporary pause via YAML "
                f"`trading.symbols` removal."
            )
        elif (w + l) >= 6 and wr > 0.65:
            recs.append(
                f"- `{sym}`: WR={_fmt_pct(wr)} across {len(tlist)} trades — outperforming; "
                f"consider marginal risk bump via per-symbol override if sample >20."
            )

    # (c) Gate-enable/disable — from rejected_signals counter-factuals. A reject
    # reason with high counterfactual_WR + high n is flagged as potentially
    # over-blocking.
    pegged = [r for r in rejects if r.hypothetical_outcome in ("WIN", "LOSS")]
    by_reason: dict[str, list[RejectedSignal]] = {}
    for r in pegged:
        by_reason.setdefault(r.reject_reason, []).append(r)
    toggle_map = {
        "vwap_misaligned": "analysis.vwap_hard_veto_enabled",
        "ema_momentum_contra": "analysis.ema_veto_enabled",
        "cross_asset_opposition": "analysis.cross_asset_opposition_enabled",
    }
    for reason, recs_list in by_reason.items():
        if reason not in toggle_map:
            continue
        wins = sum(1 for r in recs_list if r.hypothetical_outcome == "WIN")
        losses = sum(1 for r in recs_list if r.hypothetical_outcome == "LOSS")
        total = wins + losses
        if total < 10:
            continue
        cf_wr = _wr(wins, losses)
        # Over-block warning — counterfactual_WR clearly better than live WR.
        actual_w, actual_l, actual_wr = _actual_wr(trades)
        if cf_wr > actual_wr + 0.10:
            recs.append(
                f"- `{reason}` gate may be over-blocking: counterfactual WR "
                f"{_fmt_pct(cf_wr)} (n={total}) exceeds live WR {_fmt_pct(actual_wr)}. "
                f"Consider flipping `{toggle_map[reason]}: false` as a probe, "
                f"or tightening threshold rather than hard-vetoing."
            )
        elif cf_wr < 0.25 and total >= 15:
            recs.append(
                f"- `{reason}` gate appears HEALTHY: counterfactual WR "
                f"{_fmt_pct(cf_wr)} (n={total}) — most blocked signals would "
                f"have lost. Keep `{toggle_map[reason]}: true`."
            )

    if not recs:
        lines.append(
            "_No recommendations cleared the sample-size / magnitude gates. "
            "Collect more trades and re-run._"
        )
        return lines
    lines.extend(recs)
    return lines


def _render_pass2_hypotheses(trades: list[TradeRecord]) -> list[str]:
    lines = ["## 9. Pass 2 hypotheses (deferred — Arkham coverage dependent)", ""]
    lines.append(
        "Observations from Arkham segmentation that are **NOT** acted on in "
        "Pass 1 because coverage is inconsistent. Revisit once post-"
        "activation data is uniform."
    )
    lines.append("")
    arkham_active = [t for t in trades if t.on_chain_context is not None]
    if not arkham_active:
        lines.append(
            "- No trades carry `on_chain_context` — Arkham integration may "
            "not be active in this window."
        )
        return lines

    # Daily-bias delta: compare WR of bullish-bias vs bearish-bias longs/shorts.
    bullish_longs = [t for t in arkham_active
                     if (t.on_chain_context or {}).get("daily_macro_bias") == "bullish"
                     and t.direction.value == "bullish"]
    bearish_shorts = [t for t in arkham_active
                      if (t.on_chain_context or {}).get("daily_macro_bias") == "bearish"
                      and t.direction.value == "bearish"]
    bullish_shorts = [t for t in arkham_active
                      if (t.on_chain_context or {}).get("daily_macro_bias") == "bullish"
                      and t.direction.value == "bearish"]
    bearish_longs = [t for t in arkham_active
                     if (t.on_chain_context or {}).get("daily_macro_bias") == "bearish"
                     and t.direction.value == "bullish"]

    def _emit(name: str, aligned: list[TradeRecord], against: list[TradeRecord],
              knob: str) -> Optional[str]:
        if not aligned or not against:
            return None
        aw, al, a_wr = _actual_wr(aligned)
        cw, cl, c_wr = _actual_wr(against)
        return (
            f"- **{name}** — aligned (n={len(aligned)}) WR={_fmt_pct(a_wr)} "
            f"vs against (n={len(against)}) WR={_fmt_pct(c_wr)}. If delta "
            f"persists in Pass 2 with N>80 per side, bump `{knob}`."
        )

    hyp1 = _emit(
        "Daily-bias aligned longs vs contra longs",
        bullish_longs, bearish_longs,
        "on_chain.daily_bias_modifier_delta",
    )
    if hyp1:
        lines.append(hyp1)
    hyp2 = _emit(
        "Daily-bias aligned shorts vs contra shorts",
        bearish_shorts, bullish_shorts,
        "on_chain.daily_bias_modifier_delta",
    )
    if hyp2:
        lines.append(hyp2)

    # Altcoin-index: if altcoin_index high + alt longs did well, might suggest
    # the altseason-short penalty is well-calibrated; inverse = candidate tweak.
    alt_active = [t for t in arkham_active
                  if t.symbol.split("-")[0] not in ("BTC", "ETH")
                  and (t.on_chain_context or {}).get("altcoin_index") is not None]
    alt_long_bear_btc = [
        t for t in alt_active if t.direction.value == "bullish"
        and (t.on_chain_context or {}).get("altcoin_index", 50) < 33
    ]
    if alt_long_bear_btc:
        w, l, wr = _actual_wr(alt_long_bear_btc)
        lines.append(
            f"- Altcoin longs during BTC-dominance (altcoin_index<33): "
            f"n={len(alt_long_bear_btc)} WR={_fmt_pct(wr)}. If n>30 and WR<30%, "
            f"bump `on_chain.altcoin_index_penalty`."
        )

    if len(lines) <= 3:
        lines.append(
            "- Insufficient Arkham-active decisive trades to observe patterns."
        )
    return lines


# ── Assembly ────────────────────────────────────────────────────────────────


def _assemble_report(
    trades: list[TradeRecord], rejects: list[RejectedSignal],
    *, since: Optional[datetime], generated_at: datetime,
) -> str:
    """Returns the full markdown report as a single string."""
    all_factors = _collect_factor_universe(trades)
    all_pillars = _collect_pillar_universe(trades)

    out: list[str] = []
    out.append("# Phase 9 GBT Analysis Report")
    out.append("")
    out.append(f"- Generated: {generated_at.isoformat()}")
    window = "all time" if since is None else f"since {since.isoformat()}"
    out.append(f"- Window: {window}")
    out.append(f"- Trade rows: {len(trades)}   Reject rows: {len(rejects)}")
    out.append(f"- Pillars observed: {', '.join(all_pillars) or '(none)'}")
    if not all_pillars:
        out.append(
            "- _NOTE: no rows carry `confluence_pillar_scores` (pre-migration DB "
            "or column empty). GBT sections fall back to `confluence_factors` "
            "one-hot only._"
        )
    out.append("")

    out.extend(_render_dataset_summary(trades))
    out.append("")

    if len(trades) < 10:
        out.append(
            "> **Insufficient data for GBT (<10 trades); dataset summary only. "
            "Sections 2-3 skipped.**"
        )
        out.append("")
        out.extend(_render_per_factor_wr(trades))
        out.append("")
        out.extend(_render_slicing_tables(trades))
        out.append("")
        out.extend(_render_rejected_counterfactual(rejects))
        out.append("")
        out.extend(_render_arkham_segmentation(trades))
        out.append("")
        out.extend(_render_pass1_recommendations(trades, rejects))
        out.append("")
        out.extend(_render_pass2_hypotheses(trades))
        out.append("")
        return "\n".join(out)

    gbt_lines, clf, reg, X = _render_gbt_importance(trades, all_factors, all_pillars)
    out.extend(gbt_lines)
    out.append("")
    out.extend(_render_shap_summary(clf, X, all_pillars, all_factors))
    out.append("")
    out.extend(_render_per_factor_wr(trades))
    out.append("")
    out.extend(_render_slicing_tables(trades))
    out.append("")
    out.extend(_render_rejected_counterfactual(rejects))
    out.append("")
    out.extend(_render_arkham_segmentation(trades))
    out.append("")
    out.extend(_render_pass1_recommendations(trades, rejects))
    out.append("")
    out.extend(_render_pass2_hypotheses(trades))
    out.append("")
    return "\n".join(out)


# ── Programmatic entry (async) ──────────────────────────────────────────────


async def run_analysis(
    *,
    db_path: str,
    output_path: str,
    since: Optional[datetime] = None,
    ignore_clean_since: bool = False,
    print_stdout: bool = True,
) -> str:
    """Runs the analysis end-to-end and writes the markdown report.

    Returns the report body. Exposed for the smoke test so we don't need
    to spawn a subprocess. Respects `rl.clean_since` by default; pass
    `ignore_clean_since=True` to include pre-cutoff rows.
    """
    if not ignore_clean_since:
        clean_since = _resolve_clean_since()
        if clean_since is not None:
            since = clean_since if since is None else max(since, clean_since)

    # Readable path for memory: skip existence check on :memory: so the
    # smoke test can drive this via a live in-memory journal object passed
    # some other way. Path lookups on disk still honour the existence check.
    if db_path != ":memory:" and not Path(db_path).exists():
        body = (
            "# Phase 9 GBT Analysis Report\n\n"
            f"_DB not found at {db_path} — nothing to analyse._\n"
        )
        _write_output(output_path, body)
        if print_stdout:
            print(body)
        return body

    async with TradeJournal(db_path) as j:
        trades = await j.list_closed_trades(since=since)
        rejects = await j.list_rejected_signals(since=since)

    body = _assemble_report(
        trades, rejects,
        since=since,
        generated_at=datetime.now(tz=timezone.utc),
    )
    _write_output(output_path, body)
    if print_stdout:
        print(body)
    return body


def _write_output(output_path: str, body: str) -> None:
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── CLI entry ───────────────────────────────────────────────────────────────


def _default_output_path() -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"reports/analyze_{ts}.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 9 GBT (xgboost) analysis on the trade journal",
    )
    parser.add_argument("--db", default=None, help="Path to trades.db")
    parser.add_argument(
        "--last", default="30d",
        help="Window: '7d', '30d', '12h', 'all' (default 30d)",
    )
    parser.add_argument(
        "--ignore-clean-since", action="store_true",
        help="Include rows before `rl.clean_since` (default: honour cutoff)",
    )
    parser.add_argument(
        "--output", default=None,
        help=f"Output markdown path (default: {_default_output_path()})",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    try:
        since = _parse_window(args.last)
    except argparse.ArgumentTypeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    output_path = args.output or _default_output_path()

    # Early-exit on missing optional deps with a helpful message. We still
    # go through run_analysis for the non-GBT sections; the missing-dep
    # message is embedded in the report body.
    try:
        import xgboost  # noqa: F401
        import shap  # noqa: F401
        import pandas  # noqa: F401
    except ImportError as e:
        print(
            f"[WARN] optional dependency missing: {e}. GBT + SHAP sections "
            f"will be skipped; non-GBT sections still produced.",
            file=sys.stderr,
        )

    asyncio.run(run_analysis(
        db_path=db_path,
        output_path=output_path,
        since=since,
        ignore_clean_since=args.ignore_clean_since,
        print_stdout=True,
    ))
    print(f"\n[OK] Report written to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
