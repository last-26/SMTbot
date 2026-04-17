# Trade Lifecycle — İşlem Akışı Detaylı

Bu doküman bot'un bir tick'i nasıl koştuğunu, indikatör verilerini nasıl okuduğunu, işleme nasıl girdiğini, açık pozisyonu nasıl yönettiğini ve nasıl kapattığını **uçtan uca** anlatır. Kod referansları `dosya:satır` formatında.

---

## 1. Yüksek seviye mimari

```
┌────────────────┐    ┌─────────────────┐    ┌────────────────┐
│  TradingView   │ →  │  Python Bot     │ →  │      OKX       │
│  (gözler)      │    │  (beyin)        │    │  (eller)       │
│  - Pine        │    │  - confluence   │    │  - market emir │
│  - SMT Overlay │    │  - R:R sizing   │    │  - OCO algo SL/│
│  - SMT Osc.    │    │  - risk gates   │    │    TP          │
└────────────────┘    └─────────────────┘    └────────────────┘
        ↑                      ↓
        └──── multi-TF cycle ──┘
```

- **Claude Code** orkestratör: Pine'ı yazar, RL eğitir, debug eder. **Per-tick karar** Claude değil Python bot'u verir.
- **TradingView** indikatör motoru: Pine Script'ler her bar'da yeniden hesaplanır, sonuçları `SMT Signals` + `SMT Oscillator` tablolarına yazar.
- **Python core** verileri okur, confluence skorlar, R:R hesaplar, OKX'e emir atar.
- **OKX** market entry + OCO algo (SL/TP) ile pozisyonu otomatik yönetir.

---

## 2. Bir tick'te ne olur

`BotRunner.run_once()` (`src/bot/runner.py:483`) — her `bot.poll_interval_seconds` (default 5s) bir kez çalışır:

```
run_once()
├── _process_closes()                  # 1) önce kapanışları drenajla
└── for symbol in trading.symbols:     # 2) her parite için round-robin
        _run_one_symbol(symbol)
```

### Sıra çok önemli:

1. **`_process_closes()` önce** — açılmış pozisyonların kapanmış olup olmadığını OKX'e sorar (`PositionMonitor.poll`), kapananları journal'a yazar, R hesaplar, slot'u serbest bırakır.
2. **Round-robin sembol döngüsü** — `BTC → ETH → SOL`. Her sembol için chart'ı o sembole çevir, tüm TF'leri oku, karar ver, gerekirse emir at.

Bir sembolün tek bir cycle'ı şöyle ilerler (`_run_one_symbol`, `runner.py:677+`):

```
_run_one_symbol("BTC-USDT-SWAP"):

  ┌─ TV: chart → "OKX:BTCUSDT.P", uyu (symbol_settle_seconds)
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
  │     read_market_state()                    # Pine tabloları + drawings
  │     refresh entry-TF candles
  │     attach derivatives + liquidity_heatmap (best-effort)
  │
  ├─ Madde F: LTF reversal defensive close   ── (varsa pozisyonu kapatır, return)
  ├─ Symbol-level dedup (zaten açık varsa skip)
  ├─ Sizing balance hesabı (total_eq + okx_avail)
  ├─ build_trade_plan_with_reason()           ── (None ise NO_TRADE log + return)
  ├─ PLANNED log
  ├─ Reentry gate (Madde C)                    ── (block ise return)
  ├─ Risk manager can_trade()                  ── (halt ise return)
  ├─ router.place(plan)                       ── leverage + market + OCO
  ├─ monitor.register_open() + risk_mgr.register_trade_opened()
  └─ journal.record_open()
```

### Pine settle koruması (multi-TF geçişlerinde indikatör render bekleme)

`_switch_timeframe` (`runner.py:646`) her TF değişiminde:

1. **Statik bekleme** — `tf_settle_seconds` (default `3.5s`) — Pine'ın yeniden hesaplaması başlasın.
2. **Freshness poll** — `_wait_for_pine_settle` `signal_table.last_bar` flip edene kadar `pine_settle_poll_interval_s` (`0.3s`) ile yokla. Maks `pine_settle_max_wait_s` (`10s`).
3. **Post-grace** (yeni eklendi, 2026-04-17) — poll geçtikten sonra `pine_post_settle_grace_s` (`1.0s`) daha bekle. Çünkü `last_bar` flip ettiğinde **Oscillator tablosu hâlâ render olmuş olabilir** (özellikle 1m'de `last_bar` her dakika otomatik tikler, o flip "tablolar dolu" anlamına gelmez).

Bütçe: 3 parite × 4 TF switch × ~13s worst-case ≈ 152s. 3m cycle'ının 180s bütçesi içinde rahat sığar.

---

## 3. İndikatör katmanı (TradingView Pine)

İki Pine indikatörü chart'a yüklü, bot **tablolarından okur** (drawing'lerden değil — drawing'ler ek detay için):

### `pine/smt_overlay.pine` → "SMT Signals" tablosu (20 satır)

| Field | İçerik |
|---|---|
| `trend_htf`, `trend_ltf` | EMA bias yönü |
| `structure` | HH/HL/LH/LL durumu |
| `last_mss` | Son market structure shift (BOS/CHoCH) |
| `active_fvg`, `active_ob` | Yakın aktif FVG/OB var mı |
| `liquidity_above`, `liquidity_below` | Yakın likidite seviyeleri |
| `last_sweep` | Son liquidity sweep yönü |
| `session` | London/NewYork/Asia/Off |
| `vmc_ribbon`, `vmc_wt_bias`, `vmc_wt_cross`, `vmc_last_signal`, `vmc_rsi_mfi` | VuManChu Cipher A bileşenleri |
| `confluence` | 0-7 arası Pine'ın iç skoru (Python'unkinden ayrı) |
| `atr_14`, `price`, `last_bar` | Anlık metrics + tablo "tazelik beacon"u |

### `pine/smt_oscillator.pine` → "SMT Oscillator" tablosu (15 satır)

| Field | İçerik |
|---|---|
| `wt1`, `wt2`, `wt_state`, `wt_cross` | WaveTrend ana sinyal |
| `wt_vwap_fast` | VWAP-bias komponent |
| `rsi`, `rsi_mfi` | RSI + Money Flow Index karması |
| `stoch_k`, `stoch_d`, `stoch_state` | Stochastic RSI |
| `last_signal`, `last_wt_div` | Son BUY/SELL sinyali + son divergence |
| `momentum` | 0-5 arası iç momentum skoru |
| `last_bar` | Tazelik beacon'ı |

### Drawings (ek detay)

Pine ayrıca **labels** (MSS, sweep events), **boxes** (FVG, OB), **lines** (liquidity, session levels) çizer — bot bunları da `data labels/boxes/lines` ile okur ve `MarketState` içine paketler.

### `MarketState` (Pydantic)

`src/data/structured_reader.py:read_market_state()` Pine'dan tüm bunları çekip tek bir dataclass'a paketler:

```python
MarketState:
    current_price: float
    atr: float
    active_session: Session
    signal_table: SignalTableData     # SMT Overlay tablosu
    oscillator: OscillatorTableData   # SMT Oscillator tablosu
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

## 4. Confluence skorlama — yön + skor üretimi

`src/analysis/multi_timeframe.py:calculate_confluence` her TF passed sonrası `MarketState`'i tarar ve faktör listesi + toplam skor üretir.

### Faktör ağırlıkları (`DEFAULT_WEIGHTS`)

| Faktör | Ağırlık | Tetikleyici |
|---|---|---|
| `htf_trend_alignment` | 1.0 | HTF trend yönü ile aday yön aynı |
| `mss_alignment` | 1.0 | Son MSS aday yönü destekliyor |
| `at_order_block` | 1.0 | Fiyat aktif OB içinde / bitişiğinde |
| `at_fvg` | 1.0 | Fiyat aktif FVG içinde |
| `at_sr_zone` | 0.75 | Python S/R zone bitişiğinde |
| `recent_sweep` | 1.0 | Son sweep yönü reverse sinyal veriyor |
| `ltf_pattern` | 0.75 | Doji/hammer/engulfing vs. (Python price-action) |
| `oscillator_momentum` | 0.5 | Pine momentum skoru ≥ 4 ve aday yönü destekliyor |
| `oscillator_signal` | 0.5 | Oscillator BUY/SELL fresh ve aday yönü destekliyor |
| `vmc_ribbon` | 0.5 | VMC ribbon rengi aday yönü destekliyor |
| `session_filter` | 0.25 | Aktif session izinli (London / NY) |
| `ltf_momentum_alignment` | 0.5 | 1m oscillator trend/sinyal aday yön ile uyumlu |
| `derivatives_contrarian` | 0.7 | LONG_CROWDED + bearish aday (veya tersi) |
| `derivatives_capitulation` | 0.6 | CAPITULATION rejimi + tersi yön |
| `derivatives_heatmap_target` | 0.5 | Yakın liq cluster aday yönde duruyor |
| `vwap_alignment` | 0.6 | Multi-TF VWAP stack aday yönü destekliyor |

**Önemli:** Derivatives faktörlerinden **en fazla biri** elif zincirinden tetiklenir. Aynı cycle'da hem contrarian hem capitulation aktif olamaz.

### Skor + yön

`score_direction(state, BULLISH)` ve `score_direction(state, BEARISH)` ayrı ayrı hesaplanır. Yüksek olan kazanır. `min_confluence_score` (default `2.0`) eşiği altındaysa → trade yok.

---

## 5. SL seçimi — yapısal seviye öncelik sırası

`src/strategy/entry_signals.py:select_sl_price` (sırayla dener, ilk başarılı = SL):

1. **Pine OB** (`order_block_pine`) — chart'ta çizili Pine OB box'larından entry'e en yakın geçerli olan.
2. **Pine FVG** (`fvg_pine`).
3. **Python OB** (`order_block_py`) — Python tarafı yeniden tespit ettiyse.
4. **Python FVG** (`fvg_py`).
5. **Swing lookback** (`swing`) — son 20 mum içindeki extreme.
6. **ATR fallback** (`atr_fallback`) — entry ± 2 × ATR.

Her durum **buffer** ile itilir: `sl = level ± buffer_mult × ATR` (`buffer_mult = 0.2`). Yani Pine OB üst kenarı 100, ATR=1, BULLISH için → SL = `100 - 0.2 = 99.8` (entry üstündeyse).

### HTF S/R zone'ları SL'i sıkıştırır (Madde D)

`_push_sl_past_htf_zone` (`entry_signals.py:56`) — 15m S/R zone'u SL ile entry arasında duruyorsa SL'i o zone'un dışına çek. **Sadece sıkıştırır, asla genişletmez** (risk artmaz).

### Min SL distance floor — sıkı stop'ları **genişletir**

`min_sl_distance_pct` (default `0.005` = `0.5%`):
- SL distance %0.5'in altındaysa → SL **genişletilir** (rejected değil) tam %0.5'e.
- Notional otomatik küçülür (`risk_amount / sl_pct`) → R sabit kalır.
- Mantık: Yüksek leverage'da %0.05-0.1'lik OB stop'ları anında wick edilir; %0.5 floor wick'e nefes açar, sizing küçüldüğü için R hâlâ planlanan kadardır.

### Min TP distance floor — gerçek **reject**

`min_tp_distance_pct` (default `0.004` = `0.4%`):
- HTF TP ceiling uygulandıktan sonra TP distance %0.4'ün altındaysa → `tp_too_tight` reject.
- Mantık: 3-fill partial-TP lifecycle'ı `3 × 0.05% taker fee` = `0.15%` artı slippage harcar. %0.4 minimum ~2× round-trip fee'ye karşılık gelir.

---

## 6. R:R + sizing — `calculate_trade_plan`

`src/strategy/rr_system.py:calculate_trade_plan` — saf math, side-effect yok.

### Adımlar

1. **Risk amount:** `risk_amount = account_balance × risk_pct`. (Default %1 → 3200 USDT bakiyede ~32 USDT = 1R.)
2. **SL %:** `sl_pct = |entry - sl| / entry`.
3. **TP fiyatı:** `tp = entry ± (sl_distance × rr_ratio)`. (Default `rr_ratio = 3.0`.)
4. **İdeal notional:** `ideal_notional = risk_amount / sl_pct`.
5. **Required leverage:** `required_lev = ideal_notional / margin_balance`.
6. **Max-feasible leverage:** `feasible_lev = floor(_LIQ_SAFETY_FACTOR / sl_pct)` (`_LIQ_SAFETY_FACTOR = 0.6`). Leverage'ı liquidation distance'ın %60'ından öteye çıkarmaz — %40 buffer maintenance + mark drift için.
7. **Effective leverage:** `lev = min(max_leverage, max(ceil(required_lev), feasible_lev), 1)`.
8. **Margin safety:** `max_notional = margin_balance × lev × 0.95` (5% fee/mark buffer — sCode 51008'i engeller).
9. **Contract count:** `num_contracts = int(notional // (contract_size × entry))`. OKX integer contract istiyor.
10. **Actual risk:** Yuvarlandığı için `actual_risk = num_contracts × contract_size × |entry - sl|` — istenenden **biraz az** olabilir, asla fazla.

### Risk vs margin ayrımı (Session 2 düzeltmesi)

- **`account_balance`** → R hesaplamak için (total equity'den gelir, drawdown ile orantılı küçülür).
- **`margin_balance`** → leverage/notional ceiling için (per-slot fair share + okx_avail'in min'i).

Bu ayrım `cross` margin modunda 3 slot'un eş zamanlı dolup birinin diğerini sCode 51008 ile çarpmasını engelliyor.

### Per-symbol leverage cap

Effective ceiling = `min(trading.max_leverage, okx_instrument_cap, symbol_leverage_caps[sym])`.

Mesela ETH için YAML'da `30x` cap var (demo wick'leri yüksek leverage'da SL discipline tutsa bile likide eder). BTC `75x`, SOL `50x` (OKX cap).

---

## 7. Reject sebepleri ve risk gates

`build_trade_plan_with_reason` aşağıdaki sebeplerden biriyle `(None, reason)` dönebilir; runner bu reason'ı `NO_TRADE` log'una basar:

| Reason | Anlam |
|---|---|
| `below_confluence` | Skor `min_confluence_score` altında |
| `session_filter` | Aktif session izinli değil (Asia/Off) |
| `no_sl_source` | Hiçbir SL kaynağı bulunamadı |
| `crowded_skip` | Derivatives crowded gate (LONG_CROWDED + bullish + funding_z > 3.0) |
| `zero_contracts` | Sizing 0 contract verdi (notional çok küçük) |
| `htf_tp_ceiling` | HTF zone TP'yi öyle kıstı ki yeni RR `min_rr_ratio` altında |
| `tp_too_tight` | TP distance fee floor'un altında |

Ek gates (plan kabul edildikten sonra):

### Reentry gate (Madde C, `runner.py:504`)

Aynı sembol+yön için son kapanan trade hatırlanır (`LastCloseInfo`). 4 sıralı gate:

1. **Cooldown** — `min_bars_after_close × entry_tf_seconds` geçmemişse → `cooldown_3bars`.
2. **ATR move** — fiyat son exit'ten `min_atr_move × ATR` (default `0.5×ATR`) hareket etmemişse → `atr_move_insufficient`.
3. **Post-WIN quality** — son trade WIN ise yeni confluence **strictly higher** olmalı → yoksa `post_win_needs_higher_confluence`.
4. **Post-LOSS quality** — son trade LOSS ise yeni confluence **eşit veya yüksek** olmalı → yoksa `post_loss_needs_ge_confluence`.
5. **BREAKEVEN** quality gate'i bypass eder.

Karşı yönler izole — BTC long kapandıktan sonra BTC short açmak gate'e takılmaz.

### Risk manager (`risk_mgr.can_trade(plan)`)

`src/strategy/risk_manager.py` — circuit breaker zinciri (ilk match wins):

1. Drawdown ≥ `max_drawdown_pct` (25%) → **kalıcı halt** (manuel `--clear-halt` lazım).
2. `halted_until > now` → cooldown halt geçerli.
3. Daily realized loss ≥ `max_daily_loss_pct` (15%) → 24h halt.
4. Consecutive losses ≥ `max_consecutive_losses` (5) → 24h halt.
5. Open positions ≥ `max_concurrent_positions` (3) → block.
6. Plan-level: leverage > max, RR < min, contracts == 0 → block.

---

## 8. Order placement — `OrderRouter.place()`

`src/execution/order_router.py:66`. Tek bir TradePlan'ı OKX'e götürür:

### Adımlar

1. **Set leverage** — `set_leverage(inst, lever, mgnMode, posSide)`. Hata → `LeverageSetError`, hiçbir pozisyon açılmaz.
2. **Market entry** — `place_market_order(side, posSide, sz=plan.num_contracts)`. Hata → `OrderRejected` veya `InsufficientMargin`.
3. **Algo (OCO veya partial)** — aşağıda.
4. Algo başarısız → `close_on_algo_failure: true` ise pozisyon hemen kapatılır + `AlgoOrderError` raise (pozisyon asla SL/TP'siz kalmaz).

### Partial TP modu (Madde E, default ON)

`partial_tp_enabled: true`, `partial_tp_ratio: 0.5`, `partial_tp_rr: 1.5`:

- **TP1 OCO** — `size = ceil(num_contracts × 0.5)`, `tpTriggerPx = entry ± (sl_distance × 1.5)`, `slTriggerPx = plan.sl_price`.
- **TP2 OCO** — `size = num_contracts - tp1_size`, `tpTriggerPx = plan.tp_price` (3R), `slTriggerPx = plan.sl_price`.
- Her iki algo da OKX'e gider; ikisinden biri fail ederse her ikisi de cancel + position close.
- Degenerate `num_contracts == 1` → tek OCO fallback (partial yapılamaz).

### Sonuç

`ExecutionReport(order=OrderResult, algos=[AlgoResult, AlgoResult])` döner. `algo_ids` `monitor.register_open` + `journal` ile birlikte kaydedilir.

---

## 9. Açık pozisyon yaşam döngüsü — `PositionMonitor`

`src/execution/position_monitor.py`. WS yok, REST poll. Her `run_once` başında bir kez `monitor.poll()` çağrılır.

### Tracked state

```python
_Tracked:
    inst_id, pos_side, size, entry_price
    initial_size       # partial detection için referans
    algo_ids           # [tp1_algo, tp2_algo]
    tp2_price          # SL→BE replace'inde lazım
    be_already_moved   # idempotency
```

### Poll mantığı (`poll()`, `position_monitor.py:77`)

Her poll'da OKX'ten canlı pozisyonlar çekilir. Her tracked key için:

1. **Canlı listede yok** → pozisyon kapanmış. `CloseFill` üret, tracked'den sil.
2. **Canlı listede var ama size küçülmüş** → TP1 fill (partial). `_detect_tp1_and_move_sl` tetiklenir:
   - TP2 algo'sunu cancel et.
   - Yeni OCO yerleştir: `SL = entry_price` (BE), `TP = tp2_price`, `size = remaining_size`.
   - `algo_ids` güncellenir, `be_already_moved = True`.
   - `on_sl_moved` callback (journal'da `algo_ids` kolonunu update etmek için).
3. **Canlı listede var, size aynı** → güncelle (entry_price refresh) ve geç.

### LTF reversal defensive close (Madde F)

Açık pozisyon varken, her cycle'da entry pass başında (`runner.py:769+`):

- `open_trade_opened_at` ile pozisyonun yaşı kontrol edilir → `ltf_reversal_min_bars_in_position × entry_tf_seconds` (default `2 × 180s = 6dk`) altındaysa → atla (yeni pozisyona reversal sinyali için zaman tanı).
- `_is_ltf_reversal()` true ise (1m oscillator trend + `last_signal` açık yönün tersine fresh):
  - `_defensive_close()` → tüm tracked algo'ları cancel + `close_position()` market.
  - `pending_close_reasons[(sym,side)] = "ltf_reversal"` set edilir → kapanış journal'da `close_reason` olarak işlenir.
  - Idempotency: `defensive_close_in_flight` set'i tekrar tetiklemeyi engeller.

### Close enrichment — gerçek PnL (kritik)

`PositionMonitor._close_fill_from` sadece "pozisyon yok oldu" bilgisi döner — `pnl_usdt = 0, exit_price = 0`. **Gerçek PnL** `OKXClient.enrich_close_fill` ile alınır:

- `/api/v5/account/positions-history` endpoint'i sorgulanır (last 24h).
- `realizedPnl`, `closeAvgPx`, `uTime` çekilir.
- Bu olmazsa **her kapanış BREAKEVEN görünür** ve drawdown / consecutive losses **asla tetiklenmez**.

---

## 10. Kapanış akışı — `_handle_close`

`runner.py:1040`:

```python
async def _handle_close(fill):
    enriched = enrich_close_fill(fill)              # gerçek PnL
    trade_id = open_trade_ids.pop(key, None)        # in-memory cleanup
    close_reason = pending_close_reasons.pop(key)   # Madde F tag
    defensive_close_in_flight.discard(key)
    open_trade_opened_at.pop(key)

    if trade_id is None:
        # orphan — risk_mgr'ı yine de besle
        risk_mgr.register_trade_closed(...)
        return

    updated = await journal.record_close(trade_id, enriched, close_reason=close_reason)
    risk_mgr.register_trade_closed(TradeResult(pnl_usdt, pnl_r, timestamp))
    last_close[key] = LastCloseInfo(price, time, confluence, outcome)
```

### Journal `record_close` (`src/journal/database.py`)

- `exit_price`, `pnl_usdt`, `closed_at` doldurulur.
- `pnl_r = pnl_usdt / risk_amount_usdt` hesaplanır.
- `outcome` = pnl sign'ına göre: `> 0 → WIN`, `< 0 → LOSS`, `== 0 → BREAKEVEN`.
- `close_reason` (varsa: `ltf_reversal`) yazılır.

### Risk manager update

- `current_balance += pnl_usdt`.
- `peak_balance` güncellenir → `drawdown_pct` yeniden hesaplanır.
- `daily_realized_pnl += pnl_usdt`.
- WIN ise `consecutive_losses = 0`; LOSS ise `+= 1`.
- Eşik aşıldıysa `halted_until` set edilir.

### Reentry gate state

`last_close[(symbol, side)]` güncellenir → bir sonraki aynı yön reentry'sinde gate bu bilgiyi kullanır.

---

## 11. Failure isolation — neyin neyi etkilediği

| Hata | Etki |
|---|---|
| TV bridge timeout | O sembol cycle'ı skip, diğerleri normal |
| Pine settle timeout | O sembol cycle'ı skip |
| Coinalyze 401/429 | `state.derivatives = None`, derivatives faktörleri devre dışı, fiyat-yapısı entry'leri devam |
| Binance WS disconnect | Auto-reconnect (exponential backoff), heatmap historical layer eksik |
| `set_leverage` fail | Hiçbir pozisyon açılmaz, `LeverageSetError` log |
| `place_market_order` fail | Hiçbir pozisyon yok, `OrderRejected` log |
| Algo fail | Pozisyon **otomatik kapatılır** (`close_on_algo_failure: true`), `AlgoOrderError` log |
| `journal.record_open` fail | **Pozisyon yine de live** (orphan) — restart'ta `_reconcile_orphans` log basar, operatör karar verir |
| `journal.record_close` fail | Risk manager yine besleniyor (state senkron kalır), journal row update'i kayıp |
| `enrich_close_fill` fail | Raw fill kullanılır (`pnl_usdt = 0`) — drawdown/streak hesabı kayıp, **dikkat** |

---

## 12. Restart davranışı

`BotRunner._prime()` (`runner.py:962`):

1. **`journal.replay_for_risk_manager`** — kapalı trade'leri sırayla okuyup `risk_mgr.peak_balance`, `consecutive_losses`, `current_balance`'ı sıfırdan rebuild eder.
2. **`_apply_clear_halt`** (sadece `--clear-halt` flag'i ile) — halt + daily counters + peak'i reset.
3. **`_rehydrate_open_positions`** — journal'da OPEN olan trade'leri `monitor._tracked` ve `open_trade_ids`'e geri yükler. OCO algo_ids ve tp2_price korunur.
4. **`_reconcile_orphans`** — canlı OKX pozisyonları ↔ journal OPEN row'lar diff'i. Sadece **log basar**, otomatik aksiyon yok.
5. **`_load_contract_sizes`** — her sembol için OKX'ten `ctVal` + `max_leverage` çeker (per-symbol cap).

OKX tarafında OCO algo'lar bot kapalıyken de aktif olduğu için pozisyonlar SL/TP korumasız kalmaz.

---

## 13. CLI kullanımı

```bash
# Smoke test — full pipeline, tek tick, gerçek emir yok
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo (gerçek emir, OKX demo hesabı)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Halt tetiklemiş olabilir, sıfırla:
.venv/Scripts/python.exe -m src.bot --clear-halt --config config/default.yaml

# 50 trade'de auto-stop (Phase 7 veri toplama eşiği)
.venv/Scripts/python.exe -m src.bot --max-closed-trades 50

# Sadece derivatives veri toplama (entry/exit yok)
.venv/Scripts/python.exe -m src.bot --derivatives-only --duration 600

# Rapor
.venv/Scripts/python.exe scripts/report.py --last 7d
```

---

## 14. Loglama — neyi nerede aramalı

### Karar logları (`scripts/logs.py --decisions`)

```
symbol_cycle_start symbol=BTC-USDT-SWAP
symbol_decision symbol=BTC-USDT-SWAP NO_TRADE reason=below_confluence price=64500.0 session=LONDON direction=BULLISH confluence=1.50/2.0 factors=...
symbol_decision symbol=ETH-USDT-SWAP PLANNED direction=BEARISH entry=3245.5 sl=3260.0 tp=3220.0 rr=3.00 confluence=4.50 contracts=10 notional=32450.0 lev=20x margin=1622.5 risk=32.0 risk_bal=3200.0 margin_bal=1066.7 factors=...
opened BEARISH ETH-USDT-SWAP 10c @ 3245.5 trade_id=xxx
sl_moved_to_be_via_replace inst=ETH-USDT-SWAP side=short remaining_size=5.0 new_algo=...
closed trade_id=xxx outcome=WIN pnl_r=2.85
```

### Reject mantığı

`reentry_blocked symbol=… side=… reason=cooldown_3bars` — gate hangi sebeple kestiyse onu görürsün.

`blocked symbol=… reason=…` — risk_mgr halt veya breaker.

### Hatalar

`order_rejected … sCode=51008 …` — margin yetersiz (per-slot sizing patladı veya OKX-side anlık eksiklik).

`htf_settle_timeout symbol=…` — Pine TF switch'inde 10s'de last_bar flip etmedi → o sembol cycle'ı skip.

`SMT Signals table not found — using empty state` — settle poll içinde Pine henüz render etmemiş (beklenen, runner zarif handle eder).

`orphan_close key=…` — kapanan pozisyonun journal'da OPEN row'u yok (bot crash / `--max-closed-trades` exit sonrası tipik).

`journal_open_but_no_live_position key=…` — restart sonrası diff: journal'da OPEN ama OKX'te yok (manuel kapatılmış olabilir, journal stale).

---

## 15. Phase 7 öncesi durum

- **Eşik:** ≥50 kapalı trade.
- **Şu anki:** ~19 kapalı (W=3 / L=15 / BE=1), 2 açık.
- **Strateji parametreleri** Phase 7'de RL ile tunable: `confluence_threshold`, `pattern_weights`, `min_rr_ratio`, `risk_pct`, `volatility_scale`, `ob_vs_fvg_preference`.
- **Reward shape:** `pnl_r + setup_penalty + dd_penalty + consistency_bonus`.
- **Walk-forward zorunlu:** OOS iyileşmediği parametre asla deploy edilmez.

---

## Hızlı referans tablosu

| İş | Dosya | Fonksiyon |
|---|---|---|
| Bir tick | `src/bot/runner.py` | `BotRunner.run_once` |
| Tek sembol cycle | `src/bot/runner.py` | `_run_one_symbol` |
| TF switch + settle | `src/bot/runner.py` | `_switch_timeframe`, `_wait_for_pine_settle` |
| Pine veri okuma | `src/data/structured_reader.py` | `read_market_state` |
| Confluence skor | `src/analysis/multi_timeframe.py` | `calculate_confluence`, `score_direction` |
| Plan inşası | `src/strategy/entry_signals.py` | `build_trade_plan_with_reason` |
| SL seçim | `src/strategy/entry_signals.py` | `select_sl_price` |
| R:R sizing | `src/strategy/rr_system.py` | `calculate_trade_plan` |
| Reentry gate | `src/bot/runner.py` | `_check_reentry_gate` |
| LTF reversal close | `src/bot/runner.py` | `_is_ltf_reversal`, `_defensive_close` |
| Order placement | `src/execution/order_router.py` | `OrderRouter.place`, `_place_algos` |
| Pozisyon takip | `src/execution/position_monitor.py` | `PositionMonitor.poll`, `_detect_tp1_and_move_sl` |
| Kapanış handle | `src/bot/runner.py` | `_handle_close` |
| Journal CRUD | `src/journal/database.py` | `record_open`, `record_close`, `replay_for_risk_manager` |
| Circuit breakers | `src/strategy/risk_manager.py` | `RiskManager.can_trade`, `register_trade_closed` |
