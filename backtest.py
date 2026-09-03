"""
backtest.py
-----------
Stratejiyi geçmiş Binance verisi üzerinde çalıştırır.

Neden gerekli: canlı deneme ayda tek haneli işlem üretir; "bu strateji kârlı
mı?" sorusunu yanıtlamak için yüzlerce işlem gerekir. Backtest bunu saniyeler
içinde verir ve parametreleri canlıda değil geçmişte denemeni sağlar.

İndikatör hesapları bot.py'den AYNEN kullanılır; yani burada test ettiğin
strateji, canlıda çalışan stratejinin ta kendisidir.

Kullanım
    python backtest.py --download --days 730      # geçmiş veriyi indir (bir kez)
    python backtest.py                            # config.py ayarlarıyla tek koşu
    python backtest.py --rsi-buy 25 --tp 0.03     # parametre değiştirerek koşu
    python backtest.py --sweep                    # parametre taraması
    python backtest.py --sweep --split 0.7        # taramayı ikiye böl (aşırı-uydurma testi)
    python backtest.py --demo-data                # internet yokken sentetik veriyle dene
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np
import pandas as pd

import config
from bot import calculate_ema, calculate_rsi

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None


HISTORY_DIR = os.path.join(config.BASE_DIR, "data", "history")
MS_PER_MINUTE = 60_000


# ==========================================================================
# PARAMETRELER
# ==========================================================================
@dataclass(frozen=True)
class Params:
    """Bir backtest koşusunun tüm ayarları."""
    rsi_period: int = config.RSI_PERIOD
    rsi_buy: float = config.RSI_BUY_THRESHOLD
    rsi_sell: float = config.RSI_SELL_THRESHOLD
    ema_period: int = config.EMA_PERIOD
    take_profit: float = config.TAKE_PROFIT_PCT
    stop_loss: float = config.STOP_LOSS_PCT
    position_pct: float = config.POSITION_SIZE_PCT
    max_position: float = config.MAX_POSITION_USDT
    min_position: float = config.MIN_POSITION_USDT
    max_open: int = config.MAX_OPEN_POSITIONS
    fee: float = config.FEE_RATE
    slippage: float = 0.0          # emir kayması (0.0005 = %0.05)
    cooldown_bars: int = 1         # kapanıştan sonra kaç mum bekle
    initial_balance: float = config.INITIAL_BALANCE

    def label(self) -> str:
        return (f"RSI<{self.rsi_buy:g} EMA{self.ema_period} "
                f"TP%{self.take_profit*100:g} SL%{self.stop_loss*100:g}")


# ==========================================================================
# VERİ İNDİRME / YÜKLEME
# ==========================================================================
def _exchange():
    if ccxt is None:
        raise RuntimeError("ccxt kurulu değil: pip install ccxt")
    return getattr(ccxt, config.EXCHANGE_ID)({"enableRateLimit": True, "timeout": 30_000})


def history_path(symbol: str, timeframe: str) -> str:
    safe = symbol.replace("/", "-")
    return os.path.join(HISTORY_DIR, f"{safe}_{timeframe}.csv")


def download_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """
    Binance'ten sayfalayarak geçmiş mumları indirir ve CSV'ye yazar.
    Dosya varsa sadece eksik kısmı ekler.
    """
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = history_path(symbol, timeframe)
    exchange = _exchange()
    tf_ms = exchange.parse_timeframe(timeframe) * 1000

    existing = pd.DataFrame()
    if os.path.exists(path):
        existing = pd.read_csv(path)

    now_ms = exchange.milliseconds()
    since = now_ms - days * 24 * 60 * 60 * 1000
    if not existing.empty:
        since = max(since, int(existing["timestamp"].max()) + tf_ms)

    rows: list[list] = []
    print(f"  {symbol} {timeframe}: indiriliyor...", end="", flush=True)
    while since < now_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + tf_ms
        print(".", end="", flush=True)
        time.sleep(exchange.rateLimit / 1000)

    fresh = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.concat([existing, fresh], ignore_index=True) if not existing.empty else fresh
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df.to_csv(path, index=False)
    print(f" {len(df):,} mum -> {os.path.relpath(path, config.BASE_DIR)}")
    return df


def make_demo_history(symbol: str, timeframe: str, days: int, seed: int = 7) -> pd.DataFrame:
    """İnternet olmadan boru hattını denemek için sentetik fiyat serisi."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    minutes = {"m": 1, "h": 60, "d": 1440}[timeframe[-1]] * int(timeframe[:-1])
    n = days * 24 * 60 // minutes
    rng = np.random.default_rng(seed + sum(map(ord, symbol)))
    # Hafif yukarı driftli, kalın kuyruklu getiriler
    ret = rng.standard_t(df=3.5, size=n) * 0.0016 + 0.00003
    close = (65_000.0 if "BTC" in symbol else 3_200.0) * np.exp(np.cumsum(ret))
    wick = np.abs(rng.normal(0, 0.0018, n))
    start_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000) - n * minutes * MS_PER_MINUTE
    df = pd.DataFrame({
        "timestamp": start_ms + np.arange(n) * minutes * MS_PER_MINUTE,
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * (1 + wick),
        "low": close * (1 - wick),
        "close": close,
        "volume": np.abs(rng.normal(100, 20, n)),
    })
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df.to_csv(history_path(symbol, timeframe), index=False)
    return df


def load_history(symbol: str, timeframe: str) -> pd.DataFrame:
    path = history_path(symbol, timeframe)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{os.path.relpath(path, config.BASE_DIR)} yok.\n"
            f"Önce indir:  python backtest.py --download --days 730\n"
            f"veya internetsiz dene:  python backtest.py --demo-data"
        )
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("datetime").sort_index()


# ==========================================================================
# GÖSTERGE HAZIRLIĞI (ileriye bakma yok)
# ==========================================================================
def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """
    Canlı botla aynı hizalama:
      - RSI, KAPANMIŞ önceki mumdan okunur          -> shift(1)
      - Trend EMA'sı, KAPANMIŞ önceki GÜNden okunur -> günlük EMA'nın shift(1)'i
    Böylece hiçbir mumda geleceğe ait bilgi kullanılmaz.
    """
    out = df.copy()
    out["rsi"] = calculate_rsi(out["close"], p.rsi_period).shift(1)

    daily_close = out["close"].resample("1D").last().dropna()
    daily_ema = calculate_ema(daily_close, p.ema_period).shift(1)
    out["ema"] = out.index.normalize().map(daily_ema)
    return out


# ==========================================================================
# BACKTEST MOTORU
# ==========================================================================
@dataclass
class Result:
    params: Params
    trades: pd.DataFrame
    equity: pd.Series
    initial_balance: float
    buy_hold_pct: float = 0.0
    bars: int = 0
    start: Optional[pd.Timestamp] = None
    end: Optional[pd.Timestamp] = None
    stats: dict = field(default_factory=dict)


def run_backtest(data: dict[str, pd.DataFrame], p: Params) -> Result:
    """Mum mum, canlı botun kurallarıyla simülasyon."""
    prepared = {s: prepare(df, p) for s, df in data.items()}

    # Ortak zaman ekseni
    index = prepared[next(iter(prepared))].index
    for df in prepared.values():
        index = index.union(df.index)
    index = index.sort_values()

    # numpy dizileri: döngü hızı için
    cols = {}
    for s, df in prepared.items():
        d = df.reindex(index)
        cols[s] = {k: d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "rsi", "ema")}

    symbols = list(prepared.keys())
    balance = p.initial_balance
    open_pos: dict[str, dict] = {}
    cooldown: dict[str, int] = {s: -10**9 for s in symbols}
    trades: list[dict] = []
    equity_vals = np.empty(len(index))

    for i in range(len(index)):
        for s in symbols:
            c = cols[s]
            o, hi, lo, rsi, ema = c["open"][i], c["high"][i], c["low"][i], c["rsi"][i], c["ema"][i]
            if np.isnan(o):
                continue

            # ---------- ÇIKIŞ ----------
            pos = open_pos.get(s)
            if pos is not None:
                exit_price = None
                reason = ""
                # Aynı mumda hem TP hem SL değdiyse hangisinin önce geldiğini
                # bilemeyiz; kötümser varsayım: önce STOP.
                if lo <= pos["stop_loss"]:
                    exit_price, reason = pos["stop_loss"], "STOP-LOSS"
                elif hi >= pos["take_profit"]:
                    exit_price, reason = pos["take_profit"], "KÂR AL"
                elif not np.isnan(rsi) and rsi > p.rsi_sell:
                    exit_price, reason = o, f"RSI>{p.rsi_sell:g}"

                if exit_price is not None:
                    fill = exit_price * (1 - p.slippage)
                    gross = pos["amount"] * fill
                    net = gross * (1 - p.fee)
                    pnl = net - pos["cost"]
                    balance += net
                    trades.append({
                        "symbol": s,
                        "opened_at": pos["opened_at"], "closed_at": index[i],
                        "entry_price": pos["entry_price"], "exit_price": fill,
                        "amount": pos["amount"], "cost": pos["cost"],
                        "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100,
                        "reason": reason, "bars_held": i - pos["bar"],
                        "balance_after": balance,
                    })
                    del open_pos[s]
                    cooldown[s] = i

            # ---------- GİRİŞ ----------
            if s in open_pos or len(open_pos) >= p.max_open:
                continue
            if i - cooldown[s] <= p.cooldown_bars:
                continue
            if np.isnan(rsi) or np.isnan(ema):
                continue
            if not (rsi < p.rsi_buy and o > ema):
                continue

            budget = min(balance * p.position_pct, p.max_position, balance)
            if budget < p.min_position:
                continue
            entry = o * (1 + p.slippage)
            fee_in = budget * p.fee
            amount = (budget - fee_in) / entry
            balance -= budget
            open_pos[s] = {
                "amount": amount, "entry_price": entry, "cost": budget,
                "take_profit": entry * (1 + p.take_profit),
                "stop_loss": entry * (1 - p.stop_loss),
                "opened_at": index[i], "bar": i,
            }

        # ---------- equity (nakit + açık pozisyonların değeri) ----------
        mtm = balance
        for s, pos in open_pos.items():
            px = cols[s]["close"][i]
            if not np.isnan(px):
                mtm += pos["amount"] * px * (1 - p.fee)
            else:
                mtm += pos["cost"]
        equity_vals[i] = mtm

    equity = pd.Series(equity_vals, index=index)

    # Al-tut karşılaştırması (sembollere eşit ağırlık)
    bh = []
    for s, df in prepared.items():
        first, last = df["close"].iloc[0], df["close"].iloc[-1]
        bh.append((last / first - 1) * 100)
    buy_hold = float(np.mean(bh)) if bh else 0.0

    res = Result(
        params=p,
        trades=pd.DataFrame(trades),
        equity=equity,
        initial_balance=p.initial_balance,
        buy_hold_pct=buy_hold,
        bars=len(index),
        start=index[0] if len(index) else None,
        end=index[-1] if len(index) else None,
    )
    res.stats = compute_stats(res)
    return res


def compute_stats(res: Result) -> dict:
    t = res.trades
    eq = res.equity
    final = float(eq.iloc[-1]) if len(eq) else res.initial_balance
    peak = eq.cummax()
    dd = (eq - peak) / peak * 100 if len(eq) else pd.Series([0.0])

    wins = t[t["pnl"] > 0] if len(t) else t
    losses = t[t["pnl"] <= 0] if len(t) else t
    gross_win = float(wins["pnl"].sum()) if len(wins) else 0.0
    gross_loss = float(-losses["pnl"].sum()) if len(losses) else 0.0

    streak = worst_streak = 0
    for pnl in (t["pnl"].tolist() if len(t) else []):
        streak = streak + 1 if pnl <= 0 else 0
        worst_streak = max(worst_streak, streak)

    days = (res.end - res.start).days if res.start is not None and res.end is not None else 0
    return {
        "final_balance": final,
        "return_pct": (final / res.initial_balance - 1) * 100,
        "buy_hold_pct": res.buy_hold_pct,
        "trades": len(t),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(t) * 100) if len(t) else 0.0,
        "avg_win_pct": float(wins["pnl_pct"].mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses["pnl_pct"].mean()) if len(losses) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_pct": float(dd.min()) if len(dd) else 0.0,
        "worst_losing_streak": worst_streak,
        "days": days,
        "trades_per_month": (len(t) / days * 30) if days else 0.0,
    }


# ==========================================================================
# RAPOR
# ==========================================================================
def print_report(res: Result) -> None:
    s = res.stats
    p = res.params
    q = config.QUOTE_CURRENCY
    line = "─" * 62
    print(f"\n{line}")
    print(f"  {p.label()}")
    print(f"  {res.start:%d.%m.%Y} – {res.end:%d.%m.%Y}  ({s['days']} gün, {res.bars:,} mum)")
    print(line)
    print(f"  Başlangıç bakiye     : {res.initial_balance:>12,.2f} {q}")
    print(f"  Bitiş bakiyesi       : {s['final_balance']:>12,.2f} {q}")
    print(f"  Getiri               : {s['return_pct']:>12.2f} %")
    print(f"  AL-TUT getirisi      : {s['buy_hold_pct']:>12.2f} %   <- botun yenmesi gereken")
    print(line)
    print(f"  İşlem sayısı         : {s['trades']:>12}  (ayda ~{s['trades_per_month']:.1f})")
    print(f"  Kazanan / kaybeden   : {s['wins']:>6} / {s['losses']:<5}")
    # Başabaş oranı TP/SL ve KOMİSYONA bağlıdır; sabit yazmak yanıltır.
    # Alpaca kripto komisyonu (%0.245) dahilinkinin ~2.5 katı ve eşiği
    # %48.6'dan %56.9'a çıkarır — aynı strateji orada zarar edebilir.
    _eko = config.trade_economics(p.fee, p.take_profit, p.stop_loss)
    print(f"  Kazanma oranı        : {s['win_rate']:>12.1f} %   "
          f"(başa baş %{_eko['breakeven_win_rate']:.1f}, komisyon %{p.fee * 100:g})")
    print(f"  Ort. kazanç / kayıp  : {s['avg_win_pct']:>+7.2f}% / {s['avg_loss_pct']:+.2f}%")
    print(f"  Kâr faktörü          : {s['profit_factor']:>12.2f}   (>1 kârlı)")
    print(f"  Maks. düşüş (DD)     : {s['max_drawdown_pct']:>12.2f} %")
    print(f"  En uzun kayıp serisi : {s['worst_losing_streak']:>12}")
    print(line)
    if s["trades"] < 30:
        print("  ⚠ 30'dan az işlem: sonuç istatistiksel olarak anlamsız.")
    if s["return_pct"] < s["buy_hold_pct"]:
        print("  ⚠ Bot, parayı sadece tutmaktan daha kötü performans gösterdi.")
    print()


# ==========================================================================
# PARAMETRE TARAMASI
# ==========================================================================
SWEEP_GRID = {
    "rsi_buy": [20, 25, 30, 35, 40],
    "take_profit": [0.01, 0.02, 0.03, 0.05],
    "stop_loss": [0.01, 0.015, 0.02, 0.03],
    "ema_period": [20, 50],
}


def sweep(data: dict[str, pd.DataFrame], base: Params, split: Optional[float]) -> pd.DataFrame:
    """
    Tüm kombinasyonları dener. `split` verilirse veriyi ikiye böler:
    ilk kısımda en iyiyi seçer, ikinci kısımda (hiç görmediği veride) sınar.
    Aşırı-uydurmayı (overfitting) yakalamanın tek dürüst yolu budur.
    """
    keys = list(SWEEP_GRID)
    combos = list(itertools.product(*(SWEEP_GRID[k] for k in keys)))
    print(f"{len(combos)} kombinasyon deneniyor...")

    if split:
        cut = {s: df.index[int(len(df) * split)] for s, df in data.items()}
        train = {s: df[df.index < cut[s]] for s, df in data.items()}
        test = {s: df[df.index >= cut[s]] for s, df in data.items()}
    else:
        train, test = data, None

    rows = []
    for n, combo in enumerate(combos, 1):
        p = replace(base, **dict(zip(keys, combo)))
        r = run_backtest(train, p)
        # Oranları yüzdeye çevirerek yaz: 0.015 gibi değerler tabloda
        # yuvarlanıp 0.01 görünmesin (yanlış parametre seçtirir).
        row = {
            "RSI<": p.rsi_buy,
            "TP_%": p.take_profit * 100,
            "SL_%": p.stop_loss * 100,
            "EMA": p.ema_period,
            "getiri_%": r.stats["return_pct"],
            "işlem": r.stats["trades"],
            "kazanma_%": r.stats["win_rate"],
            "maks_düşüş_%": r.stats["max_drawdown_pct"],
            "kâr_faktörü": r.stats["profit_factor"],
        }
        if test is not None:
            rt = run_backtest(test, p)
            row["test_getiri_%"] = rt.stats["return_pct"]
            row["test_işlem"] = rt.stats["trades"]
        rows.append(row)
        if n % 10 == 0 or n == len(combos):
            print(f"  {n}/{len(combos)}", end="\r", flush=True)
    print()
    return pd.DataFrame(rows).sort_values("getiri_%", ascending=False).reset_index(drop=True)


# ==========================================================================
# CLI
# ==========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Strateji backtest'i")
    ap.add_argument("--download", action="store_true", help="Geçmiş veriyi Binance'ten indir")
    ap.add_argument("--days", type=int, default=730, help="Kaç günlük geçmiş (varsayılan 730)")
    ap.add_argument("--demo-data", action="store_true", help="İnternetsiz sentetik veri üret")
    ap.add_argument("--sweep", action="store_true", help="Parametre taraması yap")
    ap.add_argument("--split", type=float, metavar="ORAN",
                    help="Taramada veriyi böl (0.7 = %%70 seçim, %%30 sınama)")
    ap.add_argument("--top", type=int, default=15, help="Taramada kaç satır gösterilsin")
    ap.add_argument("--rsi-buy", type=float), ap.add_argument("--rsi-sell", type=float)
    ap.add_argument("--tp", type=float), ap.add_argument("--sl", type=float)
    ap.add_argument("--ema", type=int), ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--fee", type=float, metavar="ORAN",
                    help=f"Komisyon oranı (varsayılan dahili {config.FEE_RATE}; "
                         f"Alpaca kripto için {config.ALPACA_FEE_RATE})")
    ap.add_argument("--save-trades", metavar="DOSYA", help="İşlemleri CSV'ye yaz")
    args = ap.parse_args()

    symbols = config.SYMBOLS
    tf = config.TIMEFRAME

    if args.demo_data:
        print(f"Sentetik veri üretiliyor ({args.days} gün)...")
        for s in symbols:
            make_demo_history(s, tf, args.days)
    elif args.download:
        print(f"Binance'ten {args.days} günlük {tf} verisi indiriliyor:")
        for s in symbols:
            download_history(s, tf, args.days)
        return

    data = {s: load_history(s, tf) for s in symbols}
    for s, df in data.items():
        print(f"{s}: {len(df):,} mum  {df.index[0]:%d.%m.%Y} – {df.index[-1]:%d.%m.%Y}")

    base = Params(slippage=args.slippage,
                  **({"fee": args.fee} if args.fee is not None else {}))
    for arg, field_name in (("rsi_buy", "rsi_buy"), ("rsi_sell", "rsi_sell"),
                            ("tp", "take_profit"), ("sl", "stop_loss"), ("ema", "ema_period")):
        val = getattr(args, arg)
        if val is not None:
            base = replace(base, **{field_name: val})

    if args.sweep:
        table = sweep(data, base, args.split)
        pd.set_option("display.width", 200)
        print(f"\nEN İYİ {args.top} KOMBİNASYON"
              + (" (seçim verisinde)" if args.split else "") + ":")
        fmt = {"RSI<": "{:.0f}", "TP_%": "{:.2f}", "SL_%": "{:.2f}", "EMA": "{:.0f}",
               "işlem": "{:.0f}", "test_işlem": "{:.0f}"}
        print(table.head(args.top).to_string(
            index=False,
            formatters={c: (lambda f: (lambda v: f.format(v)))(f)
                        for c, f in fmt.items() if c in table.columns},
            float_format=lambda v: f"{v:.2f}"))
        if args.split:
            print("\n'test_getiri_%' sütunu, parametrelerin HİÇ GÖRMEDİĞİ veriden gelir.")
            print("Seçim verisinde iyi olup testte kötüleşen satırlar aşırı-uydurmadır.")
            good = table.head(args.top)
            if "test_getiri_%" in good:
                agree = (good["getiri_%"] > 0) & (good["test_getiri_%"] > 0)
                print(f"İlk {args.top} satırın {int(agree.sum())} tanesi her iki dönemde de kârlı.")
        return

    res = run_backtest(data, base)
    print_report(res)
    if args.save_trades and len(res.trades):
        res.trades.to_csv(args.save_trades, index=False)
        print(f"İşlemler yazıldı: {args.save_trades}")


if __name__ == "__main__":
    main()
