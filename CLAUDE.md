# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on OKX. Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes. Demo-runnable end-to-end today; the near-term goal is to collect a clean dataset, then learn from it.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, trains RL, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, OKX = hands, Python = brain.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 7 OKX perps — `BTC / ETH / SOL / DOGE / XRP / ADA / BNB`. 7 concurrent slots on cross margin (all active, no queue).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights. *Premium/discount gate temporarily disabled 2026-04-19 — see changelog; to be re-enabled as a soft/weighted factor (~10-15%) post-Phase-9.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. OCO SL/TP, partial TP at 1.5R with fee-buffered SL-to-BE on TP1 fill.
- **Journal:** async SQLite, schema v2 (zone source, wait/fill latency, trend regime, funding Z-scores). `rejected_signals` table with counter-factual outcome pegging.
- **Tests:** ~682, all green. Demo-runnable end-to-end.
- **Data cutoff (`rl.clean_since`):** `2026-04-19T06:30:00Z`. Reporter and future RL see only post-pivot trades.

---

## Changelog

### 2026-04-19 — Premium/discount gate temporarily disabled

- **`analysis.premium_discount_veto_enabled: true → false`** (`config/default.yaml:245`).
- **Reason:** range-bound tape after scalp-native rewire → all 7 symbols chronically rejecting on `wrong_side_of_premium_discount`; zone-based entries (VWAP retest, EMA21 pullback, FVG, sweep) never got to fire. Operator opted to let the zone layer take over for data collection.
- **Re-enable plan:** bring back as a *soft / weighted* factor, **not** a hard gate. Target weight equivalent ~**10-15%** of final confluence contribution (exact form — inverse distance from midpoint, scaled penalty, or pillar-style factor — to be chosen from Phase 9 GBT output).
- **Dataset implication:** trades opened during this window are NOT P/D-disciplined. If factor-audit shows "premium long / discount short" bleeding WR, bump `rl.clean_since` forward before RL training so Phase 10 doesn't learn chase-the-move behavior.
- **Other gates unchanged:** displacement, EMA momentum, VWAP, cross-asset opposition still active. No code changes, no test changes.

### 2026-04-19 — Scalp-native rewire

- **Zone priority reordered** (`src/strategy/setup_planner.py`): `vwap_retest → ema21_pullback → fvg_entry (entry-TF) → sweep_retest → liq_pool_near`. HTF FVG demoted to opt-in (`htf_fvg_entry_enabled=false`).
- **New source `ema21_pullback`**: fires when EMA21/55 stack aligns with direction and price is within `zone_atr × ATR` of EMA21 (half-ATR band around EMA21).
- **New source `fvg_entry`**: entry-TF (3m) unfilled FVG from `state.active_*_fvgs()`. HTF 15m FVG stays available but behind the flag.
- **Liquidity role flipped**: primary use is TP, not entry. `liq_pool_near` now gated by two filters — `liq_entry_near_max_atr=1.5` (distance) AND `liq_entry_magnitude_mult=2.5` (notional ≥ 2.5× side-median). Entry price = zone mid, not edge.
- **TP ladder from liquidity heatmap** (`tp_ladder_enabled=true`, shares `[0.40, 0.35, 0.25]`, `min_notional_frac=0.30`). `TradePlan.tp_ladder` + `ZoneSetup.tp_ladder` added; falls back to single-leg when heatmap absent.
- **Weights rebalanced** (`src/analysis/multi_timeframe.py` DEFAULT_WEIGHTS): oscillator/overlay dominant — `vwap_composite_alignment=1.25`, `money_flow_alignment=1.0`, `oscillator_high_conviction_signal=1.5`, `divergence_signal=1.25`; structure weights trimmed (`htf_trend_alignment=0.5`, `at_order_block=0.6`, `at_fvg=0.75`).
- **Runner**: candle buffer bumped `last(50) → last(100)` so EMA55 SMA-seed has clean history.
- **Config surface**: `execution.ema21_pullback_enabled`, `execution.htf_fvg_entry_enabled`, `execution.liq_entry_near_max_atr`, `execution.liq_entry_magnitude_mult`, `execution.tp_ladder_*`.
- **Tests**: `tests/test_setup_planner.py` rewritten (30 cases covering new priority, gates, ladder). Full suite 682/682.

---

## Prerequisites

Node.js 18+, Python 3.11+ (actual 3.14), TradingView Desktop (subscription), OKX demo account, Claude Code CLI.

---

## MCP Setup

### TradingView MCP

- Repo: `C:\Users\samet\Desktop\tradingview-mcp\`
- TradingView Desktop extracted from MSIX to `C:\TradingView\` — **MSIX sandbox blocks the debug port, must use standalone exe.**
- Launch: `"C:\TradingView\TradingView.exe" --remote-debugging-port=9222`. CDP at `http://localhost:9222`.
- MCP config: `~/.claude/.mcp.json` → `C:/Users/samet/Desktop/tradingview-mcp/src/server.js`.

**Key `tv` CLI:**
```bash
tv status                              # symbol, TF, indicators
tv data tables --filter "SMT Signals"  # overlay table
tv data tables --filter "SMT Oscillator"
tv data labels/boxes/lines --filter --verbose
tv pine set < script.pine              # load Pine
tv pine compile / analyze / check
tv screenshot
tv symbol OKX:BTCUSDT.P
tv timeframe 15
```

### OKX Agent Trade Kit MCP

```bash
npm install -g okx-trade-mcp okx-trade-cli
okx setup --client claude-code --profile demo --modules all
```

Required OKX account mode (bot won't place a single order otherwise):
1. Demo Trading → Settings → **Account mode = "Futures"** (`acctLv=2`). `acctLv=1` forces `net_mode` and rejects every call with `Parameter posSide error`.
2. **Position mode = "Hedge" (Long/Short)** → `posMode=long_short_mode`.
3. Verify via `get_account_config()`.

Demo API key: Read+Trade only, never withdrawal. Demo balance resets are UI-only.

**OKX naming:** Perp = `BTC-USDT-SWAP`, Spot = `BTC-USDT`. TV ticker = `OKX:BTCUSDT.P`.

---

## Pine Scripts

Two indicators on the chart. Bot reads their tables; drawings (OB/FVG boxes, liquidity lines) are read as supplementary zone sources.

| Script | File | Output |
|---|---|---|
| SMT Master Overlay | `pine/smt_overlay.pine` | 19-row "SMT Signals" table + OB/FVG boxes + liquidity/sweep drawings |
| SMT Master Oscillator | `pine/smt_oscillator.pine` | 15-row "SMT Oscillator" table (WaveTrend + RSI/MFI + Stoch + divergences) |

Pine is source-of-truth for **structure**; Python scores confluence and plans zones. Legacy single-purpose scripts are under `pine/legacy/` (not loaded).

**Critical:** Table cells use `str.tostring(val, "#.########")` not `"#.##"` — truncation zeroes DOGE/XRP ATR and causes `no_sl_source` every cycle.

---

## Architecture

Modules have docstrings; a tour for orientation:

- `src/data/` — TV bridge, `MarketState` assembly, candle buffers, Binance liq WS, Coinalyze REST, economic calendar (Finnhub + FairEconomy), HTF cache.
- `src/analysis/` — Structure (MSS/BOS/CHoCH), FVG, OB, liquidity, ATR-scaled S/R, multi-TF confluence + regime-conditional weights, derivatives regime, **ADX trend regime**, **EMA momentum veto**, **displacement / premium-discount** gates.
- `src/strategy/` — R:R math, SL hierarchy, entry orchestration, **setup planner** (zone-based limit-order plans), cross-asset snapshot veto, risk manager.
- `src/execution/` — python-okx wrapper (sync → `asyncio.to_thread`), order router (`place_limit_entry` / `cancel_pending_entry` / market fallback), REST-poll position monitor with **PENDING** state, typed errors.
- `src/journal/` — async SQLite, schema v2 trade records, `rejected_signals` + counter-factual stamps, pure-function reporter.
- `src/bot/` — YAML/env config, async outer loop (`BotRunner.run_once` — closes → snapshot → pending → per-symbol cycle), CLI entry.

End-to-end tick walkthrough: see `docs/trade_lifecycle.md`.

---

## Strategy (one-pager)

### Five pillars (scoring)

| Pillar | Concrete factors |
|---|---|
| Market Structure | `mss_alignment`, `recent_sweep` |
| Liquidity | Pine standing pools + Coinalyze heatmap + sweep-reversal |
| Money Flow | `money_flow_alignment` (MFI bias) |
| VWAP | `vwap_composite` (all-3 TF align → 1.0, 2-of-3 → 0.5, 1-of-3 → 0) |
| Divergence | `divergence_signal` (regular + hidden, bar-ago decay) + `oscillator_high_conviction_signal` |

### Hard gates (reject, not scored)

`displacement_candle` · `ema_momentum_contra` · `vwap_misaligned` · `cross_asset_opposition` (altcoin veto when BTC+ETH both oppose). *`premium_discount_zone` is wired but currently disabled (`analysis.premium_discount_veto_enabled=false`) — see changelog 2026-04-19.*

### Zone-based entry

`confluence ≥ threshold → setup_planner picks a ZoneSetup → post-only limit at zone edge → N bars wait → fill | cancel`.

Zone source priority: **unswept liq pool + P/D match** > **HTF 15m unfilled FVG + displacement** > **VWAP retest** > **sweep-and-reversal**.

Position lifecycle: `PENDING → FILLED → OPEN → CLOSED` or `PENDING → CANCELED`.

### Regime awareness

ADX (Wilder, 14) classifies `UNKNOWN / RANGING / WEAK_TREND / STRONG_TREND`. Under `STRONG_TREND`, trend-continuation factors get 1.5× and sweep factors 0.5×; `RANGING` mirrors. Journal stamps `trend_regime_at_entry` on every trade.

---

## Configuration

All config in `config/default.yaml` (self-documenting). Top-level sections: `bot`, `trading`, `circuit_breakers`, `analysis`, `execution`, `reentry`, `derivatives`, `economic_calendar`, `okx`, `rl`.

**`.env` keys:** `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons (unified):** `below_confluence`, `no_setup_zone`, `wrong_side_of_premium_discount`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip`, `no_sl_source`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`, `zone_timeout_cancel`, `pending_invalidated`. Sub-floor SL distances are **widened**, not rejected. Every reject writes to `rejected_signals`.

**Circuit breakers (currently loosened for data collection):** `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.5`. Restore to `5 / 15 / 25 / 2.0` after 20+ post-pivot closed trades.

---

## Non-obvious design notes

Things that aren't self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing

- **`_MARGIN_SAFETY=0.95` + `_LIQ_SAFETY_FACTOR=0.6`** (`rr_system.py`). Reserve 5% for fees/mark drift (else `sCode 51008`). Leverage capped at `floor(0.6/sl_pct)` so SL sits well inside liq distance.
- **Risk vs margin split.** R comes off total equity; leverage/notional sized against per-slot free margin (`total_eq / max_concurrent_positions`). Log emits `risk_bal=` + `margin_bal=` separately — they're different by design.
- **Per-symbol `ctVal`.** BTC `0.01`, ETH `0.1`, **SOL `1`**. `OKXClient.get_instrument_spec` primes `BotContext.contract_sizes`. Hardcoded YAML would 100× over-size SOL.
- **Fee-aware sizing** (`fee_reserve_pct=0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct` so stop-out caps near $R *after* entry+exit taker fees. `risk_amount_usdt` stays gross for RL reward comparability.
- **SL widening, not rejection.** Sub-floor SL distances widen to the per-symbol floor; notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant.

### Execution

- **PENDING is first-class.** A filled limit without PENDING tracking would race the confluence recompute and potentially place two OCOs.
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct=0.001`). After TP1 fill the replacement OCO's SL sits a hair past entry on the profit side — covers remaining leg's exit taker fee + slippage.
- **SL-to-BE never spins.** Cancel and place are separate try-blocks. OKX `{51400,51401,51402}` on cancel = idempotent success. Repeated cancel failure after 3 attempts → give up + mark `be_already_moved=True` (poll stops hammering). Place failure after cancel = unprotected position, CRITICAL log, operator decides — **emergency market-close is not automated**.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. SL-to-BE callback uses `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)`; `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Close enrichment is non-optional.** `PositionMonitor` only knows the position vanished. `OKXClient.enrich_close_fill` queries `/account/positions-history` for real `realizedPnl`. Without it every close looks BREAKEVEN and breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.

### Data quality

- **`CryptoSnapshot` order is load-bearing.** BTC + ETH cycle first so altcoin cycles can read the snapshot for cross-asset veto. Reorder and the veto silently fails open.
- **`bars_ago=0` is legitimate "just now".** Use `int(x) if x is not None else 99`, not `int(x or 99)` — the latter silently clobbers the freshest signal.
- **Blackout decision is BEFORE TV settle.** Saves ~46s per blacked-out symbol.
- **Derivatives failures isolate.** WS disconnect / 401 / 429 → `state.derivatives=None`, strategy degrades to pure price-structure.
- **FairEconomy `nextweek.json` 404 is normal** (file published mid-week). Without it the bot is blind to next-Mon/Tue events when run late in the week.

### Multi-pair + multi-TF

- **Pine freshness poll.** `last_bar` is the beacon. `pine_post_settle_grace_s=1.0` covers the 1m-TF lag where `last_bar` flips before the Oscillator finishes rendering.
- **HTF skip for open-position symbols.** Skipping the 15m pass saves ~5-15s per held position per cycle. Dedup would block re-entry anyway.

### Risk & state

- **Risk manager replay.** `journal.replay_for_risk_manager(mgr)` rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from closed trades on startup — durable truth over in-memory state. Drawdown breaker = permanent halt (manual restart required).
- **SL-to-BE survives restart.** `trades.sl_moved_to_be` flag forwards as `be_already_moved=True` on rehydrate so the monitor doesn't double-move the SL.
- **Orphan reconcile is log-only.** Operator decides — no auto-close.

---

## Currency pair notes

7 OKX perps — BTC / ETH / SOL / DOGE / XRP / ADA / BNB. BTC + ETH + BNB are market pillars (major-class book depth); SOL/DOGE/XRP/ADA are altcoins gated by the cross-asset veto.

`max_concurrent_positions=7` (every pair can hold a position simultaneously — no slot competition; confluence gate still picks setups, but cycle isn't queue-limited). Cross margin, `per_slot ≈ total_eq / 7 ≈ $714` on a $5k demo. R stays 1% of total equity ($50); only the notional ceiling shrinks proportionally.

Cycle timing at 3m entry TF = 180s budget: typical 210–240s with 7 pairs (post-2026-04-19 expansion; TF bütçesi aşılıyor, bar zaman zaman atlanır), worst ~330s. Worst-case skips a cycle; next bar catches up. DOGE/XRP/ADA leverage-capped at 30x; SOL/BNB inherit OKX 50x cap.

Per-symbol overrides (YAML):
- `swing_lookback_per_symbol`: DOGE/XRP/ADA=30 (thin 3m books).
- `htf_sr_buffer_atr_per_symbol`: SOL=0.10 (wide-ATR, narrower buffer); DOGE/XRP/ADA=0.15; BNB inherits global 0.2.
- `session_filter_per_symbol`: SOL/DOGE/XRP/ADA=[london] only. BNB inherits global (london+new_york) as major.
- `min_sl_distance_pct_per_symbol`: BTC 0.004, ETH 0.006, SOL 0.010, DOGE/XRP/ADA 0.008, BNB 0.005.

Adding an 8th+ pair: drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol`, watch 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`. Coinalyze free tier supports ~8 pairs at refresh_interval_s=75s; beyond that needs paid tier or longer interval.

---

## Workflow commands

```bash
# Smoke test — full pipeline, one tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo run
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Auto-stop at Phase 8 data-collection gate
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --max-closed-trades 50

# Live (after demo proven)
OKX_DEMO_FLAG=0 .venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Clear a tripped halt
.venv/Scripts/python.exe -m src.bot --clear-halt --config config/default.yaml

# Analytics
.venv/Scripts/python.exe scripts/report.py --last 7d
.venv/Scripts/python.exe scripts/factor_audit.py                   # per-symbol/session/regime WR + counter-factuals
.venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --commit # stamp rejected hypothetical outcomes

# Tests
.venv/Scripts/python.exe -m pytest tests/ -v
```

**Pine dev cycle** (via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix → `tv pine analyze` → `tv screenshot`.

---

## Forward roadmap

The bot is ready to run. Everything below is sequenced and gated — don't skip steps.

### Phase 8 — Data collection (active)

**Goal:** accumulate a clean post-pivot dataset. No code changes unless factor-audit reveals a regression.

- Run demo bot. `rl.clean_since=2026-04-19T06:30:00Z` keeps reporter + RL on post-pivot data only.
- Run `scripts/factor_audit.py` every ~10 closed trades — early-warning on factor regressions before they eat the dataset.
- Run `scripts/peg_rejected_outcomes.py --commit` weekly to stamp counter-factual outcomes on rejected signals.

**Gate to leave:** ≥50 closed post-pivot trades, WR ≥ 45%, avg R ≥ 0, ≥2 trend-regimes represented, net PnL ≥ 0.

**If the gate fails:** factor-audit output is diagnostic. Expect 2-3 iterations of per-symbol threshold / weight / veto tuning before the gate holds. Do not shortcut to GBT or RL until the gate holds.

### Phase 9 — GBT analysis

**Goal:** learn which factors and factor combos actually predict outcome on clean data.

- `scripts/analyze.py` (xgboost) on clean trades: feature importance, partial dependence plots, SHAP values per feature.
- Include `rejected_signals` counter-factuals as negative-class data — reveals which reject reasons threw away winners.
- Output: per-symbol threshold / factor weight / veto threshold recommendations.
- Manual tune YAML based on GBT signal. Re-run Phase 8 with new config, check that WR improves.

**Gate to leave:** GBT + manual tuning plateau — two consecutive tuning rounds produce no measurable WR improvement.

### Phase 10 — RL training

**Goal:** use RL to tune parameters GBT can't optimize well (interaction effects, regime transitions).

- Framework: stable-baselines3 (PPO or SAC). Environment: `gymnasium` wrapper around replay of clean trades.
- **Scope: parameter tuner, not decision maker.** RL adjusts factor weights, thresholds, zone-source priorities — not "should I trade this." The 5-pillar + hard-gate structure is fixed.
- Reward shape: `pnl_r + setup_penalty + drawdown_penalty + consistency_bonus`. Tuned on walk-forward backtests, not a single hold-out set.
- **Walk-forward is mandatory.** Train on months 1-3, validate on month 4; slide window monthly. Any parameter set that doesn't improve on out-of-sample never ships.
- Checkpoint cadence: every 10k env steps, keep last 5. Manual review of parameter drift before deploying.

**Gate to leave:** RL parameters produce ≥15% WR improvement on walk-forward OOS **and** drawdown stays within 1.1× of manual-tuned baseline. Otherwise the ceiling is structural, not parametric — stay on manual tuning and revisit after 100+ more trades.

### Phase 11 — Post-RL: live transition + scaling

**Goal:** move from demo to live with survivable position sizing, then scale risk by performance.

- **Live transition:** new OKX live account (not demo migration). Sub-account recommended. Start with **$500-1000 risk capital**, `risk_pct=0.5%`, `max_concurrent_positions=2`. Cross margin with explicit notional cap.
- **Stability period:** 2 weeks / 30 trades with no code changes. Compare live WR and avg R to demo baseline within ±5%. Slippage, fill latency, and partial fills WILL differ — measure, don't assume.
- **Scaling rules:** only scale after 100 live trades. Double risk_pct only if 30-day rolling WR ≥ demo WR - 3% and drawdown stays ≤ 15%. Asymmetric: downside scales faster than upside (halve risk_pct on any 10-trade rolling WR < 30%).
- **Monitoring:** journal-backed dashboard (simple pure-Python or Streamlit). Alert on: drawdown >20%, 5-loss streak, OKX rate-limit 429, fill latency P95 >2s, daily realized PnL < -2R.

### Phase 12 — Future enhancements (post-stable)

These are candidates, **not commitments.** Re-evaluate after Phase 11 stability.

- **On-chain data layer** — Arkham API or Glassnode/Coinalyze premium tier. Add as `src/data/on_chain.py` with the same failure-isolation pattern. Use cases: exchange inflow/outflow alignment, smart-money accumulation bias, fake-breakout filter. Only integrate if GBT/RL reveals a feature gap that on-chain fills — don't add speculatively.
- **HTF Order Block re-add** — Pine 3m OBs failed post-pivot (0% WR in Sprint 3). 15m OBs may survive; factor-audit should confirm HTF OB signal before re-enabling.
- **Pine overlay split** — `smt_overlay.pine` → `_structure.pine` + `_levels.pine`. Parallelizes TV recompute per symbol-switch. Worth the refactor only if freshness-poll latency becomes a bottleneck.
- **Additional pairs** — 6th+ OKX perp. Coinalyze budget allows ~6 symbols at free tier. Add parametrized instrument spec test + per-symbol YAML overrides before bringing online.
- **Runner trail / post-TP1 re-evaluation** — dynamic exit after TP1. Deferred from 7.D5; data-driven decision after 100+ post-pivot trades. Are we leaving too much on TP2, or is BE-after-TP1 the right discipline?
- **Multi-strategy ensemble** — after scalper matures, add a separate swing module (higher TFs, different pillar weights) and route to shared execution layer. Big scope; only meaningful once scalper is provably stable.
- **Auto-retrain loop** — monthly RL refresh on rolling window. Cron + CI pipeline. Meaningless until Phase 11 is steady.
- **Alt-exchange support** — Bybit / Binance futures. Current execution layer is OKX-specific; abstracting `ExchangeClient` is 2-3 weeks of careful refactor.

### What is explicitly NOT on the roadmap

- **Decision-making RL.** Structural decisions (5-pillar, hard gates, zone-based entry) stay fixed. RL is a parameter tuner.
- **Claude Code as runtime decider.** Claude writes code and analyzes logs; it does not decide trades per candle.
- **Higher-frequency scalping (1m/30s entry).** TV freshness-poll latency makes sub-minute entry TFs unreliable. Infrastructure rewrite (direct exchange WS feed + in-process indicators) would be a different project.
- **Leverage > 100x or custom margin modes outside cross.** Operator caps and OKX caps combine to forbid this. Do not revisit without a risk memo.

---

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, may break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. `--profile demo` first. Never enable withdrawal. Bind key to machine IP. Sub-account for live.

**Trading:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital.

**RL:** overfitting is the #1 risk — walk-forward is mandatory. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if a structural ceiling is evident.
