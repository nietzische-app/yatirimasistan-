"""
alpaca_execution.py
-------------------
TradingAgents kurulunun kararlarını Alpaca'ya (varsayılan: Paper Trading
sandbox) emir olarak gönderir, hesap/pozisyon durumunu okur ve her şeyi
SQLite'a yazar.

Neden Alpaca: profesyonel bir sanal hesap (gerçek emir defteri, komisyon ve
doldurma davranışı) ve ileride ABD hisselerine (NVDA, AAPL, TSLA...) aynı
kodla geçebilme imkânı.

İki önemli davranış farkı — bilerek böyle:
  * HİSSE  : kâr al + stop, Alpaca'ya BRACKET emri olarak gönderilir; koruma
             borsa tarafında durur, botumuz kapalı olsa bile çalışır.
  * KRİPTO : Alpaca kriptoda bracket/stop emri kabul etmez. Seviyeler
             veritabanına yazılır, korumayı bot.py'nin hızlı döngüsü uygular
             (fiyat seviyeye değince market satış).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import config
import database as db

log = logging.getLogger("alpaca")

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce
    from alpaca.trading.requests import (MarketOrderRequest, StopLossRequest,
                                         TakeProfitRequest)
    ALPACA_AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    TradingClient = None
    ALPACA_AVAILABLE = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def backend_name() -> str:
    """Etkin emir yürütme arkası: 'alpaca' veya 'internal'."""
    choice = (config.EXECUTION_BACKEND or "auto").lower()
    if choice == "alpaca":
        return "alpaca"
    if choice == "auto" and config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY:
        return "alpaca"
    return "internal"


class AlpacaExecutor:
    """Alpaca Trading API sarmalayıcısı."""

    def __init__(self) -> None:
        self._client = None

    # ------------------------------------------------------------- kurulum
    @staticmethod
    def readiness() -> tuple[bool, str]:
        if not ALPACA_AVAILABLE:
            return False, f"alpaca-py yüklenemedi -> {IMPORT_ERROR}"
        if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
            return False, ("Alpaca anahtarları yok. .env içine ALPACA_API_KEY ve "
                           "ALPACA_SECRET_KEY ekle.")
        return True, f"hazır ({'PAPER' if config.ALPACA_PAPER else 'CANLI'})"

    def client(self):
        if self._client is None:
            ok, reason = self.readiness()
            if not ok:
                raise RuntimeError(reason)
            self._client = TradingClient(
                api_key=config.ALPACA_API_KEY,
                secret_key=config.ALPACA_SECRET_KEY,
                paper=config.ALPACA_PAPER,
            )
            log.info("Alpaca bağlandı (%s)", "PAPER" if config.ALPACA_PAPER else "CANLI")
        return self._client

    # ------------------------------------------------------------- okuma
    def account(self) -> dict:
        """Hesap özeti: equity, nakit, alım gücü."""
        a = self.client().get_account()
        def _f(name):
            v = getattr(a, name, None)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        equity, last_equity = _f("equity"), _f("last_equity")
        return {
            "status": str(getattr(a, "status", "")),
            "currency": getattr(a, "currency", "USD"),
            "equity": equity,
            "cash": _f("cash"),
            "buying_power": _f("buying_power"),
            "portfolio_value": _f("portfolio_value"),
            "last_equity": last_equity,
            "day_pnl": (equity - last_equity) if (equity and last_equity) else None,
            "day_pnl_pct": ((equity / last_equity - 1) * 100)
                           if (equity and last_equity) else None,
        }

    def positions(self) -> list[dict]:
        """Alpaca'daki açık pozisyonlar (anlık K/Z dahil)."""
        out = []
        for p in self.client().get_all_positions():
            def _f(name):
                try:
                    return float(getattr(p, name))
                except (TypeError, ValueError):
                    return None
            out.append({
                "symbol": p.symbol,
                "asset_class": str(getattr(p, "asset_class", "")),
                "side": str(getattr(p, "side", "")),
                "qty": _f("qty"),
                "avg_entry_price": _f("avg_entry_price"),
                "current_price": _f("current_price"),
                "market_value": _f("market_value"),
                "cost_basis": _f("cost_basis"),
                "unrealized_pl": _f("unrealized_pl"),
                "unrealized_plpc": (_f("unrealized_plpc") or 0.0) * 100,
            })
        return out

    def position_for(self, symbol: str) -> Optional[dict]:
        target = config.alpaca_symbol(symbol)
        # Alpaca kripto pozisyonlarını "BTCUSD" biçiminde döndürebilir
        flat = target.replace("/", "")
        for p in self.positions():
            if p["symbol"] in (target, flat):
                return p
        return None

    def tradable_crypto(self) -> list[str]:
        """Alpaca'da alınıp satılabilen kripto çiftleri (WATCHLIST için)."""
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest
        assets = self.client().get_all_assets(GetAssetsRequest(
            asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
        return sorted(a.symbol for a in assets if getattr(a, "tradable", False))

    def market_open(self) -> Optional[bool]:
        try:
            return bool(self.client().get_clock().is_open)
        except Exception:
            return None

    # ------------------------------------------------------------- yazma
    def buy(self, symbol: str, notional: float, *, take_profit: Optional[float] = None,
            stop_loss: Optional[float] = None, agent_run_id: Optional[int] = None) -> dict:
        """
        Market alım emri gönderir. Hissede kâr al/stop bracket olarak eklenir;
        kriptoda seviyeler yalnızca veritabanına yazılır (bkz. modül başlığı).
        """
        broker_symbol = config.alpaca_symbol(symbol)
        crypto = config.is_crypto(symbol)
        notional = round(float(notional), 2)
        client_order_id = f"ta-{uuid.uuid4().hex[:20]}"

        if notional < config.ALPACA_MIN_NOTIONAL:
            msg = f"tutar çok küçük ({notional:.2f} < {config.ALPACA_MIN_NOTIONAL:.2f})"
            log.info("[%s] alım atlandı: %s", symbol, msg)
            return {"ok": False, "error": msg}

        kwargs: dict[str, Any] = {
            "symbol": broker_symbol,
            "notional": notional,
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.GTC if crypto else TimeInForce.DAY,
            "client_order_id": client_order_id,
        }
        # Bracket yalnızca hissede ve tutar yerine adet gerektirdiği için
        # burada sadece hisse tarafında ve notional ile uyumlu olduğunda kurulur.
        bracket = (not crypto and config.ALPACA_USE_BRACKET_FOR_EQUITY
                   and take_profit and stop_loss)
        if bracket:
            kwargs.update({
                "order_class": OrderClass.BRACKET,
                "take_profit": TakeProfitRequest(limit_price=round(float(take_profit), 2)),
                "stop_loss": StopLossRequest(stop_price=round(float(stop_loss), 2)),
                "time_in_force": TimeInForce.GTC,
            })

        order_row = db.record_broker_order(
            symbol, "buy", broker_symbol=broker_symbol, notional=notional,
            client_order_id=client_order_id, status="submitting",
            take_profit=take_profit, stop_loss=stop_loss, agent_run_id=agent_run_id,
            is_paper=config.ALPACA_PAPER)

        try:
            order = self.client().submit_order(MarketOrderRequest(**kwargs))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            db.update_broker_order(order_row, status="error", error=err)
            db.add_log("ERROR", f"{symbol}: Alpaca alım emri reddedildi — {err[:180]}", symbol)
            log.error("[%s] Alpaca alım hatası: %s", symbol, err)
            return {"ok": False, "error": err, "order_row": order_row}

        info = self._order_info(order)
        db.update_broker_order(order_row, status=info["status"],
                               broker_order_id=info["id"],
                               filled_qty=info["filled_qty"],
                               filled_avg_price=info["filled_avg_price"])
        db.add_log("BROKER",
                   f"{symbol}: Alpaca ALIM {notional:,.2f} USD gönderildi "
                   f"({'bracket' if bracket else 'market'}, durum {info['status']})", symbol)
        log.info("[%s] Alpaca alım: %s USD, durum %s", symbol, notional, info["status"])
        return {"ok": True, "order_row": order_row, **info}

    def sell(self, symbol: str, *, agent_run_id: Optional[int] = None,
             reason: str = "") -> dict:
        """Pozisyonu tamamen kapatır (Alpaca close_position)."""
        broker_symbol = config.alpaca_symbol(symbol)
        pos = self.position_for(symbol)
        if pos is None:
            return {"ok": False, "error": "Alpaca'da açık pozisyon yok"}

        order_row = db.record_broker_order(
            symbol, "sell", broker_symbol=broker_symbol, qty=pos["qty"],
            status="submitting", agent_run_id=agent_run_id, is_paper=config.ALPACA_PAPER,
            error=None)
        try:
            order = self.client().close_position(pos["symbol"])
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            db.update_broker_order(order_row, status="error", error=err)
            db.add_log("ERROR", f"{symbol}: Alpaca satış hatası — {err[:180]}", symbol)
            return {"ok": False, "error": err, "order_row": order_row}

        info = self._order_info(order)
        db.update_broker_order(order_row, status=info["status"], broker_order_id=info["id"],
                               filled_qty=info["filled_qty"],
                               filled_avg_price=info["filled_avg_price"])
        db.add_log("BROKER", f"{symbol}: Alpaca SATIŞ gönderildi ({reason or 'kurul kararı'})",
                   symbol)
        return {"ok": True, "order_row": order_row, **info}

    @staticmethod
    def _order_info(order: Any) -> dict:
        def _f(name):
            try:
                return float(getattr(order, name))
            except (TypeError, ValueError, AttributeError):
                return None
        return {
            "id": str(getattr(order, "id", "")) or None,
            "status": str(getattr(getattr(order, "status", ""), "value",
                                  getattr(order, "status", ""))) or "unknown",
            "filled_qty": _f("filled_qty"),
            "filled_avg_price": _f("filled_avg_price"),
        }

    # ------------------------------------------------- emir durumu takibi
    FINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}

    def sync_orders(self, limit: int = 20) -> int:
        """
        Gönderilen emirlerin son durumunu Alpaca'dan okuyup veritabanına yazar.

        Emir gönderildiğinde Alpaca "accepted/pending_new" döner; doldurma
        saniyeler sonra olur. Bu senkron olmadan veritabanı sonsuza dek
        "pending_new" gösterir ve gerçekleşen fiyat hiç kaydedilmez.
        Güncellenen emir sayısını döner.
        """
        updated = 0
        for row in db.get_broker_orders(limit=limit):
            if (row.get("status") or "").lower() in self.FINAL_STATUSES:
                continue
            oid = row.get("broker_order_id")
            if not oid:
                continue
            try:
                order = self.client().get_order_by_id(oid)
            except Exception as exc:
                log.debug("Emir %s okunamadı: %s", oid, exc)
                continue
            info = self._order_info(order)
            if info["status"] != row.get("status") or info["filled_avg_price"]:
                db.update_broker_order(row["id"], status=info["status"],
                                       filled_qty=info["filled_qty"],
                                       filled_avg_price=info["filled_avg_price"])
                updated += 1
                if info["status"].lower() == "filled":
                    db.add_log("BROKER",
                               f"{row['symbol']}: emir doldu — {info['filled_qty']} @ "
                               f"{info['filled_avg_price']:,.2f}", row["symbol"])
                elif info["status"].lower() in ("rejected", "canceled", "cancelled"):
                    db.add_log("ERROR",
                               f"{row['symbol']}: emir {info['status']}", row["symbol"])
        return updated

    # --------------------------------------------------- koruma (kripto)
    def check_protective_exit(self, symbol: str, price: float) -> Optional[str]:
        """
        Kripto pozisyonları için kâr al / stop kontrolü.
        Seviyeler alım emri kaydından okunur; Alpaca kriptoda bracket kabul
        etmediği için bu kontrolü bot tarafında yapmak zorundayız.
        """
        if not config.is_crypto(symbol):
            return None                      # hissede bracket borsada duruyor
        order = db.last_open_broker_order(symbol)
        if not order:
            return None
        if order.get("take_profit") and price >= float(order["take_profit"]):
            return f"KÂR AL (%{config.TAKE_PROFIT_PCT * 100:g})"
        if order.get("stop_loss") and price <= float(order["stop_loss"]):
            return "STOP-LOSS"
        return None

    # ------------------------------------------------------------- boyut
    def position_notional(self, size_factor: float = 1.0) -> float:
        """Hesap büyüklüğüne göre emir tutarı (USD)."""
        acc = self.account()
        base = acc.get("equity") or acc.get("portfolio_value") or 0.0
        notional = base * config.ALPACA_POSITION_PCT * max(0.0, min(1.0, size_factor))
        return float(min(notional, config.ALPACA_MAX_NOTIONAL,
                         acc.get("buying_power") or notional))


_EXECUTOR: Optional[AlpacaExecutor] = None


def get_executor() -> AlpacaExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = AlpacaExecutor()
    return _EXECUTOR


if __name__ == "__main__":      # python alpaca_execution.py  -> bağlantı testi
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    db.init_db()
    ok, reason = AlpacaExecutor.readiness()
    print(f"Alpaca durumu: {reason}")
    if ok:
        ex = get_executor()
        acc = ex.account()
        print(f"Hesap: {acc['status']} | equity {acc['equity']:,.2f} {acc['currency']} "
              f"| nakit {acc['cash']:,.2f} | alım gücü {acc['buying_power']:,.2f}")
        for p in ex.positions():
            print(f"  {p['symbol']}: {p['qty']} @ {p['avg_entry_price']} "
                  f"-> {p['unrealized_pl']:+,.2f} ({p['unrealized_plpc']:+.2f}%)")
