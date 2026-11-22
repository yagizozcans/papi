"""TEFAS fund prices (``https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir``) — the only price
source for ``fund`` instruments (BIST equity sources do not carry TEFAS funds).

POST JSON ``{"fonKodu": "TI1", "dil": "TR", "periyod": N}`` → ``{"errorCode": null, "errorMessage":
null, "resultList": [{"fonKodu", "fonUnvan", "tarih": "YYYY-MM-DD", "fiyat": 1784.708985, ...}]}``.
``periyod`` is a look-back in months counted from today and only ``1, 3, 6, 12, 36, 60`` are accepted
(other values answer "Sistem Hatası!!"); older dates are not retrievable. An unknown fund code answers
200 with an empty ``resultList``. ``fiyat`` is the fund's unit price for ``tarih`` (one per business
day); stored as the bar's close.

Personal use only (01 §3.3).
"""

from __future__ import annotations

import json
import math
from datetime import date

import httpx

from papi.core.timeutil import ist_date
from papi.sources.prices.base import Bar, Fetched, Throttle, http_get

SOURCE_CODE = "tefas_prices"
PRICE_URL = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
PERIODS_MONTHS = (1, 3, 6, 12, 36, 60)


def _rows(data: bytes) -> list[dict]:
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get("errorCode") or doc.get("errorMessage"):
        raise ValueError(f"error response: {str(doc)[:200]}")
    return doc.get("resultList") or []


def parse(data: bytes) -> list[Bar]:
    bars: dict[date, Bar] = {}
    for row in _rows(data):
        price = row.get("fiyat")
        if price is None:
            continue
        ts = date.fromisoformat(row["tarih"])
        bars[ts] = Bar(ts=ts, close=float(price))
    return [bars[k] for k in sorted(bars)]


def period_months(start: date, today: date | None = None) -> int:
    """Smallest accepted ``periyod`` whose shortest possible span (28-day months) reaches back to
    ``start``; capped at 60 months."""
    days = max(0, ((today or ist_date()) - start).days)
    needed = max(1, math.ceil(days / 28))
    return next((p for p in PERIODS_MONTHS if p >= needed), PERIODS_MONTHS[-1])


def _request(client: httpx.Client, throttle: Throttle, code: str, months: int) -> Fetched:
    return http_get(
        client,
        throttle,
        source_code=SOURCE_CODE,
        ticker=code,
        url=PRICE_URL,
        params={},
        parse=parse,
        json_body={"fonKodu": code, "dil": "TR", "periyod": months},
    )


def fetch(
    client: httpx.Client, throttle: Throttle, ticker: str, *, is_index: bool, start: date, end: date
) -> list[Fetched]:
    """Unit prices of fund ``ticker`` in ``[start, end]`` (one request; ``is_index`` is ignored)."""
    f = _request(client, throttle, ticker, period_months(start))
    f.bars = [bar for bar in f.bars if start <= bar.ts <= end]
    return [f]


def fund_title(client: httpx.Client, throttle: Throttle, code: str) -> str | None:
    """Official fund name for ``code``, or ``None`` when TEFAS has no prices for it in the last month.

    Raises ``RuntimeError`` when TEFAS cannot be reached or answers with an error."""
    f = _request(client, throttle, code, PERIODS_MONTHS[0])
    if not f.ok or f.body is None:
        raise RuntimeError(f"TEFAS: {f.outcome} — {f.error}")
    rows = _rows(f.body)
    return next((r["fonUnvan"] for r in rows if r.get("fonUnvan")), None)
