"""
Strateji, PnL hesabı ve veritabanı için uçtan uca testler.

Çalıştırma:
    python tests/test_strategy.py

Ağ erişimi gerektirmez; piyasa verisi deterministik sahte serilerle beslenir.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_strategy.db")
os.environ["OFFLINE_SIMULATION"] = "true"
os.environ["COOLDOWN_MINUTES"] = "0"

for _suffix in ("", "-wal", "-shm"):
    _p = os.environ["DB_PATH"] + _suffix
    if os.path.exists(_p):
        os.remove(_p)

import numpy as np, pandas as pd
import config, database as db
from bot import TradingBot, calculate_rsi, calculate_ema

db.init_db()
bot = TradingBot()

def frame(closes):
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(closes), freq="15min")
    return pd.DataFrame({"timestamp": idx.astype("int64")//10**6, "open": closes,
                         "high": closes, "low": closes, "close": closes,
                         "volume": [1.0]*len(closes), "datetime": idx})

def feed(signal_closes, daily_closes=None):
    """15m serisini ve (varsa) günlük trend serisini bota bağla."""
    def _fetch(symbol, timeframe=None, limit=None):
        if timeframe and timeframe != config.TIMEFRAME:
            return frame(daily_closes if daily_closes is not None else signal_closes)
        return frame(signal_closes)
    bot.market.fetch_ohlcv = _fetch
    bot._trend_cache.clear()

# --- 1) İndikatör doğruluğu ---
assert calculate_rsi(pd.Series(np.arange(1, 60, dtype=float)), 14).iloc[-1] == 100.0
assert calculate_rsi(pd.Series(np.arange(60, 1, -1, dtype=float)), 14).iloc[-1] == 0.0
assert abs(calculate_ema(pd.Series([100.0]*40), 20).iloc[-1] - 100.0) < 1e-9
print("✓ RSI/EMA hesapları")

# --- 2) Çok zaman dilimli giriş: 15m RSI dipte, günlük trend yukarı ---
closes = list(np.linspace(100, 160, 80)) + [159, 156, 152, 148, 143, 138, 133, 130]
daily  = list(np.linspace(60, 120, 120))          # 20 günlük EMA fiyatın çok altında
sig = frame(closes); sig["rsi"] = calculate_rsi(sig["close"], 14)
print("  15m RSI = %.1f | günlük EMA20 = %.2f | fiyat = %.2f"
      % (sig["rsi"].iloc[-2], calculate_ema(pd.Series(daily), 20).iloc[-2], closes[-1]))
assert sig["rsi"].iloc[-2] < 30

feed(closes, daily)
bot.process_symbol("BTC/USDT")
pos = db.get_open_positions("BTC/USDT")
assert pos, "AL sinyali pozisyon açmalıydı"
p = pos[0]
print("✓ pozisyon açıldı: %.6f @ %.2f | maliyet %.2f | TP %.2f | SL %.2f"
      % (p["amount"], p["entry_price"], p["cost"], p["take_profit"], p["stop_loss"]))
assert abs(p["cost"] - 2500.0) < 1e-6, "pozisyon %25 olmalı"
assert abs(db.get_balance() - 7500.0) < 1e-6, "maliyet bakiyeden düşmeli"
assert abs(p["take_profit"] / p["entry_price"] - 1.02) < 1e-9
assert abs(p["stop_loss"] / p["entry_price"] - 0.985) < 1e-9

# --- 3) Trend filtresi çalışıyor mu: günlük EMA fiyatın ÜSTÜNDEyken alım olmamalı ---
db.reset_account()
feed(closes, list(np.linspace(400, 300, 120)))    # düşen ve fiyatın çok üstünde EMA
bot.process_symbol("ETH/USDT")
assert not db.get_open_positions("ETH/USDT"), "düşüş trendinde alım yapılmamalıydı"
print("✓ trend filtresi düşüş trendinde alımı engelledi")

# --- 4) Aynı sembolde çift pozisyon yok ---
db.reset_account(); feed(closes, daily)
bot.process_symbol("BTC/USDT"); bot.process_symbol("BTC/USDT")
assert len(db.get_open_positions("BTC/USDT")) == 1
p = db.get_open_positions("BTC/USDT")[0]
print("✓ çift pozisyon engellendi")

# --- 5) KÂR AL ---
feed(closes + [p["take_profit"] * 1.002], daily)
bot.process_symbol("BTC/USDT")
assert not db.get_open_positions("BTC/USDT")
t = db.get_trades(1)[0]
expected = p["amount"] * (p["take_profit"]*1.002) * (1-config.FEE_RATE) - p["cost"]
assert abs(t["pnl"] - expected) < 1e-6, f"PnL hatalı: {t['pnl']} != {expected}"
assert t["pnl"] > 0 and "KÂR AL" in t["exit_reason"]
assert abs(db.get_balance() - t["balance_after"]) < 1e-9
print("✓ kâr al: PnL %+.2f (%%%+.2f) | bakiye %.2f" % (t["pnl"], t["pnl_pct"], t["balance_after"]))

# --- 6) STOP-LOSS ---
db.reset_account(); feed(closes, daily)
bot.process_symbol("ETH/USDT")
p2 = db.get_open_positions("ETH/USDT")[0]
feed(closes + [p2["stop_loss"] * 0.99], daily)
bot.process_symbol("ETH/USDT")
t2 = db.get_trades(1)[0]
assert t2["pnl"] < 0 and "STOP" in t2["exit_reason"], t2
print("✓ stop-loss: PnL %+.2f (%%%+.2f)" % (t2["pnl"], t2["pnl_pct"]))

# --- 7) RSI > 70 ile çıkış (TP/SL'ye değmeden) ---
db.reset_account()
config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT = 0.90, 0.90   # TP/SL devre dışı kalsın
feed(closes, daily)
bot.process_symbol("BTC/USDT")
assert db.get_open_positions("BTC/USDT")
rsi_up = closes + list(np.linspace(closes[-1], closes[-1]*1.35, 30))
r = calculate_rsi(frame(rsi_up)["close"], 14).iloc[-2]
assert r > 70, r
feed(rsi_up, daily)
bot.process_symbol("BTC/USDT")
t3 = db.get_trades(1)[0]
assert "RSI" in t3["exit_reason"], t3["exit_reason"]
print("✓ RSI %.1f > 70 ile çıkış: %s" % (r, t3["exit_reason"]))
config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT = 0.02, 0.015

# --- 8) Maksimum açık pozisyon limiti ---
db.reset_account(); feed(closes, daily)
bot.process_symbol("BTC/USDT"); bot.process_symbol("ETH/USDT")
assert len(db.get_open_positions()) == 2
config.MAX_OPEN_POSITIONS = 1
assert not bot.check_entry("XRP/USDT", 1.0, 10.0, 0.5), "limit aşılmamalı"
config.MAX_OPEN_POSITIONS = 2
print("✓ maksimum pozisyon limiti")

# --- 9) Cooldown ---
config.COOLDOWN_MINUTES = 60
db.close_position(db.get_open_positions("BTC/USDT")[0]["id"], 150.0, "test")
assert bot._in_cooldown("BTC/USDT") is True
config.COOLDOWN_MINUTES = 0
assert bot._in_cooldown("BTC/USDT") is False
print("✓ cooldown")

# --- 10) İstatistikler / equity / sıfırlama ---
bot.snapshot_equity(force=True)
stats = db.get_stats({"BTC/USDT": 160.0, "ETH/USDT": 160.0})
assert stats["total_trades"] == 1
assert abs(stats["equity"] - (stats["balance"] + stats["open_value"])) < 1e-9
assert len(db.get_equity_curve()) >= 1
print("✓ istatistik: equity %.2f | açık %d | win rate %%%.0f"
      % (stats["equity"], stats["open_positions"], stats["win_rate"]))
db.reset_account()
assert db.get_balance() == 10000.0 and not db.get_trades() and not db.get_open_positions()
print("✓ sıfırlama")

# --- 11) Yetersiz bakiye ---
db.update_balance(5.0); feed(closes, daily)
bot.process_symbol("BTC/USDT")
assert not db.get_open_positions()
print("✓ yetersiz bakiye koruması")

# --- 12) Bot başlat/durdur bayrağı ---
db.set_bot_running(True);  assert db.is_bot_running()
db.set_bot_running(False); assert not db.is_bot_running()
print("✓ başlat/durdur bayrağı")

print("\nTÜM TESTLER GEÇTİ ✅")
