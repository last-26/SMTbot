# Trade Lifecycle — End-to-End Walkthrough

This document explains how the bot runs a single tick, reads indicator data, enters a trade, manages the open position, and exits — **end to end**. Code references use `file:line` format.

---

## 1. High-level architecture

```
┌────────────────┐    ┌─────────────────┐    ┌────────────────┐
│  TradingView   │ →  │  Python Bot     │ →  │      OKX       │
│  (eyes)        │    │  (brain)        │    │  (hands)       │
│  - Pine        │    │  - confluence   │    │  - market ord. │
│  - SMT Overlay │    │  - R:R sizing   │    │  - OCO algo    │
│  - SMT Osc.    │    │  - risk gates   │    │    SL/TP       │
└────────────────┘    └─────────────────┘    └────────────────┘
        ↑                      ↓
        └──── multi-TF cycle ──┘
```

- **Claude Code** is the orchestrator: writes Pine, trains RL, debugs. **Per-tick decisions** are made by the Python bot, not Claude.
- **TradingView** is the indicator engine: Pine Scripts re-compute on every bar and write results into `SMT Signals` + `SMT Oscillator` tables.
- **Python core** reads the data, scores confluence, computes R:R, and routes orders to OKX.
- **OKX** auto-manages the position via market entry + OCO algo (SL/TP).

---

## 2. What happens in one tick

`BotRunner.run_once()` (`src/bot/runner.py:483`) — runs once per `bot.poll_interval_seconds` (default 5s):

```
run_once()
├── _process_closes()                  # 1) drain closes first
└── for symbol in trading.symbols:     # 2) round-robin per symbol
        _run_one_symbol(symbol)
```

### Order matters:

1. **`_process_closes()` first** — asks OKX whether tracked positions have closed (`PositionMonitor.poll`), writes closes to journal, computes R, frees the slot.
2. **Round-robin symbol loop** — `BTC → ETH → SOL`. For each symbol: switch chart to it, read all timeframes, decide, place order if applicable.

A single symbol cycle (`_run_one_symbol`, `runner.py:677+`):

```
_run_one_symbol("BTC-USDT-SWAP"):

  ┌─ TV: chart → "OKX:BTCUSDT.P", sleep (symbol_settle_seconds)
  │
  ├─ HTF pass (15m):
  │     switch_timeframe(15m) → settle + freshness poll + post-grace
  │     refresh candles
  │     detect_sr_zones() → htf_sr_cache[symbol]
  │
  ├─ LTF pass (1m):
  │     switch_timeframe(1m) → settle + freshness poll + post-grace
  │     ltf_reader.read() → ltf_cache[symbol]   # oscillator-based
  │
  ├─ Entry pass (3m):
  │     switch_timeframe(3m) → settle + freshness poll + post-grace
  │     read_market_state()                    # Pine tables + drawings
  │     refresh entry-TF candles
  │     attach derivatives + liquidity_heatmap (best-effort)
  │
  ├─ Madde F: LTF reversal defensive close   ── (closes position if applicable, return)
  ├─ Symbol-level dedup (skip if already open)
  ├─ Sizing balance compute (total_eq + okx_avail)
  ├─ build_trade_plan_with_reason()           ── (None → NO_TRADE log + return)
  ├─ PLANNED log
  ├─ Reentry gate (Madde C)                    ── (block → return)
  ├─ Risk manager can_trade()                  ── (halt → return)
  ├─ router.place(plan)                       ── leverage + market + OCO
  ├─ monitor.register_open() + risk_mgr.register_trade_opened()
  └─ journal.record_open()
```

### Pine settle protection (waiting for indicator render across multi-TF switches)

`_switch_timeframe` (`runner.py:646`) on every TF change:

1. **Static wait** — `tf_settle_seconds` (default `3.5s`) — let Pine begin recomputing.
2. **Freshness poll** — `_wait_for_pine_settle` polls until `signal_table.last_bar` flips, every `pine_settle_poll_interval_s` (`0.3s`). Max `pine_settle_max_wait_s` (`10s`).
3. **Post-grace** (added 2026-04-17) — after the poll passes, sleep an extra `pine_post_settle_grace_s` (`1.0s`). Because when `last_bar` flips the **Oscillator table may still be rendering** (especially on 1m where `last_bar` ticks every wall-clock minute regardless of full re-render — that flip doesn't mean "tables are full").

Budget: 3 pairs × 4 TF switches × ~13s worst-case ≈ 152s. Still fits inside the 180s 3m-cycle budget.

---

## 3. Indicator layer (TradingView Pine)

Two Pine indicators are loaded on the chart. The bot **reads from their tables** (not their drawings — drawings are supplementary):

### `pine/smt_overlay.pine` → "SMT Signals" table (20 rows)

| Field | Content |
|---|---|
| `trend_htf`, `trend_ltf` | EMA bias direction |
| `structure` | HH/HL/LH/LL state |
| `last_mss` | Latest market structure shift (BOS/CHoCH) |
| `active_fvg`, `active_ob` | Whether a nearby active FVG/OB exists |
| `liquidity_above`, `liquidity_below` | Nearest liquidity levels |
| `last_sweep` | Direction of the latest liquidity sweep |
| `session` | London/NewYork/Asia/Off |
| `vmc_ribbon`, `vmc_wt_bias`, `vmc_wt_cross`, `vmc_last_signal`, `vmc_rsi_mfi` | VuManChu Cipher A components |
| `confluence` | Pine's internal 0–7 score (separate from Python's) |
| `atr_14`, `price`, `last_bar` | Live metrics + the table "freshness beacon" |

### `pine/smt_oscillator.pine` → "SMT Oscillator" table (15 rows)

| Field | Content |
|---|---|
| `wt1`, `wt2`, `wt_state`, `wt_cross` | WaveTrend main signal |
| `wt_vwap_fast` | VWAP-bias component |
| `rsi`, `rsi_mfi` | RSI + Money Flow Index combo |
| `stoch_k`, `stoch_d`, `stoch_state` | Stochastic RSI |
| `last_signal`, `last_wt_div` | Latest BUY/SELL signal + last divergence |
| `momentum` | Internal 0–5 momentum score |
| `last_bar` | Freshness beacon |

### Drawings (supplementary detail)

Pine also draws **labels** (MSS, sweep events), **boxes** (FVG, OB), and **lines** (liquidity, session levels). The bot reads them via `data labels/boxes/lines` and packs them into `MarketState`.

### `MarketState` (Pydantic)

`src/data/structured_reader.py:read_market_state()` pulls everything from Pine into a single dataclass:

```python
MarketState:
    current_price: float
    atr: float
    active_session: Session
    signal_table: SignalTableData     # SMT Overlay table
    oscillator: OscillatorTableData   # SMT Oscillator table
    mss_events: list[MSSEvent]
    fvg_zones: list[FVGZone]
    order_blocks: list[OrderBlock]
    liquidity_levels: list[LiquidityLevel]
    sweep_events: list[SweepEvent]
    session_levels: list[SessionLevel]
    derivatives: Optional[DerivativesSnapshot]   # Phase 1.5
    liquidity_heatmap: Optional[LiquidityHeatmap]  # Phase 1.5
```

---

## 4. Confluence scoring — direction + score

`src/analysis/multi_timeframe.py:calculate_confluence` scans `MarketState` after each TF pass and produces a list of factors + total score.

### Factor weights (`DEFAULT_WEIGHTS`)

| Factor | Weight | Trigger |
|---|---|---|
| `htf_trend_alignment` | 1.0 | HTF trend matches candidate direction |
| `mss_alignment` | 1.0 | Latest MSS supports candidate direction |
| `at_order_block` | 1.0 | Price inside / adjacent to active OB |
| `at_fvg` | 1.0 | Price inside active FVG |
| `at_sr_zone` | 0.75 | Adjacent to a Python S/R zone |
| `recent_sweep` | 1.0 | Latest sweep direction implies a reversal toward our side |
| `ltf_pattern` | 0.75 | Doji / hammer / engulfing etc. (Python price-action) |
| `oscillator_momentum` | 0.5 | Pine momentum score ≥ 4 and supports direction |
| `oscillator_signal` | 0.5 | Oscillator BUY/SELL is fresh and supports direction |
| `vmc_ribbon` | 0.5 | VMC ribbon color supports direction |
| `session_filter` | 0.25 | Active session is allowed (London / NY) |
| `ltf_momentum_alignment` | 0.5 | 1m oscillator trend/signal aligned with candidate |
| `derivatives_contrarian` | 0.7 | LONG_CROWDED + bearish candidate (or vice versa) |
| `derivatives_capitulation` | 0.6 | CAPITULATION regime + counter direction |
| `derivatives_heatmap_target` | 0.5 | Nearby liq cluster sits in the trade's path |
| `vwap_alignment` | 0.6 | Multi-TF VWAP stack supports direction |

**Important:** **At most one** of the derivatives factors fires from the elif chain. The same cycle cannot have both contrarian and capitulation active.

### Score + direction

`score_direction(state, BULLISH)` and `score_direction(state, BEARISH)` are computed separately. The higher one wins. Below `min_confluence_score` (default `2.0`) → no trade.

---

## 5. SL selection — structural-level priority order

`src/strategy/entry_signals.py:select_sl_price` (tries in order, first hit = SL):

1. **Pine OB** (`order_block_pine`) — closest valid Pine OB box drawn on the chart.
2. **Pine FVG** (`fvg_pine`).
3. **Python OB** (`order_block_py`) — when Python re-detects on its side.
4. **Python FVG** (`fvg_py`).
5. **Swing lookback** (`swing`) — extreme inside the last 20 candles.
6. **ATR fallback** (`atr_fallback`) — entry ± 2 × ATR.

Each level is pushed past with a **buffer**: `sl = level ± buffer_mult × ATR` (`buffer_mult = 0.2`). E.g. Pine OB top at 100, ATR=1, BULLISH → SL = `100 - 0.2 = 99.8` (when entry sits above).

### HTF S/R zones tighten the SL (Madde D)

`_push_sl_past_htf_zone` (`entry_signals.py:56`) — if a 15m S/R zone sits between SL and entry, snap SL just past the far edge of the zone. **Only tightens, never widens** (risk does not increase).

### Min SL distance floor — **widens** tight stops

`min_sl_distance_pct` (default `0.005` = `0.5%`):
- If SL distance < 0.5% → SL is **widened** (not rejected) to exactly 0.5%.
- Notional auto-shrinks (`risk_amount / sl_pct`) → R stays constant.
- Rationale: at high leverage, a 0.05–0.1% OB stop gets wicked out instantly. The 0.5% floor gives the fill breathing room; sizing shrinks so R is still as planned.

### Min TP distance floor — actual **reject**

`min_tp_distance_pct` (default `0.004` = `0.4%`):
- After applying the HTF TP ceiling, if TP distance < 0.4% → `tp_too_tight` reject.
- Rationale: a 3-fill partial-TP lifecycle burns `3 × 0.05% taker fee = 0.15%` plus slippage. The 0.4% floor is ~2× round-trip fees.

---

## 6. R:R + sizing — `calculate_trade_plan`

`src/strategy/rr_system.py:calculate_trade_plan` — pure math, no side effects.

### Steps

1. **Risk amount:** `risk_amount = account_balance × risk_pct`. (Default 1% → ~32 USDT = 1R on a 3200 USDT balance.)
2. **SL %:** `sl_pct = |entry - sl| / entry`.
3. **TP price:** `tp = entry ± (sl_distance × rr_ratio)`. (Default `rr_ratio = 3.0`.)
4. **Ideal notional:** `ideal_notional = risk_amount / sl_pct`.
5. **Required leverage:** `required_lev = ideal_notional / margin_balance`.
6. **Max-feasible leverage:** `feasible_lev = floor(_LIQ_SAFETY_FACTOR / sl_pct)` (`_LIQ_SAFETY_FACTOR = 0.6`). Caps leverage so SL sits within 60% of the liquidation distance — 40% buffer for maintenance + mark drift.
7. **Effective leverage:** `lev = min(max_leverage, max(ceil(required_lev), feasible_lev), 1)`.
8. **Margin safety:** `max_notional = margin_balance × lev × 0.95` (5% fee/mark buffer — prevents sCode 51008).
9. **Contract count:** `num_contracts = int(notional // (contract_size × entry))`. OKX requires integer contracts.
10. **Actual risk:** because of rounding, `actual_risk = num_contracts × contract_size × |entry - sl|` — may be **slightly less** than requested, never more.

### Risk vs margin split (Session 2 fix)

- **`account_balance`** → drives R (derived from total equity, scales naturally with drawdown).
- **`margin_balance`** → drives the leverage/notional ceiling (min of per-slot fair share and live `okx_avail`).

This split prevents one of three concurrent slots from hitting sCode 51008 just because peers locked margin (under `cross` mode).

### Per-symbol leverage cap

Effective ceiling = `min(trading.max_leverage, okx_instrument_cap, symbol_leverage_caps[sym])`.

E.g. ETH is capped at `30x` in YAML (demo wicks blow ≥30x even when SL discipline holds). BTC `75x`, SOL `50x` (OKX cap).

---

## 7. Reject reasons and risk gates

`build_trade_plan_with_reason` may return `(None, reason)` for any of these — the runner logs it as `NO_TRADE`:

| Reason | Meaning |
|---|---|
| `below_confluence` | Score below `min_confluence_score` |
| `session_filter` | Active session not in allowlist (Asia/Off) |
| `no_sl_source` | No SL source available |
| `crowded_skip` | Derivatives crowded gate (LONG_CROWDED + bullish + funding_z > 3.0) |
| `zero_contracts` | Sizing produced 0 contracts (notional too small) |
| `htf_tp_ceiling` | HTF zone shrank TP so much that the new RR < `min_rr_ratio` |
| `tp_too_tight` | TP distance below the fee floor |

Additional gates (after the plan is accepted):

### Reentry gate (Madde C, `runner.py:504`)

The last close per (symbol, side) is remembered (`LastCloseInfo`). 4 sequential gates:

1. **Cooldown** — `min_bars_after_close × entry_tf_seconds` not yet elapsed → `cooldown_3bars`.
2. **ATR move** — price hasn't moved `min_atr_move × ATR` (default `0.5×ATR`) from the last exit → `atr_move_insufficient`.
3. **Post-WIN quality** — last trade WIN → new confluence must be **strictly higher** → otherwise `post_win_needs_higher_confluence`.
4. **Post-LOSS quality** — last trade LOSS → new confluence must be **equal or higher** → otherwise `post_loss_needs_ge_confluence`.
5. **BREAKEVEN** bypasses the quality gate.

Opposite sides are isolated — closing a BTC long doesn't gate opening a BTC short.

### Risk manager (`risk_mgr.can_trade(plan)`)

`src/strategy/risk_manager.py` — circuit-breaker chain (first match wins):

1. Drawdown ≥ `max_drawdown_pct` (25%) → **permanent halt** (manual `--clear-halt` required).
2. `halted_until > now` → cooldown halt active.
3. Daily realized loss ≥ `max_daily_loss_pct` (15%) → 24h halt.
4. Consecutive losses ≥ `max_consecutive_losses` (5) → 24h halt.
5. Open positions ≥ `max_concurrent_positions` (3) → block.
6. Plan-level: leverage > max, RR < min, contracts == 0 → block.

---

## 8. Order placement — `OrderRouter.place()`

`src/execution/order_router.py:66`. Routes a single TradePlan to OKX:

### Steps

1. **Set leverage** — `set_leverage(inst, lever, mgnMode, posSide)`. Failure → `LeverageSetError`, no position is opened.
2. **Market entry** — `place_market_order(side, posSide, sz=plan.num_contracts)`. Failure → `OrderRejected` or `InsufficientMargin`.
3. **Algo (OCO or partial)** — see below.
4. Algo fail → if `close_on_algo_failure: true`, position is auto-closed + `AlgoOrderError` raised (position is never left without SL/TP).

### Partial TP mode (Madde E, default ON)

`partial_tp_enabled: true`, `partial_tp_ratio: 0.5`, `partial_tp_rr: 1.5`:

- **TP1 OCO** — `size = ceil(num_contracts × 0.5)`, `tpTriggerPx = entry ± (sl_distance × 1.5)`, `slTriggerPx = plan.sl_price`.
- **TP2 OCO** — `size = num_contracts - tp1_size`, `tpTriggerPx = plan.tp_price` (3R), `slTriggerPx = plan.sl_price`.
- Both algos go to OKX; if either fails, both are cancelled + position closed.
- Degenerate `num_contracts == 1` → single OCO fallback (partial isn't possible).

### Result

Returns `ExecutionReport(order=OrderResult, algos=[AlgoResult, AlgoResult])`. `algo_ids` are persisted via `monitor.register_open` + `journal`.

---

## 9. Open-position lifecycle — `PositionMonitor`

`src/execution/position_monitor.py`. No WS, REST poll. `monitor.poll()` is called once at the start of every `run_once`.

### Tracked state

```python
_Tracked:
    inst_id, pos_side, size, entry_price
    initial_size       # reference for partial detection
    algo_ids           # [tp1_algo, tp2_algo]
    tp2_price          # needed for the SL→BE replace
    be_already_moved   # idempotency
```

### Poll logic (`poll()`, `position_monitor.py:77`)

Every poll fetches live positions from OKX. For each tracked key:

1. **Not in live list** → position closed. Emit `CloseFill`, drop from tracked.
2. **In live list, size shrunk** → TP1 fill (partial). `_detect_tp1_and_move_sl` triggers:
   - Cancel the TP2 algo.
   - Place a new OCO: `SL = entry_price` (BE), `TP = tp2_price`, `size = remaining_size`.
   - Update `algo_ids`, set `be_already_moved = True`.
   - Fire `on_sl_moved` callback (so the journal's `algo_ids` column is updated).
3. **In live list, same size** → refresh (entry_price) and continue.

### LTF reversal defensive close (Madde F)

When a position is open, at the start of every entry pass (`runner.py:769+`):

- Position age is checked via `open_trade_opened_at` — if below `ltf_reversal_min_bars_in_position × entry_tf_seconds` (default `2 × 180s = 6m`) → skip (give a fresh position time to develop before reacting to a reversal signal).
- If `_is_ltf_reversal()` is true (1m oscillator trend + `last_signal` fresh against the open side):
  - `_defensive_close()` → cancel all tracked algos + market `close_position()`.
  - `pending_close_reasons[(sym,side)] = "ltf_reversal"` is set → close is journaled with `close_reason`.
  - Idempotency: `defensive_close_in_flight` prevents re-trigger.

### Close enrichment — real PnL (critical)

`PositionMonitor._close_fill_from` only knows "the position vanished" — `pnl_usdt = 0, exit_price = 0`. **Real PnL** comes from `OKXClient.enrich_close_fill`:

- Queries `/api/v5/account/positions-history` (last 24h).
- Extracts `realizedPnl`, `closeAvgPx`, `uTime`.
- Without it, **every close looks BREAKEVEN** and drawdown / consecutive losses **never trip**.

---

## 10. Close flow — `_handle_close`

`runner.py:1040`:

```python
async def _handle_close(fill):
    enriched = enrich_close_fill(fill)              # real PnL
    trade_id = open_trade_ids.pop(key, None)        # in-memory cleanup
    close_reason = pending_close_reasons.pop(key)   # Madde F tag
    defensive_close_in_flight.discard(key)
    open_trade_opened_at.pop(key)

    if trade_id is None:
        # orphan — still feed risk_mgr
        risk_mgr.register_trade_closed(...)
        return

    updated = await journal.record_close(trade_id, enriched, close_reason=close_reason)
    risk_mgr.register_trade_closed(TradeResult(pnl_usdt, pnl_r, timestamp))
    last_close[key] = LastCloseInfo(price, time, confluence, outcome)
```

### Journal `record_close` (`src/journal/database.py`)

- Fills `exit_price`, `pnl_usdt`, `closed_at`.
- Computes `pnl_r = pnl_usdt / risk_amount_usdt`.
- `outcome` from PnL sign: `> 0 → WIN`, `< 0 → LOSS`, `== 0 → BREAKEVEN`.
- Writes `close_reason` (e.g. `ltf_reversal`).

### Risk manager update

- `current_balance += pnl_usdt`.
- `peak_balance` updated → `drawdown_pct` recomputed.
- `daily_realized_pnl += pnl_usdt`.
- WIN → `consecutive_losses = 0`; LOSS → `+= 1`.
- If a threshold tripped, `halted_until` is set.

### Reentry gate state

`last_close[(symbol, side)]` is updated → next same-direction reentry uses it via the gate.

---

## 11. Failure isolation — what affects what

| Failure | Effect |
|---|---|
| TV bridge timeout | Skip that symbol cycle, others continue |
| Pine settle timeout | Skip that symbol cycle |
| Coinalyze 401/429 | `state.derivatives = None`, derivatives factors disabled, price-structure entries continue |
| Binance WS disconnect | Auto-reconnect (exponential backoff), heatmap historical layer missing |
| `set_leverage` fail | No position opened, `LeverageSetError` log |
| `place_market_order` fail | No position, `OrderRejected` log |
| Algo fail | Position **auto-closed** (`close_on_algo_failure: true`), `AlgoOrderError` log |
| `journal.record_open` fail | **Position is still live** (orphan) — `_reconcile_orphans` logs at next startup, operator decides |
| `journal.record_close` fail | Risk manager still fed (state stays in sync), journal row update lost |
| `enrich_close_fill` fail | Raw fill used (`pnl_usdt = 0`) — drawdown/streak accounting lost, **watch out** |

---

## 12. Restart behavior

`BotRunner._prime()` (`runner.py:962`):

1. **`journal.replay_for_risk_manager`** — walks closed trades in entry order to rebuild `risk_mgr.peak_balance`, `consecutive_losses`, `current_balance` from durable truth.
2. **`_apply_clear_halt`** (only with `--clear-halt` flag) — resets halt + daily counters + peak.
3. **`_rehydrate_open_positions`** — loads OPEN journal rows into `monitor._tracked` and `open_trade_ids`. OCO `algo_ids` and `tp2_price` are preserved.
4. **`_reconcile_orphans`** — diffs live OKX positions ↔ journal OPEN rows. **Logs only**, no auto-action.
5. **`_load_contract_sizes`** — fetches `ctVal` + `max_leverage` per symbol from OKX (per-symbol cap).

OCO algos on the OKX side stay active even while the bot is down, so positions never lose SL/TP protection.

---

## 13. CLI usage

```bash
# Smoke test — full pipeline, single tick, no real orders
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo (real orders, OKX demo account)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Reset a halt that may have tripped
.venv/Scripts/python.exe -m src.bot --clear-halt --config config/default.yaml

# Auto-stop after 50 closed trades (Phase 7 data-collection threshold)
.venv/Scripts/python.exe -m src.bot --max-closed-trades 50

# Derivatives data collection only (no entry/exit)
.venv/Scripts/python.exe -m src.bot --derivatives-only --duration 600

# Report
.venv/Scripts/python.exe scripts/report.py --last 7d
```

---

## 14. Logging — where to look for what

### Decision logs (`scripts/logs.py --decisions`)

```
symbol_cycle_start symbol=BTC-USDT-SWAP
symbol_decision symbol=BTC-USDT-SWAP NO_TRADE reason=below_confluence price=64500.0 session=LONDON direction=BULLISH confluence=1.50/2.0 factors=...
symbol_decision symbol=ETH-USDT-SWAP PLANNED direction=BEARISH entry=3245.5 sl=3260.0 tp=3220.0 rr=3.00 confluence=4.50 contracts=10 notional=32450.0 lev=20x margin=1622.5 risk=32.0 risk_bal=3200.0 margin_bal=1066.7 factors=...
opened BEARISH ETH-USDT-SWAP 10c @ 3245.5 trade_id=xxx
sl_moved_to_be_via_replace inst=ETH-USDT-SWAP side=short remaining_size=5.0 new_algo=...
closed trade_id=xxx outcome=WIN pnl_r=2.85
```

### Reject reasoning

`reentry_blocked symbol=… side=… reason=cooldown_3bars` — shows which gate cut the entry.

`blocked symbol=… reason=…` — risk_mgr halt or breaker.

### Errors

`order_rejected … sCode=51008 …` — insufficient margin (per-slot sizing tripped or live OKX shortfall).

`htf_settle_timeout symbol=…` — Pine `last_bar` didn't flip within 10s after TF switch → that symbol cycle skipped.

`SMT Signals table not found — using empty state` — Pine hasn't rendered yet during the settle poll (expected; runner handles gracefully).

`orphan_close key=…` — closed position has no OPEN row in journal (typical after bot crash / `--max-closed-trades` exit).

`journal_open_but_no_live_position key=…` — restart-time diff: journal OPEN but missing on OKX (may have been closed manually; journal stale).

---

## 15. Pre-Phase 7 status

- **Threshold:** ≥50 closed trades.
- **Current:** ~19 closed (W=3 / L=15 / BE=1), 2 open.
- **Strategy parameters** that Phase 7 will tune via RL: `confluence_threshold`, `pattern_weights`, `min_rr_ratio`, `risk_pct`, `volatility_scale`, `ob_vs_fvg_preference`.
- **Reward shape:** `pnl_r + setup_penalty + dd_penalty + consistency_bonus`.
- **Walk-forward is mandatory:** parameters that don't improve OOS are never deployed.

---

## Quick-reference table

| Task | File | Function |
|---|---|---|
| One tick | `src/bot/runner.py` | `BotRunner.run_once` |
| Single symbol cycle | `src/bot/runner.py` | `_run_one_symbol` |
| TF switch + settle | `src/bot/runner.py` | `_switch_timeframe`, `_wait_for_pine_settle` |
| Pine data read | `src/data/structured_reader.py` | `read_market_state` |
| Confluence score | `src/analysis/multi_timeframe.py` | `calculate_confluence`, `score_direction` |
| Plan build | `src/strategy/entry_signals.py` | `build_trade_plan_with_reason` |
| SL select | `src/strategy/entry_signals.py` | `select_sl_price` |
| R:R sizing | `src/strategy/rr_system.py` | `calculate_trade_plan` |
| Reentry gate | `src/bot/runner.py` | `_check_reentry_gate` |
| LTF reversal close | `src/bot/runner.py` | `_is_ltf_reversal`, `_defensive_close` |
| Order placement | `src/execution/order_router.py` | `OrderRouter.place`, `_place_algos` |
| Position tracking | `src/execution/position_monitor.py` | `PositionMonitor.poll`, `_detect_tp1_and_move_sl` |
| Close handling | `src/bot/runner.py` | `_handle_close` |
| Journal CRUD | `src/journal/database.py` | `record_open`, `record_close`, `replay_for_risk_manager` |
| Circuit breakers | `src/strategy/risk_manager.py` | `RiskManager.can_trade`, `register_trade_closed` |
