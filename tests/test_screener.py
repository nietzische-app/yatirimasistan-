"""
Tarayıcı testleri: hangi coinin kurula gideceğine doğru karar veriyor mu?

Ağ erişimi gerektirmez; fiyat serileri elle kurulur.

Çalıştırma:
    python tests/test_screener.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_screener.db")
os.environ["OFFLINE_SIMULATION"] = "true"
for _s in ("", "-wal", "-shm"):
    if os.path.exists(os.environ["DB_PATH"] + _s):
        os.remove(os.environ["DB_PATH"] + _s)

import numpy as np
import pandas as pd

import config
import database as db
import screener as sc

db.init_db()


def frame(closes, volumes=None):
    n = len(closes)
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=n, freq="15min")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({
        "timestamp": idx.astype("int64") // 10**6,
        "open": c, "high": c * 1.001, "low": c * 0.999, "close": c,
        "volume": np.array(volumes, dtype=float) if volumes is not None else np.ones(n) * 100,
        "datetime": idx,
    }, index=idx)


# --- 1) Aşırı satım daha yüksek puan alır -----------------------------------
düşen = list(np.linspace(100, 82, 200))          # RSI dibe iner
yatay = [100.0] * 200
s_düşen = sc.score_symbol(frame(düşen), daily_ema=80.0)
s_yatay = sc.score_symbol(frame(yatay), daily_ema=95.0)
assert s_düşen["rsi"] < 35, s_düşen["rsi"]
assert s_düşen["score"] > s_yatay["score"], (s_düşen["score"], s_yatay["score"])
print(f"✓ aşırı satım önceliklendiriliyor (RSI {s_düşen['rsi']:.0f} puan {s_düşen['score']:.3f}"
      f" > yatay {s_yatay['score']:.3f})")

# --- 2) Hacim patlaması puanı yükseltir -------------------------------------
# Patlama son KAPANMIŞ mumda (-2). Son satır (-1) henüz oluşmakta olan mumdur;
# hacmi eksiktir ve kıyasa girmemelidir.
hacimler = [100.0] * 198 + [500.0, 20.0]
s_hacim = sc.score_symbol(frame(yatay, hacimler), daily_ema=95.0)
assert s_hacim["volume_ratio"] > 3, s_hacim["volume_ratio"]
assert s_hacim["score"] > s_yatay["score"], "hacim patlaması puanı artırmalı"
print(f"✓ hacim patlaması yakalanıyor ({s_hacim['volume_ratio']:.1f}x -> puan +"
      f"{s_hacim['score'] - s_yatay['score']:.3f})")

# Regresyon: oluşmakta olan mumun yarım hacmi oranı bastırmamalı. Eskiden
# iloc[-1] okunduğu için canlı taramada 12 coinin 12'si de 1.0x altındaydı ve
# bileşen hiç ateşlenemiyordu.
yarım = [100.0] * 199 + [15.0]          # son mum daha yeni açılmış
s_yarım = sc.score_symbol(frame(yatay, yarım), daily_ema=95.0)
assert 0.9 < s_yarım["volume_ratio"] < 1.1, \
    f"oluşan mum oranı bastırıyor: {s_yarım['volume_ratio']:.2f}x"
# ...ve patlama YALNIZCA oluşan mumdaysa henüz sayılmamalı (mum kapanınca sayılır)
s_erken = sc.score_symbol(frame(yatay, [100.0] * 199 + [500.0]), daily_ema=95.0)
assert s_erken["components"]["hacim_patlaması"] == 0.0, s_erken["components"]
print(f"✓ oluşmakta olan mum hacmi bozmuyor ({s_yarım['volume_ratio']:.2f}x, "
      f"eskiden 0.15x görünürdü)")

# --- 3) Trend bileşeni: EMA üstü/altı ---------------------------------------
üst = sc.score_symbol(frame(yatay), daily_ema=90.0)
alt = sc.score_symbol(frame(yatay), daily_ema=110.0)
assert üst["components"]["trend_yukarı"] == 1.0 and alt["components"]["trend_yukarı"] == 0.0
assert üst["score"] > alt["score"]
print("✓ günlük EMA üstündeki coin trend puanı alıyor")

# --- 3b) Puanlama pozisyona göre yön değiştirir -----------------------------
# Canlı taramada UNI %12.6 pump yapmış, RSI 75.8'de ve EN YÜKSEK puanlı aday
# seçilmişti — en ağır bileşeni (aşırı satım) tam sıfırken. Sebebi oynaklığın
# abs() kullanması ve pump sonrası trend puanının garanti gelmesiydi.
pump = list(np.linspace(100, 112.6, 200))     # +%12.6, aşırı alım
düşüş = list(np.linspace(100, 88, 200))       # -%12.0, aşırı satım

pump_giriş = sc.score_symbol(frame(pump), daily_ema=85.0, holding=False)
düşüş_giriş = sc.score_symbol(frame(düşüş), daily_ema=85.0, holding=False)
assert düşüş_giriş["score"] > pump_giriş["score"], \
    f"giriş ararken dip pumptan yüksek olmalı: {düşüş_giriş['score']} vs {pump_giriş['score']}"
assert pump_giriş["components"]["oynaklık"] == 0.0, \
    "giriş ararken yukarı hareket oynaklık puanı kazandırmamalı"
print(f"✓ giriş modu: dip {düşüş_giriş['score']:.3f} > pump {pump_giriş['score']:.3f} "
      f"(eskiden pump 0.400 ile 1. sıradaydı)")

# Elimizdeyse soru tersine döner: pump = kâr realizasyonu konuşulmalı
pump_çıkış = sc.score_symbol(frame(pump), daily_ema=85.0, holding=True)
assert pump_çıkış["score"] > pump_giriş["score"], \
    "pozisyondayken pump kurulun dikkatini çekmeli"
assert pump_çıkış["components"]["mod"] == "çıkış"
assert pump_çıkış["components"]["aşırı_alım"] > 0.5, pump_çıkış["components"]
print(f"✓ çıkış modu: aynı pump {pump_giriş['score']:.3f} -> {pump_çıkış['score']:.3f}")

# Pozisyondayken HER İKİ yöndeki sert hareket de önemli: pump kâr al demek,
# çöküş zararı kes demek. Giriş ararken yalnızca düşüş sayılıyordu.
düşüş_çıkış = sc.score_symbol(frame(düşüş), daily_ema=85.0, holding=True)
assert pump_çıkış["components"]["oynaklık"] > 0.5, pump_çıkış["components"]
assert pump_giriş["components"]["oynaklık"] == 0.0, pump_giriş["components"]
assert düşüş_çıkış["components"]["oynaklık"] == düşüş_giriş["components"]["oynaklık"], \
    "aşağı hareket her iki modda da aynı sayılmalı"
print(f"✓ çıkış modunda yukarı hareket de sayılıyor "
      f"(pump oynaklık: giriş 0.00 -> çıkış {pump_çıkış['components']['oynaklık']:.2f})")

# Trend bileşeni: giriş ararken EMA üstü iyidir, elimizdeyken EMA altı uyarıdır
yatay_üst = sc.score_symbol(frame(yatay), daily_ema=90.0, holding=True)
yatay_alt = sc.score_symbol(frame(yatay), daily_ema=110.0, holding=True)
assert yatay_alt["score"] > yatay_üst["score"], \
    "pozisyondayken fiyatın EMA altına düşmesi kurulun konuşması gereken şeydir"
print("✓ pozisyondayken trendin kırılması puan kazandırıyor")

# --- 4) Sıralama ve veritabanına yazma --------------------------------------
class FakeMarket:
    """Her sembole farklı bir seri verir."""
    def __init__(self):
        self.series = {
            "AAA/USDT": frame(list(np.linspace(100, 80, 200))),      # sert düşüş -> ilginç
            "BBB/USDT": frame([100.0] * 200),                        # yatay -> sıkıcı
            "CCC/USDT": frame(list(np.linspace(100, 96, 200))),      # hafif düşüş
        }
    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        if timeframe and timeframe != config.TIMEFRAME:
            return frame([50.0] * 200)          # günlük EMA hep fiyatın altında
        return self.series[symbol]

s = sc.Screener(market=FakeMarket())
rows = s.scan(["AAA/USDT", "BBB/USDT", "CCC/USDT"])
assert [r["symbol"] for r in rows][0] == "AAA/USDT", [r["symbol"] for r in rows]
assert rows[0]["rank"] == 1 and rows[-1]["rank"] == 3
saved = db.get_screener_results()
assert len(saved) == 3 and saved[0]["symbol"] == "AAA/USDT"
assert saved[0]["components"], "bileşenler kaydedilmeli (neden seçildiği görünsün)"
print(f"✓ sıralama ve kayıt: 1.{rows[0]['symbol']} 2.{rows[1]['symbol']} 3.{rows[2]['symbol']}")

# --- 5) Aday seçimi TOP_N kadar --------------------------------------------
_top = config.SCREENER_TOP_N
config.SCREENER_TOP_N = 2
adaylar = sc.Screener(market=FakeMarket()).candidates(symbols=["AAA/USDT", "BBB/USDT", "CCC/USDT"])
assert adaylar == ["AAA/USDT", "CCC/USDT"], adaylar
config.SCREENER_TOP_N = _top
print(f"✓ yalnızca en iyi 2 coin kurula gidiyor: {adaylar}")

# --- 6) Bir sembol patlarsa tarama devam etsin ------------------------------
class YarımMarket(FakeMarket):
    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        if symbol == "BBB/USDT":
            raise RuntimeError("borsa hatası")
        return super().fetch_ohlcv(symbol, timeframe, limit)

rows = sc.Screener(market=YarımMarket()).scan(["AAA/USDT", "BBB/USDT", "CCC/USDT"])
assert len(rows) == 2 and all(r["symbol"] != "BBB/USDT" for r in rows)
print("✓ tek coin hata verse bile tarama sürüyor")

# --- 7) Bot entegrasyonu ----------------------------------------------------
from bot import TradingBot
bot = TradingBot()
bot.market = FakeMarket()
bot._screener = sc.Screener(market=FakeMarket())

_en, _syms = config.SCREENER_ENABLED, config.SYMBOLS
config.SCREENER_ENABLED = False
assert bot.refresh_candidates() == list(config.SYMBOLS), "tarayıcı kapalıyken sabit liste"
print("✓ tarayıcı kapalıyken davranış değişmiyor")

# Bot entegrasyonunda gerçek coin adları gerekli: refresh_candidates artık
# kurulun analiz edemediği coinleri eliyor.
class GerçekAdlıMarket(FakeMarket):
    """Aynı serileri kurulun tanıdığı coin adlarıyla verir."""
    def __init__(self):
        super().__init__()
        self.series = {
            "BTC/USDT": self.series["AAA/USDT"],     # sert düşüş -> ilginç
            "ETH/USDT": self.series["BBB/USDT"],     # yatay -> sıkıcı
            "SOL/USDT": self.series["CCC/USDT"],     # hafif yükseliş
            "UNI/USDT": self.series["AAA/USDT"],     # ilginç AMA kurul analiz edemez
        }

bot._screener = sc.Screener(market=GerçekAdlıMarket())
config.SCREENER_ENABLED = True
config.SCREENER_TOP_N = 2
config.WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
bot._candidates, bot._last_screen = [], 0.0
adaylar = bot.refresh_candidates()
assert adaylar == ["BTC/USDT", "SOL/USDT"], adaylar

# Kurulun analiz edemediği coin en yüksek puanlı olsa bile slot harcamamalı:
# yerine bir alttaki uygun coin çıkmalı.
config.WATCHLIST = ["UNI/USDT", "BTC/USDT", "ETH/USDT", "SOL/USDT"]
bot._candidates, bot._last_screen = [], 0.0
adaylar_filtreli = bot.refresh_candidates()
assert "UNI/USDT" not in adaylar_filtreli, adaylar_filtreli
assert adaylar_filtreli == ["BTC/USDT", "SOL/USDT"], adaylar_filtreli
print(f"✓ kurulun analiz edemediği coin adaylıktan eleniyor: {adaylar_filtreli}")

config.WATCHLIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
bot._candidates, bot._last_screen = [], 0.0
bot.refresh_candidates()

# Açık pozisyon aday olmasa da izlenmeli (korumasız kalmasın)
bot.open_trade("ETH/USDT", 100.0, reason="test")
aktif = bot.active_symbols()
assert "ETH/USDT" in aktif, f"açık pozisyon izlenmeli: {aktif}"
assert "BTC/USDT" in aktif and "SOL/USDT" in aktif
print(f"✓ aktif semboller = adaylar + açık pozisyonlar ({len(aktif)} sembol)")

# Elimizdeki coin taramada "çıkış" moduyla puanlanmalı
satırlar = sc.Screener(market=GerçekAdlıMarket()).scan(
    ["BTC/USDT", "ETH/USDT"], holdings={"ETH/USDT"})
modlar = {r["symbol"]: r["components"]["mod"] for r in satırlar}
assert modlar == {"BTC/USDT": "giriş", "ETH/USDT": "çıkış"}, modlar
print(f"✓ tarama modu pozisyona göre seçiliyor: {modlar}")

# holdings verilmezse dahili defterden okunur (ETH/USDT açık pozisyonda)
otomatik = {r["symbol"]: r["components"]["mod"]
            for r in sc.Screener(market=GerçekAdlıMarket()).scan(["BTC/USDT", "ETH/USDT"])}
assert otomatik["ETH/USDT"] == "çıkış", otomatik
assert set(bot.held_symbols()) >= {"ETH/USDT"}, bot.held_symbols()
print("✓ pozisyonlar otomatik algılanıyor (holdings verilmese de)")

config.SCREENER_ENABLED, config.SYMBOLS, config.SCREENER_TOP_N = _en, _syms, _top
print("\nTARAYICI TESTLERİ GEÇTİ ✅")
