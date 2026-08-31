"""
Alpaca emir yürütme testleri.

API ANAHTARI GEREKTİRMEZ: TradingClient sahte bir sınıfla değiştirilir.
Test edilen şey Alpaca'nın kendisi değil, bizim ona gönderdiğimiz emrin
doğruluğu — sembol dönüşümü, tutar, bracket kuralı, veritabanı kaydı,
kurul kararıyla eşleşme ve hata yolları.

Çalıştırma:
    python tests/test_alpaca.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_alpaca.db")
os.environ["OFFLINE_SIMULATION"] = "true"
for _s in ("", "-wal", "-shm"):
    if os.path.exists(os.environ["DB_PATH"] + _s):
        os.remove(os.environ["DB_PATH"] + _s)

import config
import database as db
import alpaca_execution as ax

db.init_db()


# --- Sahte Alpaca istemcisi ---------------------------------------------------
class FakeOrder:
    def __init__(self, oid="ord-1", status="accepted", qty=None, price=None):
        self.id, self.status = oid, status
        self.filled_qty, self.filled_avg_price = qty, price


class FakePosition:
    def __init__(self, symbol, qty=0.5, entry=60000.0, cur=61000.0):
        self.symbol, self.asset_class, self.side = symbol, "crypto", "long"
        self.qty, self.avg_entry_price, self.current_price = qty, entry, cur
        self.market_value = qty * cur
        self.cost_basis = qty * entry
        self.unrealized_pl = self.market_value - self.cost_basis
        self.unrealized_plpc = (cur / entry - 1)


class FakeAccount:
    status, currency = "ACTIVE", "USD"
    equity, cash, buying_power = "100000", "40000", "80000"
    portfolio_value, last_equity = "100000", "98000"


class FakeClient:
    """TradingClient'ın kullandığımız yüzeyini taklit eder."""
    def __init__(self, positions=None, fail=None):
        self.submitted, self.closed = [], []
        self._positions = positions or []
        self.fail = fail
    def get_account(self): return FakeAccount()
    def get_all_positions(self): return list(self._positions)
    def submit_order(self, order_data):
        if self.fail:
            raise RuntimeError(self.fail)
        self.submitted.append(order_data)
        return FakeOrder(status="accepted", qty=0.04, price=65010.0)
    def close_position(self, symbol):
        if self.fail:
            raise RuntimeError(self.fail)
        self.closed.append(symbol)
        return FakeOrder(oid="ord-close", status="accepted")


def executor(client) -> ax.AlpacaExecutor:
    ex = ax.AlpacaExecutor()
    ex._client = client
    return ex


# --- 1) Sembol dönüşümü -------------------------------------------------------
assert config.alpaca_symbol("BTC/USDT") == "BTC/USD"
assert config.alpaca_symbol("ETH/USDT") == "ETH/USD"
assert config.alpaca_symbol("NVDA") == "NVDA"
assert config.is_crypto("BTC/USDT") and not config.is_crypto("NVDA")
print("✓ sembol dönüşümü: BTC/USDT -> BTC/USD, NVDA -> NVDA")

# --- 2) Arka uç seçimi --------------------------------------------------------
_k, _s, _b = config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.EXECUTION_BACKEND
config.ALPACA_API_KEY = config.ALPACA_SECRET_KEY = ""
config.EXECUTION_BACKEND = "auto"
assert ax.backend_name() == "internal", "anahtar yokken dahili defter kullanılmalı"
config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY = "k", "s"
assert ax.backend_name() == "alpaca", "anahtar varken Alpaca seçilmeli"
config.EXECUTION_BACKEND = "internal"
assert ax.backend_name() == "internal", "açık ayar otomatik seçimi ezmeli"
config.EXECUTION_BACKEND = "auto"
print("✓ yürütme arkası seçimi (auto / internal / alpaca)")

# --- 3) Hesap ve pozisyon okuma ----------------------------------------------
ex = executor(FakeClient(positions=[FakePosition("BTC/USD")]))
acc = ex.account()
assert acc["equity"] == 100000.0 and acc["buying_power"] == 80000.0
assert abs(acc["day_pnl"] - 2000.0) < 1e-9 and abs(acc["day_pnl_pct"] - 2.0408) < 1e-3
pos = ex.positions()[0]
assert pos["symbol"] == "BTC/USD" and abs(pos["unrealized_plpc"] - 1.6667) < 1e-3
assert ex.position_for("BTC/USDT") is not None, "bizim sembolden Alpaca pozisyonu bulunmalı"
assert ex.position_for("ETH/USDT") is None
print(f"✓ hesap okundu: equity {acc['equity']:,.0f}, günlük K/Z {acc['day_pnl']:+,.0f} "
      f"({acc['day_pnl_pct']:+.2f}%)")

# --- 4) Emir tutarı hesabı ---------------------------------------------------
config.ALPACA_POSITION_PCT, config.ALPACA_MAX_NOTIONAL = 0.25, 5000.0
assert ex.position_notional(1.0) == 5000.0, "üst sınır uygulanmalı"
config.ALPACA_MAX_NOTIONAL = 50000.0
assert ex.position_notional(1.0) == 25000.0        # 100k * %25
assert ex.position_notional(0.5) == 12500.0        # Overweight -> yarım
config.ALPACA_MAX_NOTIONAL = 5000.0
print("✓ emir tutarı: yüzde, üst sınır ve büyüklük çarpanı")

# --- 5) KRİPTO alım: bracket YOK, seviyeler veritabanında --------------------
client = FakeClient()
ex = executor(client)
res = ex.buy("BTC/USDT", 2500.0, take_profit=66300.0, stop_loss=63000.0, agent_run_id=42)
assert res["ok"], res
req = client.submitted[0]
assert req.symbol == "BTC/USD" and float(req.notional) == 2500.0
assert req.order_class is None or str(req.order_class) == "OrderClass.SIMPLE", \
    f"kriptoda bracket gönderilmemeli: {req.order_class}"
assert req.take_profit is None and req.stop_loss is None
order = db.get_broker_orders(1)[0]
assert order["broker_symbol"] == "BTC/USD" and order["agent_run_id"] == 42
assert order["take_profit"] == 66300.0 and order["stop_loss"] == 63000.0
assert order["filled_avg_price"] == 65010.0 and order["is_paper"] == 1
print("✓ kripto alımı: bracket gönderilmiyor, TP/SL veritabanına yazılıyor")

# --- 6) HİSSE alım: bracket VAR ----------------------------------------------
client2 = FakeClient()
ex2 = executor(client2)
res2 = ex2.buy("NVDA", 3000.0, take_profit=190.0, stop_loss=175.0, agent_run_id=43)
assert res2["ok"]
req2 = client2.submitted[0]
assert str(req2.order_class) in ("OrderClass.BRACKET", "bracket"), req2.order_class
assert req2.take_profit.limit_price == 190.0 and req2.stop_loss.stop_price == 175.0
print("✓ hisse alımı: kâr al + stop bracket emri olarak borsaya gidiyor")

# --- 7) Çok küçük tutar reddediliyor -----------------------------------------
before = len(db.get_broker_orders(100))
res3 = ex.buy("BTC/USDT", 3.0)
assert not res3["ok"] and "küçük" in res3["error"]
assert len(db.get_broker_orders(100)) == before, "reddedilen tutar için emir kaydı açılmamalı"
print("✓ minimum tutar altındaki emir gönderilmiyor")

# --- 8) API hatası: kayıt altına alınıyor, sistem çökmüyor -------------------
ex_fail = executor(FakeClient(fail="APIError: insufficient buying power"))
res4 = ex_fail.buy("BTC/USDT", 2500.0, agent_run_id=44)
assert not res4["ok"] and "insufficient" in res4["error"]
failed = db.get_broker_orders(1)[0]
assert failed["status"] == "error" and "insufficient" in failed["error"]
print("✓ Alpaca hatası kaydediliyor, süreç devam ediyor")

# --- 9) Satış: close_position çağrılıyor -------------------------------------
client5 = FakeClient(positions=[FakePosition("BTC/USD")])
ex5 = executor(client5)
res5 = ex5.sell("BTC/USDT", agent_run_id=45, reason="Kurul kararı: Sell")
assert res5["ok"] and client5.closed == ["BTC/USD"]
sell_row = db.get_broker_orders(1)[0]
assert sell_row["side"] == "sell" and sell_row["agent_run_id"] == 45
assert ex5.sell("XRP/USDT")["ok"] is False, "pozisyon yokken satış denenmemeli"
print("✓ satış emri: close_position çağrılıyor ve kaydediliyor")

# --- 10) Kripto koruması (TP/SL bot tarafında) -------------------------------
for _s2 in ("", "-wal", "-shm"):
    pass
db.init_db()
oid = db.record_broker_order("ETH/USDT", "buy", broker_symbol="ETH/USD", notional=1000.0,
                             take_profit=3300.0, stop_loss=3100.0, status="filled")
assert ex.check_protective_exit("ETH/USDT", 3200.0) is None, "seviyeler arasında çıkış olmamalı"
assert "KÂR AL" in ex.check_protective_exit("ETH/USDT", 3301.0)
assert ex.check_protective_exit("ETH/USDT", 3099.0) == "STOP-LOSS"
assert ex.check_protective_exit("NVDA", 1.0) is None, "hissede koruma borsada (bracket)"
db.record_broker_order("ETH/USDT", "sell", broker_symbol="ETH/USD", status="filled")
assert ex.check_protective_exit("ETH/USDT", 3301.0) is None, "satıştan sonra koruma aranmamalı"
print("✓ kripto kâr al/stop koruması bot tarafında çalışıyor")

# --- 11) Bot entegrasyonu: kurul kararı Alpaca'ya gidiyor --------------------
config.EXECUTION_BACKEND = "alpaca"
from bot import TradingBot
bot = TradingBot()
assert bot.backend == "alpaca", bot.backend
client6 = FakeClient()
bot.broker = executor(client6)

run_id = db.start_agent_run("BTC/USDT", 65000.0)
db.finish_agent_run(run_id, status="OK", rating="Buy", action="BUY",
                    size_factor=1.0, proposed_stop=63000.0)
bot.apply_pending_decisions("BTC/USDT", 65000.0)
assert client6.submitted, "kurul AL kararı Alpaca'ya emir olarak gitmeliydi"
sent = client6.submitted[0]
assert sent.symbol == "BTC/USD"
assert not db.get_open_positions(), "Alpaca modunda dahili deftere pozisyon yazılmamalı"
linked = db.get_broker_orders(1)[0]
assert linked["agent_run_id"] == run_id, "emir kurul kararına bağlanmalı"
assert linked["stop_loss"] == 63000.0, "ajanın stop önerisi emre işlenmeli"
assert db.get_agent_runs(1, "BTC/USDT")[0]["executed"] == 1
print(f"✓ kurul kararı -> Alpaca emri (kurul #{run_id} ile eşleşmiş)")

# SAT kararı pozisyonu kapatmalı
client7 = FakeClient(positions=[FakePosition("BTC/USD")])
bot.broker = executor(client7)
run2 = db.start_agent_run("BTC/USDT", 66000.0)
db.finish_agent_run(run2, status="OK", rating="Sell", action="SELL", size_factor=0.0)
bot.apply_pending_decisions("BTC/USDT", 66000.0)
assert client7.closed == ["BTC/USD"], "SAT kararı Alpaca pozisyonunu kapatmalıydı"
print("✓ kurul SAT kararı Alpaca pozisyonunu kapatıyor")

# Alpaca okunamıyorsa yeni emir GÖNDERİLMEMELİ (emin değilsek durma)
class BlindClient(FakeClient):
    def get_all_positions(self): raise RuntimeError("network down")
bot.broker = executor(BlindClient())
assert bot.has_position("BTC/USDT") is True, "pozisyon okunamıyorsa 'var' sayılmalı"
print("✓ pozisyon okunamadığında yeni emir gönderilmiyor (güvenli taraf)")

config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.EXECUTION_BACKEND = _k, _s, _b
print("\nALPACA ENTEGRASYON TESTLERİ GEÇTİ ✅")
