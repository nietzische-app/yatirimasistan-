"""
bot.py
------
Paper trading motoru.

Strateji (config.py'den ayarlanır):
    GİRİŞ  : 15m RSI(14) < 30  VE  fiyat > 20 GÜNLÜK EMA     -> AL (LONG)
    ÇIKIŞ  : +%2 kâr al  |  -%1.5 zarar kes  |  15m RSI > 70 -> SAT

Not: RSI sinyal zaman diliminde (config.TIMEFRAME, varsayılan 15m),
EMA trend zaman diliminde (config.EMA_TIMEFRAME, varsayılan 1d) hesaplanır.

Çalıştırma
    python bot.py              # sürekli döngü (panelden Başlat/Durdur ile kontrol edilir)
    python bot.py --once       # tek tur çalışıp çıkar
    python bot.py --force      # panel "durduruldu" olsa bile çalış
    python bot.py --simulate   # internet olmadan sentetik fiyatlarla dene
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

import config
import database as db

try:  # ccxt yoksa offline simülasyon yine de çalışsın
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


# ==========================================================================
# İNDİKATÖRLER
# ==========================================================================
def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. TA-Lib gerektirmez."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Hiç kayıp yoksa RSI = 100, hiç kazanç yoksa RSI = 0
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(avg_gain != 0.0, 0.0)
    return rsi


def calculate_ema(close: pd.Series, period: int = 20) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = calculate_rsi(df["close"], config.RSI_PERIOD)
    df["ema"] = calculate_ema(df["close"], config.EMA_PERIOD)
    return df


# ==========================================================================
# PİYASA VERİSİ
# ==========================================================================
class MarketData:
    """CCXT üzerinden mum verisi çeker. Offline modda sentetik veri üretir."""

    def __init__(self) -> None:
        self.exchange = None
        self._sim_state: dict[str, float] = {}
        if not config.OFFLINE_SIMULATION:
            self.exchange = self._build_exchange()

    @staticmethod
    def _build_exchange():
        if ccxt is None:
            raise RuntimeError(
                "ccxt kurulu değil. `pip install ccxt` çalıştır veya "
                "OFFLINE_SIMULATION=true ile dene."
            )
        params = {
            "enableRateLimit": True,
            "timeout": 20_000,
            "options": {"defaultType": "spot"},
        }
        # Gerçek moda geçildiğinde anahtarlar burada devreye girer.
        if not config.DEMO_MODE:
            if not (config.BINANCE_API_KEY and config.BINANCE_API_SECRET):
                raise RuntimeError(
                    "DEMO_MODE=False ama BINANCE_API_KEY / BINANCE_API_SECRET tanımlı değil."
                )
            params["apiKey"] = config.BINANCE_API_KEY
            params["secret"] = config.BINANCE_API_SECRET

        exchange = getattr(ccxt, config.EXCHANGE_ID)(params)
        if config.USE_TESTNET and hasattr(exchange, "set_sandbox_mode"):
            exchange.set_sandbox_mode(True)
        return exchange

    # ---------------------------------------------------------------- OHLCV
    @staticmethod
    def _pandas_freq(timeframe: str) -> str:
        """'15m' -> '15min', '4h' -> '4h', '1d' -> '1D' (sadece simülasyon için)."""
        unit = timeframe[-1]
        value = timeframe[:-1] or "1"
        return {"m": f"{value}min", "h": f"{value}h",
                "d": f"{value}D", "w": f"{value}W"}.get(unit, "15min")

    def fetch_ohlcv(self, symbol: str, timeframe: Optional[str] = None,
                    limit: Optional[int] = None) -> pd.DataFrame:
        timeframe = timeframe or config.TIMEFRAME
        limit = int(limit or config.CANDLE_LIMIT)

        if config.OFFLINE_SIMULATION:
            return self._simulated_ohlcv(symbol, timeframe, limit)

        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def _simulated_ohlcv(self, symbol: str, timeframe: Optional[str] = None,
                         limit: Optional[int] = None) -> pd.DataFrame:
        """Rastgele yürüyüş: internet olmadan arayüzü/stratejiyi test etmek için."""
        timeframe = timeframe or config.TIMEFRAME
        n = int(limit or config.CANDLE_LIMIT)
        base = self._sim_state.get(symbol) or (65_000.0 if "BTC" in symbol else 3_200.0)
        # Uzun zaman dilimlerinde adım oynaklığı daha yüksek olsun
        vol = 0.0035 if timeframe.endswith("m") else (0.01 if timeframe.endswith("h") else 0.025)
        rng = np.random.default_rng(abs(hash((symbol, timeframe, int(time.time() // 60)))) % (2**32))
        steps = rng.normal(0, vol, n).cumsum()
        close = base * np.exp(steps - steps[-1])   # son mum ~ mevcut fiyat
        if timeframe == config.TIMEFRAME:
            self._sim_state[symbol] = float(close[-1])
        now = pd.Timestamp.now(tz="UTC").floor("min")
        idx = pd.date_range(end=now, periods=n, freq=self._pandas_freq(timeframe))
        df = pd.DataFrame({
            "timestamp": (idx.astype("int64") // 10**6),
            "open": close * (1 + rng.normal(0, 0.0005, n)),
            "high": close * (1 + abs(rng.normal(0, 0.0015, n))),
            "low": close * (1 - abs(rng.normal(0, 0.0015, n))),
            "close": close,
            "volume": abs(rng.normal(100, 20, n)),
            "datetime": idx,
        })
        return df

    # ---------------------------------------------------------------- fiyat
    def fetch_price(self, symbol: str) -> Optional[float]:
        """Tek sembol için son fiyat (panelden hızlı sorgu için)."""
        if config.OFFLINE_SIMULATION:
            return float(self._simulated_ohlcv(symbol)["close"].iloc[-1])
        try:
            return float(self.exchange.fetch_ticker(symbol)["last"])
        except Exception as exc:  # pragma: no cover - ağ hatası
            log.warning("Fiyat alınamadı (%s): %s", symbol, exc)
            return None


# ==========================================================================
# BOT
# ==========================================================================
class TradingBot:
    def __init__(self) -> None:
        db.init_db()
        self.market = MarketData()
        self._last_equity_snapshot = 0.0
        self._trend_cache: dict[str, tuple[float, Optional[float]]] = {}
        mode = "DEMO (sanal para)" if config.DEMO_MODE else "GERÇEK EMİR"
        if config.OFFLINE_SIMULATION:
            mode += " · OFFLINE SİMÜLASYON"
        log.info("Bot hazır | Mod: %s | Semboller: %s", mode, ", ".join(config.SYMBOLS))
        db.set_state("mode", mode)

    # ------------------------------------------------------------- yardımcı
    @staticmethod
    def _position_size(balance: float) -> float:
        size = balance * config.POSITION_SIZE_PCT
        return min(size, config.MAX_POSITION_USDT, balance)

    @staticmethod
    def _in_cooldown(symbol: str) -> bool:
        last = db.last_trade_time(symbol)
        if not last or config.COOLDOWN_MINUTES <= 0:
            return False
        try:
            closed = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - closed < timedelta(minutes=config.COOLDOWN_MINUTES)

    # ------------------------------------------------------ emir uygulayıcı
    def _place_real_order(self, symbol: str, side: str, amount: float) -> dict:
        """
        DEMO_MODE=False olduğunda gerçek Binance emri gönderir.
        Dönüş: {"price": ..., "amount": ..., "fee": ...}
        """
        exchange = self.market.exchange
        if exchange is None:
            raise RuntimeError("Gerçek emir için borsa bağlantısı yok.")
        amount = float(exchange.amount_to_precision(symbol, amount))
        order = exchange.create_order(symbol, "market", side, amount)
        filled = float(order.get("filled") or amount)
        price = float(order.get("average") or order.get("price") or 0.0)
        if not price:
            price = float(exchange.fetch_ticker(symbol)["last"])
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or filled * price * config.FEE_RATE)
        log.info("GERÇEK EMİR gönderildi: %s %s %.8f @ %.2f", side.upper(), symbol, filled, price)
        return {"price": price, "amount": filled, "fee": fee}

    # ------------------------------------------------------------- AL / SAT
    def open_trade(self, symbol: str, price: float, rsi: float, ema: float) -> Optional[int]:
        balance = db.get_balance()
        budget = self._position_size(balance)
        if budget < config.MIN_POSITION_USDT:
            log.info("[%s] Bakiye yetersiz (%.2f), alım atlandı.", symbol, balance)
            return None

        amount = budget / price
        entry_price = price
        entry_fee = budget * config.FEE_RATE

        if not config.DEMO_MODE:
            result = self._place_real_order(symbol, "buy", amount)
            entry_price = result["price"]
            amount = result["amount"]
            entry_fee = result["fee"]
            budget = amount * entry_price + entry_fee
        else:
            # Sanal alımda bütçenin komisyon düşülmüş kısmıyla coin alınır.
            amount = (budget - entry_fee) / entry_price

        take_profit = entry_price * (1 + config.TAKE_PROFIT_PCT)
        stop_loss = entry_price * (1 - config.STOP_LOSS_PCT)
        reason = (f"RSI {rsi:.1f} < {config.RSI_BUY_THRESHOLD:g} ve fiyat "
                  f"EMA{config.EMA_PERIOD}({config.EMA_TIMEFRAME}) üstünde")

        pos_id = db.open_position(
            symbol=symbol, amount=amount, entry_price=entry_price, cost=budget,
            entry_fee=entry_fee, take_profit=take_profit, stop_loss=stop_loss,
            entry_rsi=rsi, entry_ema=ema, entry_reason=reason, is_demo=config.DEMO_MODE,
        )
        msg = (f"AL  {symbol} | {amount:.6f} @ {entry_price:,.2f} "
               f"(~{budget:,.2f} {config.QUOTE_CURRENCY}) | TP {take_profit:,.2f} / SL {stop_loss:,.2f} | {reason}")
        log.info(msg)
        db.add_log("BUY", msg, symbol)
        return pos_id

    def close_trade(self, position: dict, price: float, reason: str) -> dict:
        exit_price = price
        if not config.DEMO_MODE:
            result = self._place_real_order(position["symbol"], "sell", position["amount"])
            exit_price = result["price"]

        trade = db.close_position(position["id"], exit_price, reason)
        msg = (f"SAT {trade['symbol']} | {trade['amount']:.6f} @ {exit_price:,.2f} | "
               f"PnL {trade['pnl']:+,.2f} {config.QUOTE_CURRENCY} ({trade['pnl_pct']:+.2f}%) | "
               f"Sebep: {reason} | Yeni bakiye {trade['balance_after']:,.2f}")
        log.info(msg)
        db.add_log("SELL" if trade["pnl"] >= 0 else "STOP", msg, trade["symbol"])
        return trade

    # --------------------------------------------------------- trend filtresi
    def trend_ema(self, symbol: str) -> Optional[float]:
        """
        config.EMA_TIMEFRAME zaman diliminde EMA(config.EMA_PERIOD).
        Varsayılan "1d" olduğu için bu "20 GÜNLÜK EMA"dır.
        Her döngüde borsayı yormamak adına EMA_REFRESH_SECONDS boyunca cache'lenir.
        """
        cached = self._trend_cache.get(symbol)
        if cached and time.time() - cached[0] < config.EMA_REFRESH_SECONDS:
            return cached[1]

        need = config.EMA_PERIOD * 5 + 10          # EMA'nın oturması için yeterli mum
        df = self.market.fetch_ohlcv(symbol, config.EMA_TIMEFRAME, need)
        if df is None or len(df) < config.EMA_PERIOD + 2:
            return cached[1] if cached else None

        series = calculate_ema(df["close"], config.EMA_PERIOD)
        value = float(series.iloc[-2])             # oluşmakta olan mumu atla
        self._trend_cache[symbol] = (time.time(), value)
        return value

    # ------------------------------------------------------------- strateji
    def check_exit(self, position: dict, price: float, rsi: Optional[float]) -> Optional[str]:
        """Pozisyon kapatılmalı mı? Kapatılacaksa sebebi döner."""
        if price >= float(position["take_profit"]):
            return f"KÂR AL (%{config.TAKE_PROFIT_PCT * 100:g})"
        if price <= float(position["stop_loss"]):
            return f"STOP-LOSS (%{config.STOP_LOSS_PCT * 100:g})"
        if rsi is not None and rsi > config.RSI_SELL_THRESHOLD:
            return f"RSI {rsi:.1f} > {config.RSI_SELL_THRESHOLD:g}"
        return None

    def check_entry(self, symbol: str, price: float, rsi: Optional[float],
                    ema: Optional[float]) -> bool:
        if rsi is None or ema is None:
            return False
        if db.has_open_position(symbol):
            return False
        if len(db.get_open_positions()) >= config.MAX_OPEN_POSITIONS:
            return False
        if self._in_cooldown(symbol):
            return False
        trend_ok = price > ema * (1 - config.EMA_TOLERANCE_PCT)
        return rsi < config.RSI_BUY_THRESHOLD and trend_ok

    # ------------------------------------------------------------ ana turlar
    def process_symbol(self, symbol: str) -> None:
        df = self.market.fetch_ohlcv(symbol)
        if df is None or len(df) < max(config.RSI_PERIOD, config.EMA_PERIOD) + 2:
            log.warning("[%s] Yeterli mum verisi yok.", symbol)
            return

        df = add_indicators(df)
        price = float(df["close"].iloc[-1])          # oluşmakta olan mumun son fiyatı
        closed = df.iloc[-2]                          # sinyaller kapanmış mumdan okunur
        rsi = None if pd.isna(closed["rsi"]) else float(closed["rsi"])

        # Trend filtresi: EMA farklı bir zaman diliminden gelir (varsayılan: 20 günlük)
        if config.EMA_TIMEFRAME == config.TIMEFRAME:
            ema = None if pd.isna(closed["ema"]) else float(closed["ema"])
        else:
            try:
                ema = self.trend_ema(symbol)
            except Exception as exc:
                log.warning("[%s] Trend EMA alınamadı: %s", symbol, exc)
                ema = None

        signal = "BEKLE"

        # 1) Önce açık pozisyonlar için çıkış kontrolü
        for position in db.get_open_positions(symbol):
            reason = self.check_exit(position, price, rsi)
            if reason:
                self.close_trade(position, price, reason)
                signal = "SAT"

        # 2) Sonra yeni giriş kontrolü
        if signal != "SAT" and self.check_entry(symbol, price, rsi, ema):
            self.open_trade(symbol, price, rsi, ema)
            signal = "AL"
        elif signal != "SAT" and db.has_open_position(symbol):
            signal = "POZİSYONDA"

        db.update_market(symbol, price, rsi, ema, signal)
        log.info(
            "[%s] fiyat %s | RSI %s | EMA%s(%s) %s | %s",
            symbol,
            f"{price:,.2f}",
            f"{rsi:.1f}" if rsi is not None else "-",
            config.EMA_PERIOD,
            config.EMA_TIMEFRAME,
            f"{ema:,.2f}" if ema is not None else "-",
            signal,
        )

    def snapshot_equity(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_equity_snapshot < config.EQUITY_SNAPSHOT_SECONDS:
            return
        prices = {s: m["price"] for s, m in db.get_market().items() if m.get("price")}
        stats = db.get_stats(prices)
        db.record_equity(stats["balance"], stats["equity"])
        self._last_equity_snapshot = now

    def run_once(self) -> None:
        """Tüm sembolleri bir kez tarar."""
        for symbol in config.SYMBOLS:
            try:
                self.process_symbol(symbol)
            except Exception as exc:
                log.exception("[%s] hata: %s", symbol, exc)
                db.add_log("ERROR", f"{symbol}: {exc}", symbol)
                db.set_state("last_error", f"{symbol}: {exc}")
        self.snapshot_equity()
        db.set_state("last_run", db.utcnow())

    def run_forever(self, force: bool = False) -> None:
        """Sonsuz döngü. `force` değilse panelin Başlat/Durdur anahtarına uyar."""
        log.info("Döngü başladı (%s sn aralık). Durdurmak için Ctrl+C.",
                 config.LOOP_INTERVAL_SECONDS)
        if force:
            db.set_bot_running(True)
        try:
            while True:
                if force or db.is_bot_running():
                    self.run_once()
                else:
                    db.set_state("heartbeat", db.utcnow())
                time.sleep(config.LOOP_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Kullanıcı durdurdu.")
            db.set_bot_running(False)


# ==========================================================================
# ARKA PLAN THREAD'İ (Streamlit paneli bunu kullanır)
# ==========================================================================
class BotRunner(threading.Thread):
    """
    Panelin içinde arka planda dönen tek bir bot thread'i.
    Veritabanındaki `running` anahtarı 1 ise tur atar, değilse bekler.
    """

    def __init__(self) -> None:
        super().__init__(name="bot-runner", daemon=True)
        self._stop_event = threading.Event()
        self.last_error: Optional[str] = None
        self.bot: Optional[TradingBot] = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover - thread gövdesi
        while not self._stop_event.is_set():
            try:
                if db.is_bot_running():
                    if self.bot is None:
                        self.bot = TradingBot()
                    self.bot.run_once()
                    self.last_error = None
                db.set_state("heartbeat", db.utcnow())
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Bot döngü hatası: %s", exc)
                db.add_log("ERROR", f"Döngü hatası: {exc}")
                db.set_state("last_error", str(exc))
            # Uzun beklemeyi parçalayarak durdurmaya hızlı tepki ver
            waited = 0.0
            while waited < config.LOOP_INTERVAL_SECONDS and not self._stop_event.is_set():
                time.sleep(0.5)
                waited += 0.5


# ==========================================================================
# CLI
# ==========================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Binance Paper Trading Bot")
    parser.add_argument("--once", action="store_true", help="Tek tur çalıştır ve çık")
    parser.add_argument("--force", action="store_true",
                        help="Panel durdurulmuş olsa bile çalıştır")
    parser.add_argument("--simulate", action="store_true",
                        help="İnternet olmadan sentetik fiyatlarla çalış")
    parser.add_argument("--reset", action="store_true",
                        help="Sanal bakiyeyi sıfırlayıp çık")
    args = parser.parse_args()

    if args.simulate:
        config.OFFLINE_SIMULATION = True

    db.init_db()

    if args.reset:
        db.reset_account()
        print(f"Bakiye sıfırlandı: {config.INITIAL_BALANCE:,.2f} {config.QUOTE_CURRENCY}")
        return

    bot = TradingBot()
    if args.once:
        db.set_state("running", "1" if args.force else db.get_state("running", "0"))
        bot.run_once()
        bot.snapshot_equity(force=True)
        print(db.get_stats())
    else:
        bot.run_forever(force=args.force)


if __name__ == "__main__":
    main()
