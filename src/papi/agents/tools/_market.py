"""Live market data for the strategy tools (not a tool: modules starting with ``_`` are skipped).

Read straight from the public endpoints papi already uses for prices, never stored: İş Yatırım
``HisseTekil`` (adjusted close, USD-based price, BIST 100, USD/TRY, market cap per session),
İş Yatırım ``IndexHistoricalAll`` (BIST indices), İş Yatırım ``MaliTablo`` (financial statements)
and the Yahoo chart API (commodities, FX, US rates).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from papi.core.timeutil import IST
from papi.sources.prices import isyatirim, yahoo
from papi.sources.prices.base import make_client

FINANCIALS_URL = "https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo"

# name → (Yahoo symbol, unit)
MACRO: dict[str, tuple[str, str]] = {
    "BRENT": ("BZ=F", "USD/varil"),
    "WTI": ("CL=F", "USD/varil"),
    "HEATINGOIL": ("HO=F", "USD/galon (jet yakıtı vekili)"),
    "NATGAS": ("NG=F", "USD/MMBtu"),
    "GOLD": ("GC=F", "USD/ons"),
    "USDTRY": ("USDTRY=X", "TRY"),
    "EURTRY": ("EURTRY=X", "TRY"),
    "EURUSD": ("EURUSD=X", "USD"),
    "DXY": ("DX-Y.NYB", "endeks"),
    "US10Y": ("^TNX", "% getiri"),
    "VIX": ("^VIX", "endeks"),
    "SP500": ("^GSPC", "endeks"),
}


def _get(url: str, params: dict[str, Any]) -> Any:
    with make_client() as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return json.loads(r.content)


def stock_rows(ticker: str, start: date, end: date) -> list[dict[str, Any]]:
    """Per session: adjusted TRY close, USD-based price, BIST 100, USD/TRY, market cap (TRY, USD)."""
    rows: dict[date, dict[str, Any]] = {}
    for a, b in isyatirim._chunks(start, end):
        doc = _get(
            isyatirim.EQUITY_URL,
            {"hisse": ticker, "startdate": a.strftime("%d-%m-%Y"), "enddate": b.strftime("%d-%m-%Y")},
        )
        if not doc.get("ok"):
            raise ValueError(f"İş Yatırım: {doc.get('errorDescription') or 'veri yok'}")
        for v in doc.get("value") or []:
            if v.get("HGDG_KAPANIS") is None:
                continue
            day = datetime.strptime(v["HGDG_TARIH"], "%d-%m-%Y").date()
            rows[day] = {
                "date": day,
                "close": v["HGDG_KAPANIS"],
                "close_usd": v.get("DOLAR_BAZLI_FIYAT"),
                "xu100": v.get("END_DEGER"),
                "usdtry": v.get("DD_DEGER"),
                "mcap_try": v.get("PD"),
                "mcap_usd": v.get("PD_USD"),
                "free_float_mcap_try": v.get("HAO_PD"),
                "value_traded_try": v.get("HG_HACIM"),
            }
    return [rows[k] for k in sorted(rows)]


def index_rows(code: str, start: date, end: date) -> list[dict[str, Any]]:
    out: dict[date, float] = {}
    for a, b in isyatirim._chunks(start, end):
        params = {
            "period": 1440,
            "from": a.strftime("%Y%m%d") + "000000",
            "to": b.strftime("%Y%m%d") + "235959",
            "endeks": code,
        }
        with make_client() as client:
            r = client.get(isyatirim.INDEX_URL, params=params)
            r.raise_for_status()
        for bar in isyatirim.parse_index(r.content):
            if start <= bar.ts <= end:
                out[bar.ts] = bar.close
    return [{"date": k, "close": out[k]} for k in sorted(out)]


def macro_rows(name: str, start: date, end: date) -> list[dict[str, Any]]:
    symbol, _unit = MACRO[name]
    params = {
        "period1": int(datetime.combine(start, time(0, 0), tzinfo=IST).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=UTC).timestamp()),
        "interval": "1d",
    }
    with make_client() as client:
        r = client.get(yahoo.CHART_URL.format(symbol=symbol), params=params)
        r.raise_for_status()
    return [{"date": b.ts, "close": b.close} for b in yahoo.parse_chart(r.content, start, end)]


def financial_rows(ticker: str, periods: list[tuple[int, int]], currency: str, group: str) -> list[dict]:
    """MaliTablo rows for 1–4 (year, month) periods: ``[{itemCode, itemDescTr, value1..4}]``; the
    endpoint needs exactly four periods, so a short list is padded with its last period."""
    periods = periods + [periods[-1]] * (4 - len(periods))
    params: dict[str, Any] = {"companyCode": ticker, "exchange": currency, "financialGroup": group}
    for i, (y, m) in enumerate(periods, start=1):
        params[f"year{i}"], params[f"period{i}"] = y, m
    doc = _get(FINANCIALS_URL, params)
    if not doc.get("ok", True) and not doc.get("value"):
        raise ValueError(f"İş Yatırım: {doc.get('errorDescription') or 'veri yok'}")
    return list(doc.get("value") or [])
