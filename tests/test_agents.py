"""
Yapay zekâ kurulu (TradingAgents) entegrasyon testleri.

Çalıştırma:
    python tests/test_agents.py

LLM anahtarı GEREKTİRMEZ: kurul sahte bir grafikle taklit edilir. Test edilen
şey ajanların zekâsı değil, bizim boru hattımız — kararın emre çevrilmesi,
raporların kaydı, maliyet sınırları ve hata yolları.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_agents.db")
os.environ["OFFLINE_SIMULATION"] = "true"
for _s in ("", "-wal", "-shm"):
    if os.path.exists(os.environ["DB_PATH"] + _s):
        os.remove(os.environ["DB_PATH"] + _s)

import config
import database as db
import agents_engine as ae
from bot import TradingBot

db.init_db()

# --- 1) Not -> karar eşlemesi -------------------------------------------------
cases = {"Buy": ("BUY", 1.0), "Overweight": ("BUY", 0.5), "Hold": ("HOLD", 0.0),
         "Underweight": ("SELL", 0.0), "Sell": ("SELL", 0.0), "REVIEW": ("HOLD", 0.0),
         "": ("HOLD", 0.0)}
for rating, expected in cases.items():
    assert ae.rating_to_action(rating) == expected, f"{rating} -> {ae.rating_to_action(rating)}"
print("✓ 5 kademeli not -> AL/SAT/BEKLE eşlemesi")

# --- 2) Rapor çıkarımı --------------------------------------------------------
state = {
    "market_report": "teknik", "sentiment_report": "duygu", "news_report": "haber",
    "fundamentals_report": "temel", "investment_plan": "plan",
    "trader_investment_plan": "Giriş 100, stop-loss 97.5", "final_trade_decision": "Rating: Buy",
    "investment_debate_state": {"bull_history": "boğa", "bear_history": "ayı",
                                "judge_decision": "hüküm", "count": 2},
    "risk_debate_state": {"aggressive_history": "agresif", "conservative_history": "muhafazakâr",
                          "neutral_history": "nötr", "judge_decision": "onay"},
}
rep = ae.extract_reports(state)
for key in ("market_report", "sentiment_report", "news_report", "fundamentals_report",
            "final_trade_decision", "investment_debate", "risk_debate"):
    assert key in rep, f"{key} rapora girmedi"
assert rep["investment_debate"]["judge_decision"] == "hüküm"
assert "count" not in rep["investment_debate"], "sayısal alan metin raporuna sızmamalı"
assert rep["risk_debate"]["judge_decision"] == "onay"
print("✓ ajan raporları ve tartışmalar eksiksiz çıkarılıyor")

# --- 3) Stop-loss çıkarımı ve akıl sağlığı sınırları --------------------------
assert abs(ae.extract_stop_price(state, 100.0) - 97.5) < 1e-9
assert ae.extract_stop_price({"trader_investment_plan": "stop-loss 10"}, 100.0) is None, \
    "fiyatın %90 altındaki stop reddedilmeli"
assert ae.extract_stop_price({"trader_investment_plan": "stop-loss 99.9"}, 100.0) is None, \
    "fiyata çok yakın stop reddedilmeli"
assert ae.extract_stop_price({}, 100.0) is None
class _Proposal:
    stop_loss = 96.0
assert ae.extract_stop_price({"trader_proposal": _Proposal()}, 100.0) == 96.0, \
    "yapılandırılmış alan metinden önce gelmeli"
print("✓ stop-loss çıkarımı (yapılandırılmış + metin) ve sınır kontrolü")

# --- 3b) Stop metinden çıkarma: yüzde, mutlak, Türkçe/İngilizce sayı ---------
_P = 78816.0
_stop_cases = [
    ("stop-loss at 2%", _P * 0.98),                    # İngilizce yüzde
    ("Stop-Loss: %2 (girişin altında)", _P * 0.98),    # Türkçe yüzde, işaret önde
    ("%2 stop-loss uygulanmalı", _P * 0.98),
    ("stop-loss of 1.5% below entry", _P * 0.985),
    ("stop-loss: 76,100", 76100.0),                    # İngilizce binlik
    ("stop loss at $77,200.50", 77200.50),
    ("stop-loss seviyesi 76.100", 76100.0),            # Türkçe binlik
    ("stop-loss 76.100,50", 76100.50),                 # Türkçe binlik + ondalık
    ("stop-loss 77000", 77000.0),
    ("stop yok", None),
]
for _text, _expect in _stop_cases:
    _got = ae._stop_from_text(_text, _P)
    if _expect is None:
        assert _got is None, f"{_text!r} -> {_got}"
    else:
        assert _got is not None and abs(_got - _expect) < 0.02, \
            f"{_text!r} -> {_got}, beklenen {_expect}"
print("✓ stop metni: yüzde ve mutlak fiyat, Türkçe/İngilizce sayı biçimleri")

# Yüzdeyi fiyat sanma hatası tekrarlamasın (canlıda görüldü: stop 2.00 $ oldu)
assert ae.extract_stop_price(
    {"trader_investment_plan": "Giriş 78816, stop-loss %2, hedef %5"}, _P) is not None, \
    "yüzde ifadesi geçerli bir stop fiyatına çevrilmeli"
assert ae.extract_stop_price({"trader_investment_plan": "stop-loss 2"}, _P) is None, \
    "fiyatın çok altındaki mutlak değer hâlâ reddedilmeli"
print("✓ yüzde ifadesi fiyata çevriliyor, saçma mutlak değer reddediliyor")

# --- 4) Sahte kurul: karar -> emir ------------------------------------------
class FakeGraph:
    """propagate() imzasını taklit eder; LLM çağrısı yapmaz."""
    def __init__(self, signal, state): self.signal, self.state, self.calls = signal, state, 0
    def propagate(self, ticker, date, asset_type="stock"):
        self.calls += 1
        assert asset_type == "crypto", "kripto boru hattı seçilmeli"
        assert ticker == "BTCUSD", f"ticker dönüşümü hatalı: {ticker}"
        return self.state, self.signal

council = ae.AgentCouncil()


def install(graph):
    """
    Sahte grafiği kurula yerleştirir. Grafik sembol başına önbelleklendiği
    için testlerde geçen her sembole aynı sahteyi koyuyoruz.
    """
    from collections import OrderedDict
    council._graphs = OrderedDict(
        (s, graph) for s in ("BTC/USDT", "ETH/USDT", "XRP/USDT", "AAPL"))


install(FakeGraph("Buy", state))
res = council.analyze("BTC/USDT", price=100.0)
assert res["status"] == "OK" and res["action"] == "BUY" and res["size_factor"] == 1.0
assert abs(res["proposed_stop"] - 97.5) < 1e-9
saved = db.get_agent_run(res["run_id"])
assert saved["reports"]["market_report"] == "teknik"
assert saved["executed"] == 0, "karar henüz uygulanmamış olmalı"
print(f"✓ kurul koşusu kaydedildi (#{res['run_id']}, {res['rating']} -> {res['action']})")

bot = TradingBot()
bot.apply_pending_decisions("BTC/USDT", 100.0)
pos = db.get_open_positions("BTC/USDT")
assert len(pos) == 1, "AL kararı pozisyon açmalıydı"
assert abs(pos[0]["stop_loss"] - 97.5) < 1e-9, "ajanın stop'u kullanılmalı"
assert db.get_agent_run(res["run_id"])["executed"] == 1
print(f"✓ karar emre çevrildi: giriş {pos[0]['entry_price']:.2f}, stop {pos[0]['stop_loss']:.2f}")

# --- 5) SELL kararı pozisyonu kapatır ---------------------------------------
install(FakeGraph("Sell", {"final_trade_decision": "Rating: Sell"}))
res2 = council.analyze("BTC/USDT", price=105.0)
assert res2["action"] == "SELL"
bot.apply_pending_decisions("BTC/USDT", 105.0)
assert not db.get_open_positions("BTC/USDT"), "SAT kararı pozisyonu kapatmalıydı"
assert db.get_trades(1)[0]["exit_reason"].startswith("Kurul kararı")
print("✓ SAT kararı pozisyonu kapatıyor")

# --- 6) HOLD hiçbir şey yapmaz ----------------------------------------------
db.reset_account()
install(FakeGraph("Hold", {"final_trade_decision": "Rating: Hold"}))
council.analyze("ETH/USDT", price=3000.0)
bot.apply_pending_decisions("ETH/USDT", 3000.0)
assert not db.get_open_positions(), "BEKLE kararı işlem açmamalı"
print("✓ BEKLE kararı işlem açmıyor")

# --- 7) Hata ve zaman aşımı yolları -----------------------------------------
class BoomGraph:
    def propagate(self, *a, **k): raise RuntimeError("429 kota doldu")
install(BoomGraph())
res3 = council.analyze("BTC/USDT", price=100.0)
assert res3["status"] == "ERROR" and res3["action"] == "HOLD"
saved3 = db.get_agent_run(res3["run_id"])
assert "429" in (saved3["error"] or ""), saved3["error"]
bot.apply_pending_decisions("BTC/USDT", 100.0)
assert not db.get_open_positions(), "hatalı koşu işlem açmamalı"
print("✓ LLM hatası yakalanıyor, kaydediliyor ve işlem açmıyor")

import time as _t
class SlowGraph:
    def propagate(self, *a, **k):
        _t.sleep(5)
        return {}, "Buy"
_orig = config.AGENT_RUN_TIMEOUT_SECONDS
config.AGENT_RUN_TIMEOUT_SECONDS = 1
install(SlowGraph())
res4 = council.analyze("BTC/USDT", price=100.0)
assert res4["status"] == "TIMEOUT", res4
config.AGENT_RUN_TIMEOUT_SECONDS = _orig
print("✓ zaman aşımı yakalanıyor")

# --- 7b) Kalıcı hata kurulu durdurur, geçici hata durdurmaz -----------------
db.set_state("council_halted", "")

class QuotaGraph:
    def propagate(self, *a, **k):
        raise RuntimeError("APIStatusError: Error code: 402 - {'error': "
                           "{'message': 'This request requires more credits'}}")
install(QuotaGraph())
res_fatal = council.analyze("BTC/USDT", price=100.0)
assert res_fatal["status"] == "ERROR"
assert res_fatal.get("fatal"), "402 kalıcı hata olarak sınıflanmalı"
assert ae.AgentCouncil.halted(), "kredi bitince kurul durdurulmalı"
assert not ae.AgentCouncil.due("XRP/USDT"), "durdurulmuşken yeni toplantı olmamalı"
print(f"✓ kredi bitince kurul duruyor ({ae.AgentCouncil.halted()})")

ae.AgentCouncil.resume()
assert not ae.AgentCouncil.halted() and ae.AgentCouncil.due("XRP/USDT")
print("✓ --resume-council kurulu geri açıyor")

class FlakyGraph:
    def propagate(self, *a, **k):
        raise RuntimeError("RateLimitError: 429 Too Many Requests")
install(FlakyGraph())
council.analyze("BTC/USDT", price=100.0)
assert not ae.AgentCouncil.halted(), "geçici hata (429) kurulu durdurmamalı"
print("✓ geçici hata (429) kurulu durdurmuyor")

# --- 7c) Geçici hatadan sonra kısa sürede tekrar denenir ---------------------
import database as _db
_db.init_db()
with _db.get_connection() as _c:
    _c.execute("DELETE FROM agent_runs")

_iv0, _rt0 = config.AGENT_INTERVAL_MINUTES, config.AGENT_RETRY_MINUTES
config.AGENT_INTERVAL_MINUTES, config.AGENT_RETRY_MINUTES = 60, 0

rid = _db.start_agent_run("SOL/USDT", 100.0)
_db.finish_agent_run(rid, status="ERROR", duration_sec=218.0,
                     error="OpenAIRateLimitError: 429 rate-limited upstream")
assert ae.AgentCouncil.due("SOL/USDT"), \
    "geçici hatadan sonra kısa aralıkla tekrar denenmeli"

with _db.get_connection() as _c:
    _c.execute("DELETE FROM agent_runs")
rid2 = _db.start_agent_run("SOL/USDT", 100.0)
_db.finish_agent_run(rid2, status="OK", rating="Hold", action="HOLD", duration_sec=700.0)
assert not ae.AgentCouncil.due("SOL/USDT"), \
    "başarılı toplantıdan sonra tam süre beklenmeli"
config.AGENT_INTERVAL_MINUTES, config.AGENT_RETRY_MINUTES = _iv0, _rt0
print("✓ geçici hatada kısa, başarıda tam bekleme aralığı")

# Uzun sağlayıcı hatası tek satıra indirgeniyor
_long = ("OpenAIRateLimitError: Error code: 429 - {'error': {'metadata': "
         "{'raw': 'deepseek/deepseek-chat is temporarily rate-limited upstream', "
         "'limit_source': 'upstream_provider_shared_pool'}}}")
_short = ae.summarize_error(_long)
assert "havuz" in _short and len(_short) < 160, _short
print("✓ sağlayıcı hatası okunur özete indirgeniyor")

# --- 8) Maliyet sınırları ----------------------------------------------------
# 7c bloğu agent_runs'ı temizlediği için buradaki varsayımı yeniden kur:
# BTC/USDT için AZ ÖNCE başarılı bir toplantı yapılmış olsun.
_r = _db.start_agent_run("BTC/USDT", 100.0)
_db.finish_agent_run(_r, status="OK", rating="Hold", action="HOLD", duration_sec=10.0)

_iv = config.AGENT_INTERVAL_MINUTES
config.AGENT_INTERVAL_MINUTES = 60
assert not ae.AgentCouncil.due("BTC/USDT"), "az önce toplandı, tekrar toplanmamalı"
assert ae.AgentCouncil.due("XRP/USDT"), "hiç toplanmamış sembol için toplanmalı"
_cap = config.AGENT_MAX_RUNS_PER_DAY
config.AGENT_MAX_RUNS_PER_DAY = 1
assert not ae.AgentCouncil.due("XRP/USDT"), "günlük sınır aşılınca durmalı"
config.AGENT_MAX_RUNS_PER_DAY, config.AGENT_INTERVAL_MINUTES = _cap, _iv
print("✓ sıklık ve günlük maliyet sınırları uygulanıyor")

# --- 9) Anahtar yokken sistem çökmüyor --------------------------------------
_key = config.LLM_API_KEY
config.LLM_API_KEY = ""
ok, reason = ae.AgentCouncil.readiness()
assert not ok and "anahtar" in reason.lower()
bot2 = TradingBot()
bot2.maybe_convene("BTC/USDT", 100.0)      # sessizce atlamalı, patlamamalı
config.LLM_API_KEY = _key
print("✓ LLM anahtarı yokken bot çalışmaya devam ediyor")

# --- 10) Eski kural mantığı gerçekten kalktı mı? -----------------------------
import bot as bot_module
assert not hasattr(TradingBot, "check_entry"), "check_entry hâlâ duruyor"
import inspect
src = inspect.getsource(TradingBot.check_exit)
assert "RSI" not in src and "rsi" not in src, "check_exit hâlâ RSI kuralı içeriyor"
assert "RSI_BUY_THRESHOLD" not in inspect.getsource(bot_module), \
    "bot.py hâlâ RSI eşiğiyle karar veriyor"
print("✓ eski RSI/EMA al-sat kuralları bot.py'den kalkmış")

# --- 8) Yarıda kalan kayıt döngüde de süpürülmeli -------------------------
# Canlı sistemde görülen: süreç bir toplantının TAM ORTASINDA yeniden başladı,
# açılıştaki süpürme kaydı henüz bayat olmadığı için atladı ve kayıt 76
# dakika boyunca "RUNNING" kaldı.
import database as _db2
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_db2.init_db()
eski = (_dt.now(_tz.utc) - _td(seconds=config.AGENT_RUN_TIMEOUT_SECONDS + 600)
        ).strftime("%Y-%m-%d %H:%M:%S")
takılı = _db2.start_agent_run("DOT/USDT", 0.87)
with _db2.get_connection() as _c:
    _c.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", (eski, takılı))

assert _db2.get_agent_run(takılı)["status"] == "RUNNING"
# Takılı RUNNING kaydı üç yerde birden zarar veriyordu:
assert not ae.AgentCouncil.due("DOT/USDT"), \
    "takılı kayıt varken due() tam aralığı bekliyor (hatanın kendisi)"

bot2 = TradingBot()
bot2._last_sweep = 0.0          # açılış süpürmesinden hemen sonrasını taklit et
bot2.sweep_stale_runs()
kapandı = _db2.get_agent_run(takılı)
assert kapandı["status"] == "TIMEOUT", kapandı["status"]
assert ae.AgentCouncil.due("DOT/USDT"), \
    "süpürüldükten sonra TIMEOUT sayılıp kısa aralıkta tekrar denenmeli"
print("✓ döngüdeki süpürme takılı kaydı kapatıyor ve sembolü serbest bırakıyor")

# Süpürme kısılmalı: her turda (30 sn) UPDATE atmasın
çağrı = {"n": 0}
_orig_sweep = _db2.sweep_stale_agent_runs
_db2.sweep_stale_agent_runs = lambda *a, **k: (çağrı.__setitem__("n", çağrı["n"] + 1), 0)[1]
try:
    bot2._last_sweep = 0.0
    bot2.sweep_stale_runs()
    bot2.sweep_stale_runs()          # hemen ardından: atlanmalı
    assert çağrı["n"] == 1, f"süpürme kısılmamış: {çağrı['n']} çağrı"
finally:
    _db2.sweep_stale_agent_runs = _orig_sweep
print(f"✓ süpürme {TradingBot.SWEEP_INTERVAL_SECONDS} sn'de bir çalışıyor, her turda değil")

# --- 9) Elde olmayan coine SAT kararı sessizce yutulmamalı -----------------
_db2.reset_account()
run_sat = _db2.start_agent_run("XRP/USDT", 1.36)
_db2.finish_agent_run(run_sat, status="OK", rating="Underweight", action="SELL",
                      size_factor=0.0, duration_sec=246.0, reports={})
bot2.apply_pending_decisions("XRP/USDT", 1.36)
assert _db2.get_agent_run(run_sat)["executed"] == 1
kayıtlar = [l["message"] for l in _db2.get_logs(20) if "XRP/USDT" in (l["message"] or "")]
assert any("pozisyonumuz yok" in m for m in kayıtlar), kayıtlar
assert not _db2.get_open_positions("XRP/USDT"), "olmayan pozisyon kapatılamaz"
print("✓ pozisyon yokken SAT kararı ne emir açıyor ne de sessizce yutuluyor")

# --- 10) Kurul, BU TURUN verisiyle toplanmalı ------------------------------
# Hata: process_symbol önce maybe_convene() çağırıp SONRA update_market()
# yazıyordu; kurul bir önceki turun satırını "anlık fiyat" diye okuyordu.
# Aday listesine yeni giren bir coin için o satır saatler öncesine ait olur.
import inspect as _ins
_kaynak = _ins.getsource(TradingBot.process_symbol)
assert _kaynak.index("update_market") < _kaynak.index("maybe_convene"), \
    "piyasa satırı kurul toplanmadan ÖNCE yazılmalı"
print("✓ piyasa görüntüsü kurul toplanmadan önce yazılıyor")

# --- 11) Ardışık geçici hatada bekleme katlanmalı --------------------------
# Canlıda 5 ardışık 429 görüldü: her biri 10 dakikada tekrar deneniyor, hiçbiri
# sonuç vermiyor ama 60'lık GÜNLÜK toplantı kotasından yiyordu.
_db3 = _db2
_db3.init_db()
_sym = "BACKOFF/USDT"


def senaryo(ardışık_hata: int, son_koşu_dk_önce: float, önce_başarı: bool = False):
    """Sembolün geçmişini sıfırlayıp istenen durumu kurar (kayıtlar id sırasına
    göre okunuyor, o yüzden eskiden yeniye eklenir)."""
    with _db3.get_connection() as c:
        c.execute("DELETE FROM agent_runs WHERE symbol = ?", (_sym,))

    def yaz(durum, dk, **kw):
        rid = _db3.start_agent_run(_sym, 1.0)
        _db3.finish_agent_run(rid, status=durum, duration_sec=5.0, **kw)
        eski = (_dt.now(_tz.utc) - _td(minutes=dk)).strftime("%Y-%m-%d %H:%M:%S")
        with _db3.get_connection() as c:
            c.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", (eski, rid))

    if önce_başarı:
        yaz("OK", son_koşu_dk_önce + ardışık_hata + 5, rating="Hold", action="HOLD",
            size_factor=0.0, reports={})
    for i in range(ardışık_hata, 0, -1):
        yaz("ERROR", son_koşu_dk_önce + (i - 1), error="RateLimitError: 429 Too Many Requests")


# 1 hata -> 10 dk bekle
senaryo(1, 5);   assert not ae.AgentCouncil.due(_sym), "1 hata: 5 dk yetmemeli"
senaryo(1, 12);  assert ae.AgentCouncil.due(_sym), "1 hata: 12 dk yetmeli (10 dk)"
# 2 ardışık -> 20 dk
senaryo(2, 12);  assert not ae.AgentCouncil.due(_sym), "2 ardışık: 12 dk yetmemeli"
senaryo(2, 25);  assert ae.AgentCouncil.due(_sym), "2 ardışık: 25 dk yetmeli (20 dk)"
# 3 ardışık -> 40 dk
senaryo(3, 25);  assert not ae.AgentCouncil.due(_sym), "3 ardışık: 25 dk yetmemeli"
senaryo(3, 45);  assert ae.AgentCouncil.due(_sym), "3 ardışık: 45 dk yetmeli (40 dk)"
# Çok sayıda ardışık -> bekleme normal aralıkta tavanlanır, sonsuza gitmez
_tavan = config.AGENT_INTERVAL_MINUTES
senaryo(8, _tavan - 5)
assert not ae.AgentCouncil.due(_sym), f"8 ardışık: {_tavan - 5} dk yetmemeli"
senaryo(8, _tavan + 5)
assert ae.AgentCouncil.due(_sym), "bekleme normal aralığı AŞMAMALI (tavan)"
print(f"✓ ardışık geçici hatada bekleme katlanıyor "
      f"(10-20-40-80… tavan {config.AGENT_INTERVAL_MINUTES} dk)")

# Başarılı bir koşu sayacı sıfırlamalı: sonrasındaki ilk hata yine 10 dk
senaryo(1, 12, önce_başarı=True)
assert ae.AgentCouncil.due(_sym), "başarılı koşudan sonra sayaç sıfırlanmalı"
print("✓ başarılı toplantı geri çekilme sayacını sıfırlıyor")

# Kalıcı hata (402) geri çekilmeye değil, kurulu durdurmaya tabidir
with _db3.get_connection() as c:
    c.execute("DELETE FROM agent_runs WHERE symbol = ?", (_sym,))
_kalıcı = _db3.start_agent_run(_sym, 1.0)
_db3.finish_agent_run(_kalıcı, status="ERROR", duration_sec=2.0,
                      error="402 - requires more credits")
with _db3.get_connection() as c:
    c.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
              ((_dt.now(_tz.utc) - _td(minutes=12)).strftime("%Y-%m-%d %H:%M:%S"), _kalıcı))
assert not ae.AgentCouncil.due(_sym), "kalıcı hata geçici sayılıp kısa aralıkta denenmemeli"
print("✓ kalıcı hata (402) geri çekilme mantığına karışmıyor")

# --- 12) Pozisyon limiti Alpaca'da da geçerli olmalı ------------------------
# Hata: kontrol yalnızca dahili defterin elif dalındaydı; Alpaca kullanılırken
# MAX_OPEN_POSITIONS sessizce yok sayılıyordu (canlıda limit 2 iken 5 pozisyon).
class SahteBroker:
    def __init__(self, adet): self.adet, self.alımlar = adet, []
    def positions(self): return [{"symbol": f"X{i}USD"} for i in range(self.adet)]
    def position_for(self, symbol): return None
    def position_notional(self, size): return 5000.0 * size
    def buy(self, symbol, notional, **kw): self.alımlar.append(symbol)

_db3.reset_account()
bot3 = TradingBot()
bot3.backend = "alpaca"
_limit = config.MAX_OPEN_POSITIONS
config.MAX_OPEN_POSITIONS = 2

bot3.broker = SahteBroker(adet=5)          # limitin üstünde
rid = _db3.start_agent_run("BCH/USDT", 247.0)
_db3.finish_agent_run(rid, status="OK", rating="Buy", action="BUY",
                      size_factor=1.0, duration_sec=100.0, reports={})
bot3.apply_pending_decisions("BCH/USDT", 247.0)
assert bot3.broker.alımlar == [], "limit aşılmışken Alpaca'da emir açılmamalı"
assert any("pozisyon limiti" in (l["message"] or "") for l in _db3.get_logs(10))
print("✓ pozisyon limiti Alpaca'da da uygulanıyor")

bot3.broker = SahteBroker(adet=1)          # limitin altında
rid2 = _db3.start_agent_run("LTC/USDT", 48.0)
_db3.finish_agent_run(rid2, status="OK", rating="Buy", action="BUY",
                      size_factor=1.0, duration_sec=100.0, reports={})
bot3.apply_pending_decisions("LTC/USDT", 48.0)
assert bot3.broker.alımlar == ["LTC/USDT"], bot3.broker.alımlar
print("✓ limit altındayken emir normal açılıyor")

class KörBroker(SahteBroker):
    def positions(self): raise RuntimeError("Alpaca 500")

bot3.broker = KörBroker(adet=0)
rid3 = _db3.start_agent_run("AVAX/USDT", 7.2)
_db3.finish_agent_run(rid3, status="OK", rating="Buy", action="BUY",
                      size_factor=1.0, duration_sec=100.0, reports={})
bot3.apply_pending_decisions("AVAX/USDT", 7.2)
assert bot3.broker.alımlar == [], "pozisyon sayısı okunamıyorsa körlemesine emir gönderilmemeli"
print("✓ pozisyon sayısı okunamazsa emir gönderilmiyor")
config.MAX_OPEN_POSITIONS = _limit

print("\nAJAN ENTEGRASYON TESTLERİ GEÇTİ ✅")
