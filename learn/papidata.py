"""Notebook'lar için papi verisine salt-okunur erişim (sıkıcı kısım; öğrenilecek kısım notebook'larda).

papi'nin SQLite dosyasını ``mode=ro`` ile açar: notebook'lar papi'nin verisini asla değiştirmez.
Fiyatlar papi'deki gibi **ham** (bedelsiz/bölünme düzeltmesi yok); düzeltme 01. notebook'un konusu.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx
import pandas as pd

PAPI_HOME = Path(os.environ.get("PAPI_HOME", "~/.papi")).expanduser()
DB_PATH = PAPI_HOME / "papi.db"
DATA_DIR = Path(__file__).parent / "data"

# Her sembol için verisi olan en öncelikli TEK kaynak kullanılır (kaynakları gün gün karıştırmak, ör.
# tatil günlerinde Yahoo'nun bayat/farklı ölçekli fiyatını araya sokar).
SOURCE_PRIORITY = ("isyatirim_prices", "yahoo_prices", "tefas_prices")


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def prices(tickers: list[str], start: str = "2015-01-01", end: str | None = None) -> pd.DataFrame:
    """Günlük kapanışlar: index = tarih, sütun = sembol (ham fiyat, TL)."""
    q = """
        SELECT i.value AS ticker, p.ts AS date, s.code AS source, p.close
        FROM price_bar p
        JOIN identifier i ON i.entity_id = p.instrument_id
             AND i.entity_type = 'instrument' AND i.scheme = 'TICKER' AND i.valid_to IS NULL
        JOIN source s ON s.id = p.source_id
        WHERE p.interval = '1d' AND p.close IS NOT NULL AND p.ts >= ? AND p.ts <= ?
          AND i.value IN ({})
    """.format(",".join("?" * len(tickers)))
    with connect() as conn:
        df = pd.read_sql_query(q, conn, params=[start, end or "9999-12-31", *tickers])
    if df.empty:
        raise ValueError(f"fiyat yok: {tickers} — önce `papi prices sync --tickers ... --from {start}`")
    df["rank"] = df["source"].map({c: n for n, c in enumerate(SOURCE_PRIORITY)}).fillna(99)
    best = df.groupby("ticker")["rank"].transform("min")
    df = df[df["rank"] == best]
    wide = df.pivot(index="date", columns="ticker", values="close").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide[[t for t in tickers if t in wide.columns]]


def positions() -> pd.DataFrame:
    """Portföydeki açık pozisyonlar (papi'nin hesapladığıyla aynı: ``papi portfolio list``)."""
    from papi.core import db
    from papi.portfolio.service import positions as _positions  # papi'nin kendi hesabı

    with db.connect() as conn:
        rows = _positions(conn)
    return pd.DataFrame(
        {
            "ticker": p.ticker,
            "quantity": float(p.quantity),
            "avg_cost": float(p.avg_cost) if p.avg_cost is not None else None,
            "last_close": p.last_close,
            "market_value": float(p.market_value) if p.market_value is not None else None,
            "weight": p.weight,
        }
        for p in rows
    ).set_index("ticker")


def events(ticker: str) -> pd.DataFrame:
    """Bir hissenin papi'deki olayları (KAP bildirimlerinden): duyuru zamanı, olay kodu, önem, öncelik."""
    q = """
        SELECT e.announced_at, e.code, e.materiality, e.materiality_band, e.priority,
               d.source_ref AS kap_index, d.title
        FROM event e
        JOIN json_each(e.instrument_ids) j
        JOIN identifier i ON i.entity_id = j.value AND i.scheme = 'TICKER' AND i.value = ?
        LEFT JOIN disclosure d ON d.id = e.primary_disclosure_id
        WHERE e.superseded_by IS NULL
        ORDER BY e.announced_at
    """
    with connect() as conn:
        df = pd.read_sql_query(q, conn, params=[ticker])
    df["announced_at"] = pd.to_datetime(df["announced_at"], utc=True).dt.tz_convert("Europe/Istanbul")
    return df


def usdtry(start: str = "2015-01-01") -> pd.Series:
    """Günlük USD/TRY kapanışı (Yahoo ``TRY=X``), ``data/usdtry.csv``'de önbelleklenir."""
    path = DATA_DIR / "usdtry.csv"
    if path.exists():
        s = pd.read_csv(path, index_col=0, parse_dates=True)["usdtry"]
        if s.index.min() <= pd.Timestamp(start) and (pd.Timestamp.now() - s.index.max()).days < 3:
            return s[start:]
    p1 = int(pd.Timestamp(start).timestamp())
    p2 = int(pd.Timestamp.now().timestamp())
    r = httpx.get(
        "https://query1.finance.yahoo.com/v8/finance/chart/TRY=X",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "papi-learn/0.1"},
        timeout=30,
    )
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
    s = pd.Series(res["indicators"]["quote"][0]["close"], index=idx, name="usdtry").dropna()
    s = s[~s.index.duplicated(keep="last")]
    DATA_DIR.mkdir(exist_ok=True)
    s.to_frame().to_csv(path)
    return s
