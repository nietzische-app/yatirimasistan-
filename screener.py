"""
screener.py
-----------
Çok sayıda coini UCUZA tarar, kurulun hangi birkaçı üzerinde düşüneceğine
karar verir.

Neden gerekli: bir kurul toplantısı ~0.12 $. 500 coini saatte bir konuşmak
ayda ~14.000 $ eder. Oysa fiyat/indikatör taraması bedava (yalnızca borsa
API'si). Bu yüzden iki kademe:

    1. TARAMA  (bedava, dakikalar) : onlarca coin için RSI, hacim, oynaklık
       ve trend hesaplanır, bir ilgi puanı verilir.
    2. KURUL   (pahalı, dakikalar) : yalnızca en yüksek puanlı N coin için
       yapay zekâ kurulu toplanır.

Puan bileşenleri saklanır; panelde "bu coin neden seçildi" görülebilir.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
import database as db
from bot import MarketData, calculate_ema, calculate_rsi

log = logging.getLogger("screener")


def score_symbol(df: pd.DataFrame, daily_ema: Optional[float] = None) -> dict:
    """
    Bir coinin 'kurulun bakmaya değer bulacağı' puanı (0-1) ve bileşenleri.

    Bileşenler bilerek basit ve açıklanabilir tutuldu — bu bir strateji değil,
    pahalı düşünmeyi nereye harcayacağımıza dair bir önceliklendirme.
    """
    close = df["close"]
    price = float(close.iloc[-1])
    rsi_series = calculate_rsi(close, config.RSI_PERIOD)
    rsi = float(rsi_series.iloc[-2]) if not pd.isna(rsi_series.iloc[-2]) else 50.0

    # 1) Aşırı satım ilgisi: RSI ne kadar düşükse o kadar ilginç (dip alımı)
    oversold = max(0.0, (50.0 - rsi) / 50.0)
    # 2) Aşırı alım ilgisi: açık pozisyon varsa çıkış konuşulmalı
    overbought = max(0.0, (rsi - 50.0) / 50.0)

    # 3) Hacim patlaması: son mumun hacmi ortalamanın kaç katı
    vol = df["volume"].astype(float)
    vol_mean = float(vol.tail(96).mean()) or 1.0
    vol_ratio = float(vol.iloc[-1]) / vol_mean
    volume_surge = min(1.0, max(0.0, (vol_ratio - 1.0) / 3.0))

    # 4) Oynaklık: son 24 saatteki mutlak değişim
    lookback = min(len(close) - 1, 96)
    change_24h = (price / float(close.iloc[-1 - lookback]) - 1.0) * 100 if lookback > 0 else 0.0
    volatility = min(1.0, abs(change_24h) / 10.0)

    # 5) Trend: fiyat günlük EMA'nın üstünde mi (kurul yükselen trendi sever)
    trend = 0.0
    if daily_ema:
        trend = 1.0 if price > daily_ema else 0.0

    score = (0.40 * oversold + 0.20 * volume_surge
             + 0.20 * volatility + 0.20 * trend)

    return {
        "price": price,
        "rsi": rsi,
        "change_24h": change_24h,
        "volume_ratio": vol_ratio,
        "daily_ema": daily_ema,
        "score": round(score, 4),
        "components": {
            "aşırı_satım": round(oversold, 3),
            "aşırı_alım": round(overbought, 3),
            "hacim_patlaması": round(volume_surge, 3),
            "oynaklık": round(volatility, 3),
            "trend_yukarı": trend,
        },
    }


class Screener:
    """İzleme listesini tarar ve kurul için aday sıralaması üretir."""

    def __init__(self, market: Optional[MarketData] = None) -> None:
        self.market = market or MarketData()

    def scan(self, symbols: Optional[list[str]] = None) -> list[dict]:
        """Her sembol için puan hesaplar; yüksekten düşüğe sıralı döner."""
        symbols = symbols or config.WATCHLIST
        rows: list[dict] = []
        for symbol in symbols:
            try:
                df = self.market.fetch_ohlcv(symbol)
                if df is None or len(df) < config.RSI_PERIOD + 3:
                    continue
                daily_ema = None
                try:
                    daily = self.market.fetch_ohlcv(
                        symbol, config.EMA_TIMEFRAME, config.EMA_PERIOD * 5 + 10)
                    if daily is not None and len(daily) > config.EMA_PERIOD + 1:
                        daily_ema = float(
                            calculate_ema(daily["close"], config.EMA_PERIOD).iloc[-2])
                except Exception as exc:
                    log.debug("[%s] günlük EMA alınamadı: %s", symbol, exc)

                row = score_symbol(df, daily_ema)
                row["symbol"] = symbol
                rows.append(row)
            except Exception as exc:
                log.warning("[%s] taranamadı: %s", symbol, exc)

        rows.sort(key=lambda r: r["score"], reverse=True)
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        db.save_screener_results(rows)
        return rows

    def candidates(self, top_n: Optional[int] = None,
                   symbols: Optional[list[str]] = None,
                   keep=None) -> list[str]:
        """
        Kurulun toplanacağı sembolleri döner (en yüksek puanlı N tane).

        `keep(symbol) -> bool` verilirse eleme SIRALAMADAN SONRA, sayım
        yapılmadan önce uygulanır: kurulun analiz edemediği bir coin birinci
        sırada olsa bile slotu harcamaz, yerine bir sonraki uygun coin çıkar.
        """
        top_n = top_n or config.SCREENER_TOP_N
        sıralı = [r["symbol"] for r in self.scan(symbols)]
        if keep is not None:
            sıralı = [s for s in sıralı if keep(s)]
        return sıralı[:top_n]


def print_scan(rows: list[dict]) -> None:
    line = "─" * 88
    print(f"\n{line}\n  TARAMA SONUCU ({len(rows)} coin)\n{line}")
    print(f"  {'#':>2} {'COIN':<12} {'FİYAT':>12} {'RSI':>6} {'24s %':>8} "
          f"{'HACİM':>7} {'TREND':>6} {'PUAN':>6}")
    print(f"  {'-'*2} {'-'*12} {'-'*12} {'-'*6} {'-'*8} {'-'*7} {'-'*6} {'-'*6}")
    for r in rows:
        c = r["components"]
        print(f"  {r['rank']:>2} {r['symbol']:<12} {r['price']:>12,.4f} {r['rsi']:>6.1f} "
              f"{r['change_24h']:>+7.2f}% {r['volume_ratio']:>6.1f}x "
              f"{'↑' if c['trend_yukarı'] else '↓':>5} {r['score']:>6.3f}")
    if rows:
        try:
            from agents_engine import council_can_analyze
        except Exception:
            council_can_analyze = None
        uygun, elenen = [], []
        for r in rows:
            ok, why = council_can_analyze(r["symbol"]) if council_can_analyze else (True, "")
            (uygun if ok else elenen).append((r["symbol"], why))
        print(f"\n  Kurul şunlar için toplanacak: "
              f"{', '.join(s for s, _ in uygun[:config.SCREENER_TOP_N])}")
        for sym, why in elenen:
            print(f"  ⚠ {sym} elendi: {why}")
    print(f"{line}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    db.init_db()
    print_scan(Screener().scan())
