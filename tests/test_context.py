"""
Kurula gönderilen ek bağlam (market_context.py) testleri.

Çalıştırma:
    python tests/test_context.py

Ağ GEREKTİRMEZ: dış servisler (Korku-Açgözlülük endeksi, funding rate) sahte
fonksiyonlarla değiştirilir. Test edilen şey verinin doğruluğu değil, boru
hattı: doğru veriler toplanıyor mu, eksik/bozuk veri toplantıyı düşürüyor mu,
ve üretilen metin gerçekten ajanların promptuna ulaşıyor mu.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_context.db")
os.environ["OFFLINE_SIMULATION"] = "true"
for _s in ("", "-wal", "-shm"):
    if os.path.exists(os.environ["DB_PATH"] + _s):
        os.remove(os.environ["DB_PATH"] + _s)

import config
import database as db
import market_context as mc

db.init_db()

# --- 1) Analist takımı varlık sınıfına göre seçiliyor ------------------------
kripto = config.analysts_for("BTC/USDT")
hisse = config.analysts_for("NVDA")
assert "fundamentals" not in kripto, f"kriptoda temel analist olmamalı: {kripto}"
assert "social" not in kripto, f"kriptoda duygu analisti olmamalı: {kripto}"
assert "market" in kripto and "news" in kripto, kripto
assert "fundamentals" in hisse and "social" in hisse, hisse
print(f"✓ analist takımı: kripto {kripto} | hisse {hisse}")

# --- 2) Sanal defter bağlamı -------------------------------------------------
db.reset_account()
lines = mc.portfolio_section("BTC/USDT")
metin = " ".join(lines)
assert "Sanal hesap" in metin and "10,000" in metin, metin
assert "açık pozisyonumuz YOK" in metin, metin

db.open_position("BTC/USDT", amount=1.0, entry_price=100.0, cost=100.0,
                 entry_fee=0.1, take_profit=102.0, stop_loss=97.5)
metin = " ".join(mc.portfolio_section("BTC/USDT"))
assert "POZİSYONDAYIZ" in metin, metin
assert "97.50" in metin and "102.00" in metin, metin
assert "KAPATMAK" in metin, "pozisyondayken karar çerçevesi değişmeli"
print("✓ portföy bağlamı: açık pozisyon, stop ve kâr al seviyeleri geçiyor")

# --- 3) Broker (Alpaca) bağlamı ve eksik alanlara dayanıklılık ---------------
class FakeBroker:
    def __init__(self, pos=None, acc=None):
        self._pos, self._acc = pos, acc or {
            "equity": 100_000.0, "cash": 25_000.0,
            "buying_power": 400_000.0, "currency": "USD"}

    def account(self):
        return self._acc

    def position_for(self, symbol):
        return self._pos

metin = " ".join(mc.portfolio_section("BTC/USDT", FakeBroker(pos={
    "qty": 0.5, "avg_entry_price": 60_000.0, "current_price": 63_000.0,
    "unrealized_pl": 1_500.0, "unrealized_plpc": 5.0})))
assert "100,000 USD portföy" in metin, metin
assert "AÇIK POZİSYONUMUZ VAR" in metin and "+1,500.00" in metin, metin
print("✓ Alpaca portföyü bağlama giriyor")

# Alpaca bazen alanları None döndürür; f-string patlamamalı.
bos = FakeBroker(acc={"equity": None, "cash": None, "buying_power": None, "currency": None},
                 pos={"qty": None, "avg_entry_price": None, "current_price": None,
                      "unrealized_pl": None, "unrealized_plpc": None})
metin = " ".join(mc.portfolio_section("BTC/USDT", bos))
assert "?" in metin and "USD" in metin, metin
print("✓ eksik broker alanları çökertmiyor")


class BoomBroker:
    def account(self):
        raise RuntimeError("Alpaca 500")

    def position_for(self, symbol):
        raise RuntimeError("Alpaca 500")

assert isinstance(mc.portfolio_section("BTC/USDT", BoomBroker()), list), \
    "broker patlarsa bağlam boş dönmeli, hata fırlatmamalı"
print("✓ broker hatası bağlamı düşürüyor ama toplantıyı engellemiyor")

# --- 4) Teknik bağlam --------------------------------------------------------
db.update_market("BTC/USDT", price=65_000.0, rsi=28.4, ema=74_561.32, signal="BEKLE")
db.save_screener_results([{"symbol": "BTC/USDT", "rank": 1, "score": 0.72,
                           "price": 65_000.0, "rsi": 28.4, "change_24h": -4.1,
                           "volume_ratio": 2.3, "components": {"aşırı_satım": 0.4}}])
metin = " ".join(mc.technical_section("BTC/USDT"))
assert "65,000.0000" in metin and "28.4" in metin, metin
assert "ALTINDA" in metin, "fiyat EMA'nın altındayken trend yönü yazılmalı"
assert "-4.10%" in metin and "2.3 katı" in metin and "sıra 1" in metin, metin
assert "çelişki olursa bunlara güven" in metin, "önceliklendirme talimatı eksik"
print("✓ canlı teknik veri ve tarama sonucu bağlama giriyor")

assert mc.technical_section("DOGE/USDT") == [], "verisi olmayan sembol boş dönmeli"

# EMA'nın üstündeyken yön ters yazılmalı
db.update_market("ETH/USDT", price=4_000.0, rsi=61.0, ema=3_400.0, signal="BEKLE")
assert "ÜSTÜNDE" in " ".join(mc.technical_section("ETH/USDT"))
print("✓ trend yönü fiyat/EMA ilişkisine göre doğru yazılıyor")

# --- 5) Kripto-yerli sinyaller (sahte dış servisler) ------------------------
mc._CACHE.clear()
mc._CACHE["fng"] = (float("inf"), {"value": 18, "label": "Extreme Fear"})
mc._CACHE["deriv:BTC/USDT"] = (float("inf"),
                               {"funding_rate": 0.00042, "open_interest": 81_234.0})
metin = " ".join(mc.crypto_section("BTC/USDT"))
assert "18/100" in metin and "Extreme Fear" in metin, metin
assert "%+0.0420" in metin and "boğa baskın" in metin, metin
assert "81,234" in metin, metin
print("✓ funding rate, açık pozisyon ve Korku-Açgözlülük endeksi bağlama giriyor")

# Negatif funding ters yönü yazmalı
mc._CACHE["deriv:BTC/USDT"] = (float("inf"), {"funding_rate": -0.0003})
assert "ayı baskın" in " ".join(mc.crypto_section("BTC/USDT"))

# Servis ulaşılamazsa satır düşer, hata fırlamaz
mc._CACHE.clear()
assert mc._cached("test", 60, lambda: (_ for _ in ()).throw(RuntimeError("ağ yok"))) is None
sayaç = {"n": 0}


def _üret():
    sayaç["n"] += 1
    return sayaç["n"]

assert mc._cached("sayaç", 3600, _üret) == 1
assert mc._cached("sayaç", 3600, _üret) == 1, "TTL içinde ikinci çağrı yapılmamalı"
print("✓ dış servis hatası yutuluyor, sonuçlar önbellekleniyor")

# --- 6) Birleştirme ---------------------------------------------------------
mc._CACHE["fng"] = (float("inf"), {"value": 18, "label": "Extreme Fear"})
mc._CACHE["deriv:BTC/USDT"] = (float("inf"), {"funding_rate": 0.0004})
tam = mc.build("BTC/USDT")
for başlık in ("İŞLETMECİDEN EK BAĞLAM", "PORTFÖY DURUMUMUZ",
               "CANLI TEKNİK VERİMİZ", "KRİPTO PİYASA SİNYALLERİ"):
    assert başlık in tam, f"'{başlık}' bloğu eksik:\n{tam}"
print(f"✓ tam bağlam üretiliyor ({len(tam)} karakter, {tam.count(chr(10) + '- ')} madde)")

db.update_market("NVDA", price=170.0, rsi=55.0, ema=160.0, signal="BEKLE")
hisse_metni = mc.build("NVDA")
assert "KRİPTO PİYASA SİNYALLERİ" not in hisse_metni, "hissede kripto bloğu olmamalı"
print("✓ hisse senedinde kripto bloğu eklenmiyor")

_önce = config.AGENT_CONTEXT_ENABLED
config.AGENT_CONTEXT_ENABLED = False
assert mc.build("BTC/USDT") == "", "kapalıyken bağlam üretilmemeli"
config.AGENT_CONTEXT_ENABLED = _önce
print("✓ AGENT_CONTEXT_ENABLED=False bağlamı tamamen kapatıyor")

_önce_kripto = config.CRYPTO_SIGNALS_ENABLED
config.CRYPTO_SIGNALS_ENABLED = False
assert "KRİPTO PİYASA SİNYALLERİ" not in mc.build("BTC/USDT")
config.CRYPTO_SIGNALS_ENABLED = _önce_kripto
print("✓ CRYPTO_SIGNALS_ENABLED=False yalnız kripto bloğunu kapatıyor")

# --- 7) Bağlam gerçekten ajanların promptuna ulaşıyor mu? -------------------
import agents_engine as ae

# Üst akıştaki kanca hâlâ duruyor mu? Kütüphane bu metodu yeniden adlandırırsa
# bağlamımız sessizce kaybolur; testin yakalaması gereken şey tam olarak budur.
assert ae.AGENTS_AVAILABLE, f"tradingagents yüklenemedi: {ae.IMPORT_ERROR}"
assert hasattr(ae.TradingAgentsGraph, "resolve_instrument_context"), \
    "TradingAgentsGraph.resolve_instrument_context kayboldu — bağlam enjeksiyonu kırılır"
print("✓ üst akıştaki enjeksiyon kancası yerinde")

_gerçek = ae.TradingAgentsGraph


class StubBase:
    """Gerçek grafiğin yerine geçer: LLM anahtarı ve ağ gerektirmez."""
    def __init__(self, selected_analysts=None, debug=False, config=None):
        self.selected_analysts = list(selected_analysts or [])
        self.görülen = []

    def resolve_instrument_context(self, ticker, asset_type="stock"):
        return f"[KİMLİK:{ticker}]"

    def propagate(self, ticker, date, asset_type="stock"):
        self.görülen.append(self.resolve_instrument_context(ticker, asset_type))
        return {"final_trade_decision": "Rating: Hold"}, "Hold"

ae.TradingAgentsGraph = StubBase
try:
    Cls = ae._contextual_graph_class()
    g = Cls(selected_analysts=["market"], debug=False, config={})
    g.set_context("BTCUSD", "\n\nEK BAĞLAM BURADA")
    çıktı = g.resolve_instrument_context("BTCUSD", "crypto")
    assert çıktı == "[KİMLİK:BTCUSD]\n\nEK BAĞLAM BURADA", çıktı
    assert g.resolve_instrument_context("ETHUSD", "crypto") == "[KİMLİK:ETHUSD]", \
        "bir sembolün bağlamı diğerine sızmamalı"
    print("✓ ek bağlam kimlik metnine ekleniyor, semboller arası sızmıyor")

    # Uçtan uca: analyze() bağlamı grafiğe yerleştiriyor mu?
    ae.AgentCouncil.readiness = staticmethod(lambda: (True, "test"))
    council = ae.AgentCouncil()
    council._export_env = lambda: None
    council._build_config = lambda: {}
    sonuç = council.analyze("BTC/USDT", price=65_000.0)
    assert sonuç["status"] == "OK", sonuç
    graph = council.graph("BTC/USDT")
    assert graph.selected_analysts == kripto, graph.selected_analysts
    prompt = graph.görülen[-1]
    assert "İŞLETMECİDEN EK BAĞLAM" in prompt, prompt[:400]
    assert "Binance anlık fiyat" in prompt, prompt[:400]
    print("✓ analyze() bağlamı üretip kurula veriyor (uçtan uca)")

    kayıt = db.get_agent_run(sonuç["run_id"])
    assert "İŞLETMECİDEN EK BAĞLAM" in (kayıt["reports"].get("operator_context") or ""), \
        "bağlam tutanağa yazılmalı ki panelde geriye dönük görülebilsin"
    print("✓ verilen bağlam toplantı tutanağına kaydediliyor")

    # Hisse sembolü farklı analist takımıyla ikinci bir grafik kurmalı
    council.analyze("NVDA", price=170.0)
    assert len(council._graphs) == 2, council._graphs.keys()
    assert council.graph("NVDA").selected_analysts == hisse
    print("✓ hisse ve kripto için ayrı grafikler önbelleğe alınıyor")
finally:
    ae.TradingAgentsGraph = _gerçek

# --- 8) Aynı anda tek toplantı (checkpoint yarışı) --------------------------
# Gerçekte olan: iki toplantı üst üste bindiğinde önce biten,
# TradingAgentsGraph'ın paylaşılan checkpoint bağlantısını kapatıyor ve süren
# diğeri "Cannot operate on a closed database" ile ölüyordu.
import threading as _th
import time as _time

ae.TradingAgentsGraph = StubBase
try:
    başladı, devam_et = _th.Event(), _th.Event()

    class BlockingGraph(StubBase):
        def propagate(self, ticker, date, asset_type="stock"):
            başladı.set()
            devam_et.wait(10)
            return {"final_trade_decision": "Rating: Hold"}, "Hold"

    council2 = ae.AgentCouncil()
    council2._export_env = lambda: None
    council2._build_config = lambda: {}
    from collections import OrderedDict
    council2._graphs = OrderedDict(
        (s, BlockingGraph(selected_analysts=["market"])) for s in ("BTC/USDT", "ETH/USDT"))

    sonuçlar = {}
    t = _th.Thread(target=lambda: sonuçlar.update(
        birinci=council2.analyze("BTC/USDT", price=65_000.0)), daemon=True)
    t.start()
    assert başladı.wait(10), "ilk toplantı başlamadı"
    assert ae.AgentCouncil.busy(), "toplantı sürerken busy() True olmalı"

    önce = len(db.get_agent_runs(limit=100))
    ikinci = council2.analyze("ETH/USDT", price=2_400.0)
    assert ikinci["status"] == "BUSY", ikinci
    assert ikinci["run_id"] is None, "reddedilen toplantı için kayıt açılmamalı"
    assert len(db.get_agent_runs(limit=100)) == önce, "boşuna kayıt yazılmış"
    print("✓ ikinci toplantı sıraya alınıyor, kayıt açılmıyor")

    devam_et.set()
    t.join(10)
    assert sonuçlar["birinci"]["status"] == "OK", sonuçlar
    assert not ae.AgentCouncil.busy(), "toplantı bitince kilit bırakılmalı"
    print("✓ toplantı bitince kilit bırakılıyor")

    # Grafikler sembol başına ayrı nesne olmalı: zaman aşımına uğrayıp arka
    # planda süren bir koşu, bir sonraki sembolün checkpoint'ini bozmasın.
    council3 = ae.AgentCouncil()
    council3._export_env = lambda: None
    council3._build_config = lambda: {}
    assert council3.graph("BTC/USDT") is not council3.graph("ETH/USDT"), \
        "her sembol kendi grafik nesnesini almalı"
    assert council3.graph("BTC/USDT") is council3.graph("BTC/USDT"), "aynı sembol önbellekten"
    for i in range(ae.AgentCouncil.MAX_CACHED_GRAPHS + 3):
        council3.graph(f"X{i}/USDT")
    assert len(council3._graphs) <= ae.AgentCouncil.MAX_CACHED_GRAPHS, \
        f"önbellek sınırsız büyümüş: {len(council3._graphs)}"
    print(f"✓ sembol başına ayrı grafik, önbellek {ae.AgentCouncil.MAX_CACHED_GRAPHS} ile sınırlı")
finally:
    ae.TradingAgentsGraph = _gerçek

# --- 9) Kurulun analiz edemediği coinler --------------------------------- --
ok_btc, _ = ae.council_can_analyze("BTC/USDT")
ok_uni, sebep = ae.council_can_analyze("UNI/USDT")
ok_hisse, _ = ae.council_can_analyze("NVDA")
assert ok_btc and ok_hisse, "BTC ve hisse senedi analiz edilebilmeli"
assert not ok_uni and "UNIUSD" in sebep, sebep
print(f"✓ desteklenmeyen coin tanınıyor: {sebep}")

önce = len(db.get_agent_runs(limit=100))
sonuç = ae.get_council().analyze("UNI/USDT", price=5.8)
assert sonuç["status"] == "UNSUPPORTED" and sonuç["run_id"] is None, sonuç
assert len(db.get_agent_runs(limit=100)) == önce, \
    "analiz edilemeyen coin için kayıt açılmamalı"
print("✓ desteklenmeyen coin kurula hiç gönderilmiyor (boşuna hata kaydı yok)")

# Elenen coin aday slotunu harcamamalı: sırada altındaki uygun coin çıkmalı
import screener as sc


class SahteTarayıcı(sc.Screener):
    def __init__(self, sıra):
        self._sıra = sıra

    def scan(self, symbols=None):
        return [{"symbol": s, "score": 1.0 - i / 10, "rank": i + 1}
                for i, s in enumerate(self._sıra)]

tar = SahteTarayıcı(["UNI/USDT", "AAVE/USDT", "BTC/USDT", "ETH/USDT"])
seçilen = tar.candidates(top_n=2, keep=lambda s: ae.council_can_analyze(s)[0])
assert seçilen == ["BTC/USDT", "ETH/USDT"], seçilen
assert tar.candidates(top_n=2) == ["UNI/USDT", "AAVE/USDT"], "filtresiz davranış değişmemeli"
print(f"✓ elenen coin slot harcamıyor, sıradaki uygun coin çıkıyor: {seçilen}")

print("\nBAĞLAM VE EŞZAMANLILIK TESTLERİ GEÇTİ ✅")
