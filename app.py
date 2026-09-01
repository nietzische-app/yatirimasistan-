"""
app.py
------
Streamlit tabanlı takip paneli.

Çalıştırma:
    streamlit run app.py

Panel botu kendi içinde bir arka plan thread'i olarak da çalıştırabilir
(sidebar'daki "Botu Başlat" düğmesi). Botu ayrı bir terminalde çalıştırmak
istersen `python bot.py` de aynı veritabanını kullanır.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import agents_engine
import alpaca_execution
import config
import database as db
from bot import BotRunner, MarketData, TradingBot

try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:  # grafik kütüphanesi yoksa yerleşik chart'a düş
    HAS_PLOTLY = False


# ==========================================================================
# SAYFA AYARLARI
# ==========================================================================
st.set_page_config(
    page_title=config.APP_TITLE,
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem;}
      div[data-testid="stMetric"] {
          background: rgba(130, 145, 175, 0.10);
          border: 1px solid rgba(130, 145, 175, 0.22);
          border-radius: 14px;
          padding: 16px 18px 12px 18px;
      }
      div[data-testid="stMetricLabel"] p {font-size: 0.82rem; opacity: 0.75;}
      div[data-testid="stMetricValue"] {font-size: 1.65rem;}
      .badge {
          display:inline-block; padding:4px 12px; border-radius:999px;
          font-size:0.78rem; font-weight:600; letter-spacing:.02em;
      }
      .badge-demo {background:rgba(56,161,105,.18); color:#38a169; border:1px solid rgba(56,161,105,.4);}
      .badge-live {background:rgba(229,62,62,.18); color:#e53e3e; border:1px solid rgba(229,62,62,.4);}
      .badge-on   {background:rgba(49,130,206,.18); color:#3182ce; border:1px solid rgba(49,130,206,.4);}
      .badge-off  {background:rgba(128,128,128,.18); color:#9aa0a6; border:1px solid rgba(128,128,128,.4);}
      .muted {opacity:.65; font-size:.85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# Streamlit 1.49+ `width="stretch"` kullanır, öncesi `use_container_width=True`.
# İki sürümde de çalışsın diye tek yerden yönetiyoruz.
_ST_VERSION = tuple(int(p) for p in st.__version__.split(".")[:2] if p.isdigit())
FULL = {"width": "stretch"} if _ST_VERSION >= (1, 49) else {"use_container_width": True}


# ==========================================================================
# KAYNAKLAR (tek sefer kurulur, tüm rerun'larda paylaşılır)
# ==========================================================================
@st.cache_resource(show_spinner=False)
def bootstrap() -> BotRunner | None:
    """
    Veritabanını hazırlar; motor panelin içinde çalışacaksa arka plan
    thread'ini başlatır.

    config.RUN_BOT_IN_DASHBOARD=False ise (Docker Compose kurulumu) motor
    ayrı bir container'da döner ve panel yalnızca izleyici olur — böylece
    iki süreç aynı anda emir açıp mükerrer pozisyon yaratmaz.
    """
    db.init_db()
    if not config.RUN_BOT_IN_DASHBOARD:
        return None
    runner = BotRunner()
    runner.start()
    return runner


@st.cache_resource(show_spinner=False)
def get_executor() -> TradingBot:
    """Panelden elle yapılan işlemler (pozisyon kapatma) için tek örnek."""
    return TradingBot()


@st.cache_resource(show_spinner=False)
def get_market_data() -> MarketData | None:
    """Panelin kendi başına fiyat çekebilmesi için (bot kapalıyken)."""
    try:
        return MarketData()
    except Exception:
        return None


runner = bootstrap()


@st.cache_data(ttl=20, show_spinner=False)
def alpaca_snapshot() -> dict:
    """Hesap + pozisyonlar (20 sn cache; her rerun'da API'yi yormasın)."""
    try:
        ex = alpaca_execution.get_executor()
        return {"ok": True, "account": ex.account(), "positions": ex.positions()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# Etkin emir yürütme arkası: paneldeki rakamlar buna göre okunur
_backend = alpaca_execution.backend_name()
_alpaca_ready = _backend == "alpaca" and alpaca_execution.AlpacaExecutor.readiness()[0]
_snap = alpaca_snapshot() if _alpaca_ready else {"ok": False}
_alpaca_live = bool(_snap.get("ok"))



# ==========================================================================
# YARDIMCILAR
# ==========================================================================
def money(value: float) -> str:
    return f"{value:,.2f} {config.QUOTE_CURRENCY}"


def local_time(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return str(ts)


@st.cache_data(ttl=20, show_spinner=False)
def _live_prices(symbols: tuple[str, ...]) -> dict[str, float]:
    """Borsadan doğrudan fiyat çeker (20 sn cache'li)."""
    md = get_market_data()
    out: dict[str, float] = {}
    if md is None:
        return out
    for symbol in symbols:
        try:
            price = md.fetch_price(symbol)
        except Exception:
            price = None
        if price:
            out[symbol] = float(price)
    return out


def current_prices(open_symbols: list[str]) -> dict[str, float]:
    """
    Fiyat kaynağı: önce botun veritabanına yazdığı cache.
    Sadece AÇIK pozisyonu olup cache'te fiyatı bulunmayan semboller için
    borsaya gidilir (bot hiç çalışmadıysa panel yine de doğru K/Z gösterir).
    """
    market = db.get_market()
    prices = {s: float(m["price"]) for s, m in market.items() if m.get("price")}
    missing = tuple(sorted({s for s in open_symbols if s not in prices}))
    if missing:
        prices.update(_live_prices(missing))
    return prices


def pnl_color(value: float) -> str:
    return "#22c55e" if value > 0 else ("#ef4444" if value < 0 else "#9aa0a6")


# --- hücre biçimlendiricileri (BTC gibi büyük, XRP gibi küçük fiyatlar için) ---
def fmt_price(v) -> str:
    if v is None or pd.isna(v):
        return "-"
    v = float(v)
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.4f}"
    return f"{v:,.6f}"


def fmt_qty(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{float(v):,.6f}".rstrip("0").rstrip(".")


def fmt_money(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{float(v):,.2f}"


def fmt_signed(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{float(v):+,.2f}"


def fmt_pct(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{float(v):+.2f}%"


def fmt_num(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{float(v):,.1f}"


def render_table(df: pd.DataFrame, formats: dict, pnl_columns: list[str],
                 height: int | None = None) -> None:
    """Sayıları düzgün biçimlendirip K/Z sütunlarını renklendirerek tablo basar."""
    def _color(v):
        try:
            return f"color: {pnl_color(float(v))}; font-weight: 600;"
        except (TypeError, ValueError):
            return ""

    styler = df.style.format({k: v for k, v in formats.items() if k in df.columns}, na_rep="-")
    subset = [c for c in pnl_columns if c in df.columns]
    if subset:
        styler = styler.map(_color, subset=subset)
    kwargs = {"hide_index": True, **FULL}
    if height:
        kwargs["height"] = height
    st.dataframe(styler, **kwargs)


# ==========================================================================
# SIDEBAR — KONTROL PANELİ
# ==========================================================================
with st.sidebar:
    st.markdown("## ⚙️ Kontrol Paneli")

    mode_badge = ('<span class="badge badge-demo">DEMO · Sanal Para</span>'
                  if config.DEMO_MODE else
                  '<span class="badge badge-live">GERÇEK EMİR</span>')
    running = db.is_bot_running()
    run_badge = ('<span class="badge badge-on">● ÇALIŞIYOR</span>' if running
                 else '<span class="badge badge-off">■ DURDURULDU</span>')
    st.markdown(f"{mode_badge} &nbsp; {run_badge}", unsafe_allow_html=True)
    if config.OFFLINE_SIMULATION:
        st.caption("🔌 Offline simülasyon modu: fiyatlar sentetik üretiliyor.")

    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("▶️ Başlat", **FULL, disabled=running, type="primary"):
            db.set_bot_running(True)
            st.rerun()
    with col_b:
        if st.button("⏹️ Durdur", **FULL, disabled=not running):
            db.set_bot_running(False)
            st.rerun()

    if runner is not None:
        if st.button("🔄 Şimdi Tara (tek tur)", **FULL):
            with st.spinner("Piyasa taranıyor..."):
                try:
                    if runner.bot is None:
                        runner.bot = TradingBot()
                    runner.bot.run_once()
                    runner.bot.snapshot_equity(force=True)
                    st.success("Tarama tamamlandı.")
                except Exception as exc:
                    st.error(f"Tarama hatası: {exc}")
    else:
        st.caption("🛠️ Motor ayrı bir süreçte çalışıyor (bot.py). Panel yalnızca izler.")

    st.divider()
    st.markdown("#### 💣 Sıfırlama")
    confirm = st.checkbox("Onaylıyorum (tüm geçmiş silinir)")
    if st.button(f"Sanal Bakiyeyi Sıfırla ({config.INITIAL_BALANCE:,.0f}$)",
                 disabled=not confirm, **FULL):
        db.set_bot_running(False)
        db.reset_account()
        st.success(f"Bakiye {config.INITIAL_BALANCE:,.2f} {config.QUOTE_CURRENCY} olarak sıfırlandı.")
        time.sleep(1)
        st.rerun()

    st.divider()
    auto_refresh = st.toggle("Otomatik yenile", value=config.DASHBOARD_AUTO_REFRESH)
    refresh_seconds = st.slider("Yenileme aralığı (sn)", 3, 60,
                                config.DASHBOARD_REFRESH_SECONDS, step=1)

    st.divider()
    with st.expander("📋 Aktif Ayarlar", expanded=False):
        for key, value in config.summary().items():
            st.markdown(f"**{key}:** {value}")

    last_run = db.get_state("last_run")
    heartbeat = db.get_state("heartbeat")
    st.caption(f"Son tarama: {local_time(last_run)}")
    st.caption(f"Motor sinyali: {local_time(heartbeat)}")
    err = db.get_state("last_error")
    if err:
        st.caption(f"⚠️ Son hata: {err[:160]}")


# ==========================================================================
# ÜST BAŞLIK + METRİKLER
# ==========================================================================
st.title("📈 " + config.APP_TITLE)
st.caption(
    f"{', '.join(config.SYMBOLS)} · {config.TIMEFRAME} mumlar · "
    f"RSI({config.RSI_PERIOD}) + EMA({config.EMA_PERIOD}·{config.EMA_TIMEFRAME}) stratejisi · "
    f"TP %{config.TAKE_PROFIT_PCT * 100:g} / SL %{config.STOP_LOSS_PCT * 100:g}"
)
st.caption(
    ("🏦 Emirler **Alpaca " + ("PAPER" if config.ALPACA_PAPER else "CANLI")
     + "** hesabına gidiyor — aşağıdaki *Dahili Sanal Defter* bölümleri kullanılmıyor.")
    if _alpaca_live else
    "📒 Emirler **dahili sanal defterde** simüle ediliyor."
)

open_positions = db.get_open_positions()
prices = current_prices([p["symbol"] for p in open_positions])
stats = db.get_stats(prices)
trades = db.get_trades(limit=1000)

m1, m2, m3, m4, m5 = st.columns(5)

if _alpaca_live:
    # Emirler Alpaca'ya gidiyor: tepedeki rakamlar GERÇEK hesabı göstersin,
    # kullanılmayan dahili defteri değil.
    _acc = _snap["account"]
    _pos = _snap["positions"]
    _open_pl = sum(p["unrealized_pl"] or 0.0 for p in _pos)
    _cost = sum(p["cost_basis"] or 0.0 for p in _pos)
    m1.metric("💼 Portföy Değeri (Alpaca)",
              f"{_acc['equity']:,.2f} {_acc['currency']}" if _acc["equity"] else "-",
              (f"{_acc['day_pnl']:+,.2f} ({_acc['day_pnl_pct']:+.2f}%) bugün"
               if _acc["day_pnl"] is not None else "gün verisi yok"))
    m2.metric("💵 Nakit", f"{_acc['cash']:,.2f}" if _acc["cash"] else "-",
              f"Alım gücü: {_acc['buying_power']:,.0f}" if _acc["buying_power"] else "",
              delta_color="off")
    m3.metric("📊 Açık Pozisyon K/Z", f"{_open_pl:+,.2f}",
              f"{_open_pl / _cost * 100:+.2f}%" if _cost else "pozisyon yok",
              delta_color="normal" if _pos else "off")
    m4.metric("📦 Açık Pozisyon", f"{len(_pos)}",
              f"{len(db.get_broker_orders(200))} emir gönderildi", delta_color="off")
    _ok_runs = [r for r in db.get_agent_runs(200) if r["status"] == "OK"]
    m5.metric("🧠 Kurul Kararı", f"{len(_ok_runs)}",
              f"{sum(1 for r in _ok_runs if r['action'] == 'BUY')} AL / "
              f"{sum(1 for r in _ok_runs if r['action'] == 'SELL')} SAT", delta_color="off")
else:
    m1.metric(
        "💰 Toplam Varlık (Equity)",
        money(stats["equity"]),
        f"{stats['total_pnl']:+,.2f} ({stats['total_pnl_pct']:+.2f}%)",
    )
    m2.metric(
        "🏦 Serbest Bakiye",
        money(stats["balance"]),
        f"Açık pozisyon değeri: {stats['open_value']:,.2f}",
        delta_color="off",
    )
    m3.metric(
        "📊 Açık Pozisyon K/Z",
        f"{stats['open_pnl']:+,.2f} {config.QUOTE_CURRENCY}",
        f"{stats['open_pnl_pct']:+.2f}%" if open_positions else "pozisyon yok",
        delta_color="normal" if open_positions else "off",
    )
    m4.metric(
        "✅ Başarılı İşlem",
        f"{stats['winning_trades']} / {stats['total_trades']}",
        f"Gerçekleşen K/Z: {stats['realized_pnl']:+,.2f}",
        delta_color="off",
    )
    m5.metric(
        "🎯 Win Rate",
        f"%{stats['win_rate']:.1f}",
        f"{stats['losing_trades']} zararlı işlem",
        delta_color="off",
    )

st.divider()

# ==========================================================================
# TABLO 1 — AÇIK POZİSYONLAR
# ==========================================================================
left, right = st.columns([3, 2])

with left:
    st.subheader("🟢 Açık Pozisyonlar" + (" · dahili defter (kullanılmıyor)" if _alpaca_live else ""))
    if open_positions:
        rows = []
        for pos in open_positions:
            price = float(prices.get(pos["symbol"]) or pos["entry_price"])
            value = pos["amount"] * price * (1 - config.FEE_RATE)
            pnl = value - pos["cost"]
            rows.append({
                "Coin": pos["symbol"],
                "Miktar": pos["amount"],
                "Giriş Fiyatı": pos["entry_price"],
                "Güncel Fiyat": price,
                "Maliyet": pos["cost"],
                "Güncel Değer": value,
                "K/Z ($)": pnl,
                "K/Z (%)": (pnl / pos["cost"] * 100) if pos["cost"] else 0.0,
                "Kâr Al": pos["take_profit"],
                "Stop": pos["stop_loss"],
                "Açılış": local_time(pos["opened_at"]),
            })
        render_table(
            pd.DataFrame(rows),
            {
                "Miktar": fmt_qty, "Giriş Fiyatı": fmt_price, "Güncel Fiyat": fmt_price,
                "Maliyet": fmt_money, "Güncel Değer": fmt_money,
                "K/Z ($)": fmt_signed, "K/Z (%)": fmt_pct,
                "Kâr Al": fmt_price, "Stop": fmt_price,
            },
            ["K/Z ($)", "K/Z (%)"],
        )

        with st.expander("Pozisyonu elle kapat"):
            labels = {
                f"#{p['id']} · {p['symbol']} · giriş {p['entry_price']:,.2f}": p
                for p in open_positions
            }
            choice = st.selectbox("Pozisyon", list(labels.keys()))
            if st.button("Seçili pozisyonu kapat", type="secondary"):
                pos = labels[choice]
                price = float(prices.get(pos["symbol"]) or pos["entry_price"])
                try:
                    trade = get_executor().close_trade(pos, price, "Manuel kapatma")
                    st.success(f"Kapatıldı. PnL: {trade['pnl']:+,.2f} {config.QUOTE_CURRENCY}")
                    time.sleep(1)
                    st.rerun()
                except Exception as exc:
                    st.error(f"Kapatılamadı: {exc}")
    else:
        st.info("Şu anda açık pozisyon yok. Bot alım sinyali bekliyor.")

with right:
    st.subheader("🛰️ Piyasa Takibi")
    market = db.get_market()
    if market:
        market_rows = []
        for symbol in config.SYMBOLS:
            m = market.get(symbol, {})
            market_rows.append({
                "Coin": symbol,
                "Fiyat": float(prices.get(symbol) or m.get("price") or 0) or None,
                f"RSI({config.RSI_PERIOD})": m.get("rsi"),
                f"EMA{config.EMA_PERIOD} ({config.EMA_TIMEFRAME})": m.get("ema"),
                "Durum": m.get("signal") or "-",
                "Güncelleme": local_time(m.get("updated_at")),
            })
        render_table(
            pd.DataFrame(market_rows),
            {
                "Fiyat": fmt_price,
                f"RSI({config.RSI_PERIOD})": fmt_num,
                f"EMA{config.EMA_PERIOD} ({config.EMA_TIMEFRAME})": fmt_price,
            },
            [],
        )
    else:
        st.info("Henüz piyasa verisi yok. Botu başlat veya 'Şimdi Tara' düğmesine bas.")

    st.markdown(
        f"<span class='muted'>Toplam ödenen komisyon: {stats['total_fee']:,.2f} "
        f"{config.QUOTE_CURRENCY} · En iyi işlem: {stats['best_trade']:+,.2f} · "
        f"En kötü işlem: {stats['worst_trade']:+,.2f}</span>",
        unsafe_allow_html=True,
    )

st.divider()

# ==========================================================================
# GRAFİK — EQUITY CURVE
# ==========================================================================
st.subheader("📉 Sanal Bakiye Değişimi (Equity Curve)" + (" · dahili defter (kullanılmıyor)" if _alpaca_live else ""))
curve = db.get_equity_curve()
if curve:
    curve_df = pd.DataFrame(curve)
    curve_df["ts"] = pd.to_datetime(curve_df["ts"], utc=True).dt.tz_convert(
        datetime.now().astimezone().tzinfo
    )
    if HAS_PLOTLY:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=curve_df["ts"], y=curve_df["equity"], name="Toplam Varlık",
            mode="lines", line=dict(color="#3182ce", width=2.5),
            fill="tozeroy", fillcolor="rgba(49,130,206,0.12)",
        ))
        fig.add_trace(go.Scatter(
            x=curve_df["ts"], y=curve_df["balance"], name="Serbest Nakit",
            mode="lines", line=dict(color="#9aa0a6", width=1.4, dash="dot"),
        ))
        fig.add_hline(
            y=stats["initial_balance"], line_dash="dash", line_color="#94a3b8",
            annotation_text=f"Başlangıç {stats['initial_balance']:,.0f}",
            annotation_position="bottom right",
        )
        low = float(min(curve_df["equity"].min(), stats["initial_balance"]))
        high = float(max(curve_df["equity"].max(), stats["initial_balance"]))
        pad = max((high - low) * 0.15, high * 0.002, 1.0)
        fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=30, b=10),
            hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0),
            yaxis=dict(title=config.QUOTE_CURRENCY, range=[low - pad, high + pad]),
            xaxis=dict(title=None),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, **FULL)
    else:
        st.line_chart(curve_df.set_index("ts")[["equity", "balance"]])
else:
    st.info("Henüz equity verisi yok. Bot çalışmaya başlayınca grafik dolacak.")

st.divider()

# ==========================================================================
# TABLO 2 — İŞLEM GEÇMİŞİ
# ==========================================================================
st.subheader("🧾 İşlem Geçmişi (Kapanmış İşlemler)" + (" · dahili defter (kullanılmıyor)" if _alpaca_live else ""))
if trades:
    hist = pd.DataFrame(trades)
    hist_df = pd.DataFrame({
        "Tarih": hist["closed_at"].map(local_time),
        "Coin": hist["symbol"],
        "Miktar": hist["amount"],
        "Giriş": hist["entry_price"],
        "Çıkış": hist["exit_price"],
        "Komisyon": hist["fee"],
        "Net K/Z ($)": hist["pnl"],
        "K/Z (%)": hist["pnl_pct"],
        "Kapanış Sebebi": hist["exit_reason"],
        "Sonraki Bakiye": hist["balance_after"],
    })
    render_table(
        hist_df,
        {
            "Miktar": fmt_qty, "Giriş": fmt_price, "Çıkış": fmt_price,
            "Komisyon": fmt_money, "Net K/Z ($)": fmt_signed,
            "K/Z (%)": fmt_pct, "Sonraki Bakiye": fmt_money,
        },
        ["Net K/Z ($)", "K/Z (%)"],
        height=360,
    )
    st.download_button(
        "⬇️ CSV olarak indir",
        hist_df.round(6).to_csv(index=False).encode("utf-8-sig"),
        file_name="islem_gecmisi.csv",
        mime="text/csv",
    )
else:
    st.info("Henüz kapanmış işlem yok.")

# ==========================================================================
# ALPACA PAPER TRADING HESABI
# ==========================================================================
if _backend == "alpaca" or config.ALPACA_API_KEY:
    st.divider()
    st.subheader("🏦 Alpaca Hesabı" + (" · PAPER" if config.ALPACA_PAPER else " · ⚠️ CANLI"))
    _ok, _why = alpaca_execution.AlpacaExecutor.readiness()
    if not _ok:
        st.warning(f"Alpaca kullanılamıyor: {_why}")
    else:
        snap = _snap if _snap.get("ok") else alpaca_snapshot()
        if not snap["ok"]:
            st.error(f"Alpaca'ya bağlanılamadı: {snap['error']}")
        else:
            acc = snap["account"]
            a1, a2, a3, a4 = st.columns(4)
            a1.metric("💼 Portföy Değeri",
                      f"{acc['equity']:,.2f} {acc['currency']}" if acc["equity"] else "-",
                      (f"{acc['day_pnl']:+,.2f} ({acc['day_pnl_pct']:+.2f}%)"
                       if acc["day_pnl"] is not None else "gün verisi yok"))
            a2.metric("💵 Nakit", f"{acc['cash']:,.2f}" if acc["cash"] else "-",
                      delta_color="off")
            a3.metric("🛒 Alım Gücü", f"{acc['buying_power']:,.2f}" if acc["buying_power"] else "-",
                      delta_color="off")
            a4.metric("📡 Hesap Durumu", acc["status"] or "-", delta_color="off")

            positions = snap["positions"]
            st.markdown("**Alpaca Açık Pozisyonlar**")
            if positions:
                pos_df = pd.DataFrame([{
                    "Sembol": p["symbol"],
                    "Tür": p["asset_class"].replace("AssetClass.", ""),
                    "Miktar": fmt_qty(p["qty"]),
                    "Giriş": fmt_price(p["avg_entry_price"]),
                    "Güncel": fmt_price(p["current_price"]),
                    "Değer": fmt_money(p["market_value"]),
                    "K/Z ($)": p["unrealized_pl"],
                    "K/Z (%)": p["unrealized_plpc"],
                } for p in positions])
                render_table(pos_df, {"K/Z ($)": fmt_signed, "K/Z (%)": fmt_pct},
                             ["K/Z ($)", "K/Z (%)"])
            else:
                st.info("Alpaca hesabında açık pozisyon yok.")

    # Gönderilen emirler (kurul kararlarıyla eşleşmiş halde)
    orders = db.get_broker_orders(limit=50)
    if orders:
        st.markdown("**Gönderilen Emirler** (kurul kararlarıyla eşleşmiş)")
        ord_df = pd.DataFrame([{
            "Zaman": local_time(o["submitted_at"]),
            "Sembol": o["broker_symbol"] or o["symbol"],
            "Yön": (o["side"] or "").upper(),
            "Tutar": fmt_money(o["notional"]) if o["notional"] else "-",
            "Adet": fmt_qty(o["qty"]) if o["qty"] else "-",
            "Doldu @": fmt_price(o["filled_avg_price"]) if o["filled_avg_price"] else "-",
            "Kâr Al": fmt_price(o["take_profit"]) if o["take_profit"] else "-",
            "Stop": fmt_price(o["stop_loss"]) if o["stop_loss"] else "-",
            "Durum": o["status"] or "-",
            "Kurul #": o["agent_run_id"] if o["agent_run_id"] else "-",
        } for o in orders])
        render_table(ord_df, {}, [], height=260)
        failed = [o for o in orders if o["status"] == "error"]
        if failed:
            with st.expander(f"⚠️ Reddedilen emirler ({len(failed)})", expanded=False):
                for o in failed[:10]:
                    st.markdown(f"**{local_time(o['submitted_at'])} · {o['symbol']} · "
                                f"{(o['side'] or '').upper()}**")
                    st.caption((o.get("error") or "")[:400])

# ==========================================================================
# TARAMA SONUCU (hangi coinler kurula gidiyor)
# ==========================================================================
if config.SCREENER_ENABLED:
    st.divider()
    st.subheader("🔎 Coin Taraması")
    st.caption(
        f"{len(config.WATCHLIST)} coin ucuza taranıyor (yalnızca borsa verisi); "
        f"en yüksek puanlı **{config.SCREENER_TOP_N}** tanesi için yapay zekâ kurulu "
        f"toplanıyor. Tarama her {config.SCREENER_INTERVAL_MINUTES} dakikada yenilenir."
    )
    scan = db.get_screener_results(limit=60)
    if scan:
        top_syms = {r["symbol"] for r in scan[:config.SCREENER_TOP_N]}
        scan_df = pd.DataFrame([{
            "#": r["rank"],
            "Coin": r["symbol"],
            "Kurul": "🧠" if r["symbol"] in top_syms else "",
            "Fiyat": fmt_price(r["price"]),
            "RSI": f"{r['rsi']:.1f}" if r["rsi"] is not None else "-",
            "24s %": r["change_24h"],
            "Hacim": f"{r['volume_ratio']:.1f}x" if r["volume_ratio"] else "-",
            "Trend": "↑" if (r["components"] or {}).get("trend_yukarı") else "↓",
            "Puan": f"{r['score']:.3f}" if r["score"] is not None else "-",
        } for r in scan])
        render_table(scan_df, {"24s %": fmt_pct}, ["24s %"], height=320)
        st.caption(f"Son tarama: {local_time(db.get_state('last_screen'))}")
    else:
        st.info("Henüz tarama yapılmadı. `docker compose exec bot python bot.py --screen`")

# ==========================================================================
# YAPAY ZEKÂ KURUL RAPORLARI (TradingAgents)
# ==========================================================================
st.divider()
st.subheader("🧠 Yapay Zekâ Kurul Raporları (Agents Reasoning)")

_ready, _reason = agents_engine.AgentCouncil.readiness()
if _ready:
    st.caption(
        f"✅ Kurul aktif · {', '.join(config.AGENT_ANALYSTS)} analistleri · "
        f"tartışma {config.AGENT_DEBATE_ROUNDS} tur / risk {config.AGENT_RISK_ROUNDS} tur · "
        f"model {config.LLM_DEEP_MODEL} · her {config.AGENT_INTERVAL_MINUTES} dakikada bir toplanır"
    )
else:
    st.warning(f"Kurul devre dışı: {_reason}")

runs = db.get_agent_runs(limit=50)
if runs:
    # Sayıları burada metne çeviriyoruz: boş hücrelerde "None" yerine "-" görünsün.
    def _opt(value, fn):
        return "-" if value is None else fn(value)

    decisions = pd.DataFrame([{
        "Zaman": local_time(r["started_at"]),
        "Coin": r["symbol"],
        "Not": r["rating"] or "-",
        "Karar": r["action"] or "-",
        "Büyüklük": _opt(r["size_factor"], lambda v: f"x{float(v):g}"),
        "Önerilen Stop": _opt(r["proposed_stop"], fmt_price),
        "Fiyat": _opt(r["price_at_run"], fmt_price),
        "Süre (sn)": _opt(r["duration_sec"], lambda v: f"{float(v):,.0f}"),
        "Durum": r["status"],
        "Uygulandı": "✓" if r["executed"] else "",
    } for r in runs])
    render_table(decisions, {}, [], height=240)

    ok_runs = [r for r in runs if r["status"] == "OK"]
    if ok_runs:
        labels = {
            f"#{r['id']} · {r['symbol']} · {r['rating']} · {local_time(r['started_at'])}": r["id"]
            for r in ok_runs
        }
        chosen = st.selectbox("Hangi toplantının tutanağını görmek istersin?", list(labels))
        detail = db.get_agent_run(labels[chosen])
        reports = (detail or {}).get("reports") or {}

        if not reports:
            st.info("Bu toplantı için rapor kaydedilmemiş.")
        else:
            head = st.columns(4)
            head[0].metric("Not", detail["rating"] or "-")
            head[1].metric("Karar", detail["action"] or "-")
            head[2].metric("Önerilen Stop",
                           fmt_price(detail["proposed_stop"]) if detail["proposed_stop"] else "-")
            head[3].metric("Süre", f"{detail['duration_sec']:.0f} sn"
                           if detail.get("duration_sec") else "-")

            # Kurula bizim verdiğimiz ek bağlam (portföy + canlı teknik + kripto)
            if reports.get("operator_context"):
                with st.expander("🗂️ Kurula verdiğimiz ek bağlam", expanded=False):
                    st.caption("Ajanların kendi araçlarında olmayan, bizim ilettiğimiz veriler.")
                    st.markdown(reports["operator_context"])

            # Analist raporları ve nihai karar
            for key, title in agents_engine.REPORT_FIELDS:
                if reports.get(key):
                    with st.expander(title, expanded=(key == "final_trade_decision")):
                        st.markdown(reports[key])

            # Boğa / Ayı tartışması
            debate = reports.get("investment_debate") or {}
            if debate:
                with st.expander("🐂 Boğa ↔ 🐻 Ayı Tartışması", expanded=False):
                    bull, bear = st.columns(2)
                    with bull:
                        st.markdown("**🐂 Boğa Araştırmacısı**")
                        st.markdown(debate.get("bull_history") or "_kayıt yok_")
                    with bear:
                        st.markdown("**🐻 Ayı Araştırmacısı**")
                        st.markdown(debate.get("bear_history") or "_kayıt yok_")
                    if debate.get("judge_decision"):
                        st.markdown("---")
                        st.markdown("**⚖️ Araştırma Müdürünün Hükmü**")
                        st.markdown(debate["judge_decision"])

            # Risk kurulu
            risk = reports.get("risk_debate") or {}
            if risk:
                with st.expander("🛡️ Risk Kurulu Tartışması", expanded=False):
                    cols = st.columns(3)
                    for col, (key, title) in zip(cols, (
                            ("aggressive_history", "🔥 Agresif"),
                            ("conservative_history", "🧊 Muhafazakâr"),
                            ("neutral_history", "⚖️ Nötr"))):
                        with col:
                            st.markdown(f"**{title}**")
                            st.markdown(risk.get(key) or "_kayıt yok_")
                    if risk.get("judge_decision"):
                        st.markdown("---")
                        st.markdown("**🧑‍⚖️ Risk Yöneticisinin Onay/Ret Gerekçesi**")
                        st.markdown(risk["judge_decision"])

    errors = [r for r in runs if r["status"] in ("ERROR", "TIMEOUT")]
    if errors:
        with st.expander(f"⚠️ Başarısız toplantılar ({len(errors)})", expanded=False):
            for r in errors[:10]:
                st.markdown(f"**{local_time(r['started_at'])} · {r['symbol']} · {r['status']}**")
                st.caption((r.get("error") or "")[:400])
else:
    st.info(
        "Henüz kurul toplantısı yok. Bot çalışırken ilk toplantı kendiliğinden yapılır; "
        "hemen denemek için sunucuda:  `docker compose exec bot python bot.py --convene BTC/USDT`"
    )

# ==========================================================================
# LOGLAR
# ==========================================================================
with st.expander("🗒️ Bot Günlüğü", expanded=False):
    logs = db.get_logs(limit=120)
    if logs:
        log_df = pd.DataFrame([{
            "Zaman": local_time(l["ts"]),
            "Tür": l["level"],
            "Coin": l["symbol"] or "-",
            "Mesaj": l["message"],
        } for l in logs])
        st.dataframe(log_df, **FULL, hide_index=True, height=320)
    else:
        st.caption("Kayıt yok.")

st.markdown(
    "<p class='muted'>⚠️ Bu panel eğitim amaçlı bir simülasyondur; yatırım tavsiyesi değildir. "
    "DEMO_MODE=False yapmadan önce stratejiyi uzun süre demo bakiyeyle test et.</p>",
    unsafe_allow_html=True,
)

# ==========================================================================
# OTOMATİK YENİLEME
# ==========================================================================
if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
