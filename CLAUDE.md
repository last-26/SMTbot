# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on OKX. Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes. Demo-runnable end-to-end today; the near-term goal is to collect a clean dataset, then learn from it.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, trains RL, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, OKX = hands, Python = brain.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 5 OKX perps — `BTC / ETH / SOL / DOGE / BNB`. 5 concurrent slots on cross margin (all active, no queue).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights. *Premium/discount gate and HTF TP/SR ceiling temporarily disabled 2026-04-19 — see changelog; P/D to be re-enabled as a soft/weighted factor (~10-15%) post-Phase-9, HTF ceiling re-evaluated after Phase 9 GBT.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. Single-leg OCO SL/TP at hard **1:2 RR** (tightened 1:3→1:2 on 2026-04-21 eve; partial TP disabled 2026-04-19 late-night — see changelog; `move_sl_to_be_after_tp1` flag kept but inert while partial off). Dynamic TP revision re-anchors the runner OCO to `entry ± 2 × sl_distance` every cycle, floor at 1.0R. **MFE-triggered SL lock (Option A, 2026-04-20)**: once MFE ≥ 1.3R (scaled from 2R when RR cap tightened), the runner OCO's SL is pulled to entry (+fee buffer) so the remaining 0.7R of target is risk-free. One-shot per position. **Maker-TP resting limit (2026-04-20)**: post-only reduce-only limit sits at TP price alongside the OCO — captures wicks as maker, avoids market-trigger latency.
- **Sizing:** fee-aware ceil on per-contract total cost so total realized SL loss (price + fee reserve) ≥ target_risk across every symbol (2026-04-19 late-night-2). Overshoot bounded by one per-contract step (< $3 per position). Operator override via `RISK_AMOUNT_USDT` env (2026-04-20) bypasses percent-mode sizing; 10%-of-balance safety ceiling.
- **Journal:** async SQLite, schema v3 (+ `on_chain_context`, `demo_artifact`). `rejected_signals` table with counter-factual outcome pegging. Separate `on_chain_snapshots` time-series table captures every Arkham state mutation for Phase 9 trade-lifetime joins.
- **On-chain (Arkham):** integrated end-to-end (daily bias ±15%, hourly stablecoin pulse +0.75 threshold penalty, altcoin-index +0.5 penalty on misaligned altcoin trades, whale blackout hard gate 100M+ / 10 min). Credit-safe via v2 persistent WS streams. Weights tuned 1.5× 2026-04-21 (eve) for visibility; see changelog for re-eval triggers.
- **Tests:** 946, all green. Demo-runnable end-to-end.
- **Data cutoff (`rl.clean_since`):** `2026-04-19T19:55:00Z` (bumped after ceil sizing flipped — realized-R distribution shifts from clustered-below-target to clustered-at-or-above-target). Arkham activation did NOT bump — dataset segments by `arkham_active` categorical.

---

## Changelog

### 2026-04-21 (eve, late-2) — `on_chain_snapshots` time-series table (Phase 8 data layer)

- **Trigger:** operator flagged that on-chain context is frozen at entry-time on each `trades` row — pozisyon ömrü boyunca bias flip / whale event / pulse misalignment görünmez. Exit-time snapshot ayrı kolonda tutmak az değer (causality yok, kapanış deterministik). Daha temiz çözüm: ayrı time-series tablo.
- **New table — `on_chain_snapshots`:** `captured_at`, `daily_macro_bias`, `stablecoin_pulse_1h_usd`, `cex_btc_netflow_24h_usd`, `cex_eth_netflow_24h_usd`, `coinbase_asia_skew_usd`, `bnb_self_flow_24h_usd`, `altcoin_index`, `snapshot_age_s`, `fresh`, `whale_blackout_active`. Index on `captured_at`. Created via `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` (no ALTER migration — same idempotent startup path hits new + existing DBs).
- **Dedup-gated writer.** Runner's `_refresh_on_chain_snapshots` calls `_maybe_record_on_chain_snapshot` at tail. Fingerprint tuple `(bias, pulse, btc_flow, eth_flow, coinbase_skew, bnb_flow, altcoin_idx, fresh, whale_blackout_active)` compared against `ctx.last_on_chain_snapshot_fingerprint`. Match → skip; differ → single insert + fingerprint update. `snapshot_age_s` deliberately NOT in fingerprint (grows every tick; would churn ~1 row / 180s even on no-op ticks). `fresh` bool IS in fingerprint — staleness flip is a meaningful observation. Journal failures log + swallow via `arkham_snapshot_journal_failed`.
- **Expected cadence:** ~hourly pulse + hourly altcoin-index + once-per-UTC-day bias → 2-3 rows/hour typical, ~72/day, ~2200/month. Well inside SQLite comfort zone.
- **Phase 9 usage (documented intent, not shipped).** Analysis scripts will join `trades` onto this table via `entry_timestamp ≤ captured_at ≤ exit_timestamp`, reconstructing "which on-chain regimes did this trade live through." Enables hypothesis: "entry'de bullish bias → mid-trade flip → did outcome change?" GBT features: bias-flip-during-lifetime bool, whale-event-during-lifetime bool, pulse-sign-change-during-lifetime bool. NO runtime reaction mechanism yet — Phase 12 candidate pending Phase 9 signal validation.
- **Dataset:** `rl.clean_since` **unchanged**. Additive read-only table, zero entry/exit geometry change. Existing `on_chain_context` on trade rows preserved (complementary — entry snapshot vs lifetime journey).
- **Re-eval triggers:**
  1. Row count growth. <1 row/hour average over 24h = dedup too aggressive OR Arkham fetcher failing. Inspect `arkham_*_refreshed` log lines vs table row count.
  2. Phase 9 signal. If `bias_flipped_during_lifetime` segment shows < 5% outcome delta vs `no_flip` segment, reactive mechanism isn't worth building. If > 10%, promote to Phase 12 design.
- **Tests:** 953 passing (+7: 2 journal, 5 runner). `test_refresh_dedups_unchanged_snapshot` locks the no-churn contract.

### 2026-04-21 (eve, late) — Hard RR 1:3 → 1:2 + ETH SL floor widening

- **Trigger:** operator review of early session. Wins were wicking TP and reversing before the market-trigger TP could fill (maker-TP limit now handles this but 3R TP still far from typical post-fill momentum envelope). ETH losses were hitting the 0.6% floor too often — stops writing faster than 3m noise envelope.
- **Fix — `config/default.yaml` + pydantic defaults:**
  - `execution.target_rr_ratio: 3.0 → 2.0` (hard RR cap)
  - `trading.default_rr_ratio: 3.0 → 2.0` (pre-zone fallback; test guard renamed `test_default_yaml_runner_tp_is_hard_1_2`)
  - `execution.sl_lock_mfe_r: 2.0 → 1.3` — 2R threshold would coincide with 2R TP (lock never fires). 1.3R preserves "65% of the way to TP" proportion (old was 2R/3R = 67%).
  - `execution.tp_min_rr_floor: 1.5 → 1.0` — under 1:2, a 1.5R floor would bind on nearly every revise.
  - `min_sl_distance_pct_per_symbol.ETH-USDT-SWAP: 0.006 → 0.008` — DOGE level, still below SOL's 0.010. Notional auto-shrinks to keep $R flat (per SL widening contract).
- **Expected behavior change:**
  - Winners: TP closer, higher fill rate (maker-TP or market-trigger). Full-win payout 3R → 2R. Break-even WR 25% → 33%.
  - MFE lock fires at 1.3R instead of 2R — "almost-win → BE" more sensitive. More trades locked to BE; fewer "locked and fell back to BE" vs "walked on to TP" bucket (since TP is now closer).
  - ETH: wider stop envelope. Same $R, smaller notional. Fewer false stops from noise.
- **Safety rails:** hard-RR test (`test_default_yaml_runner_tp_is_hard_1_2`) locks the contract — if anyone drifts one value without the other, test fails at CI. MFE lock + floor scaled together so lock still fires before TP and revise still has headroom.
- **Dataset:** `rl.clean_since` **unchanged**. Exit-geometry tune (R distribution re-centered at 2R max instead of 3R max), same entry contract. Factor-audit will pick up the regime shift via `target_rr_ratio` categorical.
- **Re-eval triggers:**
  1. Win-rate post-flip. Old break-even (25%) was challenging; 33% should be within reach but tighter. If WR post-flip < 30%, tighten confluence threshold or re-examine zone sources.
  2. Avg realized R on wins. Should land 1.8-2.0R (with maker-TP, closer to 2.0). Anything systematically below 1.5R suggests early TP revise shrinking it further — bump `tp_revise_min_delta_atr`.
  3. MFE-lock fire rate. Expected up vs old config (threshold lowered 35%). If >80% of trades lock, consider bumping to 1.5R; if <40%, reconsider threshold.
  4. ETH stop-out rate on bias-aligned trades. Should drop 15-25% with wider floor. If unchanged, the issue wasn't floor-related; re-eval zone sources for ETH.
- **Tests:** 946 passing. Guard test renamed + asserts 2.0.



- **Trigger:** first post-activation observation — all 5 bullish-day shorts cleared confluence despite Arkham penalties (bias ×0.9 + pulse +0.5 threshold). Operator flagged that 10% bias + 0.5 penalty on a raw 5.0+ score is effectively ignored; if this stays invisible in data, Phase 9 GBT won't have signal to learn from.
- **Fix — `config/default.yaml` + `src/bot/config.py` defaults:**
  - `daily_bias_modifier_delta: 0.10 → 0.15` (bullish short penalty ×0.85 instead of ×0.90)
  - `stablecoin_pulse_penalty: 0.5 → 0.75` (misaligned threshold 3.0 → 3.75)
- **Expected behavior change:** raw confluence under ~4.2 on misaligned trades now rejects (vs ~3.7 before). Today's 5 shorts (raw 4.96-6.20) still pass, but setups in the 4.0-4.5 raw band become the first Arkham-induced rejects. Effective handicap on misaligned trades ~22% (was ~15%).
- **Re-eval trigger (data-driven tune):** after 30 closed trades with Arkham flags on, check journal for `below_confluence` rejects where `on_chain_context` shows misalignment:
  - Penalty-induced reject fraction `<5%` of total → bump 2× (delta=0.30, penalty=1.0)
  - `5-30%` → hold current values
  - `>30%` → drop back to 0.10 / 0.5 (over-filtering)
- **Dataset:** `rl.clean_since` unchanged. Quantitative tune within same feature-set; Phase 9 GBT segments by config hash as well as `arkham_active`. Tests: 1 updated (default-value assertion), 1 adjusted (override test now uses 0.20 as non-default). Full suite 946 passed.

### 2026-04-21 — Arkham on-chain integration (Phase A-E + F1-F3 + v2 WS migration)

Single-day five-phase integration. Full commit-level detail in `git log --grep="Arkham"`. This consolidates what matters for future decisions.

**Phases shipped:**
- **A — foundation.** `ArkhamClient` (httpx.AsyncClient, token-less rate limiting, header usage parsing, 95% auto-disable). `OnChainSnapshot`, `WhaleBlackoutState` (extend-never-shorten), `affected_symbols_for()` (stablecoins fan out, chain-natives collapse). `OnChainConfig` with master + 4 sub-feature flags (default False in pydantic, True in `config/default.yaml`). Journal schema: `on_chain_context TEXT` on trades + rejected_signals (idempotent ALTER). API key via env only.
- **B — snapshot pipeline + journal enrichment.** Scheduler runs once per tick, shared across symbols. UTC-day-rollover for daily, monotonic cadence for pulse + altcoin index. Fetch failure keeps last-known snapshot; `fresh` flag degrades gate downstream. `_on_chain_context_dict` writes JSON to every journal row.
- **C — daily bias modifier.** Applied in `calculate_confluence` top-level. Bullish → long × (1+δ), short × (1-δ). Delta ∈ [0.0, 0.5] enforced at config load. Stale snapshot → (1.0, 1.0).
- **D — whale blackout hard gate.** `ArkhamWebSocketListener` mirrors LiquidationStream pattern. 3-strike consecutive-failure disable. Gate sits between `cross_asset_opposition` and `crowded_skip`. Stablecoin events fan out to all 5 symbols; chain-native collapses to one.
- **E — stablecoin pulse penalty.** Pure helper `_stablecoin_pulse_penalty`. Misaligned (long + outflow or short + inflow) → +penalty on threshold. Rejects under existing `below_confluence` string (not new reason) — factor-audit segments by context.
- **F1 — hourly pulse via `/transfers/histogram`.** Previously stub (Arkham's `entity_balance_changes` only supports 7d/14d/30d). Two histogram calls (flow=in + flow=out), 1.1s cushion (1 req/s rate limit). `base=type:cex` captures every CEX in one query. None on leg failure, 0.0 on empty buckets.
- **F2 — altcoin-index penalty.** `/marketdata/altcoin_index` scalar 0-100. Penalty bump applies ONLY to altcoins (not BTC/ETH), only on misaligned direction (long alt in BTC-dominance OR short alt in altseason). Asymmetric by design.
- **F3 — 24h daily bias.** Rebuilt on `/transfers/histogram` with `granularity=1d, time_last=24h` (vs 7d minimum of `entity_balance_changes`). 4 calls per refresh (stables-in/out, BTC-in/out). ETH netflow dropped (was informational).
- **v2 WS migration (critical credit fix).** Operator dashboard revealed `POST /ws/sessions` (v1) costs 500 credits/call — 2 probes burned 92.5% of 30-day quota. Migrated to `POST /ws/v2/streams` (persistent, ~0 creation fee). `data/arkham_stream_id.txt` (gitignored) persists stream id across restarts. Startup: read cache → `GET /ws/v2/streams` verify → reuse or create+persist. `stop()` leaves stream in place. v1 methods kept as deprecated with docstring warnings.

**Key API-shape discoveries (undocumented but enforced):**
- `orderBy` is REQUIRED on `/intelligence/entity_balance_changes` (400 without it).
- `interval` only accepts `7d`, `14d`, `30d`.
- WS v1 subscribe envelope: `{"id":"1","type":"subscribe","payload":{"filters":{"usdGte":N,"tokens":[...]}}}`.
- `tokens` param on histogram = comma-joined string; on `entity_balance_changes` = repeated query params.
- WS v2 has NO subscribe message — filters baked into the stream at creation.

**Credit budget (post-v2):** ~7k credits/month at current cadence. Inside 10k trial quota. Dropping `stablecoin_pulse_refresh_s` 1h → 3h brings it to ~2.2k/month.

**Dataset contract:** `rl.clean_since` NOT bumped for Arkham activation. Phase 9 GBT segments by `arkham_active=true` + `on_chain_context IS NOT NULL`, not by timestamp cut. This is the deliberate divergence from the usual flip-protocol.

**Re-evaluation triggers:**
1. `on_chain_context IS NOT NULL` fraction on new rows should be ≥90% when master on. Lower = fetcher failure path being hit (investigate rate budget / API reachability).
2. Penalty-induced `below_confluence` fraction — see 2026-04-21 eve weight-bump entry for current thresholds.
3. Whale blackout frequency. <1/week = threshold too high (raise to $200M). >5/hour during bull runs = threshold too low (drop to $250M).
4. Locked-and-fell-back % after daily-bias modifier — if bullish-day shorts consistently hit SL and bearish-day longs consistently hit TP, modifier is anti-correlated with edge (turn off).

**Rollback:** flip `on_chain.enabled: false` + restart. Zero log lines from `arkham_*`, `state.on_chain=None`, new journal rows write `on_chain_context=NULL`, historical rows preserved.

**Not in scope / Phase 12 candidates:**
- F4 per-entity flow divergence (`/flow/entity/{entity}` — Coinbase premium pattern). Macro horizon mismatch with scalp. Deferred.
- F5 swap volume (`/swaps` — DEX activity). Indirect for futures. Deferred.
- Asymmetric penalty direction (long-only or short-only). Symmetric by default until data shows asymmetry.
- Per-symbol altcoin-index override (SOL vs DOGE respond differently to BTC dominance).

### 2026-04-21 — VWAP-band zone anchor (Convention X, 0.7/0.3)

- **Trigger:** operator flagged that `_vwap_zone` limit landed at 0.5σ midpoint (arbitrary 50/50 between VWAP and ±1σ band). Wanted Fib-lite anchor pulling limit closer to VWAP on directional side — catches pullback before full retrace.
- **Fix:** `AnalysisConfig.vwap_zone_long_anchor: 0.7` (validator [0.5, 1.0]) + `vwap_zone_short_anchor: 0.3` (validator [0.0, 0.5]). Zone midpoint: `low + (2·long_anchor − 1)·(high − low)` for long; `low + 2·short_anchor·(high − low)` for short. Default formula preserved for legacy callers (no-arg). `liq_pool_near` still uses plain midpoint (cluster IS target).
- **Geometry:** long limit now at VWAP + 0.4σ (was +0.5σ); short at VWAP − 0.4σ. ~10% of band-width tighter pullback required. Per-unit R improves when filled.
- **Safety rail:** validator enforces entry stays on correct structural side of VWAP. Typo `long_anchor=0.3` would place long BELOW VWAP and break SL-past-zone contract — rejected at config load.
- **Re-eval (≥30 trades):** vwap_retest fraction of zone-source selection, avg-R vs other sources, whether EQ-hug (0.65/0.35) or outer-band (0.8/0.2) tune better.

### 2026-04-21 — Pending zone timeout 10 → 7 bars (21 min)

- **Trigger:** on 3m TF, 30-minute fill window exceeded zone half-life for scalp-native sources (`vwap_retest`, `ema21_pullback`, `fvg_entry`). Operator requested tighter 21-min window.
- **Fix:** `execution.zone_max_wait_bars: 10 → 7` (YAML + pydantic + `setup_planner.build_zone_setup` default). `algoritma.md` §5, §10, §12 synced.
- **Expected:** 2-3 bar retrace fills unaffected (majority). 7-10 bar "slow retrace" group now cancels, confluence re-evaluated next cycle with fresh zone. No R spent on cancels.
- **Re-eval (≥30 trades):** `zone_timeout_cancel` reject ratio. >30% = too aggressive (bump to 8); <10% = can tighten further (5-6). Per-source cadence may be warranted (1m sources 3-4 bars, 3m 7, HTF longer).

### 2026-04-20 — Execution hardening day (5 fixes, one dev day)

Five fixes shipped same day targeting execution correctness. Full commit-level detail in `git log`.

**MFE-triggered SL lock (Option A).** When MFE ≥ 2R, cancel+replace runner OCO with SL at entry+fee_buffer. One-shot flag (`_Tracked.sl_lock_applied`) prevents retry spin. Direction guard (long `new_sl < tp`, short `>`) prevents wrong-side tightening. Failure handling mirrors `revise_runner_tp`. Config: `sl_lock_enabled=true`, `sl_lock_mfe_r=2.0`, `sl_lock_at_r=0.0` (BE+fee_buffer; >0 = locked profit). Purpose: eliminate "almost-win" round-trip -1R losses observed on trades that wicked near TP then reversed. **Re-eval (≥30 trades):** fire frequency (<30% = threshold too high, bump down to 1.5R); distribution of realized R on locked trades (should bimodal — near-0R or 3R); locked-and-fell-back % (>60% = lock load-bearing; <30% = neutral insurance).

**Resting TP limit alongside OCO (maker-TP, wick capture).** `tp_resting_limit_enabled=true`. On every bot-opened position, two TP orders live: OCO (market-on-trigger, fallback) + post-only reduce-only limit at TP (maker, primary). `clOrdId` prefix `smttp` distinguishes from entry limits (`smtbot`). `revise_runner_tp` cancels+replaces in lockstep with OCO; `lock_sl_at` leaves TP limit untouched. Fixes trade `414b4ca5`-class wick-and-reverse where market-on-trigger missed the fill. Startup rehydrate re-places fresh TP limit for every non-BE journal OPEN row (orphan-sweep wipes resting limits, rehydrate regenerates). **Re-eval (≥30 trades):** fraction of wins with TP-limit-fill vs OCO-market-trigger (>70% validates); exit price ≤ planned TP on wins (no slippage).

**Phantom-cancel orphan fix.** `poll_pending` + `cancel_pending` formerly emitted CANCELED and dropped pending row even on non-idempotent cancel failure (generic exception or sCode 50001). Now only pops row on (a) success or (b) idempotent-gone `{51400, 51401, 51402}`. Non-gone failures preserve row for next poll retry. `cancel_pending` re-raises for caller visibility. Root cause of three orphan limits during brief OKX outages that later filled into unprotected positions.

**Stale-algoId + startup reconciliation.** `revise_runner_tp` now calls `_on_sl_moved` callback (mirrors `lock_sl_at`), so journal `algo_ids` stays in sync with in-memory state across restart. Startup reconcile gained two passes: `_cancel_orphan_pending_limits` (cancels all resting limits — they're orphan by construction pre-first-tick) + `_cancel_surplus_ocos` (compares live OCOs vs journal, cancels surplus). Fixes DOGE 2-OCO stacking bug where stale journal pointed to a dead algo while live orphan sat unmanaged.

**Flat-USDT $R override + zone-resize ceil parity.** New `trading.risk_amount_usdt` / `RISK_AMOUNT_USDT` env override bypasses `balance × risk_pct` sizing. Safety rail: override ≤ 10% of account balance (ValueError at config load on exceed). `setup_planner.apply_zone_to_plan` flipped floor→ceil for contract rounding (matches 2026-04-19 `rr_system` ceil), eliminating residual $2-$13 spread from floor-rounding in zone re-size path. Operator playbook: demo `RISK_AMOUNT_USDT=50`; bump manually as balance grows (e.g., $8k → $75).

**Process learning:** *code commit ≠ live behavior.* Fix not applied until process restart. Added to operator contract.

### 2026-04-19 (late night) — Fee-aware ceil sizing (equal USDT SL/TP across symbols)

- **Trigger:** post-partial-disable, per-position `risk_amount_usdt` still varied $40-$54 on $55 target. Root cause: `int(notional // contracts_unit_usdt)` floor-rounding truncates harder on high-price symbols (BTC ctu≈$680) than fine-step (DOGE ctu≈$0.35).
- **Fix — `rr_system.py`:** un-capped path uses `num_contracts = ceil(max_risk / per_contract_cost)` where `per_contract_cost = (sl_pct + fee_reserve_pct) × contracts_unit_usdt`. Realized SL loss (price + fee reserve) clears target; overshoot bounded by one per-contract step (<$3 per position). Capped path (leverage/margin ceiling) still floors — respecting the hard cap wins over equal-risk. `actual_risk_usdt` journal field stays price-only (not effective) so RL rewards compare clean.
- **Dataset:** `rl.clean_since` bumped `2026-04-19T17:35:00Z → 2026-04-19T19:55:00Z`. Realized-R distribution flipped from left-skewed-below-target to clustered-at-or-above. Mixing the two would blur expectancy math.
- **Re-eval (≥30 trades):** `risk_amount_usdt` distribution clusters ≥ target with tail bounded at `target + per_contract_cost`. Flat-below = ceil not engaging (capped-path dominance). sCode 51008 incidence should stay at zero (ceil bumps notional slightly but margin buffer has headroom).

### 2026-04-19 — Scalp-native pivot series (consolidated)

Single-day rewire sequence. Detailed commits preserved in git log (`git log --oneline --grep="2026-04-19"`). 2026-04-20 MFE-lock and 2026-04-19 (late night, cont. #2) ceil-sizing kept verbatim above — re-evaluation still pending; everything below is stable.

**Scalp-native rewire (morning):**
- Zone priority: `vwap_retest → ema21_pullback → fvg_entry (3m) → sweep_retest → liq_pool_near`. HTF 15m FVG demoted to opt-in.
- New sources: `ema21_pullback` (EMA21/55 stack + price within `zone_atr × ATR` of EMA21), entry-TF `fvg_entry`.
- Liquidity flipped from entry-driver to TP-driver. `liq_pool_near` gated by `liq_entry_near_max_atr=1.5` + notional `≥ 2.5× side-median`.
- Weights rebalanced toward oscillator/overlay (`vwap_composite=1.25`, `money_flow=1.0`, `osc_HCS=1.5`, `divergence=1.25`); structure weights trimmed. Candle buffer `last(50) → last(100)` for EMA55 SMA-seed.
- TP ladder (`tp_ladder_enabled=true`, shares `[0.40, 0.35, 0.25]`) — inert because `partial_tp_enabled=false` (disabled same day).

**Gate changes (sequential):**
- `analysis.premium_discount_veto_enabled: true → false` — range-bound tape rejected every zone on `wrong_side_of_premium_discount`. Re-enable post-Phase-9 as soft/weighted factor (~10-15% weight).
- `analysis.htf_sr_ceiling_enabled: true → false` — hard 1:3 + tight 15m levels killed nearly all longs via `htf_tp_ceiling`; flag also gates `_push_sl_past_htf_zone`. `min_sl_distance_pct_per_symbol` floors still primary wick protection. Re-evaluate Phase-9; consider splitting flag into TP-ceiling vs SL-push.

**Unprotected-position hardening (pm):**
- **Zone SL floor re-apply** — `apply_zone_to_plan(min_sl_distance_pct=…)` re-widens structural SL past per-symbol floor (mirrors entry_signals widening; R flat via notional re-size).
- **Pre-attach mark-vs-SL guard** — `runner._handle_pending_filled` reads mark before `attach_algos`; if already breached, skip + best-effort close.
- **Coinalyze 429 non-blocking** — `self._rate_pause_until` replaces `asyncio.sleep(retry_after)` (event loop no longer stalls 57s).
- **Inline pending drain** — `run_once` drains pending between symbols, not once-per-tick; fill→OCO-attach latency 180s → <10s.
- **Attach-failure log enrichment** — `OrderRejected.code` + `.payload` surfaced.
- Symbol count 7→5 (dropped DOGE+XRP; per-slot margin +40%). Later ADA↔DOGE swap (`sCode 54031` OI cap). Per-symbol overrides for absent symbols kept in YAML.

**Hard 1:3 RR cap + dynamic TP revision (night):**
- `apply_zone_to_plan(target_rr_cap=3.0)` — zone-derived TP force-clamped to `entry ± 3 × sl_distance`. `execution.target_rr_ratio` + `trading.default_rr_ratio` both 3.0 (guarded by `test_default_yaml_runner_tp_is_hard_1_3`).
- `PositionMonitor.revise_runner_tp` — runner OCO cancel+place per cycle, gates: `tp_revise_min_delta_atr=0.5`, cooldown 30s, floor 1.5R. BE-aware via `_Tracked.sl_price`.

**VWAP band-based zone (night):**
- Pine `ta.vwap(src, anchor, stdev_mult=1.0)` emits `vwap_3m_upper/lower` in SMT Signals.
- `_vwap_zone` uses bands when 3m VWAP is nearest; zone mid = `vwap ± 0.5σ`. ATR fallback when Pine bands missing.
- Entry distance from market `0.77-1.54%` → `0.52-0.63%`.

**Partial TP disabled (late night):**
- `execution.partial_tp_enabled: true → false`. Full-win payout 2.25R → 3R; "almost-win" +0.75R bucket gone (TP1-reversal now full -1R). Break-even WR shift 22% → 25%.
- `move_sl_to_be_after_tp1` flag kept but inert. Runner coverage bumped to full `num_contracts`. Existing split positions keep 2-OCO structure until closed.

**TP-revise hardening + demo-wick artefact cross-check (late night):**
- **Immutable `plan_sl_price`** — `_Tracked` preserves plan SL distance for dynamic TP math even after SL-to-BE mutates `sl_price`. Sentinel `0.0` = unknown, disables revise.
- **51400 verify-before-replace** — `OKXClient.list_pending_algos` + `_verify_algo_gone` confirms algo truly absent after idempotent cancel code before placing replacement OCO (prevents double-stops).
- **Mark-price SL/TP triggers** — `place_oco_algo(trigger_px_type="mark")` on all OCO paths. Demo last-price-only wicks no longer fire.
- **Binance artefact cross-check** — new `BinancePublicClient.get_kline_around`; `_cross_check_close_artefacts` validates entry+exit inside concurrent Binance USD-M 1m candle (tolerance 5 bps). Journal schema v3 adds `demo_artifact`, `artifact_reason`. `scripts/report.py --exclude-artifacts`.

**`vwap_1m_alignment` re-opened at 0.2 (eve):** low-weight probe for Phase-9 GBT to evaluate per-TF VWAP alpha independent of composite.

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
- `src/strategy/` — R:R math, SL hierarchy, entry orchestration (+ **stablecoin-pulse / altcoin-index penalties + whale-blackout gate**), **setup planner** (zone-based limit-order plans), cross-asset snapshot veto, risk manager.
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

`displacement_candle` · `ema_momentum_contra` · `vwap_misaligned` · `cross_asset_opposition` (altcoin veto when BTC+ETH both oppose) · `whale_transfer_blackout` (on-chain, 100M+ CEX↔CEX transfer, 10min window). *`premium_discount_zone` and `htf_tp_ceiling` are wired but currently disabled — see changelog 2026-04-19.*

### Arkham modifiers (soft, additive to threshold)

- **Daily bias** — bullish/bearish classification from 24h CEX BTC netflow + stablecoin balance changes. Confluence multiplier ×(1±δ) on long/short; δ=0.15 default (bumped from 0.10 on 2026-04-21 eve).
- **Stablecoin pulse** — hourly USDT+USDC CEX netflow. Misaligned (long + stables leaving OR short + stables arriving) bumps `below_confluence` threshold by 0.75.
- **Altcoin index** — 0-100 scalar. ≤25 (BTC-dominance) penalises altcoin longs; ≥75 (altseason) penalises altcoin shorts. Bumps threshold by 0.5. BTC/ETH trades exempt.

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

**Reject reasons (unified):** `below_confluence`, `no_setup_zone`, `wrong_side_of_premium_discount`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `whale_transfer_blackout`, `session_filter`, `macro_event_blackout`, `crowded_skip`, `no_sl_source`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split`, `zone_timeout_cancel`, `pending_invalidated`. Sub-floor SL distances are **widened**, not rejected. Every reject writes to `rejected_signals` with `on_chain_context` JSON (when Arkham master on).

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
- **On-chain failures isolate.** Arkham snapshot None / stale / master-off → modifiers multiply 1.0, penalties add 0, whale gate never fires. Pre-Arkham behavior preserved.
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

The bot is ready to run. Everything below is sequenced and gated — don't skip steps.

### Phase 8 — Data collection (active)

**Goal:** accumulate a clean post-pivot dataset. No code changes unless factor-audit reveals a regression.

- Run demo bot. `rl.clean_since=2026-04-19T19:55:00Z` keeps reporter + RL on post-pivot data only.
- Run `scripts/factor_audit.py` every ~10 closed trades — early-warning on factor regressions before they eat the dataset.
- Run `scripts/peg_rejected_outcomes.py --commit` weekly to stamp counter-factual outcomes on rejected signals.
- `on_chain_snapshots` table passively accumulates Arkham state mutations (~2200 rows/month). Phase 9 joins this onto `trades` via `entry_timestamp ≤ captured_at ≤ exit_timestamp` to test whether mid-trade on-chain shifts (bias flip, whale event, pulse sign change) correlate with outcome. No runtime reaction until signal is validated.

**Gate to leave:** ≥50 closed post-pivot trades, WR ≥ 45%, avg R ≥ 0, ≥2 trend-regimes represented, net PnL ≥ 0.

**If the gate fails:** factor-audit output is diagnostic. Expect 2-3 iterations of per-symbol threshold / weight / veto tuning before the gate holds. Do not shortcut to GBT or RL until the gate holds.

### Phase 9 — GBT analysis

**Goal:** learn which factors and factor combos actually predict outcome on clean data.

- `scripts/analyze.py` (xgboost) on clean trades: feature importance, partial dependence plots, SHAP values per feature.
- Include `rejected_signals` counter-factuals as negative-class data — reveals which reject reasons threw away winners.
- **Arkham segmentation** — segment by `arkham_active` categorical + `on_chain_context` dict fields (daily_macro_bias, stablecoin_pulse_1h_usd, altcoin_index). Measure whether Arkham's penalties kept the bot out of losing trades or blocked winners.
- Output: per-symbol threshold / factor weight / veto threshold recommendations + Arkham weight re-tune.
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

- **Live transition:** new OKX live account (not demo migration). Sub-account recommended. Start with **$500-1000 risk capital**, `risk_pct=0.5%` (or `RISK_AMOUNT_USDT=10`), `max_concurrent_positions=2`. Cross margin with explicit notional cap.
- **Stability period:** 2 weeks / 30 trades with no code changes. Compare live WR and avg R to demo baseline within ±5%. Slippage, fill latency, and partial fills WILL differ — measure, don't assume.
- **Scaling rules:** only scale after 100 live trades. Double `RISK_AMOUNT_USDT` only if 30-day rolling WR ≥ demo WR - 3% and drawdown stays ≤ 15%. Asymmetric: downside scales faster than upside (halve on any 10-trade rolling WR < 30%).
- **Monitoring:** journal-backed dashboard (simple pure-Python or Streamlit). Alert on: drawdown >20%, 5-loss streak, OKX rate-limit 429, fill latency P95 >2s, daily realized PnL < -2R, Arkham credit usage >80%/month.

### Phase 12 — Future enhancements (post-stable)

These are candidates, **not commitments.** Re-evaluate after Phase 11 stability.

- **Arkham F4/F5** — per-entity flow divergence (Coinbase premium) + swap volume (DEX activity). Both deferred at integration time due to scalp-horizon mismatch; revisit if Phase 9 shows on-chain edge dominated by the F1-F3 set and F4/F5 could extend coverage.
- **HTF Order Block re-add** — Pine 3m OBs failed post-pivot (0% WR in Sprint 3). 15m OBs may survive; factor-audit should confirm HTF OB signal before re-enabling.
- **Pine overlay split** — `smt_overlay.pine` → `_structure.pine` + `_levels.pine`. Parallelizes TV recompute per symbol-switch. Worth the refactor only if freshness-poll latency becomes a bottleneck.
- **Additional pairs** — 6th+ OKX perp. Coinalyze budget allows ~6 symbols at free tier. Add parametrized instrument spec test + per-symbol YAML overrides + `affected_symbols_for` extension before bringing online.
- **1m as a zone source in `setup_planner`** — add `ltf_fvg_entry` and/or `ltf_sweep_retest`. Same pattern as existing sources, `max_wait_bars` likely 3-4 (not 7). Data-driven: revisit after Phase 9 GBT confirms 1m factors carry weight.
- **1m-triggered dynamic trail / runner management** — dynamic exit after TP1 using the 1m oscillator. Complements (does not replace) `ltf_reversal_close`. Revisit after 100+ post-pivot closed trades.
- **ATR-trailing SL after MFE threshold (Option B to MFE-lock)** — continue trailing SL after the 2R lock fires. `trail_atr` tuning load-bearing (too tight = shaken, too wide = reduces to Option A). Only worth the code if Option A's locked-and-fell-back data shows a meaningful "resumed to +2.5R then reversed" bucket.
- **Multi-strategy ensemble** — after scalper matures, add a separate swing module (higher TFs, different pillar weights) and route to shared execution layer. Only meaningful once scalper is provably stable.
- **Auto-retrain loop** — monthly RL refresh on rolling window. Cron + CI pipeline. Meaningless until Phase 11 is steady.
- **Alt-exchange support** — Bybit / Binance futures. Current execution layer is OKX-specific; abstracting `ExchangeClient` is 2-3 weeks of careful refactor.
- **Asymmetric Arkham penalties** — today penalties are symmetric (both directions can be penalised by misalignment). If Phase 9 shows shorts benefit more from the veto than longs, split into `long_penalty` / `short_penalty` knobs.
- **Per-symbol Arkham overrides** — e.g., SOL vs DOGE may respond differently to BTC dominance. Requires more data than current budget supports — Phase 12 candidate.

### What is explicitly NOT on the roadmap

- **Decision-making RL.** Structural decisions (5-pillar, hard gates, zone-based entry) stay fixed. RL is a parameter tuner.
- **Claude Code as runtime decider.** Claude writes code and analyzes logs; it does not decide trades per candle.
- **Higher-frequency scalping (1m/30s entry).** TV freshness-poll latency makes sub-minute entry TFs unreliable. Infrastructure rewrite (direct exchange WS feed + in-process indicators) would be a different project.
- **Leverage > 100x or custom margin modes outside cross.** Operator caps and OKX caps combine to forbid this. Do not revisit without a risk memo.

---

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, may break on TV updates → pin TV Desktop version. Data stays local.

**OKX Agent Trade Kit:** official MIT-licensed. `--profile demo` first. Never enable withdrawal. Bind key to machine IP. Sub-account for live.

**Arkham:** read-only API, no trade-path exposure. `ARKHAM_API_KEY` stored in `.env` only. Credit budget ~7k/month at current cadence (10k trial quota). Monitor dashboard for runaway usage; auto-disable at 95% is a safety net, not primary.

**Trading:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital.

**RL:** overfitting is the #1 risk — walk-forward is mandatory. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if a structural ceiling is evident.
