# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on OKX. Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes, Arkham on-chain soft signals. Demo-runnable end-to-end; Pass 1 complete 2026-04-22 — restart-ready for Pass 2 uniform-feature dataset collection.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, runs tuning, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, OKX = hands, Python = brain.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 5 OKX perps — `BTC / ETH / SOL / DOGE / BNB`. 5 concurrent slots on cross margin (all active, no queue).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights. Confluence threshold `min_confluence_score=3.75` (Pass 1 Optuna tune, 2026-04-22). *Premium/discount gate and HTF TP/SR ceiling temporarily disabled 2026-04-19 — see changelog; re-evaluated as Pass 3 candidates.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. Single-leg OCO SL/TP at hard **1:2 RR** (tightened 1:3→1:2 on 2026-04-21 eve; partial TP disabled 2026-04-19 late-night — see changelog; `move_sl_to_be_after_tp1` flag kept but inert while partial off). Dynamic TP revision re-anchors the runner OCO to `entry ± 2 × sl_distance` every cycle, floor at 1.0R. **MFE-triggered SL lock (Option A, 2026-04-20)**: once MFE ≥ 1.3R (scaled from 2R when RR cap tightened), the runner OCO's SL is pulled to entry (+fee buffer) so the remaining 0.7R of target is risk-free. One-shot per position. **Maker-TP resting limit (2026-04-20)**: post-only reduce-only limit sits at TP price alongside the OCO — captures wicks as maker, avoids market-trigger latency.
- **Sizing:** fee-aware ceil on per-contract total cost so total realized SL loss (price + fee reserve) ≥ target_risk across every symbol (2026-04-19 late-night-2). Overshoot bounded by one per-contract step (< $3 per position). Operator override via `RISK_AMOUNT_USDT` env (2026-04-20) bypasses percent-mode sizing; 10%-of-balance safety ceiling. Per-symbol `min_sl_distance_pct_per_symbol` floors: BTC 0.004, ETH 0.008, SOL 0.010, DOGE 0.008, BNB 0.005 (reverted 2026-04-24 after the 2026-04-23 Pass 2 bump mechanically widened TPs at fixed 1:2 RR and collapsed post-bump WR 66.7%→22.2% across 9 trades — see changelog).
- **Journal:** async SQLite, schema includes `on_chain_context`, `demo_artifact`, `confluence_pillar_scores`, `oscillator_raw_values` (all JSON). Separate tables: `rejected_signals` (counter-factual outcome pegged), `on_chain_snapshots` (Arkham state mutation time-series), `whale_transfers` (raw WS events for Phase 9 directional learning).
- **On-chain (Arkham):** runtime soft signals only — daily bias ±15%, hourly stablecoin pulse +0.75 threshold penalty, altcoin-index +0.5 penalty on misaligned altcoin trades, **flow_alignment** 6-input directional score (stablecoin + BTC/ETH + Coinbase/Binance/Bybit 24h netflow; weights 0.25/0.25/0.15/0.15/0.10/0.10; default penalty 0.25), **per_symbol_cex_flow** binary penalty on misaligned symbol 1h volume (default 0.25, $5M floor). **Bitfinex + Kraken 24h netflow captured journal-only** (2026-04-23 night-late, 4th + 5th named venues — biggest single inflow / outflow in live probe vs. `type:cex` aggregate; not yet wired into `_flow_alignment_score` — Pass 3 decides weights). Whale HARD GATE removed 2026-04-22 — WS listener feeds `whale_transfers` journal for Pass 3 directional classification. Per-symbol token_volume fallback (2026-04-23): when Arkham `/token/volume/{id}` returns JSON `null` (confirmed for `solana`, `wrapped-solana`), `fetch_token_volume_last_hour` falls back to `/transfers/histogram` (flow=in + flow=out, last bucket) — zero coverage gap for the traded symbol set. **Netflow freeze fix (2026-04-23 night):** per-entity netflow rewritten from `/flow/entity/{entity}` (daily buckets, froze at UTC day close) to `/transfers/histogram?base=<entity>&granularity=1h&time_last=24h`; same fix for BTC/ETH aggregate. Daily-bundle refresh flipped from UTC-day gate to 5-min monotonic cadence (`on_chain.daily_snapshot_refresh_s: 300`) so `on_chain_snapshots` DB rows actually replace frozen values intraday. Credit-safe via v2 persistent WS streams + filter-fingerprint cache. All Arkham weights tuned in Pass 3.
- **Pass 2 instrumentation:** every trade row now captures `confluence_pillar_scores` (factor name → weight dict) and `oscillator_raw_values` (per-TF dict with 1m/3m/15m OscillatorTableData numerics: wt1/wt2/rsi/rsi_mfi/stoch_k/d/momentum/divergence flags). Both sourced from existing runner TF-switch cache — zero extra TV latency.
- **Tests:** 1063, all green. Demo-runnable end-to-end.
- **Data cutoff (`rl.clean_since`):** `2026-04-22T20:33:24Z` — Pass 2 restart cut. Pre-restart DB (42 Pass 1 trades) archived as `data/trades.db.pass1_backup_2026-04-22T203324Z`. Fresh DB created on first bot startup; every new row post-restart carries uniform feature coverage.

---

## Changelog

### 2026-04-22 — Pass 1 restructure day (consolidated)

Single-day dev arc spanning five sub-waves — ETH netflow re-enable, Arkham
FAZ 2 expansion, pending-limit hard-gate early-cancel, whale hard gate
removal + flow_alignment + Pass 1 tooling, and the gece-late runtime
promotion of per-entity / per-symbol Arkham data + oscillator raw values
+ confluence threshold 3 → 3.75. Individual commits preserve per-change
detail in git log (`git log --oneline --grep="2026-04-22"`); this entry
captures the end-state behaviour that survives into Pass 2.

**Runtime behaviour changes:**

1. **Whale hard gate REMOVED.** Previously `whale_transfer_blackout`
   rejected new entries and cancelled pendings for 10 min after any
   150M+ CEX↔CEX transfer — directionally ambiguous, killing winners
   and losers equally. WS listener now only streams events into the new
   `whale_transfers` journal table (for Phase 9 directional learning) +
   informational `whale_blackout_active` snapshot bool. Config flag
   `whale_blackout_enabled` repurposed to gate listener lifecycle only
   (name preserved to avoid YAML migration).

2. **Soft Arkham signals live.** Four threshold-penalty signals feeding
   `min_confluence_score` additive bumps:
   - `daily_bias_modifier_delta` 0.15 (±15% confluence multiplier)
   - `stablecoin_pulse_penalty` 0.75
   - `altcoin_index_penalty` 0.5 (altcoin-only)
   - `flow_alignment_penalty` 0.25 — NEW 6-input directional score
     combining stablecoin + BTC/ETH + Coinbase/Binance/Bybit 24h
     netflow (weights 0.25/0.25/0.15/0.15/0.10/0.10; BTC/ETH/entity
     signs inverted so OUT-of-CEX = bullish). Replaces the whale gate's
     directional intuition.
   - `per_symbol_cex_flow_penalty` 0.25 — NEW per-traded-symbol 1h
     token flow (`token_volume_1h_net_usd_json[symbol]`). Token INTO
     CEX = bearish for symbol, OUT = bullish. Binary misalignment
     penalty above $5M noise floor.

   FAZ 2 (per-entity netflow) + FAZ 3 (per-symbol token volume) were
   initially shipped journal-only (afternoon); promoted to runtime
   gece-late so Pass 2 has uniform-feature coverage from day one.

3. **Pending limit hard-gate early-cancel** (eve wave). Helper
   `evaluate_pending_invalidation_gates` re-runs
   `vwap_misaligned → ema_momentum_contra → cross_asset_opposition` on
   every poll for pending limits. First failing gate cancels the
   pending; new `pending_hard_gate_invalidated` reject_reason.
   Previously pending limits would fill into reversed conditions.

4. **Confluence threshold 3 → 3.75.** Optuna 42-trade sweep showed
   plateau at 3.75 (WR +3.8pp to 51.4%, net_R +16.08R vs baseline
   +13.46R). Above 3.75 the curve over-filters (4.0 drops n=31,
   net=+7.35R). Re-eval after 30 new closed trades post-restart; if
   accept rate < 0.5/day sustained, retreat to 3.5.

5. **ETH netflow re-enabled** in daily Arkham snapshot — journal column
   populated on every new row (not in bias rule yet; re-evaluate in
   Pass 2 GBT).

**Pass 2 instrumentation (journal schema additions):**

- `trades.confluence_pillar_scores` + `rejected_signals.confluence_pillar_scores`
  — JSON dict `{factor_name: weight}` captured from ConfluenceScore at
  entry / reject time. Unlocks Pass 2 per-pillar Bayesian weight
  tuning (impossible before without re-fetching market state).
- `trades.oscillator_raw_values` + `rejected_signals.oscillator_raw_values`
  — JSON dict keyed by TF (`"1m"` / `"3m"` / `"15m"`), each value a
  full `OscillatorTableData.model_dump()` (wt1, wt2, wt_vwap_fast,
  rsi, rsi_mfi, stoch_k, stoch_d, momentum, divergence flags,
  last_signal). Captured at entry time (market-entry path) or
  placement time (pending-fill + pending-cancel paths via
  `PendingSetupMeta.oscillator_raw_values_at_placement`). No extra TV
  latency — 15m sourced from existing `htf_state_cache` populated
  during HTF switch pass; 1m sourced from `ltf_cache[symbol].oscillator`
  (LTFState gained the field). Enables Pass 2 GBT continuous-magnitude
  features (WaveTrend depth, RSI band, Stoch K/D position) plus
  cross-TF divergence detection.
- `whale_transfers` time-series table — raw WS events (captured_at,
  token, usd_value, from_entity, to_entity, tx_hash, affected_symbols).
  Phase 9 joins against `trades.entry_timestamp` to learn which
  directional flows correlate with outcome.

**Pass 1 tooling:**

- `scripts/analyze.py` — xgboost GBT feature importance + SHAP + per-
  factor WR + rejected-signal counter-factual. Arkham segmentation
  marked DESCRIPTIVE ONLY (Pass 1 coverage inconsistent).
- `scripts/tune_confluence.py` — Optuna TPE over NON-Arkham knobs
  (confluence_threshold + 3 hard gate bools). Walk-forward 73/27
  split with overfit warning. Pass 2 extension scaffold in
  `scripts/replay_decisions.py` (Arkham knob + pillar-weight replay
  stub present, wiring pending Pass 2).

**Deleted:** `tests/test_whale_blackout_gate.py` (~210 lines, gate
removed); 2 pending-whale tests in `test_entry_signals.py`.

**Dataset contract:** `rl.clean_since` UNCHANGED through this dev day
(stays at `2026-04-19T19:55:00Z`). Operator bumps to restart-timestamp
during the Pass 1 → Pass 2 transition when the bot is restarted with
a fresh DB. Post-restart data has uniform feature coverage: Arkham
always on, all soft signals live, per-pillar + per-TF oscillator
captured on every row.

**Tests:** 946 → 1028 (net +82 after removing deprecated whale-gate
tests). Six new test files: `test_flow_alignment.py` (16 tests),
`test_whale_transfers_journal.py` (7), `test_per_symbol_cex_flow.py`
(9), `test_scripts_analyze.py` (3), `test_scripts_tune_confluence.py`
(9), `test_oscillator_raw_values.py` (17).

**Re-eval triggers (consolidated — monitor after Pass 2 data collection):**

1. **flow_alignment hit rate** — fraction of entries with `|score|>0`.
   Target 30-60%. <10% → lower noise floor; >90% → raise floor.
2. **flow_alignment directional lift** — aligned vs misaligned trades
   in Pass 2 data. ≥5pp WR delta → keep signal; neutral → Phase 12
   drop candidate.
3. **per_symbol_cex_flow fire rate** — Target 30-60%. <10% → floor
   $3M; >90% → floor $10M.
4. **Per-entity netflow NULL fraction** on snapshot rows should be
   <5%. Higher = Arkham fetch failures; inspect `arkham_entity_flow_*`.
5. **Confluence threshold 3.75** — sustain ≥0.5 accepts/day. Lower →
   retreat to 3.5.
6. **Per-pillar coverage** — `confluence_pillar_scores != '{}'` should
   be 100% on post-restart rows. Lower = entry path regression.
7. **Oscillator per-TF coverage** on post-restart rows: 3m ~100%,
   15m ~100% on non-already-open entries, 1m ≥95% (LTF read may time
   out). Lower = TF-switch cache regression.
8. **Pass 1 → Pass 2 tune overfit gate** — Pass 2 Optuna OOS net_R ≥
   0.5 × IS net_R AND OOS WR ≥ IS WR − 5pp before applying changes.
9. **Whale transfer event rate** — `whale_transfers` inserts per day.
   <5/day = Arkham WS fetch failing or threshold too high; >500/day =
   threshold too low. Expect 20-100/day at 150M.

### 2026-04-23 — Arkham /token/volume histogram fallback (SOL coverage gap fix)

Addendum to the 2026-04-22 Pass 1 entry. First Pass 2 cycle revealed that Arkham's `/token/volume/{id}?granularity=1h` returns HTTP 200 with body literal `null` (not an error, not an empty list) for `solana` and `wrapped-solana`. Other four tokens (bitcoin / ethereum / dogecoin / binancecoin) return the expected 25-bucket array; solana lands in a new "slug recognised + data unindexed" state that our primary-path code treated as None and silently dropped SOL from `token_volume_1h_net_usd_json`. Root cause likely SPL chain accounting differs from EVM deposit/withdraw semantics, so Arkham's aggregation pipeline didn't land solana in the same bucket format.

**Fix:** `fetch_token_volume_last_hour` now splits into primary + fallback (`src/data/on_chain.py`). Primary keeps the single 3-credit `/token/volume/{id}` call for tokens that work. When primary returns null/empty/malformed, the fallback `_token_netflow_via_histogram_1h` makes two `/transfers/histogram` calls (flow=in + flow=out) against `base=type:cex, tokens=[token_id], granularity=1h, timeLast=24h`, takes the LAST bucket's `usd` field from each, and returns `in - out` as the signed USD. Same return shape as primary → zero changes needed downstream (runner, journal writes, `per_symbol_cex_flow_penalty` scoring all unchanged).

**Cost:** +2 histogram calls per gap-token per refresh. With the single known gap (solana), ~150 extra credits/day — inside the 10k trial quota. Other tokens that join `WATCHED_SYMBOL_TO_TOKEN_ID` get the same fallback for free if Arkham's volume indexing also lags for them.

**Verification (live probe 2026-04-22 21:30Z post-fix):** SOL now yields `+$4,634,107` (6.39M in − 1.75M out) — matching the histogram raw data end-to-end. Post-restart log shows `arkham_token_volume_refreshed symbols=[BTC, ETH, SOL, DOGE, BNB]` (5/5 populated). Every new trades / rejected_signals row's `on_chain_context.token_volume_1h_net_usd_json` now contains SOL alongside the other four.

**Tests:** +6 in `test_on_chain_fetchers.py` locking the primary→fallback state machine (1028 → **1034 passing**).

### 2026-04-23 (evening) — SL floor bump + derivatives journal enrichment (Pass 3 feature prep)

Two paired tunes triggered by early Pass 2 observations. Operator raised `RISK_AMOUNT_USDT` to $100 and flagged that TP/SL levels landed "too tight" — per-symbol `min_sl_distance_pct_per_symbol` floors were the binding constraint on most entries, putting SL well inside 1m–3m noise envelopes. Separately, a journal audit showed that OI + funding + liquidation stats were all computed on `DerivativesState` at cycle time but only a subset (4 of 13 numeric fields) reached the journal — leaving Pass 3 Optuna/GBT without the OI × price combinatorial signal that traders use to infer long pile-in vs short covering vs capitulation.

**Per-symbol SL floors — `config/default.yaml::min_sl_distance_pct_per_symbol`:**

| Symbol | Old | New | Δ |
|---|---:|---:|---:|
| BTC-USDT-SWAP | 0.004 | **0.006** | +50% |
| ETH-USDT-SWAP | 0.008 | **0.010** | +25% |
| SOL-USDT-SWAP | 0.010 | **0.012** | +20% |
| DOGE-USDT-SWAP | 0.008 | **0.010** | +25% |
| BNB-USDT-SWAP | 0.005 | **0.007** | +40% (also made explicit; previously inherited 0.005 global) |
| XRP / ADA | 0.008 | 0.010 | parallel; symbols not currently watched |

R stays $100 flat — fee-aware ceil sizer auto-shrinks notional: `risk_amount / sl_pct = notional`. Example BNB: old 0.5% × $20k = $100 R → new 0.7% × $14.3k = $100 R. 40% less leverage exposure, 40% more wick protection. Applies to NEW entries from next restart; existing live position (BNB LONG) keeps its old 0.5% SL (operator-controlled cancel+replace if retroactive widening desired).

**Derivatives journal enrichment — 9 REAL columns + 1 TEXT column added to both `trades` and `rejected_signals`:**

| Column | Source | Pass 3 use |
|---|---|---|
| `open_interest_usd_at_entry` | `DerivativesState.open_interest_usd` | Absolute OI pairs with change % for crowding context |
| `oi_change_1h_pct_at_entry` | `oi_change_1h_pct` | Short-window positioning shift — classic OI × price divergence |
| `funding_rate_current_at_entry` | `funding_rate_current` | Absolute funding (raw decimal); GBT learns "funding > 0.05%/8h danger zone" |
| `funding_rate_predicted_at_entry` | `funding_rate_predicted` | Next-funding estimate, cost-of-carry forward |
| `long_liq_notional_1h_at_entry` | `long_liq_notional_1h` | Long-side liquidation flow USD |
| `short_liq_notional_1h_at_entry` | `short_liq_notional_1h` | Short-side — asymmetric squeeze pressure detection |
| `ls_ratio_zscore_14d_at_entry` | `ls_ratio_zscore_14d` | Crowded-positioning speed (ratio change z-score) |
| `price_change_1h_pct_at_entry` | entry-TF candle buffer (20 bars back on 3m) | OI × price combinatorial |
| `price_change_4h_pct_at_entry` | 80 bars back | Wider context window |
| `liq_heatmap_top_clusters_json` | `LiquidityHeatmap.clusters_above/below` top-5 each | Magnet / target modelling richer than the single nearest-above/below pair already stored |

Wiring via three new helpers in `src/bot/runner.py`:
- `_timeframe_to_minutes(tf)` — safe TV-string → int conversion with fallback ('3m'→3, '4H'→240, unknown→3).
- `_price_change_pct(candles, bars_ago)` — defensive percent-change with guards for empty/short buffer, zero closes, malformed candles.
- `_top_n_heatmap_clusters(heatmap, current_price, atr, top_n=5)` — JSON-ready extraction with signed toward-price `distance_atr`.

`_derive_enrichment(state)` signature extended to `_derive_enrichment(state, candles=None, entry_tf_minutes=3)` with backward-compat defaults — existing callers that pass only state keep working (new fields default to None / empty dict). Four call sites updated: `_record_reject` takes candles kwarg threaded from the cycle's buffer; market-entry `record_open` passes candles + cfg-derived entry_tf_minutes; pending-fill / pending-cancel paths stay `candles=None` (placement-time candles not stashed in PendingSetupMeta; price_change remains None for those rows — Pass 3 GBT can segment by "has price_change").

**Funding_z_6h + funding_z_24h DEFERRED.** Existing schema placeholder columns from Phase 7.B5 could have been populated this commit but the derivatives cache's `_funding_history` mixes 1h-cadence historical samples (loaded at startup from `fetch_funding_history_series`) with 75s-cadence incremental samples (appended per `refresh_interval_s` refresh). Clean wall-clock windowed z-scores require a timestamp-aware refactor of the history buffer to `list[(ts_ms, rate)]`. Flagged as Phase 12 candidate. The existing `funding_rate_zscore_30d` stays populated via the 720-sample tail.

**Cost impact:** zero extra API calls. Every field was already computed on `DerivativesState` or derivable from the existing candle buffer. Journal writes add ~200 bytes/row (10 extra columns × average). Schema migrations idempotent — restart auto-applies; no manual steps.

**Tests:** +23 in `test_derive_enrichment.py` covering `_timeframe_to_minutes`, `_price_change_pct`, `_top_n_heatmap_clusters`, and the extended `_derive_enrichment` (DerivativesState pull-through, heatmap integration, backward-compat, entry_tf_minutes=0 safety). Full suite 1034 → **1057 passing**.

**Re-eval triggers:**
1. **Wick-out rate** (SL floor bumps) — % of closed trades where SL hit within 1 ATR of floor-widened SL. Target < 40% post-bump. Higher → loosen further (e.g. BTC 0.006 → 0.008). Lower < 15% → may have over-loosened; consider tightening one step.
2. **Accept-rate per symbol post-bump** — if per-symbol accepts drop materially (e.g., BNB from ~1/hour to <1/4h) because notional floor hits OKX minimum, one-step tightening justified.
3. **Enrichment column coverage** — `open_interest_usd_at_entry IS NOT NULL` fraction on post-restart rows should approach 100% for symbols where Coinalyze snapshot stays fresh. Lower = cache freshness regression.
4. **Price change window hit rate** — `price_change_1h_pct_at_entry IS NOT NULL` on market-entry trades should be ~100%, 0% on pending-fill trades (expected by design). Mismatch = wiring regression.

### 2026-04-23 (night-late) — Bitfinex + Kraken added as 4th + 5th named venues (journal-only)

Ad-hoc coverage audit. Operator asked where the aggregate BTC CEX inflow was landing after observing the live snapshot's `cex_btc_netflow_24h_usd = +$2.46B` while the named trio (Coinbase + Binance + Bybit) summed to net −$144M. Live Arkham probe across 14 named CEX entities (BTC 24h, via `/transfers/histogram?base=<entity>&granularity=1h&time_last=24h`) showed:

| Metric | Value | % of aggregate |
|---|---:|---:|
| `type:cex` aggregate (live) | +$3.40B | 100% |
| Tracked 3 (CB+BN+BY) | −$45M | −1.3% |
| Bitfinex (biggest named inflow) | +$193M | +5.7% |
| Kraken (biggest named outflow) | −$210M | −6.2% |
| Kalan (unlabeled CEX clusters) | ~+$3.46B | ~%100 |

Named-entity coverage captured only ~1-7% of the full CEX BTC netflow signal — the remainder sits in Arkham's CEX-clustered but unlabeled hot wallets (OTC desks, market-maker CEX accounts, smaller / new venues). Limitation of Arkham labeling, not a probe bug.

**Fix (journal-only):** added Bitfinex + Kraken to the per-entity fetch loop and journal. No runtime scoring change — `_flow_alignment_score` still reads the original 6 inputs (stable + BTC + ETH + CB + BN + BY). Pass 3 Optuna decides whether + how to weight the two new inputs once uniform post-restart data exists.

**Wiring:**
- `src/data/on_chain_types.py` — two new optional float fields on `OnChainSnapshot`: `cex_bitfinex_netflow_24h_usd`, `cex_kraken_netflow_24h_usd`.
- `src/bot/runner.py` — fetch loop extended: `for entity in ("coinbase", "binance", "bybit", "bitfinex", "kraken")`. `BotContext` carries the two new fields; all four `OnChainSnapshot(...)` construction sites plumb them through. Fingerprint tuple includes both so mutations trigger a fresh journal row.
- `src/journal/database.py` — CREATE TABLE + two idempotent `ALTER TABLE … ADD COLUMN` migrations; INSERT column list + values extended. `record_on_chain_snapshot` signature gained two keyword args with `= None` defaults.
- `on_chain_context` dict that flows into `trades` / `rejected_signals` now exposes both fields (enables Pass 3 GBT to train on 5 entities instead of 3 without re-joining snapshot rows by timestamp).

**Cost:** +2 histogram calls per 5-min daily-bundle cycle → +24 calls/h × 2 entities = +48 req/h. Label-free (verified). Total label budget untouched (558/10k/mo).

**Not done:** `_flow_alignment_score` signature, config weights, per-symbol overrides. Intentionally deferred — mechanical weight add without Pass 3 data would be a guess; journal capture is the minimum that unblocks Pass 3 tuning.

**Tests:** 1063 → 1063, all green (new fields default to `None`, existing callers unchanged; migrations idempotent).

**Re-eval triggers:**
1. **Bitfinex / Kraken coverage** on `on_chain_snapshots` rows captured after this commit — both columns should be NON-NULL on ≥95% of rows. Zero-rate = fetcher silently failing for those slugs (try `bitfinex-fx` / other variants before widening the fix).
2. **Bitfinex inflow magnitude sanity** — median |net| over 7 days should be ≥$30M. Below that = signal too thin to warrant weight allocation in Pass 3.
3. **Kraken outflow persistence** — one-shot bearish-lean days don't prove edge. 14-day rolling sign persistence is the signal; Pass 3 GBT segments on it.

### 2026-04-23 (night) — Arkham netflow freeze fix (per-entity + BTC/ETH 24h + cadence)

Two paired data bugs and a cadence rewrite, all in one sitting. DB audit on the fresh Pass 2 table showed per-entity Arkham values (Coinbase/Binance/Bybit 24h netflow) bit-exact identical across 17 consecutive `on_chain_snapshots` rows spanning ~24h — impossible for rolling 24h data on live markets. Parallel check on BTC/ETH 24h netflow found the same lock-up: 5 pre-midnight rows changed, everything after 2026-04-23T00:01 UTC stood still. Live Arkham probe vs. journal:

| Entity / Metric | Journal value | Live probe | Error |
|---|---:|---:|---|
| Coinbase 24h | +$198,815 | +$344,000,000 | ~1,700× off |
| Binance 24h | +$50,449,218 | +$11,200,000 | ~4.5× off |
| Bybit 24h | −$216,421 | +$23,800,000 | **SIGN FLIPPED** |
| BTC 24h | −$1,058,000,000 | −$785,000,000 | ~34% off |
| ETH 24h | +$72,900,000 | −$197,000,000 | **SIGN FLIPPED** |

**Root causes (two separate bugs):**

1. **`/flow/entity/{entity}` returns DAILY buckets.** Called via `fetch_entity_netflow_24h`, it returned "most recent complete UTC day" — frozen until next day closes, regardless of wall-clock drift. `/flow/entity/*` has no 1h granularity mode.
2. **`_net_flow_via_histogram` used `granularity="1d"`.** Same daily-bucket freeze for BTC/ETH aggregate netflow. Pre-UTC-midnight the active bucket still moved (why first 5 rows looked alive); post-midnight the bucket value became immutable.

**Fix (`src/data/on_chain.py`):**

- `_net_flow_via_histogram` — granularity flipped `"1d"` → `"1h"`. Same in/out diff logic, now reads the rolling 24h hourly histogram.
- `fetch_entity_netflow_24h` — rewritten to call `/transfers/histogram?base=<entity>&granularity=1h&time_last=24h` twice (flow=in + flow=out) and sum the full 24-bucket series. Return shape unchanged — downstream runner / journal / flow_alignment scoring untouched.

**Cadence flip (`src/bot/runner.py` + `config.py` + `default.yaml`):**

Granularity fix alone wasn't enough — the whole daily-bundle branch was UTC-day-gated (`if last_on_chain_daily_date != today: …`). That meant the fix would only take effect once per 24h, and the frozen journal rows would continue overwriting the fingerprint cache dedup logic nothing. New `on_chain.daily_snapshot_refresh_s: 300` (5 min) runs the bundle on monotonic cadence. Context field `last_on_chain_daily_date: date` replaced by `last_on_chain_daily_ts: float`.

- **5-min choice:** live bucket-update probe (3 samples, 75s apart) showed closed buckets (10:00, 11:00) bit-exact identical; active bucket (12:00) grew T0=$130.3M → T1(+76s)=$139.7M → T2(+77s)=$139.7M. Arkham indexer repopulates the active hour every 60-120s. 5 min sits safely above that noise floor, still catches intraday inflection within 2-3 samples per direction change.
- **Cost:** 12 histogram calls/cycle × 12 cycles/h = 144 calls/h. All histogram endpoints label-free (confirmed — label budget 558/10k/mo untouched). Rate-limit headroom: 12 calls × 1.1s = 13.2s of a 300s window (4.4% utilization).

**DB consequence:** `on_chain_snapshots` fingerprint-dedup skips no-op ticks; with fresh rolling-24h data, the fingerprint now mutates on most 5-min cycles → new rows land continuously instead of one-per-day.

**Pass 2 dataset caveat (saved to memory):** The first 8 post-restart closed trades were entered against frozen per-entity + BTC/ETH netflow values (and possibly flipped signs). Pass 3 GBT / Bayesian tune should drop those 8 trades from flow_alignment + per-entity feature columns while keeping them for non-Arkham features. `entry_timestamp` cutoff = this commit's timestamp.

**Tests:** `test_on_chain_fetchers.py` — 5 obsolete `_entity_flow_body` mocks deleted, 5 new histogram-based tests + 1 snapshot-granularity lock-in added. `test_runner_on_chain.py` — 3 UTC-date-gate tests rewritten to monotonic cadence (`test_refresh_daily_respects_cadence`, `test_refresh_daily_refetches_after_cadence_elapsed`, `test_refresh_daily_failure_keeps_previous_snapshot` seeding simplified). Full suite 1057 → **1063 passing**.

**Re-eval triggers:**

1. **`on_chain_snapshots` unique row rate** post-restart — at 5-min cadence on changing markets, expect ≥ 6 new rows/hour. < 2/hour = fingerprint dedup collision (two successive fetches returned identical values) OR Arkham fetch failing silently.
2. **Per-entity freeze regression** — SQL: `SELECT COUNT(DISTINCT cex_coinbase_netflow_24h_usd) FROM on_chain_snapshots WHERE captured_at > <fix-commit-ts>`. Value < 5 over a 24h window means the histogram-based fetcher is itself returning stale data (Arkham indexer down, or the base=<entity> filter not matching).
3. **Label budget drift** — `arkham_client.label_usage_pct` should stay flat at ~5-6% (558/10k baseline). Any upward drift means a histogram call is accidentally hitting a label-charging endpoint; investigate.
4. **Signs flipped in new rows** — periodic spot-check: pick a row, live-probe Arkham `/transfers/histogram?base=bybit&flow=in/out&granularity=1h&time_last=24h`, sum in−out, compare to stored `cex_bybit_netflow_24h_usd`. Drift > 10% = indexer re-balance or aggregation logic drift.

### 2026-04-24 — SL floor bump reverted (Pass 2 postmortem)

Single-commit revert of the 2026-04-23 evening per-symbol `min_sl_distance_pct_per_symbol` bump after a 15-trade post-bump window showed unambiguous performance collapse. Operator flagged the shift from scalp-duration holds to multi-hour positions losing in chop; DB audit confirmed.

**Data (pre-bump vs. post-bump, window = clean_since → 2026-04-24 01:00 UTC):**

| Metric | Pre-bump (n=6) | Post-bump (n=9) | Delta |
|---|---:|---:|---:|
| Win rate | 66.7% | 22.2% | **−44.4pp** |
| Net R | +4.15 | −6.01 | **−10.16R** |
| Mean R | +0.69 | −0.67 | −1.36R |
| Hold time | 70.6 min | 394.3 min | **5.6×** |
| Mean SL dist % | 0.683% | 0.822% | +20.3% |
| Mean TP dist % | 1.367% | 1.644% | +20.2% |
| Trade frequency | 1.35/h | 0.38/h | 3.6× slower |
| `zone_timeout_cancel` rejects | 14 | 52 | **3.7×** |

Per-symbol post-bump: BTC 1/3, ETH 0/1, SOL 0/1, DOGE 0/2, BNB 1/2. DOGE+SOL (widest %-bump) went 0/3.

**Causal chain (code-verified):**

1. **Fixed 1:2 RR → mechanical TP widening.** `tp_price = entry ± sl_distance × target_rr_ratio` at [rr_system.py:170-172](src/strategy/rr_system.py). A 50% SL floor bump locks in a 50% wider TP with no escape path.
2. **Dynamic TP revision re-anchors the wider distance for the full lifetime.** [runner.py:1273-1285](src/bot/runner.py) reads immutable `plan_sl_price` (captured at fill) every 30s and re-computes TP at `entry ± 2 × sl_distance`. The floor-widened SL therefore persists as widened TP across cycles.
3. **MFE-lock (1.3R) triggers later in absolute price.** Lock distance = `1.3 × sl_pct × entry` → BTC pre-bump $327 move, post-bump $491 (+50%). The "almost-win → risk-free" safety net fires less often in choppy tape; 1.0R trades peak and fall back to −1R instead of locking at BE. Accounts for most of the 7/9 post-bump loss cluster.
4. **Zone edges widened → pending limits starve.** `apply_zone_to_plan` re-applies the floor at [setup_planner.py:510-518](src/strategy/setup_planner.py); widened edges miss fills more often, inflating `zone_timeout_cancel`.

**Confounds considered:** Arkham netflow freeze (first 8 post-restart trades frozen) affected 6 pre-bump + 2 post-bump rows — biases AGAINST pre-bump group, yet pre-bump still won 66.7%, so the signal-quality confound actually understates the bump impact. Market regime (chop) amplifies the mechanism but is not causal. Sample (n=6/9) small, but effect size (−44pp WR, 5.6× hold) far exceeds plausible noise and mechanism is reproducible in code.

**Reverted values (match pre-2026-04-23 Pass 1 profile):**

| Symbol | Bumped | Reverted | Rationale |
|---|---:|---:|---|
| BTC-USDT-SWAP | 0.006 | **0.004** | Pass 1 baseline |
| ETH-USDT-SWAP | 0.010 | **0.008** | preserves 2026-04-21 eve 0.006→0.008 bump |
| SOL-USDT-SWAP | 0.012 | **0.010** | Pass 1 baseline |
| DOGE-USDT-SWAP | 0.010 | **0.008** | Pass 1 baseline |
| BNB-USDT-SWAP | 0.007 | **0.005** | back to global-default parity |
| XRP / ADA (not watched) | 0.010 | **0.008** | parallel revert |

`RISK_AMOUNT_USDT=$100` unchanged; fee-aware ceil sizer auto-widens notional (`risk / sl_pct = notional`) so R stays flat. `target_rr_ratio=2.0` and `sl_lock_mfe_r=1.3` unchanged — Pass 3 tune candidates, not bump-triggered knobs.

**4 open positions at revert time (ETH 21:13 / SOL 22:00 / BTC 00:53 / BNB 01:01) retain their bumped SL/TP** — retroactive cancel+replace risks race conditions with `_pending` and algo-sweep code. They clear naturally via SL/TP hit or timeout.

**Explicitly NOT done:** partial asymmetric revert (only DOGE+SOL), `target_rr_ratio` tighten, `sl_lock_mfe_r` lower. All deferred to Pass 3 tune — mechanical bump revert is the smallest change that restores the Pass 1 trade-shape profile.

**Tests:** config change only, no code touched. 1063 tests unchanged.

**Re-eval triggers:**

1. **Post-revert WR** over 10 closed trades — target ≥ 40% (break-even @ 1:2 RR is 33.3%, Pass 1 baseline was 47.6%).
2. **Post-revert hold time** — target < 150 min median (pre-bump was 70 min; 150 min is ~2× pre-bump, still sub-chop-horizon).
3. **`zone_timeout_cancel` rate** as fraction of total rejects — target < 25% (post-bump was 32%; pre-bump was ~16%).
4. **`no_sl_source` / `tp_too_tight` reject spikes** — BTC 0.4% floor can occasionally land SL inside OKX fee + mark drift; if either reject rate > 5% of entry attempts, tighten that specific symbol's floor one step (e.g. BTC 0.004 → 0.005).
5. **If post-revert metrics fail** — do NOT re-bump floors. Either collect more data (regime-driven noise) or investigate upstream signal quality (confluence threshold, pillar weights). Bump mechanism is proven harmful at fixed 1:2 RR.

### Historical context (pre-Pass-1, 2026-04-19 → 2026-04-21)

Design decisions baked into the current code. Git log (`git log --before=2026-04-22`) has per-commit detail; this section exists so new readers understand *why* the code looks the way it does without excavating history.

**Scalp-native pivot (2026-04-19).** Full strategic rebuild: zone source priority rewired (`vwap_retest → ema21_pullback → fvg_entry → sweep_retest → liq_pool_near`), pillar weights rebalanced toward oscillator / VWAP / money-flow / divergence with structure demoted, Pine overlay script trimmed of dead confluence rows. Partial TP disabled (`execution.partial_tp_enabled=false`) — full-win payout 2R, break-even WR 33%. HTF S/R ceiling + Premium/Discount hard vetoes disabled — both are Pass 3 candidates to return as soft-weighted factors. `vwap_1m_alignment` kept at 0.2 weight as a GBT probe.

**Fee-aware ceil sizing (2026-04-19 late).** `num_contracts = ceil(max_risk / per_contract_cost)` with `per_contract_cost = (sl_pct + fee_reserve_pct) × contracts_unit_usdt`. Guarantees realized SL loss (price + fee reserve) ≥ target R across every symbol; overshoot bounded by one per-contract step (< $3/position). Capped path (leverage ceiling) still floors to respect the hard cap.

**Execution hardening day (2026-04-20).** Five fixes, one dev day:
- **MFE-triggered SL lock (Option A)** — at MFE ≥ 1.3R, cancel + replace runner OCO with SL at entry + fee buffer. One-shot per position. Kills "almost-win → round-trip to -1R" bucket.
- **Maker-TP resting limit** — post-only reduce-only limit sits at TP price alongside the OCO. Primary (maker fill); OCO market-trigger = fallback. `clOrdId` prefix `smttp` distinguishes from entry limits (`smtbot`).
- **Phantom-cancel fix** — `poll_pending` / `cancel_pending` only drop the row on success or idempotent-gone (`51400/51401/51402`); transient failures preserve the row for next poll retry. Eliminated orphan-limit-to-OCO race during brief OKX outages.
- **Stale-algoId + startup reconcile** — `revise_runner_tp` forwards `_on_sl_moved` so journal `algo_ids` stays in sync. Startup runs `_cancel_orphan_pending_limits` + `_cancel_surplus_ocos`.
- **Flat-USDT override** — `trading.risk_amount_usdt` / `RISK_AMOUNT_USDT` env bypasses `balance × risk_pct`. 10%-of-balance safety rail at config load.

**Hard 1:2 RR cap + dynamic TP revision (2026-04-21 eve).** `target_rr_ratio=2.0` (tightened from 3.0), `tp_min_rr_floor=1.0` (from 1.5), `sl_lock_mfe_r=1.3` (scaled from 2.0). `PositionMonitor.revise_runner_tp` cancels + places runner OCO per cycle with `tp_revise_min_delta_atr=0.5` gate + 30s cooldown. ETH `min_sl_distance_pct` bumped 0.006 → 0.008 (DOGE-level; wider noise envelope). Test guard `test_default_yaml_runner_tp_is_hard_1_2` locks the contract.

**Arkham on-chain integration (2026-04-21).** Phase A-E + F1-F3 + v2 WS migration, all in one day. Delivered: `ArkhamClient` (httpx, auto-disable at 95% usage), `OnChainSnapshot` + `WhaleBlackoutState` state, daily macro bias modifier (±0.15), hourly stablecoin pulse penalty (+0.75 threshold bump), altcoin-index penalty (+0.5 on misaligned altcoin trades), whale WS listener (hard gate — since removed 2026-04-22). Credit-safe via v2 persistent WS streams (`/ws/v2/streams`) + filter-fingerprint sidecar — zero credit burn on restart. `on_chain_snapshots` time-series table (eve-late) captures every state mutation for Pass 3 lifetime joins.

**Zone refinements (2026-04-21).** VWAP-band zone anchor (Convention X): long zone mid at `VWAP + 0.4σ`, short at `VWAP − 0.4σ` (operator preference, pulls entry closer to VWAP than plain 0.5 midpoint). Pending zone timeout 10 → 7 bars (21 min on 3m; tighter pullback window matches scalp-native zone half-life).

**TP-revise hardening (2026-04-19 → 2026-04-20).** Immutable `plan_sl_price` on `_Tracked` (survives SL-to-BE). `51400 verify-before-replace` via `list_pending_algos` + `_verify_algo_gone` (prevents double-stops). Mark-price OCO triggers (`trigger_px_type="mark"`) on all paths. Binance cross-check via `BinancePublicClient.get_kline_around` validates entry/exit inside concurrent real-market candle; journal schema v3 adds `demo_artifact` + `artifact_reason` flags; `scripts/report.py --exclude-artifacts`.

**Deliberately closed features (flags preserved in code):**
- `execution.partial_tp_enabled=false` (2026-04-19 late) — Pass 3 re-enable candidate if WR < 33%.
- `analysis.htf_sr_ceiling_enabled=false` (2026-04-19) — split into TP-ceiling vs SL-push if Pass 3 shows asymmetric lift.
- `analysis.premium_discount_veto_enabled=false` (2026-04-19) — return as soft weighted factor (~10-15% weight-equivalent) post-Pass-3.
- `analysis.vwap_hard_veto_enabled=false` (guard, flip per session).
- `execution.htf_fvg_entry_enabled=false` (opt-in; Pass 3 GBT confirms 15m FVG signal first).

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

Pine is source-of-truth for **structure**; Python scores confluence and plans zones. Earlier single-purpose scripts (pre-consolidation) are archived in git history.

**Critical:** Table cells use `str.tostring(val, "#.########")` not `"#.##"` — truncation zeroes DOGE/XRP ATR and causes `no_sl_source` every cycle.

---

## Architecture

Modules have docstrings; a tour for orientation:

- `src/data/` — TV bridge, `MarketState` assembly, candle buffers, Binance liq WS, Coinalyze REST, economic calendar (Finnhub + FairEconomy), HTF cache, **Arkham client + WS listener + on-chain types**.
- `src/analysis/` — Structure (MSS/BOS/CHoCH), FVG, OB, liquidity, ATR-scaled S/R, multi-TF confluence + regime-conditional weights + **daily-bias modifier**, derivatives regime, **ADX trend regime**, **EMA momentum veto**, **displacement / premium-discount** gates.
- `src/strategy/` — R:R math, SL hierarchy, entry orchestration (+ **Arkham soft signals: daily-bias / stablecoin-pulse / altcoin-index / flow_alignment / per_symbol_cex_flow penalties**), **setup planner** (zone-based limit-order plans), cross-asset snapshot veto, risk manager.
- `src/execution/` — python-okx wrapper (sync → `asyncio.to_thread`), order router (`place_limit_entry` / `cancel_pending_entry` / `place_reduce_only_limit` / market fallback), REST-poll position monitor with **PENDING** state + **MFE-lock + TP-revise + maker-TP tracking**, typed errors.
- `src/journal/` — async SQLite, schema v3 trade records (+ `on_chain_context`, `demo_artifact`), `rejected_signals` + counter-factual stamps, `on_chain_snapshots` time-series, pure-function reporter.
- `src/bot/` — YAML/env config, async outer loop (`BotRunner.run_once` — closes → snapshot → pending → per-symbol cycle), on-chain snapshot scheduler, CLI entry.

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

`displacement_candle` · `ema_momentum_contra` · `vwap_misaligned` · `cross_asset_opposition` (altcoin veto when BTC+ETH both oppose). *`premium_discount_zone` + `htf_tp_ceiling` wired but disabled (Pass 3 soft-weighted re-add candidates). Whale `whale_transfer_blackout` gate REMOVED 2026-04-22 — see changelog; directional intuition moved to `flow_alignment` soft signal.*

### Arkham soft signals (threshold bumps, not gates)

All bump `min_confluence_score` when misaligned; aligned → 0. Tune in Pass 3.

- **Daily bias** — 24h CEX BTC netflow + stablecoin balance → bullish/bearish/neutral. Confluence multiplier `×(1±0.15)`.
- **Stablecoin pulse** — hourly USDT+USDC CEX netflow. Misaligned → `+0.75` threshold bump.
- **Altcoin index** — 0–100 scalar. ≤25 penalises altcoin longs; ≥75 penalises altcoin shorts. `+0.5` bump. BTC/ETH exempt.
- **flow_alignment** (NEW 2026-04-22) — 6-input directional score `[-1, +1]`: stablecoin pulse (0.25) + BTC netflow (0.25) + ETH (0.15) + Coinbase (0.15) + Binance (0.10) + Bybit (0.10). Stables IN = bullish, BTC/ETH/entity OUT = bullish. Misaligned → `0.25 × |score|` bump.
- **per_symbol_cex_flow** (NEW 2026-04-22) — traded symbol's own 1h token flow. INTO CEX = bearish for symbol, OUT = bullish. Binary `+0.25` bump above $5M floor.

### Zone-based entry

`confluence ≥ effective_threshold → setup_planner picks a ZoneSetup → post-only limit at zone edge → 7 bars wait → fill | cancel`.

Zone source priority: **vwap_retest → ema21_pullback → fvg_entry (3m) → sweep_retest → liq_pool_near**. VWAP-band anchor uses Convention X (0.7 long / 0.3 short, entry at VWAP ± 0.4σ).

Position lifecycle: `PENDING → FILLED → OPEN → CLOSED` or `PENDING → CANCELED`.

### Regime awareness

ADX (Wilder, 14) classifies `UNKNOWN / RANGING / WEAK_TREND / STRONG_TREND`. Under `STRONG_TREND`, trend-continuation factors get 1.5× and sweep factors 0.5×; `RANGING` mirrors. Journal stamps `trend_regime_at_entry` on every trade.

---

## Configuration

All config in `config/default.yaml` (self-documenting). Top-level sections: `bot`, `trading`, `circuit_breakers`, `analysis`, `execution`, `reentry`, `derivatives`, `economic_calendar`, `on_chain`, `okx`, `rl`.

**`.env` keys:** `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`, `OKX_DEMO_FLAG`, `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `ARKHAM_API_KEY`, `RISK_AMOUNT_USDT` (optional flat-$ override), `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons (unified):** `below_confluence`, `no_setup_zone`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip`, `no_sl_source`, `zero_contracts`, `tp_too_tight`, `zone_timeout_cancel`, `pending_invalidated`, `pending_hard_gate_invalidated` (mid-pending hard-gate flip). Deprecated but kept in vocabulary for legacy rows: `whale_transfer_blackout` (gate removed 2026-04-22), `wrong_side_of_premium_discount`, `htf_tp_ceiling`, `insufficient_contracts_for_split` (flags disabled). Sub-floor SL distances are **widened**, not rejected. Every reject writes to `rejected_signals` with `on_chain_context` + `confluence_pillar_scores` + `oscillator_raw_values` JSON columns.

**Circuit breakers (currently loosened for data collection):** `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.5`. Restore to `5 / 15 / 25 / 2.0` after 20+ post-pivot closed trades.

---

## Non-obvious design notes

Things that aren't self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing

- **`_MARGIN_SAFETY=0.95` + `_LIQ_SAFETY_FACTOR=0.6`** (`rr_system.py`). Reserve 5% for fees/mark drift (else `sCode 51008`). Leverage capped at `floor(0.6/sl_pct)` so SL sits well inside liq distance.
- **Risk vs margin split.** R comes off total equity; leverage/notional sized against per-slot free margin (`total_eq / max_concurrent_positions`). Log emits `risk_bal=` + `margin_bal=` separately — they're different by design.
- **Per-symbol `ctVal`.** BTC `0.01`, ETH `0.1`, **SOL `1`**, DOGE `1000`, BNB `0.01`. `OKXClient.get_instrument_spec` primes `BotContext.contract_sizes`. Hardcoded YAML would 100× over-size SOL.
- **Fee-aware sizing** (`fee_reserve_pct=0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct` so stop-out caps near $R *after* entry+exit taker fees. `risk_amount_usdt` stays gross for RL reward comparability.
- **SL widening, not rejection.** Sub-floor SL distances widen to the per-symbol floor; notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant.
- **Flat-$ override beats percent mode.** `RISK_AMOUNT_USDT` env bypasses `balance × risk_pct`. Safety rail: override ≤ 10% of balance. Ceil-rounding on contracts makes realized SL loss ≥ target with ≤$3 overshoot.

### Execution

- **PENDING is first-class.** A filled limit without PENDING tracking would race the confluence recompute and potentially place two OCOs.
- **Two TP orders per position.** OCO has a market-on-trigger TP (fallback); a post-only reduce-only maker limit sits at the same TP price (primary). Either fills the position flat; the other gets swept. `clOrdId` prefix `smttp` distinguishes TP limits from entry limits (`smtbot`).
- **MFE-triggered SL lock.** At MFE ≥ 2R, cancel+replace runner OCO with SL at entry+fee_buffer. One-shot flag prevents retry. Skipped if `be_already_moved=True` or `plan_sl_price=0.0` (rehydrate sentinel).
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct=0.001`). After TP1 fill the replacement OCO's SL sits a hair past entry on the profit side. *Inert while `partial_tp_enabled=false` — TP1 never fires.*
- **SL-to-BE never spins.** Cancel and place are separate try-blocks. OKX `{51400,51401,51402}` on cancel = idempotent success. 3 cancel failures → give up + mark `be_already_moved=True`. Place failure after cancel = unprotected position, CRITICAL log, operator decides — **emergency market-close is not automated**.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. Callbacks use `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)`; `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Close enrichment is non-optional.** `OKXClient.enrich_close_fill` queries `/account/positions-history` for real `realizedPnl`. Without it every close looks BREAKEVEN and breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.
- **Phantom-cancel resistance.** `poll_pending` + `cancel_pending` only pop the row on success or idempotent-gone. Transient cancel failures preserve row for next poll retry. No dropped-but-still-live orphans.
- **Startup reconcile cancels resting limits + surplus OCOs.** `_pending` is empty at startup, so any live limit is orphan by construction. Surplus OCOs (more algos live than journal shows for a key) get canceled; OCOs for keys with no journal row are log-only (never auto-cancel a stop that might protect an un-tracked position).

### Data quality

- **`CryptoSnapshot` order is load-bearing.** BTC + ETH cycle first so altcoin cycles can read the snapshot for cross-asset veto. Reorder and the veto silently fails open.
- **`bars_ago=0` is legitimate "just now".** Use `int(x) if x is not None else 99`, not `int(x or 99)` — the latter silently clobbers the freshest signal.
- **Blackout decision is BEFORE TV settle.** Saves ~46s per blacked-out symbol.
- **Derivatives failures isolate.** WS disconnect / 401 / 429 → `state.derivatives=None`, strategy degrades to pure price-structure.
- **On-chain failures isolate.** Arkham snapshot None / stale / master-off → modifiers multiply 1.0, penalties add 0, WS listener self-disables after 3 consecutive failures. Pre-Arkham behavior preserved.
- **FairEconomy `nextweek.json` 404 is normal** (file published mid-week). Without it the bot is blind to next-Mon/Tue events when run late in the week.

### Multi-pair + multi-TF

- **Pine freshness poll.** `last_bar` is the beacon. `pine_post_settle_grace_s=1.0` covers the 1m-TF lag where `last_bar` flips before the Oscillator finishes rendering.
- **HTF skip for open-position symbols.** Skipping the 15m pass saves ~5-15s per held position per cycle. Dedup would block re-entry anyway.

### Risk & state

- **Risk manager replay.** `journal.replay_for_risk_manager(mgr)` rebuilds `peak_balance`, `consecutive_losses`, `current_balance` from closed trades on startup — durable truth over in-memory state. Drawdown breaker = permanent halt (manual restart required).
- **SL-to-BE survives restart.** `trades.sl_moved_to_be` flag forwards as `be_already_moved=True` on rehydrate so the monitor doesn't double-move the SL.
- **TP limit re-placed on restart.** Orphan-sweep wipes resting limits at startup; rehydrate regenerates them for every non-BE journal OPEN row. Order: reconcile BEFORE rehydrate (else the freshly-placed TP limits get nuked).
- **Arkham stream survives restart.** `data/arkham_stream_id.txt` caches the v2 stream id (gitignored). Startup verifies via `GET /ws/v2/streams` and reuses if alive — zero credit burn on restart.

---

## Currency pair notes

5 OKX perps — BTC / ETH / SOL / DOGE / BNB. BTC + ETH + BNB are market pillars (major-class book depth); SOL + DOGE are altcoins gated by the cross-asset veto. XRP pulled on 2026-04-19 (pm) after the attach-race incident; ADA pulled on 2026-04-19 (eve) after hitting OKX demo OI platform cap (`sCode 54031`). Their per-symbol override maps remain in YAML (harmless when not watched) so reinstating any of them is one-line once the underlying blocker clears.

`max_concurrent_positions=5` (every pair can hold a position simultaneously — no slot competition; confluence gate still picks setups, but cycle isn't queue-limited). Cross margin, `per_slot ≈ total_eq / 5 ≈ $1000` on a $5k demo. R stays 1% of total equity ($50), or flat via `RISK_AMOUNT_USDT` override.

Cycle timing at 3m entry TF = 180s budget: typical 150–180s with 5 pairs (comfortable inside the budget after 7→5 rollback). DOGE + ADA (if reinstated) + XRP (if reinstated) leverage-capped at 30x; SOL/BNB inherit OKX 50x cap.

Per-symbol overrides (YAML, ADA/XRP rows kept for easy reinstatement):
- `swing_lookback_per_symbol`: DOGE=30 (thin 3m book; ADA/XRP=30 preserved).
- `htf_sr_buffer_atr_per_symbol`: SOL=0.10 (wide-ATR, narrower buffer); DOGE=0.15; BNB inherits global 0.2.
- `session_filter_per_symbol`: SOL + DOGE=[london] only. BNB inherits global (london+new_york) as major.
- `min_sl_distance_pct_per_symbol`: BTC 0.004, ETH 0.008 (bumped 2026-04-21 eve), SOL 0.010, DOGE 0.008, BNB 0.005.

Adding a 6th+ pair: drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol`, extend `affected_symbols_for` in `on_chain_types.py` for chain-native tokens, watch 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`. Coinalyze free tier supports ~8 pairs at refresh_interval_s=75s; Arkham at current cadence ≤6 pairs comfortable.

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

# Diagnostic probes (ad-hoc, read-only)
.venv/Scripts/python.exe scripts/probe_open_orders.py              # OKX live positions + pending orders
.venv/Scripts/python.exe scripts/probe_arkham.py                   # Arkham API matrix check

# Tests
.venv/Scripts/python.exe -m pytest tests/ -v
```

**Pine dev cycle** (via TV MCP): write `.pine` → `tv pine set < file` → `tv pine compile` → fix → `tv pine analyze` → `tv screenshot`.

---

## Forward roadmap

Sequenced in "Pass" + "Phase" vocabulary. Pass 1 combined the original Phase 8 (data collection) + Phase 9 (GBT analysis) + a lightweight Phase 10 (Bayesian weight tuning, not deep RL). The original phase numbering survives only inside Phase 11 (live transition) and Phase 12 (post-stable experiments).

### Pass 1 — COMPLETE (2026-04-22)

Combined on a 42-trade dataset (`rl.clean_since=2026-04-19T19:55:00Z`):

- **Data collection:** demo bot ran 2026-04-19 through 2026-04-22, 42 closed trades (WR 47.6%, net +13.46R, Sharpe 0.33).
- **GBT analysis** via `scripts/analyze.py` — xgboost feature importance + SHAP + per-factor WR + rejected-signal counter-factual. Arkham segmentation descriptive only (coverage inconsistent across the window).
- **Bayesian tune** via `scripts/tune_confluence.py` — Optuna TPE over NON-Arkham knobs (confluence_threshold + 3 hard gate bools), walk-forward 73/27 split.
- **Applied tune:** `min_confluence_score` 3 → 3.75 (curve plateau; +3.8pp WR on historical sample). No other knobs changed (Arkham coverage inconsistent, per-pillar + per-TF oscillator data not yet captured — both instrumented for Pass 2).
- **Concurrent feature work:** whale hard gate removed, `flow_alignment_score` 6-input + `per_symbol_cex_flow_penalty` soft signals live, `whale_transfers` + `confluence_pillar_scores` + `oscillator_raw_values (1m/3m/15m)` journal instrumentation shipped. See changelog 2026-04-22 entry.

### Pass 2 — Data collection (post-restart, active)

**Goal:** accumulate a uniform-feature dataset. Every new row post-restart carries full Arkham context + per-pillar scores + per-TF oscillator numerics + whale-transfer time-series. 5-day window targeted before Pass 2 tune runs.

- Operator restarts bot with fresh DB (backup preserved as `data/trades.db.pass1_backup_*`).
- `rl.clean_since` bumped to restart-timestamp.
- Demo bot runs. No code changes unless factor-audit reveals a regression.
- Run `scripts/factor_audit.py` every ~10 closed trades.
- Run `scripts/peg_rejected_outcomes.py --commit` daily.
- Passive accumulation of `on_chain_snapshots`, `whale_transfers`, per-pillar + per-TF oscillator journal rows.

**Gate to leave:** ≥30 closed trades, Arkham `on_chain_context` populated on 100% of rows, `confluence_pillar_scores` populated on 100%, `oscillator_raw_values` populated on ≥90% for each TF, net PnL ≥ 0, WR ≥ 45%.

**If the gate fails:** factor-audit is diagnostic. Expect 1-2 iterations of per-symbol confluence threshold tuning before the gate holds. Do NOT start Pass 3 until the gate holds — overfitting a broken dataset is worse than collecting more clean data.

### Pass 3 — Full Bayesian tuning on uniform data

**Goal:** tune every knob Pass 1 deferred. Arkham coverage is now uniform; per-pillar + per-TF oscillator columns unlock richer continuous feature space.

**Tunable knob set (all via Optuna TPE + walk-forward):**
- Arkham modifier deltas: `daily_bias_modifier_delta`, `stablecoin_pulse_penalty`, `altcoin_index_penalty`.
- Flow alignment: `flow_alignment_penalty`, `flow_alignment_noise_floor_usd`, plus all 6 input weights (stables, BTC, ETH, Coinbase, Binance, Bybit — currently hardcoded 0.25/0.25/0.15/0.15/0.10/0.10).
- Per-symbol CEX flow: `per_symbol_cex_flow_penalty`, `per_symbol_cex_flow_noise_floor_usd`.
- Per-pillar weights (5 pillars × continuous) using `confluence_pillar_scores` column.
- Per-symbol confluence thresholds (Pass 1 kept global at 3.75).
- 3 hard gate toggles (vwap_hard_veto, ema_veto, cross_asset_opposition).

**Method:** extend `scripts/replay_decisions.py` (scaffold already present) with pillar-reweight + Arkham-modifier replay paths. Run `scripts/tune_confluence.py` with expanded `suggest_config`.

**GBT re-run:** `scripts/analyze.py` auto-expands feature matrix when `oscillator_raw_values` non-empty; Pass 3 GBT gets continuous features (WT magnitude, RSI position, Stoch K/D, momentum) + Arkham segments (now trustworthy with uniform coverage) + whale-transfer derived features (via join).

**Gate to leave:** Pass 3 Optuna OOS net_R ≥ 0.5 × IS net_R AND OOS WR ≥ IS WR − 5pp. Otherwise structural ceiling — hold on tuning, collect more data, proceed to Phase 11 stability rather than over-fitting a small dataset.

### Phase 11 — Live transition + scaling

**Goal:** move from demo to live with survivable sizing, scale by performance.

- **Live transition:** new OKX live account (sub-account recommended). Start `RISK_AMOUNT_USDT=$10-20`, `max_concurrent_positions=2`, cross margin, explicit notional cap.
- **Stability period:** 2 weeks / 30 live trades with no code changes. Compare live WR + avg R to demo baseline within ±5%.
- **Scaling rules:** only after 100 live trades. Double `RISK_AMOUNT_USDT` only if 30-day rolling WR ≥ demo WR − 3% AND drawdown ≤ 15%. Asymmetric: halve on any 10-trade rolling WR < 30%.
- **Monitoring:** journal-backed dashboard (pure-Python or Streamlit). Alert on: drawdown >20%, 5-loss streak, OKX 429, fill latency P95 >2s, daily realized PnL < -2R, Arkham credit usage >80%/month.

### Phase 12 — Future enhancements (post-stable)

Candidates, **not commitments.** Re-evaluate after Phase 11 stability.

- **Deep RL (SB3/PPO) parameter tuner** — requires 100+ live-trade dataset. Phase 10 original deep-RL scope was superseded by Pass 1/3 Bayesian TPE which handles 6D-10D parameter search natively. Deep RL only if Bayesian plateau hits a structural ceiling AND the high-dim interaction effects are measurable.
- **Arkham F4/F5** — per-entity flow divergence (Coinbase premium delta vs Binance inflow) + DEX swap volume. Deferred at integration; revisit if Pass 3 shows per-entity netflow alone has edge.
- **Asymmetric Arkham penalties** — split symmetric penalties into `long_penalty` / `short_penalty` knobs. Depends on Pass 3 data showing direction asymmetry.
- **Per-symbol Arkham overrides** — SOL vs DOGE may respond differently to BTC dominance / altcoin index. Pass 3 candidate.
- **Whale transfer directional classification** — GBT on `whale_transfers` join reveals which flows predict direction. If signal exists, add `whale_directional_score` soft factor (replacement for the removed hard gate in a data-informed form).
- **HTF Order Block re-add** — Pine 3m OBs failed post-pivot; 15m OBs may survive. Factor-audit confirms before re-enable.
- **Additional pairs** — 6th+ OKX perp. Coinalyze budget allows ~6 symbols at free tier.
- **1m as zone source in `setup_planner`** — `ltf_fvg_entry` / `ltf_sweep_retest`. Pass 3 GBT confirms 1m factors carry weight first.
- **1m-triggered dynamic trail / runner management** — dynamic exit after TP1 using 1m oscillator. Complements `ltf_reversal_close`.
- **ATR-trailing SL after MFE threshold (Option B)** — continue trailing after 1.3R lock. Only if Option A's locked-and-fell-back data shows a meaningful "resumed then reversed" bucket.
- **Pine overlay split** — `smt_overlay.pine` → `_structure.pine` + `_levels.pine`. Worth the refactor only if freshness-poll latency becomes a bottleneck.
- **Multi-strategy ensemble** — scalper + swing module routing to shared execution layer. Only meaningful once scalper is provably stable.
- **Auto-retrain loop** — monthly Optuna refresh on rolling window. Cron + CI pipeline. Meaningless until Phase 11 is steady.
- **Alt-exchange support** — Bybit / Binance futures. Current execution layer OKX-specific; abstracting `ExchangeClient` is 2-3 weeks careful refactor.

### What is explicitly NOT on the roadmap

- **Decision-making RL.** Structural decisions (5-pillar, hard gates, zone-based entry, per-symbol flow) stay fixed. Bayesian / RL are parameter tuners only.
- **Claude Code as runtime decider.** Claude writes code and analyzes logs; it does not decide trades per candle.
- **Sub-minute entry TFs (1m / 30s).** TV freshness-poll latency makes these unreliable. Infrastructure rewrite (direct exchange WS + in-process indicators) would be a different project.
- **Leverage > 100x or non-cross margin modes.** Operator cap + OKX cap combine to forbid. Requires risk memo to revisit.

---

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, may break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. `--profile demo` first. Never enable withdrawal. Bind key to machine IP. Sub-account for live.

**Arkham:** read-only API, no trade-path exposure. `ARKHAM_API_KEY` stored in `.env` only. Credit budget ~7k/month at current cadence (10k trial quota). Monitor dashboard for runaway usage; auto-disable at 95% is a safety net, not primary.

**Trading:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital.

**RL:** overfitting is the #1 risk — walk-forward is mandatory. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if a structural ceiling is evident.
