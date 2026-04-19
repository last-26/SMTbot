# Trade Lifecycle — Uçtan Uca Yürüyüş (Post-Pivot 2026-04-19)

Bu doküman botun tek bir tick'te nasıl çalıştığını, indikatör verilerini nasıl okuduğunu, **zone tabanlı** girişi nasıl planladığını, pending → open → close geçişlerini nasıl yönettiğini ve pozisyonu nasıl kapattığını baştan sona anlatır. Kod referansları `file:line` formatındadır.

> **Not:** 2026-04-19 stratejik pivot'u (Phase 7.A→7.D4) yayında. Eski pre-pivot akış (market emir + "confluence ≥ eşik → hemen al") artık kullanılmıyor. Ham confluence skoru hâlâ hesaplanıyor ama emir direkt market olarak açılmıyor — önce **zone bekleniyor**.

---

## 1. Yüksek seviye mimari

```
┌────────────────┐    ┌──────────────────┐    ┌────────────────┐
│  TradingView   │ →  │   Python Bot     │ →  │      OKX       │
│  (gözler)      │    │   (beyin)        │    │  (eller)       │
│  - Pine        │    │  - 5-pillar      │    │  - limit emir  │
│  - SMT Overlay │    │    confluence    │    │    (post-only) │
│  - SMT Osc.    │    │  - zone planner  │    │  - OCO SL/TP   │
│                │    │  - ADX regime    │    │  - pending     │
│                │    │  - cross-asset   │    │    lifecycle   │
│                │    │    veto          │    │                │
└────────────────┘    └──────────────────┘    └────────────────┘
        ↑                      ↓
        └──── multi-TF cycle + CryptoSnapshot ──┘
```

- **Claude Code** orkestra şefi: Pine yazar, RL eğitir, hata ayıklar. **Tick bazlı kararları Python botu verir**, Claude vermez.
- **TradingView** indikatör motoru: Pine script'ler her barda yeniden hesaplar, sonuçları `SMT Signals` + `SMT Oscillator` tablolarına yazar.
- **Python core** veriyi okur, 5-pillar confluence skorlar, zone seçer, risk kapılarından geçirir, OKX'e limit emir yollar.
- **OKX** pozisyonu OCO (one-cancels-other) algoritmasıyla otomatik yönetir.

---

## 2. Bir tick'te neler oluyor

`BotRunner.run_once()` (`src/bot/runner.py`) — her `bot.poll_interval_seconds` (default 5s) bir kez çalışır:

```
run_once()
├── _process_closes()                  # 1) kapanışları önce drene et
├── _build_crypto_snapshot()           # 2) BTC+ETH snapshot → ctx
├── _process_pending_setups()          # 3) PENDING limitleri izle (fill / timeout / invalidation)
└── for symbol in trading.symbols:     # 4) round-robin 5 parite
        _run_one_symbol(symbol)
```

### Sıralama önemli:

1. **`_process_closes()` önce** — OKX'e track edilen pozisyonlar kapandı mı diye sorar (`PositionMonitor.poll`), close'ları journal'a yazar, R hesaplar, slot serbest kalır.
2. **`CryptoSnapshot` build** — BTC + ETH 15m trend + 3m momentum snapshot'ı `ctx.crypto_snapshot`'a yazılır (cross-asset veto girdisi). Layer 3.
3. **Pending setup poll** — PENDING limit emirler fill'lendi mi, timeout'a uğradı mı, zone invalidate oldu mu kontrol edilir. PENDING → FILLED olursa OCO yerleştirilir, → CANCELED olursa journal "zone_timeout_cancel" / "pending_invalidated" yazar.
4. **Round-robin parite döngüsü** — `BTC → ETH → SOL → DOGE → XRP`. BTC + ETH ilk çalışır çünkü altcoin cycle'ları `CryptoSnapshot`'ı okuyor.

Tek parite cycle'ı (`_run_one_symbol`):

```
_run_one_symbol("BTC-USDT-SWAP"):

  ┌─ Macro blackout kontrolü (Finnhub + FairEconomy, ±30/15-dk USD HIGH)
  │     → blackout → TV settle bile yapma, skip (46s tasarruf)
  │
  ├─ TV: chart → "OKX:BTCUSDT.P", symbol_settle sleep
  │
  ├─ HTF pass (15m) — SADECE açık pozisyonu olmayan pariteler için:
  │     switch_timeframe(15m) → settle + freshness poll + post-grace
  │     refresh candles
  │     detect_sr_zones() + HTF OB/FVG zone extraction → htf_sr_cache[symbol]
  │     (açık pozisyon varsa bu pass atlanır → ~5-15s/pair tasarruf)
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
  │     classify_trend_regime(candles) → ADX regime (Layer 4)
  │
  ├─ LTF reversal defensive close           ── (uygunsa pozisyon kapat + return)
  ├─ Symbol-level dedup                     ── (açık pozisyon veya PENDING setup varsa skip)
  ├─ Sizing balance compute (total_eq + okx_avail)
  ├─ build_trade_plan_with_reason()         ── (5-pillar score + hard gates + zone setup)
  │     → None dönerse NO_TRADE + rejected_signals tablosuna INSERT, return
  ├─ Zone setup seçimi (setup_planner)      ── 4 zone kaynağı öncelik sırasıyla
  ├─ PLANNED log
  ├─ Reentry gate                           ── (block → rejected_signals + return)
  ├─ Risk manager can_trade()               ── (halt → return)
  ├─ router.place_limit_entry(plan, zone)   ── post-only limit, fallback'li
  ├─ monitor.register_pending() + journal.record_pending()
  └─ (fill outer loop'un sonraki tick'inde `_process_pending_setups`'ta yakalanır)
```

### Pine settle koruması (multi-TF geçişlerde indikatörün bitmesini bekleme)

`_switch_timeframe` her TF değişiminde:

1. **Statik bekleme** — `tf_settle_seconds` (default `3.5s`) — Pine yeniden hesaplamaya başlasın.
2. **Freshness poll** — `_wait_for_pine_settle` `signal_table.last_bar` değerinin değişmesini bekler, her `pine_settle_poll_interval_s` (`0.3s`). Max `pine_settle_max_wait_s` (`10s`).
3. **Post-grace** — poll geçtikten sonra ekstra `pine_post_settle_grace_s` (`1.0s`) uyur. Çünkü `last_bar` flip ettiğinde **Oscillator tablosu hâlâ render ediyor olabilir** (özellikle 1m'de; `last_bar` her duvar-saati dakikasında tikler, bu "tablolar dolu" demek değil).

**Budget (5 parite, 3m entry TF = 180s cycle):** tipik ~125-155s (açık pozisyonlu pariteler HTF pass'ı atladığı için erken döner), worst ~247s. Worst-case zaman zaman oluşursa sadece o cycle skip olur.

---

## 3. İndikatör katmanı (TradingView Pine)

Chart'ta iki Pine indikatörü yüklü. Bot **tablolarından okur** (çizimler ek bilgi, OB/FVG zone kaynağı olarak kullanılır):

### `pine/smt_overlay.pine` → "SMT Signals" tablosu (19 satır, post-D4 trim)

| Alan | İçerik |
|---|---|
| `trend_htf`, `trend_ltf` | EMA bias yönü |
| `structure` | HH/HL/LH/LL durumu |
| `last_mss` | Son market structure shift (BOS/CHoCH) |
| `active_fvg` | Yakında aktif FVG var mı (`active_ob` D4'te kaldırıldı, factor weighted 0) |
| `liquidity_above`, `liquidity_below` | En yakın likidite seviyeleri |
| `last_sweep` | Son liquidity sweep yönü |
| `session` | London/NewYork/Asia/Off |
| `vmc_ribbon`, `vmc_wt_bias`, `vmc_wt_cross`, `vmc_last_signal`, `vmc_rsi_mfi` | VuManChu Cipher A bileşenleri |
| `atr_14`, `price`, `vwap_1m/3m/15m` | Canlı metrikler (`#.########` formatında — DOGE/XRP ATR'ı 0'a yuvarlanmasın) |
| `last_bar` | Tablonun "freshness beacon"'u |

> **D4 değişikliği:** `confluenceScore` block (~35 satır) + `confluence` satır + `active_ob` satır kaldırıldı. Confluence skorlaması artık %100 Python tarafında. OB/FVG **kutu render'ı korundu** çünkü SL seçimi onlardan beslenmeye devam ediyor.

### `pine/smt_oscillator.pine` → "SMT Oscillator" tablosu (15 satır)

| Alan | İçerik |
|---|---|
| `wt1`, `wt2`, `wt_state`, `wt_cross` | WaveTrend ana sinyal |
| `wt_vwap_fast` | VWAP-bias bileşeni |
| `rsi`, `rsi_mfi` | RSI + Money Flow Index kombo |
| `stoch_k`, `stoch_d`, `stoch_state` | Stochastic RSI |
| `last_signal`, `last_wt_div`, `last_regular_div`, `last_hidden_div` | En son BUY/SELL + divergence türleri (D2) |
| `momentum` | Dahili 0–5 momentum skoru |
| `last_bar` | Freshness beacon |

### Çizimler (ek detay)

Pine ayrıca **labels** (MSS, sweep), **boxes** (FVG, OB), **lines** (likidite, seans seviyeleri) çizer. Bot bunları `data labels/boxes/lines` üzerinden okur ve `MarketState`'e paketler.

### `MarketState` (Pydantic)

`src/data/structured_reader.py:read_market_state()` her şeyi tek dataclass'a toplar:

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
    derivatives: Optional[DerivativesSnapshot]
    liquidity_heatmap: Optional[LiquidityHeatmap]
    trend_regime: Optional[TrendRegimeResult]  # Layer 4 — ADX
```

---

## 4. 5-Pillar Confluence + Hard Gates (Post-Pivot Layer 1)

`src/analysis/multi_timeframe.py:score_direction` her aday yön için çağrılır. Pivot sonrası factor ağırlıkları **5 direğe** grupludur:

### Core scoring pillar'ları

| Pillar | Concrete factors | Ağırlık |
|---|---|---|
| **Market Structure** | `mss_alignment` (0.75), `recent_sweep` (1.0) | Core |
| **Liquidity** | Pine standing pools + Coinalyze heatmap clusters (`derivatives_heatmap_target` 0.5) + sweep-reversal | Core + zone source |
| **Money Flow** | `money_flow_alignment` (0.6, MFI bias) | Core |
| **VWAP** | `vwap_composite_alignment` — 3-TF align → 1.0, 2-of-3 → 0.5, 1-of-3 → 0 | Core (pre-pivot 3'e bölünmüştü, reconsolidate edildi) |
| **Divergence** | `divergence_signal` (1.0, regular + hidden, bar-ago decay) + `oscillator_high_conviction_signal` (1.25, GOLD_BUY/BUY_DIV/SELL_DIV) | Core |

### Demoted (skor vermez, audit için kalır)

- `at_order_block` → **weight 0** (Pine 3m OB'leri Sprint 3'te %0 WR — regime-fragile). HTF-varyant ile geri ekleme Deferred TODO'da.
- `htf_trend_alignment` (0.75) → **sadece yön-girdisi**, setup planner için. Skorlamaya katılmıyor. Aynı zamanda ADX regime path'ini besliyor.
- `oscillator_signal` (0.75), `liquidity_pool_target` (0.5) → Divergence pillar'ına / zone source'a emildi.

### Hard gates (reject-on-mismatch, skor değil)

Aşağıdaki kapılar sırayla çalışır; herhangi biri fail ederse `rejected_signals`'a INSERT + return:

| Gate | Reject reason | Mantık |
|---|---|---|
| `premium_discount_zone` | `wrong_side_of_premium_discount` | Long'lar discount yarıda (son-swing midpoint altında), short'lar premium yarıda olmalı |
| `displacement_candle` | `displacement_missing` | FVG tabanlı zone'lar son 3-5 barda taze büyük gövdeli displacement istiyor (gerçek imbalance kanıtı) |
| `ema_momentum_contra` | `ema_momentum_contra` | Long'lar 21-EMA < 55-EMA + spread genişliyorsa blok; short'lar ayna |
| `vwap_misaligned` | `vwap_misaligned` | Strict: fiyat tüm session VWAP'lerin (1m/3m/15m) yanlış tarafındaysa veto. Eksik (sıfır) VWAP'ler atlanır; hepsi eksikse fail-open |
| `cross_asset_opposition` | `cross_asset_opposition` | Altcoin (SOL/DOGE/XRP): LONG + BTC+ETH 15m BEARISH → veto; SHORT + BTC+ETH BULLISH → veto. Sektör rotasyonu tek parite muhalefetinde geçer |

### Conditional scoring (Layer 4 — ADX regime)

`trend_regime_conditional_scoring_enabled: true` ise:
- `STRONG_TREND` → `htf_trend_alignment × 1.5`, `recent_sweep × 0.5` (trend devamı > reversal)
- `RANGING` → `htf_trend_alignment × 0.5`, `recent_sweep × 1.5` (sweep/reversal setup'ları tercih)
- `WEAK_TREND` / `UNKNOWN` → değişiklik yok

### Skor + yön

`score_direction(state, BULLISH)` ve `score_direction(state, BEARISH)` ayrı hesaplanır. Yüksek olan kazanır. `min_confluence_score` (default **2.0**) altı → `below_confluence`.

---

## 5. Zone-Based Entry Planner (Post-Pivot Layer 2)

**Eski akış:** `confluence ≥ eşik → market emir, anında gir`
**Pivot akışı:** `confluence ≥ eşik → zone tanımla → zone kenarına limit emir → N bar bekle → fill | cancel`

`src/strategy/setup_planner.py:build_zone_setup` şu adımlarla `ZoneSetup` üretir:

```python
@dataclass
class ZoneSetup:
    direction: Direction
    entry_zone: tuple[float, float]       # örn. FVG range, liq pool ± buffer
    trigger_type: Literal["zone_touch", "sweep_reversal", "displacement_return"]
    sl_beyond_zone: float                  # yapısal, % floor değil
    tp_primary: float                      # ilk liq target veya HTF zone
    max_wait_bars: int
    zone_source: Literal["fvg_htf", "liq_pool", "vwap_retest", "sweep_retest"]
```

### Zone kaynağı önceliği (yüksekten düşüğe)

1. **`liq_pool`** — Unswept Coinalyze liquidity pool + premium/discount eşleşmesi (long discount pool'da, short premium pool'da).
2. **`fvg_htf`** — HTF 15m doldurulmamış FVG; fiyat dışarıdan yaklaşıyor + displacement candle gate.
3. **`vwap_retest`** — Seans VWAP re-test, seçilen yönde pullback.
4. **`sweep_retest`** — Son-swing likidite sweep-and-reversal (likidite süpürüldü, sonra içeri kapandı).

### Yürütme kuralları

- **Giriş emri:** `limit` (post-only tercihli, maker fee için). Post-only reddedildiğinde regular limit; son çare market-at-zone-edge (emergency hatch).
- **SL:** zone yapısının dışında, % floor değil. `min_sl_distance_pct` **acil durum taban**'ı — genişletir, reddetmez (patolojik ince zone'lar notional shrink ile yaşar).
- **TP:** primary = likidite hedefi veya HTF zone (sabit-R değil). Runner dinamik (D5 — 50+ trade sonrası revisit).
- **Timeout:** `max_wait_bars` (default 10 bar = 30dk/3m) dolu kalırsa cancel → `reject_reason=zone_timeout_cancel`.
- **Invalidation:** zone fill olmadan ihlal edilirse → anında cancel → `reject_reason=pending_invalidated`.
- **`max_concurrent_setups_per_symbol=1`.** Aynı paritede PENDING limit + canlı pozisyon birlikte olamaz.

### Position Monitor state'leri (Layer 2)

```
PENDING → FILLED → OPEN → CLOSED
   │
   └→ CANCELED (timeout veya invalidation)
```

`src/execution/position_monitor.py:register_pending` pending emirleri tracker'a ekler. `poll()` her tick:
- Fill → OPEN'a geç + OCO (TP1/TP2) yerleştir.
- `max_wait_bars` geçti → cancel.
- Zone ihlal (fiyat stop seviyesini fill olmadan aştı) → cancel.

Journal her state transition'ı `zone_wait_bars`, `zone_fill_latency_bars`, `setup_zone_source` ile damgalar (schema v2).

---

## 6. SL seçimi — yapısal seviye öncelik sırası

`src/strategy/entry_signals.py:select_sl_price` sırayla dener, ilk hit = SL:

1. **Pine OB** (`order_block_pine`) — chart'a çizili en yakın geçerli Pine OB kutusu.
2. **Pine FVG** (`fvg_pine`).
3. **Python OB** (`order_block_py`) — Python kendi tarafında yeniden tespit.
4. **Python FVG** (`fvg_py`).
5. **Swing lookback** (`swing`) — son `swing_lookback_per_symbol` (DOGE/XRP=30, diğer=20) mum içindeki ekstrem.
6. **ATR fallback** (`atr_fallback`) — entry ± 2×ATR.

Her seviye **buffer** ile itilir: `sl = level ± buffer_mult × ATR` (`buffer_mult=0.2`).

### Pivot sonrası değişen: Zone-SL önceliği

`setup_planner` bir `ZoneSetup` üretirse, SL artık **`sl_beyond_zone`** değeridir (structural, zone dışında). `select_sl_price` sadece zone bulunamazsa (fallback pipeline) çalışır.

### HTF S/R zone'ları SL'i sıkıştırır

`_push_sl_past_htf_zone` — 15m S/R zone'u SL ile entry arasına düşerse, SL zone'un uzak kenarının hemen dışına snap eder. **Sadece sıkıştırır, genişletmez** (risk artmaz).

### Min SL distance floor — dar stop'ları **genişletir**

`min_sl_distance_pct_per_symbol` (BTC 0.005, ETH 0.010, SOL 0.008, DOGE/XRP 0.007):
- SL mesafesi taban'ın altındaysa → SL **genişletilir** (reject değil) tabana.
- Notional otomatik küçülür (`risk_amount / sl_pct`) → R sabit kalır.
- Mantık: yüksek kaldıraçta 0.05-0.1% OB stop anında wick'lenir. Taban fill'e nefes alanı verir; sizing küçülür.

### Min TP distance floor — gerçek **reject**

`min_tp_distance_pct` (default `0.004` = `0.4%`):
- HTF TP ceiling uygulandıktan sonra TP mesafesi < 0.4% → `tp_too_tight` reject.
- Mantık: 3-fill partial-TP yaşam döngüsü `3 × 0.05% taker = 0.15%` + slippage yakar. 0.4% taban ~2× round-trip fee.

---

## 7. R:R + sizing — `calculate_trade_plan`

`src/strategy/rr_system.py:calculate_trade_plan` — saf matematik, yan etki yok.

### Adımlar

1. **Risk miktarı:** `risk_amount = account_balance × risk_pct`. (Default 1% → 5000 USDT demo bakiyede ~50 USDT = 1R.)
2. **SL %:** `sl_pct = |entry - sl| / entry`.
3. **Fee reserve:** sizing payda `sl_pct + fee_reserve_pct` (YAML `0.001`) olur — stop-out giriş+çıkış taker fee'ler SONRA ~$R'da kalsın.
4. **TP fiyatı:** zone-setup `tp_primary` kullanıyorsa o. Değilse `tp = entry ± (sl_distance × rr_ratio)` (default `rr_ratio=1.5`, Sprint 3 loosened).
5. **Ideal notional:** `ideal_notional = risk_amount / (sl_pct + fee_reserve_pct)`.
6. **Required leverage:** `required_lev = ideal_notional / margin_balance`.
7. **Max-feasible leverage:** `feasible_lev = floor(_LIQ_SAFETY_FACTOR / sl_pct)` (`_LIQ_SAFETY_FACTOR=0.6`). SL likidasyon mesafesinin %60'ı içinde kalsın diye sıkıştırır — %40 bakım + mark drift buffer.
8. **Effective leverage:** `lev = min(okx_max, symbol_caps, max(ceil(required_lev), feasible_lev), 1)`.
9. **Margin safety:** `max_notional = margin_balance × lev × 0.95` (`_MARGIN_SAFETY` — sCode 51008'i önler).
10. **Kontrat sayısı:** `num_contracts = int(notional // (contract_size × entry))`. OKX integer zorunlu.
11. **Gerçekleşen risk:** yuvarlama yüzünden `actual_risk = num_contracts × ctVal × |entry - sl|` — istenenden **biraz az** olabilir, asla fazla değil.

### Risk vs margin ayrımı

- **`account_balance`** → R sürücüsü (total equity'den, drawdown ile doğal küçülür).
- **`margin_balance`** → leverage/notional tavanı (per-slot `total_eq / max_concurrent_positions=4` ve canlı `okx_avail` minimumu).

Cross-margin'de bu ayrım, 4 eşzamanlı slot'tan birinin sCode 51008 almasını önler (peer'lar margin kilitlediği için).

### Per-symbol instrument spec + leverage cap

Effective tavan = `min(trading.max_leverage, okx_instrument_cap, symbol_leverage_caps[sym])`.

- BTC `75x`, ETH `30x` (demo flash-wick'leri 30x üstü patlatıyor), SOL `50x` (OKX cap), DOGE/XRP `30x`.
- Contract size: BTC `ctVal=0.01`, ETH `0.1`, **SOL `1`**. Hardcode YAML SOL'u 100× over-size'lardı.

---

## 8. Reject reasons + risk gates

`build_trade_plan_with_reason` `(None, reason)` döndürebilir. Runner `NO_TRADE` loglar **ve `rejected_signals` tablosuna INSERT** eder (counter-factual peg için).

### Unified reject reason listesi (post-pivot)

| Kategori | Reasons |
|---|---|
| Structure / confluence | `below_confluence`, `no_setup_zone`, `wrong_side_of_premium_discount`, `vwap_misaligned`, `ema_momentum_contra`, `cross_asset_opposition`, `session_filter`, `macro_event_blackout`, `crowded_skip` |
| R:R / sizing | `no_sl_source`, `zero_contracts`, `htf_tp_ceiling`, `tp_too_tight`, `insufficient_contracts_for_split` |
| Pending lifecycle | `zone_timeout_cancel`, `pending_invalidated` |

> Taban-altı SL mesafeleri **genişletilir**, reddedilmez.

### Reentry gate (plan kabul edildikten sonra)

Son kapanış per (symbol, side) hatırlanır (`LastCloseInfo`). 4 sıralı kapı:

1. **Cooldown** — `min_bars_after_close × entry_tf_seconds` geçmedi → `cooldown_3bars`.
2. **ATR move** — fiyat son çıkıştan `min_atr_move × ATR` (default `0.5×ATR`) kadar hareket etmedi → `atr_move_insufficient`.
3. **Post-WIN quality** — son trade WIN → yeni confluence **kesinlikle daha yüksek** olmalı → yoksa `post_win_needs_higher_confluence`.
4. **Post-LOSS quality** — son trade LOSS → yeni confluence **eşit veya yüksek** olmalı → yoksa `post_loss_needs_ge_confluence`.
5. **BREAKEVEN** quality gate'i bypass eder.

Karşıt yönler izole edilmiştir — BTC long kapatmak BTC short açmayı gate'lemez.

### Risk manager (`risk_mgr.can_trade(plan)`)

Circuit-breaker zinciri (ilk eşleşme kazanır, Sprint 3 loose'ladı):

1. Drawdown ≥ `max_drawdown_pct` (**40%** — Sprint 3 loose, eski 25%) → **kalıcı halt** (manuel `--clear-halt`).
2. `halted_until > now` → cooldown halt aktif.
3. Günlük realize kayıp ≥ `max_daily_loss_pct` (**40%** — eski 15%) → 24h halt.
4. Ardışık kayıp ≥ `max_consecutive_losses` (**9999** — eski 5, efektif kapalı) → 24h halt.
5. Açık pozisyon ≥ `max_concurrent_positions` (**4**) → blok.
6. Plan bazlı: lev > max, RR < `min_rr_ratio` (**1.5** — eski 2.0), contracts == 0 → blok.

> **Not:** Loosened değerler Phase 8 veri toplama süresince aktif. 20+ post-pivot kapanmış trade sonra 5/15/25/2.0'a restore — CLAUDE.md Deferred TODO'da.

---

## 9. Emir yerleştirme — `OrderRouter`

`src/execution/order_router.py`. İki ana path:

### 9.a. Limit entry (zone-based default) — `place_limit_entry`

1. **Set leverage** — `set_leverage(inst, lever, mgnMode, posSide)`. Hata → `LeverageSetError`, pozisyon açılmaz.
2. **Limit emir** — `place_limit_order(side, posSide, px=zone_limit_price, sz=plan.num_contracts, ordType="post_only")`.
   - Post-only reject (`sCode=51006` fiyat yanlış tarafta) → regular `limit`'e fallback.
   - Son çare: `market` at zone-edge (emergency hatch).
3. **PENDING tracker'a kaydet** — `monitor.register_pending(inst, algo_id, zone, max_wait_bars)`.
4. **OCO yerleştirilmez** — fill olana kadar. `_process_pending_setups` fill tespit ettiğinde `_place_algos` çağrılır.

### 9.b. Market entry (emergency fallback / legacy test path) — `place`

Fill anında veya fallback'te çalışır; OCO'yu hemen yerleştirir.

### Partial TP mode (default ON)

`partial_tp_enabled: true`, `partial_tp_ratio: 0.5`, `partial_tp_rr: 1.5`:

- **TP1 OCO** — `size = ceil(num_contracts × 0.5)`, `tpTriggerPx = entry ± (sl_dist × 1.5)`, `slTriggerPx = plan.sl_price`.
- **TP2 OCO** — `size = num_contracts - tp1_size`, `tpTriggerPx = plan.tp_price`, `slTriggerPx = plan.sl_price`.
- Her iki algo OKX'e gider; biri fail ederse ikisi cancel + pozisyon kapatılır.
- Dejenere `num_contracts == 1` → split imkansız → `insufficient_contracts_for_split` reject (gate fail loud).

### Sonuç

`ExecutionReport(order=OrderResult, algos=[AlgoResult, AlgoResult])`. `algo_ids` `monitor.register_open` + `journal`'a yazılır.

---

## 10. Açık pozisyon yaşam döngüsü — `PositionMonitor`

`src/execution/position_monitor.py`. WS değil, REST poll. `monitor.poll()` her `run_once` başında çağrılır.

### State'ler

```
PENDING → FILLED → OPEN → CLOSED
   │
   └→ CANCELED
```

### Tracked state (OPEN)

```python
_Tracked:
    inst_id, pos_side, size, entry_price
    initial_size       # partial detection referansı
    algo_ids           # [tp1_algo, tp2_algo]
    tp2_price          # SL→BE replace için gerekli
    be_already_moved   # idempotency
    zone_source        # schema v2 damgalama
```

### Poll mantığı

1. **Canlı listede yok** → pozisyon kapandı. `CloseFill` emit, tracker'dan düş.
2. **Canlı listede, size küçüldü** → TP1 fill (kısmi). `_detect_tp1_and_move_sl`:
   - TP2 algo'yu cancel.
   - Yeni OCO yerleştir: `SL = entry ± sl_be_offset_pct` (fee-buffered BE), `TP = tp2_price`, `size = remaining`.
   - `algo_ids` güncelle, `be_already_moved=True`.
   - `on_sl_moved` callback fire → journal `sl_moved_to_be` damgalar.
3. **Canlı listede, size aynı** → refresh (entry_price) ve devam.

### SL-to-BE spin-proof (2026-04-18 production incident patch)

`_detect_tp1_and_move_sl` cancel ve place'i ayrı try-block'lara ayırır:
- (a) OKX `{51400, 51401, 51402}` cancel'da → idempotent success sayılır, BE OCO yine yerleştirilir.
- (b) Generic cancel fail → `cancel_retry_count++`, 3 denemede pes eder, `be_already_moved=True` (poll hammer'lamayı durdurur).
- (c) Cancel ok + place fail → pozisyon korumasız (CRITICAL log, TP2 algo_ids'den düşürülür, callback fire) — emergency market-close kasıtlı OTOMATİK DEĞİL, operator karar verir.

### LTF reversal defensive close

Pozisyon açıkken, her entry pass başında:

- Pozisyon yaşı `open_trade_opened_at`'ten kontrol — `ltf_reversal_min_bars_in_position × tf_seconds` altındaysa skip (taze pozisyona gelişme zamanı).
- `_is_ltf_reversal()` true ise (1m oscillator trend + `last_signal` taze, açık pozisyonla zıt):
  - `_defensive_close()` → tüm tracked algo'ları cancel + market `close_position()`.
  - `pending_close_reasons[(sym,side)] = "ltf_reversal"` set → journal `close_reason` damgalar.
  - `defensive_close_in_flight` idempotency flag.

### Close enrichment — gerçek PnL (kritik)

`PositionMonitor._close_fill_from` sadece "pozisyon kayboldu" bilir — `pnl_usdt=0, exit_price=0`. **Gerçek PnL** `OKXClient.enrich_close_fill`'den:

- `/api/v5/account/positions-history` (son 24h) sorgular.
- `realizedPnl`, `closeAvgPx`, `fee`, `uTime` çıkarır.
- Onsuz **her kapanış BREAKEVEN görünür** ve drawdown / ardışık-kayıp breaker **asla tetiklenmez**.

---

## 11. Kapanış akışı — `_handle_close`

```python
async def _handle_close(fill):
    enriched = enrich_close_fill(fill)              # gerçek PnL
    trade_id = open_trade_ids.pop(key, None)
    close_reason = pending_close_reasons.pop(key)   # ltf_reversal vb.
    defensive_close_in_flight.discard(key)
    open_trade_opened_at.pop(key)

    if trade_id is None:
        risk_mgr.register_trade_closed(...)         # orphan — risk_mgr hâlâ beslensin
        return

    updated = await journal.record_close(
        trade_id, enriched,
        close_reason=close_reason,
        zone_fill_latency_bars=...,                 # schema v2
        trend_regime_at_entry=...,
    )
    risk_mgr.register_trade_closed(TradeResult(pnl_usdt, pnl_r, timestamp))
    last_close[key] = LastCloseInfo(price, time, confluence, outcome)
```

### Journal `record_close`

- `exit_price`, `pnl_usdt`, `closed_at` doldurur.
- `pnl_r = pnl_usdt / risk_amount_usdt` hesaplar.
- `outcome` PnL işaretinden: `>0 → WIN`, `<0 → LOSS`, `==0 → BREAKEVEN`.
- `close_reason` (`ltf_reversal`, `tp_hit`, `sl_hit`, `manual_close_*`) yazar.

### Risk manager güncelleme

- `current_balance += pnl_usdt`.
- `peak_balance` güncelle → `drawdown_pct` yeniden hesapla.
- `daily_realized_pnl += pnl_usdt`.
- WIN → `consecutive_losses = 0`; LOSS → `+= 1`.
- Bir eşik aşıldıysa `halted_until` set.

### Reentry gate state

`last_close[(symbol, side)]` güncellenir → sonraki aynı-yön reentry bunu kullanır.

---

## 12. Başarısızlık izolasyonu — neyi neyin etkilediği

| Başarısızlık | Etkisi |
|---|---|
| TV bridge timeout | O parite cycle'ı skip, diğerleri devam |
| Pine settle timeout | O parite cycle'ı skip |
| Coinalyze 401/429 | `state.derivatives=None`, derivatives factor'lar pasif, price-structure girişler devam |
| Binance WS disconnect | Auto-reconnect (exponential backoff), heatmap historical layer eksik |
| `set_leverage` fail | Pozisyon açılmaz, `LeverageSetError` |
| `place_limit_order` post-only reject | Regular limit fallback; o da reject → market emergency |
| `place_market_order` fail | Pozisyon yok, `OrderRejected` |
| Algo fail | Pozisyon **auto-close** (`close_on_algo_failure: true`), `AlgoOrderError` |
| `journal.record_pending`/`record_open` fail | **Pozisyon canlı** (orphan) — `_reconcile_orphans` startup'ta loglar, operator karar |
| `journal.record_close` fail | Risk manager yine beslenir, journal satır güncellemesi kayıp |
| `enrich_close_fill` fail | Raw fill (`pnl_usdt=0`) — drawdown/streak accounting kayıp, **dikkat** |
| `CryptoSnapshot` build fail | `ctx.crypto_snapshot=None`, altcoin cross-asset veto fail-open (cross_asset_opposition fire etmez) |

---

## 13. Restart davranışı

`BotRunner._prime()`:

1. **`journal.replay_for_risk_manager`** — kapalı trade'leri entry sırasıyla yürür, `peak_balance`, `consecutive_losses`, `current_balance`'ı durable truth'tan yeniden kurar.
2. **`_apply_clear_halt`** (sadece `--clear-halt` ile) — halt + daily counter + peak reset.
3. **`_rehydrate_open_positions`** — OPEN journal satırlarını `monitor._tracked`'e yükler. `sl_moved_to_be=True` ise `be_already_moved=True` forward → tekrar cancel/replace önlenir.
4. **`_rehydrate_pending_setups`** — PENDING rows'ları `monitor._pending`'e yükler (bot down iken zone hâlâ geçerli olabilir).
5. **`_reconcile_orphans`** — canlı OKX pozisyonları ↔ journal OPEN diff. **Sadece loglar**, otomatik action yok.
6. **`_load_contract_sizes`** — `ctVal` + `max_leverage` per sembol OKX'ten (per-symbol cap).

OKX tarafındaki OCO algolar bot down iken aktif kalır → pozisyon SL/TP korumasız kalmaz.

---

## 14. CLI kullanımı

```bash
# Smoke test — full pipeline, tek tick, gerçek emir yok
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo (gerçek emir, OKX demo hesabı)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml

# Olası halt'ı sıfırla
.venv/Scripts/python.exe -m src.bot --clear-halt --config config/default.yaml

# 50 kapanış sonrası auto-stop (Phase 8 veri eşiği)
.venv/Scripts/python.exe -m src.bot --max-closed-trades 50

# Sadece derivatives warmup (entry/exit yok)
.venv/Scripts/python.exe -m src.bot --derivatives-only --duration 600

# Rapor
.venv/Scripts/python.exe scripts/report.py --last 7d

# Factor audit — per-symbol/session/regime WR + counter-factuals
.venv/Scripts/python.exe scripts/factor_audit.py

# Rejected_signals counter-factual peg (WIN/LOSS/NEITHER damgalar)
.venv/Scripts/python.exe scripts/peg_rejected_outcomes.py --commit
```

---

## 15. Loglama — neyi nerede bakacağın

### Decision log'ları (`scripts/logs.py --decisions`)

```
symbol_cycle_start symbol=BTC-USDT-SWAP
symbol_decision symbol=BTC-USDT-SWAP NO_TRADE reason=below_confluence price=64500.0 session=LONDON direction=BULLISH confluence=1.50/2.0 factors=...
symbol_decision symbol=ETH-USDT-SWAP PLANNED direction=BEARISH zone=[3248-3252] trigger=zone_touch zone_source=fvg_htf entry=3250.0 sl=3268.0 tp=3220.0 rr=1.67 confluence=4.50 contracts=10 lev=20x risk_bal=5000.0 margin_bal=1250.0 factors=...
pending_registered BEARISH ETH-USDT-SWAP algo_id=xxx max_wait=10bars
zone_filled BEARISH ETH-USDT-SWAP algo_id=xxx fill_price=3250.3 latency_bars=2
opened BEARISH ETH-USDT-SWAP 10c @ 3250.3 trade_id=yyy
sl_moved_to_be_via_replace inst=ETH-USDT-SWAP side=short remaining_size=5.0
closed trade_id=yyy outcome=WIN pnl_r=2.85
```

### Reject gerekçesi

- `reentry_blocked symbol=… side=… reason=cooldown_3bars`
- `blocked symbol=… reason=…` (risk_mgr halt)
- `zone_timeout_cancel symbol=… waited=10bars`
- `pending_invalidated symbol=… zone_violated_at=…`

### Hatalar

- `order_rejected … sCode=51008 …` — yetersiz margin
- `htf_settle_timeout symbol=…` — Pine `last_bar` 10s'de flip etmedi
- `SMT Signals table not found — using empty state` — Pine render olmadı
- `orphan_close key=…` — kapanan pozisyonun journal OPEN satırı yok
- `journal_open_but_no_live_position key=…` — restart diff

---

## 16. Phase 8 statüsü (aktif)

- **Eşik:** 50+ temiz post-pivot kapanmış trade.
- **`rl.clean_since`:** `2026-04-19T06:30:00Z` — reporter + future RL sadece bu tarihten sonrasını görür.
- **Current:** 0 post-pivot kapanış (pivot bugün ship edildi, bot yeni başlayacak). 46 pre-pivot trade DB'de audit için duruyor.
- **Gate to leave data-collection:** 50 trade + WR ≥ 45% + avg R ≥ 0 + ≥2 trend-regime temsil + net PnL ≥ 0.
- **Sonraki adımlar:**
  1. Data collection (şimdi) — demo run, her ~10 kapanışta `factor_audit.py` ile erken-uyarı check.
  2. GBT analizi — `scripts/analyze.py` (xgboost) feature importance + partial dependence. Manuel tune per-symbol threshold, factor weight, veto threshold.
  3. Opsiyonel RL — stable-baselines3 **sadece** GBT + manuel plateau ederse. Scope: parametre tuner, karar alıcı değil.

---

## Hızlı-referans tablosu

| Görev | Dosya | Fonksiyon |
|---|---|---|
| Tek tick | `src/bot/runner.py` | `BotRunner.run_once` |
| Tek parite cycle | `src/bot/runner.py` | `_run_one_symbol` |
| TF switch + settle | `src/bot/runner.py` | `_switch_timeframe`, `_wait_for_pine_settle` |
| Pine veri okuma | `src/data/structured_reader.py` | `read_market_state` |
| 5-pillar confluence | `src/analysis/multi_timeframe.py` | `score_direction`, `_apply_trend_regime_conditional` |
| ADX regime classifier | `src/analysis/trend_regime.py` | `compute_adx`, `classify_trend_regime` |
| Cross-asset snapshot | `src/bot/runner.py` | `_build_crypto_snapshot` |
| Zone setup seçimi | `src/strategy/setup_planner.py` | `build_zone_setup`, `ZoneSetup` |
| Plan build | `src/strategy/entry_signals.py` | `build_trade_plan_with_reason` |
| SL seçimi | `src/strategy/entry_signals.py` | `select_sl_price`, `_push_sl_past_htf_zone` |
| R:R sizing | `src/strategy/rr_system.py` | `calculate_trade_plan` |
| Reentry gate | `src/bot/runner.py` | `_check_reentry_gate` |
| LTF reversal close | `src/bot/runner.py` | `_is_ltf_reversal`, `_defensive_close` |
| Limit emir + PENDING | `src/execution/order_router.py` | `OrderRouter.place_limit_entry`, `cancel_pending_entry` |
| Position tracking | `src/execution/position_monitor.py` | `PositionMonitor.poll`, `register_pending`, `_detect_tp1_and_move_sl` |
| Close handling | `src/bot/runner.py` | `_handle_close` |
| Journal CRUD | `src/journal/database.py` | `record_pending`, `record_open`, `record_close`, `replay_for_risk_manager` |
| Rejected signals | `src/journal/database.py` | `record_rejected`, `update_rejected_outcome` |
| Circuit breakers | `src/strategy/risk_manager.py` | `RiskManager.can_trade`, `register_trade_closed` |
| Factor audit | `scripts/factor_audit.py` | per-symbol/session/regime WR + counter-factuals |
| Counter-factual peg | `scripts/peg_rejected_outcomes.py` | OKX history forward-walk → WIN/LOSS/NEITHER |
