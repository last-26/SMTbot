# CLAUDE.md — Crypto Futures Trading Bot

## Overview

AI-powered crypto futures bot combining two MCP bridges with a Python core:

- **TradingView MCP** — chart data, indicator values, Pine Script drawings, Pine dev cycle
- **OKX Agent Trade Kit MCP** — order execution on OKX (demo first, live later)
- **Python Bot Core** — autonomous loop: data → analysis → strategy (R:R) → execution → journal → RL retraining

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, builds/trains RL, debugs). Per-candle trade decisions are made by the Python bot, **not** by Claude at runtime. TradingView = eyes (Pine pre-analyzes structure). OKX = hands (orders + algo SL/TP). Python bot = brain (confluence → R:R sizing → execution → learning).

## Prerequisites

Node.js 18+, Python 3.11+, TradingView Desktop (subscription), OKX account (demo needs no deposit), Claude Code.

## MCP Setup

### TradingView MCP — installed & verified

- Repo: `C:\Users\samet\Desktop\tradingview-mcp\` (npm installed)
- TradingView Desktop extracted from MSIX to `C:\TradingView\` (MSIX sandbox blocks debug port — must use standalone exe)
- Launch: `"C:\TradingView\TradingView.exe" --remote-debugging-port=9222`
- CDP verified at `http://localhost:9222`
- MCP config in `~/.claude/.mcp.json` points to `C:/Users/samet/Desktop/tradingview-mcp/src/server.js`

**Key TV CLI commands** (binary is `tv`):
```bash
tv status                              # Symbol, TF, indicators
tv ohlcv --summary                     # OHLCV bars
tv data tables --filter "SMT Signals"  # Read overlay table
tv data tables --filter "SMT Oscillator"
tv data labels --filter --max N        # MSS, sweep labels
tv data boxes  --filter --verbose      # FVG, OB boxes
tv data lines  --filter --verbose      # Session, liquidity lines
tv pine set < script.pine              # Load Pine Script
tv pine compile / analyze / check      # Compile + static analysis
tv stream tables --filter "SMT Signals"
tv screenshot
tv symbol OKX:BTCUSDT.P
tv timeframe 15
```

### OKX Agent Trade Kit MCP

```bash
npm install -g okx-trade-mcp okx-trade-cli
okx setup --client claude-code --profile demo --modules all
```

Manual `~/.claude/.mcp.json`:
```json
{
  "mcpServers": {
    "okx": {
      "command": "okx-trade-mcp",
      "args": ["--profile", "demo", "--modules", "all"],
      "env": {"OKX_API_KEY": "...", "OKX_API_SECRET": "...", "OKX_PASSPHRASE": "..."}
    }
  }
}
```

**Required OKX account mode:**
1. Demo Trading → user icon → **Settings → Account mode = "Futures"** (Single-currency margin, `acctLv=2`). Default "Simple" mode (`acctLv=1`, forced `net_mode`) rejects every call with `Parameter posSide error`.
2. **Position mode = "Hedge" (Long/Short)** — enables `posMode=long_short_mode`.
3. Verify via `get_account_config()`: `acctLv=2`, `posMode=long_short_mode`.
4. Demo balance reset is UI-only (no API endpoint); rotating keys doesn't reset.

**Demo API key:** Demo Trading → user icon → Demo Trading API → Create V5 key, Read+Trade only (never withdrawal). Demo keys are completely separate from live.

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

**OKX instrument naming:** Perp = `BTC-USDT-SWAP`, Spot = `BTC-USDT`, Dated = `BTC-USDT-250425`. TV ticker for OKX perp = `OKX:BTCUSDT.P`.

## Pine Scripts

Two production indicators (combining 6 standalone scripts + VuManChu Cipher A/B):

| Script | File | Type | Purpose |
|---|---|---|---|
| SMT Master Overlay | `pine/smt_overlay.pine` | Chart overlay | MSS/BOS + FVG/OB boxes + liquidity/sweeps + sessions + PDH/PDL/PWH/PWL + VMC Cipher A. Outputs **20-row "SMT Signals" table** (confluence 0-7). |
| SMT Master Oscillator | `pine/smt_oscillator.pine` | Lower pane | VMC Cipher B: WaveTrend + RSI + MFI + Stoch RSI + Schaff TC + divergences + dots. Outputs **15-row "SMT Oscillator" table** (momentum 0-5). |

**SMT Signals fields:** trend_htf, trend_ltf, structure, last_mss, active_fvg, active_ob, liquidity_above/below, last_sweep, session, vmc_ribbon, vmc_wt_bias, vmc_wt_cross, vmc_last_signal, vmc_rsi_mfi, confluence, atr_14, price, last_bar.

**SMT Oscillator fields:** wt1, wt2, wt_state, wt_cross, wt_vwap_fast, rsi, rsi_mfi, stoch_k, stoch_d, stoch_state, last_signal, last_wt_div, momentum, last_bar.

References: `pine/vmc_a.txt`, `pine/vmc_b.txt`. Standalone scripts (`mss_detector.pine`, `fvg_mapper.pine`, `liquidity_sweep.pine`, `session_levels.pine`, `signal_table.pine`) archived under `pine/legacy/` for design-history reference — **not loaded** on TV; superseded by the two masters.

**OB sub-module** follows @Nephew_Sam_'s opensource Orderblocks pattern (MPL 2.0): persisted fractals; OB cut when a later bar trades through one — scanning back to find extreme counter candle. Optional 3-bar FVG proximity filter + immediate wick-mitigation delete. Boxes (not lines) for `structured_reader.py:parse_ob_boxes`.

## Deferred / performance TODO

- **Overlay Pine split (~1200 lines → 2 parts)** — single-script is heavy; symbol-switch settle (~3-5s) is the dominant multi-pair cycle cost. Splitting into `smt_overlay_structure.pine` (MSS/BOS/FVG/OB) + `smt_overlay_levels.pine` (liquidity/sessions/PDH-PWH/VMC) could parallelize TV recompute. Each part needs its own Pine table; `structured_reader.py` would merge two tables into one `MarketState`. **Low priority** — tackle only if freshness-poll latency proves problematic.

## Phase status

| Phase | Status | Summary |
|---|---|---|
| 1. Pine + Data Bridge | ✅ Complete | SMT Overlay + Oscillator on TV. Python bridge → `MarketState`. |
| 2. Analysis Engine | ✅ Complete | 7 modules in `src/analysis/`, 97 tests. |
| 3. Strategy Engine (R:R) | ✅ Complete | 5 modules in `src/strategy/`, 164 tests total. |
| 4. Execution (OKX) | ✅ Complete | 5 modules in `src/execution/`, 193 tests total. |
| 5. Trade Journal | ✅ Complete | 3 modules in `src/journal/` + CLI, 224 tests total. |
| 6. Bot runtime loop | ✅ Complete | `src/bot/` async runner wiring all phases + `OKXClient.enrich_close_fill`, 247 tests total. |
| 6.5 Multi-pair + Multi-TF + Smart Entry/Exit | ✅ Complete | Madde A-F: round-robin, freshness-polled multi-TF, reentry gate, HTF S/R, partial TP + SL-to-BE, LTF defensive close. 310 tests. |
| 1.5 Derivatives Data Layer | ✅ Complete | Madde 1-7: Binance liq WS, Coinalyze REST, cache/journal, heatmap, regime classifier, entry integration, enrichment + `--derivatives-only`/`--duration` CLI. 383 tests. |
| Live-demo hardening (2026-04-17) | ✅ Complete | Three sessions of production fixes — see below. 441 tests. |
| Macro Event Blackout (2026-04-17) | ✅ Complete | Finnhub + FairEconomy union, ±30/15-min HIGH-impact USD blackout. ISO-2 → currency normalization. 469 tests. |
| Fee-aware sizing + TP1/TP2 guarantee + fee-buffered BE (2026-04-17) | ✅ Complete | `fee_reserve_pct` in `rr_system`, `insufficient_contracts_for_split` reject, `sl_be_offset_pct` in `PositionMonitor`, `CloseFill.fee_usdt` persisted. 479 tests. |
| 7. RL parameter tuner | 🔜 Next | PPO via Stable Baselines3, walk-forward. Needs ≥50 logged trades from live-demo first. |

---

### Phase 1 — Completed (2026-04-16)

**Env:** Python 3.14, Node v25.2.1, `.venv/`, `config/default.yaml`. Note: `python-okx` is 0.4.x (not 5.x as in old docs).

**Data bridge** (`src/data/`):

| File | Purpose |
|---|---|
| `models.py` | Pydantic: `MarketState`, `SignalTableData`, `OscillatorTableData`, `MSSEvent`, `FVGZone`, `OrderBlock`, `LiquidityLevel`, `SweepEvent`, `SessionLevel` |
| `tv_bridge.py` | Async wrapper around `node tradingview-mcp/src/cli/index.js` |
| `structured_reader.py` | Pine tables + drawings → `MarketState` |
| `candle_buffer.py` | `Candle`, `CandleBuffer`, `MultiTFBuffer` |

Validation: `scripts/test_market_state.py` (supports `--poll N`).

### Phase 2 — Completed (2026-04-16)

7 pure-function modules under `src/analysis/`:

| Module | Purpose | Key APIs |
|---|---|---|
| `price_action.py` | Candle patterns | `detect_all_patterns()`, `has_entry_pattern()`, `CandlePattern` |
| `market_structure.py` | HH/HL/LH/LL, BOS, CHoCH, MSS | `analyze_structure()`, `find_swing_points()` |
| `fvg.py` | FVG + mitigation | `detect_fvgs()`, `active_fvgs()`, `nearest_fvg()` |
| `order_blocks.py` | OB (impulse threshold) | `detect_order_blocks()`, `active_order_blocks()` |
| `liquidity.py` | Equal H/L clustering + sweeps | `analyze_liquidity()`, `detect_sweeps()` |
| `support_resistance.py` | ATR-scaled S/R | `detect_sr_zones()`, `at_key_level()` |
| `multi_timeframe.py` | **Capstone** — confluence | `calculate_confluence()`, `ConfluenceScore`, `ConfluenceFactor` |

**Patterns:** doji, hammer, shooting star, pin bar, bull/bear engulfing, inside bar, morning/evening star.

**Confluence factors** (RL-tunable in Phase 7): `htf_trend_alignment`, `mss_alignment`, `at_order_block`, `at_fvg`, `at_sr_zone`, `recent_sweep`, `ltf_pattern`, `oscillator_momentum`, `oscillator_signal`, `vmc_ribbon`, `session_filter`. OB/FVG accept Pine-derived or Python-recomputed zones.

**Design:** Pine is primary source of truth; Python supplements. S/R is ATR-scaled. Sweep→reversal mapping: bearish sweep (swept highs) → BULLISH factor.

### Phase 3 — Completed (2026-04-16)

5 modules under `src/strategy/` — pure, synchronous.

| Module | Purpose | Key APIs |
|---|---|---|
| `trade_plan.py` | Sized trade dataclass | `TradePlan` |
| `rr_system.py` | R:R math core | `calculate_trade_plan()`, `break_even_win_rate()`, `expected_value_r()` |
| `position_sizer.py` | SL placement | `sl_from_order_block/fvg/swing/atr()`, `recent_swing_price()` |
| `entry_signals.py` | Orchestration | `generate_entry_intent()`, `build_trade_plan_from_state()`, `select_sl_price()` |
| `risk_manager.py` | Circuit breakers | `RiskManager`, `CircuitBreakerConfig`, `TradeResult` |

**Core R:R math:**
- `risk_amount = balance * risk_pct`; `sl_pct = |entry - sl| / entry`
- `tp = entry ± sl_distance * rr_ratio`
- `ideal_notional = risk_amount / sl_pct`; `required_leverage = ideal / balance`
- **Margin safety buffer `_MARGIN_SAFETY = 0.95`**: `max_notional = balance * max_leverage * 0.95`. Reserves 5% for OKX fees + mark drift. Without it OKX rejects with `sCode 51008`.
- Leverage `max(ceil(required_leverage), min_lev_for_margin, 1)` capped at `max_leverage`. `ceil()` guarantees `notional / leverage ≤ balance * 0.95`.
- OKX contracts: `num_contracts = int(notional // (contract_size * entry))`. Actual risk re-derived from rounded contracts.

**Break-even win rates:** 1:1 → 50%, 1:2 → 33.3%, 1:3 → 25%, 1:4 → 20%.

**SL selection priority** (`select_sl_price`): Pine OB → Pine FVG → Python OB → Python FVG → swing lookback → ATR fallback. All pushed past the level by `buffer_mult * ATR` (default 0.2).

**Circuit breakers** (ordered, first-match):
1. Drawdown from peak ≥ `max_drawdown_pct` → permanent halt (manual restart).
2. Cooldown halt (`halted_until`) blocks until timestamp.
3. Daily realized loss ≥ `max_daily_loss_pct` → halt for `cooldown_hours`.
4. Consecutive losses ≥ `max_consecutive_losses` → halt for `cooldown_hours`.
5. Open positions ≥ `max_concurrent_positions` → block new entries.
6. Plan-level: leverage ≤ `max_leverage`, `rr_ratio ≥ min_rr_ratio`, `num_contracts > 0`.

`RiskManager` is pure state; journal replays trades to rebuild on startup.

### Phase 4 — Completed (2026-04-16)

5 modules under `src/execution/` — sync python-okx, async-safe via `asyncio.to_thread`.

| Module | Purpose | Key APIs |
|---|---|---|
| `errors.py` | Typed exceptions | `ExecutionError`, `OKXError`, `OrderRejected`, `InsufficientMargin`, `LeverageSetError`, `AlgoOrderError` |
| `models.py` | Records | `OrderResult`, `AlgoResult`, `ExecutionReport`, `PositionSnapshot`, `CloseFill`, `OrderStatus`, `PositionState` |
| `okx_client.py` | python-okx wrapper | `OKXClient`, `OKXCredentials`, `_check()` envelope validator |
| `order_router.py` | `TradePlan` → live orders | `OrderRouter`, `RouterConfig`, `dry_run_report()` |
| `position_monitor.py` | REST-poll → `CloseFill` | `PositionMonitor.register_open()`, `poll()` |

**Order flow (`OrderRouter.place`):**
1. `set_leverage(inst_id, lever, mgnMode, posSide)` — fails fast.
2. `place_market_order(side, posSide, sz=plan.num_contracts)`.
3. `place_oco_algo(closing_side, slTriggerPx, tpTriggerPx, slOrdPx=-1)`.
4. Algo fails → `AlgoOrderError` + optional auto-close via `close_position()`. Position never left OPEN without SL/TP unless `close_on_algo_failure` disabled.

**Demo guard:** `OKXClient` refuses `demo_flag != "1"` unless `allow_live=True` explicitly passed.

**Envelope:** `_check()` validates `{"code":"0","msg":"","data":[...]}`. Margin-fail codes `{51008, 51020, 51200, 51201}` → `InsufficientMargin`; other `sCode` failures → `OrderRejected`.

**Position monitor:** REST poll (no WS), keyed on `(inst_id, pos_side)`. Emits `CloseFill` when tracked position disappears. `exit_price`/`pnl_usdt` enriched via journal lookup.

**Dry-run:** `dry_run_report(plan)` builds fake report without network.

**OKX Python SDK reference:**
```python
import okx.Trade as Trade, okx.Account as Account
flag = "1"  # demo
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

3 modules under `src/journal/` + CLI. Async SQLite (`aiosqlite`), Pydantic `TradeRecord`, pure-function reporter.

| Module | Purpose | Key APIs |
|---|---|---|
| `models.py` | Persisted shape | `TradeRecord`, `TradeOutcome` (OPEN/WIN/LOSS/BREAKEVEN/CANCELED) |
| `database.py` | Async SQLite CRUD | `TradeJournal.record_open/close/mark_canceled`, `list_open/closed_trades`, `replay_for_risk_manager` |
| `reporter.py` | Pure metrics | `win_rate[_by_session/_by_factor]`, `avg_r`, `profit_factor`, `max_drawdown`, `equity_curve`, `sharpe_r`, `calmar`, `summary`, `format_summary` |
| `scripts/report.py` | CLI | `python scripts/report.py --last 7d [--db ...] [--starting-balance N]` |

**Lifecycle:** `record_open(TradePlan, ExecutionReport, symbol=, signal_timestamp=, …)` creates row as `OPEN` with uuid. `record_close(trade_id, CloseFill)` stamps `exit_price`, `pnl_usdt`, computes `pnl_r = pnl_usdt / risk_amount_usdt`, flips outcome by sign. `mark_canceled(trade_id, reason)` covers unfilled entries.

**Schema:** single `trades` table. `confluence_factors` as JSON TEXT. Indexes on `outcome`, `entry_timestamp`, `exit_timestamp`. Auto-created on `connect()`.

**Replay:** `journal.replay_for_risk_manager(mgr)` walks closed trades in entry order — rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from durable truth.

**Reporter notes:** Sharpe is per-trade R (un-annualized) — Phase 7 RL reward shape. `profit_factor = sum(wins)/|sum(losses)|` (inf when no losses). `max_drawdown = (usdt, pct)` from running peak. `win_rate_by_factor` explodes each trade across its factors (one trade with N factors counts once per factor).

**Config:** `journal.db_path: "data/trades.db"`. Tests use in-memory SQLite except one `tmp_path` persistence round-trip. `pytest-asyncio asyncio_mode = auto`.

### Phase 6 — Completed (2026-04-16)

4 modules under `src/bot/` + `OKXClient.enrich_close_fill`.

| Module | Purpose | Key APIs |
|---|---|---|
| `config.py` | YAML + `.env` → typed | `BotConfig`, `load_config(path)`, `breakers()`/`allowed_sessions()`/`risk_pct_fraction()` |
| `lifecycle.py` | Cross-platform shutdown | `install_shutdown_handlers(event)` |
| `runner.py` | Async outer loop | `BotRunner`, `BotContext`, `BotRunner.from_config(cfg, dry_run=)` |
| `__main__.py` | CLI | `python -m src.bot [--config] [--dry-run] [--once] [--max-closed-trades N]` |

**One tick (`run_once`):**
1. Read `MarketState` + refresh multi-TF buffer (tolerant to TV errors).
2. **Drain closes first:** `monitor.poll()` → `enrich_close_fill()` → `journal.record_close()` → `risk_mgr.register_trade_closed()`.
3. Symbol-level dedup (skip if symbol already has open position).
4. Sync sizing balance from OKX — `get_balance("USDT")` → `sizing_balance = min(okx_balance, risk_mgr.current_balance)`. Risk manager drifts high vs reality (fees); OKX rejects `51008` if over-estimated.
5. `build_trade_plan_from_state(...)` → `risk_mgr.can_trade(plan)` → `router.place(plan)`.
6. In-memory registration (`monitor.register_open`, `risk_mgr.register_trade_opened`) **before** `journal.record_open` — DB failure logs orphan rather than losing live position.

**Enrichment (critical):** `PositionMonitor._close_fill_from` emits `pnl_usdt=0, exit_price=0` (it only knows position disappeared). `OKXClient.enrich_close_fill` queries `/api/v5/account/positions-history` to return real `realizedPnl`, `closeAvgPx`, `uTime`. Without this every close looks BREAKEVEN and streaks/drawdown never trip.

**Startup prime (`_prime`):**
1. `journal.replay_for_risk_manager(risk_mgr)`.
2. `_rehydrate_open_positions()` — loads OPEN rows into monitor `_tracked` and `ctx.open_trade_ids`.
3. `_reconcile_orphans()` — diffs live OKX positions vs journal OPEN; **logs only**, operator decides.

**Shutdown:** `install_shutdown_handlers(event)` wires SIGINT/SIGTERM (+ SIGBREAK on Windows). POSIX uses `loop.add_signal_handler`; Windows falls back to `signal.signal` + `call_soon_threadsafe`. Terminal Ctrl-C on Windows also raises `KeyboardInterrupt` — caught in `__main__` as backstop.

**DI for testing:** `BotRunner(ctx)` accepts pre-assembled `BotContext`; duck-typed so conftest fakes don't inherit real classes.

**Config defaults (tuned for ~3200 USDT post-loss demo, 1R ≈ $32 / 3R ≈ $96):** `bot.starting_balance: 3200.0`, `trading.risk_per_trade_pct: 1.0`, `max_leverage: 75`, `default_rr_ratio: 3.0` (1:3 → break-even winrate ≈ 25%), `min_rr_ratio: 2.0`, `circuit_breakers.max_daily_loss_pct: 15.0`, `max_drawdown_pct: 25.0` (wide for Phase 7 data farming).

**`--max-closed-trades N`:** after each tick, exits cleanly when journal closed trades ≥ N. Open positions stay OPEN on OKX under OCO; resumed via `_rehydrate_open_positions()`.

**Usage:**
```bash
# Smoke test — full pipeline, one tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo (real orders on OKX demo)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Auto-stop at 50 closed trades
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --max-closed-trades 50
```

### Phase 6.5 — Completed (2026-04-17)

Six-part refactor (**Madde A-F**) for multi-pair parallelism + entry/exit discipline. 63 new tests (310 total).

- **A — Multi-pair round-robin.** `TradingConfig.symbols: list[str]` (legacy `symbol: str` loads with `DeprecationWarning`). `run_once` loops via `_run_one_symbol()` with per-symbol try/except. `okx_to_tv_symbol("BTC-USDT-SWAP") → "OKX:BTCUSDT.P"`.
- **B — Multi-TF + Pine freshness check.** `SignalTableData.last_bar` is the freshness beacon. `_wait_for_pine_settle()` polls until `last_bar` flips post-TF-switch (first-read `None` → fall through for test fakes); stale → skip cycle. Three passes per symbol: HTF (15m) caches `detect_sr_zones()` in `ctx.htf_sr_cache`, LTF (1m) caches `LTFState` in `ctx.ltf_cache`, entry (3m) is the trade pass. `src/data/ltf_reader.py:LTFReader` projects from the SMT Oscillator table (no extra TV calls); heuristic `wt=OVERSOLD & rsi<40 → BEARISH`, symmetric BULLISH.
- **C — Per-side reentry cooldown + quality gate.** `ReentryConfig` + `BotContext.last_close: dict[(sym,side), LastCloseInfo]`. `_check_reentry_gate()` four sequential gates, first-fail-wins: (1) cooldown = `min_bars_after_close * _tf_seconds(entry_tf)`, (2) ATR move `|price-last.price|/atr < min_atr_move`, (3) post-WIN quality `proposed_confluence ≤ last.confluence` blocks, (4) post-LOSS `proposed_confluence < last.confluence` blocks (equal passes). BREAKEVEN bypasses quality. `_handle_close` writes `LastCloseInfo` after journal stamps outcome; opposite sides isolated.
- **D — HTF S/R in SL/TP selection.** `entry_signals._push_sl_past_htf_zone()` tightens SL past a zone fully between SL and entry; `_apply_htf_tp_ceiling()` caps TP short of next opposing zone. `build_trade_plan_from_state()` takes `htf_sr_zones`, `htf_sr_ceiling_enabled`, `htf_sr_buffer_atr`. After `calculate_trade_plan`, ceiling-affected plans are rebuilt via `dataclasses.replace` with recomputed `rr_ratio`; plans below `min_rr_ratio` rejected.
- **E — Partial TP + SL-to-BE.** `ExecutionConfig`: `partial_tp_enabled`, `partial_tp_ratio=0.5`, `partial_tp_rr=1.5`, `move_sl_to_be_after_tp1=True`. `ExecutionReport.algos: list[AlgoResult]` canonical, `.algo` shim normalized bidirectionally. `OrderRouter._place_algos()` places TP1 at `entry ± sl_dist * partial_tp_rr` + TP2 at `plan.tp_price`; degenerate `num_contracts=1` falls back to single OCO. Either leg failure → both cancelled + `close_position`. `PositionMonitor` tracks `initial_size`, `algo_ids`, `tp2_price`, `be_already_moved`; size-drop-but-positive = TP1 fill → cancel TP2, place new OCO `SL=entry + TP=tp2` on remainder → `on_sl_moved` callback. Journal gets `algo_ids TEXT DEFAULT '[]'` + `close_reason TEXT` via idempotent `_MIGRATIONS` (try/except `aiosqlite.OperationalError`); `_safe_col(row, name)` shields legacy rows.
- **F — LTF reversal defensive close.** `BotContext`: `defensive_close_in_flight: set`, `pending_close_reasons: dict[(sym,side), str]`, `open_trade_opened_at: dict[(sym,side), datetime]`. `_is_ltf_reversal(ltf, open_side, max_age)` true when `last_signal_bars_ago ≤ max_age` AND trend/signal contradict open side. `_defensive_close(symbol, side, reason)` cancels every tracked `algo_id` via `okx_client.cancel_algo`, calls `close_position`, tags `pending_close_reasons[key]`; idempotent via `defensive_close_in_flight`. Gated by `ltf_reversal_min_bars_in_position * _tf_seconds(entry_tf)` (min hold time).

**Config:** new `trading.symbols`, `trading.ltf_timeframe`, `trading.symbol_settle_seconds`, `trading.tf_settle_seconds`, `trading.pine_settle_max_wait_s`, `analysis.htf_sr_ceiling_enabled`/`_buffer_atr`, full `execution:` + `reentry:` sections.

### Phase 1.5 — Completed (2026-04-17)

Seven-part derivatives data layer (**Madde 1-7**) + CLI data-collection (Commit 8). Enriches Phase 7 RL features with funding, OI, liquidation flow, L/S ratios, estimated heatmap. 73 new tests (383 total).

**Pair parity:** 5 → 3 pairs (BTC/ETH/SOL), `max_concurrent_positions: 3` (Coinalyze free tier 40/min + Binance WS load favor fewer, higher-quality pairs).

- **1 — Binance liquidation WS.** `src/data/liquidation_stream.py:LiquidationStream` subscribes to `wss://fstream.binance.com/ws/!forceOrder@arr`. Per-symbol ring buffer with `recent(symbol, lookback_ms)` + `stats(symbol, lookback_ms)`. Exponential-backoff reconnect, `ping_interval=180`. `binance_to_okx_symbol`/`okx_to_binance_symbol` helpers. `attach_journal(j)` hooks derivatives journal. Caveat: Binance WS rate-limits to the *largest* liquidation per 1s window — Coinalyze history fills the gap.
- **2 — Coinalyze REST client.** `src/data/derivatives_api.py:CoinalyzeClient` + `DerivativesSnapshot`. Token bucket 40/min. `_request` handles `429 Retry-After` + `401 silent-fail` (missing key → None, bot keeps running). `ensure_symbol_map` walks `/future-markets` with exchange priority `["A","6","3","F","H"]` (OKX first). Endpoints: current OI/funding/predicted-funding, history funding/LS/liquidations, OI-change pct helper, aggregated `fetch_snapshot(okx_symbol) -> DerivativesSnapshot`. `scripts/probe_coinalyze.py` for schema verification.
- **3 — Derivatives cache + journal.** `src/data/derivatives_cache.py:DerivativesCache` startup pulls 720h funding + 336h LS history for z-score baselines; `_refresh_loop` computes `funding_z_30d`, `ls_z_14d`, OI-change pct, liq imbalance, stamps regime. `src/journal/derivatives_journal.py` adds `liquidations` + `derivatives_snapshots` tables in same DB file (best-effort insert). `fetch_funding_history`/`fetch_oi_history` for Phase 7.
- **4 — Estimated liquidity heatmap.** `src/analysis/liquidity_heatmap.py` pure functions. `estimate_liquidation_levels(price, ls_ratio, oi_usd, leverage_buckets=[(10,0.30),(25,0.35),(50,0.20),(100,0.15)])` synthesizes liq price bands. `cluster_levels` merges near-price bands. `build_heatmap` unifies estimated + recent-realized (WS) + historical (journal) → `LiquidityHeatmap` with `clusters_above/below`, `nearest_above/below`, `largest_above/below_notional`. `MarketState` gains `derivatives` + `liquidity_heatmap` (attached at entry TF; failure isolated).
- **5 — 4-regime classifier.** `src/analysis/derivatives_regime.py:Regime` enum (LONG_CROWDED / SHORT_CROWDED / CAPITULATION / BALANCED / UNKNOWN). `classify_regime(state, **thresholds)` priority: stale → CAPITULATION (heavy 1h liq one-sided) → LONG_CROWDED (funding_z + LS high) → SHORT_CROWDED → BALANCED. Per-symbol overrides in `DerivativesConfig.regime_per_symbol_overrides`: ETH $20M, SOL $8M capitulation_liq_notional vs BTC $50M.
- **6 — Entry signal integration.** `multi_timeframe.py` adds 3 factors (`derivatives_contrarian=0.7`, `derivatives_capitulation=0.6`, `derivatives_heatmap_target=0.5`) in a single elif chain — at most one fires per cycle. `_heatmap_supports_direction` requires nearest cluster within `ATR*3` AND notional ≥ 70% of largest. `entry_signals.build_trade_plan_from_state` takes `crowded_skip_enabled`/`crowded_skip_z_threshold`; `_should_skip_for_derivatives` rejects when intent aligns with crowded regime AND `|funding_z| ≥ threshold`. Missing data never blocks — only trips with evidence.
- **7 — Journal enrichment + reporting.** 11 idempotent `ALTER TABLE` migrations (2 for partial-TP tracking: `algo_ids`, `close_reason`; 9 for derivatives snapshot). `record_open` gains 9 Optional kwargs: `regime_at_entry`, `funding_z_at_entry`, `ls_ratio_at_entry`, `oi_change_24h_at_entry`, `liq_imbalance_1h_at_entry`, 4× `nearest_liq_cluster_{above,below}_{price,notional}`. `TradeRecord` mirrors. `reporter.regime_breakdown(closed)` → per-regime trades/win_rate/avg_r/expectancy_r in `summary()` + `format_summary()`. `runner._derive_enrichment(state)` pulls from `state.derivatives` + `state.liquidity_heatmap`.
- **8 — CLI modes.** `BotRunner.derivatives_only: bool` (`run_once` early-returns after close-drain; WS + cache + close poll keep running) + `duration_seconds: Optional[int]` (wall-clock deadline; `run()` wait clamps to remaining).

**Failure isolation:** any derivatives subsystem failure (WS disconnect, 401/429, cache crash) logs warning + leaves `state.derivatives=None`/`state.liquidity_heatmap=None`. Strategy degrades to pure price-structure. Missing `COINALYZE_API_KEY` silently falls through.

**Config:** top-level `derivatives:` section — `enabled`, `liquidation_*`, `coinalyze_refresh_interval_s: 60`, `heatmap_*` + `leverage_buckets`, `regime_thresholds` + `regime_per_symbol_overrides`, `confluence_slot_enabled`, `crowded_skip_enabled`, `crowded_skip_z_threshold: 3.0`. New env `COINALYZE_API_KEY=`.

**Usage:** `--derivatives-only --duration 600` for 10-min warm-up (no orders); plain run uses derivatives factors + crowded-skip gate; `scripts/report.py` shows per-regime breakdown.

### Live-demo hardening — Completed (2026-04-17)

Three production-fix sessions during first three live-demo runs. 48 new tests (383 → 431).

**Session 1 — Multi-pair hardening (411 tests):**

*Observability:*
- `src/bot/__main__.py` — clean loguru config: stderr (color) + `logs/bot.log` sink (`colorize=False, enqueue=True, rotation="50 MB", retention=10`).
- `_run_one_symbol` emits `symbol_cycle_start symbol=…` top + terminal `symbol_decision symbol=… [NO_TRADE reason=… | PLANNED …]` — every tick's decision reconstructable from log. `PLANNED` includes `contracts= notional= lev=x margin= risk= risk_bal= margin_bal=`.
- `scripts/logs.py` — tail-and-filter viewer. `--decisions`, `--errors`, `--filter REGEX`, `--lines N`, `--no-follow`, `--no-color`. ANSI-highlights PLANNED/NO_TRADE/fills/rejects/cycles. Default: `tail -F` with 50 lines, auto-recovers on rotation.

*`ltf_momentum_alignment` confluence factor:*
- `multi_timeframe.py` — when LTF trend matches proposed direction → `0.5` weight; when last_signal is counter-trend but fresh (`bars_ago ≤ 3`) and agrees with direction → 60% partial weight. Threaded through runner via `ltf_state=ctx.ltf_cache.get(symbol)`.
- **Zero-falsy gotcha:** `bars_ago=0` is legitimate "just now" — use explicit `None` check (`raw = getattr(…); bars_ago = int(raw) if raw is not None else 99`). `int(x or 99)` silently clobbers freshest signal.

*Per-slot sizing + max-feasible leverage:*
- Problem: isolated mode locked ~80% of margin on first open; subsequent slots tripped `51008` for 26 min.
- Fix 1 — per-slot sizing: `_run_one_symbol` reads `get_total_equity("USDT")` (=`eq`, includes locked) + `get_balance("USDT")` (=`availEq`). `sizing_balance = min(total_eq / max_concurrent_positions, okx_avail, risk_mgr.current_balance)`.
- Fix 2 — max-feasible leverage: `rr_system.py` picks `min(max_leverage, floor(0.6 / sl_pct))` (new `_LIQ_SAFETY_FACTOR = 0.6` — SL sits within 60% of liq distance). With 0.5% SL + 75x, margin drops ~$715 → ~$57 per trade — three concurrent slots fit inside $4300 equity.
- `OKXClient.get_total_equity("USDT")` reads `eq`; `get_balance` keeps reading `availEq`.

*Per-symbol OKX instrument spec:*
- Problem 1 — `51008` on SOL/ETH: YAML hardcoded `contract_size: 0.01` but OKX `ctVal` is per-instrument (BTC=0.01, ETH=0.1, **SOL=1**). SOL "$798 notional" was actually $79,733 → 100× over-size.
- Problem 2 — `59102` after ctVal fix: OKX caps `max_leverage` per instrument (BTC/ETH=100x, **SOL=50x**). YAML `max_leverage=75` rejected every SOL order.
- `OKXClient.get_instrument_spec(inst_id) -> {ct_val, max_leverage}` from `/public/instruments`.
- `BotContext.contract_sizes` + `max_leverage_per_symbol` populated in `_prime()` via `_load_contract_sizes()`. YAML fallback on API hiccup.
- Runner forwards per-symbol values: `max_leverage=min(cfg_max, per_symbol_cap)`, `contract_size=ctx.contract_sizes[symbol]`.

**Session 2 — Cross margin + risk/margin split + threaded-callback fix (418 tests):**

*Cross margin mode:*
- `ExecutionConfig.margin_mode: Literal["isolated","cross"]` (default `"isolated"` back-compat). YAML flips to `cross`.
- `RouterConfig.margin_mode` wires through `from_config`. Cross lets three open positions share full equity as pool — fresh entries not rejected by `51008` just because peers locked isolated margin.
- Latent bug fixed: `_defensive_close` hardcoded `td_mode="isolated"` — now reads `ctx.config.execution.margin_mode`.

*Risk-budget vs margin-fit split:*
- `calculate_trade_plan` gains `margin_balance: Optional[float] = None` (defaults to `account_balance`).
- `max_risk_usdt = account_balance × risk_pct` — R off **total equity**, independent of locked margin.
- `required_leverage`, `max_notional`, `min_lev_for_margin` — all use `margin_balance` (free margin).
- `_run_one_symbol` pattern:
  ```python
  total_eq    = okx.get_total_equity("USDT")
  okx_avail   = okx.get_balance("USDT")
  per_slot    = total_eq / max_concurrent_positions
  risk_balance   = min(total_eq, risk_mgr.current_balance)
  margin_balance = min(per_slot, okx_avail)
  plan = build_trade_plan_with_reason(..., risk_balance, margin_balance=margin_balance, ...)
  ```
- PLANNED log: `risk_bal=<total_eq> margin_bal=<min(per_slot,avail)>` replaces old `sizing_bal=`.

*SL-to-BE threaded-callback fix:*
- Problem: `PositionMonitor.poll()` runs in worker thread via `asyncio.to_thread`. TP1 fire → callback `_on_sl_moved` tried `asyncio.create_task(...)` from worker thread → `RuntimeError: no running event loop`. OKX-side SL replacement still succeeded; only journal's `algo_ids` column update was lost.
- Fix: `BotContext.main_loop: Any = None`. `BotRunner.run()` captures `asyncio.get_running_loop()` at startup. `_on_sl_moved` schedules via `asyncio.run_coroutine_threadsafe(coro, loop)`. Fallback to `create_task` for in-loop callers (tests).

*Fee-drag observation (documented for RL):*
- ETH partial-TP at 75x/$100k notional fully round-tripped TP1+TP2 (OKX `pnl: +$95.91`) but 3 fills × 0.05% taker fee = `fee: -$100.20` → `realizedPnl: -$4.29` (LOSS, `pnl_r=-0.08`). Gross price movement was ~+1.77R win.
- Takeaway: at 75x + tight SL/TP, three-fill partial-TP can be fee-negative. Consider maker orders (0.05% → 0.02%), wider TP2, or per-symbol leverage cap when TP distance is small.

**Session 3 — Fee/slippage-aware entry gates (441 tests):**

*`min_tp_distance_pct` gate:*
- `AnalysisConfig.min_tp_distance_pct: float = 0.0` (off by default). YAML sets `0.004` (0.4%).
- After `_apply_htf_tp_ceiling`, if `abs(tp - entry)/entry < min_tp_distance_pct` → `return None, "tp_too_tight"`. Floor = ~2× round-trip taker fee + slippage headroom.

*`min_sl_distance_pct` gate — widens instead of rejects:*
- `AnalysisConfig.min_sl_distance_pct: float = 0.0` (off by default). YAML sets `0.005` (0.5%).
- Evaluated after `_push_sl_past_htf_zone` but before `calculate_trade_plan`. If `abs(entry - sl)/entry < min_sl_distance_pct`, **SL is widened to exactly the floor** (bullish: `sl = entry - entry * min_sl_distance_pct`; bearish symmetric) rather than rejected. Notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant — just smaller position + more breathing room.
- Rationale: tight Pine OB/FVG stops (often 0.05-0.15%) get wicked out instantly at high leverage. Widening gives the fill a real chance; R stays flat; leverage stays at `floor(0.6/sl_pct)` max-feasible. The 05:49 ETH 0.064%-SL scalp that lost -3.91R would have opened with a 0.5% stop and a 40% smaller position under this rule.
- Initial 0.003 floor (reject version) blocked 264/300 BTC+ETH signals across 5h — 0.005 floor + widen policy restores flow.

*`trading.symbol_leverage_caps`:*
- `TradingConfig.symbol_leverage_caps: dict[str, int] = {}` — operator-side per-symbol cap layered on OKX's instrument-level cap.
- Runner merges three sources: `max_leverage=min(cfg.trading.max_leverage, ctx.max_leverage_per_symbol.get(sym), cfg.trading.symbol_leverage_caps.get(sym))`.
- YAML default caps ETH at 30x (demo flash-down wicks blow ≥30x even when SL structure holds). BTC keeps 75x; SOL inherits OKX 50x.

*Reject reasons:* `below_confluence`, `session_filter`, `no_sl_source`, `crowded_skip`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`. Sub-floor SL distances are widened, not rejected.

**Restart-while-live verified (2026-04-17 06:16Z):** 3 open positions (BTC/ETH/SOL all BEARISH) at restart — OCO algos on OKX kept SL/TP enforcement; `_rehydrate_open_positions()` reloaded into monitor + `open_trade_ids` without `orphan_live_position_no_journal_row` or `journal_open_but_no_live_position` warnings. Restart path works end-to-end.

**Settle budget bump (2026-04-17):** raised TV/Pine settle waits to cut occasional half-rendered reads on symbol/TF switch. `symbol_settle_seconds 4.0→6.0`, `tf_settle_seconds 2.5→3.5`, `pine_settle_max_wait_s 6.0→10.0` (poll interval unchanged at `0.3`). Worst-case per symbol: `6 + 3×(3.5+10) = 46.5s` → 3 pairs ≈ 140s, still inside the 180s 3m-entry cycle. Typical case (freshness-poll returns early on `last_bar` flip) ≈ 60–80s.

**Post-settle grace (2026-04-17):** new `trading.pine_post_settle_grace_s` (default `0.0`, YAML `1.0`). `_wait_for_pine_settle` only watches `signal_table.last_bar` flip — but the **Oscillator** table can lag a beat (worst on 1m, where `last_bar` ticks every wall-clock minute regardless of full table re-render, so the freshness poll passes "early" while oscillator is still computing). After the poll returns true, `_switch_timeframe` now sleeps `pine_post_settle_grace_s` so the rest of the tables catch up before the read. Surgical fix for "1m oscillator empty/stale → switch to 3m happens before LTF render finished". Per cycle cost ≈ 4s for 4 TF switches × 1 symbol, ~12s total for 3 pairs. Worst-case 3 pairs now ≈ 152s, still inside 180s.

**HTF pass skip for already-open symbols (2026-04-17):** `_run_one_symbol` now computes `already_open = any(k[0] == symbol for k in open_trade_ids)` before the HTF pass and guards the entire 15m block (`_switch_timeframe` + `multi_tf.refresh` + `detect_sr_zones`) behind `if not already_open`. HTF S/R cache is only consumed by the entry planner (SL push past zones + TP ceiling); defensive close (Madde F) reads LTF state only, and the dedup check at step 3 would block the new entry anyway. Saves one `tf_settle (3.5s) + freshness-poll (up to 10s) + post-settle grace (1s)` ≈ 5-7s typical / 14.5s worst-case per held position per cycle. For 3-4 concurrent positions on a 5-pair cycle, worst-case shaves 15-58s off ~247s budget; typical cycle (~125-155s) keeps its comfortable 180s headroom. Stale cache is safe: the next cycle after a position closes, `already_open` flips False and HTF reloads before the planner runs. LTF (1m) pass untouched — it feeds both the new-entry `ltf_momentum_alignment` factor and the defensive close check.

### Macro Event Blackout — Completed (2026-04-17)

Conservative scheduled-event blackout: skip new entries around HIGH-impact USD macro releases (CPI / FOMC / NFP / PCE / FED minutes). Open positions untouched (OCO algos manage exit). 26 new tests (441 → 469).

- **`src/data/economic_calendar.py`** — three pieces:
  - `FinnhubClient` mirrors `CoinalyzeClient`: 60/min token bucket, `/calendar/economic?from=&to=` (UTC dates), exponential backoff on 5xx, `429` honors `Retry-After`, `401`/missing-key silent fall-through (warn + always-`None`). `_parse_finnhub_time` accepts `"YYYY-MM-DD HH:MM:SS"` UTC.
  - **Country → currency normalization (parse-time):** Finnhub returns ISO-3166 alpha-2 codes (`"US"`, `"GB"`, `"EU"`, `"JP"` …); FairEconomy returns currency codes (`"USD"`, `"GBP"`, `"EUR"`, `"JPY"`). Without normalization, `currencies: ["USD"]` filter silently dropped every Finnhub event (`"US" != "USD"`) — bot saw 0 events even with a working key. `_country_to_currency()` mapping table normalizes Finnhub at parse time so `EconomicEvent.country` always carries the currency code; downstream filter + dedup compare like-with-like. Pass-through for already-3-char codes keeps idempotence.
  - `FairEconomyClient` (no auth) — fetches **both** `https://nfs.faireconomy.media/ff_calendar_thisweek.json` AND `…/nextweek.json` in parallel via `asyncio.gather`. Single-URL failure tolerated; only when *all* URLs fail does the call return `None`. Without nextweek the bot was blind to next-Mon/Tue events when run late in the week. **404 on `nextweek.json` is normal** (file gets published mid-week) — demoted to `DEBUG` log so it doesn't pollute warnings.
  - `EconomicCalendarService` orchestrator: `refresh()` calls both providers in parallel via `asyncio.gather(return_exceptions=True)`, dedupes (`_dedup_events` clusters by normalized title + country + ±15min window — same event from multiple sources collapses to one with `source="finnhub+faireconomy"`), background `run_refresh_loop(stop_event)` polls every `refresh_interval_s` (default 21600 = 6h). Pure-sync `is_in_blackout(now) -> BlackoutInfo` walks cached events, returns `(active, event, seconds_until_event, seconds_after_event, reason)`.
- **Source diversity:** ANY provider flagging a HIGH-impact event in the [-`blackout_minutes_before`, +`blackout_minutes_after`] window activates blackout. Either provider failing alone is non-fatal (logged, falls back to other source). Both failing leaves cached events intact (soft fail = no new fetch, existing window logic still runs against last-known events).
- **Failure isolation:** missing `FINNHUB_API_KEY` → warn at construction, `fetch_events` returns `None`, FairEconomy carries the calendar alone. Both down + cache empty → `is_in_blackout(now).active = False` (bot keeps trading; we don't halt on news-feed failure).
- **Decision point:** `_run_one_symbol` calls `is_in_blackout(_utc_now())` *before* TV symbol/TF switch (saves ~46s of settle when blacked out). Blackout active → `symbol_decision symbol=… NO_TRADE reason=macro_event_blackout event='CPI m/m' country=USD impact=HIGH secs_to_event=… secs_after_event=… source=finnhub+faireconomy` log line, function returns. Open positions and OCO algos untouched. Joins existing reject-reason family (`crowded_skip`, `tp_too_tight`, `session_filter`, etc.).
- **Lifecycle:** `BotRunner._start_economic_calendar()` warms the cache via `await refresh()` then spawns `asyncio.create_task(service.run_refresh_loop(self._stop_event))`. `_stop_economic_calendar()` cancels the task + `await service.close()`. Both wrapped in best-effort try/except — any setup failure logs warning + sets `ctx.economic_calendar = None`, bot keeps running on price structure alone.
- **Out of scope (deferred to post-Phase-7):** sentiment classification of crypto news (CryptoPanic etc.), real-time event detection, exit-side news triggers. These are the overfit-prone parts; only acting on *scheduled* events keeps this layer overfit-resistant.
- **Config:** new top-level `economic_calendar:` section — `enabled` (YAML `true`, code default `false`), `finnhub_enabled`, `faireconomy_enabled`, `blackout_minutes_before` (30), `blackout_minutes_after` (15), `impact_filter` (`["High"]`), `currencies` (`["USD"]`), `refresh_interval_s` (21600), `lookahead_days` (7), per-provider `*_timeout_s` + `*_max_retries`. New env `FINNHUB_API_KEY=` (free 60/min at https://finnhub.io/dashboard).

### Fee-aware sizing + TP1/TP2 guarantee + fee-buffered BE — Completed (2026-04-17)

Three interlocking policy changes so the nominal `$30 R / $90 TP` (1:3 @ 1% of 3200 USDT) survives a three-fill partial-TP lifecycle. The ETH Session-2 case made the gap concrete: gross `pnl +$95.91` → fees `-$100.20` → realized `-$4.29` on a price-wise winner. 10 new tests (469 → 479). All three knobs are config-gated — set to 0 to revert.

- **Fee-aware sizing (`src/strategy/rr_system.py`, `src/strategy/trade_plan.py`).** `calculate_trade_plan(..., fee_reserve_pct)` widens the sizing denominator: `effective_sl_pct = sl_pct + fee_reserve_pct`, `ideal_notional = max_risk_usdt / effective_sl_pct`. Notional shrinks by `sl_pct / (sl_pct + fee_reserve_pct)` so a stop-out caps near `$R` **after** entry + exit taker fees. TP price is unchanged (still `entry ± sl_distance * rr_ratio`) — fee compensation flows through size, never by widening TP. `TradePlan.fee_reserve_pct` persisted for traceability; `risk_amount_usdt` remains gross (price-only) so `pnl_r` stays comparable for Phase 7 RL rewards.
- **TP1/TP2 guarantee (`src/strategy/entry_signals.py`, `src/execution/order_router.py`).** `build_trade_plan_with_reason` / `build_trade_plan_from_state` gain `partial_tp_enabled` + `partial_tp_ratio` kwargs. After `calculate_trade_plan` returns, the split is simulated: if `int(num_contracts * ratio) == 0` or the remainder is 0, return `(None, "insufficient_contracts_for_split")`. Joins the reject-reason family (`tp_too_tight`, `htf_tp_ceiling`, `crowded_skip`, `zero_contracts`). `OrderRouter._place_algos` used to silently fall back to single OCO on a degenerate split — now raises `RuntimeError` with a diagnostic message (wrapped by `AlgoOrderError` via `router.place`) so a bypassed gate fails loud instead of quietly degrading the two-leg structure. Degenerate plans cost a rejection, not a broken promise — user confirmed "risk discipline over trade count."
- **Fee-buffered SL-to-BE (`src/execution/position_monitor.py`, `src/bot/config.py`).** `PositionMonitor(..., sl_be_offset_pct)`. When TP1 fills (size shrinks mid-poll), the replacement OCO's SL sits at `be_price = entry + entry * offset_pct * sign` where `sign = +1 for long, -1 for short` — for a long at 100 with `offset=0.001`, BE stop is 100.10. A touch-back to "near entry" exits at **positive gross PnL** covering the remaining leg's exit taker fee + small slippage, so the realized close is ≥ 0. `sl_be_offset_pct=0` preserves exact-entry legacy behavior (kept as test default). Log line now includes `be_price=` + `offset_pct=` alongside `new_algo=`.
- **Fee audit trail (`src/execution/models.py`, `src/execution/okx_client.py`, `src/bot/runner.py`).** `CloseFill.fee_usdt` added (defaults to `0.0`). `OKXClient.enrich_close_fill` reads the `fee` field from `/account/positions-history` (OKX returns it as negative USDT). Runner passes `fees_usdt=abs(enriched.fee_usdt)` into `TradeJournal.record_close`. Column already existed; no schema migration. Lets post-hoc reporting quantify fee drag per trade/symbol, and gives RL a clean `fees_usdt` feature later.
- **Config:** new `trading.fee_reserve_pct: 0.001` (≈ 2× OKX demo taker 0.05%) and `execution.sl_be_offset_pct: 0.001` (≈ one round-trip taker on the surviving leg). Both default to `0.0` in code for test back-compat; YAML carries the live values.
- **Rollback:** `trading.fee_reserve_pct: 0.0` disables fee-aware sizing; `execution.sl_be_offset_pct: 0.0` reverts SL-to-BE to exact entry; `execution.partial_tp_enabled: false` skips both the two-leg path and the sub-split reject. No migrations, no state files — YAML edit + restart.

---

## Phase 7 — Reinforcement learning (Next)

**Architecture:** parameter tuner, NOT raw decision maker. Rule-based strategy generates signals; RL tunes:
- `confluence_threshold` (2-5), `pattern_weights` (dict), `min_rr_ratio` (1.5-5.0)
- `risk_pct` (0.005-0.02), `htf_required` (bool), `session_filter` (list)
- `volatility_scale` (0.5-2.0), `ob_vs_fvg_preference` (0.0-1.0)

**Reward** = `pnl_r + setup_penalty + dd_penalty + consistency_bonus`
- `setup_penalty = -3.0` if confluence < 2
- `dd_penalty = -2.0` if dd > 5%, `-1.0` if > 3%
- `consistency_bonus = min(sharpe_last10 * 0.5, 1.5)`

**Walk-forward:** train 1-N, validate N+1 to N+50, advance window. Rules: never deploy params that didn't improve OOS; reduce LR if params swing; retrain every 50 new trades OR weekly; min 50 trades before first training.

**Cycle:** `python scripts/train_rl.py --min-trades 50 --walk-forward`. Improved params → `config/strategies/active.yaml`.

**Pre-RL workflow (mandatory before first training run):**

1. **Filter dirty data.** Early API-test trades + pre-fix trades poison the training set — RL learns the noise as signal. Define a `clean_since` cutoff (entry_timestamp after the last meaningful policy change) and feed only trades after it. Old rows stay in DB for old-vs-new regime comparison; never delete.
2. **Read the reporter first.** `.venv/Scripts/python.exe scripts/report.py --last 7d` shows `win_rate_by_session`, `win_rate_by_factor`, `regime_breakdown`. Open-eye obvious losers (e.g. session with <%20 WR, factor that drags expectancy negative) get fixed manually in YAML — RL is not for catching things you can already see.
3. **Hand-tune the baseline.** Disable bad sessions, drop/zero pattern weights that lose money, tighten thresholds. Goal: baseline should be at least break-even on the clean window before RL touches it. RL is fine-tuning on a working strategy, not rescue surgery on a broken one.
4. **Then RL.** Walk-forward only after baseline is positive on ≥50 clean trades. RL's job is squeezing the last 10-30% out of `confluence_threshold`, `pattern_weights` ratios, `min_rr_ratio`, etc. — not flipping signs.

**What RL actually does (mental model):** reads each trade's feature columns (`confluence_score`, `confluence_factors`, `session`, `regime_at_entry`, `funding_z_at_entry`, `htf_bias`, …) and pairs them with `pnl_r`. Gradient updates parameters so the *trades that pass the filters* maximize average `pnl_r`. **It does NOT do root-cause analysis** — 16 losses from 5 different causes all look like "this feature combination = LOSS" to the optimizer. This is why step 2-3 above (human pattern-spotting on aggregate stats) cannot be skipped.

## Currency pair strategy

**5 OKX perps — BTC / ETH / SOL / DOGE / XRP.** Phase 1.5'te 5 → 3'e inilmişti (Coinalyze free-tier budget + dengeli RL dataset için). 2026-04-17'de DOGE + XRP eklendi — BTC/ETH/SOL genelde correlated, momentum-driven iki parite uncorrelated alpha ekler. Coinalyze free-tier budget hâlâ güvenli: 5 × 5 call / 60s = 25/40 min. `trading.symbols` tek kaynak; legacy single-`symbol` form `DeprecationWarning` ile yüklenir.

**`max_concurrent_positions=4`** (5 parite 4 slot için yarışır — her cycle 1 parite beklemede kalır, confluence gate daha iyi sinyal seçer; 4. pozisyon queue karakteri). `per_slot = total_eq / 4 ≈ $800` margin budget (cross margin mode ile shared pool). R hâlâ total_eq'nun %1'i sabit, sadece notional tavan %25 küçülür.

**Cycle timing (5 parite, 3m entry TF = 180s cycle):** typical ~125-155s (freshness-poll erken döner), worst ~247s (her TF switch max timeout'a giderse). Worst-case bazen oluşursa sadece o cycle skip olur, bir sonraki yakalar. DOGE + XRP 30x leverage cap'li (`symbol_leverage_caps`) ve SOL-sınıfı thin book sayılıp `$8M capitulation_liq_notional` override aldılar.

**Adding a 6th+ pair:** drop into `trading.symbols`, confirm `okx_to_tv_symbol()` (add parametrized test), add `derivatives.regime_per_symbol_overrides` (smaller OI pools → smaller `capitulation_liq_notional`), watch first 20-30 cycles for `htf_settle_timeout`/`set_symbol_failed` — illiquid pairs flunk freshness-poll more. 6 pair + 4 slot'ta Coinalyze 30/40 min, cycle typical ~150-180s → worst-case pressure başlar; `pine_settle_max_wait_s` düşürmek gerekebilir.

## Configuration

Full config in `config/default.yaml`. Sections: `bot` (mode, poll interval), `trading` (symbols, TFs, risk/max_leverage, rr_ratios, max_concurrent, `symbol_leverage_caps`), `circuit_breakers`, `analysis` (confluence, swing_lookback, sr, session_filter, `min_tp_distance_pct`, `min_sl_distance_pct`), `execution` (margin_mode, partial_tp_*, ltf_reversal_*), `reentry`, `derivatives`, `economic_calendar` (blackout window, providers), `okx` (demo_flag), `rl`.

`.env` keys: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `TV_MCP_PORT`, `LOG_LEVEL`.

## Tech stack

**Python** (`requirements.txt`): pydantic, pyyaml, python-dotenv, aiosqlite, httpx, **python-okx (0.4.x)**, websockets, pandas, numpy, ta, stable-baselines3, gymnasium, torch, loguru, rich, schedule.

**Node:** `tradingview-mcp`, `okx-trade-mcp` + `okx-trade-cli`.

## Workflow commands

```bash
python -m src.bot --config config/default.yaml           # Demo
OKX_DEMO_FLAG=0 python -m src.bot --config ...           # Live (after demo proven)
python scripts/report.py --last 7d                       # Report
python scripts/train_rl.py --min-trades 50 --walk-forward
.venv/Scripts/python.exe -m pytest tests/ -v             # Tests
.venv/Scripts/python.exe scripts/logs.py                 # Follow live log
.venv/Scripts/python.exe scripts/logs.py --decisions     # Entry/exit only
.venv/Scripts/python.exe scripts/logs.py --errors        # ERROR/WARNING only
.venv/Scripts/python.exe scripts/logs.py --filter SOL    # Filter by regex
```

**Pine dev cycle** (Claude via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix errors → `tv pine analyze` → `tv screenshot`.

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, can break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. Start `--profile demo`. Never enable withdrawal perms. Bind key to machine IP. AI is non-deterministic → verify before live. Sub-account for live.

**Trading risks:** research project, not financial advice. Crypto futures = liquidation risk. Demo first; live with minimal capital. Check OKX TOS for automated trading.

**RL risks:** overfitting is #1 — always walk-forward. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL.
