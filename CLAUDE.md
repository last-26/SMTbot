# CLAUDE.md — Crypto Futures Trading Bot

## Overview

AI-powered crypto futures bot combining two MCP bridges with a Python core:

- **TradingView MCP** — chart data, indicator values, Pine Script drawings, Pine dev cycle
- **OKX Agent Trade Kit MCP** — order execution on OKX (demo first, live later)
- **Python Bot Core** — autonomous loop: data → analysis → strategy (R:R) → execution → journal → RL retraining

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, builds/trains RL, debugs). Per-candle trade decisions are made by the Python bot, **not** by Claude at runtime.

**Division of labor:** TradingView = eyes (Pine Scripts pre-analyze structure). OKX = hands (order placement + algo SL/TP). Python bot = brain (confluence scoring → R:R sizing → execution → learning).

## Prerequisites

Node.js 18+, Python 3.11+, TradingView Desktop (subscription), OKX account (demo needs no deposit), Claude Code.

## MCP Setup

### TradingView MCP — installed & verified

- Repo: `C:\Users\samet\Desktop\tradingview-mcp\` (npm installed)
- TradingView Desktop extracted from MSIX to `C:\TradingView\` (MSIX sandbox blocks debug port — must use standalone exe)
- Launch: `"C:\TradingView\TradingView.exe" --remote-debugging-port=9222`
- CDP verified at `http://localhost:9222`
- MCP config in `~/.claude/.mcp.json` points to `C:/Users/samet/Desktop/tradingview-mcp/src/server.js`

**Key TV CLI commands** (binary is `tv`, not `tradingview-mcp`):
```bash
tv status                              # Symbol, TF, indicators
tv ohlcv --summary                     # OHLCV bars
tv data tables --filter "SMT Signals"  # Read overlay table
tv data tables --filter "SMT Oscillator"
tv data labels --filter --max N        # Label drawings (MSS, sweeps)
tv data boxes  --filter --verbose      # Box drawings (FVG, OB)
tv data lines  --filter --verbose      # Line drawings (sessions, liquidity)
tv pine set < script.pine              # Load Pine Script
tv pine compile / analyze / check      # Compile + static analysis
tv stream tables --filter "SMT Signals"  # Live table stream
tv screenshot
tv symbol OKX:BTCUSDT.P                # Set OKX perp chart
tv timeframe 15
```

### OKX Agent Trade Kit MCP — pending (needed at Phase 4)

```bash
npm install -g okx-trade-mcp okx-trade-cli
okx setup --client claude-code --profile demo --modules all
```

Manual `~/.claude/.mcp.json` entry:
```json
{
  "mcpServers": {
    "okx": {
      "command": "okx-trade-mcp",
      "args": ["--profile", "demo", "--modules", "all"],
      "env": {
        "OKX_API_KEY": "...", "OKX_API_SECRET": "...", "OKX_PASSPHRASE": "..."
      }
    }
  }
}
```

**Demo API key:** OKX → Trade → Demo Trading → Settings → Single Currency Margin Mode → user icon → Demo Trading API → Create V5 key with Read+Trade. Demo keys are completely separate from live keys.

**Key OKX CLI:**
```bash
okx market ticker BTC-USDT
okx market candles BTC-USDT-SWAP --bar 15m --limit 100
okx account balance / positions / config
okx swap place --instId BTC-USDT-SWAP --side buy --posSide long --ordType market --sz 1
okx algo place --instId BTC-USDT-SWAP --side sell --posSide long --ordType conditional \
  --slTriggerPx 67500 --slOrdPx -1 --tpTriggerPx 72000 --tpOrdPx -1 --sz 1
okx account set-leverage --instId BTC-USDT-SWAP --lever 10 --mgnMode cross
```

**OKX instrument naming:** Perp = `BTC-USDT-SWAP`, Spot = `BTC-USDT`, Dated futures = `BTC-USDT-250425`. TradingView ticker for OKX perp = `OKX:BTCUSDT.P`.

## Pine Scripts (running on TradingView)

Two production indicators built by combining 6 standalone scripts + VuManChu Cipher A/B:

| Script | File | Type | Purpose |
|---|---|---|---|
| SMT Master Overlay | `pine/smt_overlay.pine` | Chart overlay | MSS/BOS + FVG boxes + OB boxes + liquidity lines/sweeps + session H/L + PDH/PDL/PWH/PWL + VMC Cipher A. Outputs **20-row "SMT Signals" table** (confluence 0-7). |
| SMT Master Oscillator | `pine/smt_oscillator.pine` | Lower pane | VMC Cipher B: WaveTrend + RSI + MFI + Stoch RSI + Schaff TC + divergences + buy/sell/gold dots. Outputs **15-row "SMT Oscillator" table** (momentum 0-5). |

**SMT Signals fields:** trend_htf, trend_ltf, structure, last_mss, active_fvg, active_ob, liquidity_above/below, last_sweep, session, vmc_ribbon, vmc_wt_bias, vmc_wt_cross, vmc_last_signal, vmc_rsi_mfi, confluence, atr_14, price, last_bar.

**SMT Oscillator fields:** wt1, wt2, wt_state, wt_cross, wt_vwap_fast, rsi, rsi_mfi, stoch_k, stoch_d, stoch_state, last_signal, last_wt_div, momentum, last_bar.

References: `pine/vmc_a.txt`, `pine/vmc_b.txt` (original VMC source). Standalone scripts (`mss_detector.pine`, `fvg_mapper.pine`, `order_block.pine`, `liquidity_sweep.pine`, `session_levels.pine`, `signal_table.pine`) are kept for reference but **not loaded** on the chart.

## Phase status

| Phase | Status | Summary |
|---|---|---|
| 1. Pine + Data Bridge | ✅ Complete | SMT Overlay + Oscillator running on TV. Python bridge reads tables + drawings into unified `MarketState`. |
| 2. Analysis Engine | ✅ Complete | 7 modules under `src/analysis/`, 97 passing tests. |
| 3. Strategy Engine (R:R) | ✅ Complete | 5 modules under `src/strategy/`, 67 new tests (164 total). See below. |
| 4. Execution (OKX) | 🔜 Next | `src/execution/`: order lifecycle via OKX SDK or MCP. |
| 5. Trade Journal | Planned | SQLite + Pydantic models + reporter. |
| 6. RL parameter tuner | Planned | PPO via Stable Baselines3, walk-forward validated. |

### Phase 1 — Completed (2026-04-16)

**Environment:**
- Python 3.14, Node v25.2.1, `.venv/` with all deps installed (`requirements.txt`)
- Note: `python-okx` uses 0.4.x versioning (not 5.x as in old docs)
- `config/default.yaml` created
- `.env` not yet created (only `.env.example`)

**Data bridge** (`src/data/`):
| File | Purpose |
|---|---|
| `models.py` | Pydantic: `MarketState`, `SignalTableData`, `OscillatorTableData`, `MSSEvent`, `FVGZone`, `OrderBlock`, `LiquidityLevel`, `SweepEvent`, `SessionLevel` |
| `tv_bridge.py` | Async wrapper around `node tradingview-mcp/src/cli/index.js`, parallel fetch |
| `structured_reader.py` | Parses both Pine tables + drawings into `MarketState` |
| `candle_buffer.py` | `Candle`, `CandleBuffer` (single TF), `MultiTFBuffer` |

Validation: `scripts/test_market_state.py` (supports `--poll N`).

### Phase 2 — Completed (2026-04-16)

7 pure-function modules under `src/analysis/` (no I/O, no async, fast to test):

| Module | Purpose | Key APIs |
|---|---|---|
| `price_action.py` | Candle patterns | `detect_all_patterns()`, `has_entry_pattern()`, `CandlePattern` |
| `market_structure.py` | HH/HL/LH/LL, BOS, CHoCH, MSS | `analyze_structure()`, `find_swing_points()`, `MarketStructure` |
| `fvg.py` | Python-side FVG + mitigation | `detect_fvgs()`, `active_fvgs()`, `nearest_fvg()` |
| `order_blocks.py` | OB detection (impulse threshold) | `detect_order_blocks()`, `active_order_blocks()` |
| `liquidity.py` | Equal H/L clustering + sweeps | `analyze_liquidity()`, `detect_sweeps()` |
| `support_resistance.py` | ATR-scaled S/R zones | `detect_sr_zones()`, `at_key_level()` |
| `multi_timeframe.py` | **Capstone** — confluence scoring | `calculate_confluence()`, `ConfluenceScore`, `ConfluenceFactor` |

**Patterns:** doji, hammer, shooting star, pin bar, bullish/bearish engulfing, inside bar, morning/evening star.

**Confluence factors** (each independently weighted, RL-tunable in Phase 6): `htf_trend_alignment`, `mss_alignment`, `at_order_block`, `at_fvg`, `at_sr_zone`, `recent_sweep`, `ltf_pattern`, `oscillator_momentum`, `oscillator_signal`, `vmc_ribbon`, `session_filter`. OB/FVG factors accept either Pine-derived (from `MarketState.signal_table`) or Python-recomputed zones.

**Design notes:**
- Pine Script remains primary source of truth; Python supplements (HTF without chart switch, cross-checks, testability).
- S/R uses ATR-scaled band width — works on any instrument without retuning.
- Sweep→reversal mapping is explicit: bearish sweep (swept highs) → BULLISH confluence factor.

**Tests:** 97 passing across 7 files. Run: `.venv/Scripts/python.exe -m pytest tests/ -v`.

### Phase 3 — Completed (2026-04-16)

5 modules under `src/strategy/` — pure, synchronous, 67 new tests (164 total).

| Module | Purpose | Key APIs |
|---|---|---|
| `trade_plan.py` | Sized trade dataclass | `TradePlan` |
| `rr_system.py` | R:R math core | `calculate_trade_plan()`, `break_even_win_rate()`, `expected_value_r()` |
| `position_sizer.py` | SL placement helpers | `sl_from_order_block/fvg/swing/atr()`, `recent_swing_price()` |
| `entry_signals.py` | Orchestration pipeline | `generate_entry_intent()`, `build_trade_plan_from_state()`, `select_sl_price()` |
| `risk_manager.py` | Circuit breakers | `RiskManager`, `CircuitBreakerConfig`, `TradeResult` |

**Core R:R math:**
- `risk_amount = balance * risk_pct`; `sl_pct = |entry - sl| / entry`
- `tp = entry ± sl_distance * rr_ratio`
- `ideal_notional = risk_amount / sl_pct`; `required_leverage = ideal / balance`
- Leverage capped at `max_leverage`; when capped, notional SHRINKS so risk stays bounded.
- OKX contracts: `num_contracts = int(notional // (contract_size * entry))` (round down). Actual risk re-derived from rounded contracts.

**Break-even win rates:** 1:1 → 50%, 1:2 → 33.3%, 1:3 → 25%, 1:4 → 20%.

**SL selection priority** (`select_sl_price`): Pine OB → Pine FVG → Python OB → Python FVG → swing lookback → ATR fallback. All pushed past the level by `buffer_mult * ATR` (default 0.2).

**Circuit breakers** (non-negotiable, ordered):
- Drawdown from peak ≥ `max_drawdown_pct` → permanent halt (manual restart). Checked first.
- Cooldown halt (`halted_until`) blocks until timestamp.
- Daily realized loss ≥ `max_daily_loss_pct` → halt for `cooldown_hours`.
- Consecutive losses ≥ `max_consecutive_losses` → halt for `cooldown_hours`.
- Open positions ≥ `max_concurrent_positions` → block new entries.
- Plan-level: leverage ≤ `max_leverage`, `rr_ratio ≥ min_rr_ratio`, `num_contracts > 0`.

`RiskManager` is pure state + records; no DB. Journal (Phase 5) will replay trades to rebuild it on startup.

## Phase 4 — Execution via OKX

### Order flow
1. Signal fires with `TradePlan`
2. `okx account set-leverage --instId ... --lever {n} --mgnMode isolated`
3. Place entry (market or limit)
4. Immediately place algo OCO SL/TP
5. Monitor position via WebSocket
6. On exit, log to journal
7. Handle partial fills

### OKX Python SDK
```python
import okx.Trade as Trade, okx.Account as Account
flag = "1"  # demo, "0" = live

tradeAPI   = Trade.TradeAPI(api_key, secret_key, passphrase, False, flag)
accountAPI = Account.AccountAPI(api_key, secret_key, passphrase, False, flag)

accountAPI.set_leverage(instId="BTC-USDT-SWAP", lever="10", mgnMode="isolated")

tradeAPI.place_order(
    instId="BTC-USDT-SWAP", tdMode="isolated",
    side="buy", posSide="long", ordType="market", sz="1",
)

tradeAPI.place_algo_order(
    instId="BTC-USDT-SWAP", tdMode="isolated",
    side="sell", posSide="long", ordType="oco", sz="1",
    slTriggerPx="67500", slOrdPx="-1",   # -1 = market
    tpTriggerPx="72000", tpOrdPx="-1",
)
```

## Phase 5 — Trade journal

SQLite via `aiosqlite` + Pydantic `TradeRecord` model. Fields: trade_id, timestamps (signal/entry/exit), symbol, direction, timeframes, htf_bias, prices (entry/SL/TP/exit), rr_ratio, leverage, num_contracts, risk_amount_usdt, pnl_usdt, **pnl_r** (e.g. +2.8R), fees, outcome, confluence_score, patterns_detected, market_structure, liquidity_context, ob_level, fvg_level, screenshot_entry/exit, notes.

**Performance metrics:** win rate (overall + per pattern + per session), avg R, profit factor, expectancy, max consecutive W/L, max drawdown, Sharpe, Calmar.

## Phase 6 — Reinforcement learning

**Architecture:** parameter tuner, NOT raw decision maker. Rule-based strategy generates signals; RL tunes:
- `confluence_threshold` (2-5), `pattern_weights` (dict), `min_rr_ratio` (1.5-5.0)
- `risk_pct` (0.005-0.02), `htf_required` (bool), `session_filter` (list)
- `volatility_scale` (0.5-2.0), `ob_vs_fvg_preference` (0.0-1.0)

**Reward** = `pnl_r + setup_penalty + dd_penalty + consistency_bonus`
- `setup_penalty = -3.0` if confluence < 2 (heavy penalty for undisciplined trades)
- `dd_penalty = -2.0` if dd > 5%, `-1.0` if > 3%
- `consistency_bonus = min(sharpe_last10 * 0.5, 1.5)`

**Walk-forward (WFO):** train 1-N, validate N+1 to N+50, advance window. Rules:
- Never deploy params that didn't improve OOS
- If params swing wildly → reduce learning rate
- Retrain trigger: every 50 new trades OR weekly
- Min data: 50 trades before first training

**Training cycle:** Claude triggers `python scripts/train_rl.py --min-trades 50 --walk-forward`. Improved params → `config/strategies/active.yaml`.

## Currency pair strategy

**Phase 1 (now):** BTC-USDT-SWAP only. Highest liquidity, predictable PA, available on demo.

**Add ETH-USDT-SWAP only when:** ≥100 BTC demo trades logged, win rate ≥40% @ 1:2 (or ≥33% @ 1:3), profit factor > 1.2, ≥2 RL training cycles done, max DD stayed under 10%.

**Do not add more pairs until ETH stable.**

## Configuration

Full config lives in `config/default.yaml`. Key sections: `bot` (mode, poll interval), `trading` (symbol, TFs, risk_per_trade_pct, max_leverage, rr_ratios, max_concurrent), `circuit_breakers`, `analysis` (min_confluence_score, swing_lookback, sr params, session_filter), `okx` (demo_flag), `rl` (min_trades, retrain interval, lr, gamma, ppo_epochs).

`.env.example` keys: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `TV_MCP_PORT`, `LOG_LEVEL`.

## Tech stack

**Python deps** (see `requirements.txt`): pydantic, pyyaml, python-dotenv, aiosqlite, httpx, **python-okx (0.4.x)**, websockets, pandas, numpy, ta, stable-baselines3, gymnasium, torch, loguru, rich, schedule.

**Node deps:** `tradingview-mcp` (chart bridge), `okx-trade-mcp` + `okx-trade-cli` (execution).

## Workflow commands

```bash
./scripts/setup.sh                                       # Setup
python -m src.bot --config config/default.yaml           # Demo
OKX_DEMO_FLAG=0 python -m src.bot --config ...           # Live (after demo proven)
python scripts/report.py --last 7d                       # Report
python scripts/train_rl.py --min-trades 50 --walk-forward
.venv/Scripts/python.exe -m pytest tests/ -v             # Tests
```

**Pine dev cycle** (Claude via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix errors → `tv pine analyze` → `tv screenshot`.

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, can break on TV updates → pin TV Desktop version. All data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. Always start `--profile demo`. Never enable withdrawal perms on API key. Bind key to machine IP. AI is non-deterministic → verify before live. Use sub-account for live.

**Trading risks:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital. Check OKX TOS for automated trading.

**RL risks:** overfitting is #1 — always walk-forward validate. Markets change regime. Log everything. Simple parameter tuning > complex deep RL.
