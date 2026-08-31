"""
bot.py
------
Paper trading motoru.

Karar mekanizması: TradingAgents çoklu ajan kurulu (agents_engine.py).
Basit RSI/EMA al-sat kuralları KALDIRILMIŞTIR; indikatörler yalnızca panelde
gösterilen piyasa görüntüsü ve backtest için hesaplanır, karar vermez.

İki hızlı/yavaş katman:
    HIZLI (30 sn)  : fiyat takibi, açık pozisyonların TP/SL kontrolü,
                     kurulun verdiği bekleyen kararların uygulanması
    YAVAŞ (60 dk)  : kurul toplanır (arka planda, hızlı döngüyü bloklamaz)

Kurul kararı: Buy/Overweight -> AL, Sell/Underweight -> SAT, Hold -> BEKLE.
Pozisyon büyüklüğü nota göre ölçeklenir; stop-loss ajan önerirse ondan,
yoksa config.STOP_LOSS_PCT'ten alınır.

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

import agents_engine
import alpaca_execution
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
        self._council_threads: dict[str, threading.Thread] = {}
        self.council = agents_engine.get_council()
        self.broker: Optional[alpaca_execution.AlpacaExecutor] = None
        ok, reason = agents_engine.AgentCouncil.readiness()
        log.info("Karar motoru: %s", "TradingAgents kurulu" if ok else f"KAPALI — {reason}")
        db.set_state("decision_engine", "agents" if ok else f"kapalı: {reason}")

        self.backend = alpaca_execution.backend_name()
        if self.backend == "alpaca":
            ready, why = alpaca_execution.AlpacaExecutor.readiness()
            if not ready:
                log.warning("Alpaca seçildi ama kullanılamıyor (%s); dahili deftere dönülüyor.", why)
                self.backend = "internal"
            else:
                self.broker = alpaca_execution.get_executor()
        log.info("Emir yürütme: %s", "Alpaca " + ("PAPER" if config.ALPACA_PAPER else "CANLI")
                 if self.backend == "alpaca" else "dahili sanal defter")
        db.set_state("execution_backend", self.backend)
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
    def open_trade(self, symbol: str, price: float, size_factor: float = 1.0,
                   stop_price: Optional[float] = None, reason: str = "") -> Optional[int]:
        """Kurul AL dediğinde pozisyon açar. `size_factor` nota göre ölçek (0-1)."""
        balance = db.get_balance()
        budget = self._position_size(balance) * max(0.0, min(1.0, size_factor))
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
        # Stop: ajanların önerisi geçerliyse o, değilse config'teki sabit oran
        stop_loss = stop_price if stop_price else entry_price * (1 - config.STOP_LOSS_PCT)
        market = db.get_market().get(symbol, {})

        pos_id = db.open_position(
            symbol=symbol, amount=amount, entry_price=entry_price, cost=budget,
            entry_fee=entry_fee, take_profit=take_profit, stop_loss=stop_loss,
            entry_rsi=market.get("rsi"), entry_ema=market.get("ema"),
            entry_reason=reason or "Kurul kararı", is_demo=config.DEMO_MODE,
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

    # ---------------------------------------------------- pozisyon koruması
    def check_exit(self, position: dict, price: float) -> Optional[str]:
        """
        Açık pozisyonun korumaları. Bu bir al-sat stratejisi değil, risk
        yönetimidir: kâr hedefi ve stop seviyesi pozisyon açılırken sabitlenir
        (stop'u kurul önerdiyse ondan gelir).
        """
        if price >= float(position["take_profit"]):
            return f"KÂR AL (%{config.TAKE_PROFIT_PCT * 100:g})"
        if price <= float(position["stop_loss"]):
            return "STOP-LOSS"
        return None

    # -------------------------------------------------------- kurul kararı
    def maybe_convene(self, symbol: str, price: float) -> None:
        """Zamanı geldiyse kurulu ARKA PLANDA toplar; hızlı döngü beklemez."""
        if config.DECISION_ENGINE != "agents":
            return
        thread = self._council_threads.get(symbol)
        if thread is not None and thread.is_alive():
            return                              # bu sembol için toplantı sürüyor
        if not agents_engine.AgentCouncil.due(symbol):
            return
        ok, reason = agents_engine.AgentCouncil.readiness()
        if not ok:
            return

        def _run():
            try:
                self.council.analyze(symbol, price)
            except Exception as exc:
                log.exception("[%s] kurul çalıştırılamadı: %s", symbol, exc)
                db.add_log("ERROR", f"{symbol}: kurul çalıştırılamadı — {exc}", symbol)

        t = threading.Thread(target=_run, name=f"council-{symbol}", daemon=True)
        self._council_threads[symbol] = t
        t.start()

    def has_position(self, symbol: str) -> bool:
        """Açık pozisyon kontrolü — hangi yürütme arkasını kullanıyorsak ondan."""
        if self.backend == "alpaca" and self.broker is not None:
            try:
                return self.broker.position_for(symbol) is not None
            except Exception as exc:
                log.warning("[%s] Alpaca pozisyonu okunamadı: %s", symbol, exc)
                return True          # emin değilsek yeni emir GÖNDERME
        return db.has_open_position(symbol)

    def apply_pending_decisions(self, symbol: str, price: float) -> None:
        """Kurulun bitirdiği ama henüz uygulanmamış kararları emre çevirir."""
        for run in db.get_agent_runs(limit=5, symbol=symbol):
            if run["status"] != "OK" or run["executed"]:
                continue
            action = run["action"]
            has_position = self.has_position(symbol)
            size = run["size_factor"] or 1.0
            stop = run["proposed_stop"] or price * (1 - config.STOP_LOSS_PCT)
            target = price * (1 + config.TAKE_PROFIT_PCT)

            if action == "BUY" and not has_position:
                if self.backend == "alpaca":
                    self.broker.buy(symbol,
                                    self.broker.position_notional(size),
                                    take_profit=target, stop_loss=stop,
                                    agent_run_id=run["id"])
                elif len(db.get_open_positions()) >= config.MAX_OPEN_POSITIONS:
                    db.add_log("AGENT", f"{symbol}: AL kararı atlandı (pozisyon limiti)", symbol)
                else:
                    self.open_trade(symbol, price, size_factor=size,
                                    stop_price=run["proposed_stop"],
                                    reason=f"Kurul kararı: {run['rating']}")

            elif action == "SELL" and has_position and config.AGENT_EXIT_ON_SELL:
                if self.backend == "alpaca":
                    self.broker.sell(symbol, agent_run_id=run["id"],
                                     reason=f"Kurul kararı: {run['rating']}")
                else:
                    for pos in db.get_open_positions(symbol):
                        self.close_trade(pos, price, f"Kurul kararı: {run['rating']}")

            db.mark_agent_run_executed(run["id"])

    # ------------------------------------------------------------ ana turlar
    def process_symbol(self, symbol: str) -> None:
        """Hızlı döngü: fiyat + korumalar + kurul kararlarının uygulanması."""
        df = self.market.fetch_ohlcv(symbol)
        if df is None or len(df) < max(config.RSI_PERIOD, config.EMA_PERIOD) + 2:
            log.warning("[%s] Yeterli mum verisi yok.", symbol)
            return

        # İndikatörler yalnızca panelde gösterilen piyasa görüntüsü için;
        # al/sat kararına GİRMEZ (karar kurulun).
        df = add_indicators(df)
        price = float(df["close"].iloc[-1])
        closed = df.iloc[-2]
        rsi = None if pd.isna(closed["rsi"]) else float(closed["rsi"])
        if config.EMA_TIMEFRAME == config.TIMEFRAME:
            ema = None if pd.isna(closed["ema"]) else float(closed["ema"])
        else:
            try:
                ema = self.trend_ema(symbol)
            except Exception as exc:
                log.warning("[%s] Trend EMA alınamadı: %s", symbol, exc)
                ema = None

        # 1) Açık pozisyonun korumaları (kâr al / stop)
        if self.backend == "alpaca":
            # Hissede bracket emri borsada durur; kriptoda koruma bizde.
            try:
                reason = self.broker.check_protective_exit(symbol, price)
                if reason:
                    self.broker.sell(symbol, reason=reason)
            except Exception as exc:
                log.warning("[%s] Alpaca koruma kontrolü başarısız: %s", symbol, exc)
        else:
            for position in db.get_open_positions(symbol):
                reason = self.check_exit(position, price)
                if reason:
                    self.close_trade(position, price, reason)

        # 2) Kurulun tamamlanmış kararlarını uygula
        self.apply_pending_decisions(symbol, price)

        # 3) Zamanı geldiyse yeni toplantıyı arka planda başlat
        self.maybe_convene(symbol, price)

        last = db.get_agent_runs(limit=1, symbol=symbol)
        signal = "POZİSYONDA" if self.has_position(symbol) else (
            last[0]["action"] if last and last[0]["status"] == "OK" else "BEKLE")
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
                # Panelin "motor ayakta mı?" göstergesi: her turda yazılır,
                # bot duraklatılmış olsa bile.
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
# DURUM ÖZETİ
# ==========================================================================
def print_status() -> None:
    """`python bot.py --status` — tek bakışta sistemin neresinde olduğumuz."""
    from datetime import datetime, timezone

    db.init_db()
    line = "─" * 66

    def age(ts: Optional[str]) -> str:
        if not ts:
            return "hiç"
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return ts
        mins = (datetime.now(timezone.utc) - t).total_seconds() / 60
        return f"{mins:.0f} dk önce" if mins < 90 else f"{mins / 60:.1f} saat önce"

    print(f"\n{line}\n  DURUM ÖZETİ\n{line}")
    print(f"  Bot çalışıyor mu   : {'EVET' if db.is_bot_running() else 'HAYIR (panelden Başlat)'}")
    print(f"  Son tarama         : {age(db.get_state('last_run'))}")
    print(f"  Mod                : {'DEMO (sanal para)' if config.DEMO_MODE else 'GERÇEK EMİR'}")

    ok, reason = agents_engine.AgentCouncil.readiness()
    halted = agents_engine.AgentCouncil.halted()
    print(f"\n  Karar motoru       : "
          f"{'DURDURULDU' if halted else ('TradingAgents kurulu' if ok else 'KAPALI')}")
    if halted:
        print(f"    ├─ sebep         : {halted}")
        print(f"    └─ düzeltince    : python bot.py --resume-council")
    if not ok:
        print(f"    └─ {reason}")
    elif not halted:
        print(f"    ├─ analistler    : {', '.join(config.AGENT_ANALYSTS)}")
        print(f"    ├─ model         : {config.LLM_DEEP_MODEL}")
        print(f"    ├─ sıklık        : {config.AGENT_INTERVAL_MINUTES} dk/sembol, "
              f"günde en fazla {config.AGENT_MAX_RUNS_PER_DAY}")
        print(f"    ├─ bugünkü koşu  : {db.agent_runs_today()}")
        credit = agents_engine.openrouter_credit()
        if credit:
            bal = credit.get("account_balance")
            if bal is not None:
                warn = "  ⚠️ KREDİ BİTMİŞ" if bal <= 0.01 else ""
                print(f"    ├─ hesap bakiye  : {bal:.2f} $ "
                      f"(yüklenen {credit['account_total']:.2f}, "
                      f"harcanan {credit['account_used']:.2f}){warn}")
            elif credit.get("account_used") is not None:
                print(f"    ├─ hesap harcama : {credit['account_used']:.2f} $")
            if credit.get("key_remaining") is not None:
                print(f"    └─ anahtar limiti: kalan {credit['key_remaining']:.2f} $")
            else:
                print(f"    └─ anahtar limiti: yok (harcama {credit.get('key_usage') or 0:.2f} $)")
        else:
            print(f"    └─ OpenRouter    : kredi bilgisi okunamadı")

    backend = alpaca_execution.backend_name()
    print(f"\n  Emir yürütme       : ", end="")
    if backend == "alpaca":
        aok, awhy = alpaca_execution.AlpacaExecutor.readiness()
        print(f"Alpaca {'PAPER' if config.ALPACA_PAPER else 'CANLI'}")
        if not aok:
            print(f"    └─ KULLANILAMIYOR: {awhy}")
        else:
            try:
                ex = alpaca_execution.get_executor()
                acc = ex.account()
                print(f"    ├─ hesap         : {acc['status']} | equity "
                      f"{acc['equity']:,.2f} {acc['currency']} | nakit {acc['cash']:,.2f}")
                print(f"    ├─ emir tutarı   : ~{ex.position_notional(1.0):,.2f} "
                      f"(pay %{config.ALPACA_POSITION_PCT * 100:g}, tavan "
                      f"{config.ALPACA_MAX_NOTIONAL:,.0f})")
                positions = ex.positions()
                print(f"    └─ pozisyon      : {len(positions)}")
                for p in positions:
                    print(f"        {p['symbol']}: {p['qty']} @ {p['avg_entry_price']:,.2f} "
                          f"-> {p['unrealized_pl']:+,.2f} ({p['unrealized_plpc']:+.2f}%)")
            except Exception as exc:
                print(f"    └─ hesap okunamadı: {exc}")
    else:
        stats = db.get_stats()
        print("dahili sanal defter")
        print(f"    ├─ bakiye        : {stats['balance']:,.2f} {config.QUOTE_CURRENCY}")
        print(f"    └─ açık pozisyon : {stats['open_positions']}")

    runs = db.get_agent_runs(limit=5)
    print(f"\n  Son kurul toplantıları ({len(runs)}):")
    if not runs:
        print("    henüz yok — ilk toplantı için:  python bot.py --convene BTC/USDT")
    for r in runs:
        extra = f" -> {r['action']}" if r["action"] else ""
        applied = " ✓uygulandı" if r["executed"] else (" ⏳bekliyor" if r["status"] == "OK" else "")
        dur = f" [{r['duration_sec']:.0f} sn]" if r["duration_sec"] else ""
        print(f"    {age(r['started_at']):>14} · {r['symbol']:<9} · {r['status']:<7}"
              f" {r['rating'] or '-'}{extra}{dur}{applied}")
        if r["status"] in ("ERROR", "TIMEOUT") and r.get("error"):
            print(f"        └─ {r['error'][:110]}")

    orders = db.get_broker_orders(limit=5)
    if orders:
        print(f"\n  Son emirler ({len(orders)}):")
        for o in orders:
            amount = (f"{o['notional']:,.0f} USD" if o["notional"]
                      else f"{o['qty']} adet" if o["qty"] else "-")
            print(f"    {age(o['submitted_at']):>14} · {o['broker_symbol'] or o['symbol']:<9} · "
                  f"{(o['side'] or '').upper():<4} {amount:<12} {o['status']}")
            if o["status"] == "error" and o.get("error"):
                print(f"        └─ {o['error'][:110]}")

    market = db.get_market()
    if market:
        print(f"\n  Piyasa:")
        for sym, m in market.items():
            print(f"    {sym:<9} {m['price']:>12,.2f}  RSI "
                  f"{m['rsi'] if m['rsi'] is None else round(m['rsi'], 1):<6} "
                  f"({age(m['updated_at'])})")
    print(f"{line}\n")


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
    parser.add_argument("--convene", metavar="SEMBOL", nargs="?", const="",
                        help="Kurulu hemen topla (sıklık sınırını atlar) ve çık")
    parser.add_argument("--status", action="store_true",
                        help="Sistemin tam durumunu yazdır ve çık")
    parser.add_argument("--resume-council", action="store_true",
                        help="Kalıcı hata sonrası durdurulan kurulu yeniden etkinleştir")
    args = parser.parse_args()

    if args.simulate:
        config.OFFLINE_SIMULATION = True

    db.init_db()

    if args.reset:
        db.reset_account()
        print(f"Bakiye sıfırlandı: {config.INITIAL_BALANCE:,.2f} {config.QUOTE_CURRENCY}")
        return

    if args.status:
        print_status()
        return

    if args.resume_council:
        db.init_db()
        was = agents_engine.AgentCouncil.halted()
        agents_engine.AgentCouncil.resume()
        print(f"Kurul yeniden etkinleştirildi (durdurma sebebi: {was or 'yoktu'}).")
        return

    bot = TradingBot()

    if args.convene is not None:
        symbol = args.convene or config.SYMBOLS[0]
        ok, reason = agents_engine.AgentCouncil.readiness()
        if not ok:
            print(f"Kurul çalıştırılamıyor: {reason}")
            return
        print(bot.council.analyze(symbol, bot.market.fetch_price(symbol)))
        return

    if args.once:
        db.set_state("running", "1" if args.force else db.get_state("running", "0"))
        bot.run_once()
        bot.snapshot_equity(force=True)
        print(db.get_stats())
    else:
        bot.run_forever(force=args.force)


if __name__ == "__main__":
    main()
