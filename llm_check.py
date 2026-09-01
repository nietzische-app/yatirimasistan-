"""
llm_check.py
------------
Seçilen LLM'in kurulun ihtiyaçlarını karşılayıp karşılamadığını ÖLÇER.

TradingAgents modelden üç şey ister; biri eksikse kurul çalışmaz ya da
saatlerce dönüp boş rapor üretir:

  1. Sohbet          — temel çalışırlık, anahtar/model adı doğru mu
  2. ARAÇ ÇAĞIRMA    — analistler veriyi tool call ile çeker. Yoksa kurul çöker.
  3. Yapılandırılmış  — Araştırma Müdürü / Trader JSON şema ile cevap ister.
     çıktı              Yoksa serbest metne düşer, karar ayrıştırması zayıflar.

Kullanım:
    python bot.py --test-llm
    python bot.py --test-llm --model z-ai/glm-4.6      # başka modeli dene
"""

from __future__ import annotations

import json
import time
from typing import Optional

import config

WEATHER_TOOL = [{
    "type": "function",
    "function": {
        "name": "get_price",
        "description": "Bir sembolün güncel fiyatını döndürür",
        "parameters": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "örn. BTC"}},
            "required": ["symbol"],
        },
    },
}]

DECISION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "karar",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "rating": {"type": "string", "enum": ["Buy", "Hold", "Sell"]},
                "confidence": {"type": "number"},
            },
            "required": ["rating", "confidence"],
            "additionalProperties": False,
        },
    },
}


def list_models(search: str = "", only_capable: bool = False) -> list[dict]:
    """
    OpenRouter'daki modelleri listeler (uç nokta herkese açık, anahtar gerekmez).

    Önemli olan `supported_parameters`: kurul için "tools" ZORUNLU,
    "response_format" ise güçlü tavsiye. Bunu model kartından okumak yerine
    doğrudan API'den alıyoruz.
    """
    import urllib.request

    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as resp:
        models = json.loads(resp.read()).get("data", [])

    needle = (search or "").lower()
    out = []
    for m in models:
        mid = m.get("id", "")
        name = m.get("name", "")
        if needle and needle not in mid.lower() and needle not in name.lower():
            continue
        params = set(m.get("supported_parameters") or [])
        pricing = m.get("pricing") or {}

        def _price(key):
            try:
                return float(pricing.get(key))
            except (TypeError, ValueError):
                return None

        row = {
            "id": mid,
            "name": name,
            "context": m.get("context_length"),
            "tools": "tools" in params,
            "structured": "response_format" in params or "structured_outputs" in params,
            "prompt_price": _price("prompt"),
            "completion_price": _price("completion"),
        }
        row["free"] = (row["prompt_price"] == 0 and row["completion_price"] == 0) \
            or mid.endswith(":free")
        if only_capable and not row["tools"]:
            continue
        out.append(row)
    return sorted(out, key=lambda r: (not r["tools"], r["id"]))


def print_models(search: str = "", only_capable: bool = False) -> None:
    line = "─" * 96
    try:
        rows = list_models(search, only_capable)
    except Exception as exc:
        print(f"\nModel listesi alınamadı: {type(exc).__name__}: {exc}")
        print("Elle bakmak için: https://openrouter.ai/models\n")
        return

    print(f"\n{line}")
    title = f"  OPENROUTER MODELLERİ" + (f"  ·  arama: '{search}'" if search else "")
    print(f"{title}\n{line}")
    if not rows:
        print("  Eşleşen model yok. Arama terimini kısalt (ör. 'ling', 'deepseek').\n")
        return
    print(f"  {'MODEL ID':<44} {'BAĞLAM':>8}  {'ARAÇ':^5} {'JSON':^5}  {'$/M girdi':>10} {'$/M çıktı':>10}")
    print(f"  {'-'*44} {'-'*8}  {'-'*5} {'-'*5}  {'-'*10} {'-'*10}")
    for r in rows[:40]:
        ctx = f"{r['context']:,}" if r["context"] else "-"
        pin = f"{r['prompt_price'] * 1e6:.2f}" if r["prompt_price"] is not None else "-"
        pout = f"{r['completion_price'] * 1e6:.2f}" if r["completion_price"] is not None else "-"
        print(f"  {r['id'][:44]:<44} {ctx:>8}  {'✅' if r['tools'] else '❌':^4} "
              f"{'✅' if r['structured'] else '❌':^4}  {pin:>10} {pout:>10}")
    if len(rows) > 40:
        print(f"  ... ve {len(rows) - 40} model daha (aramayı daralt)")
    print(f"\n  ARAÇ sütunu ❌ olan modeller kurul için KULLANILAMAZ.")
    print(f"  Seçtiğin modeli sınamak için:  python bot.py --test-llm --model <ID>")
    print(f"{line}\n")


def _client(model: Optional[str] = None):
    from openai import OpenAI
    return OpenAI(api_key=config.LLM_API_KEY,
                  base_url=config.LLM_BACKEND_URL or None,
                  timeout=90.0, max_retries=0)


def run_checks(model: Optional[str] = None) -> dict:
    """Üç yeteneği sırayla dener; sonucu sözlük olarak döner."""
    model = model or config.LLM_DEEP_MODEL
    results: dict[str, dict] = {}
    client = _client()

    def _try(name: str, fn):
        started = time.time()
        try:
            value = fn()
            results[name] = {"ok": True, "detail": value, "sec": time.time() - started}
        except Exception as exc:
            results[name] = {"ok": False,
                             "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                             "sec": time.time() - started}

    # 1) Temel sohbet
    def _chat():
        r = client.chat.completions.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": "Sadece 'merhaba' yaz."}])
        usage = getattr(r, "usage", None)
        tok = f", {usage.total_tokens} token" if usage else ""
        return f"cevap alındı{tok}"
    _try("sohbet", _chat)

    # 2) Araç çağırma — kurul için ZORUNLU
    def _tools():
        r = client.chat.completions.create(
            model=model, max_tokens=200, tools=WEATHER_TOOL, tool_choice="auto",
            messages=[{"role": "user",
                       "content": "BTC'nin fiyatı ne? Aracı kullan."}])
        calls = getattr(r.choices[0].message, "tool_calls", None)
        if not calls:
            raise RuntimeError("model aracı çağırmadı (tool_calls boş)")
        fn = calls[0].function
        args = json.loads(fn.arguments or "{}")
        return f"{fn.name}({args})"
    _try("araç çağırma", _tools)

    # 3) Yapılandırılmış çıktı
    def _structured():
        r = client.chat.completions.create(
            model=model, max_tokens=200, response_format=DECISION_SCHEMA,
            messages=[{"role": "user",
                       "content": "BTC yükseliş trendinde. Kararını JSON ver."}])
        data = json.loads(r.choices[0].message.content)
        if "rating" not in data:
            raise RuntimeError(f"şemaya uymayan cevap: {data}")
        return f"rating={data['rating']}, confidence={data.get('confidence')}"
    _try("yapılandırılmış çıktı", _structured)

    return {"model": model, "results": results}


def print_report(model: Optional[str] = None) -> bool:
    """İnsan okunur rapor; kurul için uygun mu sonucunu döner."""
    line = "─" * 66
    model = model or config.LLM_DEEP_MODEL
    print(f"\n{line}\n  LLM UYGUNLUK TESTİ\n{line}")
    print(f"  Model    : {model}")
    print(f"  Sağlayıcı: {config.LLM_BACKEND_URL or 'varsayılan'}")
    if not config.LLM_API_KEY:
        print("\n  ⚠️  LLM anahtarı yok — .env içine OPENROUTER_API_KEY ekle.\n")
        return False

    out = run_checks(model)
    print()
    for name, r in out["results"].items():
        mark = "✅" if r["ok"] else "❌"
        print(f"  {mark} {name:<22} [{r['sec']:.1f} sn]  {r['detail']}")

    chat_ok = out["results"]["sohbet"]["ok"]
    tools_ok = out["results"]["araç çağırma"]["ok"]
    struct_ok = out["results"]["yapılandırılmış çıktı"]["ok"]

    print(f"\n{line}")
    if not chat_ok:
        print("  SONUÇ: model çalışmıyor. Model adını ve anahtarı kontrol et.")
    elif not tools_ok:
        print("  SONUÇ: ARAÇ ÇAĞIRMA YOK -> bu model kurul için KULLANILAMAZ.")
        print("         Analistler veriyi araçlarla çekiyor; onsuz rapor üretemezler.")
    elif not struct_ok:
        print("  SONUÇ: kullanılabilir ama yapılandırılmış çıktı yok.")
        print("         Karar ayrıştırması serbest metne düşer, ara sıra REVIEW çıkar.")
    else:
        print("  SONUÇ: üç yetenek de çalışıyor — kurul için uygun.")
    if "free" in model.lower() or ":free" in model.lower():
        print("\n  ⚠️  ÜCRETSİZ MODEL: OpenRouter ücretsiz katmanının günlük istek")
        print("     sınırı vardır ve ortak havuz tıkanınca 429 alırsın. Bir kurul")
        print("     toplantısı ~20-25 çağrı; 2 sembol × saatlik = günde ~1000 çağrı.")
        print("     Sınıra takılırsan AGENT_INTERVAL_MINUTES'i büyüt.")
    print(f"{line}\n")
    return chat_ok and tools_ok


if __name__ == "__main__":
    import sys
    print_report(sys.argv[1] if len(sys.argv) > 1 else None)
