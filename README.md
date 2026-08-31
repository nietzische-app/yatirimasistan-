# 📈 Binance Paper Trading Bot + Streamlit Panel

Gerçek parayla **çalışmayan**, canlı Binance fiyatlarıyla beslenen bir sanal
alım-satım (paper trading) botu ve onu tarayıcıdan takip edebileceğin bir
kontrol paneli.

* **Sanal bakiye:** 10.000 USDT (ayarlanabilir)
* **Takip:** BTC/USDT ve ETH/USDT, 15 dakikalık mumlar
* **Strateji:** 15m RSI(14) diple + 20 günlük EMA trend filtresi → AL; %2 kâr al / %1.5 stop / RSI>70 → SAT
* **Panel:** metrikler, açık pozisyonlar, işlem geçmişi, equity curve, Başlat/Durdur/Sıfırla düğmeleri
* **Gerçek hesaba geçiş:** `config.py` içinde tek satır — `DEMO_MODE = False`

---

## 📁 Dosya Yapısı

```
.
├── app.py                    # Streamlit web paneli (arayüz + kontrol düğmeleri)
├── bot.py                    # Ticaret motoru: veri çekme, indikatör, al/sat kararı
├── database.py               # SQLite katmanı: bakiye, pozisyon, işlem, equity, log
├── config.py                 # Tüm ayarlar (ortam değişkeniyle de ezilebilir)
├── requirements.txt
├── Dockerfile                # Tek imaj: hem motor hem panel
├── docker-compose.yml        # bot + dashboard servisleri, kalıcı volume
├── DEPLOY.md                 # Hetzner VPS dağıtım rehberi (adım adım)
├── deploy/
│   ├── setup-caddy.sh             # panele şifre + HTTPS kurar (tek komut)
│   ├── docker-compose.caddy.yml   # şifre + HTTPS ile yayınlama (opsiyonel)
│   ├── docker-compose.public.yml  # 8501'i doğrudan açma (opsiyonel)
│   └── Caddyfile
├── .env.example              # Gerçek moda geçerken kullanılacak şablon
├── data/                     # SQLite veritabanı buraya yazılır (git'e girmez)
└── tests/
    ├── test_strategy.py      # Strateji + PnL + veritabanı testleri (internet gerekmez)
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
python bot.py --once        # tek tur tarayıp çık
python bot.py --force       # panel "durduruldu" olsa bile çalış
python bot.py --simulate    # internet olmadan sentetik fiyatlarla dene
python bot.py --reset       # sanal bakiyeyi 10.000 USDT'ye döndür
python tests/test_strategy.py && python tests/test_dashboard.py   # testler
```

---

## 🧠 Strateji

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
