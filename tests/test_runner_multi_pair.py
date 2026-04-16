"""Multi-pair round-robin tests (Madde A).

Covers:
  * `_run_one_symbol` is called for every symbol in `trading.symbols`.
  * A per-symbol failure does not break the rest of the cycle.
  * `max_concurrent_positions` caps total open entries across symbols.
  * Legacy `trading.symbol` YAML form coerces to `symbols=[...]` with a
    `DeprecationWarning`.
  * `okx_to_tv_symbol` maps the 5 OKX perps to the right TV tickers.
"""

from __future__ import annotations

import warnings

import pytest

from src.bot.config import BotConfig
from src.bot.runner import BotRunner
from src.data.tv_bridge import okx_to_tv_symbol
from tests.conftest import FakeRouter, make_config, make_plan


def _patch_plan_builder(monkeypatch, plan_or_none):
    def _stub(*a, **kw):
        return plan_or_none
    monkeypatch.setattr("src.bot.runner.build_trade_plan_from_state", _stub)


class _RecordingBridge:
    def __init__(self):
        self.symbol_calls: list[str] = []
        self.timeframe_calls: list[str] = []

    async def set_symbol(self, sym: str):
        self.symbol_calls.append(sym)
        return {"success": True}

    async def set_timeframe(self, tf: str):
        self.timeframe_calls.append(tf)
        return {"success": True}


async def test_symbols_roundrobin_order(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)     # don't place any orders
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "AVAX-USDT-SWAP", "XRP-USDT-SWAP"]
    cfg = make_config(symbols=syms, symbol_settle_seconds=0.0,
                      tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
                      pine_settle_poll_interval_s=0.01)
    bridge = _RecordingBridge()
    ctx, fakes = make_ctx(config=cfg)
    ctx.bridge = bridge
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert bridge.symbol_calls == [okx_to_tv_symbol(s) for s in syms]


async def test_symbol_failure_does_not_break_others(monkeypatch, make_ctx):
    _patch_plan_builder(monkeypatch, None)
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "AVAX-USDT-SWAP", "XRP-USDT-SWAP"]
    cfg = make_config(symbols=syms, symbol_settle_seconds=0.0,
                      tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
                      pine_settle_poll_interval_s=0.01)
    bridge = _RecordingBridge()

    # Inject a failure on the 3rd symbol — the loop must keep going.
    original_set = bridge.set_symbol

    async def flaky(sym: str):
        if sym == okx_to_tv_symbol("SOL-USDT-SWAP"):
            raise RuntimeError("TV bridge blew up on SOL")
        return await original_set(sym)

    bridge.set_symbol = flaky       # type: ignore[assignment]
    ctx, fakes = make_ctx(config=cfg)
    ctx.bridge = bridge
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    # Either the 3rd set_symbol raised before logging (then 4+5 still ran),
    # or the loop's per-symbol try/except caught the failure. Either way,
    # the final two symbols must have been attempted.
    assert okx_to_tv_symbol("AVAX-USDT-SWAP") in bridge.symbol_calls
    assert okx_to_tv_symbol("XRP-USDT-SWAP") in bridge.symbol_calls


async def test_max_concurrent_positions_caps_entries(monkeypatch, make_ctx):
    """With max_concurrent_positions=2 and plan valid for all 5 symbols,
    only 2 orders are placed; the rest are blocked by the risk manager."""
    _patch_plan_builder(monkeypatch, make_plan())
    syms = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
            "AVAX-USDT-SWAP", "XRP-USDT-SWAP"]
    cfg = make_config(symbols=syms, symbol_settle_seconds=0.0,
                      tf_settle_seconds=0.0, pine_settle_max_wait_s=0.1,
                      pine_settle_poll_interval_s=0.01,
                      max_concurrent_positions=2)
    ctx, fakes = make_ctx(config=cfg)
    ctx.bridge = _RecordingBridge()
    runner = BotRunner(ctx)
    async with ctx.journal:
        await runner.run_once()

    assert len(fakes.router.calls) == 2
    assert fakes.risk_mgr.open_positions == 2


async def test_legacy_symbol_config_backward_compat():
    """Old single-symbol YAML still loads and emits DeprecationWarning."""
    raw = {
        "bot": {"mode": "demo", "poll_interval_seconds": 1.0,
                "timezone": "UTC", "starting_balance": 1_000.0},
        "trading": {
            "symbol": "BTC-USDT-SWAP", "entry_timeframe": "15m",
            "htf_timeframe": "4H", "risk_per_trade_pct": 1.0,
            "max_leverage": 20, "default_rr_ratio": 3.0,
            "min_rr_ratio": 2.0, "max_concurrent_positions": 2,
            "contract_size": 0.01,
        },
        "analysis": {
            "min_confluence_score": 2, "candle_buffer_size": 500,
            "swing_lookback": 20, "sr_min_touches": 3,
            "sr_zone_atr_mult": 0.5, "session_filter": [],
        },
        "okx": {"demo_flag": "1", "api_key": "k",
                "api_secret": "s", "passphrase": "p"},
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = BotConfig(**raw)
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert cfg.trading.symbols == ["BTC-USDT-SWAP"]
    assert cfg.primary_symbol() == "BTC-USDT-SWAP"


@pytest.mark.parametrize("okx,tv", [
    ("BTC-USDT-SWAP", "OKX:BTCUSDT.P"),
    ("ETH-USDT-SWAP", "OKX:ETHUSDT.P"),
    ("SOL-USDT-SWAP", "OKX:SOLUSDT.P"),
    ("AVAX-USDT-SWAP", "OKX:AVAXUSDT.P"),
    ("XRP-USDT-SWAP", "OKX:XRPUSDT.P"),
    ("BTC-USDT", "OKX:BTCUSDT"),            # spot — no .P
])
def test_okx_to_tv_symbol(okx, tv):
    assert okx_to_tv_symbol(okx) == tv
