# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on **Bybit V5 Demo** (UTA, hedge mode, USDT linear perps). Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes, Arkham on-chain soft signals (retiring before Phase 11). Demo-runnable end-to-end. The bot was initially piloted on OKX; demo-wick artefacts polluted fill data, so the venue switched to Bybit V5 Demo on 2026-04-25. Fresh dataset collection restarts under `rl.clean_since=2026-04-25T21:30:00Z`.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, runs tuning, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, Bybit = hands, Python = brain.

**Internal symbol format note:** the codebase keeps the canonical symbol string `BTC-USDT-SWAP` as the internal identifier across config, journal, runner state and tests — a pre-migration format preserved to avoid mass-renaming ~50 files + journal rows. The Bybit boundary translation (`BTC-USDT-SWAP ↔ BTCUSDT`) lives inside `src/execution/bybit_client.py`. Pre-migration journal rows therefore string-match new rows on `inst_id`, and the symbol-keyed override dicts in YAML need no migration.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 5 Bybit USDT linear perps — `BTC / ETH / SOL / DOGE / XRP`. 5 concurrent slots on UTA cross margin (collateral pool = USDT + USDC by USD value; BTC/ETH wallet stays out of collateral on demo per operator preference).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights + multi-TF scalp confirmation soft factors (`ltf_ribbon_alignment` 1m EMA21-55 bias 0.25, `ltf_mss_alignment` 1m MSS 0.25, `htf_mss_alignment` 15m MSS journal-only weight=0). Confluence threshold `min_confluence_score=3.75` (Pass 1 Optuna tune). *Premium/discount gate and HTF TP/SR ceiling disabled — Pass 3 candidates.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. **Position-attached TP/SL** at hard **1:1.5 RR**. Bybit V5: TP/SL fields on `/v5/order/create` for market entries, `/v5/position/trading-stop` for limit-fill attach + every subsequent SL/TP mutation. No separate algo orders to track — `journal.algo_ids` stays empty on Bybit-era rows. Mark-price triggers (`tpTriggerBy=slTriggerBy=MarkPrice`) for demo-wick immunity. Dynamic TP revision re-anchors TP to `entry ± 1.5 × sl_distance` every cycle, floor at 0.7R. **MFE-triggered SL lock (Option A)**: once MFE ≥ 1.0R, SL pulled to entry (+fee buffer); one-shot per position. **Maker-TP resting limit**: post-only reduce-only limit sits at TP price alongside the position-attached TP — captures wicks as maker, avoids trigger latency. **Zone timeout**: 2 entry-TF bars (~6 min on 3m).
- **Sizing:** fee-aware ceil on per-contract total cost so total realized SL loss (price + fee reserve) ≥ target_risk across every symbol. Overshoot bounded by one per-contract step (< $3 per position). Operator override via `RISK_AMOUNT_USDT` env bypasses percent-mode sizing; 10%-of-balance safety ceiling. Per-symbol `min_sl_distance_pct_per_symbol` floors: BTC 0.003, ETH 0.006, SOL 0.008, DOGE/XRP 0.006, BNB 0.004. Bybit boundary in `bybit_client.py` translates internal-format integer `num_contracts` to base-coin `qty` via per-symbol `_INTERNAL_CT_VAL` map (BTC 0.01, ETH 0.1, SOL 1, DOGE 1000, BNB 0.01); Bybit's `qtyStep` always cleanly divides the resulting qty.
- **Journal:** async SQLite, schema includes `on_chain_context`, `demo_artifact`, `confluence_pillar_scores`, `oscillator_raw_values` (all JSON). Separate tables: `rejected_signals` (counter-factual outcome pegged), `on_chain_snapshots` (Arkham state mutation time-series), `whale_transfers` (raw WS events for Phase 9 directional learning), `position_snapshots` (5-min cadence intra-trade rows for RL trajectory).
- **On-chain (Arkham):** runtime soft signals only — daily bias ±15%, hourly stablecoin pulse +0.75 threshold penalty, altcoin-index +0.5 penalty on misaligned altcoin trades, **flow_alignment** 6-input directional score (stablecoin + BTC/ETH + Coinbase/Binance/Bybit 24h netflow; weights 0.25/0.25/0.15/0.15/0.10/0.10; default penalty 0.25), **per_symbol_cex_flow** binary penalty on misaligned symbol 1h volume (default 0.25, $5M floor). Bitfinex + Kraken + OKX 24h netflow captured journal-only (4th/5th/6th venues; not yet wired into `_flow_alignment_score`). Whale HARD GATE removed — WS listener feeds `whale_transfers` journal at $10M threshold. Per-symbol token_volume fallback for Arkham null returns. Per-entity netflow uses `/transfers/histogram` 1h-bucket (not `/flow/entity` daily-bucket — was freezing). Daily-bundle refresh on 5-min monotonic cadence. Credit-safe via v2 persistent WS streams + filter-fingerprint cache. **Arkham retires 2026-05-04 (soft) → 2026-05-18 (off) before trial expiry 2026-05-20 — see Forward roadmap.**
- **Pass 2 instrumentation:** every trade / reject row captures `confluence_pillar_scores` (factor name → weight dict) and `oscillator_raw_values` (per-TF dict with 1m/3m/15m OscillatorTableData numerics). Both sourced from existing runner TF-switch cache — zero extra TV latency.
- **Tests:** ~1060, mostly green. Demo-runnable end-to-end.
- **Data cutoff (`rl.clean_since`):** `2026-04-25T21:30:00Z` — **Bybit migration cut, dashboard ile sync**. Pre-Pass-2.5 DB archived as `data/trades.db.pre_pass25_2026-04-29T222601Z`. Pre-migration OKX DB still in `data/trades.db.pre_bybit_2026-04-25T214500Z`. Pass 1 baseline before that: `data/trades.db.pass1_backup_2026-04-22T203324Z`.

---

## Recent changes (last 7 days)

Per-commit detail lives in `git log`. This section captures high-level shifts only — the design rationale lives in the relevant `## Non-obvious design notes` / `## Configuration` sections.

**2026-04-22 — Pass 1 closed + Pass 2 instrumentation.** Confluence threshold 3 → 3.75 (Optuna walk-forward over 42-trade dataset). Whale hard gate removed; replaced by `flow_alignment` 6-input soft signal + `per_symbol_cex_flow` per-symbol penalty. Journal gained `confluence_pillar_scores`, `oscillator_raw_values` (1m/3m/15m), `whale_transfers` table. Pending limits now re-run hard gates per poll; first failing gate cancels with `pending_hard_gate_invalidated`.

**2026-04-23 — Arkham netflow freeze fix + journal enrichment.** Per-entity netflow rewritten from `/flow/entity/{entity}` daily-bucket (frozen at UTC day close) to `/transfers/histogram?granularity=1h&time_last=24h` rolling 24h. Daily-bundle refresh flipped UTC-day gate → 5-min monotonic cadence (`on_chain.daily_snapshot_refresh_s: 300`). SOL `/token/volume` null fallback to `/transfers/histogram`. Bitfinex + Kraken added as 4th/5th journal-only venues. Trade rows enriched with 9 derivatives fields + `liq_heatmap_top_clusters_json`.

**2026-04-24 — SL floor bump reverted (lockstep lesson).** 2026-04-23 evening per-symbol SL floor bump (BTC 0.4→0.6%, ETH 0.8→1.0%, etc.) reverted same day after 9 post-bump trades showed WR collapse 66.7% → 22.2% and hold-time 5.6×. Mechanism: under fixed 1:2 RR, `tp = entry ± sl × rr` mechanically widens TP when SL widens; MFE-lock distance also scales proportionally and triggers later. **Lockstep mandate: SL floors and RR cap must move together — never ship one without the other.** Saved to `feedback_sl_floor_bump_backfired.md`.

**2026-04-26 — Bybit cut + dashboard era.** `rl.clean_since=2026-04-25T21:45Z` anchored at Bybit migration cut (fresh DB). VWAP daily-reset 20-min blackout (UTC 23:55-00:15) — Pine `ta.vwap` re-anchors at UTC 00:00 with collapsed bands; new entries / pending fills inside this window are rejected with `vwap_reset_blackout`. `confluence_pillar_scores` zone-wrap forwarding bug fixed (was empty `{}` on every zone-based entry). New `position_snapshots` table — 5-min cadence rows per OPEN position with live mark/PnL, MFE/MAE in R, current SL/TP + lifecycle flags + derivatives/oscillator drift, joined to `trades.trade_id`. Read-only single-page FastAPI dashboard sibling process (RO connection, ~2 Bybit wallet calls/poll). Per-venue × per-asset (BTC/ETH/stables) 24h netflow capture (journal + UI). Demo balance reset $5000 → $500; `RISK_AMOUNT_USDT=$10`. Whale threshold 150M → 75M.

**2026-04-27 — Schema cleanup + journal plumbing.** 27 dead/constant columns dropped from `trades` / `rejected_signals` / `on_chain_snapshots` (algo orders, peg-bound counter-factual fields, 1-distinct constants). F1-F6 plumbing fixes: derivatives enrichment forwarded on rejects, zone metadata + `close_reason` inference, `vwap_3m_distance_atr_now` writer (was 100% NULL), `cancel_pending` race fix with `get_order` verify on Bybit gone-codes (prevents silent fill loss when cancel races a fill). Phantom-cancel SOL short data repair. Mekanizma 1+2 (pending_confluence_decay + counter-confluence open-position protection) shipped AM, **reverted PM same day** — too noisy on borderline confluence threshold (30% decay-cancel rate, 50% recovery flicker).

**2026-04-28 — Scalp tighten + multi-TF MSS factors.** Atomic lockstep tighten: SL floors -25% (BTC 0.6→0.3%, ETH 1.0→0.6%, SOL 1.2→0.8%, DOGE/XRP 1.0→0.6%, BNB 0.7→0.4%) + `target_rr_ratio` 2.0→1.5 + `min_rr_ratio` 1.5→1.2 + `tp_min_rr_floor` 1.0→0.7 + `sl_lock_mfe_r` 1.3→1.0 + `zone_max_wait_bars` 7→2. Breakeven WR shifts 33% → 40%. Three new soft factors: `ltf_ribbon_alignment` (1m EMA21-55 bias, weight 0.25), `ltf_mss_alignment` (1m MSS, weight 0.25), `htf_mss_alignment` (15m MSS, **weight 0** journal-only — Pass 3 importance test). Whale threshold 75M → 10M after label-quota audit confirmed zero burn (WS payload carries `arkhamEntity.name` pre-resolved server-side; no `/intelligence/address` lookup in our path). Dashboard `windowInfo` UTC+3 fix. `get_positions` IEEE 754 quantization fix in `bybit_client.py:1043` — `round()` on base→contract qty division (`size / ct_val`) prevents fractional-ct_val drift from mis-firing `_detect_tp1_and_move_sl`'s "size shrank" branch and silently disabling MFE-lock.

**2026-04-29 — Roadmap reset.** Pass 3 declared Arkham-FREE; Arkham retirement plan locked (soft 2026-05-04 → off 2026-05-18 → trial expires 2026-05-20). Phase 12 trimmed to two candidates: Deep RL + HTF Order Block / Breaker Block ecosystem. Saved to `project_roadmap_reset_2026-04-29.md`.

**2026-04-29 — Pine TV resync + session drawings off.** TV resynced from `ae44ab9` (1156 lines) → HEAD (1175); activates 3m VWAP ±1σ band path that was inactive since `cce646e` (2026-04-19) — pre-resync `vwap_retest` zones (24/44 trades) all used ATR fallback, post-resync use Convention X band-anchor. `enableSessions` default `true→false` (PDH/PDL/PWH/PWL + Asia/London/NY drawings; no Python consumer; skips `request.security("D"/"W")` lookback for cycle perf).

**2026-04-29 — Macro blackout log throttle.** `_run_one_symbol` `macro_event_blackout` dalı per-symbol 60s throttle + blackout check `symbol_cycle_start` log'undan önceye taşındı. Fed Interest Rate Decision penceresinde gözlenen ~120 satır/dk → 5 satır/dk (sembol başına dakikada bir). Davranış aynı: blackout penceresi, decision, TV chart cycling tümü değişmedi.

**2026-04-29 — Pass 2 50-trade gate clear + Pass 2.5 başlangıç (cut hizalama).** Bybit-era 50 closed trade (W26/L24, +9.77R, WR 52%, Sharpe 0.15, Profit Factor 1.34). `rl.clean_since` `2026-04-26T00:29:30Z` → `2026-04-25T21:30:00Z` (ilk Bybit trade `21:38:07`'den 8 dk önce; 50 trade'in tamamı dashboard ile sync). Bybit `closed_pnl` çapraz check'inde 1 ekstra row (manuel test trade `21:28`, 1.05 BTC -$86.83) bot başlamadan önce — DB doğru, ignore. Pre-Pass-2.5 DB archived `data/trades.db.pre_pass25_2026-04-29T222601Z` (atomic sqlite3 backup). Pass 2.5 scope: counter-factual reject pegger Bybit-native rewrite (proposed SL/TP write at reject time + Bybit kline forward-walk → `hypothetical_outcome`).

**2026-04-29 — Pass 2.5 tamamlandı: 1634 reject pegged (Pass 3 GBT counter-factual matrix hazır).** 6 kolon schema'ya re-add (`proposed_sl_price/tp_price/rr_ratio` + `hypothetical_outcome/bars_to_tp/bars_to_sl`); `_record_reject` plumbing live insert path'inde proposed_* yazıyor (pre-fill: ATR-based what-if, pending-cancel: plan_sl/tp forward); pure helper `src/strategy/what_if_sltp.py` runner+backfill ortak; backfill script ~1671 legacy reject'i retro stamp'ledi (1634 pegglenebilir, 25 reason-skip, 12 missing price/atr); Bybit-native pegger script `/v5/market/kline` 3m forward-walk ile 1634 row'u WIN/LOSS/TIMEOUT damgaladı. **Sonuç: hypothetical_WR=36.4% (445 W / 776 L / 413 TIMEOUT)**, closed-trade WR=52% — bot reject'leri sistematik alpha üretiyor. **Pass 3 GBT için kritik per-reject-reason WR sinyalleri:** `ema_momentum_contra` 19.2% WR (n=254, çok güçlü gate, KORU); `cross_asset_opposition` 51.4% WR (n=56, **zayıf gate** — Pass 3 toggle adayı); `zone_timeout_cancel` 45.7% WR (n=606, zone_max_wait_bars artırma adayı); `below_confluence` 32.8% WR (n=563, threshold dengeli). Bonus: Pass 2.5 sırasında 2 KRİTİK schema bug fix'i — `idx_rejected_outcome` `_SCHEMA`'dan kaldırıldı (DB connect crash'iyordu) + `proposed_*/hypothetical_*` DROP statement'ları `_MIGRATIONS`'tan kaldırıldı (her connect'te DROP-then-ADD ile data destroying ediyordu). `analyze.py` Arkham-FREE flag eklendi (Pass 3 prep). Yan task: 8 pre-existing test fail temizlendi (PR `claude/focused-lewin-6b33e0` — bonus: `update_algo_ids` her BE-move'da silently fail eden gerçek bug bulundu + fix'lendi).

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

**Bybit naming:** USDT linear perp = `BTCUSDT` (Bybit-native). The bot keeps the canonical `BTC-USDT-SWAP` as its **internal** identifier and translates at the boundary inside `bybit_client.py`. TV ticker for charts = `BYBIT:BTCUSDT.P`.

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
- `src/execution/` — pybit V5 wrapper (sync → `asyncio.to_thread`) with internal-canonical↔Bybit boundary translation, order router (`place_limit_entry` / `cancel_pending_entry` / `attach_algos` via trading-stop / `place_reduce_only_limit` / market fallback), REST-poll position monitor with **PENDING** state + **MFE-lock + TP-revise + maker-TP tracking** (all SL/TP mutations are single trading-stop calls), typed errors.
- `src/journal/` — async SQLite, schema v3 trade records (+ `on_chain_context`, `demo_artifact`), `rejected_signals` + counter-factual stamps, `on_chain_snapshots` time-series, `position_snapshots` intra-trade trajectory, pure-function reporter.
- `src/dashboard/` — read-only FastAPI sibling process, single-page HTML, RO SQLite connection (`?mode=ro`), polls `/api/state` every 30s. Live Bybit wallet probe when `BYBIT_API_KEY/SECRET` set.
- `src/bot/` — YAML/env config, async outer loop (`BotRunner.run_once` — closes → snapshot → pending → per-symbol cycle), on-chain snapshot scheduler, CLI entry.

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

Plus multi-TF scalp confirmation soft factors: `ltf_ribbon_alignment` (1m EMA21-55 bias, 0.25), `ltf_mss_alignment` (1m MSS, 0.25), `htf_mss_alignment` (15m MSS, weight 0 journal-only).

### Hard gates (reject, not scored)

`displacement_candle` · `ema_momentum_contra` · `vwap_misaligned` · `cross_asset_opposition` (altcoin veto when BTC+ETH both oppose) · `vwap_reset_blackout` (UTC 23:55-00:15 daily VWAP re-anchor window). *`premium_discount_zone` + `htf_tp_ceiling` wired but disabled (Pass 3 soft-weighted re-add candidates). Whale `whale_transfer_blackout` gate REMOVED 2026-04-22; directional intuition moved to `flow_alignment` soft signal.*

### Arkham soft signals (threshold bumps, not gates) — retiring 2026-05-04

All bump `min_confluence_score` when misaligned; aligned → 0. Penalties zero out 2026-05-04 per Arkham retirement plan; journal writes continue until 2026-05-18.

- **Daily bias** — 24h CEX BTC netflow + stablecoin balance → bullish/bearish/neutral. Confluence multiplier `×(1±0.15)`.
- **Stablecoin pulse** — hourly USDT+USDC CEX netflow. Misaligned → `+0.75` threshold bump.
- **Altcoin index** — 0–100 scalar. ≤25 penalises altcoin longs; ≥75 penalises altcoin shorts. `+0.5` bump. BTC/ETH exempt.
- **flow_alignment** — 6-input directional score `[-1, +1]`: stablecoin pulse (0.25) + BTC netflow (0.25) + ETH (0.15) + Coinbase (0.15) + Binance (0.10) + Bybit (0.10). Stables IN = bullish, BTC/ETH/entity OUT = bullish. Misaligned → `0.25 × |score|` bump.
- **per_symbol_cex_flow** — traded symbol's own 1h token flow. INTO CEX = bearish for symbol, OUT = bullish. Binary `+0.25` bump above $5M floor.

### Zone-based entry

`confluence ≥ effective_threshold → setup_planner picks a ZoneSetup → post-only limit at zone edge → 2 bars wait → fill | cancel`.

Zone source priority: **vwap_retest → ema21_pullback → fvg_entry (3m) → sweep_retest → liq_pool_near**. VWAP-band anchor uses Convention X (0.7 long / 0.3 short, entry at VWAP ± 0.4σ).

Position lifecycle: `PENDING → FILLED → OPEN → CLOSED` or `PENDING → CANCELED`.

### Regime awareness

ADX (Wilder, 14) classifies `UNKNOWN / RANGING / WEAK_TREND / STRONG_TREND`. Under `STRONG_TREND`, trend-continuation factors get 1.5× and sweep factors 0.5×; `RANGING` mirrors. Journal stamps `trend_regime_at_entry` on every trade.

---

## Configuration

All config in `config/default.yaml` (self-documenting). Top-level sections: `bot`, `trading`, `circuit_breakers`, `analysis`, `execution`, `reentry`, `derivatives`, `economic_calendar`, `on_chain`, `bybit`, `rl`.

**`.env` keys:** `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_DEMO` (1/0), `COINALYZE_API_KEY`, `FINNHUB_API_KEY`, `ARKHAM_API_KEY`, `RISK_AMOUNT_USDT` (optional flat-$ override), `TV_MCP_PORT`, `LOG_LEVEL`.

**Reject reasons (unified):** `below_confluence`, `no_setup_zone`, `vwap_misaligned`, `vwap_reset_blackout`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip`, `no_sl_source`, `zero_contracts`, `tp_too_tight`, `zone_timeout_cancel`, `pending_invalidated`, `pending_hard_gate_invalidated` (mid-pending hard-gate flip). Deprecated but kept in vocabulary for legacy rows: `whale_transfer_blackout`, `wrong_side_of_premium_discount`, `htf_tp_ceiling`, `insufficient_contracts_for_split`, `pending_confluence_decay`, `EARLY_CLOSE_COUNTER_CONFLUENCE` (flags disabled / mechanisms reverted). Sub-floor SL distances are **widened**, not rejected. Every reject writes to `rejected_signals` with `on_chain_context` + `confluence_pillar_scores` + `oscillator_raw_values` JSON columns.

**Circuit breakers (currently loosened for data collection):** `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.2`. Restore to `5 / 15 / 25 / 1.5` after Pass 2 closes.

---

## Non-obvious design notes

Things that aren't self-evident from the code. Inline comments cover the *what*; these cover the *why it exists*.

### Sizing

- **`_MARGIN_SAFETY=0.95` + `_LIQ_SAFETY_FACTOR=0.6`** (`rr_system.py`). Reserve 5% for fees/mark drift (else Bybit `110004` insufficient-margin). Leverage capped at `floor(0.6/sl_pct)` so SL sits well inside liq distance.
- **Risk vs margin split.** R comes off `totalMarginBalance` (UTA collateral pool); leverage/notional sized against per-slot free margin (`total_margin / max_concurrent_positions`). Log emits `risk_bal=` + `margin_bal=` separately — they're different by design. UTA pools USDT + USDC; if `totalEquity` were used instead, BTC/ETH wallet balances would inflate the slot.
- **Per-symbol `ctVal`.** BTC `0.01`, ETH `0.1`, **SOL `1`**, DOGE `1000`, BNB `0.01`, XRP `100`. Hardcoded in `bybit_client._INTERNAL_CT_VAL`; `BybitClient.get_instrument_spec` returns these (NOT Bybit's `qtyStep`) for back-compat with the pre-migration sizing math. The qty sent to Bybit is `num_contracts × ct_val`, which is always an integer multiple of `qtyStep`. Hardcoded YAML would 100× over-size SOL.
- **Integer contract round-trip.** Internal `num_contracts` is integer by construction. Inverse direction (Bybit base coin → internal contracts via `size / ct_val`) needs `round()` because IEEE 754 float division drifts at ULP scale for fractional `ct_val` (e.g. ETH 0.7 / 0.1 = 6.999...). Without rounding, `_detect_tp1_and_move_sl` mis-fires its "size shrank" branch and silently disables MFE-lock.
- **Fee-aware sizing** (`fee_reserve_pct=0.001`). Sizing denominator widens to `sl_pct + fee_reserve_pct` so stop-out caps near $R *after* entry+exit taker fees. `risk_amount_usdt` stays gross for RL reward comparability.
- **SL widening, not rejection.** Sub-floor SL distances widen to the per-symbol floor; notional auto-shrinks (`risk_amount / sl_pct`) so R stays constant.
- **Flat-$ override beats percent mode.** `RISK_AMOUNT_USDT` env bypasses `balance × risk_pct`. Safety rail: override ≤ 10% of balance. Ceil-rounding on contracts makes realized SL loss ≥ target with ≤$3 overshoot.
- **Lockstep mandate (SL floors ↔ RR cap).** Under fixed RR, `tp = entry ± sl × rr` mechanically widens TP when SL widens; MFE-lock distance also scales proportionally. Tightening or loosening the per-symbol SL floor without simultaneously moving `target_rr_ratio` produces asymmetric outcomes (proven harmful by 2026-04-23/24 SL floor bump postmortem). Both knobs move together or not at all.

### Execution

- **PENDING is first-class.** A filled limit without PENDING tracking would race the confluence recompute and potentially place duplicate trading-stop attachments.
- **Two TP exits per position.** Position-attached TP (set via `/v5/order/create.takeProfit` for market entries or `/v5/position/trading-stop` for limit-fills) fires as market-on-trigger (fallback); a post-only reduce-only maker limit sits at the same TP price (primary). Either closes the position flat; the other becomes irrelevant when size→0. `orderLinkId` prefix `smttp` distinguishes TP limits from entry limits (`smtbot`).
- **MFE-triggered SL lock.** At MFE ≥ 1.0R, single `set_position_tpsl(stop_loss=lock_px)` call mutates the position's SL to BE+fee_buffer. One-shot flag prevents retry. Skipped if `be_already_moved=True` or `plan_sl_price=0.0` (rehydrate sentinel).
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct=0.001`). After TP1 fill the new SL sits a hair past entry on the profit side. *Inert while `partial_tp_enabled=false` — TP1 never fires.*
- **SL/TP mutations are atomic.** Bybit V5 trading-stop is a single REST call: success replaces the value on the position; failure leaves the existing TP/SL intact. No "unprotected window" between cancel and place. 3 consecutive failures → give up + mark `be_already_moved=True` to stop spin; old SL still protects.
- **Threaded callback → main loop.** `PositionMonitor.poll()` runs in `asyncio.to_thread`. Callbacks use `asyncio.run_coroutine_threadsafe(coro, ctx.main_loop)`; `create_task` from worker thread raises `RuntimeError: no running event loop`.
- **Close enrichment is non-optional.** `BybitClient.enrich_close_fill` queries `/v5/position/closed-pnl` for real `closedPnl` / `avgExitPrice` / `openFee+closeFee`. Without it every close looks BREAKEVEN and breakers never trip.
- **In-memory register before DB.** `monitor.register_open` + `risk_mgr.register_trade_opened` happen *before* `journal.record_open` — a DB failure logs an orphan rather than losing a live position.
- **Phantom-cancel resistance.** `cancel_pending` does NOT treat Bybit gone-codes (`110001/110008/110010/170142/170213`) as idempotent success — they cover both "already cancelled" AND "already filled". `cancel_pending` calls `get_order` to check actual status: `Filled` → emit FILLED event (`reason="phantom_cancel_recovery"`), `Cancelled/Rejected` → CANCELED event. Verify failure → unverified-cancel fallback. Without this, cancel-vs-fill races silently lose fills and leave naked positions.
- **Startup reconcile cancels resting limits.** `_pending` is empty at startup, so any live limit is orphan by construction; `_cancel_orphan_pending_limits` walks `list_open_orders()` and cancels them. On Bybit there are no separate algo orders to orphan since TP/SL is part of the position.

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
- **MFE/MAE in-memory only.** `_Tracked.mfe_r_high/mae_r_low` rebuild from 0/0 on restart. Acceptable v1; revisit if Pass 3 RL feature importance shows entry-window sensitivity.

---

## Currency pair notes

5 Bybit USDT linear perps — BTC / ETH / SOL / DOGE / XRP. BTC + ETH are market pillars (major-class book depth); SOL + DOGE + XRP are altcoins gated by the cross-asset veto. BNB swapped out for XRP on 2026-04-25 (operator pref); BNB override maps remain in YAML (harmless when not watched), `_INTERNAL_TO_BYBIT_SYMBOL` / `_INTERNAL_CT_VAL` still carry both BNB and XRP rows so re-swapping either way is a one-line YAML change. ADA pulled on 2026-04-19 after hitting the pre-migration demo OI platform cap; rows preserved for the same reason.

`max_concurrent_positions=5` (every pair can hold a position simultaneously — no slot competition; confluence gate still picks setups, but cycle isn't queue-limited). Cross margin, `per_slot ≈ total_eq / 5 ≈ $100` on a $500 demo. R is flat $10 via `RISK_AMOUNT_USDT=10` (= 2% of starting balance — operator-tightened for the dashboard-era live observation phase).

Cycle timing at 3m entry TF = 180s budget: typical 150–180s with 5 pairs (comfortable inside the budget after 7→5 rollback). DOGE + XRP leverage-capped at 30x via `symbol_leverage_caps` (Bybit instrument allows 75x; operator-tightened for thin-book scalp safety on momentum-driven pairs). SOL inherits global cap = 50x; BTC/ETH = 100x (Bybit instrument max).

Per-symbol overrides (YAML, ADA/XRP/BNB rows kept for easy reinstatement):
- `swing_lookback_per_symbol`: DOGE=30 (thin 3m book; ADA/XRP=30 preserved).
- `htf_sr_buffer_atr_per_symbol`: SOL=0.10 (wide-ATR, narrower buffer); DOGE=0.15; BNB inherits global 0.2.
- `session_filter_per_symbol`: SOL + DOGE=[london] only. BNB inherits global (london+new_york) as major.
- `min_sl_distance_pct_per_symbol`: BTC 0.003, ETH 0.006, SOL 0.008, DOGE 0.006, XRP 0.006, BNB 0.004.

Adding a 6th+ pair: drop into `trading.symbols`, add `internal_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol`, **add an entry to `bybit_client._INTERNAL_TO_BYBIT_SYMBOL` + `_INTERNAL_CT_VAL`** (boundary translation + sizing), extend `affected_symbols_for` in `on_chain_types.py` for chain-native tokens, watch 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`. Coinalyze free tier supports ~8 pairs at refresh_interval_s=75s.

---

## Workflow commands

```bash
# Smoke test — full pipeline, one tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo run
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Auto-stop at data-collection gate
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

Sequenced in **Pass** (data + tune cycles) + **Phase** (live deployment + post-stable) vocabulary. Pass 1 is complete; Pass 2 (data collection) is active. Pass 2.5 + Pass 3 + Arkham retirement land before Phase 11 (live transition); Phase 12 is post-stable.

**Top-level RL/tune contract (operator decision 2026-04-29):** Pass 3 Bayesian tune **does NOT use Arkham features.** Arkham trial expires 2026-05-20; the project is migrating off Arkham before Phase 11. All on-chain columns continue to be journaled for archive purposes during Pass 2 / 2.5, but Pass 3 GBT's feature matrix excludes them and runtime soft-signals turn off via the retirement plan below.

### Pass 1 — COMPLETE (2026-04-22)

Combined on a 42-trade dataset (`rl.clean_since=2026-04-19T19:55:00Z`):

- **Data collection:** demo bot ran 2026-04-19 through 2026-04-22, 42 closed trades (WR 47.6%, net +13.46R, Sharpe 0.33).
- **GBT analysis** via `scripts/analyze.py` — xgboost feature importance + SHAP + per-factor WR + rejected-signal counter-factual. Arkham segmentation descriptive only (coverage inconsistent across the window).
- **Bayesian tune** via `scripts/tune_confluence.py` — Optuna TPE over NON-Arkham knobs (confluence_threshold + 3 hard gate bools), walk-forward 73/27 split.
- **Applied tune:** `min_confluence_score` 3 → 3.75 (curve plateau; +3.8pp WR on historical sample). No other knobs changed (Arkham coverage inconsistent, per-pillar + per-TF oscillator data not yet captured — both instrumented for Pass 2).
- **Concurrent feature work:** whale hard gate removed, `flow_alignment_score` 6-input + `per_symbol_cex_flow_penalty` soft signals live, `whale_transfers` + `confluence_pillar_scores` + `oscillator_raw_values (1m/3m/15m)` journal instrumentation shipped.

### Pass 2 — Data collection (active)

**Goal:** uniform-feature dataset on the post-Bybit cut. Trade rows carry strategy-internal feature columns (per-pillar scores, per-TF oscillator, derivatives enrichment, multi-TF MSS factors) that Pass 3 GBT will train on. Arkham columns continue to journal for archive but are NOT a Pass 2 gate criterion.

- Operator restarted bot on 2026-04-25T21:45Z (Bybit migration cut). `rl.clean_since` anchored there; pre-migration DB archived.
- 2026-04-28 scalp tighten + multi-TF MSS factors shipped. Marker timestamp `2026-04-27T21:15:13Z` segments POST-REVERT (Bybit) vs POST-2026-04-28-TIGHTEN dataset windows.
- Demo bot runs. No code changes unless factor-audit reveals a regression.
- Run `scripts/factor_audit.py` every ~10 closed trades.
- Passive accumulation of `confluence_pillar_scores`, `oscillator_raw_values`, `derivatives` enrichment, `position_snapshots` rows.

**Gate to leave (revised 2026-04-29):**
- ≥30 closed trades (operator targets 50 for comfort margin)
- `confluence_pillar_scores` populated on 100% of post-2026-04-26 rows
- `oscillator_raw_values` populated on ≥90% per TF (1m may dip; 3m + 15m ~100%)
- Net PnL ≥ 0
- WR ≥ 45%
- **Arkham `on_chain_context` coverage is NOT a gate criterion** (RL won't use it)

**If the gate fails:** factor-audit is diagnostic. Expect 1-2 iterations of per-symbol confluence threshold tuning before the gate holds. Do NOT start Pass 2.5 / Pass 3 until the gate holds — overfitting a broken dataset is worse than collecting more clean data.

### Pass 2.5 — Pre-Pass-3 transition (after 50-trade gate)

Single-task transition window between data collection and Bayesian tune. ~2-3 day scope. Triggered when Pass 2 gate clears.

**Reject signal pegger Bybit-native rewrite.** Legacy `peg_rejected_outcomes.py` was deleted in the 2026-04-26 cleanup (it called pre-Bybit kline endpoints). Pass 3 GBT's counter-factual analysis ("which rejected signals would have actually won?") needs `rejected_signals.hypothetical_outcome` populated.

- Read every `rejected_signals` row with NULL `hypothetical_outcome` (~all post-2026-04-26 rows)
- Fetch Bybit `/v5/market/kline` candles from the reject `signal_timestamp` forward (boundary translation `BTC-USDT-SWAP → BTCUSDT`)
- Walk candle-by-candle: did the proposed SL or proposed TP hit first?
- Stamp `WIN` / `LOSS` / `TIMEOUT` + `bars_to_outcome` on the row
- Async batch fetch (rate-limit conscious)

**Output:** every Pass 2 reject row carries a hypothetical outcome; Pass 3 GBT can train hard-gate-toggle decisions on counter-factual outcome data instead of just on closed-trade outcomes.

**Note:** `proposed_sl_price` / `proposed_tp_price` columns were dropped in the 2026-04-27 schema cleanup. The pegger rewrite must also re-add a `_record_reject` plumbing path that computes proposed SL/TP at reject time and persists them, otherwise the pegger has nothing to walk against. This is part of the Pass 2.5 scope.

### Pass 3 — Bayesian tune (Arkham-FREE)

**Goal:** tune strategy-internal knobs on uniform-feature dataset. Arkham knobs are excluded by the top-level RL contract.

**Tunable knob set (Optuna TPE + walk-forward 73/27 split):**

- **Per-pillar weights** — 5 pillar continuous (`confluence_pillar_scores` column). Highest-leverage tune target.
- **Per-symbol `min_confluence_score`** — Pass 1 kept global at 3.75; per-symbol override may be tuned (BTC vs altcoin scoring sensitivity differs).
- **3 hard gate toggles** — `vwap_hard_veto_enabled`, `ema_veto_enabled`, `cross_asset_veto_enabled`. Counter-factual pegger output (Pass 2.5) drives this decision.
- **Multi-TF MSS feature importance** — particularly `htf_mss_alignment` (currently weight=0 journal-only since 2026-04-28). GBT importance threshold decides whether to flip YAML default to 0.25.
- **2026-04-28 tighten knobs:**
  - Per-symbol `min_sl_distance_pct` floors (BTC 0.3% / ETH 0.6% / SOL 0.8% / DOGE+XRP 0.6% / BNB 0.4%)
  - `target_rr_ratio` (currently 1.5)
  - `sl_lock_mfe_r` (currently 1.0R)
  - `zone_max_wait_bars` (currently 2)
  - `tp_min_rr_floor` (currently 0.7)

**Method:**
- Extend `scripts/replay_decisions.py` with pillar-reweight replay path (scaffold present).
- `scripts/tune_confluence.py` — Optuna TPE with expanded `suggest_config`.
- `scripts/analyze.py` GBT auto-extends feature matrix when `oscillator_raw_values` non-empty: continuous features (WT magnitude, RSI band, Stoch K/D, momentum). **Arkham segments explicitly DROPPED** from feature matrix.

**Gate to leave:** Pass 3 Optuna OOS net_R ≥ 0.5 × IS net_R AND OOS WR ≥ IS WR − 5pp. Otherwise structural ceiling — hold tuning, accumulate more data, proceed to Phase 11 stability rather than over-fitting a small dataset.

### Arkham retirement plan

Arkham trial key expires **2026-05-20**. Off-ramp on this schedule (operator-confirmed 2026-04-29):

| Date (~) | Step |
|---|---|
| **2026-04-29** | Decision: Pass 3 trains Arkham-FREE; runtime soft-signals turn off ahead of trial expiry |
| **~2026-05-01** | Pass 2 closes (50-trade gate clears) |
| **~2026-05-04** | **Arkham runtime soft-retire:** YAML penalties → 0 (`flow_alignment_penalty: 0.0`, `stablecoin_pulse_penalty: 0.0`, `altcoin_index_modifier_delta: 0.0`, `daily_bias_modifier_delta: 0.0`, `per_symbol_cex_flow_penalty: 0.0`). **Journal writes continue** (operator wants archived DB snapshots). Code paths kept; effect zeroed via penalty knobs. |
| **2026-05-04 → 2026-05-15** | Pass 3 Bayesian tune runs over Arkham-FREE strategy state — tuning results unbiased by Arkham penalty effects |
| **~2026-05-15** | Pass 3 results applied; Phase 11 prep begins (mainnet sub-account, IP whitelist, key generation) |
| **~2026-05-18** | **Arkham journal writes turn off** (`on_chain.enabled: false`). Label budget cleanly closed before trial-expiry deadline. WS listener stops, REST calls cease. |
| **2026-05-20** | Arkham trial expires. API calls already 0 since 05-18; zero friction. |
| **Post-Phase-11-stable** | **Hard removal:** delete `src/data/on_chain*.py`, `src/data/arkham_ws.py`, related config schema entries. **Schema columns kept in DB** (`on_chain_context`, `cex_*_netflow_24h_usd`, `whale_transfers` table, etc.) — drops not needed; archive value > schema simplicity. |

**Re-eval triggers:**
1. Soft-retire date can slip to 2026-05-08 if Pass 2 gate slips. Trial-expiry date is fixed; journal-off must precede it by ≥48h.
2. If a Pass 2 / 2.5 bug needs Arkham state for diagnosis between soft-retire and journal-off, temporarily lift `enabled: true` for read-only inspection. Don't toggle penalty knobs back on (would corrupt the Arkham-FREE tune dataset).
3. Schema column drops are **explicitly deferred indefinitely** — re-evaluate only if DB size becomes a problem (currently negligible).

### Phase 11 — Live transition + scaling

**Goal:** move from demo to live with survivable sizing, scale by performance.

- **Live transition:** Bybit mainnet account (separate sub-account recommended), API key Read+Trade only with IP whitelist. Flip `BYBIT_DEMO=0` in `.env` AND construct `BybitClient(allow_live=True)` in the runner — both are required (constructor refuses live by default). Start `RISK_AMOUNT_USDT=$10-20`, `max_concurrent_positions=2`, UTA cross margin, explicit notional cap.
- **Stability period:** 2 weeks / 30 live trades with no code changes. Compare live WR + avg R to demo baseline within ±5%.
- **Scaling rules:** only after 100 live trades. Double `RISK_AMOUNT_USDT` only if 30-day rolling WR ≥ demo WR − 3% AND drawdown ≤ 15%. Asymmetric: halve on any 10-trade rolling WR < 30%.
- **Monitoring:** journal-backed dashboard (already shipped). Alert on: drawdown >20%, 5-loss streak, Bybit `10006` rate-limit, fill latency P95 >2s, daily realized PnL < -2R.

### Phase 12 — Post-Phase-11-stable enhancements

Two candidates only. Both data-gated; commitment is conditional on observed need.

- **Deep RL (SB3/PPO) parameter tuner** — only triggered if Pass 3 Bayesian TPE plateaus AND high-dimensional knob interactions are measurable in Pass 3 data. 1-2 weeks of sim env work + GPU. Requires 100+ live trades from Phase 11. Probably never triggers; placeholder for the structural-ceiling case.

- **HTF Order Block + Breaker Block ecosystem** — operator-flagged as the primary scoring-quality candidate. Three sub-tasks:
  1. **OB detection audit.** Pine emits OB drawings; verify drawing logic against price-action concepts (operator can do this as a side task during Pass 2 — visual review of how the bot is marking OBs, no code change required).
  2. **OB drawing revision.** If audit finds mis-marking, fix Pine OB detection logic; possibly publish a focused single-purpose Pine script (OB-only or OB+Breaker) for visual confirmation.
  3. **Breaker Block addition.** Breaker = an OB that price has broken through and now retests from the opposite side. Currently absent from the project. Pine detection + scoring integration (~3-5 days). Trigger: factor-audit shows 15m OB signals correlate with WR positively (3m OBs failed pre-Bybit pivot; 15m may survive).

**Removed candidates (2026-04-29 cleanup):**
- *Arkham-dependent (4):* Whale directional classification, Arkham F4/F5 (per-entity divergence + DEX swap), Asymmetric Arkham penalties, Per-symbol Arkham overrides — all dropped because Arkham is being retired.
- *P/D + HTF ceiling SOFT re-add* — operator preference is to handle top/bottom awareness via different strategies rather than re-introducing this logic.
- *Partial TP re-enable* — operator confirmed RR 1.5 + MFE 1.0R BE-lock makes partial unnecessary (positions either hit 1.5R full or stop at locked BE).
- *ATR-trailing SL after MFE (Option B)* — RR 1.5 makes the 1.0R → 1.5R trail window too short to matter. Useful at RR 3+.
- *6+ Bybit perp* — cycle latency cost outweighs setup-density benefit at current 5-pair load.
- *1m TF activation (zone source / dynamic trail)* — already covered by `ltf_ribbon_alignment` + `ltf_mss_alignment` soft factors (2026-04-28); 3m stays the entry TF.
- *Pine overlay split* — no perf bottleneck observed; speculative refactor.
- *Multi-strategy ensemble* — out of scope; scalper-only.
- *Auto-retrain loop* — manual tune is reliable for current scale; revisit if Phase 11 + 6 months stable surfaces a need.
- *Alt-exchange support* — operator just migrated to Bybit; no demand.

### What is explicitly NOT on the roadmap

- **Decision-making RL.** Structural decisions (5-pillar, hard gates, zone-based entry, per-symbol flow) stay fixed. Bayesian / RL are parameter tuners only.
- **Claude Code as runtime decider.** Claude writes code and analyzes logs; it does not decide trades per candle.
- **Sub-minute entry TFs (1m / 30s).** TV freshness-poll latency makes these unreliable. Infrastructure rewrite (direct exchange WS + in-process indicators) would be a different project.
- **Leverage > 100x or non-cross margin modes.** Operator cap + Bybit cap combine to forbid. Requires risk memo to revisit.

---

## Safety warnings

**TradingView MCP:** unofficial, uses Electron debug interface, may break on TV updates → pin TV Desktop version. Data stays local.

**Bybit V5 API:** official `pybit` SDK. `demo=True` first; constructor refuses `demo=False` unless `allow_live=True` is passed explicitly. Never enable Withdrawal permission on the API key. IP whitelist strongly recommended (no expiry vs 90-day expiry). Sub-account for live. UTA hedge mode requires `mode=3` switch at startup (idempotent).

**Arkham:** read-only API, no trade-path exposure. `ARKHAM_API_KEY` stored in `.env` only. Retiring per Forward roadmap (soft 2026-05-04, off 2026-05-18). Auto-disable at 95% label usage is a safety net, not primary.

**Trading:** research project, not financial advice. Crypto futures = liquidation risk. Demo first, live with minimal capital.

**RL:** overfitting is the #1 risk — walk-forward is mandatory. Markets regime-shift. Log everything. Simple parameter tuning > complex deep RL. GBT + manual tuning first; RL only if a structural ceiling is evident.
