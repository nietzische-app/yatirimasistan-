"""
agents_engine.py
----------------
TradingAgents (TauricResearch) çoklu ajan kurulunu projeye bağlar.

Kurul; Market/Social/News/Fundamentals analistleri, Boğa-Ayı araştırmacı
tartışması, Trader ve Risk kurulundan oluşur ve nihai kararı 5 kademeli
not olarak verir (Buy / Overweight / Hold / Underweight / Sell).

Bu modül kurulu çalıştırır, çıktısını AL/SAT/BEKLE + pozisyon büyüklüğü +
stop-loss'a çevirir ve tüm ajan raporlarını SQLite'a yazar. Emirleri bu modül
GÖNDERMEZ; onu bot.py'deki uygulama katmanı yapar.

Maliyet uyarısı: bir toplantı onlarca LLM çağrısıdır. Sıklık config.py'deki
AGENT_INTERVAL_MINUTES ile sınırlanır (alt sınır 15 dk) ve günlük toplam
AGENT_MAX_RUNS_PER_DAY ile kesilir.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
import database as db

log = logging.getLogger("agents")

# TradingAgents ağır bir bağımlılık; yoksa sistem "manuel" moda düşsün.
try:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    AGENTS_AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    DEFAULT_CONFIG, TradingAgentsGraph = None, None
    AGENTS_AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# Panelde gösterilecek rapor alanları: (state anahtarı, başlık)
REPORT_FIELDS = [
    ("market_report", "📈 Teknik Analist"),
    ("sentiment_report", "💬 Duygu Analisti"),
    ("news_report", "📰 Haber Analisti"),
    ("fundamentals_report", "🏛️ Temel Analist"),
    ("investment_plan", "🧭 Araştırma Müdürü Planı"),
    ("trader_investment_plan", "💼 Trader Önerisi"),
    ("final_trade_decision", "⚖️ Risk Kurulu Kararı"),
]

BUY_RATINGS = {"buy", "overweight"}
SELL_RATINGS = {"sell", "underweight"}


# ==========================================================================
# YARDIMCILAR
# ==========================================================================
def _flatten_debate(state: dict, key: str) -> dict:
    """investment_debate_state / risk_debate_state alanlarını düz metne indirger."""
    raw = state.get(key)
    if not isinstance(raw, dict):
        return {}
    keep = ("bull_history", "bear_history", "aggressive_history", "conservative_history",
            "neutral_history", "judge_decision", "history")
    return {k: str(v) for k, v in raw.items() if k in keep and v}


def extract_reports(state: dict) -> dict:
    """final_state içinden panelde gösterilecek her şeyi toplar."""
    reports: dict[str, Any] = {}
    for key, _title in REPORT_FIELDS:
        val = state.get(key)
        if val:
            reports[key] = str(val)
    debate = _flatten_debate(state, "investment_debate_state")
    if debate:
        reports["investment_debate"] = debate
    risk = _flatten_debate(state, "risk_debate_state")
    if risk:
        reports["risk_debate"] = risk
    return reports


# Ajanlar stop'u üç ayrı biçimde yazabiliyor:
#   "stop-loss: 76,100"      -> mutlak fiyat
#   "stop-loss at 2%"        -> girişe göre yüzde
#   "%2 stop-loss" (Türkçe)  -> yüzde, işaret önde
# Yüzdeyi fiyat sanmak stop'u 2 dolara koymak demektir; bu yüzden yüzde
# kalıpları ÖNCE aranır.
_STOP_PCT_RE = re.compile(
    r"stop[\s\-]?loss\D{0,40}?%\s*([0-9]+(?:[.,][0-9]+)?)"       # stop-loss ... %2
    r"|stop[\s\-]?loss\D{0,40}?([0-9]+(?:[.,][0-9]+)?)\s*%"      # stop-loss ... 2%
    r"|%\s*([0-9]+(?:[.,][0-9]+)?)\D{0,20}?stop[\s\-]?loss",     # %2 stop-loss
    re.IGNORECASE)

_STOP_ABS_RE = re.compile(r"stop[\s\-]?loss\D{0,30}?([0-9][0-9.,]*)", re.IGNORECASE)


def _to_number(raw: str) -> Optional[float]:
    """
    '76,100.50' (İngilizce), '76.100,50' (Türkçe) ve '76100' biçimlerini çözer.
    Son ayırıcı hangisiyse ondalık odur; diğeri binlik ayırıcıdır.
    """
    text = (raw or "").strip().rstrip(".,")
    if not text:
        return None
    last_dot, last_comma = text.rfind("."), text.rfind(",")
    try:
        if last_dot >= 0 and last_comma >= 0:
            if last_comma > last_dot:                    # 76.100,50 -> Türkçe
                return float(text.replace(".", "").replace(",", "."))
            return float(text.replace(",", ""))          # 76,100.50 -> İngilizce
        if last_comma >= 0:
            # Tek virgül: ardından tam 3 hane geliyorsa binlik, değilse ondalık
            return float(text.replace(",", "" if len(text) - last_comma == 4 else "."))
        if last_dot >= 0 and len(text) - last_dot == 4:   # 76.100 -> binlik (Türkçe)
            return float(text.replace(".", ""))
        return float(text)
    except ValueError:
        return None


def _stop_from_text(text: str, price: float) -> Optional[float]:
    """Metinden stop FİYATI çıkarır; yüzde ifadesi fiyata çevrilir."""
    m = _STOP_PCT_RE.search(text)
    if m:
        pct = _to_number(next((g for g in m.groups() if g), None))
        if pct is not None and 0 < pct < 100:
            return price * (1 - pct / 100.0)
    m = _STOP_ABS_RE.search(text)
    if m:
        return _to_number(m.group(1))
    return None


def extract_stop_price(state: dict, price: Optional[float]) -> Optional[float]:
    """
    Ajanların önerdiği stop-loss FİYATI.

    Önce yapılandırılmış TraderProposal.stop_loss alanı aranır; yoksa karar
    metninden çıkarılır (yüzde ifadeleri fiyata çevrilir). Sonuç config'teki
    sınırlarla doğrulanır: fiyatın %0.5 – %15 altında değilse yok sayılır.
    """
    candidate: Optional[float] = None

    for key in ("trader_proposal", "trader_investment_plan_structured", "portfolio_decision"):
        obj = state.get(key)
        val = getattr(obj, "stop_loss", None) if obj is not None else None
        if val is None and isinstance(obj, dict):
            val = obj.get("stop_loss")
        if val:
            try:
                candidate = float(val)
                break
            except (TypeError, ValueError):
                pass

    if candidate is None and price:
        for key in ("trader_investment_plan", "final_trade_decision", "investment_plan"):
            text = state.get(key)
            if not text:
                continue
            candidate = _stop_from_text(str(text), price)
            if candidate is not None:
                break

    if candidate is None or not price:
        return None
    distance = (price - candidate) / price
    if config.AGENT_STOP_MIN_PCT <= distance <= config.AGENT_STOP_MAX_PCT:
        return candidate
    log.warning("Ajan stop önerisi sınır dışı (%.2f, fiyat %.2f); yok sayıldı", candidate, price)
    return None


def rating_to_action(rating: str) -> tuple[str, float]:
    """5 kademeli notu (AL/SAT/BEKLE, pozisyon çarpanı) ikilisine çevirir."""
    key = (rating or "").strip().lower()
    size = config.AGENT_SIZE_BY_RATING.get(key, 0.0)
    if key in BUY_RATINGS:
        return "BUY", float(size)
    if key in SELL_RATINGS:
        return "SELL", 0.0
    return "HOLD", 0.0


# Tekrar denemenin işe YARAMAYACAĞI hatalar: kredi bitmiş, anahtar geçersiz.
# Bunlarda saatte bir tekrar denemek yalnızca log kirletir.
FATAL_PATTERNS = (
    ("402", "OpenRouter/LLM kredisi bitmiş"),
    ("insufficient_quota", "LLM kotası bitmiş"),
    ("more credits", "OpenRouter/LLM kredisi bitmiş"),
    ("401", "LLM anahtarı geçersiz"),
    ("invalid_api_key", "LLM anahtarı geçersiz"),
    ("no auth credentials", "LLM anahtarı gönderilmiyor"),
)


def classify_error(message: str) -> Optional[str]:
    """Hata kalıcı mı? Kalıcıysa insan diliyle sebebini döner, değilse None."""
    low = (message or "").lower()
    for needle, reason in FATAL_PATTERNS:
        if needle.lower() in low:
            return reason
    return None


def _openrouter_get(path: str) -> Optional[dict]:
    """OpenRouter API'sinden JSON okur; hata olursa sessizce None."""
    if "openrouter" not in (config.LLM_BACKEND_URL or "") or not config.LLM_API_KEY:
        return None
    try:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"https://openrouter.ai/api/v1/{path}",
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read()).get("data", {})
    except Exception as exc:
        log.debug("OpenRouter %s okunamadı: %s", path, exc)
        return None


def summarize_error(message: str) -> str:
    """Uzun sağlayıcı JSON'larını tek satıra indirger."""
    low = (message or "").lower()
    if "rate-limited upstream" in low or "upstream_provider_shared_pool" in low:
        return ("model sağlayıcısının ortak havuzu geçici olarak tıkalı (429) — "
                "kendi kotan değil; birazdan tekrar denenecek")
    if "429" in low:
        return "hız sınırı (429) — birazdan tekrar denenecek"
    return (message or "")[:180]


def openrouter_credit() -> Optional[dict]:
    """
    Kredi durumu. İki ayrı bilgi var, ikisi de önemli:
      * HESAP bakiyesi (/credits)  -> 402'nin gerçek sebebi burasıdır
      * ANAHTAR limiti (/key)      -> anahtara özel harcama tavanı (olmayabilir)
    En iyi çaba: uç nokta değişirse veya ağ yoksa None döner.
    """
    def _f(d, k):
        v = (d or {}).get(k)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    account = _openrouter_get("credits")
    key = _openrouter_get("key")
    if account is None and key is None:
        return None

    total, used = _f(account, "total_credits"), _f(account, "total_usage")
    out = {
        "account_balance": (total - used) if (total is not None and used is not None) else None,
        "account_total": total,
        "account_used": used,
        "key_usage": _f(key, "usage"),
        "key_limit": _f(key, "limit"),
        "free_tier": (key or {}).get("is_free_tier"),
    }
    out["key_remaining"] = ((out["key_limit"] - out["key_usage"])
                            if (out["key_limit"] is not None and out["key_usage"] is not None)
                            else None)
    return out


# ==========================================================================
# KURUL
# ==========================================================================
class AgentCouncil:
    """TradingAgents grafiğini kurar ve sembol başına çalıştırır."""

    def __init__(self) -> None:
        self._graph = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- kurulum
    @staticmethod
    def readiness() -> tuple[bool, str]:
        """(hazır mı, sebep) — panelde ve logda göstermek için."""
        if config.DECISION_ENGINE != "agents":
            return False, "DECISION_ENGINE 'agents' değil (kurul kapalı)."
        if not AGENTS_AVAILABLE:
            return False, f"tradingagents paketi yüklenemedi -> {IMPORT_ERROR}"
        if not config.LLM_API_KEY:
            return False, ("LLM anahtarı yok. .env içine OPENROUTER_API_KEY "
                           "(veya OPENAI_API_KEY / DEEPSEEK_API_KEY) ekle.")
        return True, "hazır"

    def _build_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({
            "llm_provider": config.LLM_PROVIDER,
            "backend_url": config.LLM_BACKEND_URL or None,
            "deep_think_llm": config.LLM_DEEP_MODEL,
            "quick_think_llm": config.LLM_QUICK_MODEL,
            "max_debate_rounds": config.AGENT_DEBATE_ROUNDS,
            "max_risk_discuss_rounds": config.AGENT_RISK_ROUNDS,
            "output_language": config.AGENT_OUTPUT_LANGUAGE,
            # Yarıda kalan toplantı bir sonraki denemede kaldığı yerden devam etsin
            "checkpoint_enabled": config.AGENT_CHECKPOINT_ENABLED,
            # Ajanların hafızası/önbelleği kalıcı volume'da dursun
            "data_cache_dir": os.path.join(os.path.dirname(config.DB_PATH), "agents_cache"),
            "results_dir": os.path.join(os.path.dirname(config.DB_PATH), "agents_logs"),
            "memory_log_path": os.path.join(
                os.path.dirname(config.DB_PATH), "agents_memory", "trading_memory.md"),
        })
        for key, value in (("temperature", config.LLM_TEMPERATURE),
                           ("max_tokens", config.LLM_MAX_TOKENS),
                           ("llm_max_retries", config.LLM_MAX_RETRIES)):
            if value not in ("", None):
                cfg[key] = value
        for path in (cfg["data_cache_dir"], cfg["results_dir"],
                     os.path.dirname(cfg["memory_log_path"])):
            os.makedirs(path, exist_ok=True)
        return cfg

    def _export_env(self) -> None:
        """LangChain istemcileri ve veri sağlayıcıları anahtarları ortamdan okur."""
        if config.LLM_API_KEY:
            os.environ.setdefault("OPENAI_API_KEY", config.LLM_API_KEY)
            os.environ["OPENAI_API_KEY"] = config.LLM_API_KEY
        if config.ALPHA_VANTAGE_API_KEY:
            os.environ["ALPHA_VANTAGE_API_KEY"] = config.ALPHA_VANTAGE_API_KEY
        if config.FRED_API_KEY:
            os.environ["FRED_API_KEY"] = config.FRED_API_KEY

    def graph(self):
        """Grafiği bir kez kurar (kurulum saniyeler sürer, her turda tekrarlanmaz)."""
        with self._lock:
            if self._graph is None:
                ok, reason = self.readiness()
                if not ok:
                    raise RuntimeError(reason)
                self._export_env()
                self._graph = TradingAgentsGraph(
                    selected_analysts=config.AGENT_ANALYSTS,
                    debug=False,
                    config=self._build_config(),
                )
                log.info("Kurul hazır | analistler: %s | model: %s",
                         ", ".join(config.AGENT_ANALYSTS), config.LLM_DEEP_MODEL)
            return self._graph

    # ------------------------------------------------------- sıklık kontrolü
    @staticmethod
    def halted() -> Optional[str]:
        """
        Kurul kalıcı bir hata yüzünden durduruldu mu? Sebebini döner.
        Kredi bittiğinde saatte bir tekrar denemenin anlamı yok; sorun
        giderilince `python bot.py --resume-council` ile devam edilir.
        """
        return db.get_state("council_halted") or None

    @staticmethod
    def resume() -> None:
        db.set_state("council_halted", "")
        db.add_log("INFO", "Kurul yeniden etkinleştirildi.")

    @staticmethod
    def due(symbol: str) -> bool:
        """Bu sembol için yeni toplantı zamanı geldi mi?"""
        if AgentCouncil.halted():
            return False
        if db.agent_runs_today() >= config.AGENT_MAX_RUNS_PER_DAY:
            return False
        runs = db.get_agent_runs(limit=1, symbol=symbol)
        if not runs:
            return True
        last = runs[0]
        try:
            started = datetime.strptime(last["started_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError, KeyError):
            return True

        # Geçici hatadan sonra tam süreyi beklemek gereksiz: checkpoint sayesinde
        # tekrar deneme kaldığı yerden devam eder.
        transient_failure = (last["status"] in ("ERROR", "TIMEOUT")
                             and not classify_error(last.get("error") or ""))
        wait = config.AGENT_RETRY_MINUTES if transient_failure else config.AGENT_INTERVAL_MINUTES
        return datetime.now(timezone.utc) - started >= timedelta(minutes=wait)

    # ------------------------------------------------------------- çalıştır
    def analyze(self, symbol: str, price: Optional[float] = None) -> dict:
        """
        Kurulu bir sembol için toplar. Sonucu ve tüm raporları veritabanına yazar.
        Dönen sözlük: {run_id, status, rating, action, size_factor, proposed_stop}
        """
        run_id = db.start_agent_run(symbol, price)
        ticker = config.agent_ticker(symbol)
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        started = time.time()
        result: dict[str, Any] = {"run_id": run_id, "status": "ERROR", "action": "HOLD",
                                  "rating": None, "size_factor": 0.0, "proposed_stop": None}
        box: dict[str, Any] = {}

        def _work():
            try:
                state, signal = self.graph().propagate(ticker, trade_date, asset_type="crypto")
                box["state"], box["signal"] = state, signal
            except Exception as exc:                     # ağ/kota/model hatası
                box["error"] = f"{type(exc).__name__}: {exc}"

        log.info("[%s] Kurul toplanıyor (ticker %s, %s)...", symbol, ticker, trade_date)
        worker = threading.Thread(target=_work, name=f"council-{symbol}", daemon=True)
        worker.start()
        worker.join(timeout=config.AGENT_RUN_TIMEOUT_SECONDS)
        duration = time.time() - started

        if worker.is_alive():
            db.finish_agent_run(run_id, status="TIMEOUT", duration_sec=duration,
                                error=f"{config.AGENT_RUN_TIMEOUT_SECONDS} sn içinde bitmedi")
            db.add_log("ERROR", f"{symbol}: kurul zaman aşımına uğradı", symbol)
            result["status"] = "TIMEOUT"
            return result

        if "error" in box:
            db.finish_agent_run(run_id, status="ERROR", duration_sec=duration, error=box["error"])
            fatal = classify_error(box["error"])
            if fatal:
                # Tekrar denemek işe yaramaz: kurulu durdur ve sebebini yaz.
                db.set_state("council_halted", fatal)
                db.add_log("ERROR",
                           f"KURUL DURDURULDU — {fatal}. Sorunu giderdikten sonra: "
                           f"python bot.py --resume-council", symbol)
                log.error("[%s] Kurul DURDURULDU — %s", symbol, fatal)
            else:
                short = summarize_error(box["error"])
                db.add_log("ERROR", f"{symbol}: kurul hatası — {short}", symbol)
                log.error("[%s] Kurul hatası: %s", symbol, short)
            result["error"] = box["error"]
            result["fatal"] = fatal
            return result

        state, signal = box.get("state") or {}, box.get("signal")
        action, size = rating_to_action(str(signal))
        stop = extract_stop_price(state, price) if config.AGENT_USE_PROPOSED_STOP else None
        reports = extract_reports(state)

        db.finish_agent_run(run_id, status="OK", rating=str(signal), action=action,
                            size_factor=size, proposed_stop=stop, duration_sec=duration,
                            reports=reports)
        msg = (f"{symbol}: kurul kararı {signal} -> {action}"
               + (f" (büyüklük x{size:g})" if action == "BUY" else "")
               + (f", stop {stop:,.2f}" if stop else "")
               + f" | {duration:.0f} sn")
        db.add_log("AGENT", msg, symbol)
        log.info(msg)

        result.update({"status": "OK", "rating": str(signal), "action": action,
                       "size_factor": size, "proposed_stop": stop})
        return result


_COUNCIL: Optional[AgentCouncil] = None


def get_council() -> AgentCouncil:
    """Süreç başına tek kurul örneği."""
    global _COUNCIL
    if _COUNCIL is None:
        _COUNCIL = AgentCouncil()
    return _COUNCIL


if __name__ == "__main__":       # python agents_engine.py BTC/USDT
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    db.init_db()
    ok, reason = AgentCouncil.readiness()
    print(f"Kurul durumu: {reason}")
    if not ok:
        sys.exit(1)
    sym = sys.argv[1] if len(sys.argv) > 1 else config.SYMBOLS[0]
    print(get_council().analyze(sym))
