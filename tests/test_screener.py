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
hacimler = [100.0] * 199 + [500.0]
s_hacim = sc.score_symbol(frame(yatay, hacimler), daily_ema=95.0)
assert s_hacim["volume_ratio"] > 3, s_hacim["volume_ratio"]
assert s_hacim["score"] > s_yatay["score"], "hacim patlaması puanı artırmalı"
print(f"✓ hacim patlaması yakalanıyor ({s_hacim['volume_ratio']:.1f}x -> puan +"
      f"{s_hacim['score'] - s_yatay['score']:.3f})")

# --- 3) Trend bileşeni: EMA üstü/altı ---------------------------------------
üst = sc.score_symbol(frame(yatay), daily_ema=90.0)
alt = sc.score_symbol(frame(yatay), daily_ema=110.0)
assert üst["components"]["trend_yukarı"] == 1.0 and alt["components"]["trend_yukarı"] == 0.0
assert üst["score"] > alt["score"]
print("✓ günlük EMA üstündeki coin trend puanı alıyor")

# --- 4) Sıralama ve veritabanına yazma --------------------------------------
class FakeMarket:
    """Her sembole farklı bir seri verir."""
    def __init__(self):
        self.series = {
            "AAA/USDT": frame(list(np.linspace(100, 80, 200))),      # sert düşüş -> ilginç
            "BBB/USDT": frame([100.0] * 200),                        # yatay -> sıkıcı
            "CCC/USDT": frame(list(np.linspace(100, 104, 200))),     # hafif yükseliş
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

config.SCREENER_ENABLED = True
config.SCREENER_TOP_N = 2
config.WATCHLIST = ["AAA/USDT", "BBB/USDT", "CCC/USDT"]
bot._candidates, bot._last_screen = [], 0.0
adaylar = bot.refresh_candidates()
assert adaylar == ["AAA/USDT", "CCC/USDT"], adaylar

# Açık pozisyon aday olmasa da izlenmeli (korumasız kalmasın)
bot.open_trade("BBB/USDT", 100.0, reason="test")
aktif = bot.active_symbols()
assert "BBB/USDT" in aktif, f"açık pozisyon izlenmeli: {aktif}"
assert "AAA/USDT" in aktif and "CCC/USDT" in aktif
print(f"✓ aktif semboller = adaylar + açık pozisyonlar ({len(aktif)} sembol)")

config.SCREENER_ENABLED, config.SYMBOLS, config.SCREENER_TOP_N = _en, _syms, _top
print("\nTARAYICI TESTLERİ GEÇTİ ✅")
