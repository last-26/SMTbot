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
