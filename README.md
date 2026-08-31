# 📈 Binance Paper Trading Bot + Streamlit Panel

Gerçek parayla **çalışmayan**, canlı Binance fiyatlarıyla beslenen bir sanal
alım-satım (paper trading) botu ve onu tarayıcıdan takip edebileceğin bir
kontrol paneli.

* **Sanal bakiye:** 10.000 USDT (ayarlanabilir)
* **Takip:** BTC/USDT ve ETH/USDT, 15 dakikalık mumlar
* **Karar motoru:** [TradingAgents](https://github.com/TauricResearch/TradingAgents) çoklu yapay zekâ ajanı kurulu (Analist → Araştırmacı tartışması → Trader → Risk Kurulu)
* **Panel:** metrikler, açık pozisyonlar, işlem geçmişi, equity curve, Başlat/Durdur/Sıfırla düğmeleri
* **Gerçek hesaba geçiş:** `config.py` içinde tek satır — `DEMO_MODE = False`

---

## 📁 Dosya Yapısı

```
.
├── app.py                    # Streamlit web paneli (arayüz + kontrol düğmeleri)
├── bot.py                    # Ticaret motoru: veri çekme, indikatör, al/sat kararı
├── agents_engine.py          # TradingAgents kurulunu çalıştırır, kararı ve raporları yazar
├── alpaca_execution.py       # Kararları Alpaca Paper Trading'e emir olarak gönderir
├── trading_agents/           # TradingAgents reposu (git submodule, Apache-2.0)
├── backtest.py               # RSI/EMA taban çizgisini geçmiş veride sınama
├── database.py               # SQLite katmanı: bakiye, pozisyon, işlem, equity, log
├── config.py                 # Tüm ayarlar (ortam değişkeniyle de ezilebilir)
├── requirements.txt
├── Dockerfile                # Tek imaj: hem motor hem panel
├── docker-compose.yml        # bot + dashboard servisleri, kalıcı volume
├── DEPLOY.md                 # Hetzner VPS dağıtım rehberi (adım adım)
├── deploy/
│   ├── setup-caddy.sh             # panele şifre + HTTPS kurar (tek komut)
│   ├── setup-existing-caddy.sh    # sunucuda zaten Caddy varsa ona tanıtır
│   ├── docker-compose.caddy.yml   # şifre + HTTPS ile yayınlama (opsiyonel)
│   ├── docker-compose.public.yml  # 8501'i doğrudan açma (opsiyonel)
│   ├── docker-compose.external-proxy.yml  # mevcut vekil sunucunun ağına bağlanma
│   ├── existing-caddy-site.Caddyfile.example
│   ├── nginx-panel.conf.example   # sunucuda zaten nginx varsa
│   └── Caddyfile
├── .env.example              # Gerçek moda geçerken kullanılacak şablon
├── data/                     # SQLite veritabanı buraya yazılır (git'e girmez)
└── tests/
    ├── test_execution.py     # Pozisyon/PnL/koruma testleri (internet gerekmez)
    ├── test_agents.py        # Kurul entegrasyonu (LLM anahtarı gerekmez, sahte kurul)
    ├── test_alpaca.py        # Alpaca emir yürütme (API anahtarı gerekmez, sahte istemci)
    ├── test_backtest.py      # Backtest motoru testleri
    └── test_dashboard.py     # Paneli gerçekten çalıştıran render testi
```

---

## 🚀 Kurulum

Python 3.10+ gerekir.

```bash
# 1) Projeyi al
git clone https://github.com/nietzische-app/yatirimasistan-.git
cd yatirimasistan-

# 2) Sanal ortam
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3) Kütüphaneler
pip install -r requirements.txt
```

## ▶️ Çalıştırma

### Yol 1 — Sadece panel (en kolay)

```bash
streamlit run app.py
```

Tarayıcı `http://localhost:8501` adresinde açılır. Sol taraftaki
**"▶️ Başlat"** düğmesine bas — bot, panelin içinde arka plan thread'i olarak
çalışmaya başlar ve her 30 saniyede bir piyasayı tarar.

### Yol 2 — Bot ayrı terminalde (7/24 çalıştırmak için önerilir)

```bash
# 1. terminal: motor
python bot.py

# 2. terminal: panel
streamlit run app.py
```

İkisi aynı SQLite dosyasını (`data/trading.db`) paylaşır. Panelden Başlat/Durdur
düğmesi ayrı terminaldeki botu da kontrol eder.

### Yol 3 — Docker (sunucuda 7/24)

```bash
cp .env.example .env
docker compose up -d --build
ssh -L 8501:localhost:8501 root@SUNUCU_IP   # panele eriş: http://localhost:8501
```

Paneli kullanıcı adı/şifre ve HTTPS ile internete açmak için:

```bash
./deploy/setup-caddy.sh
```

Hetzner VPS'e sıfırdan kurulum, firewall ve yedekleme adımları için:
**[DEPLOY.md](DEPLOY.md)**

### Faydalı komutlar

```bash
python bot.py --status      # sistemin tam durumu (kurul, Alpaca, emirler, piyasa)
python bot.py --once        # tek tur tarayıp çık
python bot.py --force       # panel "durduruldu" olsa bile çalış
python bot.py --simulate    # internet olmadan sentetik fiyatlarla dene
python bot.py --reset       # sanal bakiyeyi 10.000 USDT'ye döndür
python tests/test_strategy.py && python tests/test_dashboard.py   # testler
```

---

## 🧠 Karar Motoru: TradingAgents Kurulu

Al/sat kararını artık indikatör kuralları değil, bir **yapay zekâ kurulu** verir.
[TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0)
`trading_agents/` altında git submodule olarak gelir.

```
Market · Social · News · Fundamentals analistleri
        ↓ raporlar
🐂 Boğa  ↔  🐻 Ayı araştırmacı tartışması  (N tur)
        ↓ Araştırma Müdürünün hükmü
💼 Trader önerisi (giriş, stop-loss, büyüklük)
        ↓
🛡️ Risk Kurulu: Agresif ↔ Muhafazakâr ↔ Nötr  (N tur)
        ↓ Risk Yöneticisinin onay/ret gerekçesi
Nihai not: Buy / Overweight / Hold / Underweight / Sell
```

Not → emir eşlemesi: **Buy** tam pozisyon, **Overweight** yarım, **Hold** bekle,
**Sell/Underweight** varsa pozisyonu kapat. Stop-loss'u ajanlar önerir; öneri
fiyatın %0.5–%15 altında değilse yok sayılıp `config.STOP_LOSS_PCT` kullanılır.

### İki hızlı/yavaş katman

| Katman | Sıklık | Ne yapar | Maliyet |
|---|---|---|---|
| **Uygulama** (`bot.py`) | 30 sn | fiyat takibi, açık pozisyonun TP/SL koruması, kurulun bekleyen kararlarını emre çevirme | sıfır |
| **Kurul** (`agents_engine.py`) | 60 dk / sembol | ajanlar toplanır, tartışır, karar verir | LLM çağrıları |

Kurul **arka planda** çalışır; toplantı dakikalarca sürse bile pozisyon koruması
kesintiye uğramaz.

### 💸 Maliyet uyarısı — bunu okumadan açma

Bir toplantı onlarca LLM çağrısıdır. 30 saniyede bir çalıştırmak günde binlerce
dolar demektir. Bu yüzden üç fren var:

| Fren | Varsayılan | Ne yapar |
|---|---|---|
| `AGENT_INTERVAL_MINUTES` | 60 | Sembol başına toplantı sıklığı. **Alt sınır 15 dk**, kod bunu zorlar. |
| `AGENT_MAX_RUNS_PER_DAY` | 60 | Günlük toplam toplantı tavanı; aşılınca kurul toplanmaz. |
| `AGENT_RUN_TIMEOUT_SECONDS` | 1200 | Takılan toplantı iptal edilir. |

Ucuz bir modelle başla (`deepseek/deepseek-chat`), maliyeti birkaç gün izle,
sonra `LLM_DEEP_MODEL`'i güçlendir.

### Kurul çalışmıyorsa ne olur

LLM anahtarı yoksa, kota dolduysa veya toplantı hata alırsa: **sistem çökmez.**
Yeni pozisyon açılmaz, açık pozisyonların TP/SL koruması çalışmaya devam eder,
hata panelde "Başarısız toplantılar" altında görünür.

### RSI/EMA nereye gitti

`bot.py`'deki al-sat kuralları **kaldırıldı**. İndikatörler yalnızca iki yerde
kaldı: panelde gösterilen piyasa görüntüsü ve `backtest.py`. Backtest artık bir
**taban çizgisi**: kurul, basit RSI/EMA kuralını yenebiliyor mu sorusunun ölçüsü.

---

## 🏦 Emir Yürütme: Alpaca Paper Trading

Kurulun kararı iki arka uçtan birine gidebilir:

| `EXECUTION_BACKEND` | Ne olur |
|---|---|
| `internal` | Emirler SQLite'taki sanal bakiyede simüle edilir (dış bağımlılık yok) |
| `alpaca` | Emirler [Alpaca](https://alpaca.markets) Paper Trading hesabına gönderilir |
| `auto` (varsayılan) | Alpaca anahtarı varsa `alpaca`, yoksa `internal` |

Alpaca'nın kazandırdığı: gerçek bir emir defteri, gerçekçi doldurma davranışı,
profesyonel sanal hesap — ve ileride **aynı kodla ABD hisselerine** (NVDA, AAPL,
TSLA) geçebilme. `config.SYMBOLS` içine `NVDA` yazman yeterli.

### Sembol dönüşümü

`BTC/USDT` → `BTC/USD` (kripto) · `NVDA` → `NVDA` (hisse). Dönüşüm
`config.alpaca_symbol()` içinde tek yerde.

### Kâr al / stop nerede duruyor

| Varlık | Koruma nerede | Neden |
|---|---|---|
| **Hisse** | Alpaca'da **bracket emri** | Borsa tarafında durur; bot kapalı olsa bile çalışır |
| **Kripto** | Bot tarafında (hızlı döngü) | Alpaca kriptoda bracket/stop emri kabul etmiyor |

Kripto seviyeleri `broker_orders` tablosuna yazılır ve 30 saniyelik döngü fiyat
seviyeye değince market satış gönderir.

### Güvenlik davranışı

Alpaca'dan pozisyon listesi okunamazsa (ağ hatası) sistem "pozisyon var" kabul
eder ve **yeni emir göndermez** — belirsizlikte mükerrer pozisyon açmaktansa
beklemek daha güvenli.

---

## 📊 Taban Çizgisi: RSI/EMA Stratejisi (backtest için)

| Aşama | Koşul |
|---|---|
| **AL** | 15m **RSI(14) < 30** *ve* fiyat **20 günlük EMA'nın üzerinde** |
| **SAT** | **+%2** kâr hedefi **veya** **-%1.5** stop-loss **veya** 15m **RSI > 70** |

Sinyaller **kapanmış** mumdan okunur (oluşmakta olan mum kullanılmaz), kâr/zarar
kontrolü ise **anlık fiyat** üzerinden her turda yapılır. Her iki yönde de
**%0.1 komisyon** simüle edilir, yani panelde gördüğün PnL gerçekçidir.

### ⚠️ Önemli: EMA neden 15m'de değil, günlük?

İstekte "RSI < 30 **ve** fiyat 20 **günlük** EMA'nın üzerinde" deniyordu ve bu
ayrım kritik. Aynı 15 dakikalık seride EMA(20) kullanılırsa bu iki koşul pratikte
**hiç birlikte oluşmaz**: RSI(14)'ün 30'un altına inmesi için 8-14 mum boyunca
süren bir düşüş gerekir, o düşüş de fiyatı kaçınılmaz olarak aynı serinin EMA20'sinin
altına indirir.

400 günlük simüle 15m veride (38.400 mum) ölçüm:

| Koşul | Sinyal sayısı |
|---|---|
| 15m RSI(14) < 30 | 2.393 |
| … **ve** fiyat > **15m** EMA20 | **0** |
| … **ve** fiyat > **günlük** EMA20 | **1.031** |

Bu yüzden EMA, `config.EMA_TIMEFRAME` ile ayrı bir zaman diliminden hesaplanır
(varsayılan `"1d"` = 20 günlük EMA). Böylece kural anlamına kavuşur:
*"günlük trend yukarıyken 15 dakikalıkta oluşan dipten al."*

İstersen klasik tek-zaman-dilimi davranışına dönebilirsin:

```python
EMA_TIMEFRAME = "15m"        # aynı seride EMA20 (pratikte sinyal üretmez)
EMA_TOLERANCE_PCT = 0.01     # veya toleransla: fiyat EMA'nın %1 altına kadar kabul
```

---

## 🔬 Backtest — stratejiyi geçmişte sınamak

Canlı deneme ayda tek haneli işlem üretir; "bu strateji kârlı mı?" sorusu için
yüzlerce işlem gerekir. Backtest bunu saniyeler içinde verir.

```bash
python backtest.py --download --days 730   # geçmiş veriyi indir (bir kez, ~5 dk)
python backtest.py                         # config.py ayarlarıyla koş
python backtest.py --rsi-buy 25 --tp 0.03  # parametre değiştirerek koş
python backtest.py --sweep --split 0.7     # parametre taraması + aşırı-uydurma testi
python backtest.py --demo-data             # internetsiz, sentetik veriyle dene
```

İndikatör hesapları `bot.py`'den **aynen** kullanılır; yani burada test ettiğin
strateji canlıda çalışanın ta kendisidir. Komisyon muhasebesi de birebir aynı:
%2 kâr al işlemi cebe `+%1.796`, %1.5 stop işlemi `-%1.697` bırakır.

### Rapor neyi söyler

| Satır | Anlamı |
|---|---|
| **AL-TUT getirisi** | Parayı hiç dokunmadan tutsaydın ne olurdu. Botun asıl rakibi bu. |
| **Kazanma oranı** | %2/%1.5 + komisyonla **başa baş oran %48.6**. Altı erime demek. |
| **Kâr faktörü** | Toplam kazanç ÷ toplam kayıp. 1'in altı zarar. |
| **Maks. düşüş** | Tepe noktadan en dip noktaya kayıp. Gerçek parada dayanabileceğin sayı. |
| **En uzun kayıp serisi** | Üst üste kaç kez kaybettiğin. Psikolojik dayanma sınırın. |

### Aşırı-uydurma (overfitting) tuzağı

`--sweep` yüzlerce kombinasyon dener ve en iyisini bulur — ama geçmişte en iyi
olan, gelecekte iyi olacak demek değildir. Yeterince parametre denersen rastgele
veride bile "harika" bir kombinasyon bulunur.

`--split 0.7` bunu yakalar: parametreleri verinin ilk %70'inde seçer, sonra
**hiç görmediği** son %30'da sınar. `test_getiri_%` sütunu bu ikinci dönemden
gelir. Seçim verisinde parlayıp testte çöken satırlar aşırı-uydurmadır; sadece
her iki dönemde de kârlı olanlar dikkate değer.

### Modelin varsayımları

* Aynı mumda hem kâr al hem stop seviyesine değilmişse **kötümser** davranır: stop varsayar.
* Giriş, sinyalin oluştuğu mumun **açılışında** yapılır (canlı bot mum kapanışından ~30 sn sonra tepki verir).
* Trend EMA'sı 15 dakikalık seriden günlüğe indirgenerek hesaplanır ve **önceki günün** değeri kullanılır — hiçbir mumda geleceğe ait bilgi yoktur.
* Emir kayması (slippage) varsayılan 0'dır; gerçekçi olmak için `--slippage 0.0005` ekleyebilirsin.

---

## ⚙️ Ayarlar (`config.py`)

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `DEMO_MODE` | `True` | `False` → gerçek Binance emri gönderir |
| `INITIAL_BALANCE` | `10000` | Sanal başlangıç bakiyesi (USDT) |
| `SYMBOLS` | `BTC/USDT, ETH/USDT` | Takip edilen çiftler |
| `TIMEFRAME` | `15m` | Sinyal zaman dilimi |
| `RSI_PERIOD` / eşikler | `14` / `30` / `70` | RSI ayarları |
| `EMA_PERIOD` / `EMA_TIMEFRAME` | `20` / `1d` | Trend filtresi |
| `TAKE_PROFIT_PCT` | `0.02` | %2 kâr al |
| `STOP_LOSS_PCT` | `0.015` | %1.5 zarar kes |
| `POSITION_SIZE_PCT` | `0.25` | Her pozisyona bakiyenin %25'i |
| `MAX_OPEN_POSITIONS` | `2` | Aynı anda açık pozisyon sayısı |
| `FEE_RATE` | `0.001` | Simüle edilen komisyon (%0.1) |
| `COOLDOWN_MINUTES` | `15` | Kapanıştan sonra aynı coinde bekleme |
| `LOOP_INTERVAL_SECONDS` | `30` | Tarama sıklığı |

Hepsi ortam değişkeniyle de verilebilir:

```bash
SYMBOLS="BTC/USDT,ETH/USDT,SOL/USDT" TAKE_PROFIT_PCT=0.03 streamlit run app.py
```

---

## 💸 Gerçek Hesaba Geçiş

Kod bunun için hazır: emirler tek bir noktadan (`TradingBot._place_real_order`)
geçer, `DEMO_MODE` bayrağı da alım ve satımda kontrol edilir.

1. Binance'te **sadece "Spot & Margin Trading"** yetkili bir API anahtarı üret.
   Para çekme (withdrawal) yetkisini **kesinlikle verme**, IP kısıtlaması ekle.
2. Anahtarları koda değil, ortam değişkenine yaz:
   ```bash
   cp .env.example .env      # doldur
   set -a && source .env && set +a
   ```
3. `config.py` içinde `DEMO_MODE = False` yap (veya `DEMO_MODE=false` ortam değişkeni).
4. Önce **testnet** ile dene: `USE_TESTNET=true`.
5. Küçük `POSITION_SIZE_PCT` ve `MAX_POSITION_USDT` ile başla.

Gerçek modda alım/satım `exchange.create_order(..., "market", ...)` ile gönderilir;
gerçekleşen fiyat, miktar ve komisyon emrin cevabından okunup aynı veritabanına
yazılır — yani panel hiç değişmeden çalışmaya devam eder.

---

## 🖥️ Panel Ekranları

* **Üst metrikler:** Toplam varlık (equity), serbest bakiye, açık pozisyon K/Z, başarılı işlem sayısı, win rate
* **Açık Pozisyonlar:** coin, miktar, giriş/güncel fiyat, anlık K/Z ($ ve %), TP/SL seviyeleri + elle kapatma
* **Piyasa Takibi:** her sembol için son fiyat, RSI, EMA ve botun o anki kararı
* **Equity Curve:** sanal bakiyenin zamana göre değişimi (başlangıç çizgisiyle)
* **İşlem Geçmişi:** kapanmış işlemler, kapanış sebebi, net kâr — CSV olarak indirilebilir
* **Bot Günlüğü:** son 120 olay

---

## 🩺 Sorun Giderme

| Belirti | Çözüm |
|---|---|
| `NetworkError` / `HTTP 451` | Binance API bazı ülkelerde/sunucularda engelli. VPN kullan veya `EXCHANGE_ID=binanceus` / `EXCHANGE_ID=kucoin` dene. |
| İnternet yok, yine de denemek istiyorum | `python bot.py --simulate` ya da `OFFLINE_SIMULATION=true streamlit run app.py` |
| Hiç alım yapmıyor | Normal olabilir: RSI<30 dipleri seyrektir. Test için `RSI_BUY_THRESHOLD=45` dene. |
| Panel donuyor gibi | Sol menüden "Otomatik yenile"yi kapat veya aralığı büyüt. |
| Baştan başlamak istiyorum | Panelden "Sanal Bakiyeyi Sıfırla" ya da `python bot.py --reset` |

---

## ⚠️ Yasal Uyarı

Bu proje eğitim ve deneme amaçlıdır, **yatırım tavsiyesi değildir**. RSI+EMA gibi
basit stratejiler geçmiş veride iyi görünüp canlıda para kaybettirebilir.
`DEMO_MODE = False` yapmadan önce stratejiyi uzun süre sanal bakiyeyle test et;
gerçek parayla oluşacak zararın sorumluluğu tamamen sana aittir.
