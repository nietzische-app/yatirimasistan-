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
council._graph = FakeGraph("Buy", state)
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
council._graph = FakeGraph("Sell", {"final_trade_decision": "Rating: Sell"})
res2 = council.analyze("BTC/USDT", price=105.0)
assert res2["action"] == "SELL"
bot.apply_pending_decisions("BTC/USDT", 105.0)
assert not db.get_open_positions("BTC/USDT"), "SAT kararı pozisyonu kapatmalıydı"
assert db.get_trades(1)[0]["exit_reason"].startswith("Kurul kararı")
print("✓ SAT kararı pozisyonu kapatıyor")

# --- 6) HOLD hiçbir şey yapmaz ----------------------------------------------
db.reset_account()
council._graph = FakeGraph("Hold", {"final_trade_decision": "Rating: Hold"})
council.analyze("ETH/USDT", price=3000.0)
bot.apply_pending_decisions("ETH/USDT", 3000.0)
assert not db.get_open_positions(), "BEKLE kararı işlem açmamalı"
print("✓ BEKLE kararı işlem açmıyor")

# --- 7) Hata ve zaman aşımı yolları -----------------------------------------
class BoomGraph:
    def propagate(self, *a, **k): raise RuntimeError("429 kota doldu")
council._graph = BoomGraph()
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
council._graph = SlowGraph()
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
council._graph = QuotaGraph()
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
council._graph = FlakyGraph()
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

print("\nAJAN ENTEGRASYON TESTLERİ GEÇTİ ✅")
