# CLAUDE.md — Crypto Futures Trading Bot

## Overview

AI-powered crypto futures bot: two MCP bridges + Python core.

- **TradingView MCP** — chart data, indicator values, Pine Script dev cycle
- **OKX Agent Trade Kit MCP** — order execution on OKX (demo first, live later)
- **Python Bot Core** — autonomous loop: data → analysis → strategy (R:R) → execution → journal → RL retraining

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, builds/trains RL, debugs). Per-candle trade decisions are made by the Python bot, **not** by Claude at runtime. TradingView = eyes. OKX = hands. Python bot = brain.

## Prerequisites

Node.js 18+, Python 3.11+ (actual: 3.14), TradingView Desktop (subscription), OKX account (demo needs no deposit), Claude Code.

## MCP Setup

### TradingView MCP

- Repo: `C:\Users\samet\Desktop\tradingview-mcp\`
- TradingView Desktop extracted from MSIX to `C:\TradingView\` — **MSIX sandbox blocks debug port**, must use standalone exe.
- Launch: `"C:\TradingView\TradingView.exe" --remote-debugging-port=9222`. CDP at `http://localhost:9222`.
- MCP config in `~/.claude/.mcp.json` → `C:/Users/samet/Desktop/tradingview-mcp/src/server.js`.

**Key TV CLI** (binary `tv`):
```bash
tv status                              # Symbol, TF, indicators
tv data tables --filter "SMT Signals"  # Read overlay table
tv data tables --filter "SMT Oscillator"
tv data labels/boxes/lines --filter --verbose
tv pine set < script.pine              # Load Pine
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

`~/.claude/.mcp.json`:
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

**Required OKX account mode** (non-obvious — bot won't place a single order without these):
1. Demo Trading → Settings → **Account mode = "Futures"** (Single-currency margin, `acctLv=2`). Default "Simple" (`acctLv=1`) forces `net_mode` and rejects every call with `Parameter posSide error`.
2. **Position mode = "Hedge" (Long/Short)** → enables `posMode=long_short_mode`.
3. Verify via `get_account_config()`: `acctLv=2`, `posMode=long_short_mode`.
4. Demo balance reset is UI-only (no API endpoint); rotating keys doesn't reset.

**Demo API key:** Read+Trade only, never withdrawal. Demo keys completely separate from live.

**OKX instrument naming:** Perp = `BTC-USDT-SWAP`, Spot = `BTC-USDT`. TV ticker for OKX perp = `OKX:BTCUSDT.P`. See `okx_to_tv_symbol()` / `binance_to_okx_symbol()` in `src/data/`.

## Pine Scripts

Two production indicators (combining 6 standalone scripts + VuManChu Cipher A/B):

| Script | File | Purpose |
|---|---|---|
| SMT Master Overlay | `pine/smt_overlay.pine` | MSS/BOS + FVG/OB + liquidity/sweeps + sessions + PDH/PDL + VMC Cipher A → 20-row "SMT Signals" table |
| SMT Master Oscillator | `pine/smt_oscillator.pine` | VMC Cipher B: WaveTrend + RSI + MFI + Stoch RSI + divergences → 15-row "SMT Oscillator" table |

Pine is primary source of truth; `src/data/structured_reader.py` parses tables + drawings into `MarketState`. Python supplements (re-derive OB/FVG from OHLCV when needed).

Legacy single-purpose scripts under `pine/legacy/` (not loaded).

**OB sub-module** follows @Nephew_Sam_'s opensource Orderblocks pattern (MPL 2.0): persisted fractals, cut when later bar trades through; optional 3-bar FVG proximity filter + immediate wick-mitigation delete.

## Deferred / performance TODO

- **Overlay Pine split (~1200 lines → 2 parts)** — symbol-switch settle (~3-5s) is dominant multi-pair cycle cost. Split into `_structure.pine` + `_levels.pine` could parallelize TV recompute. Low priority — tackle if freshness-poll latency becomes problematic.

## Architecture (code layout)

All modules have module + class docstrings; use those for detail.

- `src/data/` — TV bridge + `MarketState` assembly, candle buffers, Binance liq WS, Coinalyze REST, economic calendar (Finnhub + FairEconomy).
- `src/analysis/` — Price action, market structure (MSS/BOS/CHoCH), FVG, OB, liquidity, ATR-scaled S/R, multi-timeframe confluence, liquidity heatmap, derivatives regime.
- `src/strategy/` — R:R math (`rr_system.py`), SL selection hierarchy, entry orchestration, risk manager (circuit breakers).
- `src/execution/` — python-okx wrapper (sync → `asyncio.to_thread`), order router, REST-poll position monitor, typed errors.
- `src/journal/` — async SQLite (`aiosqlite`), trade records, pure-function reporter (win_rate by session/factor, regime_breakdown, Sharpe-R, equity curve).
- `src/bot/` — YAML/env config, cross-platform shutdown, async outer loop (`BotRunner.run_once`), CLI entry.

## Phase status

Phases 1–6 + 6.5 + 1.5 + live-demo hardening + macro-blackout + fee-aware sizing all complete. `git log` documents per-feature evolution. Current: **~479 tests**, demo-runnable end-to-end.

| Phase | Status |
|---|---|
| 1. Pine + Data Bridge | ✅ |
| 2. Analysis Engine | ✅ |
| 3. Strategy Engine (R:R) | ✅ |
| 4. Execution (OKX) | ✅ |
| 5. Trade Journal | ✅ |
| 6. Bot runtime loop | ✅ |
| 6.5 Multi-pair + Multi-TF + Smart Entry/Exit | ✅ |
| 1.5 Derivatives Data Layer | ✅ |
| Live-demo hardening (cross margin, per-slot sizing, per-symbol spec) | ✅ |
| Macro Event Blackout (Finnhub + FairEconomy, ±30/15-min USD HIGH) | ✅ |
| Fee-aware sizing + TP1/TP2 guarantee + fee-buffered BE | ✅ |
| 6.9 Pre-RL baseline (orphan factors + per-symbol overrides + VWAP veto) | ✅ |
| 7. RL parameter tuner | 🔜 Next |

---

## Non-obvious design notes

Gotchas and rationales not self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing & margin

- **`_MARGIN_SAFETY = 0.95` + `_LIQ_SAFETY_FACTOR = 0.6`** (`src/strategy/rr_system.py`). Reserve 5% free margin for OKX fees/mark drift (else `sCode 51008`). Leverage additionally capped at `floor(0.6 / sl_pct)` so SL sits well inside liq distance — without this, tight-SL trades at 75x liquidate before SL fires.
- **Risk-budget vs margin-fit split.** `calculate_trade_plan(..., margin_balance=…)`: R comes off **total equity**, leverage/notional sized against **per-slot free margin** = `total_eq / max_concurrent_positions`. Cross-margin pools margin across open positions. Log emits `risk_bal=` and `margin_bal=` separately — different numbers by design.
- **Per-symbol instrument spec.** OKX `ctVal` differs per contract (BTC=0.01, ETH=0.1, **SOL=1**) and so does `maxLever` (BTC/ETH=100x, **SOL=50x**). `OKXClient.get_instrument_spec` populates `BotContext.contract_sizes` + `max_leverage_per_symbol` at prime. Hardcoded YAML would 100× over-size SOL.
- **`trading.symbol_leverage_caps`** — operator layer on top of OKX's cap. Demo flash-down wicks blow ≥30x on ETH even when structure holds; YAML caps ETH/DOGE/XRP conservatively (30x); BTC keeps 75x; SOL inherits OKX 50x.
- **Fee-aware sizing** (`fee_reserve_pct`, YAML `0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct`, so stop-out caps near $R *after* entry+exit taker fees. TP price unchanged — fee compensation flows through size, not by widening TP. `risk_amount_usdt` stays gross for RL reward comparability. Set `0.0` to revert.
- **SL widening, not rejection.** `min_sl_distance_pct` floor (YAML `0.005`): if Pine OB/FVG gives a 0.1% stop, widen to 0.5% rather than reject. Notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant — just smaller position, more breathing room. Initial reject-version blocked 264/300 BTC+ETH signals over 5h.
- **`min_tp_distance_pct`** (YAML `0.004`): reject `tp_too_tight` when TP is within ~2× round-trip taker. Evaluated after `_apply_htf_tp_ceiling`.

### Execution flow

- **Partial TP split guarantee.** If `int(num_contracts * partial_tp_ratio) == 0` or the remainder is 0, plan is rejected with `insufficient_contracts_for_split`. `OrderRouter._place_algos` **raises** instead of silent-fallback to single OCO — bypassed gate fails loud. Risk discipline over trade count.
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct`, YAML `0.001`). After TP1 fill, replacement OCO's SL sits a hair *past* entry on the profit side, so a touch-back to near-entry still covers remaining leg's exit taker fee + slippage. Rationale: ETH observed case — gross `+$95.91` / fees `-$100.20` / realized `-$4.29` on a price-wise winner.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. SL-to-BE callback uses `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)` (captured at `BotRunner.run` startup). `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Enrichment is non-optional.** `PositionMonitor._close_fill_from` only knows the position disappeared (`pnl_usdt=0, exit_price=0`). `OKXClient.enrich_close_fill` queries `/account/positions-history` for real `realizedPnl`, `closeAvgPx`, `fee`, `uTime`. Without it every close looks BREAKEVEN and drawdown/streak breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.
- **Demo guard.** `OKXClient` refuses `demo_flag != "1"` unless `allow_live=True` is explicitly passed. Margin-fail codes `{51008, 51020, 51200, 51201}` map to `InsufficientMargin`; other sCode → `OrderRejected`.

### Multi-pair + multi-TF

- **Pine freshness poll.** `SignalTableData.last_bar` is the freshness beacon; `_wait_for_pine_settle` polls until it flips post-TF-switch. First-read `None` falls through for test fakes; stale → skip cycle.
- **Post-settle grace** (`pine_post_settle_grace_s=1.0`). The Oscillator table lags the Signal table by a beat on 1m TF — freshness poll passes "early" while oscillator is still computing. Grace sleep lets the rest catch up.
- **HTF skip for already-open symbols.** HTF S/R cache only feeds the entry planner (SL push past zones + TP ceiling). If symbol already has an open position, dedup blocks re-entry anyway → skip the entire 15m pass (~5-15s per held position per cycle). Defensive close (LTF reversal) reads LTF state only, so stale HTF cache is safe. **Cycle-visit-count gotcha:** a pair that just had its position opened will show 5 TF visits (15m/1m/3m in the opening cycle + 1m/3m in the next) — that's 2 cycles, not 1. Single cycle is always ≤3 switches; HTF skip is only observable on the *next* cycle. A 15m switch on an open-position cycle indicates `open_trade_ids` is empty (journal write failure → `journal_write_failed_live_position_orphaned`, or `orphan_live_position_no_journal_row` on reconcile).
- **`bars_ago=0` is legitimate "just now".** Use `int(x) if x is not None else 99`, not `int(x or 99)` — the latter silently clobbers the freshest signal.
- **LTF reversal defensive close.** Cancels every tracked algo_id + market-closes when LTF trend/signal contradict open side within `max_age`. Gated by `ltf_reversal_min_bars_in_position` (minimum hold time) and idempotent via `defensive_close_in_flight`. `_defensive_close` reads `ctx.config.execution.margin_mode` — hardcoding `isolated` there is a latent bug.

### Data quality

- **Pine table-cell precision (`"#.########"`, not `"#.##"`).** `smt_overlay.pine` writes `atr_14`, `price` and `vwap_*m` into the Signal table with `str.tostring(val, "#.########")`. `"#.##"` (2 decimals) truncates DOGE ATR (~0.0008) and XRP ATR (~0.005) to `"0"`, which `structured_reader` parses as 0.0, which makes `select_sl_price` short-circuit on `atr <= 0` and return `no_sl_source` every cycle — even when OB/FVG factors are live. `#` is an optional digit in Pine, so BTC 60000 still renders as `"60000"`; the wide format is safe for all scales. OB/FVG zone coordinates are unaffected (parsed from `box.new()` floats, not tooltip strings).
- **Country→currency normalization in economic calendar.** Finnhub returns ISO-3166 alpha-2 (`"US"`, `"GB"`); FairEconomy returns currency codes (`"USD"`, `"GBP"`). Without normalization, `currencies: ["USD"]` filter silently drops every Finnhub event. `_country_to_currency()` normalizes at parse time; 3-char codes pass through idempotently.
- **FairEconomy thisweek + nextweek.** Both fetched in parallel via `asyncio.gather`. **404 on nextweek.json is normal** (file published mid-week) → demoted to DEBUG log. Without nextweek the bot is blind to next-Mon/Tue events when run late in the week.
- **Blackout decision point is BEFORE TV settle.** `is_in_blackout(now)` runs before symbol/TF switch — saves ~46s of settle per blacked-out symbol. Open positions untouched; OCO algos manage exit.
- **Derivatives failure isolation.** WS disconnect / 401 / 429 / cache crash → logs warn, leaves `state.derivatives=None` / `state.liquidity_heatmap=None`. Strategy degrades to pure price-structure. Missing `COINALYZE_API_KEY` or `FINNHUB_API_KEY` silently falls through (warn once at construction).
- **Binance liq WS caveat.** Rate-limited to the *largest* liquidation per 1s window per symbol. Coinalyze history fills the gap.

### Risk & state

- **Risk manager replay.** `journal.replay_for_risk_manager(mgr)` rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from closed trades on startup — durable truth over in-memory state. Drawdown breaker is **permanent halt** (manual restart required).
- **Orphan reconcile is log-only.** `_reconcile_orphans` diffs live OKX positions vs journal OPEN and logs mismatches; operator decides. Restart-while-live verified end-to-end (OCO algos on OKX keep SL/TP enforcement across bot restart; `_rehydrate_open_positions` reloads monitor state).
- **SL-to-BE survives restart.** `trades.sl_moved_to_be` is stamped by `journal.update_algo_ids` when the monitor replaces TP2 with the BE OCO. On restart, `_rehydrate_open_positions` forwards it as `be_already_moved=True` to `PositionMonitor.register_open`, so `_detect_tp1_and_move_sl` short-circuits and does NOT re-cancel + re-place the remainder's already-BE'd OCO. Without this, every restart after TP1 would double-move the SL (or worse, cancel the live BE algo and fail to re-place).
- **Reentry gate** (four sequential, first-fail-wins, per `(symbol, side)`):
  1. Cooldown `min_bars_after_close * tf_seconds(entry_tf)`
  2. ATR move `|price - last.price| / atr >= min_atr_move`
  3. Post-WIN quality: `proposed_confluence ≤ last.confluence` **blocks**
  4. Post-LOSS quality: `proposed_confluence < last.confluence` blocks (`=` passes)
  BREAKEVEN bypasses the quality gate. Opposite sides are isolated.

### Confluence

- **Pine is primary source of truth; Python supplements.** OB/FVG factors accept Pine-derived or Python-recomputed zones. S/R is ATR-scaled.
- **Sweep → reversal.** Bearish sweep (swept highs) ⇒ **BULLISH** factor, not bearish — the weak hands got flushed.
- **`ltf_momentum_alignment`.** LTF trend match = 0.5 weight; last_signal fresh-but-counter-trend (`bars_ago ≤ 3`) agreeing with direction = 60% partial weight.
- **Derivatives slot — at most one of three fires per cycle** (single elif chain): `derivatives_contrarian` (0.7) | `derivatives_capitulation` (0.6) | `derivatives_heatmap_target` (0.5). `_heatmap_supports_direction` requires nearest cluster within `ATR*3` AND notional ≥ 70% of largest.
- **`crowded_skip` gate.** Rejects entries aligned with crowded regime when `|funding_z| ≥ crowded_skip_z_threshold` (YAML `3.0`). Missing data never blocks — only trips with evidence.
- **Phase 6.9 orphan-field activators (3 factors).** Pine emits MFI bias, standing liquidity pools, and Cipher-B gold/divergence flags; pre-6.9 confluence ignored them. Now: `money_flow_alignment` (0.6, requires `|rsi_mfi| ≥ min_rsi_mfi_magnitude`), `liquidity_pool_target` (0.5, nearest Pine pool within `ATR × liquidity_pool_max_atr_dist`), `oscillator_high_conviction_signal` (1.25, fires on `GOLD_BUY` / `BUY_DIV` / `SELL_DIV` with `bars_ago ≤ 3`, mutually exclusive with regular `oscillator_signal` via elif chain).
- **VWAP hard veto (`analysis.vwap_hard_veto_enabled`, default off).** Strict: rejects bullish when price is below **every** available session VWAP (1m/3m/15m), bearish when above every. Missing (zero) VWAPs are skipped; all-missing is fail-open. Reject reason `vwap_misaligned`, emitted before SL/TP math. Operator-enabled after Sprint 3 demo validation per plan.
- **Per-symbol overrides.** `trading.swing_lookback_per_symbol` (B3: DOGE/XRP=30 fights `no_sl_source` on thin 3m books), `analysis.htf_sr_buffer_atr_per_symbol` (B2: SOL=0.10 vs global 0.20 — wide-ATR pairs over-clip HTF TP ceiling), `analysis.session_filter_per_symbol` (B4: SOL/DOGE/XRP=[london] after 0/6 NY+ASIAN). BotConfig resolvers fall back to globals when symbol isn't listed.

---

## Sprint 3 baseline run — active

**Started 2026-04-17T23:50Z** with $5k demo balance, $50 R (1%), 4 concurrent-slot cap, cross margin. All Phase 6.9 changes (BLOK A factors, BLOK B per-symbol overrides, min_confluence=3.0, A4 VWAP veto off by default) are live. Pre-sprint snapshot preserved:

### Mid-sprint adjustments (2026-04-18T13Z, restart #2 with --clear-halt)

Sprint 3 first-restart burned 5 LOSS / 1 WIN / 2 open in 4h → `max_consecutive_losses=5` halt tripped at 06:56Z, bot froze for ~20h. During the halt 135 PLANNED signals were blocked. Root-cause: with only 6 closed trades the breaker is too tight to collect RL training volume.

**Two YAML levers loosened for data-gathering pass** (re-tighten post-RL):

1. **`circuit_breakers.max_consecutive_losses: 5 → 9999`** — effectively disabled. RL needs 50+ closed trades (wins AND losses) for walk-forward; halting on the first 5-loss streak starves the dataset. Drawdown breaker (25%) + daily-loss breaker (15%) still active as hard caps.
2. **`trading.min_rr_ratio: 2.0 → 1.5`** — `htf_tp_ceiling` was the dominant NO_TRADE reason (370 rejects, 47% on DOGE alone). Mechanism: post-plan TP pulled back to `htf_zone - 0.2×ATR`, if resulting RR < 2.0 → reject. DOGE/SOL/XRP clipped-TP RRs often land 1.6-1.9 on 15m HTF, which is already not a "major" TF — VWAP (1m/3m/15m) and session filters provide direction discipline. Lowering floor lets legitimate clipped setups through.

**Attribution caveat:** These changes alter Sprint 3's post-cutoff stats. If a clean 50-trade baseline is desired for RL, treat 2026-04-18T13Z as the *real* cutoff rather than 23:50Z. Candidate `rl.clean_since` bump is noted but not applied — if reporter output stays noisy, bump it.

- `data/trades.db.backup_2026-04-18_pre-sprint3` — full DB copy (32 closed trades + 4 OPEN pre-close).
- `logs/bot.log.pre-sprint3_2026-04-17` — pre-restart log, 1.4MB.
- 4 pre-restart OPEN rows flipped to `outcome=CANCELED` + `close_reason=manual_reset_pre_sprint3` in-place, so reporter never counts them.
- `rl.clean_since=2026-04-17T23:50:00Z` → reporter/RL only see post-restart rows. `scripts/report.py --ignore-clean-since` reads the full history.

**Gate for RL:** ≥50 closed trades post-cutoff AND net `pnl_r ≥ 0` before invoking `train_rl.py`. Until then, iterate YAML manually using reporter output. If baseline stays negative after 50, the next lever is the opt-in A4 VWAP hard veto (flip `analysis.vwap_hard_veto_enabled: true`) — BLOK B5 volatility-adaptive widening and BLOK C shadow timeframes come after that only if the veto alone doesn't fix WR.

## Post-Sprint 3 roadmap — BLOK D: liquidity-aware execution (Coinalyze deepening)

**Premise:** we pull 6 Coinalyze endpoints but funnel them into a single confluence slot (`derivatives_heatmap_target` @ 0.5 weight) + regime classification. Heatmap is not consulted for TP/SL placement and lookback windows are mismatched to our 3m scalp horizon (historical liq 48h, LS z-score 14d, funding z-score 30d). Short TFs need short liquidity context.

**Scope (evaluate after Sprint 3 baseline + A4 VWAP veto have each been given their 50-trade window):**

1. **Shorten liquidity-heatmap lookback** — `historical_lookback_ms` default 48h → expose per-TF knob, probably 12h for 3m entries. Old liq events far from current price are noise; clusters from 2 days ago rarely magnet intraday.
2. **Add short-window funding/LS z-scores alongside long ones** — keep 30d/14d for regime stability, add 6h/24h rolling z for "is *right now* crowded?" signal. Current `crowded_skip` using only 30d z misses short squeezes that build in hours.
3. **Liq-sweep reversal entry (user's primary ask)** — detect cascade in rolling window (Binance WS aggregated + Coinalyze `/liquidation-history` 1h as cross-check): if total liq notional in last 60-120s > threshold × symbol baseline AND price wicked ≥ 0.5× ATR through a heatmap cluster then reverted, emit a `liq_sweep_reversal` confluence factor (or standalone high-priority entry trigger) for the counter side. Caveat: Binance WS is "largest-per-1s-per-symbol" rate-limited — single events undercount cascades, so Coinalyze aggregated is mandatory for magnitude.
4. **Heatmap → TP ceiling (HTF-zone analog)** — `rr_system` should treat `nearest_big_liq_cluster ± buffer_atr` as a candidate ceiling alongside HTF S/R. Effective ceiling = min(HTF_zone, heatmap_cluster). Also the upside mirror: if proposed TP is *before* a big cluster and RR allows, extend to cluster (magnet target). Add `heatmap_tp_ceiling_enabled` YAML flag; start with large clusters only (notional ≥ X% of largest symbol-wide) to limit noise.
5. **Liq-cluster-proximity veto** — currently `derivatives_heatmap_target` rewards a cluster *in path*, which is ambiguous: cluster can be magnet OR wall. When distance < 0.5×ATR AND notional is massive, flip the sign — veto the entry instead of boosting confluence (`reject_reason=liq_cluster_too_close`). Borderline cases stay in confluence.
6. **Heatmap factor weight scaling** — today binary (0.5 fires/not). Scale by cluster notional (log-proportional to largest) so 500M cluster ≠ 50M cluster. Better RL feature signal.
7. **Journal feature columns (zero-risk, can do during Sprint 3 without polluting baseline if additive only)** — add `nearest_liq_cluster_above_notional`, `nearest_liq_cluster_below_notional`, `nearest_liq_cluster_distance_atr`, `liq_1h_imbalance_at_entry`, `funding_z_6h`, `funding_z_24h` to trade records. Doesn't change decisions, just enables post-hoc analysis: "do trades with close big clusters win more?" RL can then learn from these features.

**Ordering rule:** item 7 can go in mid-sprint if needed (it's purely additive metadata). Items 1-6 must wait until Sprint 3 and the A4 VWAP-veto window have each produced a read on baseline WR — adding them simultaneously destroys attribution.

## Phase 7 — Reinforcement learning (Next)

**Architecture:** parameter tuner, NOT raw decision maker. Rule-based strategy generates signals; RL tunes:
- `confluence_threshold` (2-5), `pattern_weights`, `min_rr_ratio` (1.5-5.0)
- `risk_pct` (0.005-0.02), `htf_required` (bool), `session_filter` (list)
- `volatility_scale` (0.5-2.0), `ob_vs_fvg_preference` (0.0-1.0)

**Reward** = `pnl_r + setup_penalty + dd_penalty + consistency_bonus`
- `setup_penalty = -3.0` if confluence < 2
- `dd_penalty = -2.0` if dd > 5%, `-1.0` if > 3%
- `consistency_bonus = min(sharpe_last10 * 0.5, 1.5)`

**Walk-forward:** train 1-N, validate N+1 to N+50, advance window. Never deploy params that didn't improve OOS. Retrain every 50 new trades OR weekly; min 50 trades before first training.

**Cycle:** `python scripts/train_rl.py --min-trades 50 --walk-forward`. Improved params → `config/strategies/active.yaml`.

**Pre-RL workflow (mandatory):**
1. **Filter dirty data.** `clean_since` cutoff (entry_timestamp after last meaningful policy change) so early API-test / pre-fix trades don't poison training. Old rows stay in DB for comparison; never delete.
2. **Read the reporter first.** `scripts/report.py --last 7d` shows `win_rate_by_session/factor`, `regime_breakdown`. Fix obvious losers manually in YAML — RL isn't for catching things you can already see.
3. **Hand-tune the baseline.** Baseline should be at least break-even on the clean window before RL touches it. RL is fine-tuning, not rescue surgery.
4. **Then RL.** Walk-forward only after baseline is positive on ≥50 clean trades.

**Mental model:** RL reads each trade's feature columns (`confluence_score`, `confluence_factors`, `session`, `regime_at_entry`, `funding_z_at_entry`, …) and pairs them with `pnl_r`. Gradient updates params so *trades that pass the filters* maximize average `pnl_r`. **It does NOT do root-cause analysis** — 16 losses from 5 different causes all look like "this feature combination = LOSS" to the optimizer. Steps 2-3 above cannot be skipped.

## Currency pair strategy

**5 OKX perps — BTC / ETH / SOL / DOGE / XRP.** Phase 1.5'te 5 → 3'e inilmişti (Coinalyze free-tier budget + dengeli RL dataset için). 2026-04-17'de DOGE + XRP eklendi — BTC/ETH/SOL genelde correlated, momentum-driven iki parite uncorrelated alpha ekler. Coinalyze free-tier budget hâlâ güvenli: 5 × 5 call / 60s = 25/40 min. `trading.symbols` tek kaynak; legacy single-`symbol` form `DeprecationWarning` ile yüklenir.

**`max_concurrent_positions=4`** (5 parite 4 slot için yarışır — her cycle 1 parite beklemede kalır, confluence gate daha iyi sinyal seçer; 4. pozisyon queue karakteri). `per_slot = total_eq / 4 ≈ $800` margin budget (cross margin mode ile shared pool). R hâlâ total_eq'nun %1'i sabit, sadece notional tavan %25 küçülür.

**Cycle timing (5 parite, 3m entry TF = 180s cycle):** typical ~125-155s (freshness-poll erken döner), worst ~247s (her TF switch max timeout'a giderse). Worst-case bazen oluşursa sadece o cycle skip olur, bir sonraki yakalar. DOGE + XRP 30x leverage cap'li (`symbol_leverage_caps`) ve SOL-sınıfı thin book sayılıp `$8M capitulation_liq_notional` override aldılar.

**Adding a 6th+ pair:** drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides` (smaller OI pools → smaller `capitulation_liq_notional`), watch first 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed` — illiquid pairs flunk freshness-poll more. 6 pair + 4 slot'ta Coinalyze 30/40 min, cycle typical ~150-180s → worst-case pressure başlar; `pine_settle_max_wait_s` düşürmek gerekebilir.

## Configuration

Full config in `config/default.yaml` (self-documenting with inline comments).

Top-level sections: `bot`, `trading` (symbols, TFs, risk, `symbol_leverage_caps`, `swing_lookback_per_symbol`, `fee_reserve_pct`), `circuit_breakers`, `analysis` (confluence, `min_tp_distance_pct`, `min_sl_distance_pct`, `htf_sr_*`, `htf_sr_buffer_atr_per_symbol`, `session_filter_per_symbol`, `vwap_hard_veto_enabled`, `min_rsi_mfi_magnitude`, `liquidity_pool_max_atr_dist`), `execution` (margin_mode, partial_tp_*, `sl_be_offset_pct`, ltf_reversal_*), `reentry`, `derivatives`, `economic_calendar`, `okx`, `rl` (`clean_since` cutoff).

`.env` keys: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons** (joined log family): `below_confluence`, `session_filter`, `no_sl_source`, `vwap_misaligned`, `crowded_skip`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`, `macro_event_blackout`. Sub-floor SL distances are **widened**, not rejected.

## Tech stack

**Python:** pydantic, pyyaml, python-dotenv, aiosqlite, httpx, **python-okx (0.4.x — not 5.x)**, websockets, pandas, numpy, ta, stable-baselines3, gymnasium, torch, loguru, rich.

**Node:** `tradingview-mcp`, `okx-trade-mcp` + `okx-trade-cli`.

## Workflow commands

```bash
.venv/Scripts/python.exe -m src.bot --config config/default.yaml               # Demo
.venv/Scripts/python.exe -m src.bot --config ... --dry-run --once              # Smoke test
.venv/Scripts/python.exe -m src.bot --config ... --max-closed-trades 50        # Auto-stop
.venv/Scripts/python.exe -m src.bot --derivatives-only --duration 600          # 10-min warmup, no orders
OKX_DEMO_FLAG=0 .venv/Scripts/python.exe -m src.bot --config ...               # Live (after demo proven)
.venv/Scripts/python.exe scripts/report.py --last 7d
.venv/Scripts/python.exe scripts/train_rl.py --min-trades 50 --walk-forward
.venv/Scripts/python.exe -m pytest tests/ -v
.venv/Scripts/python.exe scripts/logs.py [--decisions|--errors|--filter REGEX]
```

**Pine dev cycle** (via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix → `tv pine analyze` → `tv screenshot`.

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, can break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. Start `--profile demo`. Never enable withdrawal perms. Bind key to machine IP. Verify before live. Sub-account for live.

**Trading risks:** research project, not financial advice. Crypto futures = liquidation risk. Demo first; live with minimal capital. Check OKX TOS for automated trading.

**RL risks:** overfitting is #1 — always walk-forward. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL.
