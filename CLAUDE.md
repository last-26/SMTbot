# CLAUDE.md — Crypto Futures Trading Bot

## Overview

AI-powered crypto futures bot: two MCP bridges + Python core.

- **TradingView MCP** — chart data, indicator values, Pine Script dev cycle
- **OKX Agent Trade Kit MCP** — order execution on OKX (demo first, live later)
- **Python Bot Core** — autonomous loop: data → analysis → strategy (R:R) → zone-based execution → journal → (future) RL retraining

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
| SMT Master Overlay | `pine/smt_overlay.pine` | MSS/BOS + FVG/OB + liquidity/sweeps + sessions + PDH/PDL + VMC Cipher A → **19-row** "SMT Signals" table (post-D4 trim: `confluenceScore` block + `active_ob` row removed; OB/FVG box rendering preserved for SL fallback) |
| SMT Master Oscillator | `pine/smt_oscillator.pine` | VMC Cipher B: WaveTrend + RSI + MFI + Stoch RSI + divergences → 15-row "SMT Oscillator" table |

Pine is primary source of truth for **structure** (MSS, OB/FVG zones, liquidity pools, sweeps, VWAPs). Confluence *scoring* is Python-side (`multi_timeframe.score_direction`). `src/data/structured_reader.py` parses tables + drawings into `MarketState`.

Legacy single-purpose scripts under `pine/legacy/` (not loaded).

**OB sub-module** follows @Nephew_Sam_'s opensource Orderblocks pattern (MPL 2.0): persisted fractals, cut when later bar trades through; optional 3-bar FVG proximity filter + immediate wick-mitigation delete.

## Deferred / performance TODO

- **Overlay Pine split (~1200 lines → 2 parts)** — symbol-switch settle (~3-5s) is dominant multi-pair cycle cost. Split into `_structure.pine` + `_levels.pine` could parallelize TV recompute. Low priority — tackle if freshness-poll latency becomes problematic.
- **HTF Order Block re-add (post-pivot)** — Pivot 2026-04-19 removed `at_order_block` because Pine 3m OBs showed 0% WR in Sprint 3 (vs 35.7% pre-sprint — regime-fragile). Re-introduce as 15m-sourced `at_order_block_htf` once factor-audit evidence shows HTF OBs outperform current zone sources.
- **Pine overlay full rendering strip (post-Phase-8 data)** — Phase 7.D4 removed the unread `confluenceScore` block and the `active_ob` table row (factor weighted 0). Full OB/FVG box-rendering removal is deferred until the factor-audit (Phase 8) shows Python-side OB/FVG fallbacks handle SL selection without widening drawdowns. Oscillator stays largely intact.
- **Circuit-breaker restore (post-observation)** — Sprint-3 loosened values still in YAML (`max_daily_loss_pct=40`, `max_consecutive_losses=9999`, `max_drawdown_pct=40`, `min_rr_ratio=1.5`). Restore to **5 / 15 / 25 / 2.0** once 20+ post-pivot closed trades confirm zone-entry doesn't over-trigger breakers.

## Architecture (code layout)

All modules have module + class docstrings; use those for detail.

- `src/data/` — TV bridge + `MarketState` assembly, candle buffers, Binance liq WS, Coinalyze REST, economic calendar (Finnhub + FairEconomy), HTF cache.
- `src/analysis/` — Price action, market structure (MSS/BOS/CHoCH), FVG, OB, liquidity, ATR-scaled S/R, multi-timeframe confluence (5-pillar scoring + regime-conditional weights), liquidity heatmap, derivatives regime, **ADX trend regime** (`trend_regime.py`), **EMA momentum veto**, **displacement/premium-discount** gates.
- `src/strategy/` — R:R math (`rr_system.py`), SL selection hierarchy, entry orchestration (`entry_signals.py`), **setup planner** (`setup_planner.py` — zone-based limit-order plans), cross-asset snapshot veto, risk manager (circuit breakers).
- `src/execution/` — python-okx wrapper (sync → `asyncio.to_thread`), order router (`place_limit_entry` + `cancel_pending_entry` + market fallback), REST-poll position monitor with **PENDING** state, typed errors.
- `src/journal/` — async SQLite (`aiosqlite`), trade records (**schema v2** — funding Z-scores, trend regime at entry, zone source, wait/fill latency bars), **rejected_signals** table + counter-factual outcome stamps, pure-function reporter (win_rate by session/factor/regime/score-bucket, per-symbol WR, factor-combo top-N, equity curve).
- `src/bot/` — YAML/env config, cross-platform shutdown, async outer loop (`BotRunner.run_once` with crypto-snapshot lifecycle), CLI entry.

## Phase status

Phases 1–6.9 all complete. Post-pivot code (7.A→7.D4) landed 2026-04-19. Current: **~667 tests**, demo-runnable end-to-end. Next operational milestone is **Phase 8 data collection** — accumulate ≥50 clean post-pivot closed trades before GBT analysis / optional RL.

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
| **7.0 Strategy Pivot 2026-04-19** — zone-entry + 5-pillar + cross-asset + regime | ✅ |
| 7.A Quick wins (per-symbol SL floor, factor demote, EMA veto, cross-asset snapshot) | ✅ |
| 7.B Data layer (rejected_signals, counter-factuals, factor_audit, HTF cache, schema v2) | ✅ |
| 7.C Zone-based entry refactor (`setup_planner.py`, limit orders, PENDING state) | ✅ |
| 7.D1 Displacement + premium/discount gates | ✅ |
| 7.D2 Divergence factor formalization (hidden + regular, bar-ago decay) | ✅ |
| 7.D3 ADX trend regime + conditional scoring | ✅ |
| 7.D4 Pine overlay trim (confluenceScore + active_ob removed) | ✅ |
| 7.D5 Runner trail / post-TP1 exit re-evaluation | ⏸ **Deferred** — revisit after 50+ post-pivot closed trades |
| **8. Data collection → GBT → optional RL** | 🔄 **Active** — accumulating clean post-pivot trades |

---

## Current strategy architecture (post-pivot 2026-04-19)

The pivot replaced a market-order confluence-only stack with a patient zone-based stack. The factor machinery, Pine pipeline, OKX execution layer, journal, and derivatives ingestion all stayed; these four layers were **added** on top.

### Layer 1 — 5-pillar factor stack

| Pillar | Concrete factors | Role |
|---|---|---|
| **Market Structure** | `mss_alignment`, `recent_sweep` | Core score |
| **Liquidity** | Pine standing pools + Coinalyze heatmap clusters + sweep-reversal | Core score + zone source |
| **Money Flow** | `money_flow_alignment` (MFI bias) | Core score |
| **VWAP** | `vwap_composite` — all-3-TF align → 0.6, 2-of-3 → 0.3, 1-of-3 → 0 | Core score (consolidated from old 3 splits) |
| **Divergence** | Oscillator regular + hidden divergences, gold signals, bar-ago decay | Core score (absorbs `oscillator_signal` + high-conviction gold) |

**Demoted** (kept for audit, not scoring):
- `at_order_block` → weight=0 (Pine 3m OBs proved noise). Queued for HTF-variant re-add.
- `oscillator_signal`, `liquidity_pool_target` → absorbed into Divergence / zone source.
- `htf_trend_alignment` → **direction input only** for setup planner; does not add score points. Also feeds ADX regime path.

**Hard gates** (reject-on-mismatch, not scoring):
- `premium_discount_zone` — longs must sit in discount half, shorts in premium half (relative to last-swing midpoint).
- `displacement_candle` — FVG-based zones require a fresh large-body displacement within last 3-5 bars as "real imbalance" proof.
- `ema_momentum_contra` — longs blocked when 21-EMA<55-EMA and spread widening; shorts mirror.
- `vwap_misaligned` — strict hard veto when price opposes all available session VWAPs (1m/3m/15m).
- `cross_asset_opposition` — see Layer 3.

### Layer 2 — Zone-based entry (execution model)

**Flow:** `confluence ≥ threshold → identify zone → limit order at zone edge → wait N bars → fill | cancel`.

`src/strategy/setup_planner.py` picks the best `ZoneSetup`:
```python
@dataclass
class ZoneSetup:
    direction: Direction
    entry_zone: tuple[float, float]
    trigger_type: Literal["zone_touch", "sweep_reversal", "displacement_return"]
    sl_beyond_zone: float                # structural, not % floor
    tp_primary: float                    # first liq target or HTF zone
    max_wait_bars: int
    zone_source: Literal["fvg_htf", "liq_pool", "vwap_retest", "sweep_retest"]
```

**Zone source priority** (highest first):
1. Unswept Coinalyze liq pool + premium/discount match
2. HTF 15m unfilled FVG approached from outside
3. Session VWAP re-test on pullback in the chosen direction
4. Recent-swing liquidity sweep-and-reversal (swept then closed back inside)

**Execution rules:**
- Entry: `limit` (post-only preferred for maker fee). Fallback to regular limit on post-only reject; final fallback = market-at-zone-edge.
- SL: beyond zone structure. `min_sl_distance_pct` becomes emergency floor only — widens, never rejects (pathologically thin zones survive via notional shrink).
- TP: primary = liquidity target or HTF zone (not fixed-R).
- Timeout: `max_wait_bars` unfilled → cancel, `reject_reason=zone_timeout_cancel`.
- Invalidation: zone violated without fill → immediate cancel, `reject_reason=pending_invalidated`.
- `max_concurrent_setups_per_symbol=1`. Pending limit + live position cannot coexist per symbol.

**Position monitor states:** `PENDING → FILLED → OPEN → CLOSED | PENDING → CANCELED`. Monitor tracks each transition; journal stamps `zone_wait_bars`, `zone_fill_latency_bars`, `setup_zone_source`.

### Layer 3 — Cross-asset correlation

`CryptoSnapshot` struct built each outer-loop tick from BTC + ETH cycles (they run first in the symbol sequence):
```python
@dataclass
class CryptoSnapshot:
    btc_15m_trend: Direction    # BULLISH / BEARISH / NEUTRAL
    eth_15m_trend: Direction
    btc_3m_momentum: float      # last-5-bars % change
    eth_3m_momentum: float
    updated_at: datetime
```

**Altcoin veto rule** (`SOL` / `DOGE` / `XRP`):
- `LONG + btc_15m==BEARISH + eth_15m==BEARISH` → `reject("cross_asset_opposition")`
- `SHORT + btc_15m==BULLISH + eth_15m==BULLISH` → `reject("cross_asset_opposition")`

Both pillars must oppose (sector rotation passes when only one opposes). **BTC/ETH themselves:** no veto — their divergence feeds context for either.

### Layer 4 — Regime awareness (ADX trend strength)

`src/analysis/trend_regime.py` classifies each candle buffer:
- `compute_adx` — Wilder smoothing, 14-period default.
- `classify_trend_regime` → `UNKNOWN / RANGING / WEAK_TREND / STRONG_TREND` with `DEFAULT_RANGING_THRESHOLD=20.0` and `DEFAULT_STRONG_THRESHOLD=30.0`.
- Persisted as journal `trend_regime_at_entry`.

**Conditional factor scoring** (`multi_timeframe._apply_trend_regime_conditional`, opt-in `trend_regime_conditional_scoring_enabled`):
- `STRONG_TREND` → `htf_trend_alignment × 1.5`, `recent_sweep × 0.5` (trend continuation preferred over reversal).
- `RANGING` → `htf_trend_alignment × 0.5`, `recent_sweep × 1.5` (reversal/sweep setups preferred).
- `WEAK_TREND` / `UNKNOWN` → unchanged.

### Data + RL prep

- **`rejected_signals` table** — every reject path in `entry_signals.py` now INSERTs a row with snapshot features at reject time. `scripts/peg_rejected_outcomes.py` walks OKX history forward from each reject's `signal_timestamp` and stamps `hypothetical_outcome` (WIN/LOSS/NEITHER).
- **`scripts/factor_audit.py`** — per-symbol / session / derivatives-regime / ADX-regime / score-bucket WR, top-N factor combos, per-factor actual-vs-hypothetical WR, reject-reason frequency with counter-factual WR. Honours `rl.clean_since`.
- **Journal schema v2** — `funding_z_6h`, `funding_z_24h`, `trend_regime_at_entry`, `setup_zone_source`, `zone_wait_bars`, `zone_fill_latency_bars`. Nullable columns; existing rows migrate without data loss.
- **HTF 15m MarketState cache** — altcoin cycles read cached HTF state rather than re-querying TV per symbol.

### Per-symbol SL floor (Phase 7.A)

YAML `analysis.min_sl_distance_pct_per_symbol`:
```yaml
min_sl_distance_pct_per_symbol:
  BTC-USDT-SWAP: 0.005
  ETH-USDT-SWAP: 0.010
  SOL-USDT-SWAP: 0.008
  DOGE-USDT-SWAP: 0.007
  XRP-USDT-SWAP: 0.007
```
`BotConfig.resolve_min_sl_distance_pct(symbol)` falls back to global when symbol isn't listed. Emergency floor only — widens, never rejects.

### Forward plan — Phase 8

**Gate to leave data-collection:** 50 closed post-pivot trades, WR ≥ 45%, avg R ≥ 0, ≥2 trend-regimes represented, net PnL non-negative.

1. **Data collection** (active) — demo-run post-pivot bot, `rl.clean_since=2026-04-19T06:30:00Z` so reporter + future RL train on this data only. Factor-audit every ~10 closed trades for early-warning on regressions.
2. **GBT analysis** — `scripts/analyze.py` (xgboost) on clean trades for feature importance + partial dependence plots. Manual tune per-symbol thresholds, factor weights, veto thresholds.
3. **Optional RL** — stable-baselines3 *only if* GBT + manual tuning plateau. Scope: parameter tuner, not decision maker.

---

## Non-obvious design notes

Gotchas and rationales not self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing & margin

- **`_MARGIN_SAFETY = 0.95` + `_LIQ_SAFETY_FACTOR = 0.6`** (`src/strategy/rr_system.py`). Reserve 5% free margin for OKX fees/mark drift (else `sCode 51008`). Leverage additionally capped at `floor(0.6 / sl_pct)` so SL sits well inside liq distance — without this, tight-SL trades at 75x liquidate before SL fires.
- **Risk-budget vs margin-fit split.** `calculate_trade_plan(..., margin_balance=…)`: R comes off **total equity**, leverage/notional sized against **per-slot free margin** = `total_eq / max_concurrent_positions`. Cross-margin pools margin across open positions. Log emits `risk_bal=` and `margin_bal=` separately — different numbers by design.
- **Per-symbol instrument spec.** OKX `ctVal` differs per contract (BTC=0.01, ETH=0.1, **SOL=1**) and so does `maxLever` (BTC/ETH=100x, **SOL=50x**). `OKXClient.get_instrument_spec` populates `BotContext.contract_sizes` + `max_leverage_per_symbol` at prime. Hardcoded YAML would 100× over-size SOL.
- **`trading.symbol_leverage_caps`** — operator layer on top of OKX's cap. Demo flash-down wicks blow ≥30x on ETH even when structure holds; YAML caps ETH/DOGE/XRP conservatively (30x); BTC keeps 75x; SOL inherits OKX 50x.
- **Fee-aware sizing** (`fee_reserve_pct`, YAML `0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct`, so stop-out caps near $R *after* entry+exit taker fees. TP price unchanged — fee compensation flows through size, not by widening TP. `risk_amount_usdt` stays gross for RL reward comparability.
- **SL widening, not rejection.** `min_sl_distance_pct` floor: if Pine OB/FVG gives a 0.1% stop, widen to the per-symbol floor rather than reject. Notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant — just smaller position, more breathing room.
- **`min_tp_distance_pct`** (YAML `0.004`): reject `tp_too_tight` when TP is within ~2× round-trip taker. Evaluated after `_apply_htf_tp_ceiling`.

### Execution flow

- **PENDING state is first-class.** `setup_planner.ZoneSetup` → `OrderRouter.place_limit_entry` returns an algo id; `PositionMonitor.register_pending` tracks it. Every tick checks: (a) fill → transition to OPEN + place OCO, (b) `max_wait_bars` elapsed → cancel, (c) zone invalidated → immediate cancel. Without PENDING, a filled limit would race the confluence recompute and potentially place two OCOs.
- **Partial TP split guarantee.** If `int(num_contracts * partial_tp_ratio) == 0` or the remainder is 0, plan is rejected with `insufficient_contracts_for_split`. `OrderRouter._place_algos` **raises** instead of silent-fallback to single OCO — bypassed gate fails loud. Risk discipline over trade count.
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct`, YAML `0.001`). After TP1 fill, replacement OCO's SL sits a hair *past* entry on the profit side, so a touch-back to near-entry still covers remaining leg's exit taker fee + slippage.
- **SL-to-BE never spins.** `_detect_tp1_and_move_sl` splits cancel and place into separate try-blocks with three exits: (a) OKX codes `{51400,51401,51402}` on cancel are treated as idempotent success; (b) generic cancel failures increment `cancel_retry_count` and give up after `_CANCEL_MAX_RETRIES=3`, flipping `be_already_moved=True`; (c) place failure after successful cancel marks the position unprotected (CRITICAL log, drop TP2 from `algo_ids`, fire `on_sl_moved` callback so journal reflects reality) — emergency market-close is deliberately NOT automated.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. SL-to-BE callback uses `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)` (captured at `BotRunner.run` startup). `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Enrichment is non-optional.** `PositionMonitor._close_fill_from` only knows the position disappeared. `OKXClient.enrich_close_fill` queries `/account/positions-history` for real `realizedPnl`, `closeAvgPx`, `fee`, `uTime`. Without it every close looks BREAKEVEN and drawdown/streak breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.
- **Demo guard.** `OKXClient` refuses `demo_flag != "1"` unless `allow_live=True` is explicitly passed. Margin-fail codes `{51008, 51020, 51200, 51201}` map to `InsufficientMargin`; other sCode → `OrderRejected`.

### Multi-pair + multi-TF

- **Pine freshness poll.** `SignalTableData.last_bar` is the freshness beacon; `_wait_for_pine_settle` polls until it flips post-TF-switch. First-read `None` falls through for test fakes; stale → skip cycle.
- **Post-settle grace** (`pine_post_settle_grace_s=1.0`). The Oscillator table lags the Signal table by a beat on 1m TF. Grace sleep lets the rest catch up.
- **HTF skip for already-open symbols.** HTF S/R cache only feeds the entry planner. If symbol already has an open position, dedup blocks re-entry anyway → skip the entire 15m pass. **Cycle-visit-count gotcha:** a pair that just had its position opened will show 5 TF visits (15m/1m/3m in the opening cycle + 1m/3m in the next) — that's 2 cycles, not 1.
- **`bars_ago=0` is legitimate "just now".** Use `int(x) if x is not None else 99`, not `int(x or 99)` — the latter silently clobbers the freshest signal.
- **LTF reversal defensive close.** Cancels every tracked algo_id + market-closes when LTF trend/signal contradict open side within `max_age`. Gated by `ltf_reversal_min_bars_in_position` and idempotent via `defensive_close_in_flight`.

### Data quality

- **Pine table-cell precision (`"#.########"`, not `"#.##"`).** `smt_overlay.pine` writes `atr_14`, `price` and `vwap_*m` into the Signal table with `str.tostring(val, "#.########")`. `"#.##"` truncates DOGE ATR (~0.0008) and XRP ATR (~0.005) to `"0"`, which `structured_reader` parses as 0.0, which makes `select_sl_price` short-circuit on `atr <= 0` and return `no_sl_source` every cycle. `#` is an optional digit in Pine, so BTC 60000 still renders as `"60000"`; the wide format is safe for all scales.
- **Country→currency normalization in economic calendar.** Finnhub returns ISO-3166 alpha-2 (`"US"`); FairEconomy returns currency codes (`"USD"`). Without `_country_to_currency()`, `currencies: ["USD"]` filter silently drops every Finnhub event.
- **FairEconomy thisweek + nextweek.** Both fetched in parallel via `asyncio.gather`. **404 on nextweek.json is normal** (file published mid-week) → demoted to DEBUG log. Without nextweek the bot is blind to next-Mon/Tue events when run late in the week.
- **Blackout decision point is BEFORE TV settle.** `is_in_blackout(now)` runs before symbol/TF switch — saves ~46s of settle per blacked-out symbol. Open positions untouched; OCO algos manage exit.
- **Derivatives failure isolation.** WS disconnect / 401 / 429 / cache crash → logs warn, leaves `state.derivatives=None`. Strategy degrades to pure price-structure. Missing `COINALYZE_API_KEY` or `FINNHUB_API_KEY` silently falls through (warn once at construction).
- **Binance liq WS caveat.** Rate-limited to the *largest* liquidation per 1s window per symbol. Coinalyze history fills the gap.

### Risk & state

- **Risk manager replay.** `journal.replay_for_risk_manager(mgr)` rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from closed trades on startup — durable truth over in-memory state. Drawdown breaker is **permanent halt** (manual restart required).
- **Circuit breaker loosening (active).** Current YAML: `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.5`. These were loosened mid-Sprint-3 for data gathering and remain loose through Phase 8 data collection. **Restore to 5 / 15 / 25 / 2.0 once ≥20 post-pivot closed trades confirm zone-entry doesn't over-trigger.** Listed in Deferred TODO so it doesn't get forgotten.
- **Orphan reconcile is log-only.** `_reconcile_orphans` diffs live OKX positions vs journal OPEN and logs mismatches; operator decides. Restart-while-live verified end-to-end.
- **SL-to-BE survives restart.** `trades.sl_moved_to_be` is stamped by `journal.update_algo_ids` when the monitor replaces TP2 with the BE OCO. On restart, `_rehydrate_open_positions` forwards it as `be_already_moved=True` so `_detect_tp1_and_move_sl` short-circuits.
- **Reentry gate** (four sequential, first-fail-wins, per `(symbol, side)`):
  1. Cooldown `min_bars_after_close * tf_seconds(entry_tf)`
  2. ATR move `|price - last.price| / atr >= min_atr_move`
  3. Post-WIN quality: `proposed_confluence ≤ last.confluence` **blocks**
  4. Post-LOSS quality: `proposed_confluence < last.confluence` blocks (`=` passes)
  BREAKEVEN bypasses the quality gate. Opposite sides are isolated.

### Confluence (5-pillar post-pivot)

- **Pine is primary source of truth for structure; Python scores confluence.** OB/FVG factors accept Pine-derived or Python-recomputed zones. S/R is ATR-scaled.
- **Sweep → reversal.** Bearish sweep (swept highs) ⇒ **BULLISH** factor, not bearish — the weak hands got flushed.
- **Derivatives slot — at most one of three fires per cycle** (single elif chain): `derivatives_contrarian` (0.7) | `derivatives_capitulation` (0.6) | `derivatives_heatmap_target` (0.5). `_heatmap_supports_direction` requires nearest cluster within `ATR*3` AND notional ≥ 70% of largest.
- **`crowded_skip` gate.** Rejects entries aligned with crowded regime when `|funding_z| ≥ crowded_skip_z_threshold` (YAML `3.0`). Missing data never blocks — only trips with evidence.
- **VWAP composite reconsolidated post-pivot.** Old 3-way split (`vwap_{1m,3m,15m}_alignment` × 0.2 each) over-rewarded multi-TF echoes. New `vwap_composite`: all-3 align → 0.6, 2-of-3 → 0.3, 1-of-3 → 0 — concentrates reward on confluence, not repetition.
- **VWAP hard veto (`analysis.vwap_hard_veto_enabled`).** Strict: rejects bullish when price is below **every** available session VWAP, bearish when above every. Missing (zero) VWAPs are skipped; all-missing is fail-open. Reject reason `vwap_misaligned`, emitted before SL/TP math.
- **Per-symbol overrides.** `trading.swing_lookback_per_symbol` (DOGE/XRP=30 on thin 3m books), `analysis.htf_sr_buffer_atr_per_symbol` (SOL=0.10 vs global 0.20), `analysis.session_filter_per_symbol` (SOL/DOGE/XRP=[london]), `analysis.min_sl_distance_pct_per_symbol`. BotConfig resolvers fall back to globals when symbol isn't listed.

---

## Sprint 3 — archived diagnostic (2026-04-17 → 2026-04-19)

14-trade demo run that validated the **pivot thesis**. Retained here because the failure modes motivate the post-pivot architecture above.

**Run:** $5k demo, $50 R (1%), 4 slots, cross margin. Phase 6.9 stack (BLOK A + BLOK B overrides + `min_confluence=3.0`). Closed 14 trades, WR 28.6%, avg R -0.60, PnL -$456.

**Diagnostic findings that drove the pivot:**
1. **Uniform `sl_pct == 0.500%`** across every trade — `min_sl_distance_pct` floor (global 0.005) masked every structural SL. ETH-volatility pairs swept in 1-2 candles. → **Fix:** per-symbol SL floor + zone-based structural SL.
2. **Every entry `ordType="market"`** — zero zone-wait, momentum-chaser behaviour. → **Fix:** Layer 2 zone-based limit entries.
3. **14/14 trades in BALANCED regime** — `derivatives_regime` classifier not discriminating. → **Fix:** Layer 4 ADX trend-regime axis + conditional factor scoring.
4. **Factor WR regression:** `at_order_block` 35.7%→0%, `htf_trend_alignment` 35.7%→0% between pre-sprint (32 trades) and sprint 3 (14 trades). Same code, different regime → regime-fragile trend-continuation. → **Fix:** demote to weight=0; keep for audit; HTF-OB re-add queued.
5. **Zero cross-asset awareness** — SOL/DOGE shorts squeezed when BTC/ETH turned up 2026-04-19. → **Fix:** Layer 3 `CryptoSnapshot` altcoin veto.

**Inputs to pivot design (factor WR signal from 46-trade set):**
- Core 5 pillars — Market Structure, Liquidity, Money Flow, VWAP, Divergence — carry real WR signal. `recent_sweep` 45% WR, pre-split `vwap_alignment` 75% WR (n=8), `mss_alignment` 35% WR.
- LONDON session WR 45%, NEW_YORK 33%, OFF 17%. Per-symbol session filter was a correct instinct.
- Same-direction loss streaks (ETH 6× consecutive BULLISH LOSS on 2026-04-17) = missing cross-asset / regime flip detection.

**Archived artifacts:**
- `data/trades.db.sprint3_diagnostic_2026-04-19` — 46 closed trades (pre-sprint 32 + sprint 3 14), 5 OPEN → CANCELED with `close_reason=manual_close_pivot_2026-04-19`.
- `logs/bot.log.sprint3_final_2026-04-19` — pre-pivot log dump.
- `data/trades.db.backup_2026-04-18_pre-sprint3` — pre-existing checkpoint.
- `rl.clean_since` bumped to `2026-04-19T06:30:00Z` so Phase 8 baseline trains on post-pivot data only.

---

## Currency pair strategy

**5 OKX perps — BTC / ETH / SOL / DOGE / XRP.** Phase 1.5'te 5 → 3'e inilmişti (Coinalyze free-tier budget + dengeli RL dataset için). 2026-04-17'de DOGE + XRP eklendi — BTC/ETH/SOL genelde correlated, momentum-driven iki parite uncorrelated alpha ekler. Coinalyze free-tier budget güvenli: 5 × 5 call / 60s = 25/40 min. `trading.symbols` tek kaynak; legacy single-`symbol` form `DeprecationWarning` ile yüklenir.

**BTC/ETH pillar role (pivot 2026-04-19).** BTC ve ETH "piyasa direği"; SOL/DOGE/XRP altcoin olarak bunların rejim değişimlerine tabi. `CryptoSnapshot` altcoin cycle'larında zorunlu veto input'u. BTC/ETH arası ayrı bir korelasyon kontrolü yok; biri rip ederken diğeri flat ise altcoin için nötr context olarak okunur.

**`max_concurrent_positions=4`** (5 parite 4 slot için yarışır — her cycle 1 parite beklemede kalır, confluence gate daha iyi sinyal seçer). `per_slot = total_eq / 4 ≈ $800` margin budget (cross margin). R hâlâ total_eq'nun %1'i sabit, sadece notional tavan %25 küçülür.

**Cycle timing (5 parite, 3m entry TF = 180s cycle):** typical ~125-155s (freshness-poll erken döner), worst ~247s. Worst-case bazen oluşursa sadece o cycle skip olur. DOGE + XRP 30x leverage cap'li ve SOL-sınıfı thin book sayılıp `$8M capitulation_liq_notional` override aldılar.

**Adding a 6th+ pair:** drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol` entry, watch first 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`.

## Configuration

Full config in `config/default.yaml` (self-documenting with inline comments).

Top-level sections: `bot`, `trading` (symbols, TFs, risk, `symbol_leverage_caps`, `swing_lookback_per_symbol`, `fee_reserve_pct`), `circuit_breakers`, `analysis` (confluence, `min_tp_distance_pct`, `min_sl_distance_pct`, `min_sl_distance_pct_per_symbol`, `htf_sr_*`, `htf_sr_buffer_atr_per_symbol`, `session_filter_per_symbol`, `vwap_hard_veto_enabled`, `min_rsi_mfi_magnitude`, `liquidity_pool_max_atr_dist`, `trend_regime_*`, `ema_momentum_veto_*`), `execution` (margin_mode, partial_tp_*, `sl_be_offset_pct`, ltf_reversal_*, zone/PENDING timeouts), `reentry`, `derivatives`, `economic_calendar`, `okx`, `rl` (`clean_since` cutoff).

`.env` keys: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons** (unified post-pivot list):
- Structure / confluence: `below_confluence`, `no_setup_zone`, `wrong_side_of_premium_discount`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip`.
- R:R / sizing: `no_sl_source`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`.
- Pending lifecycle: `zone_timeout_cancel`, `pending_invalidated`.
- Sub-floor SL distances are **widened**, not rejected.

Every reject path writes to `rejected_signals`; `scripts/peg_rejected_outcomes.py` stamps counter-factual outcomes.

## Tech stack

**Python:** pydantic, pyyaml, python-dotenv, aiosqlite, httpx, **python-okx (0.4.x — not 5.x)**, websockets, pandas, numpy, ta, xgboost (Phase 8), stable-baselines3, gymnasium, torch, loguru, rich.

**Node:** `tradingview-mcp`, `okx-trade-mcp` + `okx-trade-cli`.

## Workflow commands

```bash
.venv/Scripts/python.exe -m src.bot --config config/default.yaml               # Demo
.venv/Scripts/python.exe -m src.bot --config ... --dry-run --once              # Smoke test
.venv/Scripts/python.exe -m src.bot --config ... --max-closed-trades 50        # Auto-stop at Phase-8 gate
.venv/Scripts/python.exe -m src.bot --derivatives-only --duration 600          # 10-min warmup, no orders
OKX_DEMO_FLAG=0 .venv/Scripts/python.exe -m src.bot --config ...               # Live (after demo proven)
.venv/Scripts/python.exe scripts/report.py --last 7d
.venv/Scripts/python.exe scripts/factor_audit.py                                # Per-symbol/session/regime WR + counter-factuals
.venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --commit              # Stamp rejected_signals hypothetical outcomes
.venv/Scripts/python.exe scripts/train_rl.py --min-trades 50 --walk-forward
.venv/Scripts/python.exe -m pytest tests/ -v
.venv/Scripts/python.exe scripts/logs.py [--decisions|--errors|--filter REGEX]
```

**Pine dev cycle** (via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix → `tv pine analyze` → `tv screenshot`.

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, can break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. Start `--profile demo`. Never enable withdrawal perms. Bind key to machine IP. Verify before live. Sub-account for live.

**Trading risks:** research project, not financial advice. Crypto futures = liquidation risk. Demo first; live with minimal capital. Check OKX TOS for automated trading.

**RL risks:** overfitting is #1 — always walk-forward. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if ceiling evident.
