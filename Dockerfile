# ---------------------------------------------------------------------------
# Binance Paper Trading Bot + Streamlit Panel
# Tek imaj, iki rol: `python bot.py` (motor) ve `streamlit run app.py` (panel).
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Türkiye saati; panel zaman damgalarını buna göre gösterir.
ENV TZ=Europe/Istanbul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl -> HEALTHCHECK için, tzdata -> saat dilimi için
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Önce sadece requirements: kod değişince pip katmanı cache'ten gelsin
COPY requirements.txt ./
# ccxt (Binance verisi), streamlit (panel), alpaca-py (emir yürütme) burada kurulur
RUN pip install --no-cache-dir -r requirements.txt

# TradingAgents çoklu ajan kurulu (git submodule olarak geliyor).
# Ayrı katman: proje kodu değişince langchain/langgraph yeniden kurulmasın.
# NOT: build'den önce sunucuda `git submodule update --init --recursive` gerekir.
COPY trading_agents/ ./trading_agents/
RUN pip install --no-cache-dir ./trading_agents

COPY . .

# root olmayan kullanıcı + veritabanı klasörü (named volume bu izinleri devralır)
RUN useradd --create-home --uid 1000 trader \
    && mkdir -p /app/data \
    && chown -R trader:trader /app
USER trader

# SQLite dosyası container dışında yaşasın
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/trading.db

EXPOSE 8501

# Streamlit'in kendi sağlık ucu (bot servisinde compose ile devre dışı bırakılır)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# Varsayılan rol: panel. Motor için compose `command:` ile ezer.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
