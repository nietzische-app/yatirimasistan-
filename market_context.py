"""
market_context.py
-----------------
Kurula, tek başına asla bilemeyeceği üç şeyi anlatır:

  1. KENDİ DEFTERİMİZ  — hangi pozisyondayız, girişimiz kaç, kâr/zararımız ne,
     ne kadar nakit var. Kendi pozisyonunu bilmeyen bir trader kötü karar verir.
  2. BİZİM TEKNİK VERİMİZ — kurul günlük Yahoo mumlarına bakıyor; bizde
     Binance'ten gelen 15 dakikalık anlık fiyat, RSI, EMA ve hacim var.
  3. KRİPTO-YERLİ SİNYALLER — funding rate, açık pozisyon (open interest) ve
     Korku & Açgözlülük endeksi. Hisse senedi dünyasının bilanço/F-K'sının
     kriptodaki karşılığı bunlardır.

Üretilen metin TradingAgents'ın instrument_context kanalından bütün ajanların
promptuna giriyor. Her parça en iyi çabadır: veri gelmezse o satır atlanır,
toplantı yine yapılır.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import config
import database as db

log = logging.getLogger("context")

_CACHE: dict[str, tuple[float, object]] = {}


def _num(value, fmt: str = ",.2f", default: str = "?") -> str:
    """None/bozuk değerler f-string'i patlatmasın diye güvenli biçimleyici."""
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return default


def _age_minutes(ts: Optional[str]) -> Optional[float]:
    """Veritabanındaki UTC zaman damgasının kaç dakika önce yazıldığı."""
    if not ts:
        return None
    try:
        yazılan = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - yazılan).total_seconds() / 60.0


def _cached(key: str, ttl: int, producer):
    """Dış servisleri her toplantıda yeniden çağırmamak için basit önbellek."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = producer()
    except Exception as exc:
        log.debug("%s alınamadı: %s", key, exc)
        value = None
    _CACHE[key] = (now, value)
    return value


# ==========================================================================
# 1) PORTFÖY DURUMU
# ==========================================================================
def portfolio_section(symbol: str, broker=None) -> list[str]:
    """Bu sembolde neredeyiz ve hesabın genel durumu ne?"""
    lines: list[str] = []

    if broker is not None:
        try:
            acc = broker.account()
            pos = broker.position_for(symbol)
            lines.append(
                f"Hesap: {_num(acc.get('equity'), ',.0f')} "
                f"{acc.get('currency') or 'USD'} portföy, "
                f"{_num(acc.get('cash'), ',.0f')} nakit, "
                f"{_num(acc.get('buying_power'), ',.0f')} alım gücü."
            )
            if pos:
                lines.append(
                    f"BU VARLIKTA AÇIK POZİSYONUMUZ VAR: {_num(pos.get('qty'), ',.6f')} adet, "
                    f"ortalama giriş {_num(pos.get('avg_entry_price'))}, "
                    f"güncel {_num(pos.get('current_price'))}, "
                    f"gerçekleşmemiş K/Z {_num(pos.get('unrealized_pl'), '+,.2f')} "
                    f"({_num(pos.get('unrealized_plpc'), '+.2f')}%)."
                )
                order = db.last_open_broker_order(symbol)
                if order and (order.get("stop_loss") or order.get("take_profit")):
                    lines.append(
                        f"Bu pozisyon için kâr al {_num(order.get('take_profit'))}, "
                        f"stop {_num(order.get('stop_loss'))} seviyesinde."
                    )
                lines.append(
                    "Karar verirken bunu dikkate al: pozisyonu KORUMAK, ARTIRMAK "
                    "veya KAPATMAK arasında seçim yapıyorsun; sıfırdan alım değil."
                )
            else:
                lines.append("Bu varlıkta açık pozisyonumuz YOK; sıfırdan giriş değerlendiriliyor.")
        except Exception as exc:
            log.debug("Portföy bağlamı alınamadı: %s", exc)
    else:
        try:
            stats = db.get_stats()
            lines.append(f"Sanal hesap: {_num(stats.get('balance'), ',.0f')} "
                         f"{config.QUOTE_CURRENCY} nakit, "
                         f"{stats.get('open_positions', 0)} açık pozisyon.")
            if db.has_open_position(symbol):
                pos = db.get_open_positions(symbol)[0]
                lines.append(f"BU VARLIKTA POZİSYONDAYIZ: giriş {_num(pos['entry_price'])}, "
                             f"kâr al {_num(pos['take_profit'])}, "
                             f"stop {_num(pos['stop_loss'])}. Karar; pozisyonu KORUMAK "
                             f"veya KAPATMAK üzerine, sıfırdan alım değil.")
            else:
                lines.append("Bu varlıkta açık pozisyonumuz YOK; sıfırdan giriş değerlendiriliyor.")
        except Exception as exc:
            log.debug("Sanal defter bağlamı alınamadı: %s", exc)

    # Kurulun geçmiş kararları — aynı şeyi tekrar tekrar söylüyor muyuz?
    try:
        runs = [r for r in db.get_agent_runs(limit=6, symbol=symbol) if r["status"] == "OK"]
    except Exception:
        runs = []
    if runs:
        özet = ", ".join(str(r["rating"]) for r in runs[:4] if r.get("rating"))
        if özet:
            lines.append(f"Bu varlık için son kurul kararlarımız (yeniden eskiye): {özet}.")
    return lines


# ==========================================================================
# 2) BİZİM TEKNİK GÖRÜNTÜMÜZ (Binance, 15 dakikalık)
# ==========================================================================
def technical_section(symbol: str) -> list[str]:
    """Botun canlı olarak gördüğü fiyat ve indikatörler."""
    lines: list[str] = []
    try:
        market = db.get_market().get(symbol)
        scan = {r["symbol"]: r for r in db.get_screener_results(limit=100)}.get(symbol)
    except Exception as exc:
        log.debug("Teknik bağlam alınamadı: %s", exc)
        return lines

    yaş = _age_minutes(market.get("updated_at")) if market else None
    if yaş is not None and yaş > config.CONTEXT_MAX_AGE_MINUTES:
        # Bayat veriyi "anlık fiyat" diye sunmak, ajanlara yanlış şeye
        # güvenmelerini söylemektir. Aday listesinden düşen semboller
        # güncellenmiyor; böyle bir satır saatler öncesine ait olabilir.
        log.info("[%s] piyasa verisi %.0f dk eski; teknik bağlam atlandı.", symbol, yaş)
        market = None

    if market and market.get("price"):
        parts = [f"Binance anlık fiyat {_num(market['price'], ',.4f')}"]
        if market.get("rsi") is not None:
            parts.append(f"{config.TIMEFRAME} RSI({config.RSI_PERIOD}) "
                         f"{_num(market['rsi'], '.1f')}")
        if market.get("ema") is not None:
            trend = "ÜSTÜNDE" if market["price"] > market["ema"] else "ALTINDA"
            parts.append(f"{config.EMA_PERIOD} periyotluk ({config.EMA_TIMEFRAME}) EMA "
                         f"{_num(market['ema'])} — fiyat bunun {trend}")
        lines.append("Kendi canlı verimiz: " + ", ".join(parts) + ".")

    # Tarama tasarımı gereği SCREENER_INTERVAL_MINUTES'ta bir yenilenir; canlı
    # fiyat eşiğiyle ölçmek onu neredeyse her zaman elerdi. İki tarama
    # kaçırılmışsa artık güvenilmez sayıyoruz.
    tarama_eşiği = max(config.CONTEXT_MAX_AGE_MINUTES,
                       config.SCREENER_INTERVAL_MINUTES * 2)
    if scan and (_age_minutes(scan.get("ts")) or 0) > tarama_eşiği:
        scan = None

    if scan:
        lines.append(
            f"Tarama: son 24 saatte {_num(scan.get('change_24h'), '+.2f')}%, "
            f"hacim ortalamanın {_num(scan.get('volume_ratio'), '.1f')} katı, "
            f"ilgi puanı {_num(scan.get('score'), '.2f')} (sıra {scan.get('rank', '?')})."
        )
    if lines:
        lines.append("Bu veriler borsadan canlı gelir ve günlük mum verisinden "
                     "daha günceldir; çelişki olursa bunlara güven.")
    return lines


# ==========================================================================
# 3) KRİPTO-YERLİ SİNYALLER
# ==========================================================================
def fear_greed() -> Optional[dict]:
    """Kripto Korku & Açgözlülük endeksi (ücretsiz, anahtar gerekmez)."""
    def _fetch():
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=10) as r:
            item = json.loads(r.read())["data"][0]
        return {"value": int(item["value"]), "label": item.get("value_classification", "")}
    return _cached("fng", 3600, _fetch)


_FUTURES = None
_FUTURES_LOCK = threading.Lock()


def _futures_exchange():
    """
    Vadeli borsa nesnesi süreç başına bir kez kurulur.

    Her sembolde yeniden kurmak, her seferinde ~2 MB'lık exchangeInfo listesini
    yeniden indirmek demek; 12 coinlik izleme listesinde bu boşa giden trafik.
    """
    global _FUTURES
    with _FUTURES_LOCK:
        if _FUTURES is None:
            import ccxt
            _FUTURES = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15000})
        return _FUTURES


def derivatives(symbol: str) -> Optional[dict]:
    """
    Vadeli piyasa sinyalleri: funding rate ve açık pozisyon.

    Funding pozitifse uzun taraf baskın (aşırı ısınma işareti olabilir),
    negatifse kısa taraf baskın. Kriptoda konumlanmayı gösteren en doğrudan
    veridir; hisse senedindeki bilançonun yerini tutar.
    """
    def _fetch():
        ex = _futures_exchange()
        base = symbol.split("/")[0]
        pair = f"{base}/USDT:USDT"
        out: dict = {}
        try:
            fr = ex.fetch_funding_rate(pair)
            out["funding_rate"] = fr.get("fundingRate")
        except Exception as exc:
            log.debug("funding alınamadı (%s): %s", pair, exc)
        try:
            oi = ex.fetch_open_interest(pair)
            out["open_interest"] = oi.get("openInterestAmount") or oi.get("openInterestValue")
        except Exception as exc:
            log.debug("open interest alınamadı (%s): %s", pair, exc)
        return out or None
    return _cached(f"deriv:{symbol}", 900, _fetch)


def crypto_section(symbol: str) -> list[str]:
    lines: list[str] = []
    fng = fear_greed()
    if fng:
        lines.append(f"Korku & Açgözlülük endeksi: {fng['value']}/100 ({fng['label']}). "
                     f"Aşırı korku dip, aşırı açgözlülük tepe sinyali olabilir.")
    d = derivatives(symbol)
    if d:
        parts = []
        if d.get("funding_rate") is not None:
            fr = d["funding_rate"] * 100
            yön = "uzun taraf ödüyor (boğa baskın)" if fr > 0 else "kısa taraf ödüyor (ayı baskın)"
            parts.append(f"funding rate %{fr:+.4f} — {yön}")
        if d.get("open_interest"):
            parts.append(f"açık pozisyon {d['open_interest']:,.0f}")
        if parts:
            lines.append("Vadeli piyasa: " + ", ".join(parts) + ".")
    return lines


# ==========================================================================
# BİRLEŞTİRİCİ
# ==========================================================================
def build(symbol: str, broker=None) -> str:
    """Kurula verilecek ek bağlam metni. Hiçbir veri yoksa boş döner."""
    if not config.AGENT_CONTEXT_ENABLED:
        return ""
    blocks: list[tuple[str, list[str]]] = [
        ("PORTFÖY DURUMUMUZ", portfolio_section(symbol, broker)),
        ("CANLI TEKNİK VERİMİZ", technical_section(symbol)),
    ]
    if config.CRYPTO_SIGNALS_ENABLED and config.is_crypto(symbol):
        blocks.append(("KRİPTO PİYASA SİNYALLERİ", crypto_section(symbol)))

    parts = [f"### {başlık}\n" + "\n".join(f"- {satır}" for satır in satırlar)
             for başlık, satırlar in blocks if satırlar]
    if not parts:
        return ""
    return ("\n\n## İŞLETMECİDEN EK BAĞLAM (bu bilgiler senin araçlarında yok)\n\n"
            + "\n\n".join(parts) + "\n")


if __name__ == "__main__":       # python market_context.py BTC/USDT
    import sys
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    sym = sys.argv[1] if len(sys.argv) > 1 else config.SYMBOLS[0]
    print(build(sym) or "(bağlam üretilemedi)")
