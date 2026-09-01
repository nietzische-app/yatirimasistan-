"""
Streamlit panelini gerçekten çalıştırıp hata veriyor mu diye bakar.

Çalıştırma:
    python tests/test_dashboard.py

Ağ erişimi gerektirmez; piyasa verisi deterministik sahte serilerle beslenir.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_dashboard.db")
os.environ["OFFLINE_SIMULATION"] = "true"
os.environ["COOLDOWN_MINUTES"] = "0"
os.environ["DASHBOARD_AUTO_REFRESH"] = "false"

APP = os.path.join(ROOT, "app.py")

for _suffix in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _suffix
    if os.path.exists(_p):
        os.remove(_p)

from streamlit.testing.v1 import AppTest

import threading

import config
import streamlit as st


def bot_thread_alive() -> bool:
    return any(t.name == "bot-runner" and t.is_alive() for t in threading.enumerate())


# 0) İZLEYİCİ MODU (Docker Compose kurulumu): panel kendi motorunu başlatmamalı
config.RUN_BOT_IN_DASHBOARD = False
at = AppTest.from_file(APP, default_timeout=90).run()
assert not at.exception, at.exception
assert not bot_thread_alive(), "izleyici modunda motor thread'i başlatılmamalı"
assert not [b for b in at.button if "Tara" in b.label], \
    "izleyici modunda 'Şimdi Tara' düğmesi gösterilmemeli (mükerrer emir riski)"
print("✓ izleyici modu: panel motoru çalıştırmıyor, tarama düğmesi gizli")

config.RUN_BOT_IN_DASHBOARD = True
st.cache_resource.clear()

# 1) Boş veritabanıyla ilk açılış
at = AppTest.from_file(APP, default_timeout=90).run()
assert not at.exception, at.exception
print("✓ panel boş veritabanıyla açıldı | metrik sayısı:", len(at.metric))
for m in at.metric:
    print("   ", m.label, "=", m.value)

# 2) "Şimdi Tara" -> sentetik veriyle bir tur
scan = [b for b in at.button if "Tara" in b.label][0]
at = scan.click().run()
assert not at.exception, at.exception
print("✓ 'Şimdi Tara' çalıştı")

# 3) Botu başlat
start = [b for b in at.button if "Başlat" in b.label][0]
at = start.click().run()
assert not at.exception, at.exception
import database as db
assert db.is_bot_running()
print("✓ Başlat düğmesi çalıştı")

# 4) Elle bir pozisyon açıp tabloları/grafiği dolu haliyle test et
import config
from bot import TradingBot
bot = TradingBot()
bot.open_trade("BTC/USDT", 65000.0, 22.5, 61000.0)
db.update_market("BTC/USDT", 66300.0, 45.0, 61000.0, "POZİSYONDA")
db.record_equity(db.get_balance(), db.get_balance() + 2500)
at = AppTest.from_file(APP, default_timeout=90).run()
assert not at.exception, at.exception
assert len(at.dataframe) >= 2, f"tablolar render olmadı: {len(at.dataframe)}"
print("✓ açık pozisyon tablosu render oldu | tablo sayısı:", len(at.dataframe))

# 5) Pozisyonu kapatıp işlem geçmişi + equity grafiğini test et
pos = db.get_open_positions("BTC/USDT")[0]
bot.close_trade(pos, 66300.0, "Test kapanışı")
bot.snapshot_equity(force=True)
at = AppTest.from_file(APP, default_timeout=90).run()
assert not at.exception, at.exception
print("✓ işlem geçmişi + equity grafiği render oldu")
print("   metrikler:", [(m.label, m.value) for m in at.metric])

# 5b) Kurul tutanağı: raporlar ve kurula verdiğimiz ek bağlam render olmalı
run_id = db.start_agent_run("BTC/USDT", 66300.0)
db.finish_agent_run(
    run_id, status="OK", rating="Overweight", action="BUY", size_factor=0.5,
    proposed_stop=61000.0, duration_sec=478.0,
    reports={
        "market_report": "RSI 45, EMA üstünde.",
        "news_report": "Önemli bir gelişme yok.",
        "final_trade_decision": "Rating: Overweight",
        "operator_context": "## İŞLETMECİDEN EK BAĞLAM\n\n- Binance anlık fiyat 66,300.",
    })
at = AppTest.from_file(APP, default_timeout=90).run()
assert not at.exception, at.exception
başlıklar = [e.label for e in at.expander]
assert any("ek bağlam" in b for b in başlıklar), başlıklar
print("✓ kurul tutanağı ve 'kurula verdiğimiz ek bağlam' bölümü render oldu")

# 6) Durdur
stop = [b for b in at.button if "Durdur" in b.label][0]
at = stop.click().run()
assert not at.exception, at.exception
assert not db.is_bot_running()
print("✓ Durdur düğmesi çalıştı")

print("\nPANEL TESTLERİ GEÇTİ ✅")
