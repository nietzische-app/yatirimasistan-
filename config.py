"""
config.py
---------
Tüm ayarlar tek yerde. Buradaki değerleri değiştirerek stratejiyi,
sembolleri ve risk parametrelerini yeniden kodlamadan yönetebilirsin.

Her ayar istersen ortam değişkeni (environment variable) ile de ezilebilir.
Örn:  export INITIAL_BALANCE=25000
"""

import os

# --------------------------------------------------------------------------
# Yardımcı okuyucular (ortam değişkeni > varsayılan)
# --------------------------------------------------------------------------
def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "evet")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# 1) ÇALIŞMA MODU
# --------------------------------------------------------------------------
# DEMO_MODE = True  -> Hiçbir gerçek emir gönderilmez, sadece sanal bakiye ile simülasyon.
# DEMO_MODE = False -> Gerçek Binance emirleri gönderilir (API key/secret zorunlu).
DEMO_MODE = _env_bool("DEMO_MODE", True)

# Gerçek moda geçerken doldurulacak anahtarlar. Kodun içine YAZMA, ortam
# değişkeni olarak ver:  export BINANCE_API_KEY=...   export BINANCE_API_SECRET=...
BINANCE_API_KEY = _env_str("BINANCE_API_KEY", "")
BINANCE_API_SECRET = _env_str("BINANCE_API_SECRET", "")

# Binance testnet (sanal para ama gerçek API akışı) kullanmak istersen True yap.
USE_TESTNET = _env_bool("USE_TESTNET", False)

# İnternet erişimi olmadan arayüzü denemek için sentetik fiyat üretir.
# Gerçek kullanımda mutlaka False kalmalı.
OFFLINE_SIMULATION = _env_bool("OFFLINE_SIMULATION", False)

EXCHANGE_ID = _env_str("EXCHANGE_ID", "binance")

# --------------------------------------------------------------------------
# 2) SANAL HESAP (PAPER TRADING)
# --------------------------------------------------------------------------
INITIAL_BALANCE = _env_float("INITIAL_BALANCE", 10_000.0)   # USDT
QUOTE_CURRENCY = _env_str("QUOTE_CURRENCY", "USDT")

# Her pozisyona bakiyenin yüzde kaçı girsin (0.25 = %25)
POSITION_SIZE_PCT = _env_float("POSITION_SIZE_PCT", 0.25)
# Pozisyon başına en fazla / en az kaç USDT kullanılsın
MAX_POSITION_USDT = _env_float("MAX_POSITION_USDT", 5_000.0)
MIN_POSITION_USDT = _env_float("MIN_POSITION_USDT", 20.0)

# Aynı anda açık kalabilecek maksimum pozisyon sayısı
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 2)

# Binance spot komisyonu (%0.1). Alışta ve satışta ayrı ayrı uygulanır.
FEE_RATE = _env_float("FEE_RATE", 0.001)

# --------------------------------------------------------------------------
# 3) TAKİP EDİLEN PİYASALAR & STRATEJİ
# --------------------------------------------------------------------------
SYMBOLS = [s.strip() for s in _env_str("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]

TIMEFRAME = _env_str("TIMEFRAME", "15m")     # 15 dakikalık mumlar
CANDLE_LIMIT = _env_int("CANDLE_LIMIT", 200) # indikatör için çekilecek mum sayısı

RSI_PERIOD = _env_int("RSI_PERIOD", 14)
RSI_BUY_THRESHOLD = _env_float("RSI_BUY_THRESHOLD", 30.0)    # RSI < 30 -> AL sinyali
RSI_SELL_THRESHOLD = _env_float("RSI_SELL_THRESHOLD", 70.0)  # RSI > 70 -> SAT sinyali

# --- Trend filtresi (EMA) ---
# ÖNEMLİ: EMA, RSI'dan FARKLI bir zaman diliminde hesaplanır.
# "20 GÜNLÜK EMA" -> EMA_TIMEFRAME="1d", EMA_PERIOD=20.
# (Aynı 15m serisinde EMA20 kullanılırsa "RSI<30 VE fiyat>EMA20" koşulu
#  matematiksel olarak neredeyse hiç oluşmaz; ayrıntı için README'ye bak.)
EMA_PERIOD = _env_int("EMA_PERIOD", 20)
EMA_TIMEFRAME = _env_str("EMA_TIMEFRAME", "1d")          # "1d" = 20 günlük EMA; TIMEFRAME ile aynı yapılabilir
EMA_REFRESH_SECONDS = _env_int("EMA_REFRESH_SECONDS", 900)  # trend EMA'sı kaç saniyede bir yenilensin
EMA_TOLERANCE_PCT = _env_float("EMA_TOLERANCE_PCT", 0.0)    # fiyat EMA'nın en fazla bu oran altında olabilir (0.01 = %1)

TAKE_PROFIT_PCT = _env_float("TAKE_PROFIT_PCT", 0.02)   # %2 kâr al
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 0.015)      # %1.5 zarar kes

# Bir sembolde pozisyon kapandıktan sonra kaç dakika yeni giriş yapılmasın
COOLDOWN_MINUTES = _env_int("COOLDOWN_MINUTES", 15)

# --------------------------------------------------------------------------
# 4) DÖNGÜ / KAYIT AYARLARI
# --------------------------------------------------------------------------
LOOP_INTERVAL_SECONDS = _env_int("LOOP_INTERVAL_SECONDS", 30)      # bot kaç saniyede bir piyasayı kontrol etsin
EQUITY_SNAPSHOT_SECONDS = _env_int("EQUITY_SNAPSHOT_SECONDS", 60)  # equity eğrisine kaç saniyede bir nokta eklensin
MAX_LOG_ROWS = _env_int("MAX_LOG_ROWS", 500)                       # veritabanında tutulacak log satırı

DB_PATH = _env_str("DB_PATH", os.path.join(BASE_DIR, "data", "trading.db"))

# --------------------------------------------------------------------------
# 5) ARAYÜZ
# --------------------------------------------------------------------------
APP_TITLE = _env_str("APP_TITLE", "Yatırım Asistanı · Paper Trading")
DASHBOARD_REFRESH_SECONDS = _env_int("DASHBOARD_REFRESH_SECONDS", 10)
DASHBOARD_AUTO_REFRESH = _env_bool("DASHBOARD_AUTO_REFRESH", True)

# Ticaret motoru panelin İÇİNDE bir arka plan thread'i olarak çalışsın mı?
#   True  -> tek başına `streamlit run app.py` yeterli (yerel kullanım)
#   False -> motor ayrı bir süreçte/container'da (`python bot.py`); panel sadece izler.
# Docker Compose kurulumunda panel container'ı bunu false yapar ki iki süreç
# aynı anda emir açıp mükerrer pozisyon yaratmasın.
RUN_BOT_IN_DASHBOARD = _env_bool("RUN_BOT_IN_DASHBOARD", True)


def summary() -> dict:
    """Arayüzde göstermek için özet ayar sözlüğü."""
    return {
        "Mod": "DEMO (Paper Trading)" if DEMO_MODE else "GERÇEK EMİR",
        "Borsa": EXCHANGE_ID,
        "Semboller": ", ".join(SYMBOLS),
        "Zaman dilimi": TIMEFRAME,
        "RSI": f"{RSI_PERIOD} (AL < {RSI_BUY_THRESHOLD:g} / SAT > {RSI_SELL_THRESHOLD:g})",
        "EMA (trend filtresi)": f"{EMA_PERIOD} × {EMA_TIMEFRAME}"
        + (f" (tolerans %{EMA_TOLERANCE_PCT * 100:g})" if EMA_TOLERANCE_PCT else ""),
        "Kâr al / Zarar kes": f"%{TAKE_PROFIT_PCT * 100:g} / %{STOP_LOSS_PCT * 100:g}",
        "Pozisyon büyüklüğü": f"%{POSITION_SIZE_PCT * 100:g} (maks {MAX_POSITION_USDT:,.0f} {QUOTE_CURRENCY})",
        "Komisyon": f"%{FEE_RATE * 100:g}",
        "Döngü": f"{LOOP_INTERVAL_SECONDS} sn",
        "Motor": "panel içinde" if RUN_BOT_IN_DASHBOARD else "ayrı süreç (bot.py)",
    }
