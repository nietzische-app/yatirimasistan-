"""
LLM uygunluk testi (llm_check.py) için testler.

GERÇEK API ANAHTARI GEREKTİRMEZ: OpenAI istemcisi sahte sınıflarla değiştirilir.

Çalıştırma:
    python tests/test_llm_check.py
"""
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(ROOT, "data", "test_llm_check.db")

import config
import llm_check as lc

config.LLM_API_KEY = "test-key"


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Resp:
    def __init__(self, msg, tokens=42):
        self.choices = [types.SimpleNamespace(message=msg)]
        self.usage = types.SimpleNamespace(total_tokens=tokens)


class _Call:
    def __init__(self, name="get_price", args='{"symbol": "BTC"}'):
        self.function = types.SimpleNamespace(name=name, arguments=args)


def fake_client(tools_ok=True, struct_ok=True, chat_ok=True):
    class C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    if "tools" in kw:
                        if not tools_ok:
                            return _Resp(_Msg(content="araç kullanmadım"))
                        return _Resp(_Msg(tool_calls=[_Call()]))
                    if "response_format" in kw:
                        if not struct_ok:
                            raise RuntimeError("400 response_format desteklenmiyor")
                        return _Resp(_Msg(content=json.dumps(
                            {"rating": "Buy", "confidence": 0.7})))
                    if not chat_ok:
                        raise RuntimeError("401 invalid api key")
                    return _Resp(_Msg(content="merhaba"))
    return C()


def check(**kw):
    lc._client = lambda model=None, k=kw: fake_client(**k)
    return lc.run_checks("test/model")["results"]


# --- 1) Her şey çalışıyor ----------------------------------------------------
r = check()
assert all(v["ok"] for v in r.values()), r
assert "get_price" in r["araç çağırma"]["detail"]
assert "Buy" in r["yapılandırılmış çıktı"]["detail"]
print("✓ üç yetenek de çalışan model 'uygun' raporlanıyor")

# --- 2) Araç çağırma yok -> KULLANILAMAZ ------------------------------------
r = check(tools_ok=False)
assert r["sohbet"]["ok"] and not r["araç çağırma"]["ok"]
assert "tool_calls" in r["araç çağırma"]["detail"]
lc._client = lambda model=None: fake_client(tools_ok=False)
assert lc.print_report("test/model") is False, "araç çağırma yoksa uygun sayılmamalı"
print("✓ araç çağırmayan model KULLANILAMAZ olarak işaretleniyor")

# --- 3) Yapılandırılmış çıktı yok -> kullanılabilir ama uyarılı --------------
r = check(struct_ok=False)
assert r["araç çağırma"]["ok"] and not r["yapılandırılmış çıktı"]["ok"]
lc._client = lambda model=None: fake_client(struct_ok=False)
assert lc.print_report("test/model") is True, "araç çağırma varsa kullanılabilir"
print("✓ yapılandırılmış çıktısı olmayan model uyarıyla kabul ediliyor")

# --- 4) Model/anahtar hatalı -> her şey düşer -------------------------------
r = check(chat_ok=False)
assert not r["sohbet"]["ok"] and "401" in r["sohbet"]["detail"]
lc._client = lambda model=None: fake_client(chat_ok=False)
assert lc.print_report("test/model") is False
print("✓ erişilemeyen model/anahtar yakalanıyor")

# --- 5) Anahtar yokken çökmüyor ---------------------------------------------
_k = config.LLM_API_KEY
config.LLM_API_KEY = ""
assert lc.print_report("test/model") is False
config.LLM_API_KEY = _k
print("✓ anahtar yokken hatasız uyarı veriyor")

print("\nLLM UYGUNLUK TESTLERİ GEÇTİ ✅")
