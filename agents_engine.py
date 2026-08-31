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


_STOP_RE = re.compile(
    r"stop[\s\-]?loss\D{0,30}?([0-9][0-9,.]*)", re.IGNORECASE)


def extract_stop_price(state: dict, price: Optional[float]) -> Optional[float]:
    """
    Ajanların önerdiği stop-loss fiyatı.

    Önce yapılandırılmış TraderProposal.stop_loss alanı aranır; yoksa karar
    metninden sayı çekilir. Saçma değerlere karşı config'teki sınırlarla
    doğrulanır (fiyatın %0.5 – %15 altında olmalı).
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

    if candidate is None:
        for key in ("trader_investment_plan", "final_trade_decision", "investment_plan"):
            text = state.get(key)
            if not text:
                continue
            m = _STOP_RE.search(str(text))
            if m:
                try:
                    candidate = float(m.group(1).replace(",", ""))
                    break
                except ValueError:
                    pass

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
    def due(symbol: str) -> bool:
        """Bu sembol için yeni toplantı zamanı geldi mi?"""
        if db.agent_runs_today() >= config.AGENT_MAX_RUNS_PER_DAY:
            return False
        last = db.last_agent_run_time(symbol)
        if not last:
            return True
        try:
            started = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - started >= timedelta(
            minutes=config.AGENT_INTERVAL_MINUTES)

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
            db.add_log("ERROR", f"{symbol}: kurul hatası — {box['error'][:180]}", symbol)
            log.error("[%s] Kurul hatası: %s", symbol, box["error"])
            result["error"] = box["error"]
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
