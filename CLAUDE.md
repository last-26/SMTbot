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

### 2026-04-19 (late night, cont.) — Partial TP disabled

- **`execution.partial_tp_enabled: true → false`** (`config/default.yaml:332`).
- **Trigger:** post-hard-1:3-cap review of expected USDT reward per winning trade. Operator's mental model: "$55 SL risk → $165 TP reward" per position at 1% R on a $5.5k demo. With `partial_tp_enabled=true`, **full-win geometry = 2.25R** (50% @ 1.5R + 50% @ 3R = 0.75R + 1.5R). Plus int-contract rounding already shrinks actual_risk below nominal $55, so effective full-win USDT reward landed around **$97-$110**, not **$165**. Operator quote: *"tp1 tp2 olarak böldüğümüz kısımı da kaldıralım. bu sayede... beklediğim o 3R lık karı elde etmiş olurum."*
- **Effect:** `OrderRouter._place_algos` (`src/execution/order_router.py:218-251`) already branches on the flag — with `false`, it places a single OCO covering `plan.num_contracts` at `plan.tp_price = entry ± 3 × sl_distance`. TP1 leg no longer exists; SL-to-BE callback is inert (no TP1 fill ever triggers). `move_sl_to_be_after_tp1` kept as `true` (no-op while partial is off; flipping partial back on later reinstates original BE behavior without a second toggle).
- **Trade-off — expectancy shift, not pure upside:**
  - *Before (partial on):* full-win = 2.25R; "almost-win" (TP1 hits, TP2 reverses to BE) = +0.75R. Loss = -1R. So a 50% WR with 50% "almost-wins" = 0.5×2.25 + 0.5×0.75 - 0 = **1.5R avg**, or 33% WR + 33% almost + 33% loss = **0.75R avg**.
  - *After (partial off):* full-win = 3R; no "almost-win" bucket — TP1-touched-then-reverse is now a full -1R loss. 50% WR → 0.5×3 - 0.5 = **1.25R avg**, 33% WR → 0.33×3 - 0.67 = **0.33R avg**.
  - **Break-even WR (pre-fees):** was 1/(1+rr_effective) ≈ 22% at 2.25R + 0.75R bucket; now 1/(1+3) = **25%** at pure 3R. 3-point WR threshold increase. Winners earn more; "survives without winning" paths no longer exist.
- **Mechanical side-effects:**
  - `_runner_size` (`src/bot/runner.py:133-145`) returns full `num_contracts` (not the TP2 slice), so dynamic TP revision (`_maybe_revise_tp_dynamic`) now covers the whole position — intended.
  - `entry_signals._apply_partial_tp_split_feasibility` (`entry_signals.py:794`) bypassed — the `insufficient_contracts_for_split` reject reason no longer fires. No YAML/CLAUDE.md changes to the reject list needed (it stays in the unified list for historical trades; fresh trades just don't hit it).
  - `OCOAttachResult.algo_ids` becomes a 1-element list instead of 2. `PositionMonitor._Tracked.algo_ids[-1]` still points to the (now sole) OCO, so `revise_runner_tp` cancel+place flow works unchanged.
- **Open-positions impact:** 5 positions currently open were built with partial TP on, so each has TWO OCOs on OKX (TP1 at 1.5R + TP2/runner at 3R). This YAML flip **does not** migrate them — they keep their split structure until closed. Dynamic TP revise will continue to rewrite **only the runner leg** (TP2) to 3R per the hard cap; TP1 at 1.5R stays live on the book. Operator can manually cancel TP1 legs on OKX if they want the full 3R payout on the existing 5, but that's a destructive action and isn't automated here.
- **Dataset:** `rl.clean_since` bumped `2026-04-19T17:30:00Z → 2026-04-19T17:35:00Z`. Rationale: single-leg 3R expectancy regime is a meaningful OCO-geometry shift from split-OCO; mixing the two in reporter or RL windows would confuse avg-R metrics. Cost: 5 mins of pre-flip clean-window is re-dirtied; no closed trades in that window (the 5 open positions opened earlier).
- **Re-evaluation:** after ≥30 post-flip closed trades, factor-audit checks:
  1. Post-flip WR vs. 25% break-even. If < 25% net of fees, the partial-TP "almost-win" bucket was load-bearing — flip back on.
  2. Distribution of "max-favorable-excursion hit 1.5R then reversed" trades — if significant, that's the -0.75R swing that partial mode used to bank. Data will tell if dropping this bucket is net-positive.
- **Tests:** no code changes; YAML-only flip. Full suite **729 passed** with new default. Tests that explicitly pass `partial_tp_enabled=True`/`=False` as kwargs (`test_partial_tp.py`, `test_entry_signals.py:551-600`) remain valid — they exercise both branches regardless of YAML default.

### 2026-04-19 (late night) — TP-revise hardening + demo-wick artefact cross-check

Bundled follow-up to the 2026-04-19 fixes. Two incidents drove this:

1. **BNB 17:50 UNPROTECTED after dynamic TP revise**: runner OCO cancelled, replacement rejected with OKX `51277` (trigger on wrong side of mark) because the revise path computed `new_tp = entry + target_rr × sl_distance` where `sl_distance` had collapsed to ~0. Root cause: after SL-to-BE, `_Tracked.sl_price` was mutated to the BE price — the same field dynamic TP used for ratio math. Post-BE the "SL distance" was entry − BE ≈ 0, producing a TP target essentially on top of mark.
2. **Demo-wick poisoning**: OKX demo book produces wicks that never hit real exchanges. SL/TP triggered at `last` price fired on these fake ticks; the journal then recorded artefact fills that would poison RL training data.

**Fix A — Immutable `plan_sl_price` for TP ratio math** (`src/execution/position_monitor.py`, `src/bot/runner.py`)
- `_Tracked` gained immutable `plan_sl_price` — set once at `register_open`, never mutated by SL-to-BE. The existing `sl_price` remains the *active* SL leg (BE-aware) used by the real SL/TP order.
- `register_open(... plan_sl_price: Optional[float] = None)` with sentinel semantics: `None` → fall back to `sl_price` (legacy callers), explicit `0.0` → "unknown, disable revise" (rehydrate path for BE-moved positions after restart).
- `get_tracked_runner` exposes `plan_sl_price` verbatim (no fallback) so the runner sees 0.0 when unknown.
- `runner._maybe_revise_tp_dynamic` reads `plan_sl_price`; `plan_sl <= 0` short-circuits revise (avoids posting degenerate-RR OCOs).
- Three `register_open` call sites thread `plan_sl_price`: `_try_place_zone_entry` (`plan.sl_price`), `_handle_pending_filled` (`plan.sl_price`), `_rehydrate_open_positions` (`0.0` if `sl_moved_to_be` else `rec.sl_price`).

**Fix B — Enriched CRITICAL place-failure logs** (`src/execution/position_monitor.py`)
- SL-to-BE and revise place-failure paths now log `code=` and `payload=` from `OrderRejected`. Previously `err={!r}` dropped those fields, so every `51277`/`51008`/etc. looked identical in the log.

**Fix C — Verify 51400 against live algos before placing replacement** (`src/execution/position_monitor.py`, `src/execution/okx_client.py`)
- OKX demo has been observed returning `51400` ("algo does not exist") on a cancel call while the algo is still on the book. Placing a replacement OCO at that point leaves **two** stops on the position, both firing back-to-back at the next adverse wick.
- New `OKXClient.list_pending_algos(inst_id, ord_type="oco")` + `PositionMonitor._verify_algo_gone(inst_id, algo_id)` helper. When cancel returns an idempotent code (`51400/51401/51402`), we now query the live pending algos; only if the specific `algoId` is truly absent do we proceed to place the replacement. Network/API failure on the verify query is treated as NOT-gone (conservative; retry next poll).

**Katman 1 — Mark-price SL/TP triggers** (`src/execution/okx_client.py`, `src/execution/order_router.py`, `src/execution/position_monitor.py`, `src/bot/config.py`, `config/default.yaml`)
- `place_oco_algo(..., trigger_px_type: str = "mark")` now emits `slTriggerPxType`/`tpTriggerPxType` when set. OKX defaults to `last` which means a single demo-book wick fires SL/TP. `mark` uses the exchange's index-weighted mark which is immune to demo-only wicks.
- Threaded top-to-bottom: `RouterConfig.algo_trigger_px_type`, `PositionMonitor.__init__(algo_trigger_px_type=...)`. `ExecutionConfig.algo_trigger_px_type: "mark"` (YAML-overridable; flip to `"last"` to restore legacy behavior).
- SL-to-BE replacement OCO and runner-TP revision OCO both now use the configured trigger type.

**Katman 2 — Post-close Binance cross-check for artefact detection** (new: `src/data/public_market_feed.py`; updates: `src/journal/models.py`, `src/journal/database.py`, `src/bot/config.py`, `src/bot/runner.py`, `scripts/report.py`)
- New `BinancePublicClient.get_kline_around(binance_symbol, ts_ms)` fetches the concurrent Binance USD-M futures 1m kline for a given timestamp. Failure-isolated: every method returns `None` on network error, non-200, parse failure, empty list.
- `okx_swap_to_binance_futures("BTC-USDT-SWAP") → "BTCUSDT"`; rejects non-SWAP symbols (returns `None` → cross-check skips).
- Journal schema v3 additions on `trades`: `real_market_entry_valid INTEGER`, `real_market_exit_valid INTEGER`, `demo_artifact INTEGER`, `artifact_reason TEXT`. Idempotent `ALTER TABLE` migrations; `_row_to_record` tri-state bool parser (`None`/`0`/`1`).
- `BotRunner._cross_check_close_artefacts` runs after `journal.record_close`: maps symbol, fetches entry + exit candles via `asyncio.to_thread`, applies `price_inside_candle(price, candle, tolerance_pct)`. Both sides invalid → `demo_artifact=True` with reason like `"exit_above_binance_high"`. Any checked side invalid → flag set. Both feed-down → flag stays `None` (tri-state). Failure swallowed (never breaks journal close).
- `execution.artefact_check_enabled=true`, `artefact_check_timeout_s=5.0`, `artefact_check_tolerance_pct=0.0005` (5 bps — catches blatant demo wicks without flagging routine OKX-vs-Binance microstructure skew).
- `scripts/report.py --exclude-artifacts` filters `demo_artifact=1` rows out of the summary so operator can compare artefact-excluded vs raw PnL.

**Tests:** 30 new + 7 existing updated.
- `tests/test_public_market_feed.py` (19 cases): symbol mapping, `price_inside_candle` band + tolerance, `get_kline_around` happy path + 4 failure modes.
- `tests/test_journal_artifact_flags.py` (4 cases): round-trip, all-None, unknown-id raises `KeyError`, mixed-validity tri-state.
- `tests/test_runner_artefact_cross_check.py` (6 cases): client=None, unmappable symbol, both sides inside (valid), exit above real high (flagged), partial feed None, both sides missing.
- FakeClients in `test_order_router.py`, `test_partial_tp.py`, `test_position_monitor.py`, `test_sl_to_be.py` gained `trigger_px_type` kwarg; `test_sl_to_be.py` and `test_position_monitor.py::FakeRevisableClient` gained `list_pending_algos` stub.
- `tests/conftest.py:FakeMonitor.register_open` accepts `plan_sl_price`.
- Full suite **729 passed**.

**Dataset:** `rl.clean_since` unchanged (`2026-04-19T17:30:00Z`) — no trading-behavior regression from these changes; they're defensive. Mark-trigger switch is expected to *reduce* artefact SL/TP fills; the Binance cross-check labels any remaining artefacts for filtering at report/RL time.

**Re-evaluation:** after ≥30 closed post-deploy trades, inspect `demo_artifact` distribution. If it's >5% the mark-trigger switch didn't help enough and we should tighten tolerance or investigate instrument-specific OKX demo behavior. If it's ~0%, mark-trigger did its job and the cross-check is belt-and-suspenders — keep it on, low-cost.

**Restart note:** existing open positions rehydrate with `plan_sl_price=0.0` when `sl_moved_to_be=True` (dynamic TP revise disabled for them — safer than reviving with degenerate sl_distance). Fresh entries post-restart get the full protection chain.

### 2026-04-19 (night) — VWAP band-based zone for `vwap_retest`

- **Trigger:** post-1:3-RR-cap demo pass 16:26Z → 17:00Z (~34min). All 4 limit orders (BTC/ETH/SOL/DOGE) placed at `vwap_retest` zones **0.77%–1.54% below market** timed out after 10 bars with zero fills. Operator quote: *"giriş yerleri neye göre belirlendi belki biraz daha yakına getirilebilir… vwap cidden işleyen bir metrik, TradingView'deki gibi high-eq-low bantları bizde de olsun."*
- **Root cause:** `_vwap_zone` returned `(target − 0.25·ATR, target + 0.25·ATR)` and `zone_limit_price` picked `zone.low` for long → entry sat *past* VWAP on the discount side. A static ATR buffer has no relationship to session-realised VWAP volatility, so tight tape sessions never retest down to the limit. Fill rate <5% observed.
- **Fix — Pine (3m VWAP ±1σ bands):**
  - `pine/smt_overlay.pine:149-154` — 3m VWAP switched to 3-arg `ta.vwap(src, anchor=timeframe.change("D"), stdev_mult=1.0)`. Returns `[vwap, upper_band, lower_band]` via tuple-returning function inside `request.security`.
  - `pine/smt_overlay.pine:1136-1148` — two new SMT Signals rows: `vwap_3m_upper`, `vwap_3m_lower`. `sigTable` capacity 23 → still fits (22/23 used).
  - 1m / 15m VWAPs kept as single-value (bands scoped to entry TF).
- **Fix — Python:**
  - `src/data/models.py:SignalTableData` — `vwap_3m_upper`, `vwap_3m_lower` fields (default 0.0 = missing).
  - `src/data/structured_reader.py` — parses new rows with `_parse_leading_float`.
  - `src/strategy/setup_planner.py:_vwap_zone` — rewritten: long zone = `(vwap, upper_band)`, short zone = `(lower_band, vwap)`. When 3m is the nearest VWAP *and* Pine emitted bands, band-based zone fires. Otherwise: single-sided ATR half-band on the directional side of VWAP (still above VWAP for long, below for short — never past VWAP on the far side like the old zone).
  - `src/strategy/setup_planner.py:zone_limit_price` — `vwap_retest` added to mid-entry list alongside `liq_pool_near`. Entry sits at zone mid = `vwap ± 0.5σ` when bands available, or `vwap ± 0.5·0.25·ATR` on fallback. Pullback-edge sources (`ema21_pullback`, `sweep_retest`, `fvg_entry`) still use near-edge — unchanged.
  - SL math unchanged: `sl_beyond_zone` = `zone.low − sl_buffer·ATR` for long. With new zone shape, this puts SL just below VWAP (structurally meaningful — long thesis broken if VWAP fully reclaimed from above).
- **Tests:** 4 new — band-based long zone, band-based short zone, ATR fallback when 3m bands missing (session too young / older Pine), 3m bands ignored when 1m is nearer. 5 existing tests updated for new entry-mid math (vwap_retest apply_zone_to_plan cases). Full suite **699 passed**.
- **Smoke (`--dry-run --once`):** entry distances from market price dropped from **0.77%–1.54%** (pre-fix) to **0.52%–0.63%** (post-fix) on ATR fallback alone. Live Pine bands will further adapt to realised session volatility — tight tape → band narrows → entry closer; volatile tape → band widens → entry farther (protects against false fills).
- **Dataset:** `rl.clean_since` bumped `2026-04-19T16:15:00Z → 2026-04-19T17:30:00Z`. Cost: pre-rewire post-disable window (~1h15m) falls out of clean data. The 4 timed-out limits never opened positions so no trade data lost.
- **Re-evaluation:** after ≥50 closed post-rewire trades, Phase 9 GBT checks whether `vwap_retest` WR diverges from other sources. If band-based entries show positive lift over ATR-fallback entries (same source, different zone shape), treat the stdev-band path as canonical and drop ATR fallback; if no meaningful difference, keep fallback for resilience.
- **Restart note:** 2 open positions at time of deploy — OCOs on OKX side are live and independent of bot. Bot reconciles on startup via `OKXClient.get_positions` + `monitor.register_open`. `sl_moved_to_be` flag preserved via DB. Safe to restart.

### 2026-04-19 (night) — HTF TP/SR ceiling temporarily disabled

- **`analysis.htf_sr_ceiling_enabled: true → false`** (`config/default.yaml:121`).
- **Trigger:** post-restart demo pass 14:16Z → 16:10Z (~2h) produced **17 NO_TRADE decisions across 5 pairs, 10 of which rejected on `htf_tp_ceiling`** with confluence 3.95–6.50 (well above threshold 3.0). Only 1 order placed (BNB BULLISH via `vwap_retest` at 14:58Z) — the one pair whose direction flipped mid-window. Other 4 pairs stuck in a 0.2% range with 15m resistance sitting inside 1.5R of entry — `_apply_htf_tp_ceiling` trimmed TP below `min_rr_ratio=1.5` → plan rejected.
- **Root cause:** the new hard 1:3 RR cap (this morning) + tight-tape 15m-level clustering combine poorly. The gate was correct in principle (don't TP past a 15m resistance), but against a 1:3 contract with the 15m pool acting as the range ceiling it kills almost every long. Operator wants indicator-reversal signals to execute cleanly during data collection rather than wait for the range to break.
- **Side effect:** flag also gates `_push_sl_past_htf_zone` (`entry_signals.py:745`) — so SL is no longer auto-widened past HTF zones on entry. `min_sl_distance_pct_per_symbol` floors (BTC 0.4%, ETH 0.6%, SOL 1.0%, DOGE 0.8%, BNB 0.5%) remain the primary wick-protection layer. Dynamic TP revision (`tp_dynamic_enabled`) and `ltf_reversal_close` still active for post-fill defense.
- **Dataset:** `rl.clean_since` bumped `2026-04-19T13:10:00Z → 2026-04-19T16:15:00Z`. Cost: all pre-disable trades from this demo run (incl. the BNB 14:58Z open position) fall out of clean window. Post-disable trades train on the new gate regime only.
- **Re-evaluation:** after ≥50 post-disable closed trades, Phase 9 GBT factor audit decides the path. Three outcomes:
  1. HTF-ceiling distance has positive WR impact → restore as hard gate.
  2. Only the SL-push side has lift → split the flag into `htf_sr_tp_ceiling_enabled` + `htf_sr_sl_push_enabled` (code change, 1 new flag + 2 branches in `plan_and_size_entry`).
  3. Neither shows lift → leave off permanently, update CLAUDE.md hard-gate list to drop the reference.
- **Tests:** no code changes — YAML flag flip only. `test_entry_signals.py` still passes its HTF-ceiling tests with `htf_sr_ceiling_enabled=True` passed explicitly. `test_default_yaml_runner_tp_is_hard_1_3` unaffected (guards `target_rr_ratio`/`default_rr_ratio` alignment only).

### 2026-04-19 (night) — Hard 1:3 RR cap + dynamic TP revision

- **Trigger:** post-restart demo log showed 5 zone_limit_placed orders all sized off heatmap clusters that landed 8-12R away from entry (e.g. BTC `sl=$300 → tp=$3600`, 12:1 effective) despite `symbol_decision` claiming RR=4.5. Operator quote: *"100 dolar stop loss başına 300 dolar kar yani 1:3 olacak şekilde setuplar kurulmasını istiyorum. Ayrıca illaki bu tp seviyeleri tek seferlik eklenmesi yerine anlık gelen verilerle yorumlanıp dinamik bir şekilde öne veya arkaya çekilebilmeli."*
- **Root cause:** `apply_zone_to_plan` overrode `plan.tp_price` with `zone.tp_primary` (= nearest unswept liq cluster from heatmap) with no RR bound. The `default_rr_ratio=4.5` knob never reached the runner because the zone path bypassed it entirely.
- **Fix — hard 1:N cap:**
  - `src/strategy/setup_planner.py:apply_zone_to_plan` — new `target_rr_cap` param. When > 0, primary TP is forced to `entry ± cap × sl_distance` and every ladder rung is clamped to the same boundary.
  - `src/bot/config.py:ExecutionConfig` — new `target_rr_ratio` (default 0.0 = off; YAML sets 3.0).
  - `config/default.yaml` — `execution.target_rr_ratio: 3.0` + `trading.default_rr_ratio: 4.5 → 3.0` (entry_signals fallback aligned). Guard test `test_default_yaml_runner_tp_is_hard_1_3` enforces both knobs match.
  - `src/bot/runner.py:_try_place_zone_entry` — threads `cfg.execution.target_rr_ratio` into the planner.
- **Fix — dynamic TP revision:**
  - `src/execution/position_monitor.py:revise_runner_tp(inst_id, pos_side, new_tp)` — cancels `algo_ids[-1]` (runner OCO), places fresh OCO using the **active** SL (BE-aware via `_Tracked.sl_price`) and `_Tracked.runner_size`. Idempotent cancel codes `{51400, 51401, 51402}` treated as success. Place failure after cancel → trim algo_ids, CRITICAL log "runner unprotected" — no auto market-close.
  - `_Tracked` extended with `sl_price` + `runner_size` + `last_tp_revise_at`; `register_open` accepts both as kwargs; SL-to-BE updates `t.sl_price` in place.
  - `src/bot/runner.py:_maybe_revise_tp_dynamic(symbol, pos_side, state)` — recomputes target from live state each cycle (`new_tp = entry + sign × target_rr × sl_dist`), gates on `tp_revise_min_delta_atr × ATR` (avoid OCO churn) + `tp_revise_cooldown_s` rate-limit + `tp_min_rr_floor` (don't revise into sub-floor RR if mark drifted past entry). Dispatched via `asyncio.to_thread(monitor.revise_runner_tp, ...)`. Wired into `_run_one_symbol` between LTF reversal close and dedup.
  - `config/default.yaml` — `execution.tp_dynamic_enabled: true`, `tp_min_rr_floor: 1.5`, `tp_revise_min_delta_atr: 0.5`, `tp_revise_cooldown_s: 30.0`.
- **Tests:** 13 new — 5 `apply_zone_to_plan(target_rr_cap=)` cases (long/short clamp, ladder collapse, off-mode, post-widening recompute), 7 `revise_runner_tp` cases (happy path, no-op, untracked, idempotent cancel, unknown cancel error, place-fail unprotect, BE-aware SL preservation), 1 YAML guard. `tests/conftest.py:FakeMonitor` extended with `sl_price`/`runner_size` kwargs + `revise_runner_tp` + `get_tracked_runner` stubs. `runner._DryRunRouter` gained `place_limit_entry` + `attach_algos` stubs so the smoke test exercises the zone path. Full suite **695 passed**.
- **Smoke (`--dry-run --once`):** all 5 symbols place zone limits at exactly 1:3. Example BTC: `entry=75332.49 sl=75633.82 tp=74428.50 rr=3.00` vs. operator log's `tp=71698.52 ≈ 12R` pre-fix.
- **Re-tuning:** to change RR contract (e.g. 1:2 or 1:4), flip `execution.target_rr_ratio` AND `trading.default_rr_ratio` together — the guard test will catch drift. Do NOT introduce weighted-reward calculations as a substitute for the hard cap.

### 2026-04-19 (eve) — `vwap_1m_alignment` re-opened at 0.2

- **Change:** `config/default.yaml:196` — `vwap_1m_alignment: 0.0 → 0.2`. Other per-TF VWAP slots (3m/15m) remain at 0.0; composite (1.25) unchanged.
- **Rationale:** 1m LTF currently contributes to confluence via only two factors (`ltf_pattern` 0.75 + `ltf_momentum_alignment` 0.75). `vwap_1m_alignment` was zeroed in the scalp-native rewire because the composite was intended to carry the multi-TF VWAP signal. Re-opening it at a **low-weight probe value** (0.2) gives Phase 9 GBT a per-TF VWAP signal to evaluate independently of the composite — answers "is 1m VWAP directionality distinct alpha, or fully absorbed by composite?" on clean data.
- **Scoring impact:** ~0.2 point bump on bullish-aligned-to-1m-VWAP trades; trades near the `min_confluence_score=3` threshold may marginally increase. Not a pivot; no architectural change.
- **Dataset:** `rl.clean_since` bumped `2026-04-19T06:30:00Z → 2026-04-19T13:10:00Z` so post-change trades aren't mixed with pre-change scoring. Cost: 1 closed post-pivot trade (BTC WIN 12:56Z) falls out of clean window.
- **Re-evaluation:** after Phase 8 gate (50 clean trades + factor-audit), if `vwap_1m_alignment` shows positive SHAP / partial-dependence, raise toward 0.3; if flat or noisy, zero again. This is a probe, not a commitment.

### 2026-04-19 (eve) — ADA ↔ DOGE swap (demo OI cap)

- **Trigger:** post-restart demo run, ADA-USDT-SWAP every cycle rejected with OKX `sCode 54031` — "Order failed. The open interest of ADA-USDT-SWAP has reached the platform's limit." Post-only + limit fallback both hit the same cap. Other 4 pairs unaffected.
- **Root cause:** OKX demo instrument-level OI ceiling for ADA perp exhausted (demo pool much smaller than live). Not a sizing/margin/leverage issue — platform-side supply block.
- **Fix:** `config/default.yaml:17` — `ADA-USDT-SWAP` → `DOGE-USDT-SWAP` in `trading.symbols`. `max_concurrent_positions` stays at 5. DOGE per-symbol overrides (leverage cap 30x, swing_lookback 30, session=london, htf_sr_buffer_atr 0.15, min_sl_distance_pct 0.008, regime capitulation_liq_notional $8M) were already present in YAML from the 7→5 rollback — no new overrides needed.
- **ADA override retention:** ADA rows kept intact in YAML (per the "harmless when not watched" pattern). Reinstating ADA later = single-line flip in `trading.symbols` once OI headroom returns.
- **Operator action pre-restart:** bot stopped, all resting limit orders cancelled manually. Clean start on next launch.
- **Test impact:** none — symbol list change is pure config. Full suite 682/682 unchanged.

### 2026-04-19 (pm) — Unprotected-position hardening + 7→5 pair rollback

- **Trigger:** post-scalp-native demo pass surfaced 3 UNPROTECTED positions (BTC / DOGE / XRP). Log pattern: `pending_fill_algo_attach_failed_position_UNPROTECTED err=OrderRejected('place_algo_order: (no message)')` → OKX sCode 51277 (trigger already on wrong side of mark between fill and OCO attach).
- **Root causes diagnosed:**
  1. **Zone SL floor bypass** — `apply_zone_to_plan` overrode entry_signals' widened SL with a tighter structural SL (`zone.sl_beyond_zone` = sl_buffer_atr × ATR past zone edge); sub-floor stops got wicked out instantly.
  2. **Coinalyze 429 blocked the entire event loop** for up to 57s via `asyncio.sleep(retry_after)` in the shared request path → pending-poll + monitor + all symbol cycles stalled together.
  3. **Pending poll only ran once per `run_once`** (~180-240s per full cycle), so a fill could sit that long before `attach_algos` fired — long enough for mark to cross the SL trigger.
  4. **Single-balance-query 51008 race** with 7 concurrent zone entries competing for cross-margin free-margin.
  5. **Logger stripped `OrderRejected.code` / `.payload`** (`err={!r}`) — failures were unactionable.
- **Fixes applied:**
  - **Phase A (quick wins)**
    - `config/default.yaml` — `trading.symbols` 7→5 (dropped DOGE + XRP, kept BTC/ETH/SOL/ADA/BNB); `max_concurrent_positions` 7→5. Per-slot margin $714 → $1000 (+40%), cycle time comes in under 180s budget.
    - `src/bot/runner.py` — attach-algo CRITICAL log extended with `getattr(exc, 'code', None)` + `getattr(exc, 'payload', None)`.
  - **Phase B (safety)**
    - `src/strategy/setup_planner.py:apply_zone_to_plan` — new `min_sl_distance_pct` parameter; widens zone SL to per-symbol floor when structural SL lands inside it (mirrors `entry_signals.py` widening pattern, R stays flat via notional re-size). Call site `runner.py:1576` threads `cfg.min_sl_distance_pct_for(symbol)`.
    - `src/bot/runner.py:_handle_pending_filled` — pre-attach mark-vs-SL guard: `client.get_mark_price` before `attach_algos`; if mark already breached `plan.sl_price`, skip attach and best-effort `close_position`. Second `close_position` failure → CRITICAL log, manual intervention (emergency close not automated).
  - **Phase C (infra)**
    - `src/data/derivatives_api.py` — 429 no longer awaits `asyncio.sleep(retry_after)`. Sets `self._rate_pause_until = now + retry_after`, returns None immediately; subsequent requests short-circuit while the pause is active. Callers fall back to stale/None snapshots (their existing failure-isolation path).
    - `src/bot/runner.py:run_once` — `_process_pending()` now drains inline between symbols, not just once per tick. Fill → OCO-attach latency drops from ~180-240s to single-digit seconds.
- **Tests:** `tests/test_derivatives_api.py::test_request_retries_on_429_with_retry_after` rewritten → `test_request_429_short_circuits_without_blocking` (asserts no inline sleep, `_rate_pause_until` set, subsequent calls short-circuit). Full suite 682/682 still green.
- **Operator action pre-deploy:** manually closed the 3 UNPROTECTED positions + 5 resting limit orders (~$500 profit). No open positions / no pending orders on restart.
- **Re-tightening (future):** 7→5 is a mitigation, not a verdict on DOGE/XRP. After 50 post-fix closed trades, revisit adding them back; momentum wick + thin book were never the root cause — the attach race was.

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
