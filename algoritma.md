# algoritma.md — Kanonik Strateji Spesifikasyonu

> **Son güncelleme:** 2026-04-20 &nbsp;|&nbsp; **CLAUDE.md hash:** `1929b66`
>
> Bu dosya, botun **an-itibarıyla çalışan** karar algoritmasının referans-manuelidir. *Niye* ve *ne zaman* sorularının tarihli cevapları [`CLAUDE.md`](./CLAUDE.md) changelog'undadır; tick-seviye akış hikâyesi [`docs/trade_lifecycle.md`](./docs/trade_lifecycle.md) dosyasındadır. **Bu dosya her zaman CLAUDE.md ile senkron kalmalıdır** — senkron dışı bıraktığınızda `.claude/settings.json` Stop hook'u uyarı verir, `git commit` yaparken de PreToolUse hook'u blok atar (bkz. §16).
>
> Dil: Türkçe. Kod içindeki string'lerle birebir eşleşmesi gereken İngilizce isimler (`displacement_candle`, `cross_asset_opposition`, `below_confluence`, ...) aynen korunur.

---

## İçindekiler

1. [Mimari üst-akış](#1-mimari-üst-akış)
2. [Yön tespiti (direction resolution)](#2-yön-tespiti-direction-resolution)
3. [5-Pillar confluence skorlaması](#3-5-pillar-confluence-skorlaması)
4. [Rejim-koşullu skorlama (ADX)](#4-rejim-koşullu-skorlama-adx)
5. [Hard gates referansı](#5-hard-gates-referansı)
6. [Zone kaynağı spesifikasyonu](#6-zone-kaynağı-spesifikasyonu)
7. [SL hiyerarşisi](#7-sl-hiyerarşisi)
8. [TP zinciri](#8-tp-zinciri)
9. [Sizing formülü](#9-sizing-formülü)
10. [Execution lifecycle](#10-execution-lifecycle)
11. [Defansif mekanizmalar](#11-defansif-mekanizmalar)
12. [Config knob referansı](#12-config-knob-referansı)
13. [Reject reason kataloğu](#13-reject-reason-kataloğu)
14. [Bilinçli kapalı özellikler](#14-bilinçli-kapalı-özellikler)
15. [Bilinen sınırlar](#15-bilinen-sınırlar)
16. [Auto-sync kontratı](#16-auto-sync-kontratı)

---

## 1. Mimari üst-akış

Her tick (`BotRunner.run_once`) aşağıdaki sırayı yürütür:

```
┌────────────────────────────────────────────────────────────────────┐
│ 0. close reconcile  — OKX positions diff'i ile in-memory state'i   │
│                       senkronla; kapanan pozisyonları enrich edip  │
│                       journal'a record_close + artefact cross-chk  │
├────────────────────────────────────────────────────────────────────┤
│ 1. snapshot         — BTC+ETH+BNB (pillar) ilk; cross-asset veto   │
│                       her altcoin döngüsünden önce hazır olsun     │
├────────────────────────────────────────────────────────────────────┤
│ 2. pending poll     — her symbol için ayrı; fill olanlarda         │
│                       mark-vs-SL guard → attach_algos → register   │
├────────────────────────────────────────────────────────────────────┤
│ 3. per-symbol cycle                                                │
│   (a) TV settle + Pine settle-poll                                 │
│   (b) state = structured_reader.build_state()                      │
│   (c) LTF reversal close (pozisyon açıksa)                         │
│   (d) dynamic TP revise (pozisyon açıksa)                          │
│   (e) MFE SL lock (pozisyon açıksa, bir kez)                       │
│   (f) dedup: pozisyon açık veya pending varsa skip                 │
│   (g) confluence + hard gates → TradePlan                          │
│   (h) zone_setup → apply_zone_to_plan                              │
│   (i) place_limit_entry → PENDING                                  │
└────────────────────────────────────────────────────────────────────┘
```

Ayrıntılı anlatı için bkz. `docs/trade_lifecycle.md`. Girdi akışı (Pine → MarketState → confluence → plan → OKX) ve durum makinesi (`PENDING → FILLED → OPEN → CLOSED`) orada sahneler halinde anlatılmıştır.

---

## 2. Yön tespiti (direction resolution)

**Kaynak:** `src/analysis/multi_timeframe.py::calculate_confluence` (satır 708-779).

- Skorlama **BULL ve BEAR için ayrı ayrı** çalıştırılır (`score_direction(Direction.BULLISH, ...)` + `score_direction(Direction.BEARISH, ...)`).
- Her yön için her faktör; ya o yöne ait sinyal verirse ağırlığıyla birlikte listeye eklenir, yoksa atlanır. `total_score = sum(weights of contributing factors)`.
- Kazanan: `max(bull.score, bear.score)`.
- **Eşitlik (tie)**: `state.trend_htf` (15m EMA21/55 stack türevi) kazananı belirler; `trend_htf == UNDEFINED` ise **BEARISH** kazanır (no-trade bias; saldırgan tarafı dışlar).
- **Her iki yön de 0 ise**: `Direction.UNDEFINED` döner, strateji motoru bar'ı atlar (`below_confluence`).

Yön Pine tarafında değil **Python'da** hesaplanır. Pine sadece faktör sinyallerini (MSS / FVG / OB / VWAP / oscillator / divergence / sweep) rapor eder; "bull mu bear mı" kararı skorlayıcıda oluşur.

---

## 3. 5-Pillar confluence skorlaması

Faktörler 5 sütuna (pillar) ayrılmıştır. Aynı pillar içinden birden fazla faktör aynı anda fire edebilir — skorlayıcı bunları **additive** toplar. Bir faktör sadece "o yönü destekliyorum" dediğinde katkı verir (ters yönde veya nötr olduğunda 0).

Ağırlıklar: kod içi `DEFAULT_WEIGHTS` (`src/analysis/multi_timeframe.py:71-102`) + YAML `analysis.confluence_weights` (override) ile birleşir. YAML anahtarı yoksa DEFAULT kullanılır.

### Tablo: 13 faktör × pillar × ağırlık × fire koşulu

| Pillar | Factor name | Weight | Fires when |
|---|---|---:|---|
| Market Structure | `htf_trend_alignment` | 0.5 | 15m trend yönü ile aday yön aynı (tie-break'te de okunur) |
| Market Structure | `mss_alignment` | 0.75 | Pine'dan gelen son MSS (Market Structure Shift) aday yönde |
| Market Structure | `at_order_block` | 0.6 | Price + aday yön ile uyumlu Pine OB içinde |
| Market Structure | `at_fvg` | 0.75 | Price + aday yön ile uyumlu unfilled FVG içinde |
| Market Structure | `at_sr_zone` | 0.75 | Price, Python ATR-scaled S/R zone'u içinde + yön uyumu |
| Market Structure | `recent_sweep` | 1.0 | Pine son 3 bar içinde aday yönü destekleyen sweep-reclaim işaretledi |
| Liquidity | `liquidity_pool_target` | 0.5 | En yakın Pine liquidity pool `liquidity_pool_max_atr_dist × ATR` içinde + doğru yönde |
| Liquidity | `ltf_pattern` | 0.75 | 1m candle pattern (pin/engulf/vb.) aday yönle uyumlu |
| Money Flow | `money_flow_alignment` | 1.0 | `|rsi_mfi|` ≥ `min_rsi_mfi_magnitude` (2.0) + bias aday yöne uygun |
| Money Flow | `oscillator_momentum` | 0.75 | WaveTrend momentum aday yönde |
| Money Flow | `oscillator_signal` | 0.75 | Pine oscillator BUY/SELL tag aday yönde |
| Money Flow | `oscillator_high_conviction_signal` | 1.5 | GOLD_BUY / *_DIV (divergence-confirmed) tag aday yönde |
| VWAP | `vwap_1m_alignment` | 0.2 | Price, 1m session VWAP'ın aday yön tarafında |
| VWAP | `vwap_3m_alignment` | 0.0 | (inert) 3m VWAP align — composite'e devredildi |
| VWAP | `vwap_15m_alignment` | 0.0 | (inert) 15m VWAP align — composite'e devredildi |
| VWAP | `vwap_composite_alignment` | 1.25 | 3/3 TF align → 1.0×weight, 2/3 → 0.5×weight, 1/3 → 0×weight |
| Divergence | `divergence_signal` | 1.25 | Pine `last_wt_div` (BULL_REG/BEAR_REG/*_HIDDEN) aday yönde; bar-ago decay ile çarpılır (bkz. aşağı) |
| Divergence | `displacement_candle` | 0.6 | Son `displacement_max_bars_ago` (5) bar içinde body ≥ `displacement_atr_mult × ATR` (1.5×) aday yönde |
| Divergence | `vmc_ribbon` | 0.5 | VMC ribbon (Pine) aday yönde |

**Ayrıca derivatives one-slot** (en fazla biri fire eder, elif zinciri):
- `derivatives_contrarian` (0.7), `derivatives_capitulation` (0.6), `derivatives_heatmap_target` (0.5).

**Ayrıca düşük-ağırlıklı yardımcılar:**
- `session_filter` (0.25) — london/new_york içindeyse.
- `ltf_momentum_alignment` (0.75) — 1m momentum aday yön ile.

### Divergence decay

`divergence_signal` fire ettikten sonra ağırlık şu şekilde çarpılır (`src/analysis/multi_timeframe.py`):

| Bar-ago | Çarpan |
|---|---:|
| ≤ `divergence_fresh_bars` (3) | ×1.00 |
| ≤ `divergence_decay_bars` (6) | ×0.50 |
| ≤ `divergence_max_bars` (9) | ×0.25 |
| > 9 | 0 (skip) |

---

## 4. Rejim-koşullu skorlama (ADX)

`analysis.trend_regime_conditional_scoring_enabled: true`. `src/analysis/trend_regime.py` Wilder's 14-period ADX hesaplar, sonucu dört etikete indirger:

| Etiket | Koşul | Etki |
|---|---|---|
| `STRONG_TREND` | ADX ≥ 30.0 (`trend_regime_strong_threshold`) | `htf_trend_alignment` ×1.5, `recent_sweep` ×0.5 |
| `WEAK_TREND` | 20.0 ≤ ADX < 30.0 | Ağırlıklar değişmez (baseline) |
| `RANGING` | ADX < 20.0 (`trend_regime_ranging_threshold`) | `htf_trend_alignment` ×0.5, `recent_sweep` ×1.5 (trend-continuation'u cezalandırır, sweep'i ödüllendirir) |
| `UNKNOWN` | ADX hesaplanamadı (< 14 bar) | Ağırlıklar değişmez |

Etiket her tick hesaplanır; açılan trade'in kaydı `trend_regime_at_entry` alanına yazılır (journal v2 schema).

---

## 5. Hard gates referansı

Gate'ler **reddedici** (sıra önemli; ilk fire eden reject reason'u döner); skorlanmaz, ağırlığı yoktur. Sıra `src/strategy/entry_signals.py::build_trade_plan_with_reason` içinde sabittir.

| # | Reject reason | YAML flag | Default | Semantik | Durum |
|---:|---|---|---|---|---|
| 1 | `below_confluence` | — | — | Confluence < `min_confluence_score` (3) veya yön `UNDEFINED` | **AKTİF** (her zaman) |
| 2 | `session_filter` | `analysis.session_filter` | `[london, new_york]` | Aktif session izin listesinde değil | **AKTİF** |
| 3 | `no_sl_source` | — | — | `select_sl_price` hiçbir kaynaktan (§7) SL üretemedi | **AKTİF** |
| 4 | `vwap_misaligned` | `analysis.vwap_hard_veto_enabled` | `false` | Price her mevcut VWAP'ın ters tarafında | **KAPALI** (guard; bkz. §14) |
| 5 | `ema_momentum_contra` | `analysis.ema_veto_enabled` | `true` | Bull-stack ile bearish entry veya ters | **AKTİF** |
| 6 | `wrong_side_of_premium_discount` | `analysis.premium_discount_veto_enabled` | `false` | Longs midpoint üstünde / shorts midpoint altında (son 40 bar range) | **KAPALI** (2026-04-19'da geçici; soft-weighted olarak Phase 9 sonrası dönecek; bkz. §14) |
| 7 | `cross_asset_opposition` | `analysis.cross_asset_veto_enabled` | `true` | Altcoin: BTC+ETH pillar EMA stack'i ters yönde (freshness `cross_asset_veto_max_age_s`=300s) | **AKTİF** |
| 8 | `crowded_skip` | `derivatives.crowded_skip_enabled` | `true` | Funding veya LS ratio Z-score ≥ `crowded_skip_z_threshold` (3.0), aday yön ters taraf | **AKTİF** |
| 9 | `htf_tp_ceiling` | `analysis.htf_sr_ceiling_enabled` | `false` | HTF S/R zone TP'yi trim ederse ve RR < `min_rr_ratio` (1.5) | **KAPALI** (2026-04-19 gece; Phase 9 GBT sonrası yeniden değerlendirilecek; bkz. §14) |
| 10 | `zero_contracts` | — | — | Kontrat yuvarlama pozisyonu 0'a düşürdü (margin/leverage ceiling bind) | **AKTİF** |
| 11 | `tp_too_tight` | `analysis.min_tp_distance_pct` | 0.004 | TP/entry mesafe 0.4% altında (fee drag eşiği) | **AKTİF** |
| 12 | `insufficient_contracts_for_split` | `execution.partial_tp_enabled` | `false` | Partial TP açıkken `int(n × ratio) == 0` veya `== n` | **INERT** (partial TP kapalı; fire etmiyor) |
| 13 | `pending_invalidated` | — | — | Pending limit emri, fill'den önce karşı confluence çıktı | **AKTİF** |
| 14 | `zone_timeout_cancel` | `execution.zone_max_wait_bars` | 10 | Limit emri N bar boyunca fill olmadı → cancel | **AKTİF** |
| 15 | `no_setup_zone` | `execution.zone_require_setup` | `true` | Zone kaynakları hiçbiri valid zone üretmedi | **AKTİF** |
| 16 | `macro_event_blackout` | `economic_calendar.enabled` | `true` | HIGH-impact USD event blackout penceresinde | **AKTİF** |

> **Widening, rejection değil.** `min_sl_distance_pct` floor'un altında kalan SL'ler reddedilmez; `min_sl_distance_pct × entry` ile genişletilir, notional `risk / sl_pct` ile otomatik küçülür (R sabit kalır). Bkz. §7.

---

## 6. Zone kaynağı spesifikasyonu

Kaynak: `src/strategy/setup_planner.py::build_zone_setup`. İlk valid zone kazanır; sıra **sabit**:

| # | Source | Flag | Entry koşulu | Entry noktası | SL anchor |
|---:|---|---|---|---|---|
| 1 | `vwap_retest` | her zaman | En yakın session VWAP (price'ın doğru tarafında: long için < price, short için > price). 3m'nin seçildiği ve Pine'ın canlı ±1σ band yayınladığı durumda zone = `(vwap, upper_band)` long veya `(lower_band, vwap)` short. Aksi durumda `half = zone_atr × ATR` tek-taraflı bant. | zone mid (`vwap ± 0.5σ` band varsa, `vwap ± 0.5 × zone_atr × ATR` fallback) | zone edge'in altında/üstünde `sl_buffer_atr × ATR` (0.5×) |
| 2 | `ema21_pullback` | `execution.ema21_pullback_enabled` (true) | Stack align (`price > EMA21 > EMA55` long; mirror short) **VE** EMA21 price'ın doğru tarafında. EMA periyotları `ema_fast_period=21`, `ema_slow_period=55`. | `EMA21 ± zone_atr × ATR` bantının near-edge'i | zone edge'in altında/üstünde `sl_buffer_atr × ATR` |
| 3 | `fvg_entry` (3m) | her zaman | `state.active_bull_fvgs()` / `active_bear_fvgs()` — entry-TF unfilled FVG, aday yöne + price'ın doğru tarafında | zone near-edge | zone far-edge'in altında/üstünde `sl_buffer_atr × ATR` |
| 4 | `sweep_retest` | her zaman | Pine'ın son sweep-reclaim işareti aday yönde + price reclaim zone'una yakın | zone near-edge | zone far-edge'in altında/üstünde `sl_buffer_atr × ATR` |
| 5 | `liq_pool_near` | her zaman | En yakın liq cluster (a) `liq_entry_near_max_atr × ATR` (1.5×) içinde VE (b) notional ≥ `liq_entry_magnitude_mult × median(side_clusters)` (2.5×) | zone mid (cluster = hedef) | zone edge'in altında/üstünde `sl_buffer_atr × ATR` |
| 6 | `fvg_htf` (15m) | `execution.htf_fvg_entry_enabled` (false) | 15m unfilled FVG + displacement onayı | zone near-edge | zone far-edge'in altında/üstünde `sl_buffer_atr × ATR` |

**Entry fiyatı (`zone_limit_price`):**
- `liq_pool_near` + `vwap_retest` → zone mid (cluster/vwap-band ortası).
- Diğerleri → near-edge (long: `zone.low`, short: `zone.high`).

**SL widening pasajı:** `apply_zone_to_plan` zone'un structural SL'ini `min_sl_distance_pct` floor'a karşı yeniden kontrol eder. Floor altındaysa SL genişletilir, `num_contracts = risk / (sl_pct + fee_reserve_pct) / contracts_unit_usdt` ile re-size edilir. R sabit.

---

## 7. SL hiyerarşisi

Kaynak: `src/strategy/entry_signals.py::select_sl_price` (satır 170-238). İlk valid kaynak kazanır:

| # | Source | Koşul | SL fiyatı | Source label |
|---:|---|---|---|---|
| 1 | Pine OB | Price doğru tarafta, en yakın Pine OB drawing | `sl_from_order_block(ob, atr, direction, buffer_mult=0.2)` | `order_block_pine` |
| 2 | Pine FVG | Price doğru tarafta, en yakın Pine FVG drawing | `sl_from_fvg(fvg, ...)` | `fvg_pine` |
| 3 | Python OB | Python-side order_blocks listesi (HTF Pine'da yoksa) | `sl_from_order_block(...)` | `order_block_py` |
| 4 | Python FVG | Python-side fvgs listesi | `sl_from_fvg(...)` | `fvg_py` |
| 5 | Swing | `recent_swing_price(candles, direction, lookback=swing_lookback)`. DOGE+XRP+ADA için `swing_lookback_per_symbol=30`; diğerleri 20. | `sl_from_swing(swing, atr, direction, buffer_mult)` | `swing` |
| 6 | ATR fallback | Hiçbir yapısal kaynak yoksa | `entry ± 2.0 × ATR` (`atr_fallback_mult=2.0`) | `atr_fallback` |

**Per-symbol SL floor** (`analysis.min_sl_distance_pct_per_symbol`):

| Symbol | Floor % | Neden |
|---|---:|---|
| BTC-USDT-SWAP | 0.4% | Derin kitap, dar ATR; tight OB yeterli |
| ETH-USDT-SWAP | 0.6% | Demo wicks geniş; yüksek leverage yakar |
| SOL-USDT-SWAP | 1.0% | Geniş ATR, orta kitap |
| DOGE-USDT-SWAP | 0.8% | Momentum-driven, thin 3m kitap |
| BNB-USDT-SWAP | 0.5% | Major-class, BTC-tight |
| (XRP/ADA) | 0.8% | Thin-book alt (reinstatement-ready) |

Sub-floor Pine OB/FVG stops → **widen edilir, reddedilmez** (§5 not).

**HTF S/R push** (`htf_sr_ceiling_enabled=false`, 2026-04-19'da kapalı): flag açıkken `_push_sl_past_htf_zone` SL'i HTF 15m S/R zone'unun `htf_sr_buffer_atr × ATR` kadar ötesine iter.

---

## 8. TP zinciri

TP fiyatı 5 aşamalı bir zincirden geçer. Her aşama bir öncekini değiştirebilir (override).

### 8.1 Aşama sırası

```
┌──────────────────────────────────────────────────────────────────┐
│ 1. Base RR                                                       │
│    entry ± rr_ratio × sl_distance                                │
│    (rr_ratio=3.0 default, fee_reserve_pct eklenmez)              │
├──────────────────────────────────────────────────────────────────┤
│ 2. Zone override (apply_zone_to_plan)                            │
│    zone.tp_primary = en yakın doğru-taraflı liq cluster          │
│    (cluster yoksa zone_width × default_rr fallback)              │
├──────────────────────────────────────────────────────────────────┤
│ 3. Hard 1:3 cap (target_rr_ratio=3.0)                            │
│    new_tp = entry ± 3.0 × sl_distance                            │
│    — her ladder rung da aynı boundary'e clamp                    │
├──────────────────────────────────────────────────────────────────┤
│ 4. HTF TP ceiling (htf_sr_ceiling_enabled=false, INERT)          │
│    Flag açıkken: TP'yi HTF zone'un htf_sr_buffer_atr × ATR        │
│    ötesine çeker; trim edilen RR < min_rr_ratio → reject         │
├──────────────────────────────────────────────────────────────────┤
│ 5. Dynamic TP revise (runtime, tp_dynamic_enabled=true)          │
│    Her cycle: new_tp = entry + sign × target_rr_ratio ×          │
│                       plan_sl_distance                           │
│    Gate: |new_tp - current_tp| ≥ tp_revise_min_delta_atr × ATR   │
│    Cooldown: tp_revise_cooldown_s (30s)                          │
│    Floor: new RR ≥ tp_min_rr_floor (1.5)                         │
│    Action: monitor.revise_runner_tp(new_tp) — cancel+place OCO   │
├──────────────────────────────────────────────────────────────────┤
│ 6. MFE SL lock (sl_lock_enabled=true, 2026-04-20)                │
│    Trigger: mfe_r = sign × (price - entry) / plan_sl_distance    │
│             ≥ sl_lock_mfe_r (2.0)                                │
│    Action: monitor.lock_sl_at(entry + sign × sl_lock_at_r ×      │
│                               plan_sl_distance)                  │
│             sl_lock_at_r=0.0 → BE + sl_be_offset_pct (fee buf)   │
│    One-shot: sl_lock_applied=True → subsequent ticks skip        │
└──────────────────────────────────────────────────────────────────┘
```

### 8.2 Sequence diagram — yaşam döngüsü

```
  t0: setup_planner     → plan.tp = base RR (entry ± 3 × sl_dist)
  t1: apply_zone_to_plan → plan.tp = zone.tp_primary (cluster)
  t1: target_rr_cap=3.0  → plan.tp = entry ± 3 × sl_dist   ← cluster override'u kesin iptal
  t2: fill               → register_open(plan_sl_price=plan.sl_price)
  t3: cycle              → _maybe_revise_tp_dynamic →
                           recompute new_tp off live state →
                           cancel OCO + place new OCO (runner leg)
  t4: cycle              → _maybe_lock_sl_on_mfe →
                           mfe_r ≥ 2.0 → cancel OCO + place new OCO
                           (SL → entry + fee buffer; TP aynı kalır)
                           → sl_lock_applied=True (bir daha tetiklenmez)
  t5: SL veya TP hit     → enrich_close_fill → journal.record_close
```

### 8.3 Gate detayları

- **Dynamic revise gate (`_maybe_revise_tp_dynamic`):** `plan_sl_price <= 0` (rehydrate sonrası BE-moved pozisyon) → revise skip (ratio matematiği güvensiz). `current_price` mark tabanlı (Pine-settled 3m close), drift ölçümü için.
- **MFE lock skip koşulları:** `plan_sl_price <= 0`, `be_already_moved=True` (legacy partial-TP BE replacement zaten yapıldı), `sl_lock_applied=True`, `current_price <= 0`, `plan_sl_distance <= 0`.
- **Wrong-side TP guard (lock sırasında):** long için `new_sl < tp2_price` zorunlu; short için `new_sl > tp2_price`. Aksi: abort (tightening into worse stop).

---

## 9. Sizing formülü

Kaynak: `src/strategy/rr_system.py::calculate_trade_plan` (satır 78-230).

```
max_risk_usdt    = account_balance × risk_pct        # R = 1% of equity
effective_sl_pct = sl_pct + fee_reserve_pct          # 0.001 ≈ 2× OKX taker
ideal_notional   = max_risk_usdt / effective_sl_pct
contracts_unit_usdt = contract_size × entry_price    # 1 kontratın USDT cost'u

# Un-capped path (2026-04-19 ceil regime):
per_contract_cost = effective_sl_pct × contracts_unit_usdt
target_contracts  = ceil(max_risk_usdt / per_contract_cost)
# Fall-through safety: target > margin ceiling'i aşarsa capped yoluna düşer
max_contracts_by_notional = floor(max_notional / contracts_unit_usdt)
num_contracts = min(target_contracts, max_contracts_by_notional)

# Capped path (leverage/margin ceiling bind):
num_contracts = floor(max_notional / contracts_unit_usdt)

# Notional/margin/leverage
max_notional       = effective_margin × max_leverage × 0.95   # _MARGIN_SAFETY
liq_safe_leverage  = floor(0.6 / sl_pct)                       # _LIQ_SAFETY_FACTOR
min_lev_for_margin = ceil(notional / (effective_margin × 0.95))
leverage           = clamp(max(min_lev_for_margin, 1),
                           1, min(max_leverage, liq_safe_leverage))

# Actual risk (journal field — price-only, fee reserve ayrı)
actual_notional  = num_contracts × contracts_unit_usdt
actual_risk_usdt = actual_notional × sl_pct
```

**Sabitler:**
- `_MARGIN_SAFETY = 0.95` (rr_system.py:35) — %5'lik buffer fee + mark drift için; `sCode 51008` önler.
- `_LIQ_SAFETY_FACTOR = 0.6` (rr_system.py:43) — leverage tavanı; SL liq mesafesinin %60'ını aşmamalı (%40 maintenance + mark drift).

**Operator contract (2026-04-19):** un-capped path'te **realized loss ≥ max_risk_usdt**, overshoot en fazla bir `per_contract_cost` step (< $3 mevcut sembol seti için). Capped path hard ceiling'e saygı göstermek için floor kullanır → actual < target düşebilir; bu operator'ın kabul ettiği trade-off.

**Per-symbol `contract_size` (ctVal):**
- BTC 0.01, ETH 0.1, SOL 1, DOGE 1000 (büyük ctVal), BNB 0.01.
- Runtime'da `OKXClient.get_instrument_spec` ile `BotContext.contract_sizes`'a yazılır; YAML'da hardcoded değil.

---

## 10. Execution lifecycle

Durum makinesi:

```
         ┌─────────────┐
  signal │             │ zone timeout (10 bar) / pending_invalidated
  ──────►│   PENDING   │──────────────────────────────────► CANCELED
         │             │ veya fill
         └──────┬──────┘
                │ fill (mark-vs-SL pre-attach guard)
                ▼
         ┌─────────────┐
         │   FILLED    │ (geçici; attach_algos arası)
         └──────┬──────┘
                │ attach_algos başarılı
                ▼
         ┌─────────────┐
         │    OPEN     │
         │             │──── dynamic TP revise (cycle)
         │             │──── MFE SL lock (cycle, one-shot)
         │             │──── LTF reversal close (cycle, veto)
         └──────┬──────┘
                │ SL / TP OCO trigger (mark price) veya manual close
                ▼
         ┌─────────────┐
         │   CLOSED    │
         └─────────────┘
               │
               ├─► enrich_close_fill (realizedPnl, fees)
               ├─► journal.record_close
               └─► artefact cross-check (Binance 1m kline)
```

**Key pieces:**
- **Post-only limit first** (`OKXClient.place_limit_entry`): maker fee avantajı. Rejected → regular limit fallback.
- **Mark-vs-SL pre-attach guard** (`_handle_pending_filled`, runner.py:1932-1960): fill sonrası ve attach öncesi `get_mark_price` çağırır; mark zaten SL'i kırmışsa attach yapmaz, pozisyonu best-effort close eder. Second close failure → CRITICAL log (emergency close manual).
- **Single-leg OCO** (partial TP kapalı olduğundan): bir OCO algo, full `num_contracts` için `tp_price` + `sl_price`. `algo_ids` listesi 1 elemanlı.
- **OCO trigger** = `mark` (`algo_trigger_px_type`, 2026-04-19). Demo-wick'e karşı bağışık; `last` kullanımı artefact üretiyordu.
- **Close enrichment** zorunlu: `OKXClient.enrich_close_fill` → `/account/positions-history` çağrısıyla `realizedPnl` alır. Olmazsa her kapanış BREAKEVEN görünür, breakers tripsiz kalır.

---

## 11. Defansif mekanizmalar

Her biri silent-fail durumunda bile stratejik doğruluğu koruyan ek katman:

1. **Mark-vs-SL pre-attach guard** — §10'da açıklandı. 2026-04-19 UNPROTECTED incident'ının root-cause'u.
2. **Mark-trigger OCO** (`algo_trigger_px_type="mark"`) — OKX index mark (Binance+Bybit+Coinbase VWAP) ile tetikler; demo-book-only wick'ler fire etmez.
3. **Binance 1m kline artefact cross-check** (`execution.artefact_check_enabled=true`): close sonrası `BinancePublicClient.get_kline_around(ts)` ile entry ve exit'i concurrent real-market candle [low, high] + 5 bps tolerance'a karşı test eder. Outside → `demo_artifact=true`, `artifact_reason` set. Reporter `--exclude-artifacts` ile filtreler. Failure-isolated (hiç network yoksa tri-state `None`).
4. **LTF reversal close** (`execution.ltf_reversal_close_enabled=true`): açık pozisyonda 1m ters yönlü confluence ≥ `ltf_reversal_min_confluence` (3), son `ltf_reversal_signal_max_age` (3) bar'da ve min. `ltf_reversal_min_bars_in_position` (2) bar pozisyonda kalmışsa market-close ile erken çık. Binary veto; trail değil.
5. **51400 verify-before-replace** (`_verify_algo_gone`): cancel call `51400/51401/51402` döndüğünde (idempotent success codes) OKX `list_pending_algos` ile algoId'nin gerçekten kalktığını teyit eder. Aksi durumda OCO replacement atlanır — demo'da 2 aynı OCO stackleme riskine karşı.
6. **429 non-blocking** (`src/data/derivatives_api.py`): Coinalyze rate-limit 429 döndüğünde `asyncio.sleep(retry_after)` yok — `_rate_pause_until` set, aynı event-loop'taki diğer sembol cycle'larını blok etmez. Snapshot stale kalır; caller failure-isolation path'ine düşer.
7. **SL-to-BE never spins**: cancel ve place ayrı try-block'lar. `{51400,51401,51402}` idempotent. 3 cancel attempt sonrası `be_already_moved=True` (poll hammering kesilir). Place failure after cancel → UNPROTECTED + CRITICAL log (otomatik market-close yok).
8. **Risk manager replay** (`journal.replay_for_risk_manager`): restart'ta kapanan trade'lerden `peak_balance`, `consecutive_losses`, `current_balance` yeniden kurar. Drawdown breaker permanent halt (manual clear gerekir).
9. **SL-to-BE rehydrate preservation**: `trades.sl_moved_to_be` flag'i restart'ta `be_already_moved=True` olarak forward edilir → monitor SL'i ikinci kez taşımaz.

---

## 12. Config knob referansı

En sık tune edilen knob'lar. Her biri: YAML path → tip/değer → consumer → durum.

| YAML path | Tip / değer | Consumer | Durum |
|---|---|---|---|
| `trading.symbols` | list[str] / 5-item | `BotRunner` döngü | **AKTİF** |
| `trading.entry_timeframe` | str / "3m" | Pine set_timeframe | **AKTİF** |
| `trading.risk_per_trade_pct` | float / 1.0 | `rr_system.calculate_trade_plan` | **AKTİF** |
| `trading.fee_reserve_pct` | float / 0.001 | `rr_system` (effective_sl_pct) | **AKTİF** |
| `trading.max_leverage` | int / 75 | `rr_system` leverage cap | **AKTİF** |
| `trading.default_rr_ratio` | float / 3.0 | `entry_signals` pre-zone fallback | **AKTİF** |
| `trading.min_rr_ratio` | float / 1.5 | `htf_tp_ceiling` + dynamic revise floor | **AKTİF** (revise floor), **INERT** (ceiling kapalı) |
| `trading.max_concurrent_positions` | int / 5 | per-slot margin split | **AKTİF** |
| `analysis.min_confluence_score` | float / 3 | `entry_signals.below_confluence` gate | **AKTİF** |
| `analysis.swing_lookback` | int / 20 | `select_sl_price` swing path | **AKTİF** |
| `analysis.min_sl_distance_pct` | float / 0.005 | SL widen floor (global) | **AKTİF** |
| `analysis.min_sl_distance_pct_per_symbol` | dict | Per-sembol SL floor | **AKTİF** |
| `analysis.min_tp_distance_pct` | float / 0.004 | `tp_too_tight` gate | **AKTİF** |
| `analysis.htf_sr_ceiling_enabled` | bool / **false** | `_apply_htf_tp_ceiling` + `_push_sl_past_htf_zone` | **KAPALI** |
| `analysis.ema_veto_enabled` | bool / **true** | `_ema_momentum_veto` | **AKTİF** |
| `analysis.cross_asset_veto_enabled` | bool / **true** | `_cross_asset_opposes` | **AKTİF** |
| `analysis.premium_discount_veto_enabled` | bool / **false** | `_premium_discount_veto` | **KAPALI** |
| `analysis.vwap_hard_veto_enabled` | bool / false | `_vwap_hard_veto` | **KAPALI** (guard) |
| `analysis.trend_regime_conditional_scoring_enabled` | bool / true | ADX ağırlık çarpanı | **AKTİF** |
| `analysis.confluence_weights` | dict | `DEFAULT_WEIGHTS` override | **AKTİF** |
| `execution.partial_tp_enabled` | bool / **false** | `OrderRouter._place_algos` branch | **KAPALI** |
| `execution.move_sl_to_be_after_tp1` | bool / true | BE callback | **INERT** (partial kapalı) |
| `execution.sl_be_offset_pct` | float / 0.001 | BE replacement OCO + MFE lock (lock_at_r=0) | **AKTİF** (MFE lock yolu) |
| `execution.zone_entry_enabled` | bool / true | Zone path on/off | **AKTİF** |
| `execution.zone_require_setup` | bool / true | `no_setup_zone` reject | **AKTİF** |
| `execution.zone_max_wait_bars` | int / 10 | Timeout cancel | **AKTİF** |
| `execution.ema21_pullback_enabled` | bool / true | Zone source #2 | **AKTİF** |
| `execution.htf_fvg_entry_enabled` | bool / false | Zone source #6 (opt-in) | **KAPALI** |
| `execution.liq_entry_near_max_atr` | float / 1.5 | Zone #5 distance gate | **AKTİF** |
| `execution.liq_entry_magnitude_mult` | float / 2.5 | Zone #5 notional gate | **AKTİF** |
| `execution.tp_ladder_enabled` | bool / true | `_build_tp_ladder` | **INERT** (partial kapalı; ladder doluyor ama consumer yok) |
| `execution.target_rr_ratio` | float / **3.0** | `apply_zone_to_plan` hard cap | **AKTİF** |
| `execution.tp_dynamic_enabled` | bool / **true** | `_maybe_revise_tp_dynamic` | **AKTİF** |
| `execution.tp_min_rr_floor` | float / 1.5 | Revise floor | **AKTİF** |
| `execution.tp_revise_min_delta_atr` | float / 0.5 | Revise churn gate | **AKTİF** |
| `execution.tp_revise_cooldown_s` | float / 30.0 | Revise rate limit | **AKTİF** |
| `execution.sl_lock_enabled` | bool / **true** | `_maybe_lock_sl_on_mfe` | **AKTİF** |
| `execution.sl_lock_mfe_r` | float / **2.0** | Lock trigger (R multiples) | **AKTİF** |
| `execution.sl_lock_at_r` | float / **0.0** | Lock yeri (0 = BE + fee buf) | **AKTİF** |
| `execution.algo_trigger_px_type` | str / **"mark"** | OCO trigger source | **AKTİF** |
| `execution.artefact_check_enabled` | bool / **true** | Binance cross-check | **AKTİF** |
| `execution.artefact_check_tolerance_pct` | float / 0.0005 | Cross-check band tolerance | **AKTİF** |
| `execution.ltf_reversal_close_enabled` | bool / true | Defensive-close gate | **AKTİF** |
| `derivatives.enabled` | bool / true | Coinalyze + Binance WS loop | **AKTİF** |
| `derivatives.crowded_skip_enabled` | bool / true | `crowded_skip` gate | **AKTİF** |
| `economic_calendar.enabled` | bool / true | `macro_event_blackout` gate | **AKTİF** |
| `rl.clean_since` | ISO-8601 / `2026-04-19T06:30:00Z` | Reporter + RL filter | **AKTİF** |

---

## 13. Reject reason kataloğu

`rejected_signals` tablosuna yazılan kanonik reason string'leri. `build_trade_plan_with_reason` ve çağrı-siteleri dışında eklenmiş ek string yoktur.

| Reason | Fire edildiği yer | Not |
|---|---|---|
| `below_confluence` | `build_trade_plan_with_reason` (entry_signals.py:691) | Confluence < threshold VEYA `UNDEFINED` yön |
| `session_filter` | `build_trade_plan_with_reason` (entry_signals.py:693) | Session allowlist match etmedi |
| `no_sl_source` | `build_trade_plan_with_reason` (entry_signals.py:694, 696) | `select_sl_price` hiçbir kaynak bulamadı |
| `vwap_misaligned` | `build_trade_plan_with_reason` (entry_signals.py:701) | `vwap_hard_veto_enabled=false` → fire etmez |
| `ema_momentum_contra` | `build_trade_plan_with_reason` (entry_signals.py:710) | EMA21/55 stack ters |
| `wrong_side_of_premium_discount` | `build_trade_plan_with_reason` (entry_signals.py:718) | `premium_discount_veto_enabled=false` → fire etmez |
| `cross_asset_opposition` | `build_trade_plan_with_reason` (entry_signals.py:721) | BTC+ETH pillar ters yönde |
| `crowded_skip` | `build_trade_plan_with_reason` (entry_signals.py:729) | Funding/LS Z-score ≥ 3.0 |
| `htf_tp_ceiling` | `build_trade_plan_with_reason` (entry_signals.py:798) | `htf_sr_ceiling_enabled=false` → fire etmez |
| `tp_too_tight` | `build_trade_plan_with_reason` (entry_signals.py:807) | TP/entry < 0.4% |
| `zero_contracts` | `build_trade_plan_with_reason` (entry_signals.py:776) | Kontrat yuvarlama 0 → margin/leverage bind |
| `insufficient_contracts_for_split` | `build_trade_plan_with_reason` (entry_signals.py:787) | Partial TP kapalı → fire etmez |
| `no_setup_zone` | `runner._try_place_zone_entry` | `build_zone_setup` hiç zone dönmedi |
| `zone_timeout_cancel` | Pending poll | `zone_max_wait_bars` bar boyunca fill yok |
| `pending_invalidated` | Pending poll | Karşı confluence çıktı, fill olmadan cancel |
| `macro_event_blackout` | `economic_calendar` gate | HIGH-impact USD event penceresinde |

> **Widening, rejection değil**: Pine OB/FVG derived SL'ler `min_sl_distance_pct` floor altında kalırsa WIDEN edilir. Bu rejection listesine yazılmaz; trade plan'i devam eder.

---

## 14. Bilinçli kapalı özellikler

Flag ile kapatılmış ama kod tabanında korunan feature'lar — her biri defans hattında veya Phase 9/10 sonrası re-enable adayı.

| Feature | Flag (değer) | Tarih | Re-enable kriteri |
|---|---|---|---|
| **Partial TP split** | `execution.partial_tp_enabled=false` | 2026-04-19 gece | Factor-audit post-flip 30+ trade: WR < %25 (break-even under pure 3R) → tekrar açılır. Aksi kalıcı. |
| **SL-to-BE after TP1** | `execution.move_sl_to_be_after_tp1=true` | — | Partial kapalı olduğundan INERT; partial geri gelirse otomatik aktif |
| **HTF TP/SR ceiling + SL push** | `analysis.htf_sr_ceiling_enabled=false` | 2026-04-19 gece | Phase 9 GBT: TP-ceiling veya SL-push side'dan hangisi lift gösteriyor? (a) Her ikisi → tek flag restore. (b) Sadece SL-push → flag'i ikiye böl (`htf_sr_tp_ceiling_enabled` + `htf_sr_sl_push_enabled`). (c) Hiçbiri → kalıcı kapalı. Re-enable sırasında `rl.clean_since` bump |
| **Premium/Discount veto** | `analysis.premium_discount_veto_enabled=false` | 2026-04-19 | Phase 9 sonrası: hard gate olarak değil, **soft-weighted factor** (~10-15% weight-equivalent) olarak dönecek. Mevcut `_premium_discount_veto` fonksiyonu re-use edilmeyecek → refactor sırasında yerini yeni weighted-pillar logic alacak |
| **VWAP hard veto** | `analysis.vwap_hard_veto_enabled=false` | — | Sprint 4 VWAP strictness için guard; tek YAML flipi |
| **HTF FVG entry (15m)** | `execution.htf_fvg_entry_enabled=false` | — | Opt-in; Phase 9 GBT 15m FVG signal'i onaylarsa açılır |
| **TP ladder (partial)** | `execution.tp_ladder_enabled=true` | — | Consumer `partial_tp_enabled` gated olduğundan INERT; partial reinstatement ile otomatik |

---

## 15. Bilinen sınırlar

Bu sınırlar açıkça kabul edilmiş teknik/pazar kısıtlardır — fix değil, farkındalık.

1. **Demo-wick poisoning** — OKX demo book tek-tradeli wick'ler üretir (gerçek exchange fiyatlarını takip etmez). Mark-trigger OCO + Binance cross-check bu artefact'ları büyük oranda filtreler, ama %5-10 residual bekleniyor. Phase 9 RL training öncesi reporter'da `demo_artifact=1` satırlar filtrelenmeli.
2. **Dataset regime mixing (2026-04-20)** — `rl.clean_since=2026-04-19T06:30:00Z`. Window içinde 5 pivot karışık: partial-on+floor / partial-off+floor / partial-off+ceil / HTF ceiling on/off / vwap_1m probe pre-post. Flat-cutoff varsayımı artık geçerli değil. Factor-audit `regime_tag` categorical feature ile segment etmeli veya pivot boundary bazlı slice'lar karşılaştırmalı.
3. **OCO concurrency on restart** — 5 açık pozisyondan 5'i de OKX-side'da bağımsız OCO'ya sahip. Bot restart'ta `rehydrate_open_positions` DB'den `sl_price` + `plan_sl_price` okur; BE-moved pozisyonlar `plan_sl_price=0.0` ile gelir → dynamic TP revise ve MFE lock o pozisyonlarda skip edilir (safer than reviving with degenerate sl_distance).
4. **MFE lock one-shot** — bir pozisyon için sadece bir kez tetiklenir. "2R lock → fell back to BE → resumed to +2.5R → reversed again" senaryosu lock'u tekrar tetiklemez. Phase 12 Option B (ATR-trailing SL) bu bucket için hazırlanmış roadmap.
5. **Pre-fill UNPROTECTED race** — `_handle_pending_filled` mark-vs-SL guard bu riski ~sıfıra indirir, ama second `close_position` failure'ı otomatik market-close ile handle edilmez; CRITICAL log düşer, operator intervention gerekir.
6. **Circuit breakers loosened** — `max_consecutive_losses=9999`, `max_daily_loss_pct=40`, `max_drawdown_pct=40`, `min_rr_ratio=1.5` data collection için geçici. 20+ post-pivot closed trade sonrası sıkılaştırılacak (`5 / 15 / 25 / 2.0`).
7. **Coinalyze free-tier ceiling** — 5 sembol + `refresh_interval_s=75s` rahat, 6+ sembol için refresh'i uzatmak veya paid tier gerekir.
8. **Pine table truncation riski** — `str.tostring(val, "#.########")` zorunlu (`"#.##"` değil); truncation DOGE/XRP ATR'sini sıfırlar, `no_sl_source` spam'i başlar.
9. **TV MCP versiyon kırılganlığı** — TradingView Desktop MSIX update'leri standalone exe debug port'unu değiştirebilir; CDP reach kaybolur, bot Pine settle timeout alır.

---

## 16. Auto-sync kontratı

Bu dosya **CLAUDE.md ile senkron** tutulur. Mekanizma:

- **Stop hook** (`.claude/settings.json`): oturum sonunda `git diff --name-only HEAD` CLAUDE.md modify'ını yakalayıp algoritma.md modify'ı yoksa stdout'a uyarı yazar.
- **PreToolUse hook** (Bash matcher, `git commit` command): `git diff --cached --name-only` CLAUDE.md staged ama algoritma.md staged değilse `exit 2` ile blok atar.
- **Memory**: Claude'un sonraki oturumlarında kural'ı **hatırlamasını** sağlar; otomatik davranışı **hook** sağlar. İkisi ayrı mekanizma.

**CLAUDE.md'de değişiklik olduğunda zorunlu kontrol listesi:**
- Changelog girdisi → bu dosyanın ilgili section'unu güncellemek gerek mi? (flag değişimi §12, yeni hard gate §5, yeni zone source §6, reject reason §13, vs.)
- "Son güncelleme" tarihini ve "CLAUDE.md hash"i güncelle (bu dosyanın ilk satırındaki frontmatter).
- Hook uyarısı geldiğinde: uyarıyı dismiss etmek yerine gerçekten senkronla — sessiz drift en zor yakalanan hatadır.

**Değiştirilmemesi gereken alanlar (stable contract):**
- Section başlıkları ve numaralama (diğer docs bu dosyaya anchor link veriyor olabilir).
- Dil: Türkçe. İngilizce kod string'leri aynen (`below_confluence`, `displacement_candle`, vs.).
- Tablo şemaları (pillar × factor × weight × fires-when); YAML schema'sı değiştiyse tablonun kolonları değil yalnız içerik değişir.

---

*Bu dosyanın kaynağı: `C:\Users\samet\Desktop\SMTbot\algoritma.md`. CLAUDE.md ↔ algoritma.md bağlantısını kıran herhangi bir değişiklik için hook mekanizması uyarı verir (bkz. §16).*
