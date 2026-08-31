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
# 3B) KARAR MOTORU: TradingAgents çoklu ajan kurulu
# --------------------------------------------------------------------------
# Al/sat kararını artık RSI/EMA kuralları değil, TradingAgents kurulu verir.
# Kurul yavaş çalışır (LLM maliyeti); pozisyon takibi (TP/SL) hızlı döngüde kalır.
#
#   "agents" -> kararı yapay zekâ kurulu verir (varsayılan)
#   "manual" -> kurul kapalı; yalnız açık pozisyonlar yönetilir, yeni alım olmaz
DECISION_ENGINE = _env_str("DECISION_ENGINE", "agents")

# Kurul bir sembol için kaç dakikada bir toplansın.
# DİKKAT: her toplantı onlarca LLM çağrısı demektir. 30 saniyede bir çalıştırmak
# günde binlerce dolar maliyet çıkarır; alt sınır bilerek 15 dakikadır.
AGENT_INTERVAL_MINUTES = max(15, _env_int("AGENT_INTERVAL_MINUTES", 60))

# Gün başına toplam toplantı üst sınırı (maliyet emniyet supabı, tüm semboller)
AGENT_MAX_RUNS_PER_DAY = _env_int("AGENT_MAX_RUNS_PER_DAY", 60)

# Geçici hatadan (429, zaman aşımı) sonra tam süreyi beklemeden tekrar dene.
# Checkpoint açıkken tekrar deneme kaldığı yerden devam eder, baştan başlamaz.
AGENT_RETRY_MINUTES = _env_int("AGENT_RETRY_MINUTES", 10)

# TradingAgents checkpoint'i: toplantı ortasında hata alırsa bir sonraki
# denemede en son başarılı adımdan devam eder. Yarıda kalan toplantının
# parası ve süresi çöpe gitmez.
AGENT_CHECKPOINT_ENABLED = _env_bool("AGENT_CHECKPOINT_ENABLED", True)

# Tek bir toplantı bu süreyi aşarsa iptal edilir.
# Gözlenen: 4 analist + 2 tur tartışma + 2 tur risk ile ~15-25 dakika.
AGENT_RUN_TIMEOUT_SECONDS = _env_int("AGENT_RUN_TIMEOUT_SECONDS", 2400)

# --- LLM sağlayıcısı ---
# OpenRouter / DeepSeek / yerel sunucular OpenAI uyumlu uçtan çalışır:
#   LLM_PROVIDER=openai + LLM_BACKEND_URL=https://openrouter.ai/api/v1
LLM_PROVIDER = _env_str("LLM_PROVIDER", "openai")
LLM_BACKEND_URL = _env_str("LLM_BACKEND_URL", "https://openrouter.ai/api/v1")
LLM_DEEP_MODEL = _env_str("LLM_DEEP_MODEL", "deepseek/deepseek-chat")     # derin düşünen (yargıçlar)
LLM_QUICK_MODEL = _env_str("LLM_QUICK_MODEL", "deepseek/deepseek-chat")   # hızlı (analistler)
LLM_API_KEY = (_env_str("OPENROUTER_API_KEY", "") or _env_str("OPENAI_API_KEY", "")
               or _env_str("DEEPSEEK_API_KEY", ""))
LLM_TEMPERATURE = _env_str("LLM_TEMPERATURE", "")     # boş = sağlayıcı varsayılanı
LLM_MAX_TOKENS = _env_str("LLM_MAX_TOKENS", "")
LLM_MAX_RETRIES = _env_str("LLM_MAX_RETRIES", "3")

# --- Kurulun bileşimi (hepsi açık = %100 kapasite) ---
AGENT_ANALYSTS = [a.strip() for a in
                  _env_str("AGENT_ANALYSTS", "market,social,news,fundamentals").split(",")
                  if a.strip()]
AGENT_DEBATE_ROUNDS = _env_int("AGENT_DEBATE_ROUNDS", 2)       # boğa/ayı tartışma turu
AGENT_RISK_ROUNDS = _env_int("AGENT_RISK_ROUNDS", 2)           # risk kurulu turu
AGENT_OUTPUT_LANGUAGE = _env_str("AGENT_OUTPUT_LANGUAGE", "Turkish")

# --- Veri sağlayıcı anahtarları (ajanların haber/temel verisi için) ---
ALPHA_VANTAGE_API_KEY = _env_str("ALPHA_VANTAGE_API_KEY", "")
FRED_API_KEY = _env_str("FRED_API_KEY", "")

# --- Karar -> emir eşlemesi ---
# Kurul 5 kademeli not verir. Hangi not ne kadar pozisyon açsın:
AGENT_SIZE_BY_RATING = {
    "buy": 1.0,          # POSITION_SIZE_PCT'in tamamı
    "overweight": 0.5,   # yarısı
    "hold": 0.0,
    "underweight": 0.0,
    "sell": 0.0,
}
# Kurul "Sell/Underweight" derse açık pozisyon kapatılsın mı?
AGENT_EXIT_ON_SELL = _env_bool("AGENT_EXIT_ON_SELL", True)
# Kurulun önerdiği stop-loss kullanılsın mı (yoksa config.STOP_LOSS_PCT)
AGENT_USE_PROPOSED_STOP = _env_bool("AGENT_USE_PROPOSED_STOP", True)
# Ajan önerisi bu sınırların dışındaysa yok sayılır (saçma stop'a karşı koruma)
AGENT_STOP_MIN_PCT = _env_float("AGENT_STOP_MIN_PCT", 0.005)
AGENT_STOP_MAX_PCT = _env_float("AGENT_STOP_MAX_PCT", 0.15)

# Binance sembolünü ajanların veri sağlayıcısının anladığı tickera çevirir
# (BTC/USDT -> BTCUSD -> yfinance'te BTC-USD).
def agent_ticker(symbol: str) -> str:
    base, _, quote = symbol.partition("/")
    return f"{base}{'USD' if quote.upper().startswith('USD') else quote.upper()}"


# --------------------------------------------------------------------------
# 3C) EMİR YÜRÜTME: dahili sanal defter mi, Alpaca paper hesabı mı?
# --------------------------------------------------------------------------
#   "internal" -> emirler SQLite'taki sanal bakiyede simüle edilir (varsayılan davranış)
#   "alpaca"   -> emirler Alpaca Paper Trading hesabına gönderilir
#   "auto"     -> Alpaca anahtarları varsa alpaca, yoksa internal
EXECUTION_BACKEND = _env_str("EXECUTION_BACKEND", "auto")

ALPACA_API_KEY = _env_str("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = _env_str("ALPACA_SECRET_KEY", "")
# True -> https://paper-api.alpaca.markets (sanal para). Gerçek hesap için False.
ALPACA_PAPER = _env_bool("ALPACA_PAPER", True)

# Alpaca emirlerinde pozisyon büyüklüğü: hesabın öz sermayesinin yüzdesi
ALPACA_POSITION_PCT = _env_float("ALPACA_POSITION_PCT", POSITION_SIZE_PCT)
ALPACA_MAX_NOTIONAL = _env_float("ALPACA_MAX_NOTIONAL", MAX_POSITION_USDT)
ALPACA_MIN_NOTIONAL = _env_float("ALPACA_MIN_NOTIONAL", 10.0)

# Hisse senetlerinde emir bracket (kâr al + stop) olarak gönderilebilir.
# Kriptoda Alpaca bracket/stop emri kabul etmez; koruma bot tarafında yapılır.
ALPACA_USE_BRACKET_FOR_EQUITY = _env_bool("ALPACA_USE_BRACKET_FOR_EQUITY", True)
# Borsa kapalıyken hisse emri gönderilsin mi (gtc ile sıraya girer)
ALPACA_ALLOW_QUEUED_EQUITY = _env_bool("ALPACA_ALLOW_QUEUED_EQUITY", True)


def alpaca_symbol(symbol: str) -> str:
    """
    Bizim sembolümüzü Alpaca'nın beklediği biçime çevirir.
        BTC/USDT -> BTC/USD   (kripto)
        NVDA     -> NVDA      (hisse)
    """
    if "/" in symbol:
        base, _, _quote = symbol.partition("/")
        return f"{base.upper()}/USD"
    return symbol.upper()


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


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
        "Karar motoru": ("TradingAgents kurulu" if DECISION_ENGINE == "agents" else "kapalı (manuel)"),
        "Emir yürütme": (f"Alpaca {'PAPER' if ALPACA_PAPER else 'CANLI'}"
                         if (EXECUTION_BACKEND == "alpaca"
                             or (EXECUTION_BACKEND == "auto" and ALPACA_API_KEY))
                         else "dahili sanal defter"),
        "Kurul sıklığı": f"{AGENT_INTERVAL_MINUTES} dk / sembol (günde en fazla {AGENT_MAX_RUNS_PER_DAY})",
        "Ajanlar": ", ".join(AGENT_ANALYSTS) + f" | tartışma {AGENT_DEBATE_ROUNDS}, risk {AGENT_RISK_ROUNDS} tur",
        "LLM": f"{LLM_DEEP_MODEL} @ {LLM_BACKEND_URL or 'varsayılan'}",
    }
