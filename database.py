"""
database.py
-----------
SQLite katmanı. Bot (yazar) ve Streamlit paneli (okur) aynı dosyayı
paylaştığı için WAL modu + kısa kilitli bağlantılar kullanılır.

Tablolar
    account       : tek satırlık sanal hesap (bakiye)
    positions     : açık / kapanmış pozisyonlar
    trades        : kapanmış işlemlerin PnL kaydı (işlem geçmişi)
    equity_curve  : zaman içindeki toplam varlık (equity) anlık görüntüleri
    market        : her sembol için son fiyat + indikatör değerleri (panel için cache)
    bot_state     : anahtar/değer durum kaydı (bot çalışıyor mu, son hata vb.)
    logs          : bot olay günlüğü
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import config

_LOCK = threading.Lock()
_INITIALISED = False


# --------------------------------------------------------------------------
# Bağlantı yönetimi
# --------------------------------------------------------------------------
def utcnow() -> str:
    """Veritabanına yazılacak ISO formatlı UTC zaman damgası."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_connection():
    """Kısa ömürlü, thread-safe bir SQLite bağlantısı verir."""
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    balance         REAL NOT NULL,
    initial_balance REAL NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL DEFAULT 'LONG',
    amount       REAL    NOT NULL,              -- coin adedi
    entry_price  REAL    NOT NULL,
    cost         REAL    NOT NULL,              -- bakiyeden düşen toplam USDT (komisyon dahil)
    entry_fee    REAL    NOT NULL DEFAULT 0,
    take_profit  REAL,
    stop_loss    REAL,
    entry_rsi    REAL,
    entry_ema    REAL,
    entry_reason TEXT,
    opened_at    TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    is_demo      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL DEFAULT 'LONG',
    amount        REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    opened_at     TEXT,
    closed_at     TEXT NOT NULL,
    gross_pnl     REAL NOT NULL,
    fee           REAL NOT NULL,
    pnl           REAL NOT NULL,
    pnl_pct       REAL NOT NULL,
    exit_reason   TEXT,
    balance_after REAL NOT NULL,
    is_demo       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    balance  REAL NOT NULL,   -- serbest nakit
    equity   REAL NOT NULL    -- nakit + açık pozisyon değeri
);

CREATE TABLE IF NOT EXISTS market (
    symbol     TEXT PRIMARY KEY,
    price      REAL,
    rsi        REAL,
    ema        REAL,
    signal     TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    symbol  TEXT,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL DEFAULT 'RUNNING',  -- RUNNING | OK | ERROR | TIMEOUT
    rating         TEXT,             -- Buy | Overweight | Hold | Underweight | Sell | REVIEW
    action         TEXT,             -- BUY | SELL | HOLD
    size_factor    REAL,             -- pozisyon büyüklüğü çarpanı (0-1)
    proposed_stop  REAL,             -- ajanların önerdiği stop fiyatı
    price_at_run   REAL,
    duration_sec   REAL,
    error          TEXT,
    executed       INTEGER NOT NULL DEFAULT 0,   -- karar uygulandı mı
    reports        TEXT              -- JSON: tüm ajan raporları ve tartışmalar
);

CREATE TABLE IF NOT EXISTS broker_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    broker            TEXT    NOT NULL DEFAULT 'alpaca',
    symbol            TEXT    NOT NULL,        -- bizim sembol (BTC/USDT)
    broker_symbol     TEXT,                    -- Alpaca sembolü (BTC/USD)
    side              TEXT    NOT NULL,        -- buy | sell
    notional          REAL,                    -- gönderilen tutar (USD)
    qty               REAL,
    submitted_at      TEXT    NOT NULL,
    broker_order_id   TEXT,
    client_order_id   TEXT,
    status            TEXT,                    -- accepted | filled | rejected | error ...
    filled_qty        REAL,
    filled_avg_price  REAL,
    take_profit       REAL,
    stop_loss         REAL,
    agent_run_id      INTEGER,                 -- kararı veren kurul toplantısı
    is_paper          INTEGER NOT NULL DEFAULT 1,
    error             TEXT,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_broker_orders_symbol ON broker_orders(symbol, id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_symbol ON agent_runs(symbol, id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_equity_ts        ON equity_curve(ts);
"""


def init_db(force_reset: bool = False) -> None:
    """Tabloları oluşturur ve hesap yoksa başlangıç bakiyesini tanımlar."""
    global _INITIALISED
    with _LOCK:
        with get_connection() as conn:
            conn.executescript(SCHEMA)
            if force_reset:
                conn.execute("DELETE FROM positions")
                conn.execute("DELETE FROM trades")
                conn.execute("DELETE FROM equity_curve")
                conn.execute("DELETE FROM account")
            row = conn.execute("SELECT COUNT(*) AS c FROM account").fetchone()
            if row["c"] == 0:
                now = utcnow()
                conn.execute(
                    "INSERT INTO account (id, balance, initial_balance, created_at, updated_at)"
                    " VALUES (1, ?, ?, ?, ?)",
                    (config.INITIAL_BALANCE, config.INITIAL_BALANCE, now, now),
                )
            conn.execute(
                "INSERT INTO bot_state (key, value) VALUES ('running', '0')"
                " ON CONFLICT(key) DO NOTHING"
            )
    _INITIALISED = True


def ensure_db() -> None:
    """Her genel fonksiyondan önce çağrılır; şemayı bir kez hazırlar."""
    if not _INITIALISED:
        init_db()


# --------------------------------------------------------------------------
# Hesap / bakiye
# --------------------------------------------------------------------------
def get_account() -> dict:
    ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    return dict(row) if row else {}


def get_balance() -> float:
    return float(get_account().get("balance", 0.0))


def update_balance(new_balance: float) -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE account SET balance = ?, updated_at = ? WHERE id = 1",
            (float(new_balance), utcnow()),
        )


def reset_account(initial_balance: Optional[float] = None) -> None:
    """Sanal bakiyeyi sıfırlar: tüm pozisyon/işlem/equity kayıtları silinir."""
    ensure_db()
    amount = float(initial_balance if initial_balance is not None else config.INITIAL_BALANCE)
    now = utcnow()
    with _LOCK:
        with get_connection() as conn:
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM equity_curve")
            conn.execute("DELETE FROM logs")
            conn.execute("DELETE FROM account")
            conn.execute(
                "INSERT INTO account (id, balance, initial_balance, created_at, updated_at)"
                " VALUES (1, ?, ?, ?, ?)",
                (amount, amount, now, now),
            )
            conn.execute(
                "INSERT INTO equity_curve (ts, balance, equity) VALUES (?, ?, ?)",
                (now, amount, amount),
            )
    add_log("INFO", f"Sanal hesap sıfırlandı: {amount:,.2f} {config.QUOTE_CURRENCY}")


# --------------------------------------------------------------------------
# Pozisyonlar
# --------------------------------------------------------------------------
def open_position(
    symbol: str,
    amount: float,
    entry_price: float,
    cost: float,
    entry_fee: float,
    take_profit: float,
    stop_loss: float,
    entry_rsi: Optional[float] = None,
    entry_ema: Optional[float] = None,
    entry_reason: str = "",
    is_demo: bool = True,
) -> int:
    """Yeni pozisyon kaydı açar ve maliyeti bakiyeden düşer."""
    ensure_db()
    with _LOCK:
        with get_connection() as conn:
            row = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()
            balance = float(row["balance"])
            if cost > balance + 1e-9:
                raise ValueError(
                    f"Yetersiz bakiye: gerekli {cost:.2f}, mevcut {balance:.2f}"
                )
            cur = conn.execute(
                """INSERT INTO positions
                   (symbol, side, amount, entry_price, cost, entry_fee, take_profit,
                    stop_loss, entry_rsi, entry_ema, entry_reason, opened_at, status, is_demo)
                   VALUES (?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                (
                    symbol, float(amount), float(entry_price), float(cost), float(entry_fee),
                    float(take_profit), float(stop_loss),
                    entry_rsi, entry_ema, entry_reason, utcnow(), 1 if is_demo else 0,
                ),
            )
            conn.execute(
                "UPDATE account SET balance = ?, updated_at = ? WHERE id = 1",
                (balance - cost, utcnow()),
            )
            return int(cur.lastrowid)


def close_position(position_id: int, exit_price: float, exit_reason: str = "") -> dict:
    """Pozisyonu kapatır, PnL hesaplar, bakiyeyi günceller ve trades'e yazar."""
    ensure_db()
    with _LOCK:
        with get_connection() as conn:
            # Pozisyonu ÖNCE atomik olarak sahiplen. Bot süreci ile panelden
            # yapılan manuel kapatma aynı anda denerse ikincisi 0 satır günceller
            # ve hata alır; böylece aynı pozisyon iki kez kapatılamaz.
            claimed = conn.execute(
                "UPDATE positions SET status = 'CLOSED' WHERE id = ? AND status = 'OPEN'",
                (position_id,),
            )
            if claimed.rowcount == 0:
                raise ValueError(f"Açık pozisyon bulunamadı (id={position_id})")

            pos = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            amount = float(pos["amount"])
            entry_price = float(pos["entry_price"])
            cost = float(pos["cost"])
            entry_fee = float(pos["entry_fee"])

            gross_proceeds = amount * float(exit_price)
            exit_fee = gross_proceeds * config.FEE_RATE
            net_proceeds = gross_proceeds - exit_fee

            gross_pnl = (float(exit_price) - entry_price) * amount
            total_fee = entry_fee + exit_fee
            pnl = net_proceeds - cost
            pnl_pct = (pnl / cost * 100.0) if cost else 0.0

            balance = float(conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()["balance"])
            new_balance = balance + net_proceeds
            now = utcnow()

            conn.execute(
                "UPDATE account SET balance = ?, updated_at = ? WHERE id = 1",
                (new_balance, now),
            )
            cur = conn.execute(
                """INSERT INTO trades
                   (position_id, symbol, side, amount, entry_price, exit_price, opened_at,
                    closed_at, gross_pnl, fee, pnl, pnl_pct, exit_reason, balance_after, is_demo)
                   VALUES (?, ?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position_id, pos["symbol"], amount, entry_price, float(exit_price),
                    pos["opened_at"], now, gross_pnl, total_fee, pnl, pnl_pct,
                    exit_reason, new_balance, pos["is_demo"],
                ),
            )
            return {
                "trade_id": int(cur.lastrowid),
                "symbol": pos["symbol"],
                "amount": amount,
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "fee": total_fee,
                "balance_after": new_balance,
                "exit_reason": exit_reason,
            }


def get_open_positions(symbol: Optional[str] = None) -> list[dict]:
    ensure_db()
    with get_connection() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' AND symbol = ? ORDER BY id",
                (symbol,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY id"
            ).fetchall()
    return [dict(r) for r in rows]


def has_open_position(symbol: str) -> bool:
    return len(get_open_positions(symbol)) > 0


def get_trades(limit: int = 500) -> list[dict]:
    ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def last_trade_time(symbol: str) -> Optional[str]:
    ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT closed_at FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol,)
        ).fetchone()
    return row["closed_at"] if row else None


# --------------------------------------------------------------------------
# Equity eğrisi
# --------------------------------------------------------------------------
def record_equity(balance: float, equity: float) -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO equity_curve (ts, balance, equity) VALUES (?, ?, ?)",
            (utcnow(), float(balance), float(equity)),
        )


def get_equity_curve(limit: int = 5000) -> list[dict]:
    ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM equity_curve ORDER BY id DESC LIMIT ?) ORDER BY id",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Piyasa cache'i (panelin canlı fiyatı bottan okuması için)
# --------------------------------------------------------------------------
def update_market(symbol: str, price: float, rsi: Optional[float],
                  ema: Optional[float], signal: str = "") -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO market (symbol, price, rsi, ema, signal, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                   price = excluded.price, rsi = excluded.rsi, ema = excluded.ema,
                   signal = excluded.signal, updated_at = excluded.updated_at""",
            (symbol, float(price), rsi, ema, signal, utcnow()),
        )


def get_market() -> dict[str, dict]:
    ensure_db()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM market").fetchall()
    return {r["symbol"]: dict(r) for r in rows}


# --------------------------------------------------------------------------
# Yapay zekâ kurulu (TradingAgents) koşuları
# --------------------------------------------------------------------------
def start_agent_run(symbol: str, price: Optional[float] = None) -> int:
    """Kurul toplantısı başlarken kaydı açar; id döner."""
    ensure_db()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO agent_runs (symbol, started_at, status, price_at_run)"
            " VALUES (?, ?, 'RUNNING', ?)",
            (symbol, utcnow(), price),
        )
        return int(cur.lastrowid)


def finish_agent_run(run_id: int, *, status: str, rating: Optional[str] = None,
                     action: Optional[str] = None, size_factor: Optional[float] = None,
                     proposed_stop: Optional[float] = None, duration_sec: Optional[float] = None,
                     error: Optional[str] = None, reports: Optional[dict] = None) -> None:
    """Toplantı bitince sonucu ve tüm ajan raporlarını yazar."""
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            """UPDATE agent_runs
               SET finished_at = ?, status = ?, rating = ?, action = ?, size_factor = ?,
                   proposed_stop = ?, duration_sec = ?, error = ?, reports = ?
               WHERE id = ?""",
            (utcnow(), status, rating, action, size_factor, proposed_stop,
             duration_sec, error, json.dumps(reports, ensure_ascii=False) if reports else None,
             run_id),
        )


def mark_agent_run_executed(run_id: int) -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute("UPDATE agent_runs SET executed = 1 WHERE id = ?", (run_id,))


def get_agent_runs(limit: int = 50, symbol: Optional[str] = None,
                   with_reports: bool = False) -> list[dict]:
    ensure_db()
    cols = "*" if with_reports else (
        "id, symbol, started_at, finished_at, status, rating, action, size_factor,"
        " proposed_stop, price_at_run, duration_sec, error, executed")
    with get_connection() as conn:
        if symbol:
            rows = conn.execute(
                f"SELECT {cols} FROM agent_runs WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol, int(limit))).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM agent_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if with_reports and d.get("reports"):
            try:
                d["reports"] = json.loads(d["reports"])
            except (TypeError, ValueError):
                d["reports"] = {}
        out.append(d)
    return out


def get_agent_run(run_id: int) -> Optional[dict]:
    ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("reports"):
        try:
            d["reports"] = json.loads(d["reports"])
        except (TypeError, ValueError):
            d["reports"] = {}
    return d


def last_agent_run_time(symbol: str) -> Optional[str]:
    """Bu sembol için en son toplantının başlangıç zamanı (sıklık kontrolü için)."""
    ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT started_at FROM agent_runs WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()
    return row["started_at"] if row else None


def agent_runs_today() -> int:
    """Bugün başlatılan toplantı sayısı (günlük maliyet sınırı için)."""
    ensure_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM agent_runs WHERE started_at >= ?", (today + " 00:00:00",)
        ).fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------
# Broker (Alpaca) emirleri
# --------------------------------------------------------------------------
def record_broker_order(symbol: str, side: str, **fields) -> int:
    """Gönderilen emri kaydeder; id döner."""
    ensure_db()
    allowed = ("broker", "broker_symbol", "notional", "qty", "broker_order_id",
               "client_order_id", "status", "filled_qty", "filled_avg_price",
               "take_profit", "stop_loss", "agent_run_id", "is_paper", "error")
    data = {k: v for k, v in fields.items() if k in allowed}
    data.setdefault("broker", "alpaca")
    cols = ["symbol", "side", "submitted_at", "updated_at"] + list(data)
    vals = [symbol, side, utcnow(), utcnow()] + [
        int(v) if isinstance(v, bool) else v for v in data.values()]
    with get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO broker_orders ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)


def update_broker_order(order_id: int, **fields) -> None:
    ensure_db()
    allowed = ("status", "filled_qty", "filled_avg_price", "broker_order_id", "error")
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data) + ", updated_at = ?"
    with get_connection() as conn:
        conn.execute(f"UPDATE broker_orders SET {sets} WHERE id = ?",
                     list(data.values()) + [utcnow(), order_id])


def get_broker_orders(limit: int = 100, symbol: Optional[str] = None) -> list[dict]:
    ensure_db()
    with get_connection() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM broker_orders WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (symbol, int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM broker_orders ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def last_open_broker_order(symbol: str) -> Optional[dict]:
    """
    Bu sembol için en son ALIM emri, ardından SATIŞ gelmemişse döner.
    Kripto pozisyonlarının kâr al / stop seviyelerini burada saklıyoruz
    (Alpaca kriptoda bracket emri kabul etmiyor).
    """
    ensure_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM broker_orders WHERE symbol = ? ORDER BY id DESC LIMIT 1",
            (symbol,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    return d if d["side"] == "buy" else None


# --------------------------------------------------------------------------
# Bot durumu (anahtar/değer)
# --------------------------------------------------------------------------
def set_state(key: str, value: Any) -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, "" if value is None else str(value)),
        )


def get_state(key: str, default: Optional[str] = None) -> Optional[str]:
    ensure_db()
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def is_bot_running() -> bool:
    return get_state("running", "0") == "1"


def set_bot_running(running: bool) -> None:
    set_state("running", "1" if running else "0")
    set_state("running_changed_at", utcnow())
    add_log("INFO", "Bot BAŞLATILDI." if running else "Bot DURDURULDU.")


# --------------------------------------------------------------------------
# Loglar
# --------------------------------------------------------------------------
def add_log(level: str, message: str, symbol: Optional[str] = None) -> None:
    ensure_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO logs (ts, level, symbol, message) VALUES (?, ?, ?, ?)",
            (utcnow(), level.upper(), symbol, message),
        )
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN "
            "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (config.MAX_LOG_ROWS,),
        )


def get_logs(limit: int = 100) -> list[dict]:
    ensure_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Özet istatistikler (panel üst metrikleri)
# --------------------------------------------------------------------------
def get_stats(prices: Optional[dict[str, float]] = None) -> dict:
    """
    Panel için tek seferde tüm özet metrikleri hesaplar.
    `prices` verilirse açık pozisyonların anlık değeri de equity'e katılır.
    """
    ensure_db()
    prices = prices or {}
    account = get_account()
    balance = float(account.get("balance", 0.0))
    initial = float(account.get("initial_balance", config.INITIAL_BALANCE))

    open_positions = get_open_positions()
    open_value = 0.0
    open_pnl = 0.0
    for pos in open_positions:
        price = float(prices.get(pos["symbol"]) or pos["entry_price"])
        gross = pos["amount"] * price
        value = gross * (1 - config.FEE_RATE)   # satış komisyonu düşülmüş gerçekçi değer
        open_value += value
        open_pnl += value - pos["cost"]

    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
                      COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS losses,
                      COALESCE(SUM(pnl), 0) AS realized_pnl,
                      COALESCE(SUM(fee), 0) AS total_fee,
                      COALESCE(MAX(pnl), 0) AS best,
                      COALESCE(MIN(pnl), 0) AS worst
               FROM trades"""
        ).fetchone()

    total = int(row["total"])
    wins = int(row["wins"])
    equity = balance + open_value

    return {
        "balance": balance,
        "initial_balance": initial,
        "open_value": open_value,
        "equity": equity,
        "open_pnl": open_pnl,
        "open_pnl_pct": (open_pnl / sum(p["cost"] for p in open_positions) * 100.0)
        if open_positions else 0.0,
        "realized_pnl": float(row["realized_pnl"]),
        "total_pnl": equity - initial,
        "total_pnl_pct": ((equity - initial) / initial * 100.0) if initial else 0.0,
        "total_trades": total,
        "winning_trades": wins,
        "losing_trades": int(row["losses"]),
        "win_rate": (wins / total * 100.0) if total else 0.0,
        "total_fee": float(row["total_fee"]),
        "best_trade": float(row["best"]),
        "worst_trade": float(row["worst"]),
        "open_positions": len(open_positions),
    }


if __name__ == "__main__":  # python database.py -> şemayı kur ve özet bas
    init_db()
    print(f"Veritabanı hazır: {config.DB_PATH}")
    print(get_stats())
