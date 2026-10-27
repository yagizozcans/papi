"""İş Yatırım public historical price endpoints (the ones the ``isyatirimhisse`` library wraps).

- Equities: ``Data.aspx/HisseTekil?hisse=THYAO&startdate=DD-MM-YYYY&enddate=DD-MM-YYYY`` → JSON
  ``{"ok": true, "value": [{...}]}``. ``HG_*`` fields are raw (unadjusted) prices, ``HGDG_*`` are
  adjusted for corporate actions (verified on ASELS' 2023 %100 bedelsiz). Fields used: ``HGDG_TARIH``
  (session date), ``HG_KAPANIS`` close, ``HG_MAX`` high, ``HG_MIN`` low, ``HG_HACIM`` traded value
  (TRY). The endpoint has no open price and no share volume.
- Indices: ``ChartData.aspx/IndexHistoricalAll?period=1440&from=YYYYMMDD000000&to=YYYYMMDD235959
  &endeks=XU100`` → ``{"data": [[epoch_ms, close], ...]}``; ``epoch_ms`` is Istanbul midnight of the
  session date. Close only.

Personal use only (01 §3.3).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx

from papi.core.timeutil import IST
from papi.sources.prices.base import Bar, Fetched, Throttle, http_get

SOURCE_CODE = "isyatirim_prices"
EQUITY_URL = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/HisseTekil"
INDEX_URL = (
    "https://www.isyatirim.com.tr/_Layouts/15/IsYatirim.Website/Common/ChartData.aspx/IndexHistoricalAll"
)
CHUNK_DAYS = 366


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)  # type: ignore[arg-type]


def parse_equity(data: bytes) -> list[Bar]:
    doc = json.loads(data)
    if not isinstance(doc, dict) or doc.get("ok") is False:
        raise ValueError(f"error response: {str(doc)[:200]}")
    bars: dict[date, Bar] = {}
    for row in doc.get("value") or []:
        close = _num(row.get("HG_KAPANIS"))
        if close is None:
            continue
        ts = datetime.strptime(row["HGDG_TARIH"], "%d-%m-%Y").date()
        bars[ts] = Bar(
            ts=ts,
            close=close,
            high=_num(row.get("HG_MAX")),
            low=_num(row.get("HG_MIN")),
            value_traded=_num(row.get("HG_HACIM")),
        )
    return [bars[k] for k in sorted(bars)]


def parse_index(data: bytes) -> list[Bar]:
    doc = json.loads(data)
    if not isinstance(doc, dict) or "data" not in doc:
        raise ValueError(f"unexpected response: {str(doc)[:200]}")
    bars: dict[date, Bar] = {}
    for epoch_ms, value in doc["data"] or []:
        if value is None:
            continue
        ts = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).astimezone(IST).date()
        bars[ts] = Bar(ts=ts, close=float(value))
    return [bars[k] for k in sorted(bars)]


def _chunks(start: date, end: date) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=CHUNK_DAYS - 1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def fetch(
    client: httpx.Client, throttle: Throttle, ticker: str, *, is_index: bool, start: date, end: date
) -> list[Fetched]:
    """Fetch raw daily bars for ``ticker`` in ``[start, end]`` (one request per ≤ 1-year chunk)."""
    results: list[Fetched] = []
    for a, b in _chunks(start, end):
        if is_index:
            params = {
                "period": 1440,
                "from": a.strftime("%Y%m%d") + "000000",
                "to": b.strftime("%Y%m%d") + "235959",
                "endeks": ticker,
            }
            url, parse = INDEX_URL, parse_index
        else:
            params = {"hisse": ticker, "startdate": a.strftime("%d-%m-%Y"), "enddate": b.strftime("%d-%m-%Y")}
            url, parse = EQUITY_URL, parse_equity
        f = http_get(
            client, throttle, source_code=SOURCE_CODE, ticker=ticker, url=url, params=params, parse=parse
        )
        f.bars = [bar for bar in f.bars if start <= bar.ts <= end]
        results.append(f)
    return results
