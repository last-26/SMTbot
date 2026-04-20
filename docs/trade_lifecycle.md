# İşlem Stratejisi ve Algoritma Akışı

Bu doküman botun **nasıl karar verdiğini** ve **trade anında ne yaptığını** anlatır. Kurulum / CLI / Pine / OKX ayarı burada yok — bunlar için `CLAUDE.md`. Tarihli değişim notları ve sebepler için `CLAUDE.md` changelog bölümü.

---

## Felsefe

Bot **sabırlı bir scalper**. Momentum kovalamıyor: sinyal gelse bile **zone beklemeye** geçiyor, zone'a fiyat gelirse post-only limit ile giriyor, gelmezse iptal ediyor. Amaç market'e basmak değil; **iyi fiyata fill olmayı** beklemek.

Üç prensip:
1. **Yapı > momentum.** MSS/BOS, liquidity pool, FVG, OB, VWAP bandı — hem giriş zone'u hem de yapısal SL kaynağı.
2. **Reject yüksek, fill düşük.** Hard gate'ler sinyallerin çoğunu öldürür. Kalanın çoğu da fill olmadan timeout'a gider. Bu doğru — kötü trade almaktansa hiç trade almamak.
3. **Her reject kayıt altında.** Alınmayan sinyaller `rejected_signals` tablosuna yazılır; OKX tarihinden retrospektif outcome stamp'leri Phase 9 GBT'ye "bu reject aslında kazanan mıydı?" cevabını verir.

---

## Tick döngüsü (üst seviye)

```
Tick (~5s)
  │
  ├── 1) Kapanan pozisyonları işle (enrich, journal, Binance cross-check, R hesabı, slot boşalt)
  ├── 2) BTC + ETH snapshot'ı oluştur (cross-asset veto girdisi)
  ├── 3) PENDING limit emirleri tara (inline drain — her sembolden önce)
  └── 4) 5 parite döngüsü (BTC → ETH → SOL → DOGE → BNB)
          │
          ├── Macro blackout? → skip
          ├── Açık pozisyon var mı?
          │     ├── Evet → LTF reversal / MFE SL lock / dynamic TP revise / dedup kontrolü
          │     └── Hayır → HTF(15m) + LTF(1m) + Entry(3m) oku, ADX regime sınıflandır
          ├── 5-pillar confluence (her iki yön için ayrı skor)
          ├── Hard gate'ler
          ├── Zone seç → SL/TP/sizing → reentry + risk gate
          └── Post-only limit → PENDING tracker
```

5 parite × 5 slot: artık slot kuyruğu yok, her sembol paralel taşıyabilir. Gate'ler setup kalitesini seçer; döngü zaman bütçesine (3m TF → 180s) sığar.

PENDING drain'in her semboldan önce çalışması kritik: fill → OCO ekleme gecikmesi eski tek-seferlik pattern'de 180-240s'e kadar çıkıyordu, şimdi tekli saniyeler.

---

## 5-Pillar Confluence

Her cycle iki yön ayrı ayrı skorlanır: `BULLISH` ve `BEARISH`. Yüksek olan kazanır, `min_confluence_score=3.0` altıysa trade yok.

Beş sütun (ağırlıklar `config/default.yaml`'da, burada özet):

| Pillar | Temsil eden faktörler |
|---|---|
| **Market Structure** | `mss_alignment`, `recent_sweep` |
| **Liquidity** | Pine standing pool'ları + Coinalyze heatmap cluster'ları + sweep-reversal |
| **Money Flow** | `money_flow_alignment` (MFI bias) |
| **VWAP** | `vwap_composite_alignment` (1m/3m/15m hepsi align → 1.0, 2/3 → 0.5, 1/3 → 0). `vwap_1m_alignment` 0.2 probe ağırlığında açık. |
| **Divergence** | `divergence_signal` + `oscillator_high_conviction_signal` (en yüksek ağırlık) |

`htf_trend_alignment` skorlamaya girmez — sadece setup planner'a "bu bir long mu short mu" girdisi ve ADX rejim path'i için.

### ADX rejimi (koşullu ağırlık)

Wilder ADX (14) her 3m cycle'da hesaplanır:
- `STRONG_TREND` (ADX ≥ 30) — trend-continuation faktörleri ×1.5, sweep ×0.5.
- `RANGING` (ADX < 20) — mirror; sweep/reversal ağırlığı artar.
- `WEAK_TREND` / `UNKNOWN` — nötr.

Her trade'in `trend_regime_at_entry` değeri journal'a yazılır. Phase 9 GBT rejim-bazlı WR'ı buradan okur.

---

## Hard gate'ler (skordan sonra, reject tabanlı)

Confluence eşiği geçtikten sonra gate'ler sırayla çalışır. Birinde fail → trade yok, `rejected_signals`'e yazılır, sonraki sembole geçilir.

| Gate | Reject reason | Durum |
|---|---|---|
| Displacement Candle | `displacement_missing` | aktif |
| EMA Momentum Contra | `ema_momentum_contra` | aktif |
| VWAP Misalignment | `vwap_misaligned` | aktif |
| Cross-Asset Opposition | `cross_asset_opposition` | aktif (altcoin short'lar BTC+ETH 15m BULLISH ise veto; long'lar aynası) |
| Crowded Regime | `crowded_skip` | aktif (`|funding_z| ≥ 3.0` + kalabalığa katılıyoruz → blok) |
| Session Filter | `session_filter` | aktif (SOL/DOGE LONDON-only) |
| Macro Blackout | `macro_event_blackout` | aktif (USD HIGH-impact ±30/15 dk) |
| Premium/Discount Zone | `wrong_side_of_premium_discount` | **şu an kapalı** — Phase 9 sonrası soft (weighted, ~%10-15) olarak geri dönecek |
| HTF S/R Ceiling | `htf_tp_ceiling` | **şu an kapalı** — 1:3 hard cap ile çatışıyordu; Phase 9 factor audit'ten sonra karar |

---

## Zone seçimi (öncelik sırası)

Confluence + gate'ler geçti, şimdi *doğru zone*'u bekleyeceğiz. `setup_planner.build_zone_setup` 5 kaynakta sırayla arar, ilk bulunan kazanır:

1. **`vwap_retest`** — Aktif session VWAP ±1σ bandı (Pine 3m band tablosu). Band varsa zone = `(vwap, upper)` long için, `(lower, vwap)` short için; entry zone mid (`vwap ± 0.5σ`). Band yoksa ATR-half-band fallback.
2. **`ema21_pullback`** — EMA21/55 stack yön ile aligned, fiyat EMA21'e `zone_atr × ATR` içinde.
3. **`fvg_entry`** — Entry TF (3m) doldurulmamış FVG. HTF (15m) FVG opt-in flag arkasında.
4. **`sweep_retest`** — Son swing likiditesi süpürüldü, sonra içeri kapandı (reversal setup).
5. **`liq_pool_near`** — Coinalyze unswept liq pool. **Giriş için gate'li**: `liq_entry_near_max_atr=1.5` mesafesi + `liq_entry_magnitude_mult=2.5× side-median` notional. Düşük notional pool'lar giriş için kullanılmaz, sadece TP kaynağı olarak yaşar.

Seçilen zone `ZoneSetup`'a paketlenir: `entry_zone`, `trigger_type`, `sl_beyond_zone`, `tp_primary`, `max_wait_bars`, `zone_source`. Liq pool + FVG heatmap pool'ları varsa `tp_ladder` (shares `[0.40, 0.35, 0.25]`) oluşur; yoksa tek-leg fallback.

### Limit emir yerleştirme

**Post-only** tercihli (maker fee). Fiyat yanlış tarafta ise regular limit fallback. Son çare market-at-zone-edge (nadir).

---

## Limit emrin yaşamı: PENDING → FILLED → OPEN

Her tick `_process_pending_setups` şu durumlara bakar:

- **Fill oldu** → `OPEN`. `order_router.attach_algos` ile OCO yerleştirilir. Attach öncesi **mark-vs-SL guard** çalışır: mark zaten SL tarafına geçmişse attach iptal, pozisyon best-effort market-close (aksi halde korumasız pozisyon).
- **`max_wait_bars` doldu** → cancel. `zone_timeout_cancel`.
- **Fiyat zone'a değmeden SL tarafına kaçtı** → cancel. `pending_invalidated`.

`max_concurrent_setups_per_symbol=1` — aynı sembolde PENDING + OPEN aynı anda yok.

---

## Pozisyon yönetimi (canlı pozisyon açıldıktan sonra)

Bir sembolde OPEN pozisyon varken her tick şunlar çalışır:

### 1. Dynamic TP revision (`tp_dynamic_enabled: true`)

Runner OCO'nun TP'si her cycle `entry ± 3 × plan_sl_distance`'a re-anchor edilir. `plan_sl_distance` immutable (`plan_sl_price`, BE sonrası değişmez); bu sayede SL-to-BE sonrası "sl_distance = 0" → degenerate TP hatası olmaz.

Gate'ler: ATR delta (`tp_revise_min_delta_atr=0.5`) ve cooldown (`tp_revise_cooldown_s=30s`) ile OCO churn'ü engellenir. RR mark'a göre `tp_min_rr_floor=1.5` altına düşerse revise skip.

Cancel+place. Cancel `51400/51401/51402` → idempotent + `list_pending_algos` ile gerçekten gittiğini doğrula (demo 51400-but-still-live davranışı). Place fail → CRITICAL log, pozisyon korumasız — auto-close yok, operator karar.

### 2. MFE-triggered SL lock (`sl_lock_enabled: true`, 2026-04-20)

Pozisyon en az `sl_lock_mfe_r=2.0` R ilerlediyse (MFE), SL bir kere `entry ± sl_lock_at_r × sl_distance` seviyesine çekilir. Default `sl_lock_at_r=0.0` → BE + fee buffer. One-shot (`sl_lock_applied=True`); TP tarafının yanlış tarafına düşen new_sl reddedilir.

Amacı: TP'ye yakın dönen pozisyonlar full -1R'a round-trip yapmasın. 2R ilerledikten sonra kalan 1R ödül risk-siz olur.

### 3. LTF (1m) defensive close (`ltf_reversal_close`)

1m oscillator trend + taze signal pozisyonun zıttı, pozisyon yaşı minimum eşiği geçmişse:
- Tracked algo'lar cancel edilir.
- Pozisyon market close.
- `close_reason=ltf_reversal`.

Idempotency flag (`defensive_close_in_flight`) ile tekrar tetik yok.

### 4. Dedup (aynı sembolde yeni PENDING açma)

OPEN pozisyon varken yeni confluence cycle'ı setup planner'a kadar gitmez — sadece defensive gate'ler çalışır.

---

## Stop Loss

SL **yapısal**. Hiyerarşi sırayla denenir, ilk hit kazanır: zone-SL → Pine OB → Pine FVG → Python OB → Python FVG → swing lookback (BTC/ETH=20, alt=30) → ATR fallback. Her seviye `sl = level ± 0.2 × ATR` buffer'la itilir (wick koruma).

**Per-symbol SL taban'ı** (min_sl_distance_pct_per_symbol): BTC 0.004, ETH 0.006, SOL 0.010, DOGE 0.008, BNB 0.005. Yapısal SL bunun altındaysa **genişletilir**, trade reject edilmez. Notional `risk_amount / sl_pct` ile otomatik küçülür; R sabit kalır.

Zone-path'te de aynı taban uygulanır (`apply_zone_to_plan(min_sl_distance_pct=...)`), zone tighten'ı emniyet floor'unu bypass edemez.

---

## Take Profit: hard 1:3 + tek-leg OCO

- **Partial TP kapalı** (`partial_tp_enabled=false`). Pozisyon tek-leg; full 3R kazanç ya da full -1R kayıp. "Almost-win" (TP1 dokundu, TP2 geri döndü → +0.75R) bucket'ı artık yok.
- **Hard 1:3 RR cap** (`execution.target_rr_ratio=3.0`, `trading.default_rr_ratio=3.0`). Heatmap'ten gelen zone TP'si bu cap'e clamp'lanır. Guard test knob drift'i yakalar.
- **OCO trigger = mark-price** (`algo_trigger_px_type="mark"`) — OKX demo'nun fake wick'leri mark-price'a değmez, SL/TP sadece gerçek hareket ile tetiklenir.
- **SL-to-BE kod yolu hayatta** ama partial kapalı olduğu için TP1 fire etmediğinden inert. Partial'ı geri açmak tek YAML flip'idir.

Break-even WR: partial off → 1/(1+3) = **%25** (pre-fee). Winners $165 taşır (1R ≈ $55 nominal, $5.5k demo'da).

---

## Sizing

`rr_system.plan_and_size_entry`:

- `risk_amount = total_equity × risk_pct=0.01`. $5k demo → $50 = 1R.
- **Fee-aware ceil**: `num_contracts = math.ceil(max_risk / per_contract_cost)` where `per_contract_cost = (sl_pct + fee_reserve_pct) × contracts_unit_usdt`. Realize kayıp (fiyat + fee reserve) her sembolde **≥ target_risk**; overshoot en geniş sembolün bir `per_contract_cost` adımı kadar (< $3.50). Eski floor-rounding $40-$54 spread'i verirdi, şimdi $55-$58 bandında.
- **Liq safety**: kaldıraç `floor(0.6 / sl_pct)`'ten büyük olamaz.
- **Margin safety**: notional `margin_balance × lev × 0.95`'i geçemez.
- Capped path (leverage/margin floor binding) ise floor'a düşer; `max_contracts_by_notional=0` honest propagate → `zero_contracts` reject.

Per-symbol leverage cap: BTC 75x, ETH 30x, SOL 50x, DOGE 30x, BNB 50x.

---

## Reentry

Bir trade kapandıktan sonra aynı `(symbol, side)` için 4 sıralı kapı:

1. **Cooldown** — `min_bars_after_close × entry_tf_seconds` (3 bar × 180s = 9 dk).
2. **ATR move** — `≥ 0.5 × ATR` fiyat hareketi son çıkıştan.
3. **Post-WIN quality** — yeni confluence **daha yüksek** olmalı.
4. **Post-LOSS quality** — yeni confluence **eşit veya yüksek** olmalı.

BREAKEVEN quality gate'i bypass eder. Karşıt yönler izole (BTC long close → BTC short open'ı gate'lemez).

---

## Circuit breakers (ne zaman bot durur)

Her kapanışta risk manager sırayla kontrol eder:

| Tetikleyici | Davranış |
|---|---|
| Drawdown ≥ `max_drawdown_pct` (şu an %40, hedef %25) | **Kalıcı halt** — `--clear-halt` ile manuel restart |
| Günlük realize kayıp ≥ `max_daily_loss_pct` (şu an %40, hedef %15) | 24 saat halt |
| Ardışık kayıp ≥ `max_consecutive_losses` (şu an 9999, hedef 5 — efektif kapalı) | 24 saat halt |
| Açık pozisyon ≥ `max_concurrent_positions=5` | O cycle'da yeni trade blokla |
| Plan-level: lev > max, RR < `min_rr_ratio=1.5`, contracts==0 | Trade reject |

Değerler Phase 8 veri toplama boyunca loose — 20+ post-pivot kapanmış trade sonra 5/15/25/2.0'a restore edilecek.

---

## Kapanış + artefact cross-check

Pozisyon kapandığında:

1. **OKX enrich** — `/account/positions-history`'den gerçek `realizedPnl`, `closeAvgPx`, `fee`, `uTime`. Onsuz her close BREAKEVEN görünürdü, breaker'lar hiç tetiklenmezdi.
2. **Binance cross-check** (`artefact_check_enabled=true`) — Entry ve exit timestamp'leri için Binance USD-M 1m kline çekilir, fiyatlar `±0.0005` toleransla kline bandında mı diye bakılır. Herhangi bir taraf bandın dışındaysa `demo_artifact=1` + reason (`exit_above_binance_high` gibi) yazılır. Feed-down → flag `NULL` (tri-state). Hata silent — journal close asla bozulmaz.
3. **Journal** — `exit_price`, `pnl_usdt`, `pnl_r`, `outcome`, `close_reason`, `trend_regime_at_entry`, `zone_fill_latency_bars`, `demo_artifact`, `artifact_reason`.
4. **Risk manager** — `current_balance += pnl_usdt`, `peak_balance` revize, `consecutive_losses` 0/+1.
5. **Reentry state** — `last_close[(symbol, side)]` = son confluence, fiyat, outcome, zaman.

Raporlama: `scripts/report.py --exclude-artifacts` demo wick artefact'lerini dışarıda bırakarak temiz PnL verir.

### Rejected signal counter-factual

Alınmayan her sinyal `rejected_signals`'de satır. `scripts/peg_rejected_outcomes.py --commit` OKX history'den ileriye yürür, her reject için hipotetik outcome (TP'ye mi, SL'e mi, NEITHER mi) stamp'ler. `scripts/factor_audit.py` her 10 kapanışta per-faktör actual-vs-hypothetical WR karşılaştırması verir — bu reject gate'lerimin gerçekten doğru trade'leri mi attığının cevabı.

---

## Özet karar ağacı

```
Tick
  ↓
Kapanan pozisyonları işle → enrich → Binance cross-check → journal
  ↓
BTC/ETH snapshot
  ↓
PENDING drain
  ↓
[Her sembol için]
  Macro blackout? → skip
  ↓
  Açık pozisyon var mı?
    ├── Evet →
    │     Dynamic TP revise (ATR/cooldown gate)
    │     MFE ≥ 2R? → SL'i BE+fee'ye çek (one-shot)
    │     LTF reversal? → defensive close → return
    │     Dedup → return
    │
    └── Hayır →
          HTF(15m) + LTF(1m) + Entry(3m) oku
          ADX regime sınıflandır
          5-pillar skor (iki yön)
          Max skor ≥ 3.0? → Hayır → below_confluence → INSERT rejected
          Hard gate'ler sırayla → fail? → INSERT rejected
          Zone seç (vwap_retest > ema21_pullback > fvg_entry > sweep_retest > liq_pool_near)
          Zone yok? → no_setup_zone
          SL (hiyerarşi + per-symbol floor) + TP (hard 1:3 cap) + ceil sizing
          Reentry gate'leri → fail? → reject
          Risk manager can_trade? → Hayır → blocked
          ↓
          Post-only limit → PENDING tracker
          ↓
          [Sonraki tick'lerde]
            Fill → mark-vs-SL guard → OCO attach → OPEN
            Timeout → zone_timeout_cancel
            Invalidation → pending_invalidated
          ↓
          OPEN → pozisyon yönetimi (TP revise / MFE lock / LTF reversal)
          ↓
          TP hit / SL hit / defensive close → CLOSED
          ↓
          Enrich + Binance cross-check + journal + risk mgr + reentry state
```
