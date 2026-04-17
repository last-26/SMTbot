# Phase 1.5 — Derivatives Data Layer (Likidasyon + Coinalyze + Tahmini Heatmap)

## Context

Crypto futures trading bot projesi Phase 1-6 tamamlandı, Phase 7 öncesi multi-pair / multi-TF iyileştirmeleri (phase7_prep_prompt.md) üzerinde çalışılıyor. Bu doküman, o hazırlık paketinin **yanında paralel** ilerletilecek yeni bir mimari katmanı açıyor: **türev piyasa verisi**.

> **ÖNEMLİ — Parite listesi güncellemesi:**
> Phase 7 prep'te 5 parite (BTC/ETH/SOL/AVAX/XRP) vardı. Bu planlama sırasında tekrar değerlendirildi ve **3 pariteye düşürüldü: BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP**.
>
> Gerekçeler:
> - `max_concurrent_positions: 3` ile birebir örtüşüyor — tüm pariteler aynı anda dolabilir
> - 3dk entry TF'te bir cycle ~51s → mum başına ~2 tarama sığar (5 parite'de ~85s, mumun yarısından fazlası tarama)
> - TV freshness: rotasyon sonunda 1. sembole döndüğünde daha taze veri
> - Coinalyze rate budget: 5 parite × 5 call = 25/min yerine 3 × 5 = 15/min (refresh interval'ı 45-60s'de rahat)
> - Majors'ta konsantre likidite: BTC (patron), ETH (2. en likid), SOL (derivatives verisi yüksek, volatilite fırsat veriyor)
>
> Bu doküman boyunca kod örneklerinde `watched_symbols` / `cfg.trading.symbols` **3 sembol** içerir. Phase 7 prep'teki 5 paritelik config örnekleri bu değişikliğe göre override edilecek.

Şu an bot sadece fiyat yapısına (MSS / FVG / OB / sweep / S-R / LTF signals) bakarak pozisyon alıyor. Ancak vadeli piyasalarda fiyatın gittiği yer büyük ölçüde **likidasyon kümelerinin** ve **pozisyon yoğunluğunun** nerede olduğuyla ilgili. Coinglass gibi ücretli servislerin yaptığı şey aslında kamuya açık verilerden (Binance WebSocket `forceOrder` stream + Coinalyze aggregated API) türetilebiliyor. Bu katman eklendiğinde:

- Confluence score daha zengin olur (yapı + derivatives rejimi)
- Crowded trade tuzaklarından (herkes long'ken long açmak gibi) kaçınılabilir
- RL feature vector'ı sadece fiyat-yapı değil, pozisyonlanma bilgisini de görür
- Likidasyon kümelerine doğru fiyat çekildiğinde proaktif hazırlık yapılabilir

Bu dokümandaki **7 maddeyi sırayla** uygulamanı istiyorum. Her madde bağımsız commit olsun, her birinin testi olsun.

---

## Project Alignment Overrides (2026-04-17 review)

> Bu prompt Claude web UI'da (GitHub repo paylaşımı ile) hazırlandığı için bazı detaylar gerçek kod tabanıyla örtüşmüyor. **Implementasyon sırasında bu override'lar prompt içindeki eski bilgilere üstündür.**

### 1. Parite listesi + eşzamanlı pozisyon (bu commit ile sabitlendi)
- `config/default.yaml → trading.symbols = [BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP]`
- `trading.max_concurrent_positions: 3` — 3 parite × 3 slot eşleşmesi için 2'den 3'e çıkarıldı
- Prompt içinde karşılaşacağın 5 paritelik eski örnekler / rate-budget hesapları (özellikle Madde 2'deki "5 sembol × 4 endpoint = 20 istek" satırı) **geçersiz** — daima 3 sembolle hesapla.

### 2. Phase 6.5 ZATEN tamamlandı
- Phase 7 prep (`phase7_prep_prompt.md`) Madde A-F hepsi merge edildi → CLAUDE.md Phase 6.5 bölümüne bak. O dosya repo'dan silindi (git `D`).
- Bu prompt "Madde A + B bittikten sonra başla" diyor — koşul zaten sağlandı, doğrudan Phase 1.5'e başlanabilir.

### 3. `calculate_confluence` gerçek yeri ve imzası
- Prompt `src/strategy/entry_signals.py` diyor — **gerçek konum `src/analysis/multi_timeframe.py:291`**.
- Mevcut mimari **weighted**: `calculate_confluence` → `score_direction` → `ConfluenceScore(direction, score: float, factors: list[ConfluenceFactor])`. Prompt'taki `tuple[int, list[str]]` **kullanılmayacak**.
- Madde 6'daki "+1 slot" aslında `score_direction()` içine yeni `ConfluenceFactor` eklemek demek. Öneri: 3 ayrı factor (`derivatives_contrarian`, `derivatives_heatmap_target`, `derivatives_capitulation`) — aynı cycle'da en fazla bir tanesi eklenir, weight `DEFAULT_WEIGHTS`'a konur (RL Phase 7'de tune eder).
- Mevcut 11 faktör var (prompt "5 faktör" yazıyor — güncel değil).

### 4. `BotContext` ayrı dosyada DEĞİL
- Prompt `src/bot/context.py` sanıyor — **gerçekte `src/bot/runner.py:135` içinde dataclass**. Yeni alanlar (`liquidation_stream`, `derivatives_cache`, `coinalyze_client`) buraya eklenecek; **ayrı dosya oluşturma**.

### 5. `MarketState` field eklemeleri açıkça
- `src/data/models.py` Pydantic `MarketState`'e iki opsiyonel field: `derivatives: Optional[DerivativesState] = None`, `liquidity_heatmap: Optional[LiquidityHeatmap] = None`.
- `MarketState.atr` property **zaten var** (`signal_table.atr_14` üzerinden) — yeniden tanımlama.
- Doldurma yeri: `runner._run_one_symbol` içinde `read_market_state` sonrası — `state.derivatives = ctx.derivatives_cache.get(symbol)` ve `state.liquidity_heatmap = build_heatmap(...)`.

### 6. Startup/shutdown lifecycle pattern
- Prompt "async def startup/shutdown" public metotları varsayıyor — **mevcutta yok**. Pattern: `BotRunner.from_config` ctor wiring + `_prime` priming + `install_shutdown_handlers(self.shutdown)` + main loop.
- Derivatives bootstrap `from_config` sonuna; cascade stop (stream → cache → coinalyze.close) shutdown event set olduktan sonra main loop'un `finally`'sinde.

### 7. Migration pattern birebir uyum
- `trades` tablosuna `ALTER TABLE` satırları (Madde 7) mevcut `_MIGRATIONS` listesine eklenmeli (`src/journal/database.py:109`). Liste her `connect()` çağrısında try/except `aiosqlite.OperationalError` ile döner — idempotent garanti.
- Yeni tablolar (`liquidations`, `derivatives_snapshots`) `CREATE TABLE IF NOT EXISTS` olarak aynı init path'e girmeli (ayrı `DerivativesJournal.ensure_schema()` de olabilir; tek DB dosyası paylaşılıyor).

### 8. Entry gate çağrı yeri
- `should_skip_for_derivatives` prompt runner'da çağırıyor — **gerçek entry kararı `src/strategy/entry_signals.py:build_trade_plan_from_state`'de alınıyor**. Gate buraya taşınmalı:
  - Fonksiyon imzasına `deriv_state: Optional[DerivativesState] = None`, `cfg_derivatives = None` eklensin.
  - Confluence hesabından **önce** skip kontrolü → `return None` (Madde D'deki HTF S/R reject pattern'iyle birebir aynı).
  - Runner `_run_one_symbol` içinden `state.derivatives` + `cfg.derivatives`'i plan builder'a forward etsin.

### 9. Eksik CLI/script ekleri
- `--derivatives-only` + `--duration` bayrakları `src/bot/__main__.py` argparse'ına yeni eklenecek (mevcut değil).
- `scripts/report.py` içinde `regime_breakdown()` sıfırdan yazılacak — mevcut sadece `format_summary` çağırıyor.
- `BotConfig` içine `derivatives: DerivativesConfig` field'ı (`src/bot/config.py`). Default `enabled: False` — eski config dosyaları kırılmasın.

---

## Bu Phase'in Phase 7 prep ile ilişkisi

- Phase 7 prep **rule-based stratejiyi disiplinli hale getiriyor** (multi-pair, multi-TF, cooldown, partial TP, LTF reversal)
- Phase 1.5 **yeni bir veri kaynağı katmanı ekliyor** (derivatives)
- İkisi çakışmaz, aynı mimariye farklı yerlerden temas ederler
- Önerilen sıra: Phase 7 prep'in **Madde A (multi-pair) ve Madde B (multi-TF)** bittikten sonra bu doküman başlar. Çünkü bu doküman her sembol için ayrı derivatives state tutuyor — multi-pair altyapısı olmazsa tekilleştirilmesi zor
- Phase 7 prep'in **Madde C-F'si** (cooldown, HTF S/R, partial TP, LTF reversal) bu dokümanla **paralel** yürüyebilir — aynı `BotContext`'i paylaşacaklar

---

## Mevcut mimari (hatırlatma)

- `src/bot/runner.py` — poll loop, `_run_one_symbol` multi-pair round-robin
- `src/data/tv_bridge.py`, `src/data/structured_reader.py`, `src/data/candle_buffer.py`, `src/data/okx_bridge.py`
- `src/analysis/` — market_structure, support_resistance, fvg, order_blocks, liquidity (mevcut: sweep/equal H-L), multi_timeframe (confluence)
- `src/strategy/` — risk_manager, SL/TP seçici, plan üretici
- `src/execution/order_router.py` + `position_monitor.py`
- `src/journal/` — aiosqlite trade journal
- Config: `config/default.yaml` + pydantic `BotConfig`

Bu dokümanda eklenecek yeni modüller:
```
src/data/
├── liquidation_stream.py    (YENİ - Binance WebSocket forceOrder)
├── derivatives_api.py       (YENİ - Coinalyze REST client)
└── derivatives_cache.py     (YENİ - per-symbol rolling snapshot)

src/analysis/
├── liquidation_clusters.py  (YENİ - gerçekleşmiş likidasyon kümeleri)
├── liquidity_heatmap.py     (YENİ - tahmini likidasyon haritası)
└── derivatives_regime.py    (YENİ - funding/OI/L-S rejim tespiti)
```

---

## Genel kurallar (her maddeye uygulanır)

1. **Önce oku, sonra yaz** — her maddeden önce ilgili dosya(lar)ı `view` ile incele
2. **Minimal değişiklik** — mevcut testleri kırma, mevcut public API'yi bozma
3. **Her madde için test yaz** — `tests/test_<modul>.py` (pytest)
4. **Geriye dönük uyum** — eski config dosyaları çalışmaya devam etsin
5. **Her madde ayrı commit** — `feat: <madde>` prefix'i
6. **Her maddeden sonra** `.venv/Scripts/python.exe -m pytest tests/ -v` — tüm testler geçmeli
7. **Smoke test** — `python -m src.bot --config config/default.yaml --dry-run --once`
8. **Failure isolation** — derivatives katmanı çökerse bot çökmemeli, sadece o cycle için derivatives boost'u skip edilmeli. Tüm erişimler `try/except` + fallback değerle sarılmalı.

## Yapmayacağın şeyler

- Phase 7 (RL) başlatma — bu doküman Phase 7 **öncesi** hazırlık
- Pine Script'lere dokunma
- `OKX_DEMO_FLAG=1` guard'ını bozma
- Circuit breaker'ları bozma
- Coinalyze rate limit'ini delme (40 req/min; adaptive backoff zorunlu)
- Binance WebSocket'i API key ile açma — `!forceOrder@arr` public stream, auth yok
- Ücretli API entegrasyonu — Coinalyze free tier + Binance public WS yeterli
- Confluence score'u bu katmanla "şişirme" — Madde F'de confluence'a etkisi **+1'lik bir slot** olacak, daha fazlası overfit riskli

---

## Kurulum — Çalışmaya başlamadan önce

### 1. Coinalyze API Key

`.env` dosyana şu satırı ekle (dosya zaten varsa en alta):

```
# Coinalyze API — free tier, 40 req/min
# Keyi almak için: https://coinalyze.net/account/api-key/
COINALYZE_API_KEY=senin_keyin_buraya
```

`.env.example` dosyasına da aynı satırları (değer olmadan) ekle ki versiyon kontrolünde görünsün:
```
COINALYZE_API_KEY=
```

### 2. Bot config'inde derivatives'ı etkinleştir

`config/default.yaml` içinde yeni `derivatives` bölümü olacak — Madde 1, 2, 4, 5, 6 her biri ekleyecek anahtarlar. Varsayılan `enabled: false` — her şey hazır olana kadar kapalı tutulabilir, Madde 7'den sonra `true` yapılacak.

### 3. Probe script — API erişimini doğrula (implementasyondan önce)

`scripts/probe_coinalyze.py` oluştur. Bu script Madde 1 kod yazılmadan önce çalıştırılıp gerçek API response'larını gözlemlemek için kullanılır:

```python
"""Coinalyze API probe — gerçek response şemasını runtime'da doğrula.

Kullanım: .venv/Scripts/python.exe scripts/probe_coinalyze.py
"""
import asyncio, json, os, time
from dotenv import load_dotenv
import httpx

load_dotenv()
API_KEY = os.getenv("COINALYZE_API_KEY")
BASE = "https://api.coinalyze.net/v1"

async def probe():
    assert API_KEY, "COINALYZE_API_KEY missing in .env"
    async with httpx.AsyncClient(base_url=BASE, headers={"api_key": API_KEY}, timeout=10) as c:
        # 1) Future markets (BTC için Binance/Bybit/OKX mevcut mu?)
        r = await c.get("/future-markets")
        markets = r.json()
        btc_usdt = [m for m in markets if m["base_asset"] == "BTC"
                    and m["quote_asset"] == "USDT" and m["is_perpetual"]]
        print(f"BTC/USDT perpetual markets: {len(btc_usdt)}")
        for m in btc_usdt[:5]:
            print(f"  {m['symbol']:25s} exchange={m['exchange']:10s} margined={m['margined']}")

        # 2) Binance BTCUSDT perp seç
        binance_btc = next((m["symbol"] for m in btc_usdt if m["symbol"].endswith(".A")), None)
        assert binance_btc, "Binance BTC perp bulunamadı"
        print(f"\nChosen: {binance_btc}")

        # 3) Her endpoint'in gerçek response'unu göster
        endpoints = [
            ("/open-interest", {"symbols": binance_btc, "convert_to_usd": "true"}),
            ("/funding-rate", {"symbols": binance_btc}),
            ("/predicted-funding-rate", {"symbols": binance_btc}),
            ("/long-short-ratio-history", {
                "symbols": binance_btc, "interval": "1hour",
                "from": int(time.time())-7200, "to": int(time.time())}),
            ("/liquidation-history", {
                "symbols": binance_btc, "interval": "1hour",
                "from": int(time.time())-3600, "to": int(time.time()),
                "convert_to_usd": "true"}),
        ]
        for path, params in endpoints:
            r = await c.get(path, params=params)
            print(f"\n{path}:")
            print(json.dumps(r.json(), indent=2)[:500])

if __name__ == "__main__":
    asyncio.run(probe())
```

Bu script'i Madde 2 implementasyonundan **önce** çalıştır — gerçek payload şemaları promptta yazdığımla birebir örtüşüyor mu doğrula. Örtüşmüyorsa schema'yı prompt'taki şemalara göre değil **gerçek response'a göre** implement et.

---

## Öngörülen bağımlılıklar

`requirements.txt`'e eklenecek yeni bir şey yok — `websockets>=12.0` ve `httpx>=0.27` zaten mevcut.

---

## MADDE 1 — Binance Liquidation Stream (`liquidation_stream.py`)

### Hedef

Binance Futures `!forceOrder@arr` WebSocket stream'ini dinleyerek tüm pariteler için zorla likidasyon emirlerini gerçek zamanlı toplamak. SQLite'a yaz + in-memory son 24h buffer tut.

### Önemli Binance davranışı (dikkat)

- Endpoint: `wss://fstream.binance.com/ws/!forceOrder@arr` (public, auth yok)
- Binance 2025 güncellemesi: 1000ms pencerede sadece **en büyük** likidasyon emri yayınlanıyor (eskiden "latest" idi)
- Yani toplam hacim olduğundan az görünür — bu yüzden Madde 2'de (Coinalyze) aggregated fallback'i var
- Event payload: `{"e":"forceOrder","E":<ts>,"o":{"s":"BTCUSDT","S":"BUY"|"SELL","q":"...","p":"...","ap":"...","T":<ts>, ...}}`
- `S=SELL` → **LONG likidasyonu** (long pozisyon zorla kapatıldı, sell emriyle)
- `S=BUY` → **SHORT likidasyonu**

### Sembol mapping

Binance sembolleri OKX'ten farklı. Mapping helper:
```python
def okx_to_binance_symbol(okx_symbol: str) -> str:
    """'BTC-USDT-SWAP' → 'BTCUSDT'"""
    return okx_symbol.replace("-SWAP", "").replace("-", "")

# Ters mapping (WS'den gelen sembolleri internal format'a çevir)
def binance_to_okx_symbol(binance_symbol: str) -> str:
    """'BTCUSDT' → 'BTC-USDT-SWAP'  (sadece ana stablecoin çiftleri)"""
    # Basit heuristic: sadece USDT için (BUSD, USDC vs. ignore)
    if binance_symbol.endswith("USDT"):
        base = binance_symbol[:-4]
        return f"{base}-USDT-SWAP"
    return None  # ignore
```

### Değişiklikler

**1. Yeni dosya `src/data/liquidation_stream.py`**

```python
"""Binance Futures liquidation stream dinleyicisi.
!forceOrder@arr endpoint'ini dinler, sadece izlenen sembollerin
likidasyonlarını in-memory buffer + SQLite'a yazar.

Failure policy: WebSocket kopar → exponential backoff reconnect.
Parser exception → log + skip, asla crash.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import websockets
from loguru import logger

BINANCE_FORCE_ORDER_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

@dataclass(frozen=True)
class LiquidationEvent:
    symbol: str           # OKX format: 'BTC-USDT-SWAP'
    side: str             # 'LONG_LIQ' (long liquidated) | 'SHORT_LIQ'
    price: float          # fill avg price
    quantity: float       # base asset quantity
    notional_usd: float   # price * quantity
    ts_ms: int            # trade timestamp (ms)

class LiquidationStream:
    def __init__(
        self,
        watched_symbols: list[str],   # OKX format, e.g. ['BTC-USDT-SWAP', ...]
        buffer_size_per_symbol: int = 5000,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 60.0,
    ):
        self.watched = set(watched_symbols)
        self.buffers: dict[str, deque[LiquidationEvent]] = {
            s: deque(maxlen=buffer_size_per_symbol) for s in self.watched
        }
        self._journal = None  # DerivativesJournal enjekte edilecek (Madde 3)
        self._stop = asyncio.Event()
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._task: Optional[asyncio.Task] = None

    def attach_journal(self, journal):
        self._journal = journal

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self):
        backoff = self._reconnect_min_s
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    BINANCE_FORCE_ORDER_URL,
                    ping_interval=180,   # Binance pings every 3min
                    ping_timeout=60,
                ) as ws:
                    logger.info("liquidation_stream_connected url={}", BINANCE_FORCE_ORDER_URL)
                    backoff = self._reconnect_min_s
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self._handle(raw)
                        except Exception as e:
                            logger.warning("liq_parse_failed err={!r}", e)
            except Exception as e:
                logger.warning("liq_ws_disconnected err={!r} backoff={}s", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max_s)

    def _handle(self, raw: str):
        msg = json.loads(raw)
        o = msg.get("o") or {}
        binance_sym = o.get("s", "")
        okx_sym = binance_to_okx_symbol(binance_sym)
        if okx_sym is None or okx_sym not in self.watched:
            return  # not in watchlist

        side_raw = o.get("S")  # BUY or SELL
        side = "LONG_LIQ" if side_raw == "SELL" else "SHORT_LIQ"
        price = float(o.get("ap") or o.get("p") or 0)
        qty = float(o.get("q") or 0)
        if price <= 0 or qty <= 0:
            return

        ev = LiquidationEvent(
            symbol=okx_sym,
            side=side,
            price=price,
            quantity=qty,
            notional_usd=price * qty,
            ts_ms=int(o.get("T") or msg.get("E") or time.time() * 1000),
        )
        self.buffers[okx_sym].append(ev)
        if self._journal:
            # fire-and-forget; journal kendi hata yönetimini yapar
            asyncio.create_task(self._journal.insert_liquidation(ev))

    # --- Query API ---

    def recent(self, symbol: str, lookback_ms: int) -> list[LiquidationEvent]:
        buf = self.buffers.get(symbol)
        if not buf:
            return []
        cutoff = int(time.time() * 1000) - lookback_ms
        return [e for e in buf if e.ts_ms >= cutoff]

    def stats(self, symbol: str, lookback_ms: int) -> dict:
        """Özet istatistikler — runner'ın derivatives_regime analizine girdi."""
        events = self.recent(symbol, lookback_ms)
        long_liqs = [e for e in events if e.side == "LONG_LIQ"]
        short_liqs = [e for e in events if e.side == "SHORT_LIQ"]
        return {
            "long_liq_notional": sum(e.notional_usd for e in long_liqs),
            "short_liq_notional": sum(e.notional_usd for e in short_liqs),
            "long_liq_count": len(long_liqs),
            "short_liq_count": len(short_liqs),
            "max_liq_notional": max((e.notional_usd for e in events), default=0),
        }
```

**2. SQLite schema (Madde 3'te detaylı — burada sadece placeholder)**

**3. `BotConfig` — `src/bot/config.py` içinde `DerivativesConfig`**
```python
class DerivativesConfig(BaseModel):
    enabled: bool = False  # Varsayılan KAPALI — opt-in
    liquidation_buffer_size: int = 5000
    liquidation_lookback_1h_ms: int = 60 * 60 * 1000
    liquidation_lookback_4h_ms: int = 4 * 60 * 60 * 1000
    liquidation_lookback_24h_ms: int = 24 * 60 * 60 * 1000
```

**4. `config/default.yaml`**
```yaml
derivatives:
  enabled: true             # Phase 1.5 aktif
  liquidation_buffer_size: 5000
  liquidation_lookback_1h_ms: 3600000
  liquidation_lookback_4h_ms: 14400000
  liquidation_lookback_24h_ms: 86400000
```

**5. `BotContext` — liquidation_stream referansı**
```python
# src/bot/context.py
liquidation_stream: Optional["LiquidationStream"] = None
```

**6. `runner.py` — startup'ta başlat, shutdown'da durdur**
```python
async def startup(self):
    # ... mevcut ...
    if self.ctx.config.derivatives.enabled:
        self.ctx.liquidation_stream = LiquidationStream(
            watched_symbols=self.ctx.config.trading.symbols,
            buffer_size_per_symbol=self.ctx.config.derivatives.liquidation_buffer_size,
        )
        # journal Madde 3'te enjekte edilir
        await self.ctx.liquidation_stream.start()

async def shutdown(self):
    if self.ctx.liquidation_stream:
        await self.ctx.liquidation_stream.stop()
    # ...
```

### Test

`tests/test_liquidation_stream.py`:

- `_handle` fonksiyonunu fake raw mesaj ile çağır → doğru `LiquidationEvent` üretiyor mu?
- `binance_to_okx_symbol('BTCUSDT')` → `'BTC-USDT-SWAP'`; `binance_to_okx_symbol('BTCBUSD')` → `None`
- SELL side → `LONG_LIQ`; BUY side → `SHORT_LIQ`
- Watchlist dışı sembol (ör. `DOGEUSDT`, watched={BTC,ETH}) → buffer'a yazılmıyor
- `recent(symbol, lookback_ms)` — eski event'ler filtreleniyor mu?
- `stats()` — toplam notional doğru hesaplanıyor mu?
- Malformed JSON → crash yok, sadece warn log (monkey-patch logger ile assert)
- Reconnect testi: fake WS server iki kere connection'ı düşür → 3. denemede event'ler buffer'a geliyor (bu testi opsiyonel tut, integration test)

---

## MADDE 2 — Coinalyze REST Client (`derivatives_api.py`)

### Hedef

Coinalyze'in ücretsiz API'sinden her izlenen sembol için:
- Open Interest history (1m/5m/15m/1h)
- Funding Rate (current + predicted)
- Long/Short Ratio
- Aggregated liquidations (Binance filtresini kapatmak için)

verisini düzenli olarak çek, cache'le.

### Önemli Coinalyze davranışı (API doc'a göre kesin bilgi)

- **Base URL:** `https://api.coinalyze.net/v1`
- **Auth:** header `api_key: <KEY>` veya query param `?api_key=<KEY>`
- **Rate limit:** 40 istek/dakika per API key. 429'da `Retry-After` header var (saniye cinsinden)
- **Call cost:** Her endpoint `symbols` parametresini comma-separated alır, **maksimum 20 sembol**. ÖNEMLİ: "each symbol consume one API call" — yani `symbols=A,B,C` tek istek gibi görünse de 3 call sayılır. Dakikada maksimum 40 sembol-çağrısı bütçen var.
- **Sembol formatı:** Coinalyze kendi formatını kullanır: `BTCUSDT_PERP.A` (A=Binance), `.6`=Bybit, `.3`=OKX, `.0`=inverse. Bu mapping `/future-markets` endpoint'inden alınır (startup'ta bir kez).
- **Intraday history:** 1min-12hour interval'larda sadece 1500-2000 datapoint tutulur, eski data silinir. `daily` interval'da tüm geçmiş var.
- **Historical data ordering:** Hepsi ascending order (eski → yeni) — en son değer için `history[-1]` kullan.

### Kullanılacak endpoint'ler (kesin şema)

**A) Snapshot endpoint'leri** (flat response, `value` alanı):

```
GET /open-interest?symbols=<csv>&convert_to_usd=true
GET /funding-rate?symbols=<csv>
GET /predicted-funding-rate?symbols=<csv>
```

Response örneği:
```json
[
  {"symbol": "BTCUSDT_PERP.A", "value": 48200000000, "update": 1713350400}
]
```

**B) History endpoint'leri** (nested, OHLC tarzı):

```
GET /open-interest-history?symbols=<csv>&interval=<tf>&from=<ts>&to=<ts>&convert_to_usd=true
GET /funding-rate-history?symbols=<csv>&interval=<tf>&from=<ts>&to=<ts>
GET /predicted-funding-rate-history?symbols=<csv>&interval=<tf>&from=<ts>&to=<ts>
```

Response örneği (OHLC şeması — `c` = close/kapanış değeri):
```json
[{
  "symbol": "BTCUSDT_PERP.A",
  "history": [
    {"t": 1713346800, "o": 48000000000, "h": 48300000000, "l": 47900000000, "c": 48200000000}
  ]
}]
```

**C) Liquidation history** (özel şema — `l` ve `s`):

```
GET /liquidation-history?symbols=<csv>&interval=<tf>&from=<ts>&to=<ts>&convert_to_usd=true
```

Response:
```json
[{
  "symbol": "BTCUSDT_PERP.A",
  "history": [
    {"t": 1713346800, "l": 1250000, "s": 3400000}
  ]
}]
```
- `l` = long likidasyon toplamı (zorla kapanmış long'lar, USD cinsinden)
- `s` = short likidasyon toplamı

**D) Long/Short ratio history**:

```
GET /long-short-ratio-history?symbols=<csv>&interval=<tf>&from=<ts>&to=<ts>
```

Response:
```json
[{
  "symbol": "BTCUSDT_PERP.A",
  "history": [
    {"t": 1713346800, "r": 1.45, "l": 0.59, "s": 0.41}
  ]
}]
```
- `r` = long/short ratio (>1 = daha çok long)
- `l` = long share (0-1 arası)
- `s` = short share (l + s = 1)

**E) /future-markets** (mapping için):

```
GET /future-markets
```

Response (her market için tüm alanlar):
```json
[{
  "symbol": "BTCUSDT_PERP.A",
  "exchange": "A",
  "symbol_on_exchange": "BTCUSDT",
  "base_asset": "BTC",
  "quote_asset": "USDT",
  "is_perpetual": true,
  "margined": "STABLE",
  "expire_at": 0,
  "oi_lq_vol_denominated_in": "BASE_ASSET",
  "has_long_short_ratio_data": true,
  "has_ohlcv_data": true,
  "has_buy_sell_data": true
}]
```

### Rate budget hesabı (3 parite ile)

3 sembol × 4 endpoint (OI current, funding current, LS history, liquidation history) = **12 sembol-çağrısı/refresh**. 60s refresh ile dakikada 12 çağrı → çok güvenli (limit 40/min, ~%30 bandwidth).

Hatta bu budget ile **daha zengin veri** çekilebilir:
- Refresh interval'ı 45s'ye çekilebilir (dakikada 16 call, hâlâ güvende)
- Predicted funding rate ek call ile katılır (3 × 1 = 3 call/refresh)
- OI change 1h + 24h ayrı ayrı her refresh çekilebilir (3 × 2 = 6 extra)

Toplam **24 call/refresh @ 60s** veya **18 call @ 45s** — her iki senaryo da 40/min altında.

Funding ve LS history **z-score buffer'ımız için** sadece startup'ta bir kez çekilir (3 × 2 = 6 call, one-time), sonra current snapshot'lar buffer'a append edilir. Bu kritik — yoksa her refresh 30 günlük history çekip rate limit'i delerdi.

### Değişiklikler

**1. Yeni dosya `src/data/derivatives_api.py`**

```python
"""Coinalyze REST API client.
Funding rate, open interest, long/short ratio, aggregated liquidations.
Rate limit: 40 req/min. Adaptive backoff + in-memory history cache.

Her sembol her endpoint çağrısında 1 call sayılır — rate budget'ı dikkatli harca.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger

COINALYZE_BASE = "https://api.coinalyze.net/v1"

# Interval string'leri Coinalyze formatında
COINALYZE_INTERVAL = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1hour", "2h": "2hour", "4h": "4hour", "6h": "6hour",
    "12h": "12hour", "1d": "daily",
}


@dataclass
class DerivativesSnapshot:
    """Bir sembol için anlık türev verisi (cache'e yazılır)."""
    symbol: str                           # OKX format: 'BTC-USDT-SWAP'
    ts_ms: int
    funding_rate_current: float = 0.0     # mevcut funding (Coinalyze'in raw rate'i — borsa başına farklı normalize olabilir)
    funding_rate_predicted: float = 0.0   # bir sonraki periyod tahmini
    open_interest_usd: float = 0.0        # current OI USD (convert_to_usd=true)
    long_short_ratio: float = 1.0         # >1 = daha çok long
    long_share: float = 0.5               # 0-1
    short_share: float = 0.5              # 0-1
    aggregated_long_liq_1h_usd: float = 0.0
    aggregated_short_liq_1h_usd: float = 0.0


class CoinalyzeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.getenv("COINALYZE_API_KEY")
        if not self.api_key:
            logger.warning("COINALYZE_API_KEY missing; derivatives_api will return None snapshots")
        self._client = httpx.AsyncClient(
            base_url=COINALYZE_BASE,
            timeout=timeout_s,
            headers={"api_key": self.api_key or ""},
        )
        self._max_retries = max_retries
        # OKX sembol → Coinalyze sembol mapping (tek market seçiyoruz; basitlik için aggregated DEĞİL)
        # Strateji: base_asset match + USDT + is_perpetual + exchange öncelik sırası
        # Öncelik: Binance (A) > Bybit (6) > OKX (3) — en yüksek likidite
        self._symbol_map: dict[str, str] = {}
        self._symbol_map_loaded = False
        # Token bucket — 40/min
        self._rate_tokens = 40.0
        self._rate_capacity = 40.0
        self._rate_last_refill = time.monotonic()
        self._rate_lock = asyncio.Lock()

    async def _consume_token(self, cost: int = 1):
        """N-token consume — multi-symbol çağrı için cost=len(symbols)."""
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._rate_last_refill
            refill = elapsed * (40.0 / 60.0)
            self._rate_tokens = min(self._rate_capacity, self._rate_tokens + refill)
            self._rate_last_refill = now
            if self._rate_tokens < cost:
                wait = (cost - self._rate_tokens) * (60.0 / 40.0)
                await asyncio.sleep(wait)
                self._rate_tokens = 0.0
            else:
                self._rate_tokens -= cost

    async def _request(self, path: str, params: dict, cost: int) -> Optional[list]:
        if not self.api_key:
            return None
        for attempt in range(self._max_retries):
            await self._consume_token(cost=cost)
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    logger.warning("coinalyze_429 path={} retry_after={}", path, retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code == 401:
                    logger.error("coinalyze_401 invalid_api_key")
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("coinalyze_request_failed path={} attempt={} err={!r}",
                               path, attempt + 1, e)
                await asyncio.sleep(1.5 ** attempt)
        return None

    async def ensure_symbol_map(self, watched: list[str]):
        """/future-markets'ten OKX → Coinalyze symbol mapping kur.
        Tercih sırası: Binance > Bybit > OKX (likidite sırası)."""
        if self._symbol_map_loaded:
            return
        data = await self._request("/future-markets", {}, cost=1)
        if not data:
            logger.warning("coinalyze_symbol_map_empty; derivatives will be None")
            self._symbol_map_loaded = True  # retry loop'tan kaçınmak için
            return

        # Exchange öncelik sırası (suffix kodları)
        EXCHANGE_PRIORITY = ["A", "6", "3", "F", "H"]  # Binance, Bybit, OKX, Deribit, HTX

        for okx_sym in watched:
            base = okx_sym.split("-")[0]   # BTC, ETH, SOL, ...
            candidates = [
                m for m in data
                if m.get("base_asset") == base
                and m.get("quote_asset") == "USDT"
                and m.get("is_perpetual") is True
                and m.get("margined") == "STABLE"
            ]
            if not candidates:
                logger.warning("coinalyze_no_market_for_symbol okx_sym={} base={}", okx_sym, base)
                continue

            # Öncelikli exchange'i seç
            chosen = None
            for prio in EXCHANGE_PRIORITY:
                for c in candidates:
                    if c.get("symbol", "").endswith(f".{prio}"):
                        chosen = c
                        break
                if chosen:
                    break
            if not chosen:
                chosen = candidates[0]  # fallback: ilk match

            self._symbol_map[okx_sym] = chosen["symbol"]
            logger.info("coinalyze_mapping okx={} coinalyze={}", okx_sym, chosen["symbol"])

        self._symbol_map_loaded = True

    # --- Current snapshot (flat response: {symbol, value, update}) ---

    async def _fetch_current(self, path: str, coinalyze_symbol: str) -> Optional[float]:
        """Flat endpoint'ler için ortak helper: OI, funding, predicted funding."""
        data = await self._request(path, {"symbols": coinalyze_symbol}, cost=1)
        if not data or not isinstance(data, list) or not data:
            return None
        try:
            return float(data[0].get("value", 0.0))
        except (KeyError, ValueError, TypeError):
            return None

    async def fetch_current_oi_usd(self, coinalyze_symbol: str) -> Optional[float]:
        data = await self._request(
            "/open-interest",
            {"symbols": coinalyze_symbol, "convert_to_usd": "true"},
            cost=1,
        )
        if not data:
            return None
        try:
            return float(data[0].get("value", 0.0))
        except Exception:
            return None

    async def fetch_current_funding(self, coinalyze_symbol: str) -> Optional[float]:
        return await self._fetch_current("/funding-rate", coinalyze_symbol)

    async def fetch_predicted_funding(self, coinalyze_symbol: str) -> Optional[float]:
        return await self._fetch_current("/predicted-funding-rate", coinalyze_symbol)

    # --- History endpoints (nested response: {symbol, history: [...]}) ---

    async def fetch_liquidation_history(
        self,
        coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 1,
    ) -> Optional[dict]:
        """Schema: {t, l, s} — l=long liq USD, s=short liq USD (convert_to_usd=true ile).

        Returns: {"long_usd": float, "short_usd": float} (lookback içindeki toplam)
        """
        now = int(time.time())
        data = await self._request(
            "/liquidation-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
                "convert_to_usd": "true",
            },
            cost=1,
        )
        if not data or not data[0].get("history"):
            return None
        history = data[0]["history"]
        return {
            "long_usd": sum(float(h.get("l", 0)) for h in history),
            "short_usd": sum(float(h.get("s", 0)) for h in history),
            "bucket_count": len(history),
        }

    async def fetch_long_short_ratio(
        self,
        coinalyze_symbol: str,
        interval: str = "1hour",
    ) -> Optional[dict]:
        """Schema: {t, r, l, s} — r=ratio, l=long_share, s=short_share.

        Returns: son 1 bar'ın {"ratio", "long_share", "short_share"}
        """
        now = int(time.time())
        data = await self._request(
            "/long-short-ratio-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - 2 * 3600,  # son 2 saat, son bar'ı yakala
                "to": now,
            },
            cost=1,
        )
        if not data or not data[0].get("history"):
            return None
        latest = data[0]["history"][-1]
        return {
            "ratio": float(latest.get("r", 1.0)),
            "long_share": float(latest.get("l", 0.5)),
            "short_share": float(latest.get("s", 0.5)),
        }

    async def fetch_funding_history_series(
        self,
        coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 720,   # 30 gün
    ) -> Optional[list[float]]:
        """OHLC şemasından sadece 'c' (close) serisini döndür — z-score için.

        Startup'ta bir kere çekilir, sonra buffer'a current değer append edilir.
        """
        now = int(time.time())
        data = await self._request(
            "/funding-rate-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
            },
            cost=1,
        )
        if not data or not data[0].get("history"):
            return None
        return [float(h.get("c", 0.0)) for h in data[0]["history"]]

    async def fetch_ls_ratio_history_series(
        self,
        coinalyze_symbol: str,
        interval: str = "1hour",
        lookback_hours: int = 336,   # 14 gün
    ) -> Optional[list[float]]:
        """LS ratio geçmişi — z-score için."""
        now = int(time.time())
        data = await self._request(
            "/long-short-ratio-history",
            {
                "symbols": coinalyze_symbol,
                "interval": interval,
                "from": now - lookback_hours * 3600,
                "to": now,
            },
            cost=1,
        )
        if not data or not data[0].get("history"):
            return None
        return [float(h.get("r", 1.0)) for h in data[0]["history"]]

    async def fetch_oi_change_pct(
        self,
        coinalyze_symbol: str,
        lookback_hours: int = 24,
    ) -> Optional[float]:
        """OI'ın lookback süre öncesine göre yüzde değişimi.

        İki datapoint istiyor (başlangıç + son), cost=1.
        """
        now = int(time.time())
        data = await self._request(
            "/open-interest-history",
            {
                "symbols": coinalyze_symbol,
                "interval": "1hour",
                "from": now - (lookback_hours + 1) * 3600,
                "to": now,
                "convert_to_usd": "true",
            },
            cost=1,
        )
        if not data or not data[0].get("history") or len(data[0]["history"]) < 2:
            return None
        history = data[0]["history"]
        start = float(history[0].get("c", 0))
        end = float(history[-1].get("c", 0))
        if start <= 0:
            return None
        return (end - start) / start * 100.0

    # --- Aggregate snapshot — one call per endpoint, per symbol ---

    async def fetch_snapshot(self, okx_symbol: str) -> Optional[DerivativesSnapshot]:
        """Tek sembol için mevcut türev snapshot'ı çek.
        MALİYET: ~5 API call (OI + funding + predicted + liq-history + LS).

        OI change ve z-score için history calls ayrıca ve seyrek yapılır (bkz. DerivativesCache).
        """
        cn_sym = self._symbol_map.get(okx_symbol)
        if not cn_sym:
            return None

        # Sıralı çek — her biri rate token harcar; paralel yapmak token bucket'ı patlatır
        oi = await self.fetch_current_oi_usd(cn_sym)
        funding = await self.fetch_current_funding(cn_sym)
        predicted = await self.fetch_predicted_funding(cn_sym)
        liq = await self.fetch_liquidation_history(cn_sym, interval="1hour", lookback_hours=1)
        ls = await self.fetch_long_short_ratio(cn_sym, interval="1hour")

        return DerivativesSnapshot(
            symbol=okx_symbol,
            ts_ms=int(time.time() * 1000),
            funding_rate_current=funding or 0.0,
            funding_rate_predicted=predicted or 0.0,
            open_interest_usd=oi or 0.0,
            long_short_ratio=(ls or {}).get("ratio", 1.0),
            long_share=(ls or {}).get("long_share", 0.5),
            short_share=(ls or {}).get("short_share", 0.5),
            aggregated_long_liq_1h_usd=(liq or {}).get("long_usd", 0.0),
            aggregated_short_liq_1h_usd=(liq or {}).get("short_usd", 0.0),
        )

    async def close(self):
        await self._client.aclose()
```

**2. Rate budget güncellemesi (3 parite ile)**

3 sembol için `fetch_snapshot` başına ~5 call × 3 sembol = **15 call/refresh**. 60s refresh'te 15 call/min — limit (40/min) altında çok güvende (%37 kullanım). Ekstras:

- OI 24h change için ayrı history call (sembol başına 1) = 3 extra call
- Startup cost: 3 sembol × 2 history call (funding 30d + LS 14d) = 6 call, **bir kez**
- Refresh cost: 3 × 5 + 3 × 1 = **18 call/refresh** @ 60s = 18/min

Bu rahat bütçe ile ileride şunlar eklenebilir:
- Refresh'i 45s'ye çek (24 call/min, hâlâ güvende)
- 4h OI change de her refresh takip edilsin (3 extra call)
- Predicted funding her refresh güncellensin (3 extra call)

**2. Config**
```yaml
derivatives:
  coinalyze_enabled: true
  coinalyze_refresh_interval_s: 60       # her 60s'de bir her sembol için snapshot
  coinalyze_timeout_s: 10.0
  coinalyze_max_retries: 3
```

**3. `.env.example` güncelle** (dokümanın başında belirtildi)

### Önemli notlar

- **Real payload şemasını doğrula:** `fetch_snapshot` içindeki `_first_value` çağrılarında kullandığım key isimleri (`value`, `ratio`, `long_liquidations_usd`) Coinalyze doc'undan **runtime'da doğrulanmalı**. İlk implementasyonda bir debug script (`scripts/probe_coinalyze.py`) yaz — gerçek response'u logla, sonra key'leri sabitle.
- Rate limit çok önemli: 5 sembol × 4 endpoint = 20 istek per refresh. 60s refresh ile dakikada 20 istek — güvenli.
- Eğer `COINALYZE_API_KEY` env'de yoksa client warning atıp sessizce None snapshot döndürür — bot yine çalışır.

### Test

`tests/test_derivatives_api.py`:
- Rate limit token bucket testi — 45 ardışık `_consume_token(1)` çağrısı, `asyncio.sleep` mock'lanmış → toplam uyku süresi >= (5 × 1.5) = 7.5s
- `cost=3` ile consume → 3 token birden düşüyor
- 429 response + `Retry-After: 10` header → 10 saniye uyku çağrıldı mı?
- 401 response → warning log + None dönüş (retry yok)
- `api_key` None → tüm fetch metodları None döndürüyor, exception atmıyor
- `ensure_symbol_map` boş data → `_symbol_map` boş, `_symbol_map_loaded=True`, ikinci çağrı request atmıyor
- Exchange priority: fake `/future-markets` içinde BTC için hem Binance (.A) hem OKX (.3) varsa → `.A` seçiliyor
- Fake Coinalyze response `[{"symbol":"BTCUSDT_PERP.A","value":12345,"update":...}]` → `fetch_current_funding` 12345 döndürüyor
- Fake liquidation-history `{"history":[{"t":.., "l":100, "s":50}, {"t":.., "l":200, "s":150}]}` → `long_usd=300, short_usd=200`
- Fake long/short response `{"history":[{"t":.., "r":1.5, "l":0.6, "s":0.4}]}` → en son bar değerleri doğru çözülüyor
- Empty history (`{"history":[]}`) → None dönüş, KeyError yok
- `fetch_oi_change_pct` 2 datapoint'li (start=1000, end=1200) → 20.0 dönüyor; start=0 → None (division guard)

---

## MADDE 3 — Derivatives Journal + Cache (`derivatives_cache.py`)

### Hedef

Her sembol için **rolling snapshot** tut:
- Son N likidasyon event'i (Madde 1'den)
- Son Coinalyze snapshot'ı (Madde 2'den)
- Türev feature'ları (z-score, imbalance, OI momentum)

Ayrıca SQLite'a persist et — Phase 7 RL eğitimi için historical dataset.

### Değişiklikler

**1. Yeni SQLite tabloları**

`src/journal/database.py` içinde yeni migration:
```sql
CREATE TABLE IF NOT EXISTS liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,           -- LONG_LIQ | SHORT_LIQ
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional_usd REAL NOT NULL,
    ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_symbol_ts ON liquidations(symbol, ts_ms DESC);

CREATE TABLE IF NOT EXISTS derivatives_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    funding_rate_current REAL,
    funding_rate_predicted REAL,
    open_interest_usd REAL,
    oi_change_1h_pct REAL,
    oi_change_24h_pct REAL,
    long_short_ratio REAL,
    aggregated_long_liq_1h_usd REAL,
    aggregated_short_liq_1h_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_deriv_symbol_ts ON derivatives_snapshots(symbol, ts_ms DESC);
```

**2. Yeni dosya `src/journal/derivatives_journal.py`**

```python
"""Derivatives persist layer — liquidations + snapshots."""

import aiosqlite
from loguru import logger

class DerivativesJournal:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def insert_liquidation(self, ev):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO liquidations (symbol, side, price, quantity, notional_usd, ts_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ev.symbol, ev.side, ev.price, ev.quantity, ev.notional_usd, ev.ts_ms),
                )
                await db.commit()
        except Exception as e:
            logger.warning("liq_insert_failed err={!r}", e)

    async def insert_snapshot(self, snap):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO derivatives_snapshots "
                    "(symbol, ts_ms, funding_rate_current, funding_rate_predicted, "
                    " open_interest_usd, oi_change_1h_pct, oi_change_24h_pct, "
                    " long_short_ratio, aggregated_long_liq_1h_usd, aggregated_short_liq_1h_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (snap.symbol, snap.ts_ms, snap.funding_rate_current,
                     snap.funding_rate_predicted, snap.open_interest_usd,
                     snap.oi_change_1h_pct, snap.oi_change_24h_pct,
                     snap.long_short_ratio, snap.aggregated_long_liq_1h_usd,
                     snap.aggregated_short_liq_1h_usd),
                )
                await db.commit()
        except Exception as e:
            logger.warning("snap_insert_failed err={!r}", e)

    async def fetch_funding_history(self, symbol: str, lookback_ms: int):
        """Z-score hesaplaması için tarihsel funding oku."""
        # ...

    async def fetch_oi_history(self, symbol: str, lookback_ms: int):
        # ...
```

**3. Yeni dosya `src/data/derivatives_cache.py`**

```python
"""Per-symbol in-memory rolling cache — runner tarafından okunur.
LiquidationStream event'leri + CoinalyzeClient snapshot'larını birleştirir.
"""

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

@dataclass
class DerivativesState:
    """Runner'ın read_market_state adımında consume edeceği özet."""
    symbol: str
    ts_ms: int = 0

    # Liquidation stats (son 1h / 4h / 24h)
    long_liq_notional_1h: float = 0.0
    short_liq_notional_1h: float = 0.0
    long_liq_notional_4h: float = 0.0
    short_liq_notional_4h: float = 0.0
    liq_imbalance_1h: float = 0.0   # (short - long) / (short + long); +1 = short-heavy

    # Coinalyze snapshot
    funding_rate_current: float = 0.0
    funding_rate_zscore_30d: float = 0.0
    open_interest_usd: float = 0.0
    oi_change_1h_pct: float = 0.0
    oi_change_24h_pct: float = 0.0
    long_short_ratio: float = 1.0
    ls_ratio_zscore_14d: float = 0.0

    # Regime label (Madde 5'ten)
    regime: str = "UNKNOWN"  # LONG_CROWDED | SHORT_CROWDED | BALANCED | CAPITULATION | UNKNOWN

    # Health flags
    liq_stream_healthy: bool = False
    coinalyze_snapshot_age_s: float = 9999.0

class DerivativesCache:
    def __init__(
        self,
        watched: list[str],
        liq_stream,              # LiquidationStream (Madde 1)
        coinalyze,               # CoinalyzeClient (Madde 2)
        journal,                 # DerivativesJournal (bu madde)
        refresh_interval_s: float = 60.0,
    ):
        self.watched = watched
        self.liq_stream = liq_stream
        self.coinalyze = coinalyze
        self.journal = journal
        self.refresh_interval_s = refresh_interval_s
        self._states: dict[str, DerivativesState] = {
            s: DerivativesState(symbol=s) for s in watched
        }
        self._funding_history: dict[str, list[float]] = {s: [] for s in watched}
        self._ls_history: dict[str, list[float]] = {s: [] for s in watched}
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        await self.coinalyze.ensure_symbol_map(self.watched)
        # Startup'ta her sembol için history buffer'ları bir kez doldur
        for symbol in self.watched:
            cn_sym = self.coinalyze._symbol_map.get(symbol)
            if not cn_sym:
                continue
            funding_hist = await self.coinalyze.fetch_funding_history_series(
                cn_sym, interval="1hour", lookback_hours=720
            )
            if funding_hist:
                self._funding_history[symbol] = funding_hist[-720:]
            ls_hist = await self.coinalyze.fetch_ls_ratio_history_series(
                cn_sym, interval="1hour", lookback_hours=336
            )
            if ls_hist:
                self._ls_history[symbol] = ls_hist[-336:]
            logger.info("deriv_history_loaded symbol={} funding_pts={} ls_pts={}",
                        symbol, len(self._funding_history[symbol]), len(self._ls_history[symbol]))
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task

    async def _refresh_loop(self):
        while not self._stop.is_set():
            for symbol in self.watched:
                try:
                    await self._refresh_one(symbol)
                except Exception as e:
                    logger.warning("deriv_refresh_failed symbol={} err={!r}", symbol, e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _refresh_one(self, symbol: str):
        state = self._states[symbol]
        now_ms = int(time.time() * 1000)

        # 1) Liquidation stats (Binance WS'den, lokal buffer — API call YOK)
        stats_1h = self.liq_stream.stats(symbol, lookback_ms=60 * 60 * 1000)
        stats_4h = self.liq_stream.stats(symbol, lookback_ms=4 * 60 * 60 * 1000)
        state.long_liq_notional_1h = stats_1h["long_liq_notional"]
        state.short_liq_notional_1h = stats_1h["short_liq_notional"]
        state.long_liq_notional_4h = stats_4h["long_liq_notional"]
        state.short_liq_notional_4h = stats_4h["short_liq_notional"]
        total = state.long_liq_notional_1h + state.short_liq_notional_1h
        state.liq_imbalance_1h = (
            (state.short_liq_notional_1h - state.long_liq_notional_1h) / total
            if total > 0 else 0.0
        )
        state.liq_stream_healthy = self.liq_stream is not None

        # 2) Coinalyze snapshot (5 API call)
        snap = await self.coinalyze.fetch_snapshot(symbol)
        if snap:
            await self.journal.insert_snapshot(snap)
            state.funding_rate_current = snap.funding_rate_current
            state.open_interest_usd = snap.open_interest_usd
            state.long_short_ratio = snap.long_short_ratio
            state.coinalyze_snapshot_age_s = 0.0

            # Binance WS filtresini telafi — Coinalyze aggregated liq verisi
            # WS'den gelen BTC-only filtered veriyle birleştir:
            # Coinalyze aggregated VAR VE > WS sum → Coinalyze'i tercih et
            coinalyze_long = snap.aggregated_long_liq_1h_usd
            coinalyze_short = snap.aggregated_short_liq_1h_usd
            if coinalyze_long > state.long_liq_notional_1h:
                state.long_liq_notional_1h = coinalyze_long
            if coinalyze_short > state.short_liq_notional_1h:
                state.short_liq_notional_1h = coinalyze_short

            # Z-score update — funding
            self._funding_history[symbol].append(snap.funding_rate_current)
            self._funding_history[symbol] = self._funding_history[symbol][-720:]
            state.funding_rate_zscore_30d = self._zscore(
                snap.funding_rate_current, self._funding_history[symbol]
            )
            # Z-score update — LS ratio
            self._ls_history[symbol].append(snap.long_short_ratio)
            self._ls_history[symbol] = self._ls_history[symbol][-336:]
            state.ls_ratio_zscore_14d = self._zscore(
                snap.long_short_ratio, self._ls_history[symbol]
            )
        else:
            # Coinalyze çekemedik — eski veriyle devam, yaşlandığını işaretle
            state.coinalyze_snapshot_age_s += self.refresh_interval_s

        # 3) OI change — her refresh'te çekmek pahalı, 5 refresh'te bir (5 min) güncelle
        if not hasattr(self, "_oi_refresh_counter"):
            self._oi_refresh_counter = {s: 0 for s in self.watched}
        self._oi_refresh_counter[symbol] += 1
        if self._oi_refresh_counter[symbol] >= 5:  # her ~5 dakikada bir
            self._oi_refresh_counter[symbol] = 0
            cn_sym = self.coinalyze._symbol_map.get(symbol)
            if cn_sym:
                oi_24h = await self.coinalyze.fetch_oi_change_pct(cn_sym, lookback_hours=24)
                oi_1h = await self.coinalyze.fetch_oi_change_pct(cn_sym, lookback_hours=1)
                if oi_24h is not None:
                    state.oi_change_24h_pct = oi_24h
                if oi_1h is not None:
                    state.oi_change_1h_pct = oi_1h

        state.ts_ms = now_ms
        # Regime Madde 5'te hesaplanır

    @staticmethod
    def _zscore(value: float, history: list[float]) -> float:
        if len(history) < 10:
            return 0.0
        mean = statistics.mean(history)
        stdev = statistics.stdev(history)
        return (value - mean) / stdev if stdev > 1e-9 else 0.0

    def get(self, symbol: str) -> DerivativesState:
        return self._states.get(symbol, DerivativesState(symbol=symbol))
```

**4. `BotContext` — cache referansı**
```python
derivatives_cache: Optional["DerivativesCache"] = None
```

**5. Runner startup**
```python
if cfg.derivatives.enabled:
    journal = DerivativesJournal(db_path=cfg.journal.db_path)
    await journal.ensure_schema()  # migration

    liq_stream = LiquidationStream(watched_symbols=cfg.trading.symbols)
    liq_stream.attach_journal(journal)
    await liq_stream.start()

    coinalyze = CoinalyzeClient()

    cache = DerivativesCache(
        watched=cfg.trading.symbols,
        liq_stream=liq_stream,
        coinalyze=coinalyze,
        journal=journal,
        refresh_interval_s=cfg.derivatives.coinalyze_refresh_interval_s,
    )
    await cache.start()

    self.ctx.liquidation_stream = liq_stream
    self.ctx.derivatives_cache = cache
    self.ctx.coinalyze_client = coinalyze
```

### Test

`tests/test_derivatives_cache.py`:
- `_zscore` — normal dağılım 100 sample, son değer +2 sigma → ~2.0 döndürüyor mu?
- `_zscore` sample < 10 → 0.0 döndürüyor
- `_zscore` stdev=0 → 0.0 döndürüyor (division guard)
- Mock liq_stream + mock coinalyze → `_refresh_one` sonrası state doğru dolmuş
- Coinalyze None döndürürse state'in diğer alanları bozulmuyor (sadece coinalyze_snapshot_age_s artıyor)
- 30 dakika simülasyon → funding_history uzunluğu 720'yi geçmiyor

---

## MADDE 4 — Tahmini Likidite Haritası (`liquidity_heatmap.py`)

### Hedef

Coinglass'ın "heatmap"ini yeniden üret: **gerçekleşmiş + tahmini likidasyon kümeleri**.

- **Gerçekleşmiş:** son 24-48h likidasyonları fiyat seviyelerine bucket'la. Bu zaten likiditenin nerede silindiğini söyler.
- **Tahmini:** mevcut OI + varsayılan leverage dağılımı (%10x/25x/50x/100x) → her fiyat seviyesi için likidasyon notional tahmini.

Output bir liste: `[(price, notional_usd, side, kind), ...]` — `kind ∈ {historical, estimated}`

### Leverage dağılımı varsayımı

Başlangıç için şu basit dağılımla çalışacağız (RL sonra tune eder):
```python
LEVERAGE_BUCKETS = [
    (10, 0.30),   # 30% of OI is at 10x leverage
    (25, 0.35),   # 35% at 25x
    (50, 0.20),   # 20% at 50x
    (100, 0.15),  # 15% at 100x
]
```

Bu tamamen varsayım. Farklı borsalar farklı dağılım gösterir. Ama Coinalyze'in aggregated OI'ını girdi olarak kullandığımız için cross-exchange ortalama iyi bir yaklaşımdır.

### Hesaplama

```python
def estimate_liquidation_levels(
    current_price: float,
    long_short_ratio: float,      # >1 = daha çok long
    total_oi_usd: float,
    leverage_buckets=LEVERAGE_BUCKETS,
) -> list[EstimatedLiqLevel]:
    """OI'ı long/short'a böl, her leverage için liq fiyatını hesapla.

    Long liquidation price (approx):
        liq_price = entry_price * (1 - 1/leverage + fee_buffer)
    Short liquidation price (approx):
        liq_price = entry_price * (1 + 1/leverage - fee_buffer)
    """
    total_ratio = long_short_ratio + 1
    long_oi = total_oi_usd * (long_short_ratio / total_ratio)
    short_oi = total_oi_usd * (1 / total_ratio)

    levels = []
    for lev, share in leverage_buckets:
        long_notional = long_oi * share
        short_notional = short_oi * share
        # Approx liq price — gerçekte maintenance margin, funding, fee etkili
        # ama Coinglass de bu basitleştirmeyi yapıyor
        long_liq_price = current_price * (1 - 1 / lev + 0.005)   # +0.5% fee buffer
        short_liq_price = current_price * (1 + 1 / lev - 0.005)
        levels.append(EstimatedLiqLevel(
            price=long_liq_price,
            notional_usd=long_notional,
            side="LONG_LIQ",
            leverage=lev,
        ))
        levels.append(EstimatedLiqLevel(
            price=short_liq_price,
            notional_usd=short_notional,
            side="SHORT_LIQ",
            leverage=lev,
        ))
    return levels
```

**Önemli:** Bu **entry fiyatı = current_price** varsayımını yapıyor. Gerçekte pozisyonlar farklı fiyatlardan açıldı — dolayısıyla bu "mevcut fiyattan hareket eden trader'ların liq seviyeleri" demek. Yaklaşımın kısıtı budur, açıkça logla.

Daha iyi bir model için: OI değişim geçmişinden ağırlıklı ortalama entry çıkarılabilir. Bu **Phase 7 sonrası** iyileştirme olarak bırakılsın.

### Kümeleme

Estimated + historical level'ları birleştirdikten sonra yakın fiyatları **bucket**'la:
```python
def cluster_levels(
    levels: list[EstimatedLiqLevel],
    bucket_pct: float = 0.002,   # 0.2% fiyat aralığı
) -> list[Cluster]:
    """Yakın fiyatları birleştir, toplam notional'larını topla."""
```

### Output

`LiquidityHeatmap` dataclass:
```python
@dataclass
class LiquidityHeatmap:
    symbol: str
    current_price: float
    clusters_above: list[Cluster]  # price > current, sorted by price asc
    clusters_below: list[Cluster]  # price < current, sorted by price desc
    nearest_above: Optional[Cluster]
    nearest_below: Optional[Cluster]
    largest_above_notional: float
    largest_below_notional: float
```

### Integration

Runner'ın `read_market_state` adımında:
```python
deriv_state = self.ctx.derivatives_cache.get(symbol)
heatmap = build_heatmap(
    symbol=symbol,
    current_price=state.price,
    deriv_state=deriv_state,
    liq_stream=self.ctx.liquidation_stream,
    bucket_pct=cfg.derivatives.heatmap_bucket_pct,
    historical_lookback_ms=cfg.derivatives.heatmap_historical_lookback_ms,
)
market_state.liquidity_heatmap = heatmap
```

### Config

```yaml
derivatives:
  heatmap_enabled: true
  heatmap_bucket_pct: 0.002            # %0.2 bucket genişliği
  heatmap_historical_lookback_ms: 172800000   # 48h
  heatmap_max_clusters_each_side: 10
  leverage_buckets:
    - [10, 0.30]
    - [25, 0.35]
    - [50, 0.20]
    - [100, 0.15]
```

### Test

`tests/test_liquidity_heatmap.py`:
- `estimate_liquidation_levels` — BTC 100k, LS=1.5, OI=1B → 10x long liq yaklaşık 90.5k civarı mı?
- `cluster_levels` — 5 yakın level (0.1% aralıkta) tek cluster'a birleşiyor mu?
- `build_heatmap` — current_price'ın üstü/altı doğru ayrılıyor mu?
- Historical liq'lerin mevcut fiyata uzaklığına göre sıralama
- LS=1.0 (dengeli) → long_notional ≈ short_notional
- OI=0 → tüm estimated clusters boş notional

---

## MADDE 5 — Derivatives Regime Tespiti (`derivatives_regime.py`)

### Hedef

Her cycle'da derivatives_state'e bakarak 4 rejimden birini etiketle:

1. **LONG_CROWDED** — funding z-score > +2, LS ratio z-score > +1.5, OI son 24h'ta belirgin arttı
2. **SHORT_CROWDED** — funding z-score < -2, LS ratio z-score < -1.5, OI belirgin arttı
3. **CAPITULATION** — son 4h'ta büyük likidasyon (toplam notional > $threshold), OI sert düşüş
4. **BALANCED** — yukarıdakilerin hiçbiri

### Değişiklikler

**1. Yeni dosya `src/analysis/derivatives_regime.py`**

```python
from enum import Enum

class Regime(str, Enum):
    LONG_CROWDED = "LONG_CROWDED"
    SHORT_CROWDED = "SHORT_CROWDED"
    CAPITULATION = "CAPITULATION"
    BALANCED = "BALANCED"
    UNKNOWN = "UNKNOWN"

@dataclass
class RegimeAnalysis:
    regime: Regime
    confidence: float  # 0.0-1.0
    reasoning: list[str]  # human-readable reasons

def classify_regime(
    deriv_state: DerivativesState,
    *,
    funding_crowded_z: float = 2.0,
    ls_crowded_z: float = 1.5,
    oi_surge_pct: float = 10.0,
    oi_crash_pct: float = -8.0,
    capitulation_liq_notional: float = 50_000_000.0,   # $50M in 4h (BTC için; config'le override)
) -> RegimeAnalysis:
    reasons = []

    # Veri yeterli mi?
    if deriv_state.coinalyze_snapshot_age_s > 300:
        return RegimeAnalysis(Regime.UNKNOWN, 0.0, ["coinalyze_data_stale"])

    # CAPITULATION önce — en güçlü sinyal
    total_liq_4h = deriv_state.long_liq_notional_4h + deriv_state.short_liq_notional_4h
    if total_liq_4h > capitulation_liq_notional and deriv_state.oi_change_24h_pct < oi_crash_pct:
        reasons.append(f"massive_liquidation_4h=${total_liq_4h:,.0f}")
        reasons.append(f"oi_crash_24h={deriv_state.oi_change_24h_pct:.1f}%")
        return RegimeAnalysis(Regime.CAPITULATION, 0.9, reasons)

    # LONG_CROWDED
    if (deriv_state.funding_rate_zscore_30d > funding_crowded_z
        and deriv_state.ls_ratio_zscore_14d > ls_crowded_z
        and deriv_state.oi_change_24h_pct > oi_surge_pct):
        reasons.append(f"funding_z={deriv_state.funding_rate_zscore_30d:.2f}")
        reasons.append(f"ls_ratio_z={deriv_state.ls_ratio_zscore_14d:.2f}")
        reasons.append(f"oi_surge_24h={deriv_state.oi_change_24h_pct:.1f}%")
        return RegimeAnalysis(Regime.LONG_CROWDED, 0.8, reasons)

    # SHORT_CROWDED
    if (deriv_state.funding_rate_zscore_30d < -funding_crowded_z
        and deriv_state.ls_ratio_zscore_14d < -ls_crowded_z
        and deriv_state.oi_change_24h_pct > oi_surge_pct):
        reasons.append(f"funding_z={deriv_state.funding_rate_zscore_30d:.2f}")
        reasons.append(f"ls_ratio_z={deriv_state.ls_ratio_zscore_14d:.2f}")
        reasons.append(f"oi_surge_24h={deriv_state.oi_change_24h_pct:.1f}%")
        return RegimeAnalysis(Regime.SHORT_CROWDED, 0.8, reasons)

    return RegimeAnalysis(Regime.BALANCED, 0.5, ["no_extreme_readings"])
```

**2. Cache'e integration**

`DerivativesCache._refresh_one` sonunda:
```python
regime_analysis = classify_regime(state, **cfg.derivatives.regime_thresholds)
state.regime = regime_analysis.regime
logger.debug("regime symbol={} regime={} conf={:.2f} reasons={}",
             symbol, state.regime, regime_analysis.confidence, regime_analysis.reasoning)
```

**3. Config**

```yaml
derivatives:
  regime_thresholds:
    funding_crowded_z: 2.0
    ls_crowded_z: 1.5
    oi_surge_pct: 10.0
    oi_crash_pct: -8.0
    # Default BTC için. Altcoin'lerde 4h'te $50M likidasyon nadir —
    # per-symbol override ile altcoinler için düşürülmeli.
    capitulation_liq_notional: 50000000
  per_symbol_overrides:
    ETH-USDT-SWAP:
      capitulation_liq_notional: 20000000    # $20M
    SOL-USDT-SWAP:
      capitulation_liq_notional: 8000000     # $8M  (volatilitesi yüksek, eşik hassas)
```

**Implementation notu:** `classify_regime` çağrılırken `cfg.derivatives.per_symbol_overrides.get(symbol, {})` ile default threshold'ları override et. Eksik anahtar → default'a düş.

### Test

`tests/test_derivatives_regime.py`:
- Her 4 rejim için synthetic state ile test
- `coinalyze_snapshot_age_s=600` → UNKNOWN
- İki şart sağlanıp biri sağlanmazsa LONG_CROWDED değil BALANCED
- CAPITULATION önceliği — hem LONG_CROWDED hem CAPITULATION koşulları sağlansa bile CAPITULATION döner
- Boundary: funding_z = exactly 2.0 → crowded sayılmıyor (strict >)
- Config override ile threshold değişince sınıflandırma değişiyor

---

## MADDE 6 — Entry Signal'a Entegrasyon (confluence + kalite gate)

### Hedef

Derivatives state'i entry kararına **prensipli** şekilde entegre et. Yanlış yapmanın yolu: 10 yeni sinyali confluence'a toplamak (overfit garanti). Doğru yol: **tek bir boost/penalty slot'u** ve **contrarian kalite gate'i**.

### Değişiklikler

**1. `src/strategy/entry_signals.py` — confluence +1 slot**

Mevcut confluence 5 faktöre bakıyor (HTF trend, key level, recent sweep, MSS, LTF pattern). **6. slot ekle:**

```python
def calculate_confluence(market_state: MarketState, signal_direction: str) -> tuple[int, list[str]]:
    score, reasons = 0, []
    # ... mevcut 5 faktör ...

    # 6. Derivatives alignment (Phase 1.5)
    if market_state.derivatives:
        ds = market_state.derivatives
        # Contrarian boost — herkes tersindeysen sen haklı olabilirsin
        if signal_direction == "LONG" and ds.regime == "SHORT_CROWDED":
            score += 1
            reasons.append("deriv:contrarian_short_crowded")
        elif signal_direction == "SHORT" and ds.regime == "LONG_CROWDED":
            score += 1
            reasons.append("deriv:contrarian_long_crowded")
        # CAPITULATION + trend direction = reversal entry
        elif ds.regime == "CAPITULATION":
            score += 1
            reasons.append("deriv:capitulation_reversal")
        # Liquidity cluster yakınındayız
        elif _heatmap_supports_direction(market_state, signal_direction):
            score += 1
            reasons.append("deriv:heatmap_cluster_target")

    return score, reasons


def _heatmap_supports_direction(market_state, signal_direction: str) -> bool:
    """Hedef tarafta (signal_direction yönünde) büyük bir likidite kümesi var mı?
    Fiyat genelde büyük kümelere doğru 'mıknatıslanır'."""
    hm = market_state.liquidity_heatmap
    if not hm:
        return False
    atr = market_state.atr or 0.0
    if atr <= 0:
        return False
    target_cluster = hm.nearest_above if signal_direction == "LONG" else hm.nearest_below
    if not target_cluster:
        return False
    distance = abs(target_cluster.price - hm.current_price)
    # Küme ATR*3'ten yakın ve büyük bir küme mi?
    return (distance < atr * 3.0
            and target_cluster.notional_usd > hm.largest_above_notional * 0.7)
```

**2. Kalite gate — crowded yönde trade etmeyi kısıtla**

`entry_signals.py` içinde:
```python
def should_skip_for_derivatives(market_state, signal_direction: str, cfg) -> tuple[bool, str]:
    """Crowded trade'e girme — overriding filter.
    Confluence'dan BAĞIMSIZ — yüksek confluence bile olsa crowded yönde skip."""
    if not cfg.derivatives.crowded_skip_enabled or not market_state.derivatives:
        return False, ""
    ds = market_state.derivatives
    if signal_direction == "LONG" and ds.regime == "LONG_CROWDED":
        # Funding çok aşırı mı? Uç durumda skip
        if ds.funding_rate_zscore_30d > cfg.derivatives.crowded_skip_z_threshold:
            return True, "crowded_long_skip"
    elif signal_direction == "SHORT" and ds.regime == "SHORT_CROWDED":
        if ds.funding_rate_zscore_30d < -cfg.derivatives.crowded_skip_z_threshold:
            return True, "crowded_short_skip"
    return False, ""
```

Runner'da:
```python
skip, reason = should_skip_for_derivatives(market_state, direction, cfg)
if skip:
    logger.info("entry_skipped symbol={} direction={} reason={}", symbol, direction, reason)
    return
```

**3. R:R dinamik ayarlama (opsiyonel — bu maddede feature flag'li)**

Crowded ile aynı yönde girmek ZORUNDAYSAN (örneğin RL sonra karar verecek), en azından R:R'ı yükselt:

```python
def adjust_min_rr_for_regime(base_rr: float, regime: str, signal_direction: str, cfg) -> float:
    if not cfg.derivatives.regime_rr_adjust_enabled:
        return base_rr
    if (signal_direction == "LONG" and regime == "LONG_CROWDED") or \
       (signal_direction == "SHORT" and regime == "SHORT_CROWDED"):
        return base_rr * cfg.derivatives.crowded_same_side_rr_multiplier   # örn 1.33
    if (signal_direction == "LONG" and regime == "SHORT_CROWDED") or \
       (signal_direction == "SHORT" and regime == "LONG_CROWDED"):
        return base_rr * cfg.derivatives.contrarian_rr_multiplier          # örn 0.9 (daha agresif)
    return base_rr
```

**4. Config**

```yaml
derivatives:
  confluence_slot_enabled: true
  crowded_skip_enabled: true
  crowded_skip_z_threshold: 3.0         # sadece çok uç durumda skip
  regime_rr_adjust_enabled: false       # Phase 7'de RL açsın; şimdilik kapalı
  crowded_same_side_rr_multiplier: 1.33
  contrarian_rr_multiplier: 0.9
```

### Test

`tests/test_entry_signals_derivatives.py`:
- LONG signal + SHORT_CROWDED regime → confluence +1, reason `deriv:contrarian_short_crowded`
- LONG signal + LONG_CROWDED + funding_z=3.5 + crowded_skip_enabled → skip döner
- LONG signal + LONG_CROWDED + funding_z=1.5 → skip DÖNMEZ (threshold altında)
- BALANCED regime → confluence boost 0 (başka condition olmadığında)
- `market_state.derivatives=None` → fonksiyon güvenle 0 döndürüyor, exception yok
- Heatmap nearest_above ATR*5 uzakta → `_heatmap_supports_direction` False
- Heatmap cluster ATR*2 yakın ve `largest_above_notional`'ın %80'i → True

---

## MADDE 7 — Logging + Observability + Journal Enrichment

### Hedef

Her açılan pozisyon için derivatives snapshot'ı trade journal'a yaz. Böylece Phase 7 RL eğitiminde "bu trade'in açıldığı anda funding neydi, regime neydi" feature'larını çekebilelim.

### Değişiklikler

**1. `trades` tablosuna yeni kolonlar**

`src/journal/database.py` migration:
```sql
ALTER TABLE trades ADD COLUMN regime_at_entry TEXT;
ALTER TABLE trades ADD COLUMN funding_z_at_entry REAL;
ALTER TABLE trades ADD COLUMN ls_ratio_at_entry REAL;
ALTER TABLE trades ADD COLUMN oi_change_24h_at_entry REAL;
ALTER TABLE trades ADD COLUMN liq_imbalance_1h_at_entry REAL;
ALTER TABLE trades ADD COLUMN nearest_liq_cluster_above_price REAL;
ALTER TABLE trades ADD COLUMN nearest_liq_cluster_below_price REAL;
ALTER TABLE trades ADD COLUMN nearest_liq_cluster_above_notional REAL;
ALTER TABLE trades ADD COLUMN nearest_liq_cluster_below_notional REAL;
```

Her `ALTER TABLE`'ı try/except ile sar (Phase 7 prep Madde E'deki pattern).

**2. Trade kaydında derivatives snapshot'ı enjekte et**

`src/journal/models.py` → `TradeRecord` dataclass'ına alanlar ekle (defaults 0/None).

`src/execution/order_router.py` → plan submit sonrası journal'a yazarken `market_state.derivatives` + `market_state.liquidity_heatmap` değerlerini topla ve `TradeRecord`'a geçir.

**3. Structured log event'leri**

Her cycle'da her sembol için `derivatives_snapshot` log event'i:
```python
logger.info(
    "derivatives_snapshot symbol={} regime={} funding_z={:.2f} "
    "ls_z={:.2f} oi_24h={:.1f}% liq_imb_1h={:.2f}",
    symbol, ds.regime, ds.funding_rate_zscore_30d,
    ds.ls_ratio_zscore_14d, ds.oi_change_24h_pct, ds.liq_imbalance_1h,
)
```

Entry kararında:
```python
logger.info(
    "entry_decision symbol={} direction={} confluence={} reasons={} "
    "regime={} derivatives_boost={}",
    symbol, direction, confluence_score, reasons, ds.regime, deriv_contribution,
)
```

**4. Report script güncellemesi**

`scripts/report.py`:
- Her rejim için ayrı win rate / profit factor
- Heatmap cluster mıknatıslanma sayısı (fiyat cluster'a değdi mi?)

```
Regime breakdown (last 7d):
  BALANCED:      42 trades, WR 45%, PF 1.35
  SHORT_CROWDED:  8 trades, WR 62%, PF 2.10  (contrarian long'lar güçlü)
  LONG_CROWDED:   5 trades, WR 20%, PF 0.45  (skip threshold düşürmeli)
  CAPITULATION:   3 trades, WR 66%, PF 3.20  (küçük sample, dikkat)
```

### Test

`tests/test_journal_derivatives.py`:
- Migration idempotent — ikinci kez çağırıldığında hata yok
- TradeRecord derivatives alanları boşken `insert` çalışıyor
- Derivatives dolu snapshot ile insert + fetch → değerler geri geliyor
- `report.py regime_breakdown()` fonksiyonu fixture DB ile doğru WR/PF üretiyor

---

## Teslimat kontrolü

Tüm maddelerden sonra:

```bash
# Unit testler
.venv/Scripts/python.exe -m pytest tests/ -v

# Coinalyze gerçek entegrasyon probe'u (manual, opsiyonel)
.venv/Scripts/python.exe scripts/probe_coinalyze.py

# Smoke test — dry-run
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --dry-run --once

# Derivatives-only mod — sadece stream'i çalıştır, entry atma (veri toplama)
.venv/Scripts/python.exe -m src.bot --config config/default.yaml --derivatives-only --duration 1h
```

### Kabul kriterleri (checklist)

- [ ] `pytest tests/ -v` tamamı yeşil
- [ ] Binance WS ilk 60 saniyede en az 1 likidasyon event'i yakaladı (log'da `liquidation_stream_connected` + en az 1 event)
- [ ] `data/journal.db` içinde `liquidations` tablosunda >0 kayıt var (3 paritenin en az birinden)
- [ ] Coinalyze API'den 3 sembol için de snapshot geldi (`derivatives_snapshots` tablosu BTC/ETH/SOL her biri için >0 kayıt)
- [ ] 1 saatlik run sonrası her sembol için `funding_rate_zscore_30d` dolu (startup'ta history çekildiği için ilk refresh'ten itibaren)
- [ ] En az bir entry'de `regime_at_entry` kolonu dolu görülüyor
- [ ] Log'da `derivatives_snapshot` event'leri her cycle'da 3 sembol için de çıkıyor
- [ ] Rate limit testi — 1 saatte hiç 429 log'u yok (18 call/min @ 60s refresh)
- [ ] `COINALYZE_API_KEY` env'den çıkarılırsa bot yine başlıyor (sadece fallback values, hata yok)
- [ ] Binance WS bir süre bağlantısız kalsa bile reconnect oluyor (`liq_ws_disconnected` → `liquidation_stream_connected` sequence log'da var)
- [ ] Heatmap en az 5 cluster yukarı + 5 cluster aşağı içeriyor
- [ ] Entry decision log'unda `derivatives_boost` görülüyor (0 veya 1)
- [ ] SOL için `capitulation_liq_notional` override'ı çalışıyor (BTC default $50M, SOL $8M)
- [ ] `CLAUDE.md` güncel — Phase 1.5 section'ı, yeni config anahtarları, derivatives mimarisi diyagramı, **3 parite listesi**
- [ ] `.env.example` `COINALYZE_API_KEY` içeriyor

### Commit sırası (önerilen)

1. `feat: liquidation stream — Binance forceOrder WS (Madde 1)`
2. `feat: coinalyze REST client with rate limiting (Madde 2)`
3. `feat: derivatives cache + journal (Madde 3)`
4. `feat: estimated liquidity heatmap (Madde 4)`
5. `feat: derivatives regime classifier (Madde 5)`
6. `feat: entry signal derivatives integration (Madde 6)`
7. `feat: journal derivatives enrichment + reporting (Madde 7)`
8. `docs: update CLAUDE.md for Phase 1.5`

---

## Phase 7 için bu katmanın anlamı

Bu 7 madde tamamlandıktan sonra RL feature vector'ına eklenecek yeni feature'lar:

```python
# Phase 7 feature_extractor.py içinde:
derivatives_features = [
    ds.funding_rate_zscore_30d,           # crowded olup olmadığını ölçer
    ds.ls_ratio_zscore_14d,                # pozisyonlanma ekstremi
    ds.oi_change_24h_pct / 100.0,          # trend mi yoksa squeeze mi
    ds.liq_imbalance_1h,                   # son 1h hangi taraf temizlendi
    regime_one_hot(ds.regime),             # 4-dim one-hot
    heatmap_nearest_above_distance_atr,    # büyük cluster ne kadar yakın
    heatmap_nearest_below_distance_atr,
    heatmap_imbalance,                     # (above_notional - below_notional) / total
]
```

RL agent bu feature'ların gerçekten tahmin gücü olup olmadığını **kendisi öğrenecek**. Eğer yoksa reward katkısı düşer, ağırlık küçülür. En güzel yanı bu — manuel parameter tuning'i elimine ediyor.

## Notlar

- **Tahmini heatmap gerçek değildir** — Coinglass'ın "Model 3"ü bile tahmindir. Kullanımı confluence'ın BİR parçası olarak, tek başına sinyal olarak DEĞİL.
- **Binance filtresi** — 1000ms'de "en büyük" gelen likidasyon küçük likidasyonları görmeyebilir. Bu yüzden Coinalyze aggregated liquidations verisi önemli — birlikte kullan.
- **Rate limit disiplini** — Coinalyze 40/min deldiğinde API key ban'lenebilir. Token bucket + adaptive backoff zorunlu.
- **Leverage bucket'ları varsayımdır** — ileride Phase 7+ olarak, tarihsel likidasyon verisinden bucket'ları kalibre eden bir script yazılabilir. Şimdilik sabit varsayım ile başla.
- **Multi-exchange aggregation (ileriye):** Şu an sadece Binance WS + Coinalyze agregasyonu. İleride OKX kendi forceOrder stream'i + Bybit stream'i eklenebilir. Mimari bunu destekleyecek şekilde kuruldu (`LiquidationStream` generic, kaynak-bağımsız buffer tutuyor).
