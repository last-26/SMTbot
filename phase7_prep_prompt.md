# Phase 7 Öncesi İyileştirmeler — Multi-Pair + Multi-TF + Smart Entry/Exit

## Context

Crypto futures trading bot projemde Phase 1-6 tamamlandı. Bot şu an **BTC-USDT-SWAP** üzerinde demo modda işlem alıyor. Phase 7 (RL parameter tuner) başlamadan önce rule-based stratejiyi **disiplinli ve multi-asset** hale getirmek istiyorum. RL'in iyi eğitilebilmesi için strateji deterministik çalışmalı ve yeterli veri (ideal olarak 5 paritede 100+ işlem) üretmeli.

Bu dokümandaki **6 maddeyi sırayla** uygulamanı istiyorum. Her madde bağımsız commit olsun, her birinin testi olsun.

---

## Mevcut mimari (hatırlatma)

- `src/bot/runner.py` — poll loop: her cycle'da `read_market_state → analyze → plan → execute`
- `src/data/tv_bridge.py` — TV CLI wrapper, `set_symbol()` / `set_timeframe()` / `get_ohlcv()` mevcut
- `src/data/structured_reader.py` — Pine tablolarını `MarketState`'e çevirir
- `src/data/candle_buffer.py` — `MultiTFBuffer` zaten birden fazla TF için buffer tutabiliyor
- `src/analysis/` — market_structure, support_resistance, fvg, order_blocks, multi_timeframe (confluence)
- `src/strategy/` — risk_manager, SL/TP seçici, plan üretici
- `src/execution/order_router.py` — OKX'e plan gönderir, OCO algo attach eder
- `src/execution/position_monitor.py` — REST-poll ile fill/close tespiti
- `src/journal/` — aiosqlite trade journal
- Config: `config/default.yaml` + pydantic `BotConfig`

---

## Genel kurallar (her maddeye uygulanır)

1. **Önce oku, sonra yaz** — her maddeden önce ilgili dosya(lar)ı `view` ile incele, mevcut davranışı anla
2. **Minimal değişiklik** — mevcut testleri kırma, mevcut public API'yi bozma
3. **Her madde için test yaz** — `tests/test_<modul>.py` (pytest)
4. **Geriye dönük uyum** — eski config dosyaları çalışmaya devam etsin (deprecation warning ile)
5. **Her madde ayrı commit** — `feat: <madde>` veya `fix: <madde>` prefix'i ile
6. **Her maddeden sonra** `.venv/Scripts/python.exe -m pytest tests/ -v` — tüm testler geçmeli
7. **Smoke test** — `python -m src.bot --config config/default.yaml --dry-run --once` — exception olmadan tamamlanmalı

## Yapmayacağın şeyler

- Phase 7 (RL) başlatma — bu doküman sadece Phase 7 **öncesi** hazırlık
- Pine Script'lere dokunma — `pine/` altındakiler çalışıyor, bırak
- `OKX_DEMO_FLAG=1` guard'ını bozma (demo first)
- Circuit breaker'ları bozma (max_drawdown, daily loss, consecutive loss)
- Yeni Python dependency ekleme — mevcut `requirements.txt` yeterli
- Live mode'a geçme

---

## MADDE A — Multi-pair round-robin

### Hedef
Bot tek `symbol` yerine **5 paritelik liste** üzerinde sıralı tarama yapacak. Her cycle'da tüm semboller tek tek dolaşılacak.

### Pariteler
BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, AVAX-USDT-SWAP, XRP-USDT-SWAP

### Değişiklikler

**1. `config/default.yaml`**
```yaml
trading:
  # Eski 'symbol' field'ı deprecated ama destek devam (validator tek sembol → listeye dönüştürsün + warning)
  # NOT: 5 parite TV Desktop'ta watchlist'e ekli (OKX:BTCUSDT.P / ETHUSDT.P / SOLUSDT.P / AVAXUSDT.P / XRPUSDT.P).
  # BNB YOK — liste sabit 5 parite. Liste genişletmek için Phase 7 sonrası "Currency pair strategy" kurallarına bak.
  symbols:
    - BTC-USDT-SWAP
    - ETH-USDT-SWAP
    - SOL-USDT-SWAP
    - AVAX-USDT-SWAP
    - XRP-USDT-SWAP
  symbol_settle_seconds: 4.0       # sembol değiştikten sonra minimum bekleme (Pine freshness-poll'un altı)
  pine_settle_max_wait_s: 6.0      # freshness poll timeout (aşağıda Madde B)
  pine_settle_poll_interval_s: 0.3
  tf_settle_seconds: 2.5           # set_timeframe sonrası minimum bekleme (1.5s yetmiyor — SMT Overlay ~1200 satır, ağır)
  entry_timeframe: "3m"
  htf_timeframe: "15m"
  ltf_timeframe: "1m"              # MADDE B'de kullanılacak
  max_concurrent_positions: 3
```

**2. `BotConfig` — `src/bot/config.py` içindeki `TradingConfig` modeline ekle**
- `symbols: list[str]` ekle (non-empty validator)
- Backward compat validator: `symbol` geldiyse `symbols=[symbol]` yap, `DeprecationWarning` fırlat; `symbols` zaten varsa eski `symbol` yok sayılır
- `symbol_settle_seconds: float = 4.0`
- `pine_settle_max_wait_s: float = 6.0`
- `pine_settle_poll_interval_s: float = 0.3`
- `tf_settle_seconds: float = 2.5`
- `ltf_timeframe: str = "1m"`
- `BotConfig.primary_symbol()` helper'ı geri uyum için `symbols[0]` döndürsün (mevcut tek-sembol call-site'ları için)

**3. `src/data/tv_bridge.py` — sembol helper**
```python
def okx_to_tv_symbol(okx_symbol: str) -> str:
    """'BTC-USDT-SWAP' → 'OKX:BTCUSDT.P'"""
    base = okx_symbol.replace("-SWAP", "").replace("-", "")
    return f"OKX:{base}.P"
```

**4. `src/bot/runner.py` — `run_once()` refactor**

Eski akış: tek sembol, doğrudan read → analyze → execute.

Yeni akış:
```python
async def run_once(self):
    for symbol in self.ctx.config.trading.symbols:
        try:
            await self._run_one_symbol(symbol)
        except Exception:
            logger.exception("symbol_cycle_failed symbol={}", symbol)
            continue  # bir sembol çökerse diğerleri devam etsin

async def _run_one_symbol(self, symbol: str):
    tv_sym = okx_to_tv_symbol(symbol)
    await self.ctx.bridge.set_symbol(tv_sym)
    await asyncio.sleep(self.ctx.config.trading.symbol_settle_seconds)
    # (MADDE B bu adımı multi-TF loop ile genişletecek)
    await self.ctx.bridge.set_timeframe(self.ctx.config.trading.entry_timeframe)
    await asyncio.sleep(1)
    # Mevcut run_once mantığının kalanı — state oku, plan yap, execute
    ...
```

**5. Önemli korunması gerekenler**
- `open_trade_ids` zaten `(symbol, side)` tuple — değişiklik gerekmiyor
- `max_concurrent_positions` kontrolü `RiskManager.can_trade()` içinde — değişmiyor
- Symbol-level dedup (`any(k[0] == symbol for k in self.ctx.open_trade_ids)`) — aynen kalsın
- Circuit breaker'lar global — tüm pariteleri etkiler, bu doğru davranış

### Test
`tests/test_runner_multi_pair.py`:
- FakeBridge (set_symbol çağrılarını listeye kaydeden) ver → 5 sembolün sırayla çağrıldığını doğrula
- 3. sembolde exception fırlat → 4. ve 5. sembol yine de çağrılmış mı?
- max_concurrent_positions=2 iken 2 açık pozisyon varsa yeni entry atılmıyor mu?
- Backward compat: sadece `symbol: BTC-USDT-SWAP` olan eski config hâlâ çalışıyor mu?

---

## MADDE B — Multi-TF data pipeline (1m + 3m + 15m)

### Hedef
Her sembol için 3 TF'den veri topla:
- **1m (LTF)** — reversal detection için (MADDE F'de kullanılacak)
- **3m (entry TF)** — ana confluence analizi ve giriş kararı
- **15m (HTF)** — S/R zonları ve bias için (MADDE D'de kullanılacak)

### Değişiklikler

**1. `src/bot/runner.py` — `_run_one_symbol` içinde çoklu TF turu**

**Önce** — sabit `asyncio.sleep()` ile körü körüne beklemek yerine Pine'in fiilen yerleştiğini doğrulayan bir helper. Sembol/TF geçişi sonrası SMT Signals tablosundaki `last_bar` alanı değişiyorsa Pine yeni veriyle yerleşmiş demektir:

```python
async def _wait_for_pine_settle(
    bridge,
    reader,
    *,
    max_wait_s: float,
    poll_interval_s: float,
) -> bool:
    """`signal_table.last_bar` yeni bir değere geldiğinde TRUE.
    Timeout'ta FALSE — çağıran sembolü (veya TF'i) o cycle için atlar."""
    import asyncio, time
    deadline = time.monotonic() + max_wait_s
    baseline: int | None = None
    while time.monotonic() < deadline:
        try:
            state = await reader.read_market_state()
            sig = state.signal_table
            if sig and sig.last_bar:
                if baseline is None:
                    baseline = sig.last_bar
                elif sig.last_bar != baseline:
                    return True
        except Exception:
            pass  # tablo henüz hazır değil — yeniden dene
        await asyncio.sleep(poll_interval_s)
    return False
```

Multi-TF turu:

```python
async def _run_one_symbol(self, symbol: str):
    cfg = self.ctx.config.trading
    tv_sym = okx_to_tv_symbol(symbol)
    await self.ctx.bridge.set_symbol(tv_sym)
    await asyncio.sleep(cfg.symbol_settle_seconds)       # minimum taban (~4s)

    # 1) HTF — bias + S/R
    await self.ctx.bridge.set_timeframe(cfg.htf_timeframe)
    await asyncio.sleep(cfg.tf_settle_seconds)           # minimum taban (~2.5s)
    if not await _wait_for_pine_settle(
        self.ctx.bridge, self.ctx.reader,
        max_wait_s=cfg.pine_settle_max_wait_s,
        poll_interval_s=cfg.pine_settle_poll_interval_s,
    ):
        logger.warning("pine_stale_skip symbol={} tf={}", symbol, cfg.htf_timeframe)
        return   # bu sembolü bu cycle için atla — bir sonraki tick'te yeniden dene
    htf_key = _timeframe_key(cfg.htf_timeframe)
    await self.ctx.multi_tf.refresh(htf_key, count=200)
    htf_candles = self.ctx.multi_tf.get_buffer(htf_key).last(200)
    htf_sr_zones = detect_sr_zones(
        htf_candles,
        swing_lookback=cfg.analysis.swing_lookback,
        zone_atr_mult=cfg.analysis.sr_zone_atr_mult,
        min_touches=cfg.analysis.sr_min_touches,
    )
    self.ctx.htf_sr_cache[symbol] = htf_sr_zones

    # 2) LTF — hafif momentum/trend snapshot (MADDE F)
    await self.ctx.bridge.set_timeframe(cfg.ltf_timeframe)
    await asyncio.sleep(cfg.tf_settle_seconds)
    ltf_ready = await _wait_for_pine_settle(
        self.ctx.bridge, self.ctx.reader,
        max_wait_s=cfg.pine_settle_max_wait_s,
        poll_interval_s=cfg.pine_settle_poll_interval_s,
    )
    if ltf_ready:
        ltf_key = _timeframe_key(cfg.ltf_timeframe)
        await self.ctx.multi_tf.refresh(ltf_key, count=100)
        self.ctx.ltf_cache[symbol] = await self.ctx.ltf_reader.read(symbol)
    else:
        # LTF yerleşmezse sembol atlanmaz — ltf_cache boş kalır, MADDE F no-op olur
        logger.warning("pine_stale_ltf symbol={}", symbol)
        self.ctx.ltf_cache.pop(symbol, None)

    # 3) Entry TF — ana analiz
    await self.ctx.bridge.set_timeframe(cfg.entry_timeframe)
    await asyncio.sleep(cfg.tf_settle_seconds)
    if not await _wait_for_pine_settle(
        self.ctx.bridge, self.ctx.reader,
        max_wait_s=cfg.pine_settle_max_wait_s,
        poll_interval_s=cfg.pine_settle_poll_interval_s,
    ):
        logger.warning("pine_stale_skip symbol={} tf=entry", symbol)
        return
    entry_key = _timeframe_key(cfg.entry_timeframe)
    await self.ctx.multi_tf.refresh(entry_key, count=100)
    state = await self.ctx.reader.read_market_state()

    # Analyze — htf_sr_cache[symbol] ve ltf_cache[symbol] context olarak kullanılır
    ...
```

**Süre bütçesi (gerçekçi — sabit sleep değil, poll ile):**
- set_symbol → symbol_settle 4s + freshness 0.3–3s ≈ **4–7s**
- her TF switch → tf_settle 2.5s + freshness 0.3–3s + refresh CLI 1–2s + reader 1–2s ≈ **5–9s** × 3 TF = 15–27s
- per-symbol toplam: **~20–35s** (iyimser ~20s, Pine yavaş yerleşirse ~35s)
- 5 sembol × ort. 25s ≈ **~2 dakika** tam tarama. 3m mum kapanışı 180s — mum başına ~1.5 tarama. Yeterli.
- Eğer latency ölçüldükten sonra bütçe fazla görünürse `symbol_settle_seconds` ve `tf_settle_seconds` düşürülebilir — freshness poll güvenlik ağı.

**Fail-soft:**
- HTF veya entry TF freshness timeout → sembol o cycle atlanır (`pine_stale_skip`), bir sonraki cycle'da tekrar denenir.
- LTF timeout → sembol atlanmaz, sadece `ltf_cache` boşalır; MADDE F defensive close o tur çalışmaz.
- Tüm sembollerin aynı cycle'da stale dönmesi TV/MCP bağlantı sorunudur — üst seviye `run_once` try/except zaten yakalıyor.

**2. `src/data/ltf_reader.py` — yeni dosya**

Full MarketState gerekmez, sadece hafif momentum snapshot:
```python
from dataclasses import dataclass
from src.data.models import Direction
from src.data.tv_bridge import TVBridge

@dataclass
class LTFState:
    symbol: str
    timeframe: str
    price: float
    rsi: float
    wt_state: str          # OVERBOUGHT / OVERSOLD / NEUTRAL
    wt_cross: str          # UP / DOWN / —
    last_signal: str       # BUY / SELL / —
    last_signal_bars_ago: int
    trend: Direction       # derived from wt + rsi heuristics

class LTFReader:
    def __init__(self, bridge: TVBridge):
        self.bridge = bridge

    async def read(self, symbol: str) -> LTFState:
        # Oscillator tablosunu filter ile çek (SMT Oscillator zaten tüm chart'ta yüklü)
        tables = await self.bridge.get_pine_tables(study_filter="SMT Oscillator")
        # OscillatorTableData parser'ı zaten src/data/structured_reader.py'de var
        # Onu import edip aynı tabloyu parse et, LTFState'e dönüştür
        ...
```

**3. `BotContext`'e ekle**
```python
ltf_reader: Any                       # .read(symbol) -> LTFState
htf_sr_cache: dict[str, list[SRZone]] = field(default_factory=dict)
ltf_cache: dict[str, LTFState] = field(default_factory=dict)
```

### Test
`tests/test_multi_tf_pipeline.py`:
- FakeBridge'e set_timeframe çağrı sırasını kaydettir → HTF → LTF → entry sırası doğrulanmalı
- HTF candle buffer'ı dolduktan sonra `detect_sr_zones` çağrıldı mı?
- Herhangi bir TF refresh'i fail olursa: log + diğer TF'leri denemeye devam et (fail-soft)

---

## MADDE C — Per-side cooldown + re-entry quality gate

### Hedef
TP/SL sonrası aynı yönde **anında** yeniden giriş engellenecek. Re-entry için:
- En az N mum geçmeli (zaman gate)
- Fiyat en az K * ATR uzaklaşmalı (fiyat gate)
- Eğer son kapanış WIN idiyse confluence skoru **artmış** olmalı (kalite gate)

### Neden gerekli
Örnek: long 74.600'den 75.250 TP'ye kapandı, hemen aynı yerden yine long açıldı, piyasa dönmüştü, SL'e gidiyor. Sinyal hâlâ bullish görünüyor ama **aynı setup iki kere oynanmamalı** — yeni bir tetik olayı (MSS, sweep, yeni OB) gerekir.

### Değişiklikler

**1. `src/bot/runner.py` — BotContext + LastCloseInfo**

```python
@dataclass
class LastCloseInfo:
    price: float
    time: datetime
    confluence: int                   # kapanan pozisyonun giriş anındaki confluence
    outcome: str                      # "WIN" / "LOSS" / "BREAKEVEN"

@dataclass
class BotContext:
    # ... mevcut field'lar ...
    last_close: dict[tuple[str, str], LastCloseInfo] = field(default_factory=dict)
```

**2. `_process_closes()` sonunda**
Trade kapandığında `last_close[(symbol, side)] = LastCloseInfo(...)` güncelle.

**3. Yeni helper: `_check_reentry_gate`**

```python
def _check_reentry_gate(
    self,
    symbol: str,
    side: str,
    proposed_confluence: int,
    current_price: float,
    atr: float,
    now: datetime,
) -> tuple[bool, str]:
    last = self.ctx.last_close.get((symbol, side))
    if last is None:
        return True, ""
    cfg = self.ctx.config.reentry

    # Zaman gate
    elapsed_s = (now - last.time).total_seconds()
    tf_s = _tf_seconds(self.ctx.config.trading.entry_timeframe)
    if elapsed_s < cfg.min_bars_after_close * tf_s:
        return False, f"cooldown_active elapsed={elapsed_s:.0f}s bars={cfg.min_bars_after_close}"

    # Fiyat gate
    if atr > 0:
        price_move_atr = abs(current_price - last.price) / atr
        if price_move_atr < cfg.min_atr_move:
            return False, f"insufficient_move atr_mult={price_move_atr:.2f}"

    # Kalite gate (WIN sonrası)
    if last.outcome == "WIN" and cfg.require_higher_confluence_after_win:
        if proposed_confluence <= last.confluence:
            return False, f"confluence_not_improved last={last.confluence} now={proposed_confluence}"

    # Kalite gate (LOSS sonrası — aynı kötü setup'ı tekrar oynama)
    if last.outcome == "LOSS" and cfg.require_higher_or_equal_confluence_after_loss:
        if proposed_confluence < last.confluence:
            return False, f"confluence_degraded_after_loss last={last.confluence} now={proposed_confluence}"

    return True, ""
```

**4. run_once içinde giriş öncesi gate çağrısı**
Mevcut "symbol-level dedup"tan sonra, plan üretiminden önce bu gate'i çağır. Blokaj varsa `logger.info` ile sebebi logla ve `return`.

**5. Config**
```yaml
reentry:
  min_bars_after_close: 3          # entry TF mumu cinsinden
  min_atr_move: 0.5                # fiyatın ATR cinsinden minimum hareketi
  require_higher_confluence_after_win: true
  require_higher_or_equal_confluence_after_loss: true   # LOSS sonrası aynı kötü setup'ı oynama
```

**6. BotConfig'e `ReentryConfig` pydantic modelini ekle**

### Test
`tests/test_reentry_gate.py`:
- Cooldown aktif (1 bar geçti, 3 gerekli) → bloklanıyor
- Fiyat 0.2 ATR hareket etti (0.5 gerekli) → bloklanıyor
- WIN sonrası confluence 3 → 3 (aynı, artmadı) → bloklanıyor
- WIN sonrası confluence 3 → 4 → geçiyor
- LOSS sonrası confluence 4 → 3 (düştü) → bloklanıyor (`confluence_degraded_after_loss`)
- LOSS sonrası confluence 3 → 3 (eşit) → geçiyor
- LOSS sonrası confluence 3 → 4 → geçiyor
- Ters yön (long kapandı, short girilmek isteniyor) → cooldown uygulanmıyor, serbest

---

## MADDE D — HTF S/R entegrasyonu (SL/TP seçiminde)

### Hedef
MADDE B'de topladığımız HTF S/R zonlarını plan üretiminde kullan:
- **SL**: seçilen stop ile entry arasında HTF zone varsa, stop'u zonun ötesine it (stop avı engelleme)
- **TP**: yol üstünde ters yönlü HTF zone varsa TP'yi o zonun biraz öncesine çek (zone'a vurdurup kârı kaptırma engelleme)

### Değişiklikler

**1. `src/strategy/` — `select_sl_price` genişletmesi**

Mevcut priority: Pine OB → Pine FVG → Python OB → Python FVG → swing lookback → ATR fallback.

Sonuca ek kontrol:
```python
def _push_past_htf_sr(
    sl: float,
    entry: float,
    direction: Direction,
    htf_zones: list[SRZone],
    buffer_atr: float,
    atr: float,
) -> float:
    """If an HTF zone lies between entry and sl, push sl past the zone."""
    buffer = buffer_atr * atr
    for zone in htf_zones:
        if direction == Direction.BULLISH:
            # long — sl entry'nin altında; zone sl ile entry arası mı?
            if sl < zone.bottom < entry:
                sl = min(sl, zone.bottom - buffer)
        else:  # BEARISH
            if entry < zone.top < sl:
                sl = max(sl, zone.top + buffer)
    return sl
```

**2. TP projection — yol üstündeki direnç/destek ceiling**

```python
def _apply_htf_tp_ceiling(
    tp: float,
    entry: float,
    direction: Direction,
    htf_zones: list[SRZone],
    buffer_atr: float,
    atr: float,
) -> float:
    """If an HTF reversal zone sits between entry and target tp, pull tp short of it."""
    buffer = buffer_atr * atr
    for zone in htf_zones:
        if direction == Direction.BULLISH:
            # long — tp entry'nin üstünde; ters zonu (resistance) arıyoruz
            if zone.role in ("RESISTANCE", "MIXED") and entry < zone.bottom < tp:
                tp = min(tp, zone.bottom - buffer)
        else:
            if zone.role in ("SUPPORT", "MIXED") and tp < zone.top < entry:
                tp = max(tp, zone.top + buffer)
    return tp
```

**3. Plan üretimi**
- SL'i hesapla → `_push_past_htf_sr` uygula
- TP'yi R:R ile hesapla → `_apply_htf_tp_ceiling` uygula
- Yeni R:R'ı hesapla → `min_rr_ratio` altındaysa trade iptal (`logger.info("trade_rejected_rr_too_low ...")`)

**4. Config**
```yaml
analysis:
  htf_sr_ceiling_enabled: true
  htf_sr_buffer_atr: 0.2
```

### Test
`tests/test_htf_sr_integration.py`:
- Long, entry=100, atr=1: HTF resistance zone 103-104 varsa TP=105 hedefi → TP 102.8'e çekilmeli
- Long, entry=100: HTF support zone 97-98 varsa SL=96 planlandı → SL 96.8'e (zonun altına + buffer) itilmeli
- HTF zone TP'den sonra (TP=102, zone=105) → TP değişmemeli
- HTF S/R devre dışıysa (`enabled=false`) → eski davranış
- R:R min altına düşerse → trade reject

---

## MADDE E — Partial TP + SL-to-BE

### Hedef
TP1'de pozisyonun yarısını kapat + SL'i break-even'a taşı → kalan yarı "risk-free" trendi kovalasın. Trailing ilk sürümde kapalı; sadece iki kademeli TP.

### Değişiklikler

**1. `src/execution/order_router.py` — partial mode**

Mevcut: tek OCO algo (`place_oco_algo(sl, tp, size=full)`).

Yeni (partial_tp_enabled iken):
```python
# Entry market order (değişmedi)
entry = self.client.place_market_order(...)

if self.config.partial_tp_enabled:
    size1 = int(plan.num_contracts * self.config.partial_tp_ratio)
    size2 = plan.num_contracts - size1

    # Edge case — bölünemeyen pozisyon (1 contract veya ratio pay'i 0'a yuvarlanırsa)
    if size1 == 0 or size2 == 0:
        logger.info("partial_tp_fallback_to_single contracts={} ratio={}",
                    plan.num_contracts, self.config.partial_tp_ratio)
        algo = self.client.place_oco_algo(...)   # normal tek-algo akışına düş
    else:
        tp1_price = plan.entry_price + (plan.entry_price - plan.sl_price) * self.config.partial_tp_rr * sign
        algo1 = self.client.place_oco_algo(
            ..., sl_trigger_px=plan.sl_price, tp_trigger_px=tp1_price, size_contracts=size1,
        )
        algo2 = self.client.place_oco_algo(
            ..., sl_trigger_px=plan.sl_price, tp_trigger_px=plan.tp_price, size_contracts=size2,
        )
        # ExecutionReport'a algo_ids = [algo1.id, algo2.id] olarak yaz (aşağıda journal uyumu)
else:
    algo = self.client.place_oco_algo(...)
```

**2. `src/execution/position_monitor.py` — TP1 fill → SL-to-BE (cancel + re-place)**

`python-okx` 0.4.x'te `amend_algo_order` wrapper'ı güvenilir değil (sürüme göre yok ya da yalnızca belirli alanları kabul ediyor). Bu yüzden **amend** yerine **cancel + new** pattern'i kullan:

```python
if current_size < expected_size and current_size > 0:
    # TP1 fill oldu — kalan algo2'yi BE SL ile yeniden oluştur
    if self.config.move_sl_to_be_after_tp1 and not fill.be_already_moved:
        try:
            await self.client.cancel_algo_order(algo_id=fill.algo2_id, inst_id=fill.inst_id)
            new_algo = await self.client.place_oco_algo(
                inst_id=fill.inst_id,
                side=fill.closing_side,
                pos_side=fill.pos_side,
                sl_trigger_px=fill.entry_price,          # BE
                tp_trigger_px=fill.tp2_price,
                size_contracts=current_size,             # kalan contract
                sl_ord_px="-1",
                tp_ord_px="-1",
            )
            fill.algo2_id = new_algo.algo_id
            fill.be_already_moved = True
            logger.info("sl_moved_to_be_via_replace trade_id={} new_algo={}",
                        fill.trade_id, new_algo.algo_id)
            # Journal'da algo_ids listesini UPDATE et (aşağıda helper)
            await self.journal.update_algo_ids(fill.trade_id, [fill.algo1_id, new_algo.algo_id])
        except Exception as e:
            # Cancel veya re-place başarısız — mevcut algo2 yerinde duruyor olabilir
            logger.warning("sl_be_cancel_failed trade_id={} err={!r}", fill.trade_id, e)
            # Flag'i set ETME — bir sonraki poll'da tekrar denensin
```

**Sıra önemli** — önce `cancel_algo_order`, sonra `place_oco_algo`. İkisi arasında kısa süre (<1s) pozisyon algo-koruması altında değil. OKX mark-fiyat tick rate'i bu pencerede liquidation tetikleyemeyecek kadar yavaş, ama arada `sleep`/blocking I/O koyma.

**3. `src/execution/okx_client.py` — yeni helper'lar**
- `cancel_algo_order(algo_id, inst_id)` — `python-okx` `TradeAPI.cancel_algo_order` wrapper'ı varsa onu çağır; yoksa REST endpoint `/api/v5/trade/cancel-algos` ile manuel POST (`_check()` envelope validator zaten mevcut).
- `place_oco_algo` imzası zaten var — yeni bir şey gerekmez, yalnızca `size_contracts` parametresinin partial size için doğru döndüğü test edilmeli.

**4. Config**
```yaml
execution:
  partial_tp_enabled: true
  partial_tp_ratio: 0.5               # ilk TP'de kapatılacak contract oranı
  partial_tp_rr: 1.5                  # TP1 = entry + risk * 1.5
  move_sl_to_be_after_tp1: true
  trail_after_partial: false          # v1'de kapalı — sonra açılacak
```

**5. ExecutionReport + Journal uyum**

Mevcut `trades` tablosunda tek bir algo_id TEXT kolonu var. İki algo ID + re-placement'ı desteklemek için:

- Yeni `algo_ids` TEXT kolonu ekle — JSON list olarak yaz (`'["abc123","def456"]'`). Eski `algo_id` kolonu geri uyum için kalsın; yeni yazımlarda `list[0]` doldur.
- `TradeJournal.connect()` içindeki şema kurulumuna idempotent ALTER ekle (eski DB'ler için):
  ```python
  try:
      await db.execute("ALTER TABLE trades ADD COLUMN algo_ids TEXT")
  except aiosqlite.OperationalError:
      pass  # kolon zaten var
  try:
      await db.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT")
  except aiosqlite.OperationalError:
      pass
  ```
  (`close_reason` MADDE F'de kullanılacak.)
- Yeni helper: `TradeJournal.update_algo_ids(trade_id, ids: list[str])` — SL-to-BE re-placement sonrası çağrılır.

Phase 7 dataset bu kolon üzerinden iki-algo ayrımını öğrenmek zorunda değil — RL state'i için önemsiz, sadece operatör teşhisi için.

### Test
`tests/test_partial_tp.py`:
- `partial_tp_enabled=True` → `place_oco_algo` iki kez çağrılmış mı? Size'lar toplamı num_contracts'a eşit mi?
- TP1 fill simülasyonu (fake monitor) → `modify_algo` çağrılmış mı? Yeni SL = entry_price mi?
- `partial_tp_enabled=False` → mevcut davranış korunmuş mu (tek algo)?
- `partial_tp_ratio=0.5` + `num_contracts=7` (tek sayı) → size1=3, size2=4 (veya tam tersi, toplam 7 olmalı)
- `num_contracts=1` + `partial_tp_enabled=True` → tek algo fallback (`partial_tp_fallback_to_single` log; `place_oco_algo` bir kez çağrılır)
- SL-to-BE başarılı akış → `cancel_algo_order` çağrıldı mı + yeni `place_oco_algo` (sl=entry) çağrıldı mı + `update_algo_ids` journal'da doğru ID'ler yazdı mı
- SL-to-BE cancel-fail senaryosu (cancel exception atar) → `be_already_moved=False` kalıyor mu (bir sonraki poll'da tekrar denenebilir); log `sl_be_cancel_failed` çıkıyor mu

---

## MADDE F — LTF reversal early close

### Hedef
Pozisyon açıkken 1m LTF'de güçlü ters sinyal gelirse defensif olarak kapat. **Flip yok** — sadece çık, bir sonraki cycle yeniden değerlendirir (MADDE C cooldown'ı hâlâ uygulanır).

### Değişiklikler

**1. `src/bot/runner.py` — `_run_one_symbol` içinde**

Pozisyon açıksa ve LTF reader verisi varsa:
```python
open_side = self._get_open_side(symbol)
if open_side is not None and cfg.execution.ltf_reversal_close_enabled:
    ltf = self.ctx.ltf_cache.get(symbol)
    if ltf and self._is_ltf_reversal(ltf, open_side):
        await self._defensive_close(symbol, open_side, reason="ltf_reversal")
        return  # bu cycle bu sembol için yeni entry değerlendirme
```

**2. `_is_ltf_reversal`**
```python
def _is_ltf_reversal(self, ltf: LTFState, open_side: str) -> bool:
    cfg = self.ctx.config.execution
    if open_side == "long":
        reversed_ = (
            ltf.trend == Direction.BEARISH
            and ltf.last_signal in ("SELL", "SELL_DIV", "BLOOD_DIAMOND_SELL")
            and ltf.last_signal_bars_ago <= cfg.ltf_reversal_signal_max_age
        )
    else:
        reversed_ = (
            ltf.trend == Direction.BULLISH
            and ltf.last_signal in ("BUY", "BUY_DIV", "GOLD_BUY", "YELLOW_X_BUY")
            and ltf.last_signal_bars_ago <= cfg.ltf_reversal_signal_max_age
        )
    return reversed_
```

**3. Minimum holding time guard**
Çok erken flip'i önle — pozisyon açıldıktan en az N mum geçsin:
```python
opened_at = self.ctx.open_trade_opened_at.get((symbol, open_side))
if opened_at:
    elapsed_bars = (now - opened_at).total_seconds() / _tf_seconds(cfg.trading.entry_timeframe)
    if elapsed_bars < cfg.execution.ltf_reversal_min_bars_in_position:
        return  # ignore, too early
```

**4. `BotContext` ek alanlar** (MADDE B'deki BotContext ekleme bloğuna birleştir)
```python
defensive_close_in_flight: set[tuple[str, str]] = field(default_factory=set)
pending_close_reasons: dict[tuple[str, str], str] = field(default_factory=dict)
open_trade_opened_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
```

**5. `_defensive_close`**
```python
async def _defensive_close(self, symbol: str, side: str, reason: str):
    # Idempotent guard — aynı cycle'da iki kez çağrılmamalı
    if (symbol, side) in self.ctx.defensive_close_in_flight:
        return
    self.ctx.defensive_close_in_flight.add((symbol, side))

    # 1) AKTİF TÜM algo order'ları iptal et — partial mode'da iki algo_id de iptal edilmeli;
    #    algo2 hâlâ aktifse market close'u OKX reject edebilir (size mismatch).
    for algo_id in self._active_algo_ids(symbol, side):
        try:
            await self.ctx.okx_client.cancel_algo_order(algo_id=algo_id, inst_id=inst_id)
        except Exception as e:
            logger.warning("defensive_cancel_failed algo={} err={!r}", algo_id, e)

    # 2) Market close — `okx_client.close_position(inst_id, pos_side)`
    await self.ctx.okx_client.close_position(inst_id, pos_side=side)

    # 3) Close reason'ı stamp et — monitor poll'u CloseFill'i üretince `_process_closes`
    #    `pending_close_reasons` sözlüğünden okuyup `close_reason` kolonuna yazar.
    self.ctx.pending_close_reasons[(symbol, side)] = "EARLY_CLOSE_LTF_REVERSAL"

    # 4) ÖNEMLİ — burada manuel olarak `record_close`, `register_trade_closed`,
    #    `open_trade_ids` pop veya `last_close` update YAPMA. `_process_closes`
    #    monitor'dan gelen enriched CloseFill ile bunları zaten yapıyor.
    #    Çift kayıt riski yüksek (enriched PnL yerine 0 yazma tuzağı).

    # 5) Flag temizliği: `_process_closes` enrich ettikten sonra
    #    `defensive_close_in_flight.discard((symbol, side))` çağrılır.
```

**Akış özeti:** defensive_close → algo cancel → market close → reason stamp. Geri kalan (journal `record_close`, `last_close` update, `open_trade_ids` pop) bir sonraki poll'da `_process_closes` tarafından enriched CloseFill ile yapılır. Bu sırayla `pnl_usdt=0` yazılma riski yok — enrichment `OKXClient.enrich_close_fill` (`positions-history` endpoint) üzerinden gerçekleşir.

**6. Config**
```yaml
execution:
  ltf_reversal_close_enabled: true
  ltf_reversal_min_confluence: 3        # şimdilik LTFState'te confluence yok; opsiyonel gelecek kullanım
  ltf_reversal_min_bars_in_position: 2
  ltf_reversal_signal_max_age: 3        # LTF signal bars_ago <= 3 olmalı (taze olsun)
```

### Test
`tests/test_ltf_reversal.py`:
- Long pozisyon + LTF BEARISH trend + fresh SELL signal → close çağrılıyor
- Long pozisyon + LTF BEARISH + eski sell signal (bars_ago=10) → çağrılmıyor
- min_bars_in_position=3, elapsed=1 → çağrılmıyor (henüz erken)
- Aynı yön LTF sinyali → hiçbir şey yapma
- `ltf_reversal_close_enabled=false` → tüm koşullar sağlansa bile close yok

---

## Teslimat kontrolü

Tüm maddelerden sonra:

```bash
# Unit testler
.venv/Scripts/python.exe -m pytest tests/ -v

# Smoke test — dry-run, tek tick, 5 sembol, 3 TF
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Demo veri toplama — 5 paritede 50 kapalı işleme kadar çalıştır (Phase 7 dataset)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --max-closed-trades 50
```

### Kabul kriterleri (checklist)
- [ ] `pytest tests/ -v` tamamı yeşil
- [ ] Dry-run log'unda 5 sembolün de set_symbol'ü çağrıldığı görülüyor
- [ ] Dry-run log'unda her sembol için HTF → LTF → entry TF geçişleri görülüyor
- [ ] Manuel demo test: TP hit olduktan sonra aynı pariteden aynı yönde re-entry cooldown log'u çıkıyor (`cooldown_active` / `insufficient_move` / `confluence_not_improved`)
- [ ] Manuel demo test: HTF direnç yakınında plan üretildiğinde TP'nin `htf_sr_ceiling` ile düşürüldüğü log'ta var
- [ ] Manuel demo test: Partial TP aktifken OKX demo'da iki algo order görülüyor, TP1 sonrası algo2'nin SL'i BE'ye kaydırıldı
- [ ] Manuel demo test: LTF reversal tetiklendiğinde journal'da `EARLY_CLOSE_LTF_REVERSAL` reason'ı görülüyor
- [ ] `CLAUDE.md` güncel — yeni config anahtarları, yeni pariteler, Phase 6.5 bölümü eklenmiş
- [ ] `config/default.yaml` yorum satırlarıyla birlikte net, yeni bir operatörün anlayabileceği durumda

### Commit sırası (önerilen)
1. `feat: multi-pair round-robin (Madde A)`
2. `feat: multi-TF data pipeline — 1m/3m/15m (Madde B)`
3. `feat: per-side reentry cooldown + quality gate (Madde C)`
4. `feat: HTF S/R integration in SL/TP selection (Madde D)`
5. `feat: partial TP + SL-to-BE (Madde E)`
6. `feat: LTF reversal defensive close (Madde F)`
7. `docs: update CLAUDE.md for Phase 6.5`

---

## Not — Phase 7'ye hazırlık anlamı

Bu 6 madde tamamlandıktan sonra bot:
- 5 paritede demo işlem toplayabiliyor olacak → 100+ işlem hedefine hızla ulaşır
- Aynı setup'ı iki kere oynamadığı için journal verisi "temiz" olacak (RL eğitimi için kritik)
- Multi-TF bakışı sayesinde SL/TP seçimleri tutarlı olacak → reward sinyali gürültüsüz
- Defensive close sayesinde tail-risk trade'ler azalacak → DD istatistikleri sağlıklı olacak

RL ancak disiplinli bir rule-based strateji üzerine iyi parameter tuner olur. Bu yüzden Phase 7 başlamadan bu 6 madde bitmeli.
