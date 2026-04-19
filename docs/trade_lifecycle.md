# İşlem Stratejisi ve Algoritma Akışı

Bu doküman botun **nasıl karar verdiğini** anlatır. Emir geçerken hangi koşullara bakıyor, hangi filtreleri uyguluyor, kazananı nasıl yönetiyor, kaybı nasıl kapatıyor. Kurulum / CLI / Pine / OKX ayarı burada yok — bunlar için `CLAUDE.md`.

---

## Felsefe

Bot **sabırlı bir scalper**. Momentum kovalamıyor: sinyal gelse bile **zone beklemeye** geçiyor, zone'a fiyat gelirse post-only limit ile giriyor, gelmezse iptal ediyor. Amaç, confluence ≥ eşik olduğu anda market'e basmak değil; **iyi bir fiyata fill olmayı** beklemek.

Üç temel prensip:
1. **Yapı > momentum.** Market structure shift, liquidity pool, FVG, order block — bunlar hem giriş zone'u hem de stop seviyesi.
2. **Reject yüksek, fill düşük.** Hard gate'ler çoğu sinyali öldürür. Kalan azı da zone fill olmazsa timeout'a gider. Bu doğru — kötü trade almaktansa hiç trade almamak.
3. **Her reject kayıt altında.** Alınmayan sinyaller `rejected_signals` tablosuna yazılır, sonradan OKX tarihinden "bu reject aslında kazanan olur muydu?" diye retrospektif olarak puanlanır. Stratejinin yanlış atığı trade'leri geri izleme.

---

## İşlem akışı (üst seviye)

```
Tick (5s)
   │
   ├── 1) Kapanan pozisyonları işle (journal, R hesabı, slot boşalt)
   ├── 2) BTC + ETH snapshot'ı oluştur (cross-asset veto girdisi)
   ├── 3) PENDING limit emirleri kontrol et (fill? timeout? invalidation?)
   └── 4) 5 parite döngüsü (BTC → ETH → SOL → DOGE → XRP)
           │
           ├── Macro blackout? → skip
           ├── Açık pozisyon var mı? → sadece defensive close kontrolü
           ├── HTF context oku (15m) — sadece pozisyon yoksa
           ├── LTF confirm oku (1m)
           ├── Entry TF oku (3m) + ADX regime sınıflandır
           ├── 5-Pillar confluence skor hesapla
           ├── Hard gate'ler (premium/discount, displacement, EMA, VWAP, cross-asset)
           ├── Skor ≥ eşik + gate'ler geçti?
           │      │
           │      └── Evet → zone seç → limit emir yerleştir → PENDING
           │
           └── Reentry + risk gate'leri → risk manager halt?
```

5 parite × 4 slot: her cycle 1 parite slot beklemede kalır. Bu kasıtlı — confluence gate'in daha iyi sinyal seçmesine izin veriyor.

---

## Sinyal avı: 5-Pillar Confluence

Bot her cycle iki yönü ayrı ayrı skorlar: `BULLISH` ve `BEARISH`. Yüksek olan kazanır, `min_confluence_score=2.0` altıysa trade yok.

Skorlama 5 sütun üzerinde yapılır:

### 1. Market Structure
- **`mss_alignment` (0.75)** — Son market structure shift (BOS/CHoCH) aday yönü destekliyor mu?
- **`recent_sweep` (1.0)** — Son likidite sweep'i *reversal* sinyali veriyor mu? (Bearish sweep = bullish faktör; çünkü short'lar likid edildi, artık yukarı alan açıldı.)

### 2. Liquidity
- Pine'ın çizdiği standing liquidity pool'ları + Coinalyze heatmap cluster'ları.
- **`derivatives_heatmap_target` (0.5)** — Trade'in hedef yönünde büyük bir liq cluster var mı? Market buna yürür çünkü o likidite orada.

### 3. Money Flow
- **`money_flow_alignment` (0.6)** — MFI (Money Flow Index) bias'ı aday yönü destekliyor mu? `|rsi_mfi| ≥ min_rsi_mfi_magnitude` eşiği gerekli; zayıf MFI sinyali gürültü.

### 4. VWAP
- **`vwap_composite_alignment` (0.0 / 0.5 / 1.0)** — 3 session VWAP'ı (1m, 3m, 15m) aday yönü destekliyor mu?
  - Üçü birden align → 1.0
  - 2'si align → 0.5
  - 1'i align → 0
  - Hiçbiri / missing → 0
- Pre-pivot'ta 3'e bölünmüş halde toplam 0.6 veriyordu (`0.2 × 3`). Bu, tek bir TF aynı yöne bakınca sanki 3 ayrı sinyalmiş gibi çift-sayıma sebep oluyordu. Reconsolidate edildi.

### 5. Divergence
- **`divergence_signal` (1.0)** — Osilatörde regular veya hidden divergence var mı? Bar-ago decay ile ağırlığı taze sinyalde yüksek.
- **`oscillator_high_conviction_signal` (1.25)** — GOLD_BUY / BUY_DIV / SELL_DIV gibi yüksek güvenli sinyaller son 3 barda geldi mi? En yüksek ağırlıklı faktör.

### Demote edilmiş faktörler (audit için duruyor, skor vermez)
- **`at_order_block` → weight 0** — Pine 3m OB'ler Sprint 3'te %0 WR verdi. HTF (15m) varyantla geri ekleme deferred.
- **`htf_trend_alignment` (0.75)** → skorlamaya katılmıyor, sadece setup planner'a "bu bir long setup" diye yön girdisi. Ayrıca ADX regime path'ini besliyor.
- **`oscillator_signal` (0.75), `liquidity_pool_target` (0.5)** → Divergence pillar'ına / zone source'a emildi.

---

## Hard gate'ler (skora değil, reddetmeye dayalı)

5-Pillar skoru eşiği geçtikten sonra, şu gate'ler sırayla çalışır. Birinde fail → trade yok, `rejected_signals` tablosuna INSERT, döngü bir sonraki pariteye geçer.

| Gate | Reject reason | Mantık |
|---|---|---|
| **Premium/Discount Zone** | `wrong_side_of_premium_discount` | Son swing'in midpoint'ine göre: long'lar discount yarıda (altta), short'lar premium yarıda (üstte) olmalı. Yüksekten long almıyoruz. |
| **Displacement Candle** | `displacement_missing` | FVG tabanlı zone'lar için son 3-5 barda büyük gövdeli bir displacement istiyor. Yoksa o FVG "gerçek imbalance" değil, sadece grafikte boşluk. |
| **EMA Momentum Contra** | `ema_momentum_contra` | Long'lar: 21-EMA < 55-EMA + spread genişliyorsa blok (trend aşağı ve ivmeleniyor). Short'lar ayna. |
| **VWAP Misalignment** | `vwap_misaligned` | Strict: fiyat tüm mevcut session VWAP'lerin yanlış tarafındaysa veto. Opsiyonel flag (`analysis.vwap_hard_veto_enabled`). |
| **Cross-Asset Opposition** | `cross_asset_opposition` | Altcoin veto — aşağıda. |
| **Crowded Regime** | `crowded_skip` | Derivatives verisi "aşırı kalabalık" diyorsa (`|funding_z| ≥ 3.0`) ve biz o kalabalığa katılıyorsak blok. |
| **Session Filter** | `session_filter` | Per-symbol session allowlist (SOL/DOGE/XRP = sadece LONDON). |
| **Macro Blackout** | `macro_event_blackout` | USD HIGH-impact event ±30/15 dk içinde ise skip. |

---

## Yön seçimi + ADX Regime (koşullu ağırlık)

Pure confluence skoru yön verir, ama ADX regime o yönün *ne tür* bir işlem olacağını etkiler:

`src/analysis/trend_regime.py` her 3m cycle'da ADX hesaplar (Wilder smoothing, 14 period). Sonuç:

- **`UNKNOWN`** — yeterli veri yok veya degenerate bar'lar (flat chart).
- **`RANGING`** — ADX < 20. Yatay market; reversal setup'ları daha iyi çalışır.
- **`WEAK_TREND`** — 20 ≤ ADX < 30. Karışık; ağırlık değişimi yok.
- **`STRONG_TREND`** — ADX ≥ 30. Güçlü yön; trend-continuation setup'ları tercih edilir.

Koşullu ağırlık (`trend_regime_conditional_scoring_enabled: true`):

| Regime | `htf_trend_alignment` | `recent_sweep` |
|---|---|---|
| `STRONG_TREND` | ×1.5 | ×0.5 |
| `RANGING` | ×0.5 | ×1.5 |
| `WEAK_TREND` / `UNKNOWN` | ×1.0 | ×1.0 |

Mantık: trend güçlüyken trend ile gitmek, range'de sweep/reversal avlamak. Bot aynı yapısal yapıya farklı market karakterinde farklı tepki verir.

Her trade'in `trend_regime_at_entry` değeri journal'a yazılır — sonraki analiz "BTC STRONG_TREND altında ortalama %X WR" gibi rejim bazlı ayrıştırma yapabilir.

---

## Cross-Asset Veto (altcoin koruması)

BTC ve ETH "market pillar" sayılıyor; SOL/DOGE/XRP onların rejim değişimlerine tabi.

Her cycle başında `CryptoSnapshot` inşa edilir:
```python
CryptoSnapshot:
    btc_15m_trend: BULLISH / BEARISH / NEUTRAL
    eth_15m_trend: ...
    btc_3m_momentum: son 5 bar % değişim
    eth_3m_momentum: ...
```

Altcoin (SOL/DOGE/XRP) cycle'ları bunu okur ve:
- **LONG + BTC 15m BEARISH + ETH 15m BEARISH** → veto. Alt'larda long denerken BTC+ETH aşağı gidiyorsa 3. parti hareketine güvenme.
- **SHORT + BTC 15m BULLISH + ETH 15m BULLISH** → veto. Sprint 3'te SOL/DOGE short'ları BTC+ETH yukarı dönünce squeeze yedi; bu veto o deneyimin ürünü.

**İki pillar da zıt olmalı** — sadece biri zıtsa sektör rotasyonu geçer. BTC rip ederken ETH flat ise altcoin için nötr context olarak okunur.

BTC ve ETH'in kendisinde cross-asset veto yok — aralarındaki divergence zaten context.

---

## Zone-Based Entry (işleme giriş)

Confluence skoru ≥ eşik + tüm gate'ler geçti. Şimdi iş market emre basmak değil, **doğru zone'u** seçip orada beklemek.

### Zone seçimi (öncelik sırası)

`src/strategy/setup_planner.py:build_zone_setup` 4 kaynakta sırayla arar, ilk bulunan kazanır:

1. **`liq_pool`** — Coinalyze'ın bulduğu unswept liquidity pool + premium/discount eşleşmesi. Long'lar discount yarıda bir pool'un altına limit koyar (pool süpürülürse fiyat geri döner varsayımı), short'lar tersine.
2. **`fvg_htf`** — HTF (15m) doldurulmamış FVG; fiyat dışarıdan yaklaşıyor + displacement candle gate geçti. Limit FVG'nin kenarına konur.
3. **`vwap_retest`** — Aktif session VWAP (genellikle 1m veya 3m); fiyat aynı yönde pullback yapıyor, VWAP'a dönüşü bekleniyor.
4. **`sweep_retest`** — Son swing'in likiditesi süpürüldü, sonra içeri kapandı. Reversal setup'ı.

Seçilen zone `ZoneSetup` dataclass'ına paketlenir:
- `entry_zone: (low, high)` — limit fiyat aralığı
- `trigger_type: "zone_touch" | "sweep_reversal" | "displacement_return"`
- `sl_beyond_zone` — SL zone'un dışında, yapısal
- `tp_primary` — ilk liquidity target veya HTF zone
- `max_wait_bars` — sabırsızlık limiti
- `zone_source` — yukarıdaki 4'ten hangisi

### Limit emir yerleştirme

**Post-only** tercihli (maker fee avantajı). Post-only reddedildiğinde (fiyat zaten yanlış tarafta) regular limit'e fallback. Hiçbiri olmazsa son çare market-at-zone-edge (acil durum kaçışı; normal akışta nadirdir).

Emir yerleştirildi → position monitor'e `PENDING` olarak kaydolur. Canlı pozisyon henüz yok.

### PENDING yaşam döngüsü

Her tick'te (`_process_pending_setups`):
- **Fill oldu** → `PENDING → FILLED → OPEN`. OCO (TP1/TP2) şimdi yerleştirilir. Journal `zone_fill_latency_bars` damgalar.
- **`max_wait_bars` doldu, fill yok** → cancel. `reject_reason=zone_timeout_cancel`. Zone bekledi ama fiyat gelmedi, fırsat geçti.
- **Zone ihlal edildi (fiyat SL seviyesini fill olmadan geçti)** → cancel. `reject_reason=pending_invalidated`. Fiyat zone'a değmeden kötü tarafa kaçtı, trade geçerliliği bitti.

**`max_concurrent_setups_per_symbol=1`** — Aynı sembolde PENDING limit + canlı pozisyon birlikte olamaz. Her sembol için tek slot, spam koruması.

---

## Stop Loss seçimi

SL yapısal. Sırayla denenir, ilk hit = SL:

1. **Zone-SL** — Setup planner bir `ZoneSetup` ürettiyse, SL `sl_beyond_zone` değeridir (zone yapısının dışında).
2. **Pine OB** — Chart'a çizili en yakın geçerli Pine order block kutusu.
3. **Pine FVG** — Pine'ın çizdiği fair value gap.
4. **Python OB** — Python'un kendi tarafında yeniden tespit ettiği OB.
5. **Python FVG** — Python-computed FVG.
6. **Swing lookback** — Son N barın (BTC/ETH=20, DOGE/XRP=30) ekstremi.
7. **ATR fallback** — entry ± 2×ATR (son çare).

Her seviye **buffer** ile itilir: `sl = level ± 0.2 × ATR`. Seviye tam üzerine değil, biraz ötesine — wick koruması.

### HTF S/R sıkıştırma

15m S/R zone'u SL ile entry arasına denk gelirse, SL zone'un uzak kenarının hemen dışına snap edilir. **Sadece sıkıştırır, genişletmez** — risk artmaz, sadece bazı trade'lerde daha sıkı SL.

### Per-symbol SL taban'ı (acil durum)

`min_sl_distance_pct_per_symbol`:
- BTC `0.005` (%0.5)
- ETH `0.010` (%1.0 — yüksek beta)
- SOL `0.008`
- DOGE/XRP `0.007`

Eğer yapısal SL bu tabanın altındaysa **SL genişletilir, trade reddedilmez**. Notional otomatik küçülür (`risk_amount / sl_pct`) → R sabit kalır, sadece pozisyon daha küçük. Mantık: yüksek kaldıraçta 0.05-0.1%'lik OB stop'u anında wick'lenir, genişletip nefes alanı vermek gerekli.

---

## Take Profit + Partial TP + SL-to-BE

### TP fiyatı
- **Zone-TP** — Zone setup varsa `tp_primary` (ilk liquidity target veya HTF zone). Sabit-R değil; "fiyat nereye doğal olarak gider" mantığı.
- **Fallback** — R bazlı: `tp = entry ± (sl_distance × rr_ratio)`. Default `rr_ratio=1.5` (Sprint 3'ten kalma loose değer).

### Partial TP (default ON)

Pozisyon iki parçaya bölünür:
- **TP1** — %50'si, entry'den 1.5R mesafede. Hızlı kâr al, risk'i yarıya indir.
- **TP2** — %50'si, orijinal TP fiyatında.

İkisi de OCO algosu olarak OKX'e gider. Biri fail ederse ikisi cancel + pozisyon kapatılır — **gate fail loud**, sessiz fallback yok.

Eğer `num_contracts * 0.5` integer olmazsa (örn. 1 kontrat → 0.5 bölünemez) → trade **en baştan reddedilir** (`insufficient_contracts_for_split`). "Yarım kontrat olmaz, tam kontratlı bir pozisyon aç, yoksa hiç açma" disiplini.

### TP1 fill sonrası: SL-to-BreakEven

TP1 dolar dolmaz:
1. TP2 algo'yu cancel et.
2. Yeni OCO yerleştir: SL = entry ± `sl_be_offset_pct` (default %0.1), TP = orijinal tp2_price, size = kalan kontrat.

SL entry'nin tam üstüne (long için) veya tam altına (short için) değil, **fee-buffered** — taker fee + slippage için tampon. Fiyat entry'ye geri değse bile kalan kontratın exit fee'si karşılanır.

### SL-to-BE spin-proof

2026-04-18'de BTC'de SL-to-BE retry loop'u bir pozisyonu korumasız bıraktı. Fix:

- Cancel ve place ayrı try-block'larda.
- OKX'in `51400/51401/51402` kodları cancel'da → idempotent success sayılır, BE OCO yine yerleştirilir.
- Generic cancel fail → 3 denemede pes, `be_already_moved=True` set → poll artık hammer'lamaz.
- Cancel başarılı ama place fail → pozisyon korumasız, CRITICAL log, TP2 algo_ids'den düşürülür, callback fire edilir. **Emergency market-close otomatik değil** — operator karar verir.

---

## Risk ve sizing

### R hesabı

- `risk_amount = total_equity × risk_pct`. Default `risk_pct=0.01` (%1). $5k demo → $50 risk per trade (1R).
- Kaldıraç her pozisyonda dinamik: `required_lev = ideal_notional / per_slot_margin`. Per-slot margin = `total_eq / 4` (cross margin).
- **Liq safety:** kaldıraç `floor(0.6 / sl_pct)`'den büyük olamaz — SL liq mesafesinin %60'ı içinde kalır, %40 bakım + mark drift buffer.
- **Margin safety:** notional `margin_balance × lev × 0.95`'tan büyük olamaz — %5 fee/drift tampon.

### Per-symbol leverage cap
- BTC 75x
- ETH 30x (demo flash-wick'leri 30x üstünü patlatıyor)
- SOL 50x (OKX cap)
- DOGE/XRP 30x

### Fee-aware sizing
Sizing paydasına `fee_reserve_pct=0.001` eklenir. Yani bot sizing yaparken sl_pct'i yüzde 0.1 daha geniş sayar. Bu sayede stop-out olduğunda giriş + çıkış taker fee'lerinden sonra gerçek kayıp $R civarında kalır (daha fazla değil).

### 4 slot

`max_concurrent_positions=4`. 5 parite 4 slot için yarışır. Margin budget `per_slot ≈ $1250` $5k demo'da. R hâlâ total_eq'nun %1'i — sadece notional tavan %25 küçülür.

---

## Reentry kuralları

Bir trade kapandıktan sonra aynı (symbol, side) için hemen yeni trade yok. 4 sıralı kapı (per symbol+side):

1. **Cooldown** — `min_bars_after_close × entry_tf_seconds`. Default 3 bar × 180s = 9 dakika. Taze kapanıştan sonra market'in toparlanmasını bekle.
2. **ATR move** — Fiyat son çıkıştan en az `0.5 × ATR` hareket etmeli. Aynı yerde tekrar aynı trade yok.
3. **Post-WIN quality** — Önceki trade WIN ise: yeni confluence kesinlikle **daha yüksek** olmalı. Kazanan seri bot'u aç gözlü yapmasın.
4. **Post-LOSS quality** — Önceki trade LOSS ise: yeni confluence **eşit veya yüksek** olmalı. Kayıp sonrası zayıf sinyal yok.

BREAKEVEN kapanışlar quality gate'i bypass eder — R = 0, yeterince bilgi yok, yeniden dene.

**Karşıt yönler izole**. BTC long kapatmak BTC short açmayı gate'lemez; farklı hikâye.

---

## Defensive Close (pozisyon koruma)

Bir pozisyon açıkken LTF (1m) market yön değiştirirse ne olur? `_is_ltf_reversal` kontrolü:

- Pozisyon yaşı `ltf_reversal_min_bars_in_position × tf_seconds`'ten fazla mı? (Taze pozisyona gelişme zamanı ver.)
- 1m oscillator trend + son signal açık pozisyonun zıttı ve taze mi?

İkisi de evetse `_defensive_close`:
- Tüm tracked algo'ları cancel et.
- Market close pozisyon.
- Journal'a `close_reason=ltf_reversal` damgala.

Idempotency flag (`defensive_close_in_flight`) tekrar tetiklenmeyi önler.

---

## Circuit breaker'lar (ne zaman bot durur)

Her trade kapandığında risk manager kontrol eder (ilk eşleşme kazanır):

| Tetikleyici | Davranış |
|---|---|
| Drawdown ≥ `max_drawdown_pct` (şu an %40, eski %25) | **Kalıcı halt** — `--clear-halt` flag'i ile manuel restart |
| Günlük realize kayıp ≥ `max_daily_loss_pct` (şu an %40, eski %15) | 24 saat halt |
| Ardışık kayıp ≥ `max_consecutive_losses` (şu an 9999, eski 5 — efektif kapalı) | 24 saat halt |
| Açık pozisyon ≥ `max_concurrent_positions` (4) | O cycle'da yeni trade blokla |
| Plan bazlı: lev > max, RR < `min_rr_ratio` (şu an 1.5, eski 2.0), contracts == 0 | Trade reddedilir |

Değerler Phase 8 veri toplama süresince loose — en ufak kayıpta bot halt olmasın, stratejiyi yeterli veri üzerinde görelim diye. 20+ post-pivot kapanmış trade sonra 5/15/25/2.0'a restore.

---

## Kapanış + öğrenme

Pozisyon kapandığında:

1. **OKX'ten enrich** — gerçek `realizedPnl`, `closeAvgPx`, `fee`, `uTime`. Onsuz her close BREAKEVEN görünürdü ve breaker'lar hiç tetiklenmezdi.
2. **Journal'a yaz** — `exit_price`, `pnl_usdt`, `pnl_r = pnl_usdt / risk_amount`, `outcome` (WIN/LOSS/BREAKEVEN), `close_reason`, `trend_regime_at_entry`, `zone_fill_latency_bars`.
3. **Risk manager güncelle** — `current_balance += pnl_usdt`, `peak_balance` revize, `consecutive_losses` 0 veya +1.
4. **Reentry state güncelle** — `last_close[(symbol, side)]` = son confluence, fiyat, outcome, zaman. Sonraki reentry gate bunu kullanır.

### Rejected signal counter-factual

Alınmayan her sinyal `rejected_signals` tablosunda satır. `scripts/peg_rejected_outcomes.py` OKX history'den ileriye yürür ve her reject için hipotetik outcome stampler:
- Eğer trade alınsaydı TP'ye mi, SL'e mi değerdi?
- Aynı bar'da ikisi de touch → LOSS (muhafazakâr).
- Hiçbiri `max_wait_bars` içinde touch etmedi → NEITHER.

Bu veri "reject gate'lerim aslında doğru trade'leri de atıyor mu?" sorusuna cevap verir. Her 10 kapanışta bir `scripts/factor_audit.py` bu sayıları toplar ve per-faktör actual-vs-hypothetical WR karşılaştırması yapar. Eğer bir reject reason hipotetik olarak yüksek WR veriyorsa, o reject gate'i ayarlanmalı.

---

## Özet: karar ağacı

```
Tick geldi
  ↓
Açık pozisyon var mı?
  ├── Evet → LTF reversal? → evetse defensive close → return
  └── Hayır → devam
           ↓
        Macro blackout? → evet → skip
           ↓
        HTF + LTF + Entry TF oku (3 pass)
           ↓
        ADX regime sınıflandır
           ↓
        5-Pillar skor hesapla (her iki yön için ayrı)
           ↓
        En yüksek yönün skoru ≥ 2.0?
           ├── Hayır → below_confluence reject → INSERT rejected_signals → return
           └── Evet → devam
                   ↓
                Hard gate'ler sırayla:
                  premium/discount → displacement → EMA → VWAP → cross-asset → session → crowded
                   ↓
                Herhangi biri fail? → reject → INSERT → return
                   ↓
                Zone seç (liq_pool > fvg_htf > vwap_retest > sweep_retest)
                   ↓
                Zone bulunamadı? → no_setup_zone reject → return
                   ↓
                SL + TP hesapla, sizing yap
                   ↓
                Reentry gate'leri geç?
                  ├── Hayır → reject → return
                  └── Evet → risk manager can_trade?
                              ├── Hayır → blocked → return
                              └── Evet → post-only limit emir yerleştir
                                          ↓
                                     PENDING tracker'a ekle
                                          ↓
                               (Sonraki tick'lerde fill / timeout / invalidation izlenir)
                                          ↓
                                       FILL oldu → OCO (TP1/TP2) yerleştir → OPEN
                                          ↓
                                       TP1 hit → SL-to-BE replace → partial close
                                          ↓
                                       TP2 hit veya SL hit veya defensive close → CLOSED
                                          ↓
                                       Journal + risk manager + reentry state update
```
