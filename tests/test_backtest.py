"""
Backtest motoru testleri.

Çalıştırma:
    python tests/test_backtest.py

Ağ erişimi gerektirmez: veri elle kurulur ya da sentetik üretilir.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_backtest.db")
os.environ["OFFLINE_SIMULATION"] = "true"

import numpy as np
import pandas as pd

import config
import backtest as bt
from backtest import Params, prepare, run_backtest


def frame(closes, highs=None, lows=None, start="2025-01-01"):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({
        "timestamp": (idx.astype("int64") // 10**6),
        "open": np.concatenate([[c[0]], c[:-1]]),
        "high": c if highs is None else np.array(highs, float),
        "low": c if lows is None else np.array(lows, float),
        "close": c,
        "volume": np.ones(n),
    }, index=idx)


def net_pct(move: float, fee: float) -> float:
    """Komisyon dahil, bir işlemin cebe giren yüzdesi (canlı botla aynı formül)."""
    budget = 1000.0
    amount = (budget - budget * fee) / 100.0
    return (amount * 100.0 * (1 + move) * (1 - fee) - budget) / budget * 100.0


# --- 1) İleriye bakma yok: gösterge değerleri kaydırılmış olmalı --------------
closes = list(np.linspace(100, 300, 96 * 30)) + list(np.linspace(300, 262, 12))
df = frame(closes)
prep = prepare(df, Params())
from bot import calculate_rsi
raw_rsi = calculate_rsi(df["close"], config.RSI_PERIOD)
assert np.isclose(prep["rsi"].iloc[-1], raw_rsi.iloc[-2]), "RSI bir mum kaydırılmalı"
# Günlük EMA, o günün kendi verisini içermemeli
day_last = df["close"].resample("1D").last().dropna()
assert prep["ema"].iloc[-1] < day_last.iloc[-2], "trend EMA'sı önceki güne ait olmalı"
print("✓ ileriye bakma yok (RSI ve trend EMA'sı kaydırılmış)")

# --- 2-4) Muhasebe testleri --------------------------------------------------
# Gerçekçi RSI dinamiğinden bağımsız, tek bir işlemi izole etmek için:
#   rsi_buy=101      -> RSI koşulu her zaman sağlanır
#   rsi_sell=1e9     -> RSI ile çıkış hiç olmaz
#   cooldown_bars=∞  -> tüm seride yalnızca bir giriş
# Böylece çıkışı sadece TP/SL seviyeleri belirler ve rakamlar formülle
# birebir karşılaştırılabilir.
ISOLATE = dict(rsi_buy=101.0, rsi_sell=1e9, cooldown_bars=10**9)

rise = list(np.linspace(100, 300, 96 * 40))

# 2) Kâr al: SL erişilemez uzaklıkta
p_tp = Params(take_profit=0.02, stop_loss=0.90, **ISOLATE)
res = run_backtest({"BTC/USDT": frame(rise, highs=list(np.array(rise) * 1.03),
                                      lows=list(np.array(rise) * 0.995))}, p_tp)
assert len(res.trades) == 1, f"tam 1 işlem bekleniyordu, {len(res.trades)}"
tr = res.trades.iloc[0]
assert tr["reason"] == "KÂR AL", tr["reason"]
expected = net_pct(p_tp.take_profit, p_tp.fee)
assert np.isclose(tr["pnl_pct"], expected, atol=1e-9), f"{tr['pnl_pct']} != {expected}"
assert np.isclose(tr["exit_price"], tr["entry_price"] * (1 + p_tp.take_profit))
print(f"✓ kâr al muhasebesi: %{tr['pnl_pct']:+.4f} (formülle birebir)")

# 3) Stop-loss: TP erişilemez uzaklıkta
p_sl = Params(take_profit=10.0, stop_loss=0.015, **ISOLATE)
res3 = run_backtest({"BTC/USDT": frame(rise, highs=list(np.array(rise) * 1.005),
                                       lows=list(np.array(rise) * 0.97))}, p_sl)
assert len(res3.trades) == 1
tr3 = res3.trades.iloc[0]
assert tr3["reason"] == "STOP-LOSS", tr3["reason"]
expected_sl = net_pct(-p_sl.stop_loss, p_sl.fee)
assert np.isclose(tr3["pnl_pct"], expected_sl, atol=1e-9), f"{tr3['pnl_pct']} != {expected_sl}"
print(f"✓ stop-loss muhasebesi: %{tr3['pnl_pct']:+.4f} (formülle birebir)")

# 4) Aynı mumda hem TP hem SL -> kötümser varsayım: önce stop
p_both = Params(take_profit=0.02, stop_loss=0.015, **ISOLATE)
res4 = run_backtest({"BTC/USDT": frame(rise, highs=list(np.array(rise) * 1.05),
                                       lows=list(np.array(rise) * 0.95))}, p_both)
assert res4.trades.iloc[0]["reason"] == "STOP-LOSS", "belirsizlikte stop varsayılmalı"
print("✓ aynı mumda TP+SL -> kötümser (stop) varsayımı")

# --- 5) Trend filtresi: fiyat günlük EMA'nın altındayken alım yok -------------
p = Params()          # buradan itibaren gerçek strateji ayarları
down = list(np.linspace(300, 100, 96 * 30)) + list(np.linspace(100, 88, 12))
res5 = run_backtest({"BTC/USDT": frame(down)}, p)
assert len(res5.trades) == 0, "düşüş trendinde işlem açılmamalı"
print("✓ trend filtresi düşüş trendinde alımı engelliyor")

# --- 6) Sentetik uzun seride yapısal kısıtlar ---------------------------------
for s in config.SYMBOLS:
    bt.make_demo_history(s, config.TIMEFRAME, days=180, seed=11)
data = {s: bt.load_history(s, config.TIMEFRAME) for s in config.SYMBOLS}
res6 = run_backtest(data, p)
t = res6.trades
assert len(t) > 20, f"anlamlı bir örneklem bekleniyordu, {len(t)} işlem"
assert (res6.equity >= 0).all(), "equity negatife düşmemeli"
for s in config.SYMBOLS:
    ts = t[t.symbol == s].sort_values("opened_at")
    overlap = int((ts.opened_at.values[1:] < ts.closed_at.values[:-1]).sum())
    assert overlap == 0, f"{s}: {overlap} çakışan pozisyon"
tp_rows = t[t.reason == "KÂR AL"]
sl_rows = t[t.reason == "STOP-LOSS"]
assert np.allclose(tp_rows.pnl_pct, net_pct(p.take_profit, p.fee), atol=1e-6)
assert np.allclose(sl_rows.pnl_pct, net_pct(-p.stop_loss, p.fee), atol=1e-6)
assert np.isclose(res6.stats["final_balance"], res6.equity.iloc[-1])
print(f"✓ {len(t)} işlemlik seride kısıtlar ve muhasebe tutarlı")

# --- 7) Rastgele yürüyüşte sonuç başa baş civarında olmalı --------------------
wr = res6.stats["win_rate"]
assert 30 < wr < 70, f"rastgele veride kazanma oranı {wr:.1f}% — motor şüpheli"
print(f"✓ rastgele veride kazanma oranı %{wr:.1f} (motor kâr uydurmuyor)")

# --- 8) Maks. açık pozisyon sınırı -------------------------------------------
p1 = Params(max_open=1)
res8 = run_backtest(data, p1)
t8 = res8.trades.sort_values("opened_at")
concurrent_ok = True
for i in range(1, len(t8)):
    if t8.iloc[i]["opened_at"] < t8.iloc[i - 1]["closed_at"]:
        concurrent_ok = False
        break
assert concurrent_ok, "max_open=1 iken iki pozisyon aynı anda açık kalmış"
print("✓ maksimum açık pozisyon sınırı uygulanıyor")

# --- 9) İndirme mantığı: sayfalama, tekrar temizleme, dosyaya ekleme ----------
class FakeExchange:
    rateLimit = 0
    def __init__(self, bars): self.bars, self.calls = bars, 0
    def milliseconds(self): return int(self.bars[-1][0]) + 900_000
    def parse_timeframe(self, tf): return 900
    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=1000):
        self.calls += 1
        return [b for b in self.bars if b[0] >= since][:limit]

base_ms = 1_700_000_000_000
bars = [[base_ms + i * 900_000, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(2500)]
fake = FakeExchange(bars)
bt._exchange = lambda: fake
path = bt.history_path("TEST/USDT", "15m")
if os.path.exists(path):
    os.remove(path)
out = bt.download_history("TEST/USDT", "15m", days=30)
assert len(out) == 2500, f"2500 mum bekleniyordu, {len(out)}"
assert out["timestamp"].is_monotonic_increasing and out["timestamp"].is_unique
assert fake.calls >= 3, f"sayfalama çalışmadı (limit 1000, {fake.calls} istek)"
again = bt.download_history("TEST/USDT", "15m", days=30)   # ikinci kez: tekrar yazmamalı
assert len(again) == 2500, f"tekrarlı indirmede satır çoğaldı: {len(again)}"
os.remove(path)
print(f"✓ indirme: {fake.calls} sayfa, tekrarsız, ikinci koşuda çoğalmıyor")

print("\nBACKTEST TESTLERİ GEÇTİ ✅")
