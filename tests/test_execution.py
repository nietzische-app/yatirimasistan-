"""
Uygulama katmanı testleri: pozisyon açma/kapama, kâr al & stop koruması,
komisyon dahil PnL muhasebesi ve veritabanı tutarlılığı.

Al/sat KARARI artık burada test edilmiyor — o TradingAgents kuruluna geçti
(bkz. tests/test_agents.py). Bu dosya kararın doğru uygulandığını doğrular.

Çalıştırma:
    python tests/test_execution.py

Ağ erişimi gerektirmez.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_execution.db")
os.environ["OFFLINE_SIMULATION"] = "true"
for _s in ("", "-wal", "-shm"):
    if os.path.exists(os.environ["DB_PATH"] + _s):
        os.remove(os.environ["DB_PATH"] + _s)

import numpy as np
import pandas as pd

import config
import database as db
from bot import TradingBot, calculate_ema, calculate_rsi

db.init_db()
bot = TradingBot()


def frame(closes):
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(closes), freq="15min")
    return pd.DataFrame({"timestamp": idx.astype("int64") // 10**6, "open": closes,
                         "high": closes, "low": closes, "close": closes,
                         "volume": [1.0] * len(closes), "datetime": idx})


def feed(closes, daily=None):
    def _fetch(symbol, timeframe=None, limit=None):
        if timeframe and timeframe != config.TIMEFRAME:
            return frame(daily if daily is not None else closes)
        return frame(closes)
    bot.market.fetch_ohlcv = _fetch
    bot._trend_cache.clear()


def net_pct(move: float) -> float:
    budget = 1000.0
    amount = (budget - budget * config.FEE_RATE) / 100.0
    return (amount * 100.0 * (1 + move) * (1 - config.FEE_RATE) - budget) / budget * 100.0


# --- 1) İndikatörler (panelde gösterim ve backtest için hâlâ kullanılıyor) ----
assert calculate_rsi(pd.Series(np.arange(1, 60, dtype=float)), 14).iloc[-1] == 100.0
assert calculate_rsi(pd.Series(np.arange(60, 1, -1, dtype=float)), 14).iloc[-1] == 0.0
assert abs(calculate_ema(pd.Series([100.0] * 40), 20).iloc[-1] - 100.0) < 1e-9
print("✓ RSI/EMA hesapları")

# --- 2) Pozisyon açma: bakiye, komisyon, TP/SL seviyeleri --------------------
db.reset_account()
pid = bot.open_trade("BTC/USDT", 100.0, size_factor=1.0, reason="test")
pos = db.get_open_positions("BTC/USDT")[0]
assert pid and abs(pos["cost"] - 2500.0) < 1e-9, pos["cost"]
assert abs(db.get_balance() - 7500.0) < 1e-9
assert abs(pos["take_profit"] / pos["entry_price"] - (1 + config.TAKE_PROFIT_PCT)) < 1e-12
assert abs(pos["stop_loss"] / pos["entry_price"] - (1 - config.STOP_LOSS_PCT)) < 1e-12
assert abs(pos["amount"] - (2500.0 - 2500.0 * config.FEE_RATE) / 100.0) < 1e-12
print(f"✓ pozisyon açıldı: {pos['amount']:.6f} @ {pos['entry_price']:.2f}, TP {pos['take_profit']:.2f}, SL {pos['stop_loss']:.2f}")

# --- 3) Büyüklük çarpanı (kurulun 'Overweight' notu yarım pozisyon açar) -----
db.reset_account()
bot.open_trade("ETH/USDT", 100.0, size_factor=0.5, reason="test")
assert abs(db.get_open_positions("ETH/USDT")[0]["cost"] - 1250.0) < 1e-9
print("✓ pozisyon büyüklüğü çarpanı uygulanıyor")

# --- 4) Ajanın önerdiği stop kullanılıyor -----------------------------------
db.reset_account()
bot.open_trade("BTC/USDT", 100.0, stop_price=96.0, reason="kurul")
assert abs(db.get_open_positions("BTC/USDT")[0]["stop_loss"] - 96.0) < 1e-9
print("✓ ajan stop önerisi pozisyona işleniyor")

# --- 5) Kâr al ve stop korumaları -------------------------------------------
db.reset_account()
bot.open_trade("BTC/USDT", 100.0, reason="test")
p = db.get_open_positions("BTC/USDT")[0]
assert bot.check_exit(p, p["take_profit"]) is not None
assert bot.check_exit(p, p["stop_loss"]) is not None
assert bot.check_exit(p, 100.5) is None, "TP/SL arasında çıkış olmamalı"
trade = bot.close_trade(p, p["take_profit"], "KÂR AL (%2)")
assert abs(trade["pnl_pct"] - net_pct(config.TAKE_PROFIT_PCT)) < 1e-9, trade["pnl_pct"]
print(f"✓ kâr al: PnL %{trade['pnl_pct']:+.4f} (formülle birebir)")

db.reset_account()
bot.open_trade("BTC/USDT", 100.0, reason="test")
p = db.get_open_positions("BTC/USDT")[0]
trade = bot.close_trade(p, p["stop_loss"], "STOP-LOSS")
assert abs(trade["pnl_pct"] - net_pct(-config.STOP_LOSS_PCT)) < 1e-9
assert abs(db.get_balance() - trade["balance_after"]) < 1e-9
print(f"✓ stop-loss: PnL %{trade['pnl_pct']:+.4f}")

# --- 6) Mükerrer kapatma engelleniyor (bot + panel aynı anda) ----------------
db.reset_account()
bot.open_trade("BTC/USDT", 100.0, reason="test")
p = db.get_open_positions("BTC/USDT")[0]
db.close_position(p["id"], 101.0, "ilk")
try:
    db.close_position(p["id"], 101.0, "ikinci")
    raise SystemExit("HATA: pozisyon iki kez kapatıldı")
except ValueError:
    pass
assert len(db.get_trades()) == 1
print("✓ mükerrer kapatma engellendi")

# --- 7) Yetersiz bakiye ------------------------------------------------------
db.reset_account()
db.update_balance(5.0)
assert bot.open_trade("BTC/USDT", 100.0, reason="test") is None
assert not db.get_open_positions()
print("✓ yetersiz bakiye koruması")

# --- 8) Hızlı döngü: kararsız da olsa piyasa görüntüsü yazılıyor ------------
db.reset_account()
closes = list(np.linspace(100, 160, 80)) + [159, 156, 152, 148, 143, 138, 133, 130]
feed(closes, list(np.linspace(60, 120, 120)))
bot.process_symbol("BTC/USDT")
market = db.get_market()["BTC/USDT"]
assert market["price"] > 0 and market["rsi"] is not None and market["ema"] is not None
assert not db.get_open_positions(), "kurul kararı olmadan pozisyon açılmamalı"
print(f"✓ hızlı döngü piyasa görüntüsü yazıyor (RSI {market['rsi']:.1f}) ve kendiliğinden işlem AÇMIYOR")

# --- 9) İstatistikler, equity ve sıfırlama ----------------------------------
bot.open_trade("BTC/USDT", 130.0, reason="test")
bot.snapshot_equity(force=True)
stats = db.get_stats({"BTC/USDT": 130.0})
assert abs(stats["equity"] - (stats["balance"] + stats["open_value"])) < 1e-9
assert len(db.get_equity_curve()) >= 1
db.reset_account()
assert db.get_balance() == 10000.0 and not db.get_trades() and not db.get_open_positions()
print("✓ istatistik, equity ve sıfırlama")

# --- 10) Başlat/durdur bayrağı ----------------------------------------------
db.set_bot_running(True);  assert db.is_bot_running()
db.set_bot_running(False); assert not db.is_bot_running()
print("✓ başlat/durdur bayrağı")



# --- Komisyon düşülmüş işlem ekonomisi -------------------------------------
# Ham %2 / %1.5 oranı "lehimize" görünür ama komisyon iki yönden kesilir:
# kazançtan düşer, kayba EKLENİR. Başabaş oranı bilinmeden stratejinin kârlı
# olup olmadığı söylenemez.
eko_bedava = config.trade_economics(0.0)
assert abs(eko_bedava["net_win_pct"] - config.TAKE_PROFIT_PCT * 100) < 1e-9
assert abs(eko_bedava["net_loss_pct"] - config.STOP_LOSS_PCT * 100) < 1e-9
print("✓ komisyon sıfırken ham oranlar aynen çıkıyor")

eko = config.trade_economics(config.ALPACA_FEE_RATE)
assert eko["net_win_pct"] < config.TAKE_PROFIT_PCT * 100, "komisyon kazancı azaltmalı"
assert eko["net_loss_pct"] > config.STOP_LOSS_PCT * 100, "komisyon kaybı BÜYÜTMELİ"
assert eko["breakeven_win_rate"] > eko_bedava["breakeven_win_rate"], \
    "komisyon başabaş eşiğini yükseltmeli"
# Komisyon, kazanç-kayıp makasını iki yönden birden daraltır
beklenen_fark = 2 * config.ALPACA_FEE_RATE * 100 * (1 + config.TAKE_PROFIT_PCT)
assert abs((config.TAKE_PROFIT_PCT * 100 - eko["net_win_pct"]) - beklenen_fark) < 0.01, \
    (config.TAKE_PROFIT_PCT * 100 - eko["net_win_pct"], beklenen_fark)
print(f"✓ Alpaca komisyonuyla: kazanç {eko['net_win_pct']:+.2f}% / "
      f"kayıp {-eko['net_loss_pct']:.2f}% -> başabaş %{eko['breakeven_win_rate']:.1f}")

# Dahili defter daha ucuz olduğu için eşiği de düşük — ikisi karıştırılmamalı
eko_dahili = config.trade_economics(config.FEE_RATE)
assert eko_dahili["breakeven_win_rate"] < eko["breakeven_win_rate"], \
    "dahili defterin eşiği Alpaca'nınkinden düşük olmalı"
assert "Alpaca" in config.summary()["Komisyon"], \
    "panel yalnızca dahili oranı göstermemeli (yanıltıcı)"
print(f"✓ iki arka ucun eşiği ayrı: dahili %{eko_dahili['breakeven_win_rate']:.1f} vs "
      f"Alpaca %{eko['breakeven_win_rate']:.1f}")

print("\nUYGULAMA KATMANI TESTLERİ GEÇTİ ✅")
