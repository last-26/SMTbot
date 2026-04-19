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
- **HTF Order Block re-add (post-pivot)** — Pivot 2026-04-19 removed `at_order_block` because Pine 3m OBs showed 0% WR in Sprint 3 (vs 35.7% pre-sprint — regime-fragile). Re-introduce as 15m-sourced `at_order_block_htf` once zone-planner is stable; gate on factor-audit evidence that HTF OBs outperform current zone sources.
- **Pine overlay refactor (post-pivot, after Phase D)** — once 5-pillar factor stack is final, strip overlay to Pillar visuals only (drop OB rendering, unused tooltip fields, redundant session labels). Oscillator stays largely intact.

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
| **7.0 Strategy Pivot 2026-04-19** — zone-based entry + 5-pillar stack + cross-asset veto | 🔄 **Active** |
| 7.A Quick wins (per-symbol SL floor, factor demote, EMA veto, cross-asset snapshot) | 🔜 |
| 7.B Data layer (counter-factual rejects, factor audit, HTF cache, journal schema v2) | 🔜 |
| 7.C Zone-based entry refactor (`setup_planner.py`, limit orders, PENDING state) | 🔜 |
| 7.D Structural refinements (displacement, premium/discount, ADX trend-regime, Pine trim) | 🔜 |
| 8. Analytics (GBT) → optional RL | 🔜 |

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
- **SL-to-BE never spins.** `_detect_tp1_and_move_sl` splits cancel and place into separate try-blocks with three exits, so the observed 2026-04-18 BTC pathology (cancel succeeded, place failed, retry loop hammered an already-cancelled algo for hours leaving the runner unprotected) cannot recur: (a) OKX codes `{51400,51401,51402}` on cancel are treated as idempotent success and the BE OCO is still placed; (b) generic cancel failures increment `cancel_retry_count` and give up after `_CANCEL_MAX_RETRIES=3` attempts, flipping `be_already_moved=True` so poll stops hammering; (c) place failure after successful cancel marks the position unprotected (CRITICAL log, drop TP2 from `algo_ids`, fire `on_sl_moved` callback so journal reflects reality) — emergency market-close is deliberately NOT automated, operator decides. Path (c) is rare but catastrophic when it happens; paths (a)+(b) covered the actual production incident.
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

## Sprint 3 — archived (diagnostic) 2026-04-17 → 2026-04-19

**Started 2026-04-17T23:50Z, paused 2026-04-19 after pivot decision.** $5k demo, $50 R (1%), 4 slots, cross margin. Phase 6.9 stack (BLOK A + BLOK B overrides + `min_confluence=3.0`, VWAP veto off). Closed 14 trades, WR 28.6%, avg R -0.60, PnL -$456 (DB balance 10000→9544 includes pre-sprint carry).

**Why it was archived:** Sprint 3 + pre-sprint data together (46 closed trades) showed the strategy is **regime-fragile**, not mis-tuned. Core forensics:
1. **Every sprint 3 trade has `sl_pct == 0.500%`** — `entry_signals.py:558-564` `min_sl_distance_pct` floor hits every structural SL. Journal's `sl_source` label is nominal; real SL is uniform flat percent. ETH-volatility pairs get swept in 1-2 candles.
2. **Every entry `ordType="market"`** (`okx_client.py:214`). Zero zone-wait, zero limit orders. Bot is a momentum-chaser, not a scalper.
3. **14/14 sprint 3 trades in BALANCED regime** — existing `derivatives_regime` classifier not discriminating. RL has no regime signal to learn from.
4. **Factor WR regression: `at_order_block` 35.7%→0%, `htf_trend_alignment` 35.7%→0% between pre-sprint and sprint 3.** Same code, different market → factors are regime-fragile trend-continuation signals, not intrinsically broken. Deleting them is wrong fix; conditioning them on regime is the right fix.
5. **Zero cross-asset awareness** — each symbol in isolation. SOL/DOGE shorts closed in profit 2026-04-19 because BTC/ETH turned up while shorts stayed active. Correlation blind spot.

**Mid-sprint adjustments (now moot, context only):** `max_consecutive_losses 5→9999`, `max_daily_loss_pct 15→40`, `max_drawdown_pct 25→40`, `min_rr_ratio 2.0→1.5`. POST-PIVOT RESTORE to 5 / 15 / 25 / 2.0 after Phase 7.C demonstrates zone-entry doesn't over-trigger breakers.

**Archived artifacts:**
- `data/trades.db.sprint3_diagnostic_2026-04-19` — 46 closed trades (pre-sprint 32 + sprint 3 14), 5 OPEN rows converted to CANCELED with `close_reason=manual_close_pivot_2026-04-19`.
- `logs/bot.log.sprint3_final_2026-04-19` — pre-pivot log dump.
- `data/trades.db.backup_2026-04-18_pre-sprint3` — pre-existing checkpoint.
- `rl.clean_since` updated to `2026-04-19T<pivot-start>Z` after Phase 7.A ships.

**What pre-sprint + sprint 3 data taught us (inputs to pivot design):**
- 5-pillar factors — Market Structure, Liquidity, Money Flow, VWAP, Divergence — carry the real WR signal. `recent_sweep` 45% WR, pre-split `vwap_alignment` 75% WR (n=8, strong), `mss_alignment` 35% WR.
- LONDON session WR 45%, NEW_YORK 33%, OFF 17% — session discipline is validated. Per-symbol session filter (BLOK B4) was a correct instinct.
- Same-direction loss streaks (ETH 6× consecutive BULLISH LOSS on 2026-04-17) point directly at missing cross-asset / regime flip detection.
- Trend-continuation factors alone without regime gating = trap at move-end. Zone-based entry plus patience fixes the "enter at end of move" pattern.

## Strategy Pivot 2026-04-19 — zone-entry + 5-pillar + cross-asset veto

**Framing:** not "tear down and rewrite". The factor machinery, Pine pipeline, OKX execution layer, journal, and derivatives ingestion all stay. Pivot adds **three missing layers** and **demotes** (not deletes) the regime-fragile parts of the current stack.

### Layer 1 — 5-Pillar factor stack

| Pillar | Concrete factors | Role |
|---|---|---|
| **Market Structure** | `mss_alignment`, `recent_sweep` | Core score |
| **Liquidity** | Pine standing pools + Coinalyze heatmap clusters + sweeps | Core score + zone source |
| **Money Flow** | `money_flow_alignment` (MFI bias), oscillator MFI trend | Core score |
| **VWAP** | `vwap_composite` = all-3 TFs align → 0.6, 2-of-3 → 0.3, 1-of-3 → 0 | Core score (re-consolidated from split) |
| **Divergence** | Oscillator regular + hidden divergences, gold signals | Core score (absorbs `oscillator_signal` + `oscillator_high_conviction_signal`) |

**Demoted from scoring (become direction-picker / zone-source / deferred):**
- `htf_trend_alignment` → moved from scoring factor to **setup-planner direction input** (tells planner "this is a long setup" but does not add score points). Plus feeds regime classifier.
- `at_order_block` → **removed entirely for now** (Pine 3m OB = noise). Queued in Deferred as HTF-variant re-add.
- `liquidity_pool_target` → absorbed into zone source (no longer a stand-alone score contributor).
- `oscillator_signal` → absorbed into Divergence pillar as an ingredient, not a separate entry.

**New factors (Phase 7.D additions):**
- `displacement_candle` — large-body fast-move candle within last 3-5 bars, used as "real imbalance" gate on FVGs.
- `premium_discount_zone` — long setups need discount side (below last-swing midpoint), shorts need premium. Reject-on-mismatch, not scoring.

### Layer 2 — Zone-based entry (execution model overhaul)

**Current flow:** `confluence ≥ threshold → market order at current price`
**Pivot flow:** `confluence ≥ threshold → identify zone → limit order at zone edge → wait N bars → fill or cancel`

**New module** `src/strategy/setup_planner.py`:
```python
@dataclass
class ZoneSetup:
    direction: Direction
    entry_zone: tuple[float, float]     # e.g. FVG range, liq pool ± buffer
    trigger_type: Literal["zone_touch", "sweep_reversal", "displacement_return"]
    sl_beyond_zone: float                # structural, not % floor
    tp_primary: float                    # first liq target or HTF zone
    max_wait_bars: int
    zone_source: Literal["fvg_htf", "liq_pool", "vwap_retest", "sweep_retest"]
```

**Zone source priority (highest first):**
1. Unswept Coinalyze liq pool + premium/discount match (long at discount pool, short at premium pool)
2. HTF 15m unfilled FVG, price approaching from outside
3. Session VWAP re-test on pullback in the chosen direction
4. Recent-swing liquidity sweep-and-reversal setup (swept then closed back inside)

**Execution rules:**
- Entry: `limit` (post-only preferred for maker fee). Fallback to regular limit when post-only is rejected because price is on the wrong side.
- SL: beyond zone structure, not % floor. `min_sl_distance_pct` becomes emergency floor only (widen when structural zone is pathologically thin).
- TP: primary = liquidity target or HTF zone (not fixed-R). Runner dynamic (post-TP1 optional trail, see Phase 7.D revisit).
- Timeout: after `max_wait_bars` (default 10 bars = 30 min on 3m), cancel if unfilled — `reject_reason=zone_timeout_cancel`.
- Invalidation: zone violated without fill → immediate cancel.
- `max_concurrent_setups_per_symbol = 1`. Pending limit + live position cannot coexist on same symbol.

**Position monitor new state:** `PENDING` (limit placed, not filled). Transitions: `PENDING → FILLED → OPEN → CLOSED | PENDING → CANCELED`.

### Layer 3 — Cross-asset correlation layer

**New struct:** `CryptoSnapshot`
```python
@dataclass
class CryptoSnapshot:
    btc_15m_trend: Direction                # BULLISH / BEARISH / NEUTRAL
    eth_15m_trend: Direction
    btc_3m_momentum: float                   # last-5-bars % change
    eth_3m_momentum: float
    updated_at: datetime
```

**Lifecycle:** outer loop builds snapshot from BTC + ETH cycles (they run first in the symbol sequence), stores on `ctx.crypto_snapshot`. Altcoin cycles (`SOL`/`DOGE`/`XRP`) read it before entry.

**Veto rule (conservative v1):**
```
altcoin_symbol in {SOL, DOGE, XRP}:
  LONG  + btc_15m==BEARISH + eth_15m==BEARISH → reject("cross_asset_opposition")
  SHORT + btc_15m==BULLISH + eth_15m==BULLISH → reject("cross_asset_opposition")
```
Both pillars must oppose (sector rotation passes when only one opposes). Later tightening uses momentum delta thresholds.

**BTC/ETH themselves:** no cross-asset veto on the pillars (BTC ↔ ETH divergence info used as neutral context for either).

### Layer 4 — Regime-awareness (trend-strength axis)

Current `derivatives_regime` returns BALANCED 14/14 in sprint 3. Add **trend-strength axis** (Phase 7.D):
- ADX-like directional movement index over last N bars
- Classification: `RANGING` / `WEAK_TREND` / `STRONG_TREND`
- Persist as new journal column `trend_regime_at_entry`

**Conditional factor scoring** (Phase 7.D activation):
- Trend-continuation factors (HTF trend direction input, VWAP alignment in trend direction) gain weight only in `WEAK_TREND`/`STRONG_TREND`.
- Reversal factors (sweep-reversal, counter-trend zones) gain weight only in `RANGING` or regime-transition states.

### Per-symbol SL floor (Phase 7.A)

YAML `analysis.min_sl_distance_pct_per_symbol`:
```yaml
min_sl_distance_pct_per_symbol:
  BTC-USDT-SWAP: 0.005
  ETH-USDT-SWAP: 0.010       # 2× — higher beta
  SOL-USDT-SWAP: 0.008
  DOGE-USDT-SWAP: 0.007
  XRP-USDT-SWAP: 0.007
```
`BotConfig.resolve_min_sl_distance_pct(symbol)` — pattern matches existing `swing_lookback_per_symbol` resolver. Floor remains emergency-only once Phase 7.C structural SL binds.

### EMA 21/55 momentum veto (Phase 7.A)

Short-TF trap filter: long signals blocked when 21-EMA < 55-EMA and spread widening; shorts blocked in the mirror case. Reject reason `ema_momentum_contra`. Not a scoring factor (lagging indicator); pre-entry gate only.

### Phased plan

**Phase 7.A — Quick wins (1-2 days, zero refactor risk)**
- A1: Per-symbol `min_sl_distance_pct` resolver + YAML block.
- A2: Demote `at_order_block`, `oscillator_signal`, `liquidity_pool_target` to `weight=0` (keep functions for audit).
- A3: `htf_trend_alignment` → direction-picker only (no score contribution).
- A4: VWAP composite (`vwap_composite` replaces 3 independent `vwap_{1m,3m,15m}_alignment` contributions).
- A5: EMA 21/55 momentum veto.
- A6: `CryptoSnapshot` + altcoin cross-asset veto.
- A7: Reporter extensions (per-symbol WR, factor-combo top-10, confluence-score bucket, cluster-distance bucket).

**Phase 7.B — Data layer (3-5 days, additive, no decision change)**
- B1: `rejected_signals` table + INSERT on every reject path in `entry_signals.py`. Snapshot features at reject time.
- B2: `scripts/peg_rejected_outcomes.py` — N-bar-ahead hypothetical TP/SL outcome for each reject. Yields counter-factual validation set.
- B3: `scripts/factor_audit.py` — factor-combo cross-tab, per-symbol WR, score-bucket WR, regime split.
- B4: HTF 15m MarketState caching for altcoin cycles (OB/FVG source param plumbing for later).
- B5: Journal schema v2 — `funding_z_6h`, `funding_z_24h`, `trend_regime_at_entry`, `setup_zone_source`, `zone_wait_bars`, `zone_fill_latency_bars`.

**Phase 7.C — Zone-based entry (1-2 weeks)**
- C1: `src/strategy/setup_planner.py` (new module).
- C2: `OrderRouter.place_limit_entry` + `cancel_pending_entry`.
- C3: `PositionMonitor` `PENDING` state with timeout/invalidation logic.
- C4: `BotRunner` pending-setup lifecycle — creation, swap (better setup preempts worse), cancel, transition to OPEN on fill.
- C5: Integration tests — mock OKX, limit fill simulation, timeout cancel, invalidation, post-only rejection fallback.
- C6: Demo observation, 2+ days, fill-rate monitoring.

**Phase 7.D — Structural refinements (1 week)**
- D1: `displacement_candle` + `premium_discount_zone` factors.
- D2: Divergence factor formalization (hidden + regular, bar-ago decay).
- D3: ADX-based `trend_regime` classifier + conditional factor scoring.
- D4: Pine overlay trim (drop OB rendering + unused tooltip rows once Pillar stack is final).
- D5: Runner trail / post-TP1 exit re-evaluation (former BLOK E). Zone-based entries may hit TP2 more often; revisit necessity with data.

**Phase 8 — Analytics & optional RL (after 50+ clean post-pivot trades)**
- E1: `scripts/analyze.py` — GBT (xgboost) feature importance + partial dependence plots on clean trades.
- E2: Manual tune based on GBT output (per-symbol thresholds, factor weights, veto thresholds).
- E3: RL (stable-baselines3) **only if** GBT + manual hit a clear ceiling. Scope unchanged from legacy Phase 7 spec — parameter tuner, not decision maker.

### Gates

- **7.A → 7.B:** `pytest` green, smoke run clean, factor-reduced confluence logs consistent, cross-asset veto fires at least once in 4h demo without false positives on BTC/ETH themselves.
- **7.B → 7.C:** `rejected_signals` ≥ 200 rows, `factor_audit.py` produces stable per-symbol output, HTF cache hit rate ≥ 80%.
- **7.C → 7.D:** 20 closed trades via zone-entry, WR ≥ 40%, fill rate ≥ 50% (half of setups fill before timeout), no `PENDING` state bugs.
- **7.D → 8:** 50 closed post-pivot trades, WR ≥ 45%, avg R ≥ 0, ≥2 trend-regimes represented in data, net PnL non-negative.
- **8 (GBT) → maybe RL:** 100+ post-pivot trades, GBT + manual tuning plateau evident.

### Risks and mitigations

- **Zone fill rate too low** → bot idles, no data. Mitigation: start with generous `max_wait_bars=15`, tighten after observation; fallback-to-market option as emergency hatch during Phase 7.C bring-up.
- **Cross-asset veto too restrictive** → altcoins rarely trade. Mitigation: v1 requires BOTH BTC+ETH opposition; relax if altcoin trade volume drops >60%.
- **OKX post-only rejection** → price on wrong side at placement. Mitigation: integration test covers fallback to regular limit, then market-at-zone-edge as final fallback.
- **Phase 7.B schema change interacts with 7.C code** → counter-factual features may not match zone-entry context. Mitigation: keep schema flexible (nullable columns), regenerate hypothetical outcomes after 7.C ships.
- **Factor demote surprises** → pre-sprint data says demoted factors had 35% WR. If removal causes a flood of `below_confluence` rejects, consider keeping at weight=0.2 transitionally.

### Reject reasons post-pivot (extends legacy list)

Added: `cross_asset_opposition`, `ema_momentum_contra`, `zone_timeout_cancel`, `no_setup_zone`, `pending_invalidated`, `wrong_side_of_premium_discount`. Legacy list (`below_confluence`, `session_filter`, `no_sl_source`, `vwap_misaligned`, `crowded_skip`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`, `macro_event_blackout`) remains active.

## Currency pair strategy

**5 OKX perps — BTC / ETH / SOL / DOGE / XRP.** Phase 1.5'te 5 → 3'e inilmişti (Coinalyze free-tier budget + dengeli RL dataset için). 2026-04-17'de DOGE + XRP eklendi — BTC/ETH/SOL genelde correlated, momentum-driven iki parite uncorrelated alpha ekler. Coinalyze free-tier budget hâlâ güvenli: 5 × 5 call / 60s = 25/40 min. `trading.symbols` tek kaynak; legacy single-`symbol` form `DeprecationWarning` ile yüklenir.

**Pivot 2026-04-19 — BTC/ETH pillar role.** BTC ve ETH "piyasa direği" olarak ele alınıyor; SOL/DOGE/XRP altcoin olarak bunların rejim değişimlerine tabi. Sprint 3'te SOL/DOGE short'larının BTC/ETH yukarı dönüşünde squeeze yemesi bu kör noktanın kanıtıydı. `CryptoSnapshot` (Layer 3) altcoin cycle'larında zorunlu veto input'u. BTC/ETH arası ayrı bir korelasyon kontrolü yok; biri rip ederken diğeri flat ise altcoin için nötr context olarak okunur.

**`max_concurrent_positions=4`** (5 parite 4 slot için yarışır — her cycle 1 parite beklemede kalır, confluence gate daha iyi sinyal seçer; 4. pozisyon queue karakteri). `per_slot = total_eq / 4 ≈ $800` margin budget (cross margin mode ile shared pool). R hâlâ total_eq'nun %1'i sabit, sadece notional tavan %25 küçülür.

**Cycle timing (5 parite, 3m entry TF = 180s cycle):** typical ~125-155s (freshness-poll erken döner), worst ~247s (her TF switch max timeout'a giderse). Worst-case bazen oluşursa sadece o cycle skip olur, bir sonraki yakalar. DOGE + XRP 30x leverage cap'li (`symbol_leverage_caps`) ve SOL-sınıfı thin book sayılıp `$8M capitulation_liq_notional` override aldılar.

**Adding a 6th+ pair:** drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides` (smaller OI pools → smaller `capitulation_liq_notional`), watch first 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed` — illiquid pairs flunk freshness-poll more. 6 pair + 4 slot'ta Coinalyze 30/40 min, cycle typical ~150-180s → worst-case pressure başlar; `pine_settle_max_wait_s` düşürmek gerekebilir.

## Configuration

Full config in `config/default.yaml` (self-documenting with inline comments).

Top-level sections: `bot`, `trading` (symbols, TFs, risk, `symbol_leverage_caps`, `swing_lookback_per_symbol`, `fee_reserve_pct`), `circuit_breakers`, `analysis` (confluence, `min_tp_distance_pct`, `min_sl_distance_pct`, `htf_sr_*`, `htf_sr_buffer_atr_per_symbol`, `session_filter_per_symbol`, `vwap_hard_veto_enabled`, `min_rsi_mfi_magnitude`, `liquidity_pool_max_atr_dist`), `execution` (margin_mode, partial_tp_*, `sl_be_offset_pct`, ltf_reversal_*), `reentry`, `derivatives`, `economic_calendar`, `okx`, `rl` (`clean_since` cutoff).

`.env` keys: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons** (joined log family, legacy): `below_confluence`, `session_filter`, `no_sl_source`, `vwap_misaligned`, `crowded_skip`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`, `macro_event_blackout`. Sub-floor SL distances are **widened**, not rejected.

**Pivot-era additions** (Phase 7.A+): `cross_asset_opposition`, `ema_momentum_contra`, `zone_timeout_cancel`, `no_setup_zone`, `pending_invalidated`, `wrong_side_of_premium_discount`.

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
