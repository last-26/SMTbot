# CLAUDE.md — Crypto Futures Trading Bot

AI-driven crypto-futures scalper on OKX. Zone-based limit entries, 5-pillar confluence, cross-asset + regime-aware vetoes. Demo-runnable end-to-end today; the near-term goal is to collect a clean dataset, then learn from it.

**Architectural principle:** Claude Code is the *orchestrator* (writes Pine, trains RL, debugs). Runtime decisions are made by the Python bot, **not** Claude. TradingView = eyes, OKX = hands, Python = brain.

---

## Current state (snapshot)

- **Strategy:** zone-based scalper. Confluence ≥ threshold → identify zone → post-only limit order at zone edge → wait N bars → fill | cancel.
- **Pairs:** 5 OKX perps — `BTC / ETH / SOL / DOGE / BNB`. 5 concurrent slots on cross margin (all active, no queue).
- **Entry TF:** 3m. HTF context 15m, LTF confirmation 1m.
- **Scoring:** 5 pillars (Market Structure, Liquidity, Money Flow, VWAP, Divergence) + hard gates (displacement, EMA momentum, VWAP, cross-asset opposition) + ADX regime-conditional weights. *Premium/discount gate and HTF TP/SR ceiling temporarily disabled 2026-04-19 — see changelog; P/D to be re-enabled as a soft/weighted factor (~10-15%) post-Phase-9, HTF ceiling re-evaluated after Phase 9 GBT.*
- **Execution:** post-only limit → regular limit → market-at-edge fallback. Single-leg OCO SL/TP at hard 1:3 RR (partial TP disabled 2026-04-19 late-night — see changelog; `move_sl_to_be_after_tp1` flag kept but inert while partial off). Dynamic TP revision re-anchors the runner OCO to `entry ± 3 × sl_distance` every cycle. **MFE-triggered SL lock (Option A, 2026-04-20)**: once MFE ≥ 2R, the runner OCO's SL is pulled to entry (+fee buffer) so the remaining 1R of target is risk-free. One-shot per position.
- **Sizing:** fee-aware ceil on per-contract total cost so total realized SL loss (price + fee reserve) ≥ target_risk across every symbol (2026-04-19 late-night-2 — see changelog). Previously floor-rounding produced $40-$54 variance on nominal $55; overshoot now bounded by one per-contract step (< $3 per position on current symbols).
- **Journal:** async SQLite, schema v2 (zone source, wait/fill latency, trend regime, funding Z-scores). `rejected_signals` table with counter-factual outcome pegging.
- **Tests:** 738, all green. Demo-runnable end-to-end.
- **Data cutoff (`rl.clean_since`):** `2026-04-19T19:55:00Z` (bumped after ceil sizing flipped — realized-R distribution shifts from clustered-below-target to clustered-at-or-above-target). Reporter and future RL see only post-pivot trades.

---

## Changelog

### 2026-04-21 — Arkham on-chain integration, Phase D (whale-transfer blackout hard gate + WS listener)

- **Trigger:** Phase C shipped (c7dbf5c). Phase D adds the event-driven hard gate `whale_transfer_blackout` and the WebSocket listener that feeds it. Gate sits in `build_trade_plan_with_reason` just after `cross_asset_opposition` and just before `crowded_skip` — semantically event-driven (like `macro_event_blackout`) but higher-priority because whale moves can lead the macro tape by minutes.
- **Design decisions (documented in chat before code):**
  - **WS listener follows the `LiquidationStream` pattern.** Background `asyncio.Task` owned by the runner, `start()` / `stop()` lifecycle, exponential reconnect backoff, message-handler pure function tested independently of the socket. No new dependency — the `websockets` library was already pulled in for Binance liquidations.
  - **Session token refreshed per reconnect, not per message.** Arkham's WS API uses a short-lived `sessionId` minted via REST. After a disconnect we re-mint rather than retry with the stale token; stale-token replay during long outages would just fail auth. `ArkhamClient.create_ws_session()` (Phase A) already handles the REST side.
  - **Parser is a module-level pure function.** `parse_transfer_message(raw, threshold)` returns `Optional[(token, usd_value, ts_ms)]`. Tests cover happy path / heartbeat / ack / malformed JSON / non-dict / missing token / under-threshold / alternate key names (`tokenId`, `usd_value`, `ts_ms`). The listener's `_handle()` method is a thin wrapper — everything testable without a live socket.
  - **Blast radius via `affected_symbols_for(token)`.** Stablecoin events (tether / usd-coin) shock every watched perp — massive USDT/USDC moves imply imminent CEX buy/sell pressure across every pair. Chain-native events (bitcoin / ethereum / solana / dogecoin / binancecoin) collapse to their single OKX perp. Unknown tokens return an empty tuple and silently no-op — a new Arkham-tracked asset appearing mid-run degrades rather than crashing.
  - **Blackout is extend-never-shorten.** `WhaleBlackoutState.set_blackout` uses `max(current, new)` so two overlapping events on the same symbol don't trim the second event's window down to the first event's shorter tail. Tested in Phase A.
  - **Gate is per-symbol, `is_active(symbol, now_ms)` called on every entry.** Chain-native BTC event blocks only BTC entries; SOL trades on the same tick still get evaluated normally. Stablecoin events fan out to every symbol so every pair sees the blackout. The check runs on every `build_trade_plan_with_reason` call so there's no stale-cache problem.
  - **Gate position in the chain: after `cross_asset_opposition`, before `crowded_skip`.** Semantically closest to `macro_event_blackout` (event-driven preemptive veto) — both are "the environment is unusually hostile, skip this tick". The plan spec placed it just above `macro_event_blackout`, but on closer inspection the natural cluster is cross-asset / whale / crowded (all three are "adverse conditions" judgments) followed by the structural gates (SL distance, HTF ceiling, TP width). Placing whale before crowded gives whale events priority over funding-based crowd skips, which matches the intuition that an in-progress whale dump is a stronger veto than a high funding Z-score.
  - **Gate swallows exceptions on the blackout check.** A corrupt `WhaleBlackoutState` (e.g., a test fake with a raising `is_active`) must not crash the entry pipeline — the gate logs nothing (try/except swallows) and the pipeline continues to downstream gates. Regular operation doesn't exercise this path; defensive against a future refactor.
  - **Listener `disabled` flag one-shot after N consecutive failures.** Three failed session-create or connect attempts in a row → `self._disabled = True` → listener exits cleanly. The blackout state stays empty (no writes); gate continues to evaluate against an empty state (always returns False). Rationale: a bot stuck on a dead WS spamming retries would burn rate budget on `create_ws_session` REST calls. Better to disable the signal than starve the rate budget.
- **Fix — Phase D deliverables (28 new tests, 875 → 903):**
  - `src/data/on_chain_ws.py` (new, 230 lines) — `ArkhamWebSocketListener` class with `start()` / `stop()` lifecycle. Reconnect loop mirrors `LiquidationStream._run` pattern: fresh session token per reconnect, exponential backoff `reconnect_min_s → reconnect_max_s`, 3-strike consecutive-failure disable. `_handle` routes one message to the blackout state via `parse_transfer_message` + `affected_symbols_for`. Two module-level pure functions (`build_subscribe_message`, `parse_transfer_message`) isolate serialisation / parsing so unit tests don't need a socket.
  - `src/strategy/entry_signals.py:build_trade_plan_with_reason` — 3 new kwargs: `whale_blackout_enabled: bool = False`, `whale_blackout: Optional[Any] = None`, `whale_blackout_symbol: Optional[str] = None`. Gate fires between `cross_asset_opposition` and `crowded_skip`. Defensive try/except around the `is_active` call.
  - `src/bot/runner.py:BotContext.arkham_ws` — new field. None when master off or sub-feature off.
  - `src/bot/runner.py:from_config` — when `on_chain.enabled AND whale_blackout_enabled`, instantiate `ArkhamWebSocketListener(ctx.arkham_client, ctx.whale_blackout_state, usd_gte=..., blackout_duration_s=...)`. Listener borrows the Phase A ArkhamClient for session management and writes to the Phase B WhaleBlackoutState — single source of truth for the state.
  - `src/bot/runner.py:_start_on_chain_ws` + updated `_stop_on_chain` — lifecycle plumbing. `start()` in the same block as derivatives + economic_calendar startup; `stop()` drained in the `finally` block alongside the other shutdown cascades.
  - `src/bot/runner.py` primary `build_trade_plan_with_reason` call site — threads `whale_blackout_enabled = cfg.on_chain.enabled AND cfg.on_chain.whale_blackout_enabled`, `whale_blackout = self.ctx.whale_blackout_state`, `whale_blackout_symbol = symbol`.
- **Expected behavior change:**
  - `on_chain.enabled=false` (default): bit-identical to pre-commit. All 903 tests pass, no arkham / websocket log entries on smoke.
  - `on_chain.enabled=true, whale_blackout_enabled=false`: listener never starts, `whale_blackout_state` stays empty, gate never fires.
  - `on_chain.enabled=true, whale_blackout_enabled=true, ARKHAM_API_KEY set`: listener starts on runner boot, connects to `wss://ws.arkm.com/intel/transfers`, subscribes with `{"op":"subscribe","sessionId":"…","filter":{"tokens":[…],"usdGte":100000000.0}}`. On every whale transfer ≥ 100M USD, relevant symbols get a 600s blackout. During the window, entry attempts on those symbols reject with `whale_transfer_blackout`. Open positions untouched (their OCO manages exit).
- **Safety rails:**
  * Listener never crashes the runner. `start()` wraps `ws.start()` in try/except; `stop()` swallows exceptions; reconnect loop has broad except; 3-strike disable prevents unbounded retry spam.
  * Gate never crashes the entry pipeline. Try/except around `is_active` check.
  * Config validators (Phase A) already enforce `whale_threshold_usd ≥ 10_000_000` (Arkham WS minimum) and `whale_blackout_duration_s > 0`.
  * `WhaleBlackoutState` is a per-process in-memory registry — state is lost on restart. Operator implication: immediately after restart, the gate can't veto a whale event that happened during the outage. Mitigation is operator awareness, not a code fix.
- **Tests:** 28 new, all green (903 passed total, up from 875):
  - `tests/test_on_chain_ws.py` (19 new) — `build_subscribe_message` shape + serialisation, `parse_transfer_message` 9 cases (happy / under-threshold / at-threshold / invalid-JSON / non-dict / heartbeat / missing-token / alternate-keys / fills-timestamp), `_handle` 7 cases (stablecoin fans out to all 5 symbols, bitcoin → BTC only, ethereum → ETH only, under-threshold no-op, unknown token no-op, malformed no-op, repeated events extend), `disabled` flag default false.
  - `tests/test_whale_blackout_gate.py` (9 new) — flag-off never fires, no-state never fires, no-symbol never fires, empty-blackouts never fires, expired-blackout never fires, active-blackout fires (with `generate_entry_intent` stub), different-symbol doesn't fire, stablecoin-style (all 5) fires for every symbol, corrupt-state doesn't crash.
- **Not fixed / explicitly out of scope (Phase D boundaries):**
  - Listener persistence across restart — blackout state is per-process in-memory. Reboot during a whale event = gap in coverage until the next qualifying event. Adding a SQLite table for historic blackouts is Phase 12 candidate.
  - No real-time observability (Prometheus / StatsD) for `arkham_whale_blackout_set` events — only stderr log. If operator wants to chart whale-event frequency, add it in a future observability pass.
  - Arkham's WS protocol is plan-spec-derived. If Arkham ships a different subscribe envelope, only `build_subscribe_message` needs adjustment — rest of listener is protocol-neutral.
  - Cross-chain token resolution — new Arkham-tracked tokens that aren't in `affected_symbols_for` are silently skipped. When the operator adds a 6th pair (e.g. XRP back), need to update `affected_symbols_for` at the same time.
- **Dataset:** `rl.clean_since` **unchanged**. `whale_transfer_blackout` reject rows already carry the snapshot + context dict from Phase B; downstream analysis in Phase 9 GBT will segment by the reject reason + `on_chain_context.whale_blackout_active`.
- **Re-evaluation (after ≥10 whale-blackout reject rows):**
  1. Frequency of `whale_transfer_blackout` rejects. If <1 per week, threshold too high (raise to $200M), or 100M whale transfers are rarer than expected. If >5 per hour during a bull run, threshold too low (drop to $250M).
  2. Counter-factual WR on whale_transfer_blackout rejects via `peg_rejected_outcomes.py`. If rejected setups would have won at baseline WR or better, the gate is being too cautious — loosen threshold or shorten duration. If they would have lost at a worse-than-baseline rate, the gate is capturing real edge.
  3. Listener WS uptime. Track `arkham_ws_disconnected` frequency. If reconnect rate > 5% of runtime, increase `reconnect_max_s` or investigate Arkham stability.

### 2026-04-21 — Arkham on-chain integration, Phase C (daily macro-bias confluence modifier)

- **Trigger:** Phase B shipped (c094753) — snapshots cached + journal enriched + zero decision impact. Phase C starts reading the snapshot: `calculate_confluence` applies a ±δ multiplier to long / short scores based on `on_chain.daily_macro_bias`, gated by `on_chain.daily_bias_enabled`. Master still defaults off; operator flips when ready for live impact.
- **Design decisions (documented in chat before code):**
  - **Multiplier at `calculate_confluence` top-level, not per-pillar.** Applying inside `score_direction` would have conflated the per-pillar weight interpretation (`htf_trend_alignment=0.5` etc.) with a macro-bias modifier that's not pillar-specific. Top-level multiply is a single scalar twiddle, introspectable in one line, trivially reversible. `ConfluenceScore.factors` list stays pure — shows the raw pillar breakdown; modifier is the wrapper around the aggregate.
  - **Multiplier applied BEFORE tie-break + threshold compare.** `below_confluence` rejections reflect the *adjusted* score. This is the point of the modifier — a setup below threshold on baseline can cross it under favorable bias, or vice versa. If we applied it after the threshold check, the modifier would only shuffle factor breakdowns without affecting decision outcomes.
  - **Stale snapshots skip silently.** `_daily_bias_multipliers` reads `on_chain.fresh` (property on OnChainSnapshot) and returns (1.0, 1.0) when False. So a 24-hour Arkham outage leaves the modifier inert without logging errors per cycle — the `fresh` flag is the contract for "is this snapshot load-bearing". Downstream consumers just need to know the result is (1.0, 1.0), not why.
  - **Delta range enforced at config time, not modifier time.** `OnChainConfig.daily_bias_modifier_delta` validator rejects values outside [0.0, 0.5] at YAML-load. At runtime the modifier just trusts the value. Removes a branch, keeps the hot path clean.
  - **Neutral bias always = no-op.** Even with delta = 0.5, neutral bias produces (1.0, 1.0). The Arkham rule classifies a day as bullish / bearish ONLY when stablecoin flow + BTC netflow both clear their thresholds in aligned directions — neutral is the default when one or both are ambiguous. Honoring neutral as no-op is the correct read: "we don't know, don't lean".
  - **Threaded through `build_trade_plan_with_reason` not just the diagnostic path.** Two call sites to `calculate_confluence` exist in `entry_signals.py`: one inside `generate_entry_intent` (the primary "is this tradable") and one after the reject path for diagnostic labeling. Both get the modifier so the primary decision uses the adjusted score, and the diagnostic reports the adjusted score consistently. Missing the primary call would have made the modifier purely cosmetic.
- **Fix — Phase C deliverables (15 new tests, 860 → 875):**
  - `src/analysis/multi_timeframe.py:calculate_confluence` — new `daily_bias_enabled: bool = False` + `daily_bias_delta: float = 0.0` kwargs. Modifier branch wraps bull / bear ConfluenceScore via `_daily_bias_multipliers` helper. Default (both flags false / delta zero) short-circuits at `mult_long == 1.0 and mult_short == 1.0` without constructing replacement ConfluenceScore objects — zero GC overhead when disabled.
  - `src/analysis/multi_timeframe.py:_daily_bias_multipliers` — new module-level helper. Reads `on_chain.fresh` via getattr with False fallback, `on_chain.daily_macro_bias` with "neutral" fallback. Pure function, no side effects — tested in isolation without needing a MarketState.
  - `src/strategy/entry_signals.py:generate_entry_intent` — added `daily_bias_enabled` + `daily_bias_delta` kwargs, threaded into the primary `calculate_confluence` call inside the function.
  - `src/strategy/entry_signals.py:build_trade_plan_with_reason` — added same kwargs, forwarded to `generate_entry_intent` AND to the diagnostic `calculate_confluence` call in the `intent is None` branch. Both call sites see the modifier; rejects reflect adjusted scores.
  - `src/bot/runner.py` — 3 `calculate_confluence` / `build_trade_plan_with_reason` call sites updated:
    1. `build_trade_plan_with_reason` at ~1500 (primary entry decision) — threads `daily_bias_enabled = cfg.on_chain.enabled AND cfg.on_chain.daily_bias_enabled` + `cfg.on_chain.daily_bias_modifier_delta`.
    2. `calculate_confluence` at ~1562 (post-reject diagnostic) — same threading.
    3. `calculate_confluence` at ~1665 (no_setup_zone diagnostic) — same threading.
  - Composite guard `cfg.on_chain.enabled AND cfg.on_chain.daily_bias_enabled` — this is the safety knot. Modifier only fires when master is on AND sub-feature flag is on. Flipping master without flipping sub-feature leaves Phase C inert. Flipping sub-feature without master leaves it inert too.
- **Expected behavior change:**
  - `on_chain.enabled=false` (default): bit-identical to pre-commit. All 875 tests green, `--dry-run --once` smoke is noise-free.
  - `on_chain.enabled=true, daily_bias_enabled=false`: Phase B / A behavior unchanged. Snapshots fetched + journal enriched, but confluence scoring identical to pre-C.
  - `on_chain.enabled=true, daily_bias_enabled=true, bullish snapshot, delta=0.10`: long confluence scores × 1.10, short × 0.90. A setup at baseline 2.85 clears `min_confluence_score=3.0` → trade taken (previously reject). Short setups at borderline get pushed below threshold → reject with `below_confluence`.
  - Symmetric mirror for bearish snapshot.
- **Safety rails:** `OnChainConfig._daily_bias_delta_sane` validator at config-load (already from Phase A) rejects delta < 0 or > 0.5. `_daily_bias_multipliers` is a pure function, can't mutate state. Modifier short-circuits on stale / absent snapshot — no unbounded behavior when Arkham is down. Failed `calculate_confluence` call still propagates to the `except Exception` in runner.py:1550 (pre-existing behavior preserved).
- **Tests:** 15 new, all green (875 passed total, up from 860):
  - `tests/test_daily_bias_modifier.py` (15 new):
    * `_daily_bias_multipliers` — 6 tests: delta=0 → (1.0, 1.0), absent snapshot → (1.0, 1.0), stale snapshot → (1.0, 1.0), bullish boosts long / dampens short, bearish mirrors, neutral → (1.0, 1.0).
    * `calculate_confluence` integration (with mocked `score_direction`) — 9 tests: flag off unchanged, flag on + no snapshot unchanged, bullish bias boosts long, bearish bias dampens long (verified exact 0.90 mult), strong enough bearish bias can FLIP winner from bull to bear, stale snapshot skipped, factors list preserved through modifier, borderline setup lifts to tradable (2.85 × 1.10 = 3.135 > 3.0), symmetric for bear setup.
- **Not fixed / explicitly out of scope (Phase C boundaries):**
  - No whale blackout gate (Phase D).
  - No stablecoin pulse penalty (Phase E).
  - Modifier is symmetric — both long AND short get adjusted. A variant that only boosts the aligned side and leaves the opposite untouched could reduce "over-penalizing" borderline reverse-bias trades, but that would bias the sizing distribution. Symmetric multiply is the principled choice until GBT shows otherwise.
  - No per-symbol override. If `BTC` should react to macro bias differently than `DOGE` (stablecoin day affects majors differently than memecoins), that's a Phase 12 candidate, not a Phase C deliverable.
- **Dataset:** `rl.clean_since` **unchanged**. Modifier is off-by-default; when operator flips it on post-deploy, Phase 9 GBT will segment by `on_chain_context.daily_macro_bias` categorical + `modifier_applied` boolean, not by a `clean_since` cut.
- **Re-evaluation (after ≥10 enabled-modifier closed trades):**
  1. `below_confluence` reject-to-open ratio segmented by `daily_macro_bias` — on bullish days we expect MORE longs and FEWER shorts to clear; on bearish days the mirror. Reject rates tilting the wrong way = modifier is anti-correlated with actual edge, reconsider delta.
  2. WR by `daily_macro_bias` + direction cross-tab. Longs on bullish days should show higher WR than longs on bearish days; shorts mirror. Equal WR across the cross-tab = the signal has no predictive value at the current delta, shrink or turn off.
  3. Borderline setups (confluence in [threshold, threshold × 1.10]) — how many got lifted to trades by the modifier, and what was their realized avg R vs baseline-clearing trades? If lifted-to-tradable setups underperform, delta is too aggressive.

### 2026-04-21 — Arkham on-chain integration, Phase B (snapshot pipeline + journal enrichment)

- **Trigger:** Phase A foundation shipped as b54a0ae. Phase B wires the snapshot-fetch scheduler + attaches the cached snapshot to MarketState + threads the on-chain context through all four journal write paths. Still zero decision impact — no gate, no modifier, no penalty reads the snapshot yet. Objective: by the end of this commit, flipping `on_chain.enabled=true` with `ARKHAM_API_KEY` in env produces journal rows with populated `on_chain_context` JSON while leaving every trading decision identical to pre-commit.
- **Design decisions (documented in chat before code):**
  - **Scheduler runs once per tick, shared across all symbols.** `_refresh_on_chain_snapshots` fires in `run_once` AFTER close / pending drain but BEFORE the per-symbol loop. Consequence: every symbol in a given tick sees the same `on_chain_snapshot` — a BTC open and a parallel SOL reject share identical `on_chain_context` JSON. Avoids per-symbol HTTP calls (which would either spike Arkham usage 5× or risk inter-symbol snapshot drift).
  - **Daily-on-UTC-day-rollover, pulse-on-monotonic-cadence.** Two independent clocks so neither blocks the other: the daily fetch keys off `datetime.now(tz=UTC).date() != last_on_chain_daily_date`, the pulse fetch keys off `time.monotonic() - last_on_chain_pulse_ts >= refresh_s`. This matches operator intent — "one daily read per wall-clock day, hourly pulse on 3600s cadence".
  - **Failure-isolation contract: keep last-known snapshot on fetch failure.** `fetch_daily_snapshot` / `fetch_hourly_stablecoin_pulse` return None on any HTTP error per the Phase A Coinalyze-mirror contract. The scheduler only OVERWRITES `on_chain_snapshot` when the fetch succeeds — a 24h Arkham outage leaves yesterday's snapshot cached. Downstream gates / modifiers see the `fresh` flag fall through to False once `snapshot_age_s > stale_threshold_s`, so stale data is harmless (modifiers / gates read None-equivalent). This is better than flipping to None on first failure: an outage mid-day would nuke the entire day's bias signal rather than degrading gracefully.
  - **Fetcher functions are module-level, not ArkhamClient methods.** Separation of concerns: `ArkhamClient` is transport-only (HTTP + rate limit + auto-disable), fetchers are derivation (entity IDs, pricing IDs, bias-classification rule). Lets unit tests mock the client with a canned dict response and exercise the full bias-rule logic (bullish / bearish / neutral / threshold edges) without touching httpx. Follows the separation already used in `src/data/economic_calendar.py` (transport = `FinnhubClient`, derivation = `EconomicCalendarService.is_in_blackout`).
  - **`whale_blackout_state` stays allocated even in Phase B.** An empty `WhaleBlackoutState()` instance lives on `BotContext` from startup. Phase D will add the WS listener that writes to it; meanwhile the Phase B `_on_chain_context_dict` helper reads `.blackouts` to compute `whale_blackout_active=False` (always, since no writes yet). Having the object alive across all phases means Phase D ships by adding the listener + gate without touching `_on_chain_context_dict` again.
  - **Journal context is a snapshot of the CURRENT tick, not the fill moment.** The zone-based entry path places a limit, then fills N bars later. The `on_chain_context` we persist at fill time is the snapshot at the fill tick, not the placement tick. Rationale: RL / GBT reads the context as "what did the bot KNOW when it committed to the position". A limit placed 10 bars ago still commits at fill time — the fresh snapshot is what the entry race is running against.
- **Fix — Phase B deliverables (26 new tests, 834 → 860):**
  - `src/data/on_chain.py` — added 2 fetcher functions + 3 module constants (DEFAULT_CEX_ENTITY_IDS, DEFAULT_STABLECOIN_PRICING_IDS, DEFAULT_DAILY_PRICING_IDS). `fetch_daily_snapshot` returns an `OnChainSnapshot` with `daily_macro_bias` computed from stablecoin balance Δ + BTC netflow per the plan's §1 rule; `fetch_hourly_stablecoin_pulse` returns a signed USD scalar (USDT + USDC summed). Both None-on-failure per the Phase A contract. `_extract_net_change_usd` private helper tolerates missing entities / missing pricing rows / non-dict entity values.
  - `src/bot/runner.py:BotContext` — added 6 new fields: `arkham_client`, `on_chain_snapshot`, `stablecoin_pulse_1h_usd`, `whale_blackout_state`, `last_on_chain_daily_date`, `last_on_chain_pulse_ts`. All default to None / 0.0 so tests constructing BotContext don't need updates.
  - `src/bot/runner.py:BotRunner.from_config` — instantiates `ArkhamClient(timeout_s, auto_disable_pct)` when `cfg.on_chain.enabled=true`. Client reads ARKHAM_API_KEY from env directly (mirrors Coinalyze). Always allocates a `WhaleBlackoutState()` even when master is off so the field is never None at runtime.
  - `src/bot/runner.py:BotRunner._refresh_on_chain_snapshots` — new method. Short-circuits on master-off / client-None / hard-disabled. Calls `fetch_daily_snapshot` when UTC day rolled over; preserves `stablecoin_pulse_1h_usd` across the daily refresh by constructing a new `OnChainSnapshot` with the prior pulse value patched in. Calls `fetch_hourly_stablecoin_pulse` when monotonic cadence elapsed; patches the returned pulse into the cached daily snapshot. Broad try/except around each fetch — Arkham outage can never crash a tick.
  - `src/bot/runner.py:BotRunner._on_chain_context_dict` — new method. Returns None when master is off or `on_chain_snapshot` is None. Populated dict carries `daily_macro_bias`, `stablecoin_pulse_1h_usd`, `cex_btc_netflow_24h_usd`, `cex_eth_netflow_24h_usd`, `coinbase_asia_skew_usd`, `bnb_self_flow_24h_usd`, `snapshot_age_s`, `fresh`, `whale_blackout_active` (aggregated from WhaleBlackoutState). Scalar-only so downstream tooling indexes by name without a schema contract.
  - `src/bot/runner.py:BotRunner._run_one_symbol` — attaches `state.on_chain = self.ctx.on_chain_snapshot` and `state.whale_blackout = self.ctx.whale_blackout_state` right after `read_market_state()`. In Phase B neither field is read by any downstream code, but Phase C / D / E will read them via `state.*` so the attachment happens once, not per-gate.
  - `src/bot/runner.py:BotRunner.run_once` — calls `_refresh_on_chain_snapshots` after close + pending drain, before the per-symbol loop. Symmetric placement with derivatives + economic-calendar refresh-in-loop pattern.
  - `src/bot/runner.py:BotRunner._stop_on_chain` — new shutdown method, mirrors `_stop_derivatives` / `_stop_economic_calendar`. Called in the `finally` block alongside the other shutdown cascades.
  - `src/bot/runner.py` journal call-sites (4 paths) — every `record_open` + `record_rejected_signal` call now threads `on_chain_context=self._on_chain_context_dict()`. Specifically: market-entry journal at ~1667, pre-entry reject at ~1042, pending-fill promotion at ~2085, pending-cancel reject at ~2140. Write-path reads the dict once and persists the JSON; downstream tooling sees a consistent shape.
- **Expected behavior change:** **none** on gate / modifier / sizing decisions. Observable changes:
  - When master off (default): bit-identical to pre-commit. `--dry-run --once` smoke test shows zero `arkham_*` log entries. All 860 tests pass.
  - When master on + API key in env + Arkham reachable: on first tick, two `arkham_*` INFO log entries fire (`arkham_daily_snapshot_refreshed bias=... btc_netflow=... eth_netflow=...` + `arkham_stablecoin_pulse_refreshed pulse_usd=...`). Subsequent ticks within the same UTC day + same pulse refresh window stay silent (cache hit). Every journal write from that point forward carries `on_chain_context` populated JSON.
  - When master on + API key in env + Arkham unreachable (429 / 5xx / network): `arkham_daily_snapshot_failed` WARN + `arkham_pulse_fetch_failed` WARN fire once; client's `_rate_pause_until` may kick in after a 429 so subsequent calls short-circuit. `on_chain_snapshot` stays None until the first successful fetch; journal writes `on_chain_context=NULL` meanwhile. Trading behaviour identical to master-off.
- **Safety rails:** every scheduler branch guarded by `try/except` around the fetcher call — if Arkham breaks in an unexpected way mid-trial, the tick continues. Snapshot-overwrite is single-threaded (runner's main loop), so no race between refresh and reader. `_on_chain_context_dict` constructs a fresh dict every call — no mutation of shared state by downstream journal callers.
- **Tests:** 26 new, all green (860 passed total, up from 834):
  - `tests/test_on_chain_fetchers.py` (11 new) — `fetch_daily_snapshot` bullish / bearish / neutral / threshold-below / HTTP-failure / missing-entities / stale-threshold propagation. `fetch_hourly_stablecoin_pulse` happy path summing USDT+USDC / HTTP failure / empty entities / 1h-interval in request params.
  - `tests/test_runner_on_chain.py` (15 new) — scheduler: master-off no-op, client-None no-op, hard-disabled no-op, daily fetched once per UTC day, daily refetches on day rollover, pulse respects refresh cadence (fires / skipped / rewound), daily failure keeps previous snapshot. Context helper: None when master off, None when snapshot absent, populated from snapshot, whale_blackout_active flag reflects state, preserves None optionals. Shutdown: closes client, no-op when absent, swallows close exception.
- **Not fixed / explicitly out of scope (Phase B boundaries):**
  - No confluence modifier (Phase C).
  - No whale blackout gate / WS listener (Phase D).
  - No stablecoin pulse penalty (Phase E).
  - `api_usage_auto_disable_pct` priming at startup via `get_subscription_usage` — deferred until the usage signal actually matters downstream (currently the headers absorbed on every request are enough).
  - No in-process metric (Prometheus / StatsD) for snapshot freshness. If operator wants to monitor burn rate without tailing logs, Phase E or a separate observability pass can add it.
- **Dataset:** `rl.clean_since` **unchanged**. Phase B writes structured data into a new nullable column; reads from the column in downstream analysis are explicitly Phase 9 GBT work, not part of the current flow.
- **Re-evaluation (after ≥10 enabled-master closed trades):**
  1. `on_chain_context IS NOT NULL` fraction on new rows → should be ≥90% when master on. Lower = fetcher failure path being hit frequently, needs operator investigation of rate budget / API reachability.
  2. `arkham_daily_snapshot_refreshed` cadence — should fire once per UTC day at the first run_once after midnight UTC. If firing more than once per day, the UTC-day-rollover logic has a bug (timezone handling).
  3. Per-tick runtime impact — `_refresh_on_chain_snapshots` should add ≤ 2s to the tick budget in steady state (cache hit) and ≤ 10s on daily fetch (single HTTP round-trip). If the hot path grows beyond this, either Arkham is slow or retry loop is firing too aggressively; consider shrinking `max_retries` or widening the daily cadence.

### 2026-04-21 — Arkham on-chain integration, Phase A (foundation, no behavior change)

- **Trigger:** operator granted 30-day Arkham Intel Platform trial access. Goal: layer on-chain flow signals (daily CEX balance changes, stablecoin pulse, whale-transfer blackouts) onto the existing 5-pillar + hard-gate scalper **without disturbing the clean data collection window**. Ground rules from the operator-written integration plan: (1) feature-flag-gated so a post-trial rollback is zero-kalıntı, (2) no `rl.clean_since` bump — the dataset segment by an `arkham_active` categorical feature in Phase 9 GBT rather than by timestamp cut, (3) ship in 5 atomic phases (A–E), each independently reversible, each passing full regression + smoke before commit.
- **Pre-integration correction — EMA timeframe plan item dropped:** the operator-supplied plan §10 called for a 15m → 3m EMA21/55 entry-driver switch with a `rl.clean_since` bump. Exploration of the runtime code confirmed the stack was **already** on the 3m entry TF across every consumer: `_pillar_bias_from` (runner.py:198) reads `candles` = `buf.last(100)` at runner.py:1308 where `tf_key = _timeframe_key(cfg.trading.entry_timeframe)`; `_ema_momentum_veto` (entry_signals.py:277) reads the same `candles` threaded through runner.py:1432; `_ema21_pullback_zone` (setup_planner.py:117) reads `ltf_candles` threaded from the same entry-TF buffer. The plan's "switch" was based on an outdated mental model — no code change existed to ship, therefore no commit and no `clean_since` bump. Reported to operator before Phase A.
- **Design decisions (documented in chat before code):**
  - **httpx.AsyncClient over sync requests + asyncio.to_thread.** The plan suggested `asyncio.to_thread(requests.get, ...)` for ArkhamClient. The codebase's `CoinalyzeClient` (src/data/derivatives_api.py:63) uses `httpx.AsyncClient` natively — mirroring that keeps the rate-limit bookkeeping (`_rate_pause_until`) consistent across data clients and avoids spawning threads per request. ArkhamClient follows the Coinalyze pattern verbatim: 401/403 → log + None (no retry loop), 429 → `Retry-After` sets the monotonic pause deadline and next call short-circuits, any other exception → `await asyncio.sleep(1.5 ** attempt)` up to `max_retries`, missing API key → warn at construction and every fetch returns None.
  - **Journal migration via idempotent ALTER TABLE, not schema v3→v4.** The plan assumed an explicit `SCHEMA_VERSION` constant and a `_migrate_v3_to_v4` function. The actual codebase has no version tracking — `src/journal/database.py` runs the `_MIGRATIONS` list on every connect, each `ALTER TABLE ... ADD COLUMN` wrapped in `try/except aiosqlite.OperationalError` so a re-run on an already-migrated DB is a no-op. Added two new idempotent entries (`trades.on_chain_context TEXT`, `rejected_signals.on_chain_context TEXT`); no version bump.
  - **API key stays in env only.** Mirrors COINALYZE_API_KEY and FINNHUB_API_KEY handling: the client reads `os.getenv("ARKHAM_API_KEY")` at construction, config (`cfg.on_chain`) never carries the secret. `load_config` intentionally does NOT thread `ARKHAM_API_KEY` into the raw YAML dict — OnChainConfig pydantic class would reject the extra key, and more importantly, the config object (which gets logged / serialised in debug paths) should be credential-free.
  - **Four flags, not one.** Master `on_chain.enabled` (kill switch) + per-phase `daily_bias_enabled` / `stablecoin_pulse_enabled` / `whale_blackout_enabled`. Phase B journal enrichment rides the master flag — no separate flag since a NULL `on_chain_context` column is indistinguishable from "feature disabled" downstream. Every flag defaults `false` in Phase A so this commit is strictly additive: `pytest` full suite + `--dry-run --once` both pass with zero Arkham log lines emitted, identical pre-Phase-A behaviour.
  - **Custom fakes, not `responses` / `aioresponses`.** The plan's test matrix assumed `responses` library. The codebase uses bare `unittest.mock` + purpose-built `_FakeResponse` / `_FakeClient` queue pattern (see `tests/test_derivatives_api.py`). Adding a new HTTP mock dependency for one module is a tax not worth paying; ArkhamClient tests copy the Coinalyze fake pattern.
- **Fix — Phase A deliverables (42 new tests, 792 → 834):**
  - `src/data/on_chain_types.py` (new, 110 lines) — `OnChainSnapshot` frozen dataclass with `daily_macro_bias` / `stablecoin_pulse_1h_usd` / `cex_*_netflow_24h_usd` / `snapshot_age_s` + `fresh` property; `WhaleEvent` frozen dataclass; `WhaleBlackoutState` mutable per-symbol registry with `set_blackout` (extend-never-shorten) + `is_active(symbol, now_ms)`; `affected_symbols_for(token_id)` mapper — stablecoin → all 5 symbols, chain-native → single symbol, unknown → empty tuple.
  - `src/data/on_chain.py` (new, 230 lines) — `ArkhamClient` with httpx.AsyncClient, token-less rate limiting (Arkham is label-budget not per-minute), `X-Intel-Datapoints-Usage/Limit/Remaining` header parsing into `_last_usage_snapshot`, auto-disable at `api_usage_auto_disable_pct` (default 95%). Public methods: `get_entity_balance_changes(entity_ids, pricing_ids, interval)`, `create_ws_session()`, `delete_ws_session(sid)`, `get_subscription_usage()`, `close()`. None-on-any-failure contract.
  - `src/data/models.py:205` — `MarketState.on_chain: Optional[Any] = None` + `whale_blackout: Optional[Any] = None` alongside existing `derivatives` / `liquidity_heatmap`. Typed as `Any` (not the dataclass directly) to keep the pydantic model import-cycle-free.
  - `src/bot/config.py:534` — new `OnChainConfig` pydantic class with 11 fields + 4 field_validators (delta ∈ [0, 0.5], whale threshold ≥ 10M enforcing Arkham WS minimum, durations > 0, auto-disable pct ∈ (0, 100]). Wired into `BotConfig` at line 581 alongside the other sub-configs.
  - `src/bot/config.py` `load_config` — **no** env injection for ARKHAM_API_KEY (contrast with FINNHUB_API_KEY which DOES go through YAML because `economic_calendar.finnhub_api_key` is a typed field). The comment block documents the decision.
  - `src/journal/database.py` — `_SCHEMA` CREATE TABLE additions for `on_chain_context TEXT` on both tables, `_COLUMNS` + `_REJECTED_COLUMNS` lists extended, `_record_to_row` + `_rejected_to_row` JSON-serialise the dict, `_row_to_record` + `_row_to_rejected` via new helper `_parse_on_chain_context` that tolerates missing column (pre-migration rows), NULL, and invalid JSON. `_MIGRATIONS` list gets two new idempotent ALTER TABLE entries. `record_open` and `record_rejected_signal` gain `on_chain_context: Optional[dict] = None` kwarg.
  - `src/journal/models.py` — `TradeRecord.on_chain_context` and `RejectedSignal.on_chain_context` as `Optional[dict]` with the structured-write-back JSON payload documented inline.
  - `config/default.yaml` — new `on_chain:` section after `economic_calendar:`, master flag `enabled: false`, per-phase flags all `false`, all thresholds / durations documented with operator-visible comments.
  - `.env.example` — new `ARKHAM_API_KEY=` entry with a comment pointing to the rollback procedure.
- **Expected behavior change:** **none**. The full test suite is green (834 passed, 46 warnings — the pre-existing pydantic `trading.symbol` deprecation), `--dry-run --once` shows no `arkham_*` log lines whatsoever, and live bot behaviour is bit-identical to pre-Phase-A. `MarketState.on_chain` stays `None` on every cycle because the runner does not yet construct an `ArkhamClient`. Journal rows written in this regime carry `on_chain_context=NULL` (pre-Phase-B rows already do, by definition).
- **Safety rails:** pydantic validators reject ill-formed on_chain config at YAML-load time (stops the bot at startup with a clear ValidationError, never at runtime). Migration entries are idempotent via try/except `aiosqlite.OperationalError` — re-running on a post-migration DB is a no-op. ArkhamClient stays fully isolated: if Phase B / C / D / E were run before B explicitly instantiated the client, they'd all see `state.on_chain = None` and degrade to pre-Arkham behaviour (the gates / modifiers all check for None or `fresh = False` before applying).
- **Tests:** 42 new, all green (834 passed total, up from 792):
  - `tests/test_on_chain_types.py` (16 new) — `OnChainSnapshot` defaults + fresh-flag + threshold math, `affected_symbols_for` dispatch for stablecoins / chain-natives / aliases / unknown, `WhaleBlackoutState` empty / active-inside / extend-never-shorten / per-symbol isolation, `WhaleEvent` frozen-dataclass contract.
  - `tests/test_on_chain_client.py` (16 new) — `_FakeResponse` + `_FakeClient` queue pattern; happy path with header absorb, 429 populates `_rate_pause_until` and short-circuits the next call, 401 / 403 return None without retry (one call only), 5xx retries `max_retries` times then returns None, generic exception swallowed + retried, auto-disable fires at usage ≥ threshold and blocks subsequent calls even with queued successes, auto-disable does not fire below threshold, malformed usage headers handled gracefully, `create_ws_session` happy / no-key-in-body / http-error, `delete_ws_session` 2xx → True / failure → False / no-api-key short-circuit, `close` idempotent.
  - `tests/test_bot_config.py` (9 new) — defaults all off, YAML load of the on_chain block, delta out-of-range rejected, whale threshold below Arkham's 10M minimum rejected, whale threshold at minimum accepted, durations must be positive, auto_disable_pct must be in (0, 100], negative thresholds rejected, section absent still produces default.
  - `tests/test_journal_database.py` (4 new) — `record_open` persists the dict as JSON and round-trips through `get_trade`, `record_open` default `None` round-trips, `record_rejected_signal` persists via `list_rejected_signals`, `record_rejected_signal` default `None` round-trips.
- **Not fixed / explicitly out of scope (Phase A boundaries):**
  - No snapshot fetchers (Phase B).
  - No runner scheduler / `_refresh_on_chain_snapshots` (Phase B).
  - No confluence modifier (Phase C).
  - No WebSocket listener / whale gate (Phase D).
  - No pulse penalty (Phase E).
  - `subscription/intel-usage` priming at startup — the client exposes `get_subscription_usage` but the runner does not call it yet. Phase B wires the startup priming.
- **Dataset:** `rl.clean_since` **unchanged** per operator decision. Post-deploy trades will be segmented by the `arkham_active` categorical feature (derived from `on_chain.enabled` at open time + `on_chain_context IS NOT NULL`) in Phase 9 GBT analysis, not by timestamp cut. This is the single most important divergence from the bot's usual flip-protocol and is deliberately called out here so future maintainers don't reflexively bump `clean_since` when master flips to `true`.
- **Rollback (trial-expired):** flip `on_chain.enabled: false` in config, restart. Verification checklist: no `arkham_*` log lines on startup or per-cycle, `state.on_chain` is None on every cycle, every new `record_open` / `record_rejected_signal` writes `on_chain_context=NULL`, historical pre-rollback rows keep their populated `on_chain_context` values (zero data loss). Schema columns remain in place — extreme rollback (column drop) is documented in the integration plan §7 but practically unnecessary: a NULL column costs no storage and keeps the bot symmetric with historical journals.
- **Re-evaluation (after Phase B ships and ≥10 post-enable closed trades):**
  1. `ArkhamClient._last_usage_snapshot` burn rate per 24h — if >5% of daily label budget is being consumed by startup priming + journal enrichment alone, adjust `on_chain.api_usage_auto_disable_pct` or shorten snapshot refresh cadence.
  2. `on_chain_context` column completeness on new rows — if any row carries `NULL` while master is `true`, investigate the fetcher failure path (429 rate pause, 5xx outage, auto-disable trigger).

### 2026-04-21 — Pending zone timeout 10 → 7 bars

- **Trigger:** operator quote: *"emir girildikten sonra 10 tane 3 dakikalık mum bekleniyor şu anda. bu da 30 dakika yapıyor. ben bunu 7 mum ve 21 dakikaya çekmek istiyorum."* Rationale: on the 3m entry TF, 30 dakikalık bir fill penceresi scalp-native zone kaynaklarının (`vwap_retest`, `ema21_pullback`, `fvg_entry`) yarı-ömründen uzun; zone'un "stale" olması durumunda confluence yeniden hesaplanıp daha yeni bir setup sunulabilmeli. 21 dakika (7 bar) aynı ritmi koruyor ama eskimiş bir zone'a yapışıp kalma süresini ~%30 kısaltıyor.
- **Fix — tek satırlık default kırpma:**
  - `config/default.yaml:391` — `execution.zone_max_wait_bars: 10 → 7`.
  - `src/bot/config.py:340` — `ExecutionConfig.zone_max_wait_bars: int = 10 → 7` (pydantic default; YAML override ile aynı değer).
  - `src/strategy/setup_planner.py:351` — `build_zone_setup(max_wait_bars: int = 10 → 7)` (yalnız planner-native default; runner zaten `cfg.execution.zone_max_wait_bars`'ı geçiyor — bu değişiklik doğrudan caller'lar olmadan çağrılan yerler için güvenli fallback).
  - `algoritma.md` §5, §10, §12 — tablo değerleri ve lifecycle diyagramı 10 → 7 (21 dk) açıklamasıyla birlikte güncellendi.
- **Expected behavior change:** kullanılmayan zone limit emirleri 30 dk yerine 21 dk sonra cancel olur. 3m TF'de bu, zone confluence hesaplarının 3 bar daha erken yenilenmesi demek. Fill rate beklentisi: hızlı retrace'ler (zone'un 2-3 bar içinde touch olduğu senaryolar) zaten 7 bar'ın altında kalıyor — bu grup etkilenmez. 7-10 bar aralığında fill olacak "yavaş retrace" grubu cancel'a gidecek, ardından bir sonraki cycle'da güncel confluence + güncel zone'la yeniden değerlendirilecek. Net etki: ya daha taze bir setup yakalanır (pozitif), ya cancel kalır (nötr, R harcanmadı).
- **Safety rails:** yok; zaman parametresi, kontrat/geometri yok. Cancel path idempotent (`_reconcile_orphans` + `poll_pending` mevcut). Restart-safe — `_pending` bellekte tutuluyor, restart'ta 7-bar saat sıfırlanır ama bu pre-deploy davranışından farklı değil.
- **Tests:** yeni test eklenmedi. Mevcut `test_runner_zone_entry.py` ve `test_setup_planner.py` caller'ları `max_wait_bars` parametresini açıkça 10 olarak geçiyor — default değişikliği bu testlerin davranışını etkilemez, mevcut 730+ test geçmeye devam eder. Default'un yansıması için `config/default.yaml`'ı doğrudan okuyan bir test de yok (pydantic validator'lar pozitiflik kontrolü yapmıyor — int>0 varsayımı implicit).
- **Dataset:** `rl.clean_since` **değişmedi** (`2026-04-19T19:55:00Z`). Sadece pending-fill penceresi kısalıyor; entry geometrisi, scoring, sizing, SL/TP mesafesi, MFE lock, TP revise — hiçbirine dokunulmadı. Fill rate dağılımı hafif kayabilir ama regime-shift seviyesinde değil.
- **Re-evaluation:** ≥30 post-deploy kapalı trade sonrası journal'da `zone_timeout_cancel` reject oranı kontrol edilecek. Eğer cancel oranı %30'un üstünde seyrederse 7 bar fazla agresif demek — 8'e çıkarma veya zone kaynağına göre parametre yapma (1m sources için 3-4 bar, 3m için 7, HTF için daha uzun) gündeme gelebilir. Cancel oranı %10'un altında kalırsa 7 bar yeterince rahat ve daha da kırpılabilir (5-6).

### 2026-04-21 — VWAP-band zone anchor (Convention X)

- **Trigger:** operator flagged that the `_vwap_zone` limit was landing at the 0.5σ midpoint between VWAP and the ±1σ band (arbitrary 50/50 split). Wanted a Fib-lite anchor that pulls the limit closer to VWAP on the directional side — catches the pullback before it fully retraces, with VWAP acting as a natural structural pivot (SL already sits past VWAP per the zone contract). Operator quote: *"üst bant 1 eq 0.5 se ve fiyat yukarı düşünülüyorsa ve fiyat yukarıdaysa 0.7 den işlem atılacak 0.65 de olabilir vwape biraz daha yakın olması açısından. alt bant 0, eq 0.5 ken short girilecekse de yine 0.3 den girilmesi lazım."*
- **Design decisions (documented in chat before code):**
  - **Convention X** (absolute position on the [lower_band, upper_band] axis, 0.5 = VWAP) picked over Convention Y (single VWAP-relative scalar) — operator-sezgisel, matches how a human reads the band on a chart. Two knobs per direction, no ambiguity on "which side of VWAP".
  - **VWAP is NOT a pozisyon-içi invalidation monitor** — only an entry anchor. Explicit rejection of the active-monitor variant (no new cycle gate, no defensive-close path). The existing OCO SL sits past VWAP per the structural zone contract, so a cross of VWAP against the trade still hits the SL naturally.
  - **Zone width unchanged** — only the zone midpoint shifts. Mevcut ATR-tabanlı tolerans aralığı (`zone_buffer_atr × ATR`) same as before; the `(low, high)` zone still spans VWAP to the band. The anchor formula interpolates inside that existing span.
  - **Rejim-bağımlılığı deferred** — every ADX regime (`RANGING` / `WEAK_TREND` / `STRONG_TREND` / `UNKNOWN`) uses the same anchor today. Operator's range-EQ observation (true mean-reversion entries at the outer bands with TP at the opposite band) logged as a Phase 12 candidate — needs a separate zone source and does not belong in a single-scalar shift.
- **Fix — anchor-driven limit inside the VWAP band** (`src/strategy/setup_planner.py`, `src/bot/config.py`, `src/bot/runner.py`):
  - `zone_limit_price` signature extended with `vwap_long_anchor: float = 0.75` and `vwap_short_anchor: float = 0.25` (defaults preserve the pre-pivot midpoint behaviour; any legacy caller without the new args gets the old 0.5σ mid). For `vwap_retest`: `long_limit = low + (2·long_anchor − 1)·(high − low)`; `short_limit = low + 2·short_anchor·(high − low)`. `liq_pool_near` still uses plain midpoint (the cluster IS the target — no anchor interpretation there).
  - `apply_zone_to_plan` forwards both anchors (same defaults) into `zone_limit_price`.
  - `AnalysisConfig.vwap_zone_long_anchor: float = 0.7` + `vwap_zone_short_anchor: float = 0.3`, pydantic-validated `long ∈ [0.5, 1.0]` (strictly on the upper half) and `short ∈ [0.0, 0.5]` (strictly on the lower half). Out-of-half values would place the entry on the wrong structural side of VWAP and are rejected at YAML-load time.
  - `config/default.yaml` under `analysis:` — inline operator-note explaining 0.5 / 1.0 axis semantics + EQ-hug nudge direction (0.65 / 0.35 for tighter VWAP hug, 0.8 / 0.2 for chasing the outer band).
  - `BotRunner._apply_zone_to_plan` call (runner.py:1809) threads `cfg.analysis.vwap_zone_long_anchor` + `vwap_zone_short_anchor` into the planner. Production path uses the operator-set 0.7 / 0.3; tests / legacy fixtures keep the 0.75 / 0.25 defaults via unchanged call sites.
- **Expected behavior change:**
  - Before: long limit landed at `VWAP + 0.5σ` (halfway to upper band); short at `VWAP − 0.5σ`.
  - After: long limit lands at `VWAP + 0.4σ` (40% of the way to upper band); short at `VWAP − 0.4σ`.
  - In user's range-example (VWAP=100, bands=50/150): old long=125 / old short=75 → new long=120 / new short=80.
  - Fill rate: marginally deeper pullback required (~10% of band width), so fill rate dips slightly vs the 0.5σ mid. Precision: entry sits inside the Fib-lite retracement zone, so when fill does land, SL distance from VWAP is tighter on average — R per unit notional slightly improves.
  - Relative to a hypothetical VWAP-exact limit (operator's mental baseline), 0.7 / 0.3 is meaningfully earlier — the 0.4σ offset from VWAP captures the pullback before it fully retraces, as the operator explicitly wanted.
- **Safety rails:**
  - `long_anchor ∈ [0.5, 1.0]` and `short_anchor ∈ [0.0, 0.5]` — enforces entry stays on the correct structural side of VWAP. An operator typo like `long_anchor=0.3` would otherwise compute `(2·0.3 − 1)·width = −0.4·width` → a long limit BELOW VWAP (below the zone.low), breaking the SL-past-zone contract. Rejected at config load.
  - Default values preserve backwards compat — the 14 existing `zone_limit_price` / `apply_zone_to_plan` callers across tests pass no anchor args and still get the old 0.5σ midpoint. No test updates needed beyond the 6 new ones that explicitly pass 0.7 / 0.3.
- **Tests:** 12 new, all green (792 passed, up from 780):
  - `tests/test_setup_planner.py` — 6 new: long anchor 0.7 → VWAP+0.4σ happy path, short 0.3 → VWAP−0.4σ happy path, anchor=0.5 collapses to VWAP, anchor extremes (1.0 / 0.0) hit outer bands, default anchors preserve midpoint contract, ATR fallback path applies same formula.
  - `tests/test_bot_config.py` — 6 new: default 0.7 / 0.3, operator EQ-hug (0.65 / 0.35) accepted, long < 0.5 rejected, long > 1.0 rejected, short > 0.5 rejected, short < 0.0 rejected.
- **Not fixed / explicitly out of scope:**
  - **ADX-conditional anchor** — not implemented. `RANGING` rejim could benefit from a different entry geometry (mean-reversion at the outer bands, TP at the opposite band) but that's a new zone source, not a parameter tweak. Operator explicitly agreed to rafaya kaldır.
  - **Asymmetric band handling** — the implementation assumes Pine's symmetric `vwap ± stdev_mult·σ` bands. If a future Pine change produces asymmetric bands (e.g. separate upper/lower multipliers), the Convention X axis semantics still hold but the formula may need revisiting. Not a concern today.
  - **Active VWAP invalidation monitor** — explicitly rejected in design discussion. If a future data cut shows the existing OCO SL isn't catching VWAP-cross reversals fast enough, revisit as a `ltf_reversal_close`-style defensive gate; not part of this change.
- **Dataset:** `rl.clean_since` **unchanged** (`2026-04-19T19:55:00Z`). Entry-geometry shift is small (0.5σ → 0.4σ = 10% of band width) and strictly additive to the existing `vwap_retest` zone logic. Realized-R distribution should not regime-shift. Memory'deki "clean_since pin" kuralına sadık kalıyoruz. Post-deploy ilk 10 trade sonrası factor-audit'te anomali görürsek tekrar değerlendiririz.
- **Re-evaluation:** after ≥30 post-deploy closed trades, check:
  1. `vwap_retest` fraksiyonunun post-deploy / pre-deploy oranı (bu zone source'un seçilme frekansı). Anchor shift, seçim kriterini değiştirmez ama entry mesafesini değiştirdiği için fill rate düşerse source-priority tablosunda pozisyonu değişebilir.
  2. `vwap_retest` kaynaklı trade'lerin avg-R'si vs diğer zone source'lar. 0.7/0.3 Fib iddiasının veri ile doğrulanması — 0.5σ midpoint'ten daha iyi R/$ üretmeli.
  3. Anchor değerini EQ-hug'a (0.65/0.35) çekmek daha iyi mi, outer-band'a (0.8/0.2) çekmek daha iyi mi — GBT phase'de parametrik olarak öğrenilecek.

### 2026-04-20 — Resting TP limit alongside OCO (maker-TP, wick capture)

- **Trigger:** BTC trade `414b4ca5` observed wicking to TP price then reversing before the OCO's market-on-trigger could fire — SL hit at BE on the pullback, realized ≈ 0R on what was structurally a +3R winner. Operator quote: *"tp seviyesindeki emire fitil gelmiş … limit order şeklinde direkt piyasaya versek olmaz mı. yüksek ihtimal tp sl kısmından girildiği için trigger tetiklenene kadar geri düşmüş olabilir fiyat."* Root cause: OCO's TP leg uses `tpOrdPx="-1"` (market-on-trigger). The trigger fires at mark==tpTriggerPx, then a market order is submitted — by the time that market fill lands, price has already reversed off the wick. Three options weighed (A: trigger-limit-at-tp, B: resting reduce-only limit at tp alongside OCO, C: last-price trigger). Operator picked B: *"resting limit nedir. Normal limit order koysak işte poz miktarı kadar tamam olması lazım."*
- **Fix — additive maker-TP layer** (co-placed, not replacing, the OCO):
  - `src/execution/okx_client.py:place_reduce_only_limit` — new method placing a post-only reduce-only limit at the TP price. Closing side = sell-for-long / buy-for-short. `clOrdId` prefix `smttp` distinguishes TP limits from entry limits (`smtbot`) for future orphan-sweep filtering. `post_only=True` by default rejects with sCode 51124 if the book has already reached TP — safe, the OCO's market fallback catches that case.
  - `src/bot/config.py:ExecutionConfig.tp_resting_limit_enabled=True` (new flag, default on). Off disables both `_handle_pending_filled`'s place and `_rehydrate_open_positions`' re-place; OCO remains primary.
  - `src/execution/position_monitor.py` — `_Tracked.tp_limit_order_id` tracks the live TP-limit ordId in memory. `register_open` accepts the kwarg; `poll()` close path cancels best-effort via `_cancel_tp_limit_best_effort` (tolerates idempotent-gone `_ALGO_GONE_CODES` + generic exceptions — reduce-only means worst case it sits inert until next restart sweep). `revise_runner_tp` cancels old TP limit + places new one at the revised TP in lockstep with the OCO replace; TP-limit failure does NOT unwind the successful OCO revise (OCO market-TP still protects). `lock_sl_at` deliberately leaves the TP limit untouched — SL-only change doesn't shift TP price.
  - `src/bot/runner.py:_handle_pending_filled` — places the TP limit between `attach_algos` and `register_open` so the order id is threaded through to the monitor. Best-effort: a place failure logs `tp_limit_place_failed` but does NOT unroll OCO attach (would leave the position in limbo).
  - `src/bot/runner.py:_rehydrate_open_positions` — re-places a fresh TP limit for every non-BE journal OPEN row on restart. Required because the orphan-pending-limit sweep (below) wipes every resting limit at startup; without rehydrate re-place, post-restart positions lose maker-TP coverage until next revise cycle.
  - **Reconcile-before-rehydrate swap** (`src/bot/runner.py:_prime`): the orphan-pending-limit sweep now runs FIRST, then rehydrate. Rationale: if rehydrate ran first, the fresh TP limits it placed would immediately get nuked by the orphan sweep. Trade-off: `_reconcile_orphans`' position-mismatch check had been reading `ctx.open_trade_ids` (empty before rehydrate) — fixed by reading journal OPEN rows directly via `journal.list_open_trades()` inside reconcile. Both mismatch check and `_cancel_surplus_ocos` now use the same journal-derived `journal_keys` set.
- **Expected behavior change:** on every bot-opened position, two TP orders are live on OKX:
  1. The OCO (sl_trigger_px=SL, tp_trigger_px=TP, ordPx=-1 market-on-trigger) — primary SL protection, fallback TP if the wick is so fast the limit can't fill.
  2. A resting post-only reduce-only limit at TP — fills as maker the instant the book touches TP price. No trigger latency, no slippage, pays maker rebate instead of taker fee.
  - When TP limit fills: OCO's TP leg never triggers (position is already flat), OCO auto-cancels, `poll()` close path detects flat position and cancels the (already-gone, idempotent 51400) TP limit log-only.
  - When wick-and-reverse happens (the bug from trade 414b4ca5): resting limit captures the wick as maker, position closes at exact TP, OCO's market-trigger never fires. Fees are lower (maker rebate vs taker), and more importantly the "almost-win" bucket disappears.
  - When revise_runner_tp changes TP: both the OCO and the TP limit are cancel+replaced at the new TP. Failure modes independent — revise succeeds on OCO even if TP-limit re-place fails.
  - When lock_sl_at fires (MFE-lock): only OCO's SL moves; TP limit untouched.
- **Not fixed / out-of-scope:**
  - Entry-side fills still use the existing limit → regular-limit → market-at-edge fallback. This change is TP-only.
  - Partial TP remains disabled (`partial_tp_enabled=false`); the resting limit covers the full `runner_size`. If partial is re-enabled later, the TP1 algo (market-on-trigger at partial TP) is separate from the runner's TP limit — future work to decide whether to add a second resting limit for partial TP too.
  - `client_order_id` prefix filter for orphan-pending-limit sweep: currently `_cancel_orphan_pending_limits` cancels ALL resting limits regardless of prefix. This is actually desirable at restart because `_pending` (entry limits) and `tp_limit_order_id` (TP limits) are both lost on restart, and rehydrate re-places the TP limit fresh. But if a future feature wants to preserve some resting orders across restart, it'd need to filter by `clOrdId` prefix (`smttp` vs `smtbot`).
  - Journal write-back for trade `414b4ca5` itself: pending operator decision — either leave as-is (realistic record of what OKX reported) or manually rewrite the exit fields to BE stop price. This changelog entry is about *prevention*; the historical fix-up is a one-shot separate from the code work.
- **Dataset:** `rl.clean_since` unchanged (`2026-04-19T19:55:00Z`). This change is an exit-execution tightening (maker-TP vs market-on-trigger), additive to the existing OCO geometry. The 3R hard cap + zone-driven TP + MFE-lock are identical. Post-deploy avg-R may drift slightly upward from fewer missed TPs; factor-audit picks it up cleanly since no entry-path or scoring parameter changed.
- **Re-evaluation:** after ≥30 post-deploy closed trades, check the journal for:
  1. Fraction of wins where the TP limit filled vs where the OCO market-trigger filled. High TP-limit fill rate (>70%) validates the fix. Low rate means either the book is thin at TP (post_only rejects → market fallback) or price generally walks rather than wicks into TP.
  2. Realized exit price vs planned TP. Should be ≤ planned TP on wins (no slippage), exactly at TP when the limit fills as maker, better-than-TP on rare gap opens.
  3. `tp_limit_place_failed` frequency. High (>5% of positions) indicates either too-tight `ordPx` rounding or a systematic book-reaching-tp-immediately issue (e.g., during breakout momentum). May need to fall back to plain limit (non-post-only) as a secondary retry.
- **Tests:** 17 new, all green (780 passed total, up from 763):
  - `tests/test_okx_client.py` — `place_reduce_only_limit` long/short parity, post_only vs limit ordType, custom client_order_id, envelope sCode 51124 raises OrderRejected.
  - `tests/test_position_monitor.py` — poll cancels TP limit on close, poll skips cancel when none registered, poll tolerates idempotent 51400 cancel, poll tolerates generic cancel exception, revise cancels+replaces TP limit, revise skips when no TP limit registered, revise swallows TP-limit place failure, lock_sl_at leaves TP limit untouched.
  - `tests/test_bot_runner.py` — rehydrate places fresh TP limit, rehydrate skips when BE already moved, rehydrate skips when feature disabled, rehydrate tolerates place failure.
- **Smoke (`--dry-run --once`):** confirmed live TP-limit placement on a pre-existing BNB short: `tp_limit_placed` → revise fires → `tp_limit_canceled` + `tp_limit_replaced` (lockstep with OCO), restart → `orphan_pending_limit_canceled` (old TP limit wiped) → `tp_limit_replaced_on_rehydrate` (fresh one placed). Reconcile/rehydrate swap working as designed.

### 2026-04-20 — Stale-algoId + orphan pending-limit reconciliation (DOGE 2-OCO postmortem)

- **Trigger:** operator spotted two DOGE OCOs for the same live long position — `3495282693644861440` (sl=0.09379, MFE-locked, active) *and* `3494715650050920448` (sl=0.09296, older). Both with sz=64 sharing the same TP (0.0959). After a manual restart, also two pre-restart resting limits stuck on OKX (ETH@2280.04 sz=29, BNB@620.3 sz=1182). Operator quote: *"dogede 2 tane sl var gibi yine güncel pozisyon ve emirleri kontrol eder misin. (botu yeniden başlattığıma rağmen, başlatmadan önce emirleri silmiştim.)"*
- **Root cause — stale algoId in journal** (`src/execution/position_monitor.py:revise_runner_tp`):
  - DOGE timeline reconstructed from log:
    1. 02:15 long fill → initial OCO `3494658137318264832` placed + journaled (`algo_ids=["3494658137318264832"]`).
    2. 05:44 dynamic-TP revise → cancels initial, places `3494715650050920448`. **In-memory `_tracked.algo_ids` updated; journal NOT touched.** Journal still shows `["3494658137318264832"]`.
    3. 07:07 bot restart → `_rehydrate_open_positions` reads journal → `_tracked.algo_ids = ["3494658137318264832"]`. The actually-live `3494715650050920448` is now untracked orphan.
    4. 07:09 next revise fires → attempts to cancel `algo_ids[-1] = 3494658137318264832`. OKX returns 51400 (already_gone — truly gone since 05:44). `_verify_algo_gone` confirms. Proceeds, places `3494887702514929664`. In-memory `algo_ids` now `["3494887702514929664"]`; journal still stale.
    5. 10:25 MFE-lock fires → cancels `3494887702514929664`, places `3495282693644861440`. MFE-lock's success path *does* call `_on_sl_moved` → journal updates to `["3495282693644861440"]`. But `3494715650050920448` is still orphan on OKX.
  - Code-level bug: `revise_runner_tp` updates `_tracked.algo_ids` on line 437 but never calls `self._on_sl_moved(...)` like `lock_sl_at` does on line 570-574. Callback writes `journal.update_algo_ids` — without it, every restart rewinds the journal's view of `algo_ids` to the initial attach.
- **Root cause — no startup reconciliation for pending limits** (`src/bot/runner.py:_reconcile_orphans`):
  - `_rehydrate_open_positions` restores the monitor's tracked **positions** from journal OPEN rows. It does not touch `_pending` (the in-memory dict of resting limit orders). `_pending` is lost on restart by design — limit orders are short-lived and the monitor re-registers on fresh `place_limit_entry` calls.
  - Result: any limit order resting on OKX at restart time is invisible to the restarted bot. If it fills, `_handle_pending_filled` never fires → no OCO attach → unprotected position. Same class as the phantom-cancel orphans, different root.
  - Operator's *"manuel sildim"* observation partially true: they deleted the live MFE-locked OCO in the UI (which bot then recreated on next cycle) but couldn't see the orphan from 05:44 since OKX's algo page groups by symbol, not by algoId — the two stacked OCOs looked like one row.
- **Fix A — journal algoId after every OCO replacement** (`src/execution/position_monitor.py:444`):
  - Added `self._on_sl_moved(t.inst_id, t.pos_side, list(t.algo_ids))` right after the `tp_revised` log in `revise_runner_tp`. Matches `lock_sl_at`'s existing pattern. Callback failures are swallowed (logged as `on_sl_moved_callback_failed_after_tp_revise`) — the OKX-side revise already succeeded, returning False would cause the next cycle to re-try cancel+replace against an already-new algo.
  - Contract tightening: after revise, journal's `algo_ids` and in-memory `_tracked.algo_ids` stay in sync. Next restart's rehydrate reads the live algo, next revise cancels the correct ghost, no orphan.
- **Fix B — startup reconciliation for orphans** (`src/bot/runner.py:_reconcile_orphans`):
  - Existing log-only pass for position mismatch unchanged.
  - New pass 1: `_cancel_orphan_pending_limits` — scan `client.trade.get_order_list(instType="SWAP")`, cancel every resting limit. Safe because `_pending` is empty at startup (first cycle hasn't run yet); any live limit is orphan by construction.
  - New pass 2: `_cancel_surplus_ocos` — scan `list_pending_algos("oco")`. For each (inst_id, pos_side) with a journal OPEN row, compare algoIds to `journal.algo_ids` and cancel surplus. OCOs for keys *without* a journal row get log-only (`orphan_oco_no_journal_row`) — never auto-cancel a stop that might be protecting an untracked-but-legitimate position; operator intervenes via log alert. This is defense-in-depth: even if Fix A regresses, the next restart self-heals.
- **Immediate remediation (one-shot):** `scripts/cancel_orphans.py` canceled the three live orphans found via `probe_open_orders.py`:
  - DOGE OCO `3494715650050920448` (surplus stop).
  - ETH resting limit `3495274735394340864` (@2280.04 sz=29).
  - BNB resting limit `3495272138516185088` (@620.3 sz=1182).
  - Post-cleanup probe: 5 positions each with one OCO, 0 pending limits.
- **Tests:** 6 new, all green.
  - `test_position_monitor.py` — `test_revise_runner_tp_invokes_on_sl_moved_with_new_algo_ids` (regression), `test_revise_runner_tp_does_not_invoke_callback_on_place_failure` (callback only fires on success), `test_revise_runner_tp_swallows_on_sl_moved_exception` (journal failure doesn't re-try the OCO).
  - `test_bot_runner.py` — `test_reconcile_cancels_every_resting_pending_limit`, `test_reconcile_cancels_surplus_oco_not_in_journal`, `test_reconcile_leaves_oco_for_keys_without_journal_row`.
  - Full suite **763 passed** (up from 757).
- **Dataset:** `rl.clean_since` unchanged. These are correctness fixes to restart reconciliation + journal write-back — they don't alter scoring, sizing, entry, or exit geometry. Post-deploy trades sit on the same regime.
- **Operator contract (restart):** when the bot restarts, logs will now show `orphan_pending_limit_canceled` or `surplus_oco_canceled` warnings if any resting orders / surplus OCOs existed pre-restart. Zero warnings = clean state. `orphan_oco_no_journal_row` ERROR still needs manual investigation (a live OCO without a tracked position is either a legitimate un-journaled position or a leftover from a manual UI action).
- **What this does NOT fix:** the `tp_revise_runner_already_gone` verified=true pathway at 07:09 UTC in the root cause was OKX correctly reporting a truly-gone algo; the bug wasn't there. It's a harmless side-effect of the stale-algoId bug but would remain even with both fixes applied. When `revise_runner_tp` successfully journals the new algoId (Fix A), rehydrate won't produce stale tracking anymore, so 07:09-class logs should disappear post-deploy.

### 2026-04-20 — Postmortem: 3rd phantom-cancel orphan (fix shipped, bot not restarted)

- **Trigger:** operator observed a BTC position with no OCO attached (live long 11 contracts, entry 74274.9) and reported *"emrin yarısı tp olmuş diğer yarısı da iptal edilmiş … tp sl siz bir btc pozu var şu anda"*. Asked for detailed diagnosis of how another unprotected BTC emerged after the phantom-cancel fix.
- **Root cause (operational, not a code regression):**
  - The phantom-cancel fix (commit `a624385`) was authored at **2026-04-20 07:06 local** (04:06 UTC). The bot had been running continuously since 2026-04-18 02:55 and **was never restarted**, so the running instance still contained the old buggy `poll_pending` that emitted `CANCELED` + dropped tracking whenever `cancel_order` raised a generic `Exception`.
  - Between bot start and the fix timestamp, three `pending_timeout_cancel_(failed|exception)` events occurred in the log:
    1. 04:08:25 local — BTC ord 3494461668590866432, sCode 50001 → the operator found + manually canceled this one (captured in the original phantom-cancel changelog).
    2. 04:12:56 local — DOGE ord 3494471132685520896, sCode 50001 → also found + manually canceled.
    3. **05:53:30 local — BTC ord 3494673746828185600, generic exception** → **not noticed at the time**. Limit was at 74268.1, sl 73971.0, tp 75159.3.
  - Orphan #3's timeline:
    - 05:53:30 local: cancel attempt threw generic exception under old code → `logger.exception(pending_timeout_cancel_exception…)` followed by `pending_canceled reason=timeout` emitted 3 ms later. Pending row popped. Order **still live on OKX**.
    - 06:06:07 local (03:06:07 UTC): orphan limit filled naturally at 74268 (price retraced through the zone on its own).
    - 06:04:29 local (22 seconds earlier): a fresh BTC limit B at 74281.8 also filled. Journal records trade `7bc9d5bb051a486fb03437866a23a102` for B with `num_contracts=11`. OCO attached to B.
    - OKX aggregated both longs into a single position: `size=22 entry=74274.9` (weighted average of 74268 × 11 and 74281.8 × 11). Bot believed `runner_size=11`.
  - Today's closing half:
    - 07:18:25 local: dynamic TP revise replaced B's OCO (new algo 3494905668732227584, tp 75145.53, sl 73984.69). sz still 11.
    - 10:21:05 local: MFE-lock fired (price crossed +2R MFE). Cancel + replace worked (`sl_locked_via_replace … new_algo=3495273429333295104 new_sl=74349.17 tp=75145.53`).
    - 10:21:21 local: BTC wicked to 75163.4 → new OCO's TP triggered → reduce-only market sell 11 contracts at 75163.4. OKX position went 22 → 11. Journal trade 7bc9d5bb remained `OPEN` because the monitor's tracked size was 11 (not 22); OKX reporting 11 looked like "no change" to the old-code monitor.
  - Result: 11 orphan contracts from limit A (entry 74268) remain live as BTC long with no OCO. Aggregated entry reads 74274.9 in the UI because OKX didn't split the avg basis when half closed.
- **What the operator saw:** in the OKX algo history, the MFE-locked OCO `3495273429333295104` shows as split: TP leg → effective/filled at 75163.4, SL leg → auto-canceled (standard OCO contract). This is the "half TP, half iptal" view. Underneath, the position didn't close — half the aggregate was just never tracked.
- **Why the phantom-cancel fix doesn't cover this retroactively:** the fix only prevents *future* dropped rows. Orphan #3 was already dropped before the fix existed. The running bot also wouldn't benefit from the fix without a restart — so a 4th+ orphan could happen on any further transient OKX outage until the process restarts.
- **Immediate remediation (operator):**
  1. Close the unprotected 11 BTC long manually on OKX (market close, reduce-only).
  2. Restart bot — this picks up both the phantom-cancel fix (`a624385`) *and* the flat-USDT $R override (`4c0e971`, needed for the operator's new `RISK_AMOUNT_USDT=60` in .env).
  3. Journal cleanup for trade `7bc9d5bb`: either mark closed manually (WIN, exit 75163.4, pnl ≈ +$135) or let the reconcile-orphans path log it and adjust later from the OKX fills history. Risk replay will reset correctly on restart from `journal.replay_for_risk_manager`.
- **Process learning added:** after any code change to execution-path modules (`position_monitor.py`, `runner.py`, `okx_client.py`), a bot restart is required for the fix to apply. `.env` changes (e.g. `RISK_AMOUNT_USDT`) also need a restart. Add to operator checklist: *"kod commit'i = bot yeniden başlat; yeniden başlatmadan fix canlı değildir."*
- **Secondary journal inconsistency (logged, not critical):** MFE-lock's `_on_sl_moved` callback set `sl_moved_to_be=1` in the journal but did **not** update `sl_price` — journal still shows original plan SL (73984.69) while the live OCO had new SL (74349.17). Separate fix to journal the replaced SL price alongside the flag. Tracked as a future cleanup (no trade-correctness impact today because the trade closed on TP, not SL).
- **No code change in this entry** — this is a postmortem. The preventive fix is already in `a624385` and activates on the next bot restart.

### 2026-04-20 — Flat-USDT $R override + zone-resize ceil parity

- **Trigger:** operator observed 5 open positions with notional spread from $4,280 (SOL) to $16,381-ish (BTC) and mis-read this as unequal $R. Digging into the journal showed notionals were *correct* (bigger notional ↔ tighter sl_pct gives equal $R per construction), but planned `risk_amount_usdt` actually varied $32.68 → $47.98 across the 5 positions — a $15 spread on a nominal $50 target. Operator quote: *"R=\$50 belirliyorsam stopu buna göre ayarlamasını sağla … bakiye arttıkça oradaki istediğim r miktarını manuel olarak elle değiştirip buna göre devam edebilirim."*
- **Root cause (two stacked bugs):**
  1. `runner.py:1413` — `risk_balance = min(total_eq, risk_mgr.current_balance)`. Live OKX equity includes unrealized PnL on concurrent open positions. When 5 symbols open in quick succession (multi-open window 02:12–03:06 UTC), each subsequent symbol sees `total_eq` dragged down by the prior positions' floating drawdown (entry spread + fees). Each entry sized off a slightly different balance snapshot → different `max_risk_usdt` → different `plan.risk_amount_usdt`.
  2. `setup_planner.apply_zone_to_plan` — zone path is the PRIMARY execution path (scalp-native rewire 2026-04-19), but it still floor-rounded contracts (`max(1, int(notional / ctu))`) even after rr_system's 2026-04-19 ceil contract flipped elsewhere. Any ceil work in `calculate_trade_plan` got undone immediately when the zone re-sized.
- **Fix — operator-set flat $R override (bypasses mechanism #1 entirely):**
  - New optional knob: `trading.risk_amount_usdt` (YAML) + `RISK_AMOUNT_USDT` (env). Env wins over YAML. Null/absent = legacy `balance × risk_per_trade_pct`.
  - `calculate_trade_plan` (`src/strategy/rr_system.py`) accepts `risk_amount_usdt_override`: when provided, bypasses `account_balance × risk_pct` and uses the override as `max_risk_usdt` directly. Safety rail: override ≤ 10% of `account_balance` (mirrors the existing `risk_pct ≤ 0.1` ceiling) — raises `ValueError` if exceeded so a stale too-high override on a crashed balance can't size past the per-trade loss cap. Threaded through `build_trade_plan_with_reason` → `runner.py` → read from `cfg.trading.risk_amount_usdt`.
  - `.env.example` — new `RISK_AMOUNT_USDT=` entry with docstring: operator-visible $R constant, bump manually as balance grows (demo $5k → $50; live $10k → $50/$100 per your own ramp plan).
  - `config/default.yaml` — new `trading.risk_amount_usdt: null` with inline operator note.
- **Fix — zone-resize ceil parity (bypasses mechanism #2):**
  - `setup_planner.apply_zone_to_plan` now mirrors `calculate_trade_plan`'s 2026-04-19 ceil contract: `plan.capped=False` → `num_contracts = ceil(risk / per_contract_cost)`; `capped=True` → floor (respects leverage/margin ceiling). Removes the silent `max(1, …)` minimum that would sometimes size 1 contract above the override.
  - Keeps equal-$R guarantee through the zone re-size — was the load-bearing gap between rr_system's ceil and the execution path's floor that produced the residual $2-$13 spread even after mechanism #1 was hypothetically fixed.
- **Expected behavior change:**
  - Before: 5 sequential opens at balance $3,268–$4,798 produced `risk_amount_usdt` ∈ [$32.68, $47.98]; zone re-size floored below target; operator saw $15 spread across 5 live positions on a nominal $50 target.
  - After (with `RISK_AMOUNT_USDT=50`): every position sizes at `max_risk_usdt = $50` regardless of live equity or position-open sequence; zone re-size ceil lands at $50 + ≤ one per_contract_cost step (< $3 per current symbol universe); operator sees $50-$53 band across all 5 positions. TP still zone/heatmap-driven via `tp_dynamic_enabled=true` + `target_rr_cap=3.0` — override *only* controls $R, not TP geometry.
  - Percent mode unchanged when override is null — no impact on tests or downstream callers that don't pass the override.
- **Safety rail specifics:**
  - Override must be > 0 (ValueError on ≤ 0).
  - Override must be ≤ `account_balance × 0.1` (ValueError on exceed). Prevents the "balance crashed to $500, stale $50 override sizes at 10%" scenario from silently sliding past the per-trade loss ceiling.
  - `TradingConfig._risk_amount_positive` pydantic validator rejects non-positive YAML values at load time.
  - `load_config` parses `RISK_AMOUNT_USDT` env var; raises `ValueError("not a valid float")` on garbage input (e.g. `RISK_AMOUNT_USDT=notanumber`). Empty string / unset preserves YAML value.
- **Tests:** 11 new, all green (757 passed total, up from 738):
  - `test_rr_system.py` — `test_override_replaces_balance_times_pct`, `test_override_equal_r_across_heterogeneous_symbols` (5-symbol matrix, spread < $3.50), `test_override_bypasses_balance_shimmer` (same override on $1k vs $10k balances → identical plan), `test_override_safety_rail_rejects_above_10pct_of_balance`, `test_override_non_positive_raises`, `test_override_none_falls_back_to_percent_mode`.
  - `test_setup_planner.py` — `test_apply_zone_to_plan_ceil_keeps_risk_at_or_above_target_uncapped`, `test_apply_zone_to_plan_capped_plan_still_floors` (capped path still uses floor).
  - `test_bot_config.py` — `test_risk_amount_usdt_null_default`, `test_risk_amount_usdt_parsed_from_yaml`, `test_risk_amount_usdt_rejects_non_positive`, `test_risk_amount_usdt_env_wins_over_yaml`, `test_risk_amount_usdt_env_empty_falls_back_to_yaml`, `test_risk_amount_usdt_env_rejects_invalid_float`.
- **Operator playbook:**
  - Demo (balance $3k-$8k): add `RISK_AMOUNT_USDT=50` to `.env`. Every SL loses ≈ $50, every TP pays ≈ $150 (3R hard cap, before fees).
  - Bakiye $8k → $15k'ya çıktığında: R'yi manuel olarak $75 veya $100'e çıkar (`RISK_AMOUNT_USDT=75.0`), bot'u yeniden başlat. Safety rail 10% ceiling'i (dolayısıyla $8k'da max $800) vurmadığı sürece sorunsuz.
  - Live'a geçerken: boş bırak veya `RISK_AMOUNT_USDT=` ile sil, `trading.risk_per_trade_pct: 0.5` (demo'dan düşürülmüş) percent mode yeterli. Override'ı live'da açmak operatörün sermaye rampaya göre tercihi.
- **Dataset:** `rl.clean_since` unchanged. Override is a *sizing input*, not a scoring/exit regime shift; the ceil parity fix in `apply_zone_to_plan` tightens an existing invariant rather than flipping it. Post-deploy `risk_amount_usdt` distribution will cluster tighter (spread $15 → ≤ $3) but max-R ceiling and TP geometry are identical.
- **What's explicitly NOT fixed:** the `min(total_eq, current_balance)` anomaly in runner.py (mechanism #1) is *bypassed* by the override but not repaired for percent mode. Future work when live/percent-mode matters; on demo with override active the anomaly is mooted. Tracked as a Phase 11 prerequisite before cutting to live percent mode.

### 2026-04-20 — Phantom-cancel fix (orphan resting limits)

- **Trigger:** operator observed 2 extra resting limit orders on BTC + DOGE while 5 positions were already open (BTC long orphan @ 74053.4 sz=12, DOGE long orphan @ 0.09345 sz=64 — both unrelated to the live positions, no OCO attached). Operator quote: *"Pozisyon olan paritede 2. bir long işlemi neden var … ben manuel olarak iptal ediyorum. Sen de kod üzerinde bir daha böyle bir şey yaşanmaması için bu sorunu düzelt."*
- **Root cause:** `PositionMonitor.poll_pending` + `cancel_pending` emitted a CANCELED event and dropped the pending row **even when the OKX cancel call failed with a non-idempotent error** (sCode 50001 "service temporarily unavailable", or any generic exception). Smoking-gun log lines confirmed it: `pending_timeout_cancel_failed … code=50001 … emitting CANCELED anyway`. Both orphans originated from 2026-04-20 04:08 / 04:12 UTC during a brief OKX transient outage — the monitor claimed the limits were canceled, runner cleared the pending slot, next cycle placed fresh limits that eventually filled (current 5 positions), old limits remained live as unmonitored orphans. If price had drifted ~0.7-0.8% down, they'd have filled into unprotected longs (no OCO, no journal row, no MFE lock, no dynamic-TP revise).
- **Fix** (`src/execution/position_monitor.py`):
  - `poll_pending` — added `cancel_landed: bool` tracker. Set to True only on (a) success, or (b) idempotent-gone `OrderRejected` code in `{51400, 51401, 51402}`. Non-gone `OrderRejected` (e.g. 50001) and generic exceptions log + `continue` — pending row is preserved so the next poll retries. Old path emitted CANCELED + popped the row unconditionally after the except clause; new path only emits + pops when `cancel_landed=True`.
  - `cancel_pending` — mirror fix, but **re-raises** on non-gone failure instead of swallow+continue (caller-driven cancel; caller needs to know the cancel didn't land so it can retry/alert). Idempotent-gone still swallowed as success. No production callers today — only test callers — so re-raise is a safe contract tightening.
  - Log wording changed from "emitting CANCELED anyway" → "keeping tracking, retry next poll" (poll) / "re-raising" (cancel_pending). The prior wording was literally the bug description.
- **Tests:** 5 new regressions in `tests/test_pending_monitor.py`:
  - `test_poll_pending_keeps_row_when_timeout_cancel_fails_transient` — sCode 50001 on cancel → no event, row preserved, one cancel attempt logged.
  - `test_poll_pending_keeps_row_when_timeout_cancel_raises_generic` — same for non-`OrderRejected` exceptions.
  - `test_poll_pending_retries_cancel_on_next_poll_after_transient_failure` — first poll fails, second poll succeeds → row finally clears, both cancel calls recorded.
  - `test_cancel_pending_reraises_on_non_gone_rejection` — caller-driven cancel + sCode 50001 → `pytest.raises(OrderRejected)`, row preserved.
  - `test_cancel_pending_reraises_on_generic_exception` — same for `RuntimeError`.
- **Probe script:** `scripts/probe_open_orders.py` — read-only diagnostic listing live positions + pending limits + pending algos. Handy for future orphan hunts without touching account state.
- **Dataset:** `rl.clean_since` unchanged. This is a correctness fix to the cancel path; it doesn't shift scoring, sizing, or exit geometry on post-deploy trades.
- **Operator contract:** on the next OKX transient outage, the bot will log the failure and keep retrying each poll (180s cycle cadence) until the cancel lands. Pending order stays in the monitor until OKX says it's truly gone. No phantom orphans.

### 2026-04-20 — MFE-triggered SL lock (Option A)

- **Trigger:** operator observed two open shorts almost touching TP (~2.5R MFE) then reversing. With single-leg 3R OCO + dynamic TP revise + partial TP disabled, nothing protects a deep winner from round-tripping back to -1R — the static SL sits at plan distance forever. Operator quote: *"shortlar neredeyse tp seviyesin yakın bir yerden döndü … burada girişte stop olmak yerine nasıl bir geliştirme yapabiliriz"*. Four options discussed (MFE-lock, ATR-trail, momentum-fade near TP, partial-TP reinstatement at 2R); picked Option A for its simplicity + high-EV "risk removal" contract.
- **Fix — cancel+replace runner OCO when MFE crosses threshold** (`src/execution/position_monitor.py`, `src/bot/runner.py`):
  - `_Tracked.sl_lock_applied: bool = False` — one-shot flag, True blocks further locks on the same position.
  - `PositionMonitor.lock_sl_at(inst_id, pos_side, new_sl)` — cancels runner OCO (`algo_ids[-1]`), re-places with `new_sl` + original TP + original runner_size. Mirrors `revise_runner_tp`'s failure handling verbatim: idempotent cancel codes `{51400,51401,51402}` verified against live-pending list; unknown cancel error → abort, OCO untouched; place failure after cancel → CRITICAL log, UNPROTECTED, `sl_lock_applied` still set (prevents retry spin on the same broken cycle). Sets `t.sl_price = new_sl` so a subsequent dynamic-TP revise uses the locked SL on the replacement.
  - Direction guard: long's `new_sl < tp2_price`, short's `new_sl > tp2_price` — else abort (would tighten into a worse stop).
  - `get_tracked_runner` now exposes `sl_lock_applied` so the runner gate can short-circuit without touching monitor internals.
  - `BotRunner._maybe_lock_sl_on_mfe(symbol, pos_side, state)` in the per-symbol cycle, right after `_maybe_revise_tp_dynamic`. Computes `mfe_r = sign × (current_price - entry) / plan_sl_distance` using `state.current_price` (Pine-settled 3m close). Fires when `mfe_r ≥ sl_lock_mfe_r` AND not already applied AND not post-BE (TP1 BE replacement is already at BE — re-locking is churn at best). Dispatches via `asyncio.to_thread(monitor.lock_sl_at, …)`.
  - New SL computation: `lock_r == 0.0` → `entry + sign × entry × sl_be_offset_pct` (BE with fee buffer, matches TP1 BE replacement convention); `lock_r > 0` → `entry + sign × lock_r × plan_sl_distance` (locked profit).
- **Config (`config/default.yaml` + `ExecutionConfig`):**
  - `execution.sl_lock_enabled: true` (default on)
  - `execution.sl_lock_mfe_r: 2.0` — trigger at 2R MFE
  - `execution.sl_lock_at_r: 0.0` — lock at BE + fee buffer (set >0 for profit-lock, e.g. 0.5 = guaranteed +0.5R)
- **Expected behavior change:**
  - Before: short goes +2.5R → reverses → stops out at -1R → round-trip loss = full -1R on what was a deep winner.
  - After: short goes +2.0R → monitor pulls SL to entry+fee_buffer (BE). If the reversal continues past entry, SL fires at BE (realized ≈ 0R before fees, ~-0.05R after). If price resumes down to TP, win = 3R (unchanged). Net effect: "almost-winners" no longer cost -1R; upper bound on reward is still 3R.
  - **Break-even WR shift:** at pure 3R (current) break-even is 25% (1/(1+3)). With the MFE lock, winners that *almost* won now contribute ≈ 0R instead of -1R; break-even falls proportional to the frequency of "hit 2R then reversed" trades. Data-driven — factor-audit will quantify after ≥30 closed post-deploy trades.
- **Skip conditions (explicit):**
  - `plan_sl_price <= 0` (post-BE rehydrate, plan SL lost across restart) — skip, the ratio math is unreliable.
  - `be_already_moved=True` (legacy partial-TP cascade hit BE) — skip, runner OCO already at BE.
  - `sl_lock_applied=True` — skip, already locked.
  - `current_price <= 0` or `plan_sl_distance <= 0` — skip, bad state.
- **Tests:** 8 new in `tests/test_position_monitor.py` — happy path, one-shot idempotency, untracked position, wrong-side-of-TP guard, short-direction parity, place-failure unprotect, unknown-cancel abort, idempotent-cancel-proceeds. Full suite **738 passed**.
- **Dataset:** `rl.clean_since` unchanged (`2026-04-19T19:55:00Z`). This change affects *exit* geometry on post-deploy trades; it's additive to the SL/TP contract, not a scoring or sizing regime shift. Avg-R distribution will shift post-deploy but in a well-defined direction (reduced left tail from "almost-wins"), which factor-audit will pick up cleanly without mixing regimes.
- **Re-evaluation:** after ≥30 post-deploy closed trades, factor-audit checks:
  1. Frequency of `sl_lock_applied` fires. Low (<30% of trades) → threshold too high (`sl_lock_mfe_r=2.0` rarely reached) or reversal pattern was exaggerated. Bump threshold down to 1.5R or reconsider.
  2. Distribution of realized R on locked trades. Should bimodal — cluster near 0R (locked and fell back) + cluster at 3R (went all the way). No middle-cluster = working as designed.
  3. Locked-and-fell-back %. If >60%, the "almost-win" bucket was real and the lock is load-bearing; if <30%, most 2R+ trades went to 3R anyway and the lock is neutral insurance.
- **Restart note:** existing positions opened pre-deploy rehydrate with `sl_lock_applied=False` default. Any of them that hit 2R MFE post-restart will now lock — a post-hoc benefit on the in-flight DOGE/BNB shorts at deploy time. `plan_sl_price` preserved across restart via rehydrate path; only the BE-moved rehydrate path (which passes `plan_sl_price=0.0`) skips the lock.

### 2026-04-19 (late night, cont. #2) — Fee-aware ceil sizing (equal USDT SL/TP across symbols)

- **Trigger:** post-partial-disable restart review of per-position realized risk. Operator quote: *"hala pozisyonlardaki sl ve kar miktarları farklı bunları eşitlemen gerektiğini söylemiştim sana"* — SL/TP USDT amounts were still varying $40-$54 per position (on nominal $55 target) even after partial TP came off. Root cause: `int(notional // contracts_unit_usdt)` floor-rounding truncates harder on symbols with large per-contract USDT steps (BTC ctu=$680 at 0.01 ctVal × $68k) than on symbols with fine steps (DOGE ctu=$0.35). Low-price coins landed closer to $55; BTC landed ~$43.
- **Fix — ceil on per-contract TOTAL cost** (`src/strategy/rr_system.py:188-203`):
  - Un-capped path: `num_contracts = math.ceil(max_risk_usdt / per_contract_cost)` where `per_contract_cost = effective_sl_pct × contracts_unit_usdt` and `effective_sl_pct = sl_pct + fee_reserve_pct`.
  - This sizes contracts so **total realized loss** (price move + fee reserve budget) clears `max_risk_usdt` on every symbol. Overshoot bounded by one per_contract_cost step — < $3 per position on the current universe (BTC $3.40, SOL $1.54, ETH $1.68, DOGE $0.003, BNB $0.42).
  - Capped path (leverage/margin ceiling binds) still floors — respecting the hard leverage cap wins over the equal-risk target. When the ceiling can't afford a single contract, `max_contracts_by_notional = 0` propagates honestly (no forced `max(1, …)`) so entry_signals rejects with `zero_contracts`.
  - `actual_risk_usdt` journal field stays **price-only** (`sl_pct`, not `effective`) so it represents the bare price-move slice. The fee reserve portion of per_contract_cost is not realized loss if fees/slippage come in under budget — it's a sizing headroom.
- **Operator-visible contract:** each position's realized SL loss on OKX (price + fees) is now ≥ target_risk, with max overshoot ≈ the widest symbol's per_contract_cost. At 1% R on $5,500 demo: each position lands in $55-$58 band instead of the former $40-$54 band. TP reward clears $165 on winners (ceil * 3 × rr) and is bounded above by ~`$165 + 3 × per_contract_cost`.
- **Mechanical side-effects:**
  - `required_leverage` still reports off `ideal_notional = max_risk / effective_sl_pct` (unchanged) for telemetry.
  - `min_lev_for_margin` computed off pre-ceil `notional`, not `actual_notional`. In practice margin headroom is large enough that ceil's contract bump never exceeds the margin floor — smoke test confirms.
  - Module docstring updated (`rr_system.py:10-18`): "actual risk below requested" rule reworded — now only true in capped path; un-capped path targets realized ≥ requested with bounded overshoot.
- **Dataset:** `rl.clean_since` bumped `2026-04-19T17:35:00Z → 2026-04-19T19:55:00Z`. Rationale: realized-R distribution under floor-rounding was left-skewed below nominal; under ceil it's near-target with right-tail bounded. Mixing the two in avg-R / expectancy calcs would blur the regime shift. Cost: 2h20m of post-partial-off clean window falls out; 0 closed trades in that window (the 5 positions the operator manually closed opened earlier).
- **Re-evaluation:** after ≥30 post-flip closed trades, factor-audit inspects:
  1. Distribution of `risk_amount_usdt` (journal) + matching `realizedPnl` (OKX). Should cluster ≥ max_risk_usdt with tail bounded at `max_risk + per_contract_cost`. Flat-below target → ceil not engaging (likely capped-path dominance).
  2. sCode 51008 incidence. Ceil raises notional slightly vs floor; if margin buffer is too tight, 51008 re-emerges. None observed in smoke — expect zero on live.
- **Tests:** 1 new + 2 existing updated.
  - `tests/test_rr_system.py::test_contract_rounding_keeps_risk_at_or_above_target` (renamed from `_below_target`) — flips the invariant for the un-capped path.
  - `tests/test_rr_system.py::test_equal_realized_loss_across_heterogeneous_symbols` — 5-symbol matrix (BTC/ETH/SOL/DOGE/BNB), asserts total realized (price+fee reserve) ≥ target and spread < $3.50.
  - `tests/test_entry_signals.py::test_reject_when_partial_tp_split_would_be_degenerate` + `test_partial_tp_disabled_skips_split_gate` — tightened OB (470→440 at same price) so sl_pct≥10% produces per_contract_cost≥max_risk → ceil lands on exactly 1 contract (not splittable). Same logical scenario, params tuned to new ceil math.
  - Full suite **730 passed**.
- **Smoke (`--dry-run --once`):** 2 PLANNED decisions (ETH short + BNB short) at ~$50 total realized target on $5000 dry-run balance. Per-symbol math: BNB `contracts=202 notional=$4663 risk_price_only=$45.41` → total incl fee reserve ≈ $50.08 (ceil overshoot $0.08). ETH `contracts=755139 risk_price_only=$42.86` → total ≈ $50 (tighter overshoot due to fine ctu).
- **Restart note:** operator has 0 open positions + 0 pending algos at time of this change (verified earlier this session). Next fresh bot cycle will produce positions sized under ceil regime.

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

`displacement_candle` · `ema_momentum_contra` · `vwap_misaligned` · `cross_asset_opposition` (altcoin veto when BTC+ETH both oppose). *`premium_discount_zone` and `htf_tp_ceiling` are wired but currently disabled (`analysis.premium_discount_veto_enabled=false` and `analysis.htf_sr_ceiling_enabled=false`) — see changelog 2026-04-19.*

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
- **Fee-buffered SL-to-BE** (`sl_be_offset_pct=0.001`). After TP1 fill the replacement OCO's SL sits a hair past entry on the profit side — covers remaining leg's exit taker fee + slippage. *Inert while `partial_tp_enabled=false` (2026-04-19 late-night) — TP1 never fires, so BE callback never runs. The code path stays; flipping partial back on reinstates the BE behavior without a second toggle.*
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

5 OKX perps — BTC / ETH / SOL / DOGE / BNB. BTC + ETH + BNB are market pillars (major-class book depth); SOL + DOGE are altcoins gated by the cross-asset veto. XRP pulled on 2026-04-19 (pm) after the attach-race incident; ADA pulled on 2026-04-19 (eve) after hitting OKX demo OI platform cap (`sCode 54031`). Their per-symbol override maps remain in YAML (harmless when not watched) so reinstating any of them is one-line once the underlying blocker clears.

`max_concurrent_positions=5` (every pair can hold a position simultaneously — no slot competition; confluence gate still picks setups, but cycle isn't queue-limited). Cross margin, `per_slot ≈ total_eq / 5 ≈ $1000` on a $5k demo. R stays 1% of total equity ($50); only the notional ceiling shrinks proportionally.

Cycle timing at 3m entry TF = 180s budget: typical 150–180s with 5 pairs (comfortable inside the budget after 7→5 rollback). DOGE + ADA (if reinstated) + XRP (if reinstated) leverage-capped at 30x; SOL/BNB inherit OKX 50x cap.

Per-symbol overrides (YAML, ADA/XRP rows kept for easy reinstatement):
- `swing_lookback_per_symbol`: DOGE=30 (thin 3m book; ADA/XRP=30 preserved).
- `htf_sr_buffer_atr_per_symbol`: SOL=0.10 (wide-ATR, narrower buffer); DOGE=0.15; BNB inherits global 0.2.
- `session_filter_per_symbol`: SOL + DOGE=[london] only. BNB inherits global (london+new_york) as major.
- `min_sl_distance_pct_per_symbol`: BTC 0.004, ETH 0.006, SOL 0.010, DOGE 0.008, BNB 0.005.

Adding a 6th+ pair: drop into `trading.symbols`, add `okx_to_tv_symbol()` parametrized test, add `derivatives.regime_per_symbol_overrides`, add `min_sl_distance_pct_per_symbol`, watch 20-30 cycles for `htf_settle_timeout` / `set_symbol_failed`. Coinalyze free tier supports ~8 pairs at refresh_interval_s=75s; beyond that needs paid tier or longer interval.

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
- **1m as a zone source in `setup_planner`** — add `ltf_fvg_entry` and/or `ltf_sweep_retest` as new zone sources (1m unfilled FVG or 1m sweep-reversal). Same architectural pattern as existing sources (zone + post-only limit + `max_wait_bars` cancel), just tighter stops → larger notional at flat R. Expected tradeoff: better micro-entry quality at the cost of higher cancel rate (1m FVGs fill fast). `max_wait_bars` for 1m sources likely needs to be 3-4 (not 10). Data-driven decision: revisit after Phase 9 GBT confirms 1m factors carry weight; if they don't, a 1m zone source likely won't either.
- **1m-triggered dynamic trail / runner management** — dynamic exit after TP1 using the 1m oscillator. Currently SL-to-BE is static at TP1 fill; a 1m momentum fade could progressively tighten SL on the runner leg. Complements (does not replace) the existing `ltf_reversal_close` defensive-close gate, which is a binary veto, not a trail. Data-driven decision after 100+ post-pivot closed trades — are we leaving too much on TP2, or is BE-after-TP1 the right discipline?
- **ATR-trailing SL after MFE threshold (Option B to the 2026-04-20 MFE-lock)** — the MFE-lock (Option A) is one-shot: crosses 2R → SL pulled to BE → done. A true trail would keep going: after the lock fires, every cycle update SL to `current_mark ± trail_atr × ATR` so the stop walks with price. Chandelier-style. Tradeoff: `trail_atr` tuning is load-bearing — too tight (0.5 ATR) gets shaken out by normal noise, too wide (2 ATR) reduces to Option A. Only worth the code if Option A's locked-and-fell-back frequency data (see 2026-04-20 re-evaluation) shows a meaningful third bucket: "locked at BE but then price resumed to +2.5R and reversed again" — that's where a trail would have captured an extra 1-2R. Re-evaluate after ≥50 post-Option-A closed trades. Implementation sketch: new `execution.sl_trail_enabled` + `sl_trail_atr_mult` knobs, new `_Tracked.sl_trail_active` flag, new `monitor.trail_sl_to(new_sl)` method (mostly `lock_sl_at` minus the one-shot guard), runner gate `_maybe_trail_sl_after_lock` that only fires when `sl_lock_applied=True` AND `mfe_r > sl_lock_mfe_r + trail_atr_step_margin`. Keep cooldown + min-delta semantics from dynamic-TP revise to avoid OCO churn.
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
