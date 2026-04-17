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

**Required OKX account mode (before anything works):**
1. Demo Trading → user icon → **Settings → Account mode** = **"Futures"** (aka Single-currency margin, `acctLv=2`). Default "Simple" mode has `acctLv=1` + forced `posMode=net_mode`, which rejects every call with `Parameter posSide error` because the code sends `posSide=long/short`.
2. Same settings page → **Position mode = "Hedge" (Long/Short mode)** — enables `posMode=long_short_mode`.
3. Verify via `get_account_config()`: expect `acctLv=2`, `posMode=long_short_mode`.
4. Demo balance reset is **UI-only** (no API endpoint); rotating API keys does not reset balance. The account ships with ~5000 USDT + test BTC/ETH/OKB.

**Demo API key:** Demo Trading → user icon → Demo Trading API → Create V5 key with Read+Trade (never withdrawal). Demo keys are completely separate from live keys.

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

References: `pine/vmc_a.txt`, `pine/vmc_b.txt` (original VMC source). Standalone scripts (`mss_detector.pine`, `fvg_mapper.pine`, `liquidity_sweep.pine`, `session_levels.pine`, `signal_table.pine`) are kept for reference but **not loaded** on the chart.

**OB sub-module inside the overlay** follows @Nephew_Sam_'s opensource Orderblocks pattern (MPL 2.0): fractals are persisted, and an OB is cut when a later bar trades through one — scanning back in time to the fractal to find the extreme counter candle. Optional 3-bar FVG proximity filter + immediate wick-mitigation delete. Boxes (not lines) are used so `src/data/structured_reader.py:parse_ob_boxes` keeps working.

## Deferred / performans TODO

- **Overlay Pine split (~1200 satır → 2 parça)** — `pine/smt_overlay.pine` tek script olarak ağır; multi-pair round-robin'de sembol değiştikten sonra yerleşme süresi (~3-5s) tarama cycle'ının ana maliyeti. İki parçaya bölmek (`smt_overlay_structure.pine` — MSS/BOS/FVG/OB + `smt_overlay_levels.pine` — liquidity/sessions/PDH-PWH/VMC) TV'nin recompute'unu paralelleştirebilir ve tarama latency'sini aşağı çekebilir. Her iki parça da kendi Pine tablosuna yazmalı; `src/data/structured_reader.py` iki tabloyu tek `MarketState`'e birleştirmeli. **Yapma önceliği düşük** — önce `phase7_prep_prompt.md` (Madde A-F) bitecek, demo'da freshness-poll latency'si fiilen sorun olursa bu optimizasyon ele alınacak.

## Phase status

| Phase | Status | Summary |
|---|---|---|
| 1. Pine + Data Bridge | ✅ Complete | SMT Overlay + Oscillator running on TV. Python bridge reads tables + drawings into unified `MarketState`. |
| 2. Analysis Engine | ✅ Complete | 7 modules under `src/analysis/`, 97 passing tests. |
| 3. Strategy Engine (R:R) | ✅ Complete | 5 modules under `src/strategy/`, 67 new tests (164 total). See below. |
| 4. Execution (OKX) | ✅ Complete | 5 modules under `src/execution/`, 29 new tests (193 total). See below. |
| 5. Trade Journal | ✅ Complete | 3 modules under `src/journal/` + CLI, 31 new tests (224 total). See below. |
| 6. Bot runtime loop | ✅ Complete | `src/bot/` async runner wiring TV → analysis → strategy → risk → execution → journal. `OKXClient.enrich_close_fill` added. 23 new tests (247 total). See below. |
| 6.5 Multi-pair + Multi-TF + Smart Entry/Exit | ✅ Complete | 6-part refactor (Madde A-F): multi-pair round-robin, freshness-polled multi-TF, reentry cooldown/quality gate, HTF S/R ceiling, partial TP + SL-to-BE, LTF reversal defensive close. 63 new tests (310 total). See below. |
| 1.5 Derivatives Data Layer | ✅ Complete | 7-part build (Madde 1-7) + CLI modes: Binance liquidation WS, Coinalyze REST client, derivatives cache + journal, estimated liquidity heatmap, 4-regime classifier, entry signal integration (contrarian/capitulation/heatmap factors + crowded-skip gate), journal enrichment + regime breakdown reporter, `--derivatives-only` / `--duration` runtime modes. 73 new tests (383 total). See below. |
| Multi-pair live-demo hardening | ✅ Complete (2026-04-17) | Post-Phase-1.5 production fixes observed during first live demo run: LTF momentum factor, clean logging, per-cycle decision visibility log, per-slot sizing + max-feasible leverage, per-symbol OKX `ctVal` + max-leverage lookup (51008/59102 fixes), `scripts/logs.py` viewer. 411 tests total (+28). See below. |
| 7. RL parameter tuner | 🔜 Next | PPO via Stable Baselines3, walk-forward validated. Needs ≥50 logged demo trades with derivatives snapshots from Phase 1.5 first. |

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

**Confluence factors** (each independently weighted, RL-tunable in Phase 7): `htf_trend_alignment`, `mss_alignment`, `at_order_block`, `at_fvg`, `at_sr_zone`, `recent_sweep`, `ltf_pattern`, `oscillator_momentum`, `oscillator_signal`, `vmc_ribbon`, `session_filter`. OB/FVG factors accept either Pine-derived (from `MarketState.signal_table`) or Python-recomputed zones.

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
- **Margin safety buffer (`_MARGIN_SAFETY = 0.95`)**: `max_notional = balance * max_leverage * 0.95`. Reserves 5% of balance for OKX fees (0.05% taker) + mark drift between `set_leverage` and `place_order`. Without this buffer OKX rejects with `sCode 51008`.
- Leverage is `max(ceil(required_leverage), min_lev_for_margin, 1)` capped at `max_leverage`. Using `ceil()` (not `round()`) guarantees `notional / leverage ≤ balance * 0.95`.
- When capped, notional SHRINKS so actual risk < target. Never above.
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

### Phase 4 — Completed (2026-04-16)

5 modules under `src/execution/` — sync python-okx calls, async-safe (wrap in `asyncio.to_thread` from the bot loop).

| Module | Purpose | Key APIs |
|---|---|---|
| `errors.py` | Typed exception hierarchy | `ExecutionError`, `OKXError`, `OrderRejected`, `InsufficientMargin`, `LeverageSetError`, `AlgoOrderError` |
| `models.py` | Execution records | `OrderResult`, `AlgoResult`, `ExecutionReport`, `PositionSnapshot`, `CloseFill`, `OrderStatus`, `PositionState` |
| `okx_client.py` | Typed wrapper over python-okx | `OKXClient`, `OKXCredentials`, `_check()` envelope validator |
| `order_router.py` | `TradePlan` → live orders | `OrderRouter`, `RouterConfig`, `dry_run_report()` |
| `position_monitor.py` | REST-poll positions → `CloseFill` | `PositionMonitor.register_open()`, `poll()` |

**Order flow (`OrderRouter.place`):**
1. `set_leverage(inst_id, lever, mgnMode="isolated", posSide)` — fails fast before any order
2. `place_market_order(side=buy/sell, posSide=long/short, sz=plan.num_contracts)`
3. `place_oco_algo(closing_side, sl/tpTriggerPx=plan.sl_price/tp_price, slOrdPx=-1)`
4. If algo fails: raise `AlgoOrderError` and (optionally) auto-close via `close_position()` — the position is never left OPEN without SL/TP unless operator disables `close_on_algo_failure`.

**Demo guard:** `OKXClient` refuses to construct with `demo_flag != "1"` unless `allow_live=True` is explicitly passed. One gate, no accidents.

**Envelope handling:** `_check()` validates OKX's `{"code": "0", "msg": "", "data": [...]}` envelope and raises typed errors on failure. Known margin-fail codes `{51008, 51020, 51200, 51201}` map to `InsufficientMargin`; other per-order `sCode` failures raise `OrderRejected`.

**Position monitor:** REST poll (no websocket) — keyed on `(inst_id, pos_side)`. Emits `CloseFill` when a tracked position disappears from `get_positions`. Caller converts `CloseFill` → `TradeResult` and calls `RiskManager.register_trade_closed()`. `exit_price`/`pnl_usdt` are enriched via journal lookup in Phase 5.

**Dry-run:** `dry_run_report(plan)` builds a fake `ExecutionReport` without touching the network — paper-trading hook for pipeline validation.

### OKX setup (manual, before live loop)
```bash
# Demo key: OKX → user icon → Demo Trading API → Create V5, Read+Trade, NO withdraw
# Fill .env: OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE / OKX_DEMO_FLAG=1
```

### OKX Python SDK reference
```python
import okx.Trade as Trade, okx.Account as Account
flag = "1"  # demo, "0" = live

tradeAPI   = Trade.TradeAPI(api_key, secret_key, passphrase, False, flag)
accountAPI = Account.AccountAPI(api_key, secret_key, passphrase, False, flag)

accountAPI.set_leverage(instId="BTC-USDT-SWAP", lever="10", mgnMode="isolated")
tradeAPI.place_order(instId="BTC-USDT-SWAP", tdMode="isolated",
    side="buy", posSide="long", ordType="market", sz="1")
tradeAPI.place_algo_order(instId="BTC-USDT-SWAP", tdMode="isolated",
    side="sell", posSide="long", ordType="oco", sz="1",
    slTriggerPx="67500", slOrdPx="-1", tpTriggerPx="72000", tpOrdPx="-1")
```

### Phase 5 — Completed (2026-04-16)

3 modules under `src/journal/` + a CLI. Async SQLite via `aiosqlite`, Pydantic `TradeRecord`, pure-function reporter. 31 new tests (224 total).

| Module | Purpose | Key APIs |
|---|---|---|
| `models.py` | Persisted trade shape | `TradeRecord` (Pydantic), `TradeOutcome` (OPEN/WIN/LOSS/BREAKEVEN/CANCELED) |
| `database.py` | Async SQLite CRUD | `TradeJournal.record_open/close/mark_canceled`, `list_open_trades/list_closed_trades`, `replay_for_risk_manager` |
| `reporter.py` | Pure metrics | `win_rate[_by_session/_by_factor]`, `avg_r`, `profit_factor`, `max_drawdown`, `equity_curve`, `sharpe_r`, `calmar`, `summary`, `format_summary` |
| `scripts/report.py` | CLI | `python scripts/report.py --last 7d [--db ...] [--starting-balance N]` |

**Lifecycle:** `record_open(TradePlan, ExecutionReport, symbol=, signal_timestamp=, …)` → row with `TradeOutcome.OPEN` and a fresh `uuid4().hex` `trade_id`. `record_close(trade_id, CloseFill)` stamps `exit_price`, `pnl_usdt`, computes `pnl_r = pnl_usdt / risk_amount_usdt`, flips outcome to WIN/LOSS/BREAKEVEN by sign. `mark_canceled(trade_id, reason)` covers entries that never filled.

**Schema:** single `trades` table, `confluence_factors` stored as JSON-encoded TEXT, indexes on `outcome`, `entry_timestamp`, `exit_timestamp`. Auto-created on `connect()`. `data/` directory auto-created if missing.

**Replay:** `await journal.replay_for_risk_manager(mgr)` walks closed trades in `entry_timestamp` order, calling `register_trade_opened()` + `register_trade_closed(TradeResult)` on the manager — reconstructs `peak_balance`, `consecutive_losses`, `current_balance` from durable truth. `open_positions` returns to 0 because every open is paired with a close.

**Reporter:** Sharpe is deliberately *un-annualized* per-trade R Sharpe — Phase 7 RL uses it as a reward shape, not a finance-standard stat. `profit_factor` is `sum(wins_usdt)/|sum(losses_usdt)|` (`inf` when no losses). `max_drawdown` returns `(usdt, pct)` from running peak. Bucketings: `win_rate_by_session` keyed on `TradeRecord.session`, `win_rate_by_factor` explodes each trade across its `confluence_factors` (one trade tagged with N factors counts once per factor).

**Integration hooks (not yet wired):** `OrderRouter` and `PositionMonitor` do not call the journal directly — that wiring lives in the outer bot loop (Phase 6). The journal's contract is `(TradePlan, ExecutionReport, CloseFill)` so glue will be a handful of lines.

**Config:** `config/default.yaml` → `journal.db_path: "data/trades.db"`. CLI reads this when `--db` omitted.

**Tests use** `pytest-asyncio` in `asyncio_mode = auto` (see `pytest.ini`). In-memory SQLite (`":memory:"`) for most tests; one `tmp_path` round-trip test confirms on-disk persistence.

### Phase 6 — Completed (2026-04-16)

4 modules under `src/bot/` + one new method on `OKXClient` + conftest fakes. 23 new tests (247 total).

| Module | Purpose | Key APIs |
|---|---|---|
| `config.py` | YAML + `.env` → typed config | `BotConfig`, `load_config(path)`, `BotConfig.breakers()` / `allowed_sessions()` / `risk_pct_fraction()` |
| `lifecycle.py` | Cross-platform shutdown | `install_shutdown_handlers(event)` |
| `runner.py` | Async outer loop | `BotRunner`, `BotContext`, `BotRunner.from_config(cfg, dry_run=)` |
| `__main__.py` | CLI entrypoint | `python -m src.bot [--config] [--dry-run] [--once] [--max-closed-trades N]` |

**One tick (`run_once`):**
1. `reader.read_market_state()` + `multi_tf.refresh(tf)` — tolerant to TV errors.
2. **Drain closes first** — `monitor.poll()` → `okx_client.enrich_close_fill(fill)` → `journal.record_close(trade_id, enriched)` → `risk_mgr.register_trade_closed(TradeResult)`.
3. Symbol-level dedup — skip open if `any(k[0] == symbol for k in ctx.open_trade_ids)`. (Bar-level would need `SignalTableData.last_bar` — not a parsed field today.)
4. **Sync sizing balance from OKX** — `okx_client.get_balance("USDT")` → `sizing_balance = min(okx_balance, risk_mgr.current_balance)`. The risk manager's `current_balance` only tracks P&L and drifts high vs reality (fees, funding); OKX rejects `sCode 51008` when the bot over-estimates available margin.
5. `build_trade_plan_from_state(state, sizing_balance, ...)` → `risk_mgr.can_trade(plan)` → `router.place(plan)` (or `_DryRunRouter` when `--dry-run`).
6. In-memory registration (`monitor.register_open`, `risk_mgr.register_trade_opened`) **before** `journal.record_open`; DB failure logs an orphan rather than losing the live position.

**Order rejection logging:** `OrderRejected` / `InsufficientMargin` / `LeverageSetError` exceptions log with `code=` and `payload=` attached so the upstream OKX `sCode` (51008, 51020, etc.) is visible without ad-hoc patching.

**Enrichment (critical fix):** `PositionMonitor._close_fill_from` emits `pnl_usdt=0, exit_price=0` (it only knows the position disappeared). Without enrichment every close looks break-even and risk-manager streaks / drawdown never trip. `OKXClient.enrich_close_fill` queries `/api/v5/account/positions-history`, picks the most recent `(instId, posSide)` row, and returns a `CloseFill` with real `realizedPnl`, `closeAvgPx`, `uTime`. When no match is returned (e.g. fills arriving after REST sync), the raw fill is passed through unchanged.

**Startup prime (`_prime`):**
1. `journal.replay_for_risk_manager(risk_mgr)` — rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from closed trades.
2. `_rehydrate_open_positions()` — loads any OPEN rows back into `monitor._tracked` and `ctx.open_trade_ids` so the next poll knows what to expect.
3. `_reconcile_orphans()` — diffs live OKX positions against journal OPEN rows; **logs only**, never auto-closes (operator decides).

**Shutdown:** `install_shutdown_handlers(event)` wires SIGINT / SIGTERM (+ SIGBREAK on Windows) to `asyncio.Event.set()`. POSIX uses `loop.add_signal_handler`; Windows ProactorEventLoop falls back to `signal.signal` + `loop.call_soon_threadsafe`. Terminal Ctrl-C on Windows still raises `KeyboardInterrupt` at `asyncio.run`, so `__main__` catches that as the reliable backstop.

**DI for testing:** `BotRunner(ctx)` accepts a fully-assembled `BotContext` — reader/router/monitor/okx_client are duck-typed, so `tests/conftest.py` fakes don't inherit from the real classes. `BotRunner.from_config(cfg)` is the production path that wires `TVBridge → StructuredReader`, `OKXClient`, `OrderRouter`, `PositionMonitor`, `TradeJournal`, `RiskManager`.

**Config additions to `config/default.yaml`:** `bot.starting_balance`, `trading.contract_size: 0.01`. Current defaults tuned for the real demo account (~4255 USDT, 1R ≈ $106):
- `bot.starting_balance: 4255.0`
- `trading.risk_per_trade_pct: 2.5` / `max_leverage: 75` / `default_rr_ratio: 3.0` / `min_rr_ratio: 2.0`
- `circuit_breakers.max_daily_loss_pct: 15.0` / `max_drawdown_pct: 25.0` (wide while farming Phase 7 data; tighten for live).

Secrets (`OKX_API_KEY/SECRET/PASSPHRASE/DEMO_FLAG`) come from `.env` via `python-dotenv`; `BotConfig` validator rejects empty credentials.

**`--max-closed-trades N`:** after each tick, if `len(journal.list_closed_trades()) >= N` the runner sets the shutdown event and exits cleanly. Exactly the primitive Phase 7 needs ("collect 50 closed demo trades, then stop"). Open positions at the moment of stop stay OPEN on OKX under their OCO algo — they resume on next start via `_rehydrate_open_positions()`.

**Usage:**
```bash
# Smoke test — full pipeline, one tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo (real orders on OKX demo env)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Demo with auto-stop at 50 closed trades (Phase 7 data-collection run)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --max-closed-trades 50
```

### Phase 6.5 — Completed (2026-04-17)

Six-part refactor ("Madde A-F") run between Phase 6 and Phase 7. Goal: (a) multi-asset so 50+ demo trades land faster, (b) stricter entry/exit discipline so the RL reward signal in Phase 7 is cleaner. Six feature commits + this docs commit + a push. 63 new tests total, all 310 passing.

**Madde A — Multi-pair round-robin** (`feat: multi-pair round-robin (Madde A)`)
- `TradingConfig.symbols: list[str]`; legacy `symbol: str` still loads with `DeprecationWarning`. `BotConfig.primary_symbol()` returns `symbols[0]`.
- `run_once` extracts `_run_one_symbol(symbol)`; drains closes once at the start then loops symbols with per-symbol try/except so one bad symbol can't break the others.
- `okx_to_tv_symbol("BTC-USDT-SWAP") → "OKX:BTCUSDT.P"` in `src/data/tv_bridge.py`.
- Defaults: `symbols: [BTC, ETH, SOL, AVAX, XRP]-USDT-SWAP`. 5 tests (`tests/test_runner_multi_pair.py`).

**Madde B — Multi-TF data pipeline with Pine freshness-check** (`feat: multi-TF pipeline + Pine freshness-check (Madde B)`)
- Added `SignalTableData.last_bar: Optional[int]` (parsed from row 20 of the SMT Signals Pine table) — the freshness beacon the runner polls between TF switches.
- `BotRunner._wait_for_pine_settle()`: polls `state.signal_table.last_bar` until it differs from the first reading (meaning Pine has re-rendered for the new chart state). First-read `None` is treated as "Pine doesn't emit last_bar, fall through" so tests with fake readers still work.
- `_switch_timeframe(tf)`: `bridge.set_timeframe(tf)` → static `tf_settle_seconds` sleep → freshness-poll. False result → skip this symbol's cycle.
- `_run_one_symbol` runs 3 TF passes: HTF (15m) → LTF (1m) → entry TF (3m). HTF pass caches `detect_sr_zones()` in `ctx.htf_sr_cache[symbol]`; LTF pass caches an `LTFState` in `ctx.ltf_cache[symbol]`; entry pass reads `MarketState`. HTF or entry stale → return; LTF stale → clear cache and continue.
- `src/data/ltf_reader.py`: new `LTFReader` + `LTFState` dataclass. Thin projection over the SMT Oscillator table — no extra TV calls. `_trend_from_oscillator` heuristic: `wt=OVERSOLD & rsi<40` → BEARISH, symmetric BULLISH, else RANGING.
- 9 tests (`tests/test_multi_tf_pipeline.py`, `tests/test_ltf_reader.py`).

**Madde C — Per-side reentry cooldown + quality gate** (`feat: per-side reentry cooldown + quality gate (Madde C)`)
- New `ReentryConfig` in `src/bot/config.py` + `BotContext.last_close: dict[(sym, side), LastCloseInfo]`.
- `_check_reentry_gate()` — four sequential gates, first fail wins: (1) cooldown `min_bars_after_close * _tf_seconds(entry_tf)`, (2) ATR move `|price - last.price| / atr < min_atr_move`, (3) post-WIN quality `proposed_confluence ≤ last.confluence` blocks, (4) post-LOSS quality `proposed_confluence < last.confluence` blocks (equal passes). BREAKEVEN bypasses the quality gate.
- `_handle_close` writes `LastCloseInfo` into `ctx.last_close` after the journal stamps outcome. Opposite sides isolated — closing a long doesn't cool off a short.
- 9 tests (`tests/test_reentry_gate.py`).

**Madde D — HTF S/R integration in SL/TP selection** (`feat: HTF S/R integration in SL/TP selection (Madde D)`)
- Two pure helpers in `src/strategy/entry_signals.py`: `_push_sl_past_htf_zone()` tightens SL past a zone fully between SL and entry (`sl < z.bottom < z.top < entry` for bullish, symmetric bearish); `_apply_htf_tp_ceiling()` caps TP short of the next opposing zone.
- `build_trade_plan_from_state()` accepts `htf_sr_zones`, `htf_sr_ceiling_enabled`, `htf_sr_buffer_atr`. After `calculate_trade_plan`, if TP ceiling applies, plan is rebuilt via `dataclasses.replace` with a recomputed `rr_ratio`; plans whose new R:R falls below `min_rr_ratio` are rejected (`return None`).
- `AnalysisConfig.htf_sr_ceiling_enabled=True` / `htf_sr_buffer_atr=0.2` (defaults). Runner wires `ctx.htf_sr_cache[symbol]` into the plan builder.
- 8 tests (`tests/test_htf_sr_integration.py`).

**Madde E — Partial TP + SL-to-BE** (`feat: partial TP + SL-to-BE (Madde E)`)
- `ExecutionConfig` (new): `partial_tp_enabled`, `partial_tp_ratio=0.5`, `partial_tp_rr=1.5`, `move_sl_to_be_after_tp1=True`, `trail_after_partial=False` (reserved for later).
- `ExecutionReport.algos: list[AlgoResult]` is now the canonical field; the original `algo: Optional[AlgoResult]` lives on as a bi-directionally-normalized back-compat shim (`__post_init__`). `is_protected` checks `bool(self.algos)`.
- `OrderRouter._place_algos()`: in partial mode, places two OCOs — TP1 at `entry ± sl_dist * partial_tp_rr * sign` on `int(num * ratio)` contracts, TP2 at `plan.tp_price` on the remainder. Degenerate size1/size2 (e.g. `num_contracts=1`) falls back to single algo with a log line. Either leg failing → both cancelled + `close_position`.
- `PositionMonitor` tracks `initial_size`, `algo_ids`, `tp2_price`, `be_already_moved`. When live size drops below `initial_size` but stays > 0 (= TP1 fill), the monitor cancels the TP2 algo, places a new OCO with SL=entry_price + TP=tp2_price on remaining contracts, and fires an `on_sl_moved(inst_id, pos_side, new_algo_ids)` callback. Failure leaves `be_already_moved=False` so the next poll retries.
- `from_config` wires `on_sl_moved` via a `ctx_holder` closure → `journal.update_algo_ids(trade_id, new_ids)` so the persisted row tracks the post-TP1 algo state.
- Journal: new columns `algo_ids TEXT DEFAULT '[]'`, `close_reason TEXT`. Idempotent `_MIGRATIONS` list runs inside try/except `aiosqlite.OperationalError` on every `connect()`, so existing demo databases upgrade cleanly. `_safe_col(row, name)` shields reads from legacy rows. `update_algo_ids()` helper. `record_close(..., close_reason=...)` uses `COALESCE` so passing `None` is a no-op.
- 16 tests (`tests/test_partial_tp.py`, `tests/test_sl_to_be.py`, `tests/test_journal_partial_tp.py`).

**Madde F — LTF reversal defensive close** (`feat: LTF reversal defensive close (Madde F)`)
- New fields on `BotContext`: `defensive_close_in_flight: set`, `pending_close_reasons: dict[(sym, side), str]`, `open_trade_opened_at: dict[(sym, side), datetime]`.
- `_is_ltf_reversal(ltf, open_side, max_age)`: true when `last_signal_bars_ago ≤ max_age` AND trend/signal contradict the open side (long → BEARISH+SELL, short → BULLISH+BUY).
- `_defensive_close(symbol, side, reason)`: cancels every tracked algo_id via `okx_client.cancel_algo`, calls `okx_client.close_position`, tags `pending_close_reasons[key] = "EARLY_CLOSE_LTF_REVERSAL"`. Idempotent via `defensive_close_in_flight`.
- `_run_one_symbol` runs the reversal check between the LTF read and the symbol-level dedup block, gated by a minimum-holding-time guard (`ltf_reversal_min_bars_in_position * _tf_seconds(entry_tf)`).
- `_handle_close` pops `pending_close_reasons` + `open_trade_opened_at`, discards `defensive_close_in_flight`, and passes `close_reason` to `journal.record_close` so the closed trade row records why.
- `ExecutionConfig` flags: `ltf_reversal_close_enabled=True`, `ltf_reversal_min_confluence=3` (reserved), `ltf_reversal_min_bars_in_position=2`, `ltf_reversal_signal_max_age=3`.
- 10 tests (`tests/test_ltf_reversal.py`).

**Data pipeline / config changes (global):**
- `config/default.yaml` grows `trading.symbols` (5 pairs), `trading.ltf_timeframe: "1m"`, `trading.symbol_settle_seconds: 4.0`, `trading.tf_settle_seconds: 2.5`, `trading.pine_settle_max_wait_s: 6.0`, `trading.pine_settle_poll_interval_s: 0.3`, `analysis.htf_sr_ceiling_enabled`/`_buffer_atr`, full `execution:` section, full `reentry:` section.
- `OKXClient.cancel_algo(inst_id, algo_id)` is pre-existing (Phase 4); Madde E + F reuse it — no new OKX method.

**New files:** `src/data/ltf_reader.py`, `tests/test_runner_multi_pair.py`, `tests/test_multi_tf_pipeline.py`, `tests/test_ltf_reader.py`, `tests/test_reentry_gate.py`, `tests/test_htf_sr_integration.py`, `tests/test_partial_tp.py`, `tests/test_sl_to_be.py`, `tests/test_journal_partial_tp.py`, `tests/test_ltf_reversal.py`.

**Phase 7 unlocks after Phase 6.5 because:** five pairs in parallel should land 50 closed demo trades an order of magnitude faster, and the entry/exit gates kill the noisiest failure modes (revenge re-entries, TP within reach of the next HTF zone, late exits when the LTF has already rolled).

### Phase 1.5 — Completed (2026-04-17)

Seven-part derivatives data layer ("Madde 1-7") + a CLI data-collection mode (Commit 8) run between Phase 6.5 and Phase 7. Goal: feed Phase 7 RL a richer feature vector than pure price structure — funding rates, open interest, liquidation flow, long/short ratios, and an estimated liquidity heatmap — plus use them as a principled, weighted entry-signal slot + crowded-skip gate without over-fitting.

**Pair parity (pre-Madde 1):** 5 → 3 pairs (BTC/ETH/SOL), `max_concurrent_positions: 3`. The Coinalyze free tier + Binance WS load both favor fewer, higher-quality pairs — and three pairs is enough to keep the RL training dataset balanced.

**Madde 1 — Binance liquidation WS** (`feat: liquidation stream — Binance forceOrder WS`)
- `src/data/liquidation_stream.py`: `LiquidationEvent` dataclass + `LiquidationStream` class. Subscribes to `!forceOrder@arr`, parses `e` / `s` / `S` / `p` / `q` into typed events. Per-symbol ring buffer with `recent(symbol, lookback_ms)` + `stats(symbol, lookback_ms)` query APIs. Exponential-backoff reconnect, `ping_interval=180`. `binance_to_okx_symbol` / `okx_to_binance_symbol` helpers. `attach_journal(j)` hooks the derivatives journal writer.
- New `DerivativesConfig` on `BotConfig.derivatives` (top-level, not under `analysis:` — matches `execution:` / `reentry:`). `enabled: False` default; `config/default.yaml` flips it `True`.

**Madde 2 — Coinalyze REST client** (`feat: coinalyze REST client with rate limiting`)
- `src/data/derivatives_api.py`: `CoinalyzeClient` + `DerivativesSnapshot` dataclass. Token bucket (40/min). `_request` handles `429 Retry-After` and `401 silent-fail` (missing key → None snapshots, bot keeps running). `ensure_symbol_map` walks `/future-markets` with exchange priority `["A","6","3","F","H"]` (OKX first).
- Endpoints: current OI/funding/predicted-funding, history funding/LS/liquidations series, OI-change % helper, and the aggregated `fetch_snapshot(okx_symbol) -> DerivativesSnapshot` used by the cache.
- `scripts/probe_coinalyze.py`: manual schema verification tool (run once before implementation to lock real response key names).

**Madde 3 — Derivatives cache + journal** (`feat: derivatives cache + journal`)
- `src/data/derivatives_cache.py`: `DerivativesState` + `DerivativesCache`. Startup pulls 720 h funding history + 336 h LS history per symbol for z-score baselines; periodic `_refresh_loop` (configurable interval, default 60 s) computes funding_z_30d / ls_z_14d / OI-change pct / liq imbalance and stamps the regime (Madde 5). `get(symbol) -> DerivativesState`.
- `src/journal/derivatives_journal.py`: separate async SQLite helper — `CREATE TABLE IF NOT EXISTS liquidations`, `derivatives_snapshots`. Shares the same DB file as the trades journal (single-file backup ops). Insert best-effort (try/except warn); `fetch_funding_history` / `fetch_oi_history` for Phase 7 retrospective training.

**Madde 4 — Estimated liquidity heatmap** (`feat: estimated liquidity heatmap`)
- `src/analysis/liquidity_heatmap.py`: pure-function builders. `estimate_liquidation_levels(current_price, long_short_ratio, total_oi_usd, leverage_buckets)` synthesizes long/short liq price bands from the configured leverage distribution `[(10, 0.30), (25, 0.35), (50, 0.20), (100, 0.15)]`. `cluster_levels` merges near-price bands. `build_heatmap` unifies estimated + recent-realized (from the liquidation stream) + historical (from `DerivativesJournal`) levels into a single `LiquidityHeatmap` with `clusters_above/below`, `nearest_above/below`, `largest_above/below_notional`.
- `src/data/models.py`: `LiquidityHeatmap` Pydantic model + two new `Optional` fields on `MarketState`: `derivatives: Optional[DerivativesState]`, `liquidity_heatmap: Optional[LiquidityHeatmap]`. Attached in `_run_one_symbol` at the entry TF pass — failure isolated (try/except logs `deriv_attach_failed`, symbol cycle continues).

**Madde 5 — 4-regime classifier** (`feat: derivatives regime classifier`)
- `src/analysis/derivatives_regime.py`: `Regime` enum (LONG_CROWDED / SHORT_CROWDED / CAPITULATION / BALANCED / UNKNOWN), `RegimeAnalysis(regime, confidence, reasoning)` dataclass, pure `classify_regime(state, **thresholds)` function. Priority: stale → CAPITULATION (heavy 1 h liq one-sided) → LONG_CROWDED (funding_z high + LS high) → SHORT_CROWDED (symmetric) → BALANCED.
- Per-symbol threshold overrides in `DerivativesConfig.regime_per_symbol_overrides` — ETH and SOL get smaller `capitulation_liq_notional` ($20 M / $8 M vs $50 M for BTC) because their OI pool is smaller.

**Madde 6 — Entry signal integration** (`feat: entry signal derivatives integration`)
- `src/analysis/multi_timeframe.py`: 3 new `ConfluenceFactor` types (`derivatives_contrarian=0.7`, `derivatives_capitulation=0.6`, `derivatives_heatmap_target=0.5` in `DEFAULT_WEIGHTS`), added as a single elif chain so at most one fires per cycle — keeps the slot principled, not stacked. `_heatmap_supports_direction(state, direction)` helper gates the heatmap factor on `nearest_cluster within ATR*3` + `notional ≥ 70% of largest`.
- `src/strategy/entry_signals.py`: `build_trade_plan_from_state` accepts `crowded_skip_enabled` + `crowded_skip_z_threshold`. After `generate_entry_intent`, `_should_skip_for_derivatives(deriv_state, direction, enabled, threshold)` rejects the plan (`return None`) when the intent aligns with a one-sided crowded regime (long into LONG_CROWDED, short into SHORT_CROWDED) AND `|funding_z| ≥ threshold`. Missing data never silently blocks — the gate only trips with evidence.

**Madde 7 — Journal enrichment + reporting** (`feat: journal derivatives enrichment + reporting`)
- `src/journal/database.py`: 9 idempotent `ALTER TABLE trades ADD COLUMN` migrations wrapped in try/except `aiosqlite.OperationalError` so existing demo databases upgrade cleanly. `record_open` gains 9 Optional kwargs (`regime_at_entry`, `funding_z_at_entry`, `ls_ratio_at_entry`, `oi_change_24h_at_entry`, `liq_imbalance_1h_at_entry`, and 4 `nearest_liq_cluster_{above,below}_{price,notional}` fields). `_safe_col` reads shield legacy rows.
- `src/journal/models.py:TradeRecord`: 9 matching `Optional[float|str] = None` fields.
- `src/journal/reporter.py`: `regime_breakdown(closed)` aggregator — per-regime num_trades / win_rate / avg_r / expectancy_r (None → "UNKNOWN" bucket so they stay visible). Surfaced in `summary()["regime_breakdown"]` and rendered by `format_summary()`.
- `src/bot/runner.py`: `_derive_enrichment(state)` helper extracts the 9 fields from `state.derivatives` + `state.liquidity_heatmap` and spreads them into `journal.record_open(**enrichment)`.

**Commit 8 — CLI `--derivatives-only` + `--duration N`** (`feat: CLI --derivatives-only + --duration modes`)
- `BotRunner` gains `derivatives_only: bool` and `duration_seconds: Optional[int]` ctor kwargs.
- `--derivatives-only`: `run_once` early-returns after the close-drain so the entry pipeline is bypassed; WS + Coinalyze cache + close poll keep running. Perfect for "warm the DB with a few hours of liq + funding data before arming the strategy."
- `--duration N`: wall-clock deadline — `run()`'s wait step clamps to the remaining time so the loop exits within one poll interval of N. Works with or without `--derivatives-only`.

**New files:** `src/data/liquidation_stream.py`, `src/data/derivatives_api.py`, `src/data/derivatives_cache.py`, `src/analysis/liquidity_heatmap.py`, `src/analysis/derivatives_regime.py`, `src/journal/derivatives_journal.py`, `scripts/probe_coinalyze.py`, plus 8 new test modules (`test_liquidation_stream`, `test_derivatives_api`, `test_derivatives_cache`, `test_liquidity_heatmap`, `test_derivatives_regime`, `test_entry_signals_derivatives`, `test_journal_derivatives`, `test_runner_derivatives_only`).

**Global config additions (`config/default.yaml`):** top-level `derivatives:` section with `enabled: true`, `liquidation_buffer_size`, `liquidation_lookback_*_ms`, `coinalyze_refresh_interval_s: 60`, `coinalyze_timeout_s`, `coinalyze_max_retries`, full `heatmap_*` block + `leverage_buckets`, full `regime_thresholds` block + `regime_per_symbol_overrides` (BTC default, ETH $20M capitulation, SOL $8M), `confluence_slot_enabled: true`, `crowded_skip_enabled: true`, `crowded_skip_z_threshold: 3.0`. New env: `COINALYZE_API_KEY=` in `.env.example`.

**Derivatives data sources (like "Pine Scripts" for price):**
- **Binance USDT-M perp `!forceOrder@arr`** — real-time aggregated liquidation stream (`wss://fstream.binance.com/ws/!forceOrder@arr`). Note: Binance rate-limits to the *largest* liquidation per 1 s window, so small liquidations are invisible — Coinalyze aggregate history fills that gap.
- **Coinalyze REST** — `open-interest`, `funding-rate`, `predicted-funding`, `long-short-ratio`, `liquidation-history` endpoints. Free-tier key. 40 req/min token bucket. Priority-ordered exchange filter: OKX → Binance → Bybit → Bitget → Huobi.

**Failure isolation posture:** any derivatives subsystem failure (WS disconnect, 401/429 from Coinalyze, cache refresh crash) logs a warning and leaves `state.derivatives=None` / `state.liquidity_heatmap=None`. The strategy then degrades gracefully to pure price-structure signals — the bot never crashes on derivatives errors. Missing `COINALYZE_API_KEY` silently falls through to None snapshots.

**Total tests:** 383 passing (310 → 383, +73).

**Usage examples:**
```bash
# Data warm-up: 10 min of liq stream + Coinalyze snapshots, no orders placed
.venv/Scripts/python.exe -m src.bot --config config/default.yaml \
    --derivatives-only --duration 600

# Regular demo trading with derivatives factors + crowded-skip gate (default)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Report with per-regime win-rate breakdown (summary always includes it now)
.venv/Scripts/python.exe scripts/report.py --last 7d
```

### Multi-pair live-demo hardening — Completed (2026-04-17)

Post-Phase-1.5, during the first real multi-pair demo run, three groups of production bugs surfaced. Each was diagnosed from the fresh log + an OKX balance/positions query and fixed before letting the bot continue. 28 net new tests (383 → 411).

**Observability (three commits: `12c8223`, `1cc5a86`, plus `scripts/logs.py`):**
- `src/bot/__main__.py` — clean loguru config at entry: stderr with color + `logs/bot.log` sink with `colorize=False, enqueue=True, rotation="50 MB", retention=10`. Keeps `tail -f` on Windows readable and cycles the file at 50 MB.
- `_run_one_symbol` emits `symbol_cycle_start symbol=…` at the top of every per-symbol pass, and a terminal `symbol_decision symbol=… [NO_TRADE reason=… | PLANNED …]` line so the operator can reconstruct every tick's decision from the log alone. `NO_TRADE reason` is one of `below_confluence` / `crowded_skip` / `downstream_reject` — reason is inferred by re-running `calculate_confluence` when `build_trade_plan_from_state` returns None.
- `PLANNED` log includes `contracts=… notional=… lev=…x margin=… risk=… sizing_bal=…` so a future 51008/59102 is diagnosable from the single line above it.

**`ltf_momentum_alignment` confluence factor** (`12c8223`):
- New factor in `src/analysis/multi_timeframe.py`: when the LTF buffer (from `LTFReader`) has a trend matching the proposed direction → full `0.5` weight; when the LTF last_signal is the opposite of the trend but fresh (`last_signal_bars_ago ≤ 3`) and agrees with the direction → 60% partial weight. Eight unit tests in `tests/test_ltf_entry_factor.py`. Threaded through `generate_entry_intent` → `build_trade_plan_from_state` → `_run_one_symbol` via `ltf_state=self.ctx.ltf_cache.get(symbol)`.
- **Zero-falsy gotcha:** `bars_ago=0` is a legitimate "just now" value, not falsy. Uses explicit `None` check: `raw = getattr(…, None); bars_ago = int(raw) if raw is not None else 99`. The shorter `int(x or 99)` silently clobbered the freshest signal.

**Per-slot sizing + max-feasible leverage** (`7201b9b`):
- **Problem observed:** BTC opened with the old leverage formula locked ~80% of isolated margin ($3490 / $4348 eq). SOL/ETH then tripped sCode 51008 (`your available margin is too low for borrowing`) on every cycle for 26 min — 18 SOL + 1 ETH rejections.
- **Fix 1 — per-slot sizing:** `_run_one_symbol` now reads both `okx.get_total_equity("USDT")` (= `eq`, total equity including locked margin) and `okx.get_balance("USDT")` (= `availEq`, free for new orders). `sizing_balance = min(total_eq / max_concurrent_positions, okx_avail, risk_mgr.current_balance)`. Each of N configured slots gets a fair share of the account, independent of what's currently locked.
- **Fix 2 — max-feasible leverage:** `src/strategy/rr_system.py` no longer picks the minimum leverage that fits margin inside `balance × 0.95`. It picks the maximum feasible = `min(max_leverage, floor(0.6 / sl_pct))` (new `_LIQ_SAFETY_FACTOR = 0.6` constant — SL must sit within 60% of the liquidation distance at chosen leverage, 40% buffer for maintenance + mark drift). With tighter 0.5% SL on 75x max, margin drops from ~$715/trade to ~$57/trade so three concurrent positions coexist inside a $4300 equity.
- `src/execution/okx_client.py` gains `get_total_equity("USDT")` reading OKX's `eq` field. `get_balance` keeps reading `availEq`.
- Three new tests in `tests/test_rr_system.py` (`test_leverage_picks_max_feasible_for_concurrent_sizing`, `test_leverage_ceiling_scales_with_sl_width`) and one updated expectation in `test_long_basic_sizing` (chosen leverage = 20, not 2).

**Per-symbol OKX instrument spec** (`b13cd4a`, `bc07c85`):
- **Problem 1 — 51008 on SOL/ETH even after sizing fix:** YAML hardcoded `trading.contract_size: 0.01` for all pairs, but OKX `ctVal` is per-instrument: BTC=0.01, ETH=0.1, **SOL=1**. A "$798 notional" for SOL was actually 904 × 1 SOL × $88.20 ≈ **$79,733** on OKX's books → 100× over-size → margin blown every time.
- **Problem 2 — 59102 once ctVal was fixed:** OKX caps `max_leverage` per instrument too. BTC/ETH=100x, **SOL=50x**. `cfg.trading.max_leverage=75` worked for BTC/ETH but rejected every SOL order.
- `OKXClient.get_instrument_spec(inst_id) -> {ct_val, max_leverage}` pulls both fields from `/public/instruments` in one call.
- `BotContext.contract_sizes: dict[str, float]` + `max_leverage_per_symbol: dict[str, int]` populated in `_prime()` via `_load_contract_sizes()`. Falls back to YAML defaults + logs exception on any OKX fetch error — bot never refuses to start on a transient API hiccup.
- `_run_one_symbol` forwards the per-symbol values into `build_trade_plan_from_state`: `max_leverage=min(cfg.trading.max_leverage, per_symbol_cap)`, `contract_size=ctx.contract_sizes[symbol]`.
- First successful SOL fill after the fix cycle landed at 2026-04-17 05:02:54Z: `opened BULLISH SOL-USDT-SWAP 9c @ 88.18 notional=793.62 lev=22x margin=36.07 risk=21.19`.

**`scripts/logs.py` — terminal log viewer:**
- Small tail-and-filter utility, no new deps. Reads `logs/bot.log`, supports `--decisions` (only `symbol_decision` / `opened` / `order_rejected` / `reentry_blocked` / `defensive_close` / `closed` / `algo_failure`), `--errors` (ERROR+WARNING only), `--filter REGEX`, `--lines N`, `--no-follow`, `--no-color`. ANSI-highlights PLANNED (green), NO_TRADE (dim), fills (bold cyan), rejects (red), reentry/blocked (magenta), cycle-start (blue). Default mode is `tail -F` with 50 lines of history and auto-recovers on log rotation.
- Ships at `scripts/logs.py`; run from the project root with `.venv/Scripts/python.exe scripts/logs.py [options]`.

**Total tests:** 411 passing (383 → 411, +28).

## Phase 7 — Reinforcement learning

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
- Min data: 50 trades before first training (Phase 6 must have run long enough to log them)

**Training cycle:** Claude triggers `python scripts/train_rl.py --min-trades 50 --walk-forward`. Improved params → `config/strategies/active.yaml`.

## Currency pair strategy

**Phase 1.5 onwards:** 3 OKX perps run round-robin per tick — BTC / ETH / SOL. (Phase 6.5 shipped with 5 pairs; Phase 1.5 trimmed to 3 because the Coinalyze free-tier 40-req/min budget + Binance WS load both favor fewer, higher-quality pairs, and 3 is enough to keep the Phase 7 RL training dataset balanced.) `trading.symbols` in YAML is the single source of truth; the legacy single-`symbol` YAML form still loads with a `DeprecationWarning`. Circuit breakers (`max_concurrent_positions=3` in defaults) cap total simultaneous exposure across all symbols, not per-symbol.

**To add a 4th+ pair:** drop it into `trading.symbols`, confirm `okx_to_tv_symbol()` produces a valid TV ticker (add a parametrized test case), add per-symbol regime overrides in `derivatives.regime_per_symbol_overrides` (smaller OI pools → smaller `capitulation_liq_notional`), and watch the first 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed` log lines — illiquid pairs will flunk the freshness-poll more often.

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
.venv/Scripts/python.exe scripts/logs.py                 # Follow live log (all)
.venv/Scripts/python.exe scripts/logs.py --decisions     # Only entry/exit decisions
.venv/Scripts/python.exe scripts/logs.py --errors        # Only ERROR / WARNING
.venv/Scripts/python.exe scripts/logs.py --filter SOL    # Filter by regex
```

**Pine dev cycle** (Claude via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix errors → `tv pine analyze` → `tv screenshot`.

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, can break on TV updates → pin TV Desktop version. All data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. Always start `--profile demo`. Never enable withdrawal perms on API key. Bind key to machine IP. AI is non-deterministic → verify before live. Use sub-account for live.

**Trading risks:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital. Check OKX TOS for automated trading.

**RL risks:** overfitting is #1 — always walk-forward validate. Markets change regime. Log everything. Simple parameter tuning > complex deep RL.
