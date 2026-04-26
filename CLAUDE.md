# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on **Bybit V5 Demo** (UTA, hedge mode, USDT linear perps). Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes, Arkham on-chain soft signals. Demo-runnable end-to-end. Pass 1 complete 2026-04-22 on OKX; **venue migrated to Bybit on 2026-04-25** — fresh dataset collection restarts under `rl.clean_since=2026-04-25T21:45:00Z`.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, runs tuning, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, Bybit = hands, Python = brain.

**Internal symbol format note:** the codebase keeps the OKX-style symbol string `BTC-USDT-SWAP` as the canonical internal identifier across config, journal, runner state and tests. The Bybit boundary translation (`BTC-USDT-SWAP ↔ BTCUSDT`) lives inside `src/execution/bybit_client.py`. Old journal rows (Pass 1 + early Pass 2 from OKX) therefore string-match new rows on `inst_id`, and the symbol-keyed override dicts in YAML need no migration.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 5 Bybit USDT linear perps — `BTC / ETH / SOL / DOGE / XRP` (BNB swapped out for XRP on 2026-04-25 per operator preference; internal symbol format `BTC-USDT-SWAP` etc, translated at the Bybit boundary). 5 concurrent slots on UTA cross margin (collateral pool = USDT + USDC by USD value; BTC/ETH wallet stays out of collateral on demo per operator preference).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights. Confluence threshold `min_confluence_score=3.75` (Pass 1 Optuna tune, 2026-04-22). *Premium/discount gate and HTF TP/SR ceiling temporarily disabled 2026-04-19 — see changelog; re-evaluated as Pass 3 candidates.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. **Position-attached TP/SL** at hard **1:2 RR** (Bybit V5: TP/SL fields on `/v5/order/create` for market entries, `/v5/position/trading-stop` for limit-fill attach + every subsequent SL/TP mutation). No separate algo orders to track — `journal.algo_ids` stays empty on Bybit-era rows. Mark-price triggers (`tpTriggerBy=slTriggerBy=MarkPrice`) for demo-wick immunity. Dynamic TP revision re-anchors TP to `entry ± 2 × sl_distance` every cycle, floor at 1.0R. **MFE-triggered SL lock (Option A, 2026-04-20)**: once MFE ≥ 1.3R, SL pulled to entry (+fee buffer); one-shot per position. **Maker-TP resting limit (2026-04-20)**: post-only reduce-only limit sits at TP price alongside the position-attached TP — captures wicks as maker, avoids trigger latency.
- **Sizing:** fee-aware ceil on per-contract total cost so total realized SL loss (price + fee reserve) ≥ target_risk across every symbol. Overshoot bounded by one per-contract step (< $3 per position). Operator override via `RISK_AMOUNT_USDT` env bypasses percent-mode sizing; 10%-of-balance safety ceiling. Per-symbol `min_sl_distance_pct_per_symbol` floors: BTC 0.004, ETH 0.008, SOL 0.010, DOGE 0.008, BNB 0.005. Bybit boundary in `bybit_client.py` translates OKX-style integer `num_contracts` to base-coin `qty` via per-symbol `_OKX_CT_VAL` map (BTC 0.01, ETH 0.1, SOL 1, DOGE 1000, BNB 0.01); Bybit's `qtyStep` always cleanly divides the resulting qty (verified 2026-04-25 via `scripts/test_bybit_connection.py`).
- **Journal:** async SQLite, schema includes `on_chain_context`, `demo_artifact`, `confluence_pillar_scores`, `oscillator_raw_values` (all JSON). Separate tables: `rejected_signals` (counter-factual outcome pegged), `on_chain_snapshots` (Arkham state mutation time-series), `whale_transfers` (raw WS events for Phase 9 directional learning). *Per-exchange derivatives capture attempted 2026-04-24 and reverted same day — Coinalyze free-tier 40/min ceiling can't sustain it alongside per-symbol baseline (25 calls/cycle).*
- **On-chain (Arkham):** runtime soft signals only — daily bias ±15%, hourly stablecoin pulse +0.75 threshold penalty, altcoin-index +0.5 penalty on misaligned altcoin trades, **flow_alignment** 6-input directional score (stablecoin + BTC/ETH + Coinbase/Binance/Bybit 24h netflow; weights 0.25/0.25/0.15/0.15/0.10/0.10; default penalty 0.25), **per_symbol_cex_flow** binary penalty on misaligned symbol 1h volume (default 0.25, $5M floor). **Bitfinex + Kraken 24h netflow captured journal-only** (2026-04-23 night-late, 4th + 5th named venues — biggest single inflow / outflow in live probe vs. `type:cex` aggregate). **OKX 24h netflow captured journal-only** (2026-04-24, 6th venue — bot's own trading exchange, self-signal; 24h net ≈ 0 structurally but $58M max hourly |net|). None of 4/5/6 yet wired into `_flow_alignment_score` — Pass 3 decides weights. Whale HARD GATE removed 2026-04-22 — WS listener feeds `whale_transfers` journal for Pass 3 directional classification. Per-symbol token_volume fallback (2026-04-23): when Arkham `/token/volume/{id}` returns JSON `null` (confirmed for `solana`, `wrapped-solana`), `fetch_token_volume_last_hour` falls back to `/transfers/histogram` (flow=in + flow=out, last bucket) — zero coverage gap for the traded symbol set. **Netflow freeze fix (2026-04-23 night):** per-entity netflow rewritten from `/flow/entity/{entity}` (daily buckets, froze at UTC day close) to `/transfers/histogram?base=<entity>&granularity=1h&time_last=24h`; same fix for BTC/ETH aggregate. Daily-bundle refresh flipped from UTC-day gate to 5-min monotonic cadence (`on_chain.daily_snapshot_refresh_s: 300`) so `on_chain_snapshots` DB rows actually replace frozen values intraday. Credit-safe via v2 persistent WS streams + filter-fingerprint cache. All Arkham weights tuned in Pass 3.
- **Pass 2 instrumentation:** every trade row now captures `confluence_pillar_scores` (factor name → weight dict) and `oscillator_raw_values` (per-TF dict with 1m/3m/15m OscillatorTableData numerics: wt1/wt2/rsi/rsi_mfi/stoch_k/d/momentum/divergence flags). Both sourced from existing runner TF-switch cache — zero extra TV latency.
- **Tests:** ~1060, mostly green. Demo-runnable end-to-end. (Test count fluctuates with the migration: `test_okx_client.py` / `test_okx_enrichment.py` / `test_limit_entry.py` deleted as OKX-internal; `FakeBybitClient` in `conftest.py` keeps the OKX-era assertion vocabulary working via aliasing.)
- **Data cutoff (`rl.clean_since`):** `2026-04-25T21:45:00Z` — **Bybit migration cut**. Pre-migration DB archived as `data/trades.db.pre_bybit_2026-04-25T214500Z` (4.6 MB; mixes OKX Pass 1 + Pass 2 trades plus the SL-floor-bump losing cluster). Pass 1 baseline before that: `data/trades.db.pass1_backup_2026-04-22T203324Z`. Fresh DB created on first Bybit bot startup; reporter / GBT tooling reads only post-cutoff rows.

---

## Changelog

### 2026-04-26 (late-night) — Per-venue × per-asset (BTC/ETH/stables) 24h netflow capture + dashboard breakdown

Operator wanted per-venue netflow on the dashboard split by asset class
("her borsa için btc eth ve stablecoin görmek istiyorum") instead of the
single all-token Σ that the 6-venue grid shipped with on the morning
dashboard commit. Pure additive: journal-only schema, fire-and-forget
background fetcher (does NOT block the trade cycle), zero runtime scoring
change. Pass 3 candidate.

**Schema (3 JSON-as-TEXT columns on `on_chain_snapshots`):** dict keyed by
entity slug → signed USD float. Adding a 7th venue won't require schema
migration.
- `cex_per_venue_btc_netflow_24h_usd_json`
- `cex_per_venue_eth_netflow_24h_usd_json`
- `cex_per_venue_stables_netflow_24h_usd_json`

Idempotent `ALTER TABLE … ADD COLUMN` migrations apply on next bot startup.
Mirrored on `OnChainSnapshot` dataclass + `record_on_chain_snapshot`
signature (3 new kwargs default `None`) + INSERT column list (18 → 21).

**Fetcher:** [src/data/on_chain.py](src/data/on_chain.py) — new
`fetch_entity_per_asset_netflow_24h(client, entity, token_ids)` makes 2
`/transfers/histogram` calls (`flow=in`, `flow=out`) with
`base=<entity>&tokens=<token_id>&granularity=1h&time_last=24h`, sums the
24-bucket series, returns `in − out`. Same shape + label-free contract as
the entity-aggregate fetcher (verified 2026-04-23 night). 6 venues × 3
asset groups × 2 flows = **36 calls per refresh** at 1.1s rate cushion ≈
40-60s wall-clock — too long for the trade cycle.

**Background task (fire-and-forget):** [src/bot/runner.py](src/bot/runner.py)
— `_kick_per_venue_per_asset_refresh(client)` checks the previous task
via `prev.done()` and creates a fresh `asyncio.Task` (no stacking on slow
fetchers). `_refresh_per_venue_per_asset(client)` populates 3 dict caches
on `BotContext` (`per_venue_btc_netflow_24h_usd`, `_eth_`, `_stables_`)
plus `last_per_venue_per_asset_ts`. Wired into the existing daily-bundle
branch right after the per-entity netflow loop. The trade cycle reads
from the cache (None until the first refresh completes); the next
`OnChainSnapshot` construction with mutated dicts triggers a fresh
journal row via the fingerprint dedup. Stable-asset group passes
`("tether", "usd-coin")` to the same fetcher (one summed netflow per
venue).

**Dashboard:** [src/dashboard/state.py](src/dashboard/state.py) — 2 new
payload keys:
- `on_chain_per_venue_per_asset_24h: {venue: {btc|eth|stables: [{ts, v}…]}}`
- `on_chain_aggregate_per_asset_24h: {btc|eth|stables: [{ts, v}…]}`

Both built from the most-recent 24h slice of `on_chain_snapshots`.
Frontend ([src/dashboard/static/index.html](src/dashboard/static/index.html))
splits the existing 6-venue card grid: aggregate Σ (all-tokens) stays in
the top-right tile of each card (operator pref: "toplamı sadece sağ
üstte"); main viz is a 3-line chart (BTC red / ETH purple / Stables
green) with shared Y-scale, real-time x-axis (24h ending NOW), zero-line,
4h tick labels, latest-point dots, and a multi-line hover tooltip.
Inline legend under each card title shows latest BTC/ETH/Stb values
colour-coded by sign. New section "Total netflow per asset" reuses the
same renderer with single-series payloads — 3 standalone cards summing
across all 6 venues per timestamp.

**Cost:** +36 calls per `daily_snapshot_refresh_s` cycle (5 min) but
dispatched as a fire-and-forget background task — net zero impact on
the trade cycle's 180s budget. Label-free (Arkham `tokens=` filter
preserves the histogram endpoint's free tier; verified via the same
2026-04-23 probe). Total label budget unchanged at ~558/10k/mo.

**Pass 3 candidacy:** journal-only at this commit. Per-venue per-asset
opens combinatorial signals the aggregate Σ collapses — e.g. "Coinbase
ETH inflow accelerating while Coinbase BTC outflow stable" (ETH
distribution into a CEX hub, often pre-dump on alts). Pass 3 GBT/Optuna
will decide whether any subset earns runtime scoring weight.

**Files touched:**
- New code: `_dump_per_venue_dict` helper + 4 BotContext fields + 3
  OnChainSnapshot construction sites + fingerprint extension +
  `_on_chain_context_dict` keys + 2 new methods on BotRunner.
- Schema: 3 ALTER migrations + 3 INSERT columns + 3 reader keys + 3
  TradeRecord/RejectedSignal exposure paths via `_on_chain_context_dict`.
- Dashboard: 2 new payload builders + 4 JSON dict normalisation keys
  (was 1 — `token_volume_1h_net_usd_json` only) + new render functions
  `renderExchangeCandles` (rewrite) + `renderAggregatePerAsset` +
  `_drawPerAssetLines` + `_attachPerAssetHover` + CSS for inline legend
  dots/values.

**Re-eval triggers:**
1. **Per-asset coverage** on `on_chain_snapshots` rows captured after
   this commit — all 3 JSON columns should be NON-NULL on ≥95% of rows
   once the first background refresh completes (T+5min from startup).
   Zero-rate = `_per_venue_per_asset_task` never spawning or
   `affected_symbols_for` rejecting the slug.
2. **Stables-group magnitude sanity** — median venue stables 24h
   |net| should bracket the same range as the existing global
   stablecoin pulse (~$50M-200M); if zero across all 6 venues, the
   `("tether", "usd-coin")` token list dropped a slug variant.
3. **Background task latency** — `arkham_per_venue_per_asset_done`
   log line should arrive within 90s of `arkham_per_venue_per_asset_start`.
   Higher = Arkham rate-limit pressure (raise `rate_pause_s` from 1.1s).
4. **Dashboard payload size** — was ~30 KB/poll; per-venue per-asset
   adds ~24 series × 24 buckets × ~30 bytes = ~17 KB. New floor
   ~50 KB/poll. >100 KB sustained = the 24h slice limit lifted.

### 2026-04-26 (late-late-night) — Dashboard UX polish session

UI-only polish pass on [src/dashboard/static/index.html](src/dashboard/static/index.html). Zero backend / payload / strategy changes — every edit is in the single HTML file. Captures operator feedback across one observation session.

**Changes:**
- **Poll cadence 60s → 30s.** `POLL_MS` constant. Dashboard now refreshes twice per minute; cost stays trivial (RO journal read + 2 Bybit wallet calls).
- **Display layer UTC → UTC+3 (Turkey).** DB stays UTC (bot is schema owner; RO dashboard does NOT mutate timestamps). Single helper `_toTzDate(s)` shifts a Date by `TZ_OFFSET_MIN=180` so subsequent `.toISOString().slice()` reads as TR-local. All `fmtTs` / `fmtTsShort` / `fmtTsHM` route through it; clock + per-asset hover + candle hover + last-update timestamp + Rejected-signals + on-chain "captured" line all carry `+03` suffix. Asia/London/NY session window indicators STILL evaluate on `getUTCHours()` (markets are absolute, not local).
- **Edge-aligned x-axis tick labels** on candle chart + per-asset 3-line cards: first tick `textAlign="left"`, last tick `textAlign="right"`, middle ticks `"center"` — fixes label clipping at canvas edges that operator screenshotted.
- **KPI tiles restructured 8 → 9 tiles in 3 groups.** Flex layout with 1px gradient dividers (`<div class="kpi-divider">`) to visually separate groups: Account (Wallet, Starting, Open positions) │ Trade performance (Closed trades, Win rate, Net R, Profit factor) │ Risk (Max drawdown, Sharpe). Smaller tiles (`flex: 1 1 130px`, padding 10/12/9, label 9px, value 19px, sub 10px) to fit 9 cards. New "Starting" tile sources `summary.starting_balance`.
- **Profit factor tile** explicit branch: `null/undefined → "—"`, `Infinity → "∞"`, else `fmtNum(v, 2)`. Sub-line shows "no losses yet" when `num_losses === 0 && num_trades > 0` instead of misleading `—`. Backend already sanitises `inf → None` via `_finite_or_none` to keep `/api/state` JSON-encodable.
- **UPnL cell visual emphasis** on Open positions table: 17px bold `td.upnl-cell` shows `$` value primary; R appears as small dim subline `.upnl-r-sub`. Replaced earlier row-highlight attempt (operator reverted that — wanted clearer numbers, not row colour).
- **Removed "Setup" column** from Open positions: `setup_zone_source` is NULL on every current row (verified DB-side). Dropped the redundant column rather than show `—` everywhere.
- **Removed ticker cards** above Open positions table — same data already present in the rows below. `renderTicker` function + `.ticker` CSS deleted.
- **Contrast tuning** for dark theme: defined `--text-1: #dde3ed` (was referenced but undefined → some labels rendered transparent / browser-default), lightened `--muted` `#7782` → `#97a1b1`, lightened `--dim`. Body font 13 → 14px.
- **Header polish:** "live" → "LIVE" status badge; removed "READ-ONLY" subtitle from terminal name (was redundant with the read-only-by-architecture story); "demo baseline" → "baseline" sub on Starting tile. UTC label in clock area shows `TR · UTC+3`.

**Backend payload contract NOT changed.** All work is presentation-layer; `state.py` ships the same JSON shape as the morning per-venue per-asset commit.

**Files touched:** `src/dashboard/static/index.html` only (~95+/108− lines). No tests; pure UI.

**Re-eval triggers:**
1. **30s poll cost on `data/trades.db`** — `SQLITE_BUSY` rate should stay near-zero. If non-zero at twice the prior cadence, lift to WAL or back off poll to 45s.
2. **Bybit demo wallet rate-limit** — wallet endpoint has its own quota separate from order/position. 2 calls × 2/min = 240/h. If `bybit_demo_dns_pin_failed` or wallet tile NULL rate spikes, half the cadence.
3. **Operator session timezone correctness** — daylight-saving doesn't apply to TR (fixed UTC+3 since 2016). If TR ever re-introduces DST, `TZ_OFFSET_MIN` becomes a function of the date.

### 2026-04-26 (late-late-night, +3) — whale_threshold_usd 150M → 75M

Operator-flagged: `whale_transfers` table 0 rows in the ~24h since the
2026-04-25 Bybit restart, despite WS listener being alive (logs show
continuous reconnects with `usd_gte=150000000`). CLAUDE.md re-eval
trigger explicitly states `<5/day = WS fetch failing OR threshold too
high`, expected 20-100/day at $150M.

**Diagnostic probe**: lowered to `whale_threshold_usd: 75000000.0`
(`config/default.yaml`). Pure WS-filter change, zero extra API calls.
Two outcomes inform the next step:

- **Events flow** (≥10/day inserting into `whale_transfers`): root cause
  was threshold mismatched to current market activity. Pass 3 GBT will
  decide a tuned value; for now $75M is a reasonable mid-point between
  Pass 1's $100M working baseline and the $150M dry-spell.
- **Still zero**: threshold is not the binding constraint — bug lives
  in `parse_transfer_message` / cached `stream_id` filter fingerprint /
  WS event handler. Investigate before tuning further.

**Why $75M not $10M**: $10M risks crossing the Arkham label budget
(currently ~558/10k labels/month). Whale event labels resolve via
`/intelligence/address` which IS label-counted. At $10M we'd see
hundreds of events/day, each potentially fetching 2 entity labels;
budget impact non-trivial. $75M sits comfortably above the label
burn risk while still 50% below the $150M dry-spell threshold.

**Re-eval after operator restart + 24h:**
- 0 events → listener bug; abandon threshold tuning, debug WS path
- 1-50 events → threshold was the issue; consider Pass 3 tune
- 50-300 events → operator-acceptable signal density; hold here
- >300 events → noise floor too low; raise toward $100M

**Files touched:** `config/default.yaml` only (single line).
No code, no schema. Zero tests.

### 2026-04-26 (late-late-night, +2) — Macro panel readability + spark cache bug fix

Two paired changes — operator wanted clearer interpretation of every macro
tile's sign, and spotted that macro-panel sparks went blank after the first
30s refresh (only on poll #2 onward).

**Readability:**
- Static legend strip below the section header explains the universal sign
  convention. Stables flip the rule so it's spelled out in two rows:
  asset/venue netflow `+` = bearish (INTO CEX), `−` = bullish (OUT);
  stables `+` = bullish (cash arriving), `−` = bearish.
- Per-tile sub-lines now context-aware: BTC/ETH show `↑ supply into CEX
  · bearish bias` vs `↓ supply out of CEX · bullish bias`; stables show
  `↑ buying power arriving · bullish` vs `↓ buying power leaving ·
  bearish`. Helpers `_assetFlowSub`, `_stableFlowSub`.
- BTC/ETH tile **tone inverted** via new `_assetFlowTone` so positive (INTO
  CEX, bearish) renders red and negative (OUT, bullish) renders green —
  matches what the operator reads in the legend. Previous mapping used
  `_macroToneFromValue` (positive→green) which was misleading for asset
  netflows since the convention is inverted.

**Bug fix — spark Chart instances bound to detached canvases:**
- `renderMacroPanel` rebuilds `root.innerHTML = tiles.map(...)` on every
  poll, which destroys the `<canvas id="spark-X">` DOM nodes and creates
  fresh ones with the same IDs. But `sparkCharts[canvasId]` still cached
  the **first poll's Chart instance** whose internal canvas reference
  pointed at the detached node.
- On poll ≥2, `_renderSpark` hit the `if (sparkCharts[canvasId])` cache
  branch and called `c.update("none")` — which redrew into the detached
  canvas (invisible). The fresh visible canvas stayed blank. From the
  operator's perspective the macro pulse "veriler kayoluyor" — the
  textual values + sub-lines were still rendering correctly (set BEFORE
  the spark loop), but the visible trend lines vanished.
- Fix: detect `c.canvas !== el || !document.body.contains(c.canvas)` and
  destroy + recreate. Cheap (3 sparks: BTC / ETH / Stb), runs once per
  poll only when the canvas was rebuilt.

Backend `on_chain_latest` payload was verified rebuilt fresh per request
(`state.py` rebuilds on every `/api/state` call); the issue was purely
client-side Chart.js cache invalidation.

**Files touched:** `src/dashboard/static/index.html` only.

**Re-eval triggers:**
1. Sparks visible on poll #2 and onward — observable in the chart
   beneath BTC/ETH/Stb tiles. If still blank, Chart.js may have changed
   `c.canvas` semantics; fall back to always destroying.
2. CPU profile during poll — destroying + recreating 3 Charts every 30s
   is ~negligible but if perf shows blocking, switch to one-time setup
   on first poll only and reuse the canvas elements (would require
   `renderMacroPanel` to update text without `innerHTML =`).

### 2026-04-26 (late-late-night, +1) — Macro panel: flow_alignment + top venue tiles

Pure presentation-layer extension to the macro pulse panel. Three new tiles surface Arkham fields already present in `on_chain_latest` payload — zero backend changes.

- **Flow alignment** — composite [-1, +1] computed in JS from the 6-input formula mirrored from `strategy/entry_signals.py::_flow_alignment_score` (stables 0.25 + BTC 0.25 + ETH 0.15 + Coinbase 0.15 + Binance 0.10 + Bybit 0.10). Same noise-floor semantics ($1M default → 0). Tone: pos ≥+0.25 / neg ≤-0.25 / amber otherwise. Sub-line: "strong bullish / bullish lean / neutral / bearish lean / strong bearish". Not pulled from `trades.on_chain_flow_alignment_now` (that column lives on trade rows, not snapshots) — recomputing on the dashboard side keeps the macro panel self-contained on `on_chain_latest`.
- **Top CEX inflow** — picks max-positive of the 6 named venues (Coinbase / Binance / Bybit / Bitfinex / Kraken / OKX) on the latest snapshot. Tile colour `neg` (inflow = bearish bias). Sub: "<Venue> · most bearish venue". Hidden ("no positive inflow") if all 6 are non-positive.
- **Top CEX outflow** — picks min-negative. Tile `pos` (outflow = bullish). Sub: "<Venue> · most bullish venue".

Macro grid `repeat(3, 1fr)` already accommodates 9 tiles (3x3) with no CSS change. `fmtFlowM` handles signed M/B/K formatting unchanged.

**Coinalyze derivatives NOT added.** `DerivativesState` (per-symbol funding, OI 1h%, LS-ratio z) lives only on the bot side; surfacing requires a `state.py` payload extension. Deferred — the operator's question was "useful additions" and this commit covers the immediate Arkham wins; Coinalyze tiles are a separate plumbing pass.

**Files touched:** `src/dashboard/static/index.html` only — 3 new helpers (`_computeFlowAlignment`, `_flowAlignmentTone`, `_flowAlignmentLabel`, `_topVenueFlow`) + 3 IIFE tile entries appended to the `tiles` array in `renderMacroPanel`.

**Re-eval triggers:**
1. **Flow alignment NULL rate** — should match the underlying snapshot field coverage. If `on_chain_latest` has all 6 fields populated (post-2026-04-23 freeze fix this is normal) but tile shows "—", `_computeFlowAlignment` is mis-keyed.
2. **Top venue tile flicker** — if Coinbase / Binance keep flipping inflow→outflow rank between polls, the 24h rolling values are oscillating near zero; consider a hysteresis or magnitude floor before claiming a "winner".
3. **Score divergence vs runtime** — periodic spot check: snapshot `on_chain_flow_alignment_now` from a recent `trades` row should equal dashboard tile within 0.05 (different snapshot, slight time skew). Larger gap = formula or weight drift.

### 2026-04-26 (night) — Read-only single-page dashboard + demo balance reset

Two paired changes triggered by the operator wanting to inspect live trade
state without opening `data/trades.db` directly in a SQLite browser, plus a
demo balance reset for a more conservative observation phase.

**Dashboard (`src/dashboard/`):** new sibling FastAPI process, read-only, one
scrollable HTML page. No tabs (operator preference: single consolidated panel
"genel gözlem yapabileceğim bir front sayfası"). Polls `GET /api/state` every
5s and re-renders. Sections: KPI tiles (closed trades / WR / Net R / PF / max
DD / Sharpe / wallet / open count), open positions joined to latest
`position_snapshots` row (live mark, UPnL $/R, MFE/MAE, current SL/TP, BE +
MFE-lock flags), equity curve (Chart.js cumulative R), reject reason
histogram, three 24h on-chain charts (BTC netflow / ETH netflow / stablecoin
pulse 1h — sliced from `on_chain_snapshots` rows in last 24h), closed trades
last 50, per-symbol + per-regime breakdown tables, latest on-chain snapshot
card (15 fields covering all 6 named CEX venues), rejected signals last 50,
whale transfers last 25.

**Read-only concurrency:** `src/dashboard/state.py::ReadOnlyJournal` subclass
overrides `connect()` to open the DB via `aiosqlite.connect("file:...?mode=ro",
uri=True, timeout=10)` and skips schema setup — bot remains the schema owner,
dashboard is a passive reader. Default DELETE journal mode serializes
writers vs. readers, brief `SQLITE_BUSY` rides through the 10s timeout. WAL
mode NOT enabled (would persist in the file and require operator opt-in).

**Live wallet probe:** when `BYBIT_API_KEY/SECRET` present in `.env`,
`fetch_wallet()` calls `BybitClient.get_balance()` + `get_total_equity()`
in `asyncio.to_thread` with an 8s timeout per request and surfaces
`{available_usd, margin_balance_usd, demo}` in the payload. Frontend "Wallet"
tile shows `margin_balance_usd` with `available` as the sub-line. Missing
creds or any failure → tile falls back to journal-simulated equity. Adds
~2 round-trips per 5s poll cycle (cheap; no rate-limit pressure on Bybit V5).

**Demo balance reset (`config/default.yaml`):** `bot.starting_balance`
`5000.0 → 500.0`. Operator reset the Bybit demo account to a $500 baseline
for a more conservative observation phase. `RISK_AMOUNT_USDT=10` (.env)
unchanged in mechanism but now represents 2% of equity (was 0.2% on $5k).
Per-slot collateral `total_eq / 5 ≈ $100`. Sizing math reads `wallet`
from Bybit at runtime, not the YAML constant — `starting_balance` is only
used by `reporter.summary()` for the journal-simulated equity baseline
(equity-curve KPI when wallet probe is unavailable).

**`logs.bat` removed** at repo root — operator uses `scripts/logs.py`
directly when needed; dashboard log-streaming is out of scope here.

**`dashboard.bat` startup hardening:** added `pause` after the python
process exits so a bind error (port 8765 already in use) leaves the
console window open with a visible exit code. Without it the window
closed silently and operator couldn't diagnose stale-instance
conflicts.

**Files touched:**
- New: [src/dashboard/__init__.py](src/dashboard/__init__.py), [__main__.py](src/dashboard/__main__.py),
  [state.py](src/dashboard/state.py), [server.py](src/dashboard/server.py),
  [static/index.html](src/dashboard/static/index.html), [dashboard.bat](dashboard.bat).
- Modified: [requirements.txt](requirements.txt) (`fastapi>=0.115`, `uvicorn[standard]>=0.32`),
  [config/default.yaml](config/default.yaml) (`starting_balance: 5000 → 500`).
- Removed: `logs.bat`.

**Cost:** zero on the bot writer (separate process, RO connection). On the
dashboard side, ~2 Bybit REST calls per 5s poll while the page is open;
no rate-limit pressure (Bybit V5 wallet endpoint has its own quota separate
from order/position).

**Re-eval triggers:**
1. **`SQLITE_BUSY` log line frequency** in dashboard logs — should be near-zero
   under DELETE journal mode at 5s polling. Higher than ~1/min = the bot's
   commit window is blocking the dashboard read more than expected; consider
   WAL opt-in.
2. **Wallet tile NULL rate** — `wallet` key missing from `/api/state` more
   than ~5% of polls = Bybit demo edge or DNS pin failing intermittently;
   inspect `bybit_demo_dns_pin_failed` log.
3. **Dashboard payload size** — currently ~30 KB/poll at 50 closed + 50
   rejected + 25 whales + 45 on_chain rows. >200 KB sustained = a list
   limit was inadvertently lifted; tighten `_RECENT_*_LIMIT` constants in
   `state.py`.

### 2026-04-26 (evening) — Intra-trade `position_snapshots` table for RL trajectory data

New `position_snapshots` table joined to `trades.trade_id`, populated every
5 min (configurable `journal.position_snapshot_cadence_s`, validated [60, 3600])
for every OPEN position. Captures live mark/PnL (from Bybit `get_positions` —
zero extra API), running MFE/MAE in R, current SL/TP + lifecycle flags
(BE moved, MFE-lock applied), and drift fields for derivatives + on-chain +
3m oscillator + VWAP-band distance.

Hook: `_process_closes()` after `monitor.poll()` returns `(fills, live_snaps)`
tuple. Cadence gate via `time.monotonic()`, bumped only after a successful
write window (so a no-write cycle doesn't reset the timer). MFE/MAE updated
on every 5s poll inside `PositionMonitor.poll()` (not just 5-min snapshot)
so excursion peaks aren't missed. Per-symbol `last_market_state_per_symbol`
cache on `BotContext` populated at end of `_run_one_symbol` enables
oscillator/VWAP enrichment outside the per-symbol cycle.

Pass 3+ use: post-hoc trajectory replay — "trade X peaked at +1.3R at minute
12, was stopped at −1R at minute 47; would early-exit at +1.0R MFE have
captured edge?" Hourly heatmap — "do positions opened in 09-12 UTC band drift
away from VWAP composite faster than 14-17 UTC?" Joins to `trades.trade_id`
allow per-trade trajectory reconstruction without re-fetching market state.

**Restart caveat:** MFE/MAE running counters are in-memory only; rehydrated
positions reset to 0/0 and rebuild from the next poll. Acceptable for v1; the
alternative (4th column on `trades` + rehydrate plumbing change) is deferred
unless RL feature importance on these columns shows entry-window sensitivity.
Similarly, the FIRST snapshot per symbol after restart will have NULL
oscillator/VWAP enrichment until that symbol's first per-symbol cycle
populates the `last_market_state_per_symbol` cache.

**Files touched:** new `position_snapshots` schema + `record_position_snapshot`
+ `get_position_snapshots` reader + `PositionSnapshotRecord` model in journal;
`_Tracked.mfe_r_high/mae_r_low` + per-poll update + `poll() → (fills, snaps)`
tuple + `get_tracked()` accessor in [src/execution/position_monitor.py](src/execution/position_monitor.py);
`BotContext.last_market_state_per_symbol` + `last_position_snapshot_ts` +
`_maybe_write_position_snapshots` + cache hook + `_process_closes` rewire in
[src/bot/runner.py](src/bot/runner.py); `JournalConfig.position_snapshot_*`
fields + `[60, 3600]` validator in [src/bot/config.py](src/bot/config.py);
`journal:` block extended in [config/default.yaml](config/default.yaml).

**Tests:** +26 across `test_journal_position_snapshots.py` (11 — schema,
round-trip, NULL handling, JSON nested-dict, idempotent migration, cross-trade
isolation), `test_position_monitor_mfe_mae.py` (8 — defaults, long/short
sign-aware excursion, multi-poll persistence, plan_sl=0 skip, tuple return),
`test_runner_position_snapshots.py` (7 — cadence gate, disabled config, empty
snaps, missing tracked, rehydrate plan_sl=0 skip, end-to-end MFE/MAE write).
+5 `JournalConfig` validator tests in `test_bot_config.py`. New tests 26/26
green; full-suite delta `911 → 937 passed`. Pre-existing 33 OKX→Bybit
migration leftover failures untouched (separate cleanup task).

**Cost:** zero extra API calls — `live_snaps` already fetched by `monitor.poll()`,
just plumbed through. ~1 KB/day SQLite growth at 5 positions × 1 row / 5 min.

**Re-eval triggers:**
1. **Snapshot row coverage** — `SELECT COUNT(DISTINCT trade_id) FROM position_snapshots`
   over a 24h window with N OPEN positions held >5 min should equal N. Lower
   = cadence gate or hook regression.
2. **MFE/MAE non-zero rate** — fraction of snapshot rows with `mfe_r_so_far > 0
   OR mae_r_so_far < 0` should approach 100% after ~10 polls (50s) into a
   position. Lower = per-poll update regression.
3. **Oscillator drift coverage** — `oscillator_3m_now_json != NULL` rate should
   approach 100% on snapshots taken AFTER each symbol's first cycle post-
   restart. Cold-start gap is expected NULL.
4. **Write throughput** — at 5 positions × 1 row / 5 min = ~1 row/min sustained.
   `data/trades.db` growth should track ~1 KB/day from this table at full
   tilt; alarm if > 100 KB/day (cadence gate failed).

### 2026-04-26 — VWAP daily-reset blackout + pillar_scores forwarding bugfix

Two paired changes triggered by a single morning chart-review session: operator noticed Pine VWAP (1m/3m/15m) "resets" at UTC 00:00 — bands collapse and re-anchor — and asked whether new entries are protected from the noisy ~10-30 min post-reset window. Same review surfaced that all 5 OPEN positions have empty `confluence_pillar_scores={}` despite populated `confluence_factors`, breaking Pass 2 instrumentation for zone-based entries.

**Root cause #1 — VWAP daily reset:** `pine/smt_overlay.pine:154-158` anchors all three VWAPs on `timeframe.change("D")`, which fires at UTC 00:00. `ta.vwap(src, anchor, 1.0)` re-initialises stdev to ~0 at the anchor flip, so the ±1σ bands collapse onto the VWAP line for the first few bars and the `vwap_composite_alignment` soft pillar (weight 1.25) reads near-noise. Effect: a long at 00:03 UTC sees a VWAP that's anchored to one price and bands that are mathematically unstable; the same trade at 00:30 UTC sees a stable rolling distribution.

**Fix — Time-based blackout window:**
- New helper `in_vwap_reset_blackout(now, *, pre_minutes, post_minutes)` in [src/strategy/entry_signals.py](src/strategy/entry_signals.py) — pure function, returns True inside `[00:00 - pre_minutes, 00:00 + post_minutes)`. Naive `datetime` treated as UTC; aware `datetime` converted via `astimezone`. Both windows zero short-circuits to False (kill switch).
- Wired into [src/bot/runner.py](src/bot/runner.py) `_run_one_symbol` as an early-return AFTER macro_event_blackout (same pattern: log + return, no rejected_signals row — operationally a planned outage, not a strategy reject).
- Wired into [src/strategy/entry_signals.py](src/strategy/entry_signals.py) `evaluate_pending_invalidation_gates` as the FIRST gate (before `vwap_misaligned`) — resting pendings inside the blackout get cancelled with `reason=vwap_reset_blackout` so they don't fill into the unreliable just-reset VWAP. Order matters: `vwap_misaligned` itself reads the unstable VWAPs so it would mis-attribute the cancel reason.
- Config: `analysis.vwap_reset_blackout_enabled: true`, `vwap_reset_blackout_window_pre_min: 5`, `vwap_reset_blackout_window_post_min: 15` in [config/default.yaml](config/default.yaml). Pydantic validators clamp pre/post to `[0, 60]`. 20-minute total downtime per day matches operator's "yeterli" sign-off.

**Root cause #2 — `confluence_pillar_scores` dropped on zone-wrapped plans:** [src/strategy/setup_planner.py:560-581](src/strategy/setup_planner.py) `apply_zone_to_plan` builds a fresh `TradePlan` from the input plan, forwarding `confluence_factors` but NOT `confluence_pillar_scores`. The new plan defaults the field to `{}` (`field(default_factory=dict)`). Every zone-based entry — which is every entry the bot makes, since the strategy is zone-based — therefore stamps an empty dict into the journal. Audit on the 5 currently OPEN Bybit positions (entered 2026-04-25 21:38 → 2026-04-26 00:12 UTC) confirmed all 5 have `pillar_scores='{}'` while `factors` (the string list) is populated and `oscillator_raw_values` (1100+ char JSON) + `on_chain_context` (790+ char JSON) + per-symbol derivatives enrichment are all populated. The gap is specific to this one column.

**Fix — One-line forwarding:**
- Added `confluence_pillar_scores=dict(plan.confluence_pillar_scores)` to the `TradePlan(...)` construction in `apply_zone_to_plan`. Defensive copy (mutating the wrapped plan must not bleed back into the source).
- Regression test `test_apply_zone_to_plan_preserves_confluence_pillar_scores` in [tests/test_setup_planner.py](tests/test_setup_planner.py) locks the contract: a plan with three pillar weights round-trips through `apply_zone_to_plan` with bit-exact equality, plus a defensive-copy assertion.

**Pass 2 dataset caveat:** the 5 currently OPEN trades (and any closed trade post-Bybit-cut at 2026-04-25T21:45Z that pre-dates this commit) have empty `pillar_scores`. Pass 3 GBT/Optuna over per-pillar weights should treat `confluence_pillar_scores='{}'` as MISSING-by-bug (not "no factors fired") and either drop those rows from the per-pillar feature matrix or back-fill from `confluence_factors` using nominal `confluence_weights` from YAML at row entry time. The two are not equivalent (factor names lose the regime-conditional weight multipliers that ConfluenceScore.factors actually carried), but a back-fill is closer to the truth than the empty dict.

**Reject vocabulary:** `vwap_reset_blackout` added to the unified reject_reason list (currently only emitted from the pending-invalidation path; the runner early-return is no-row-write by design, matching macro_event_blackout's pattern).

**Cost:** zero API calls, zero latency. Both fixes are pure-function additions / one-line plumbing.

**Tests:** +22 in `test_vwap_reset_blackout.py` (window edges, kill switch, asymmetric windows, naive/aware datetime handling, pending-eval integration, gate ordering, config validators) + 1 regression in `test_setup_planner.py`. Targeted suite (setup_planner + vwap_blackout + entry_signals + runner_zone_entry + oscillator_raw_values + journal_database) = 181/181 green.

**Re-eval triggers:**
1. **`pillar_scores` coverage on post-fix rows** — `SELECT COUNT(*) FROM trades WHERE entry_timestamp > '2026-04-26T<commit-ts>' AND length(confluence_pillar_scores) <= 2` should be 0. Non-zero = a 5th call site to `record_open` / `record_rejected_signal` exists that doesn't read from `plan.confluence_pillar_scores`.
2. **Blackout fire rate** — per-day count of `vwap_reset_blackout` log lines in the runner. Expect 5 symbols × ~1 cycle/min × 20 min = ~100 NO_TRADE log emissions per day. Materially higher = clock skew or naive-datetime handling regression.
3. **Pending-cancel attribution** — fraction of pending cancels with `reason=vwap_reset_blackout` should track ~1.4% of total cancels (20min/24h = 1.39%). Higher = pending limits clustering in the blackout window (zone-source bias toward late-day setups); lower = blackout firing on fewer pendings than expected.
4. **Operator pendings holding through midnight** — if a pending placed at 23:50 UTC gets cancelled at 23:55 UTC (5 min into pre-window), confirm operator considers this acceptable; otherwise tighten `pre_min` to 0 and accept the 15-min post-only outage.

### 2026-04-25 — OKX → Bybit V5 Demo migration (venue swap)

Operator-driven full venue swap. OKX completely removed from the codebase; bot trades against Bybit V5 Demo (`https://api-demo.bybit.com`) under a UTA hedge-mode account. Decision drivers: cleaner demo wick behaviour at mark-price triggers, simpler API surface (TP/SL is a position property, not a separate algo), better long-term roadmap fit. No strategy / scoring changes — only the execution layer + config / docs / scripts touched.

**Architectural shifts:**

1. **TP/SL are now position-attached.** OKX placed an OCO algo as a separate order with its own `algoId`; Bybit treats `takeProfit` / `stopLoss` as fields on the position itself, set via `POST /v5/order/create` (market entry) or `POST /v5/position/trading-stop` (limit-fill attach + every subsequent mutation). Eliminates the entire OKX-era machinery: `place_oco_algo` / `cancel_algo` / `list_pending_algos` / `_verify_algo_gone` / `_cancel_surplus_ocos` / `_cancel_algos_best_effort` / `algo_ids[]` tracking. SL-to-BE, TP-revise, and MFE-lock all collapse to a single trading-stop call with no cancel+place dance and no "unprotected window" between cancel and place.

2. **Hedge mode via `positionIdx=1/2`**, not OKX `posSide=long/short`. Set once at startup via `POST /v5/position/switch-mode {mode: 3, coin: USDT}`. Bot still speaks OKX's "long"/"short" vocabulary internally; `bybit_client._pos_idx()` translates at the boundary (long→1, short→2). Account-wide margin mode (UTA `REGULAR_MARGIN` ≈ cross) replaces OKX's per-call `tdMode=isolated/cross` — `RouterConfig.margin_mode` field is preserved but no longer forwarded to API calls.

3. **Internal symbol format kept OKX-style** (`BTC-USDT-SWAP`). The Bybit boundary in `bybit_client.py` translates `_OKX_TO_BYBIT_SYMBOL["BTC-USDT-SWAP"] → "BTCUSDT"` on outgoing requests and `_BYBIT_TO_OKX_SYMBOL["BTCUSDT"] → "BTC-USDT-SWAP"` on incoming responses. Trade-off: 7-line lookup map vs. mass-rename of ~50 files (config keys, journal column values, test fixtures, runner literals, on-chain mapping). Old journal rows remain string-comparable to new ones.

4. **Sizing math unchanged.** OKX's `num_contracts × ctVal × price` was preserved by hardcoding `_OKX_CT_VAL = {BTC=0.01, ETH=0.1, SOL=1, DOGE=1000, BNB=0.01}` in `bybit_client.py`; the boundary multiplies `num_contracts × ct_val` to produce Bybit's required base-coin `qty` string. Verified against Bybit's `qtyStep` filter: every symbol's contract size is an integer multiple of step (BTC ct_val 0.01 = 10 × step 0.001; DOGE 1000 = 1000 × step 1.0; etc).

5. **Wallet reads UTA-aware.** `get_balance()` returns `totalAvailableBalance` (USD-aggregated, USDT+USDC pooled by USD value when both are toggled as collateral). `get_total_equity()` returns `totalMarginBalance` — the collateral pool that actually backs margin, NOT the wider `totalEquity` (which includes BTC/ETH wallet balances when those have "Used as Collateral" off). Sizing math therefore reflects the bot's true usable capital, not visual-wallet noise.

6. **Demo CloudFront edge auto-pin.** Some ISPs (observed on TR-mobile / TR-fiber egress) silently drop TCP-443 SYNs to the `13.249.8.0/24` CloudFront range that `api-demo.bybit.com` sometimes resolves to. Mainnet uses a different distribution that routes fine, which made the issue look like a credentials problem. `BybitClient._maybe_pin_demo_dns()` now resolves the host at construction, probes each returned IP with a 2s TCP-443 connect, and pins the first reachable one to the requests session via a custom HTTPS adapter (TLS still validates against the real hostname via SNI). Falls back to a hardcoded shortlist of known-working edges when system DNS yields only blocked IPs.

7. **Error-code hierarchy** kept identical (`InsufficientMargin`, `OrderRejected`, `LeverageSetError`, `AlgoOrderError`); class `OKXError` renamed to `BybitError` with Bybit retCodes:
   - 110004/110007/110012 → `InsufficientMargin`
   - 110001/110008/110010/170142/170213 → `_ORDER_GONE_CODES` (idempotent cancel)
   - 170218 → `OrderRejected` (post-only would cross — triggers limit fallback)
   - 110086/110087 → `LeverageSetError`
   - 110021 → operator-visible (OI cap), not auto-handled

**Code surface (files touched):**

- New: [src/execution/bybit_client.py](src/execution/bybit_client.py) — full pybit V5 wrapper with boundary translation, DNS-pin, and AlgoResult/cancel_algo back-compat shims so journal models + old test fixtures stay valid.
- Deleted: `src/execution/okx_client.py`, `tests/test_okx_client.py`, `tests/test_okx_enrichment.py`, `tests/test_limit_entry.py`, `scripts/test_okx_connection.py`, `scripts/cancel_orphans.py`.
- Rewritten: [src/execution/order_router.py](src/execution/order_router.py) (no separate algo placement; market entry passes TP/SL on `place_order`; pending-fill path calls `set_position_tpsl`), [src/execution/position_monitor.py](src/execution/position_monitor.py) (SL-to-BE / TP-revise / SL-lock all simplified to single trading-stop calls), [src/execution/__init__.py](src/execution/__init__.py), [scripts/probe_open_orders.py](scripts/probe_open_orders.py), [scripts/test_bybit_connection.py](scripts/test_bybit_connection.py) (new smoke).
- Updated: [src/execution/errors.py](src/execution/errors.py) (`BybitError`), [src/bot/config.py](src/bot/config.py) (`BybitConfigBlock`, `BYBIT_*` env loading), [src/bot/runner.py](src/bot/runner.py) (`bybit_client` field, ~15 call sites, `_cancel_surplus_ocos` neutered to no-op, `_cancel_orphan_pending_limits` rewired to `list_open_orders`), [src/data/tv_bridge.py](src/data/tv_bridge.py) (`OKX:` → `BYBIT:` in TV ticker), [config/default.yaml](config/default.yaml) (`bybit:` block, `clean_since` reset to 2026-04-25T21:45:00Z), [.env.example](.env.example) (`BYBIT_*`), [requirements.txt](requirements.txt) (`pybit>=5.7.0`, `python-okx` removed).

**Verification (2026-04-25 21:42 local):**

`python scripts/test_bybit_connection.py` against demo:
- DNS-pin selected edge `3.168.236.5` (after switching to Google DNS + disabling GoodbyeDPI which was breaking TLS to the demo distribution).
- Wallet: `totalAvailableBalance` = `totalMarginBalance` = `50,013.40 USDT` (matches operator's adjusted demo balance).
- All 5 instrument specs returned cleanly with correct `qtyStep` / `maxLeverage`.
- Mark prices fetched live (BTC $77,296 / ETH $2,311 / SOL $85.66 / DOGE $0.0977 / BNB $628.50).
- No live positions, no resting orders (clean account, expected).

**Pre-restart DB archive:** `data/trades.db.pre_bybit_2026-04-25T214500Z` (4.6 MB). Window from 2026-04-22 Pass 2 restart through 2026-04-25 had two known data-quality issues: (a) demo-wick artefact pollution (operator-flagged as a primary motivator for the venue swap), (b) the 2026-04-23 SL-floor-bump losing cluster (WR collapsed 66.7% → 22.2% before the 2026-04-24 revert). Both make that window unsuitable for Pass 3 tuning; cutting clean.

**Re-eval triggers (post-Bybit, monitor over first 20 closed trades):**

1. **DNS-pin success rate** — `bybit_demo_dns_pinned` log line on every restart should pick a reachable IP within 1 probe round (≤ 2s). If the helper logs `bybit_demo_dns_pin_failed` repeatedly, the hardcoded fallback list (`_DEMO_FALLBACK_IPS`) is stale; refresh from a working host.
2. **TP/SL attachment latency** — for limit-fill entries, the `set_position_tpsl` call should land within 500ms of the fill event. Higher = Bybit-side queue or rate-limit; investigate.
3. **trading-stop "lose binding relationship" warning** — Bybit warns that one-sided modify (BE move, TP revise, SL lock) unbinds the TP/SL pair. Functionally fine (both legs still work; position-close auto-cancels orphan), but if Bybit later changes that contract the bot would silently double-fire. Periodic spot-check via `probe_open_orders.py` after a TP1 event.
4. **UTA collateral ratio** — `totalAvailableBalance / totalMarginBalance` should hover near 1.0 when no positions are open. If it drifts below 0.95 with no positions, a haircut policy or cross-margin loan is consuming collateral; investigate before Pass 3.
5. **`get_total_equity` field robustness** — Bybit demo response has been seen to omit `totalMarginBalance` on rare empty-account states. Code falls back to `totalEquity`; if logs show `totalEquity` being read on a populated account (per-slot sizing would over-allocate), inspect.
6. **Bybit demo 7-day order persistence** — Bybit auto-expires demo orders after 7 days. Doesn't affect bot logic (positions reconcile every restart), but if a long pending limit unexpectedly disappears between cycles, this is the cause.

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

### 2026-04-24 (evening) — Per-exchange derivatives capture REVERTED

After 4 fix iterations against Coinalyze's 40/min free-tier rate limit, operator called it: **"çekemediklerimizi dbden kaldırırız"** — keep what works, drop what doesn't. Full revert shipped as `4fb1018` (−369 lines net). Rationale:

Per-symbol refresh baseline is ~20 calls/cycle (OI + funding + predicted + liq_history + ls_ratio × 5 symbols). At `refresh_interval_s=60s`, that's ~16-17 calls/min sustained. 60s rolling server-side window continuously saturated to ~80% of the 40/min ceiling from per-symbol alone. 3 extra per-exchange batch calls (OI + funding + predicted) fire as a burst, pushing instantaneous rate over the limit. Server 429s on whichever endpoint is called second in the burst.

Iterations of fix-and-test:

| Commit | Fix attempt | Result |
|---|---|---|
| `21e8f49` | 2s sleep between per-exchange batches | 429 on funding stayed |
| `55c8a70` | cadence 3, OKX 3rd construction site | Funding still 429 |
| `a262d50` | cadence 5, early-skip, 3s sleep | Still 429 |
| `9526909` | drop predicted_funding from per-symbol | 429 shifted to /open-interest instead |
| `4fb1018` | **full revert** | This commit |

On the last run (03:29:35 UTC, cadence-5), 6 consecutive per-exchange fires between 06:34-07:07 local ALL 429'd on the first `/open-interest` call → zero data captured. Pattern shift (funding→OI) suggested endpoint-level limiting that our client-side 40/min token bucket can't predict.

**Revert removes:**
- 3 JSON columns on `trades` + `rejected_signals` (`oi_per_exchange_usd_json_at_entry`, `funding_rate_per_exchange_json_at_entry`, `funding_rate_predicted_per_exchange_json_at_entry`) via idempotent `ALTER TABLE ... DROP COLUMN` migrations
- 3 `DerivativesState` dict fields (`oi_per_exchange_usd`, `funding_per_exchange`, `funding_predicted_per_exchange`)
- `_refresh_per_exchange_snapshot` method + cycle counter + cadence knob in `DerivativesCache`
- Per-exchange symbol map + `_batch_fetch_current_values` + `_regroup_by_okx_symbol` helpers + 3 batch fetcher methods in `CoinalyzeClient`
- Corresponding signatures + serializers + readers in `database.py`
- 3 dict fields on `TradeRecord` + `RejectedSignal` models
- `_parse_per_exchange_dict` helper

**Revert preserves** (unrelated features stay):
- OKX as 6th Arkham netflow venue (`202f107`) — different feature, works fine
- All single-exchange derivatives journal enrichment (2026-04-23 eve, 9 REAL columns + 1 JSON column: OI, funding_current, funding_predicted, liq notionals, LS z-score, price_change 1h/4h, heatmap top clusters)
- Core on_chain_snapshots schema (all 6 named venues, daily bias, stablecoin pulse, altcoin index, per-symbol token volume JSON)
- All other Pass 2 instrumentation (confluence_pillar_scores, oscillator_raw_values, whale_transfers)

**Pre-revert path for re-enable (Pass 3+):**

1. **Upgrade Coinalyze tier** — paid plans offer 500/min+ which trivialises the capacity tension.
2. **First reduce per-symbol cost** — `fetch_liquidation_history` is the cheapest drop (Binance WS already provides this, the Coinalyze call is a gap-filler for WS throttle). Frees ~5 calls/min per cycle. Could also drop `fetch_long_short_ratio` (1h bucket, changes slowly; z-score still works from 336h seeded history).
3. **Revisit with tooling** — if Coinalyze rate still tight, decouple per-exchange fetch into own asyncio task with 15-min cadence + aggressive rate-pause awareness (option C from iter-5 discussion, not shipped).

**Lessons captured to memory** (`feedback_free_tier_rate_budget_backfired.md`): when a feature requires API calls near a known sustained rate limit, the first iteration should audit the EXISTING sustained rate before adding new calls. If baseline is >70% of the ceiling, don't add a parallel burst.

**Tests:** 131 targeted pass across 7 suites. Operator restart required to clear the now-unused column population attempts; DROP COLUMN migrations run on next startup.

### 2026-04-24 — Per-exchange derivatives journal capture (Binance/Bybit/OKX) [REVERTED — see 2026-04-24 (evening) above]

Operator flagged that bot trades derivatives on OKX/Bybit/Binance but the
journal only holds ONE exchange's snapshot per symbol (liquidity-ranked via
`EXCHANGE_PRIORITY=[A=Binance, 6=Bybit, 3=OKX, F=Deribit, H=HTX]`, almost
always the Binance one). Pass 3 features like funding-spread, OI-share,
and cross-venue divergence are therefore invisible to the model. Added
per-exchange journal-only capture without touching runtime scoring.

**Coinalyze research findings (`scripts/probe_arkham.py`-style ad-hoc):**
- 11 endpoints total; 40 calls/min per key; data retention ~2k intraday datapoints
- Documented as "no per-exchange breakdown" — technically WRONG. Per-exchange
  IS available via symbol encoding: `BTCUSDT_PERP.A` (Binance), `BTCUSDT.6`
  (Bybit; note no `_PERP`), `BTCUSDT_PERP.3` (OKX) all return separate rows
  from the same endpoint
- Missing: spot-perp basis, whale endpoints, top-trader L/S split

**Live probe (2026-04-24) — spreads are meaningful, not noise:**

| Symbol | Funding Binance | Funding Bybit | Funding OKX | Spread |
|---|---:|---:|---:|---:|
| SOL | −77bp | **+48bp** | −20bp | **125bp** |
| DOGE | **+100bp** | −39bp | −17bp | **139bp** |
| ETH | −36bp | −20bp | −47bp | 27bp |
| BNB | +33bp | +50bp | **+66bp** | 33bp |
| BTC | −38bp | −34bp | −37bp | 4bp |

OI shares (BTC): Binance $7.76B / Bybit $4.05B / OKX $2.81B — 3:2:1 ratio.
Funding spread signals crowded one-side positioning (GBT-learnable);
OI share drift hints at flow-of-money between venues.

**Tier decision:** Tier A only (OI current + funding current + funding
predicted, 3 batch calls per refresh). Tier B (per-exchange liquidation +
L/S ratio history) deferred — higher cost (5 calls × 2 metrics vs 3 total),
lower marginal value until Tier A confirms column coverage.

**Shipped as two paired commits:**

Commit `2cc5a36` — journal schema + model:
- `src/journal/models.py` — 3 new `dict` fields on `TradeRecord` + `RejectedSignal` (default `{}`):
  - `oi_per_exchange_usd_at_entry`
  - `funding_rate_per_exchange_at_entry`
  - `funding_rate_predicted_per_exchange_at_entry`
- `src/journal/database.py` — CREATE TABLE columns on `trades` + `rejected_signals`; 6 idempotent ALTER migrations (3 cols × 2 tables); `_COLUMNS`/`_REJECTED_COLUMNS` lists; `record_open` + `record_rejected_signal` signatures; `_record_to_row`/`_rejected_to_row` writers; `_row_to_record`/`_row_to_rejected` readers via new `_parse_per_exchange_dict` helper.

Commit `1cd2498` — fetcher + cache + runner wiring:
- `src/data/derivatives_api.py`:
  - `_per_exchange_symbol_map: dict[okx_sym, dict[binance|bybit|okx, coinalyze_sym]]` populated alongside `_symbol_map` in `ensure_symbol_map` (no extra API call — reuses the `/future-markets` response already cached there)
  - `_batch_fetch_current_values(path)` — single comma-joined `symbols=` query covering all watched × 3 exchanges (up to 15 symbols; API limit 20)
  - `_regroup_by_okx_symbol(flat)` — pivots flat `{coinalyze_sym: value}` back to `{okx_sym: {exchange_label: value}}`
  - `fetch_per_exchange_oi_usd`, `fetch_per_exchange_funding`, `fetch_per_exchange_predicted_funding` — 1 API call each
- `src/data/derivatives_cache.py`:
  - `DerivativesState` gains 3 `field(default_factory=dict)` fields
  - `_refresh_loop` calls new `_refresh_per_exchange_snapshot` once per FULL cycle (not per symbol) — 3 batch calls total, metric-level independent failure isolation
- `src/bot/runner.py`:
  - `_derive_enrichment` — 3 new keys copy per-exchange dicts from `state.derivatives`
  - Both `record_rejected_signal` call sites extended with 3 new explicit kwargs (matches the existing explicit-extraction style at those sites; broader plumbing gap where 2026-04-23 derivatives fields also don't reach `rejected_signals` flagged as separate follow-up, not fixed opportunistically)

**Cost:** +3 Coinalyze calls per `refresh_interval_s` cycle (default 60s) = +180 calls/h. 40/min budget leaves comfortable headroom (existing ~20-30/min).

**Not done (intentional):**
- Runtime scoring integration (Pass 3 Optuna decides weights).
- Per-exchange liquidation / L/S ratio history (Tier B; deferred).
- Fixing the broader `record_rejected_signal` gap where 2026-04-23 single-exchange derivatives fields also go unpopulated — separate follow-up task; doing both here would have bundled an unrelated bugfix into the feature commit.

**Tests:** 82 targeted tests pass across `test_derivatives_api.py`, `test_derivatives_cache.py`, `test_derive_enrichment.py`, `test_journal_database.py`, `test_journal_derivatives.py`. New fields default to empty dict; legacy rows + fixtures unaffected; migrations idempotent.

**Re-eval triggers:**
1. **Per-exchange column coverage** on post-commit trades/rejected_signals — expect ≥95% non-empty JSON (`!= '{}'`). Zero-rate = `_per_exchange_symbol_map` not populating (check `ensure_symbol_map` log for `coinalyze_mapping` lines).
2. **Funding spread magnitude** — median |max − min| across Binance/Bybit/OKX per symbol. If consistently <10bp for 7 days, the cross-venue signal is too quiet to feature-engineer on. Expected range based on 2026-04-24 probe: 20-140bp with DOGE/SOL routinely spiking.
3. **Rate-limit saturation** — watch for `coinalyze_429` warnings. If frequent, drop per-exchange refresh to every N cycles rather than every cycle.
4. **OI share drift as Pass 3 feature importance** — if GBT assigns >0.03 feature importance to `oi_binance_share = oi_binance / (oi_binance + oi_bybit + oi_okx)`, cross-venue signal has edge; if near zero after 50+ trades, drop the OI-per-exchange columns and keep only funding-per-exchange.

### 2026-04-24 — OKX added as 6th named netflow venue (journal-only)

Operator asked whether OKX — the bot's own trading exchange — should join the per-entity netflow pool alongside Coinbase/Binance/Bybit/Bitfinex/Kraken. Argument for: OKX's derivatives volume is large and the bot trades here, so the venue's own on-chain flow is a natural self-signal.

**Live probe (2026-04-24) before commit:**

| Entity | 24h gross turnover | 24h net | Net/turnover | Max 1h \|net\| |
|---|---:|---:|---:|---:|
| Bitfinex | $1.85B | +$243M | +13.1% (bullish) | $403M |
| Kraken | $4.57B | −$439M | −9.6% (bearish) | $459M |
| Bybit | $2.60B | −$13M | −0.5% (balanced) | $112M |
| **OKX** | **$1.86B** | **−$2M** | **−0.12%** (balanced) | **$58M** |

Key finding: OKX's **gross turnover** matches Bitfinex scale — the "Arkham can't see OKX futures" concern was wrong, OKX is well-tracked on-chain. But the **24h net is structurally near-zero** because in/out are almost perfectly balanced (OKX is derivatives-heavy; traders cycle collateral in/out rapidly). The signal is likely hidden in hourly variance ($58M single-hour spikes) rather than the 24h aggregate.

**Decision:** Add OKX as a 6th venue journal-only at the 24h grain, following the same pattern as Bitfinex/Kraken (2026-04-23 night-late). Parity with existing entities + Pass 3 data for exploration. A short-window (1h) OKX slot is NOT added today — that would be a new integration pattern (existing pool is all 24h), deferred to Pass 3 if the exploration justifies it.

**Wiring (same template as Bitfinex/Kraken commit `38c3938`):**
- [on_chain_types.py](src/data/on_chain_types.py) — `OnChainSnapshot.cex_okx_netflow_24h_usd` field
- [runner.py](src/bot/runner.py) — `BotContext` field + entity tuple extended `("coinbase", "binance", "bybit", "bitfinex", "kraken", "okx")`; 3 `OnChainSnapshot(...)` construction sites, fingerprint tuple, `record_on_chain_snapshot(...)` call, `on_chain_context` dict all thread it through
- [database.py](src/journal/database.py) — CREATE TABLE column + idempotent ALTER TABLE migration + `record_on_chain_snapshot` kwarg

**Cost:** +2 histogram calls per 5-min daily-bundle cycle → +24/h (label-free, confirmed 2026-04-23 night). Total label budget untouched (558/10k/mo).

**Not done:** `_flow_alignment_score` weight, config exposure. Deliberate — same rationale as Bitfinex/Kraken: mechanical weight add without Pass 3 data would be a guess; journal capture is the minimum that unblocks Pass 3 tuning.

**Tests:** 1063 → 1063 (79 targeted tests in `test_on_chain_fetchers.py` + `test_runner_on_chain.py` + `test_journal_database.py` pass; new field defaults to `None`, existing callers unchanged, migration idempotent).

**Re-eval triggers:**
1. **OKX coverage** on `on_chain_snapshots` rows post-commit — column should be NON-NULL on ≥95% of rows. Zero-rate = slug `okx` not resolving.
2. **24h net magnitude sanity** — expect median |net| ≤$10M over 7 days (we predicted ~$0 from probe). If ≥$50M, our "balanced derivatives venue" model was wrong; re-examine.
3. **Hourly volatility capture** — the true OKX signal is in short-window buckets. If Pass 3 shows 24h OKX has near-zero feature importance but hourly volatility correlates with outcome, add a 1h-window OKX slot.

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

Node.js 18+, Python 3.11+ (actual 3.14), TradingView Desktop (subscription), Bybit Demo Trading account, Claude Code CLI.

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
tv symbol BYBIT:BTCUSDT.P
tv timeframe 15
```

### Bybit V5 Demo Trading

The bot calls Bybit's V5 REST API directly via the `pybit` Python SDK — there is no Bybit-specific MCP. Account requirements:

1. Bybit mainnet account → switch to **Demo Trading** mode (top-left badge).
2. Generate a separate API key from the Demo Trading "API" panel — these credentials are distinct from mainnet.
3. **Account type:** UNIFIED (UTA). Cross margin enabled by default.
4. **Position mode:** Hedge mode for USDT linear perps. Bot sets this once at startup via `POST /v5/position/switch-mode {category: linear, coin: USDT, mode: 3}`; idempotent if already enabled.
5. **Collateral toggles:** keep USDT + USDC "Used as Collateral" ON, BTC / ETH (or any spot wallet asset) OFF — UTA pools collateral by USD value, the bot reads `totalMarginBalance` for sizing and over-allocates if non-trading wallet balance is included in the pool.
6. **API key permissions:** Read + Trade only, never Withdrawal. IP whitelist recommended (90-day expiry without it, no expiry with).
7. Smoke test: `python scripts/test_bybit_connection.py` — exercises wallet, instruments-info, mark price, positions, open orders.

**Bybit naming:** USDT linear perp = `BTCUSDT` (Bybit-native). The bot keeps the OKX-style `BTC-USDT-SWAP` as its **internal** identifier and translates at the boundary inside `bybit_client.py`. TV ticker for charts = `BYBIT:BTCUSDT.P`.

**Demo endpoint quirk (TR ISP egress):** Some networks silently drop TCP-443 to specific CloudFront ranges that `api-demo.bybit.com` resolves to (observed: `13.249.8.0/24`). `BybitClient._maybe_pin_demo_dns()` probes each resolved IP at construction and pins a reachable edge to the requests session. If the bot logs `bybit_demo_dns_pin_failed`, switch system DNS to `8.8.8.8` / `1.1.1.1` and disable any DPI bypass tool (e.g. GoodbyeDPI, which fragments TLS in a way the demo distribution rejects).

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
- `src/execution/` — pybit V5 wrapper (sync → `asyncio.to_thread`) with OKX↔Bybit boundary translation, order router (`place_limit_entry` / `cancel_pending_entry` / `attach_algos` via trading-stop / `place_reduce_only_limit` / market fallback), REST-poll position monitor with **PENDING** state + **MFE-lock + TP-revise + maker-TP tracking** (all SL/TP mutations are single trading-stop calls), typed errors.
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

All config in `config/default.yaml` (self-documenting). Top-level sections: `bot`, `trading`, `circuit_breakers`, `analysis`, `execution`, `reentry`, `derivatives`, `economic_calendar`, `on_chain`, `bybit`, `rl`.

**`.env` keys:** `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_DEMO` (1/0), `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `ARKHAM_API_KEY`, `RISK_AMOUNT_USDT` (optional flat-$ override), `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons (unified):** `below_confluence`, `no_setup_zone`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip`, `no_sl_source`, `zero_contracts`, `tp_too_tight`, `zone_timeout_cancel`, `pending_invalidated`, `pending_hard_gate_invalidated` (mid-pending hard-gate flip). Deprecated but kept in vocabulary for legacy rows: `whale_transfer_blackout` (gate removed 2026-04-22), `wrong_side_of_premium_discount`, `htf_tp_ceiling`, `insufficient_contracts_for_split` (flags disabled). Sub-floor SL distances are **widened**, not rejected. Every reject writes to `rejected_signals` with `on_chain_context` + `confluence_pillar_scores` + `oscillator_raw_values` JSON columns.

**Circuit breakers (currently loosened for data collection):** `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.5`. Restore to `5 / 15 / 25 / 2.0` after 20+ post-pivot closed trades.

---

## Non-obvious design notes

Things that aren't self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing

- **`_MARGIN_SAFETY=0.95` + `_LIQ_SAFETY_FACTOR=0.6`** (`rr_system.py`). Reserve 5% for fees/mark drift (else Bybit `110004` insufficient-margin). Leverage capped at `floor(0.6/sl_pct)` so SL sits well inside liq distance.
- **Risk vs margin split.** R comes off `totalMarginBalance` (UTA collateral pool); leverage/notional sized against per-slot free margin (`total_margin / max_concurrent_positions`). Log emits `risk_bal=` + `margin_bal=` separately — they're different by design. UTA pools USDT + USDC; if `totalEquity` were used instead, BTC/ETH wallet balances would inflate the slot.
- **Per-symbol `ctVal`.** BTC `0.01`, ETH `0.1`, **SOL `1`**, DOGE `1000`, BNB `0.01`. Hardcoded in `bybit_client._OKX_CT_VAL`; `BybitClient.get_instrument_spec` returns these (NOT Bybit's `qtyStep`) for back-compat with OKX-era sizing math. The qty sent to Bybit is `num_contracts × ct_val`, which is always an integer multiple of `qtyStep`. Hardcoded YAML would 100× over-size SOL.
- **Fee-aware sizing** (`fee_reserve_pct=0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct` so stop-out caps near $R *after* entry+exit taker fees. `risk_amount_usdt` stays gross for RL reward comparability.
- **SL widening, not rejection.** Sub-floor SL distances widen to the per-symbol floor; notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant.
- **Flat-$ override beats percent mode.** `RISK_AMOUNT_USDT` env bypasses `balance × risk_pct`. Safety rail: override ≤ 10% of balance. Ceil-rounding on contracts makes realized SL loss ≥ target with ≤$3 overshoot.

### Execution

- **PENDING is first-class.** A filled limit without PENDING tracking would race the confluence recompute and potentially place duplicate trading-stop attachments.
- **Two TP exits per position.** Position-attached TP (set via `/v5/order/create.takeProfit` for market entries or `/v5/position/trading-stop` for limit-fills) fires as market-on-trigger (fallback); a post-only reduce-only maker limit sits at the same TP price (primary). Either closes the position flat; the other becomes irrelevant when size→0. `orderLinkId` prefix `smttp` distinguishes TP limits from entry limits (`smtbot`).
- **MFE-triggered SL lock.** At MFE ≥ 1.3R, single `set_position_tpsl(stop_loss=lock_px)` call mutates the position's SL to BE+fee_buffer. One-shot flag prevents retry. Skipped if `be_already_moved=True` or `plan_sl_price=0.0` (rehydrate sentinel).
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct=0.001`). After TP1 fill the new SL sits a hair past entry on the profit side. *Inert while `partial_tp_enabled=false` — TP1 never fires.*
- **SL/TP mutations are atomic.** Bybit V5 trading-stop is a single REST call: success replaces the value on the position; failure leaves the existing TP/SL intact. No "unprotected window" between cancel and place (the OKX-era 3-step dance is gone). 3 consecutive failures → give up + mark `be_already_moved=True` to stop spin; old SL still protects.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. Callbacks use `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)`; `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Close enrichment is non-optional.** `BybitClient.enrich_close_fill` queries `/v5/position/closed-pnl` for real `closedPnl` / `avgExitPrice` / `openFee+closeFee`. Without it every close looks BREAKEVEN and breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.
- **Phantom-cancel resistance.** `poll_pending` + `cancel_pending` only pop the row on success or idempotent-gone (Bybit codes `110001/110008/110010/170142/170213`). Transient cancel failures preserve row for next poll retry. No dropped-but-still-live orphans.
- **Startup reconcile cancels resting limits.** `_pending` is empty at startup, so any live limit is orphan by construction; `_cancel_orphan_pending_limits` walks `list_open_orders()` and cancels them. The pre-migration `_cancel_surplus_ocos` no-op was removed in the 2026-04-26 OKX cleanup — on Bybit there are no separate algo orders to orphan since TP/SL is part of the position.

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

5 Bybit USDT linear perps — BTC / ETH / SOL / DOGE / XRP (post-2026-04-25). BTC + ETH are market pillars (major-class book depth); SOL + DOGE + XRP are altcoins gated by the cross-asset veto. BNB swapped out for XRP on 2026-04-25 (operator pref); BNB override maps remain in YAML (harmless when not watched), `_OKX_TO_BYBIT_SYMBOL` / `_OKX_CT_VAL` still carry both BNB and XRP rows so re-swapping either way is a one-line YAML change. ADA pulled on 2026-04-19 (eve, OKX-era) after hitting OKX demo OI platform cap; rows preserved for the same reason.

`max_concurrent_positions=5` (every pair can hold a position simultaneously — no slot competition; confluence gate still picks setups, but cycle isn't queue-limited). Cross margin, `per_slot ≈ total_eq / 5 ≈ $100` on a $500 demo (2026-04-26 reset). R is flat $10 via `RISK_AMOUNT_USDT=10` (= 2% of starting balance — operator-tightened for the dashboard-era live observation phase; previously $100 on a $50k demo).

Cycle timing at 3m entry TF = 180s budget: typical 150–180s with 5 pairs (comfortable inside the budget after 7→5 rollback). DOGE + XRP leverage-capped at 30x via `symbol_leverage_caps` (Bybit instrument allows 75x; operator-tightened for thin-book scalp safety on momentum-driven pairs). SOL inherits global cap = 50x; BTC/ETH = 100x (Bybit instrument max).

Per-symbol overrides (YAML, ADA/XRP rows kept for easy reinstatement):
- `swing_lookback_per_symbol`: DOGE=30 (thin 3m book; ADA/XRP=30 preserved).
- `htf_sr_buffer_atr_per_symbol`: SOL=0.10 (wide-ATR, narrower buffer); DOGE=0.15; BNB inherits global 0.2.
- `session_filter_per_symbol`: SOL + DOGE=[london] only. BNB inherits global (london+new_york) as major.
- `min_sl_distance_pct_per_symbol`: BTC 0.004, ETH 0.008 (bumped 2026-04-21 eve), SOL 0.010, DOGE 0.008, BNB 0.005.

Adding a 6th+ pair: drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol`, **add an entry to `bybit_client._OKX_TO_BYBIT_SYMBOL` + `_OKX_CT_VAL`** (boundary translation + sizing), extend `affected_symbols_for` in `on_chain_types.py` for chain-native tokens, watch 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`. Coinalyze free tier supports ~8 pairs at refresh_interval_s=75s; Arkham at current cadence ≤6 pairs comfortable.

---

## Workflow commands

```bash
# Smoke test — full pipeline, one tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo run
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Auto-stop at Phase 8 data-collection gate
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --max-closed-trades 50

# Live (after demo proven — set BYBIT_DEMO=0 in .env first AND construct
# BybitClient with allow_live=True; the constructor refuses live by default)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Clear a tripped halt
.venv/Scripts/python.exe -m src.bot --clear-halt --config config/default.yaml

# Analytics
.venv/Scripts/python.exe scripts/report.py --last 7d
.venv/Scripts/python.exe scripts/factor_audit.py                   # per-symbol/session/regime WR + counter-factuals

# Diagnostic probes (ad-hoc, read-only)
.venv/Scripts/python.exe scripts/test_bybit_connection.py          # Bybit demo: wallet, instruments, mark, positions, orders
.venv/Scripts/python.exe scripts/probe_open_orders.py              # Bybit live positions + position-attached TP/SL + open orders
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
- (Counter-factual pegging on rejected signals: legacy `peg_rejected_outcomes.py`
  was removed in the 2026-04-26 OKX cleanup; needs a Bybit-native rewrite
  before Pass 3. Until then, post-migration rejected_signals carry NULL
  `hypothetical_outcome`.)
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

- **Live transition:** Bybit mainnet account (separate sub-account recommended), API key Read+Trade only with IP whitelist. Flip `BYBIT_DEMO=0` in `.env` AND construct `BybitClient(allow_live=True)` in the runner — both are required (constructor refuses live by default). Start `RISK_AMOUNT_USDT=$10-20`, `max_concurrent_positions=2`, UTA cross margin, explicit notional cap.
- **Stability period:** 2 weeks / 30 live trades with no code changes. Compare live WR + avg R to demo baseline within ±5%.
- **Scaling rules:** only after 100 live trades. Double `RISK_AMOUNT_USDT` only if 30-day rolling WR ≥ demo WR − 3% AND drawdown ≤ 15%. Asymmetric: halve on any 10-trade rolling WR < 30%.
- **Monitoring:** journal-backed dashboard (pure-Python or Streamlit). Alert on: drawdown >20%, 5-loss streak, Bybit `10006` rate-limit, fill latency P95 >2s, daily realized PnL < -2R, Arkham credit usage >80%/month.

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
- **Leverage > 100x or non-cross margin modes.** Operator cap + Bybit cap combine to forbid. Requires risk memo to revisit.

---

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, may break on TV updates → pin TV Desktop version. Data stays local.

**Bybit V5 API:** official `pybit` SDK. `demo=True` first; constructor refuses `demo=False` unless `allow_live=True` is passed explicitly. Never enable Withdrawal permission on the API key. IP whitelist strongly recommended (no expiry vs 90-day expiry). Sub-account for live. UTA hedge mode requires `mode=3` switch at startup (idempotent).

**Arkham:** read-only API, no trade-path exposure. `ARKHAM_API_KEY` stored in `.env` only. Credit budget ~7k/month at current cadence (10k trial quota). Monitor dashboard for runaway usage; auto-disable at 95% is a safety net, not primary.

**Trading:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital.

**RL:** overfitting is the #1 risk — walk-forward is mandatory. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if a structural ceiling is evident.
