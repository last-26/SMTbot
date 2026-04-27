"""YAML invariant guard for the runner-leg RR cap.

Originally the OCO-algo partial-TP tests lived here. Those tested the
pre-2026-04-25 (Bybit V5 migration) execution architecture where TP/SL
were placed as separate algo orders with their own algoIds; on Bybit V5
TP/SL are part of the position itself (set via /v5/order/create or
/v5/position/trading-stop), so `place_oco_algo` no longer exists and
the partial-TP feature itself stays `partial_tp_enabled=false` pending
a Pass 3 re-enable as a maker-limit + position-attached-TP pair.

The single guard test below survives because it only reads YAML and
has no execution-layer coupling.
"""

from __future__ import annotations

import pytest


def test_default_yaml_runner_tp_is_hard_1_n():
    """Guard: config/default.yaml must enforce a hard 1:N RR on the runner
    leg via `execution.target_rr_ratio`, and the trading fallback
    `default_rr_ratio` must stay aligned. Hard cap was 3.0 from 2026-04-19;
    tightened to 2.0 on 2026-04-21 (eve); tightened again to 1.5 on
    2026-04-28 paired with per-symbol SL-floor tighten (-25%) for
    scalp-native trade shape. Keep `execution.target_rr_ratio` and
    `trading.default_rr_ratio` in lockstep for the pre-zone fallback.
    """
    import pathlib
    import yaml

    cfg_path = pathlib.Path(__file__).parent.parent / "config" / "default.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    target_rr = float(raw["execution"]["target_rr_ratio"])
    default_rr = float(raw["trading"]["default_rr_ratio"])
    assert target_rr == pytest.approx(1.5), (
        f"execution.target_rr_ratio = {target_rr}, expected 1.5 — runner TP "
        "must enforce hard 1:1.5 RR per CLAUDE.md changelog 2026-04-28 "
        "(scalp tighten: SL floors -25%, RR 2.0 → 1.5)."
    )
    assert default_rr == pytest.approx(target_rr), (
        f"trading.default_rr_ratio ({default_rr}) drifted from "
        f"execution.target_rr_ratio ({target_rr}). Keep them aligned so the "
        "entry_signals fallback path also enforces 1:1.5 when zones aren't used."
    )
