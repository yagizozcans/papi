"""Yahoo Finance chart API (``/v8/finance/chart/<TICKER>.IS``) — cross-check / fallback source.

Yahoo's ``quote`` OHLCV is split-adjusted (BIST bedelsiz issues appear as ``splits`` events, e.g.
ASELS 2023-08-25 ``200:100``), not dividend-adjusted. papi stores *raw* bars, so prices before each
split are multiplied back by ``numerator/denominator`` and volumes divided by it. Splits are only
reported inside the requested window, so the request always runs to *now* and bars are filtered to
``[start, end]`` afterwards. Bars with a null close (the session still in progress) are skipped.
``adjclose`` is never used (01 §3.3: adjusted series come from KAP corporate actions).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from functools import partial

import httpx

from papi.core.timeutil import IST, now_utc
from papi.sources.prices.base import Bar, Fetched, Throttle, http_get

SOURCE_CODE = "yahoo_prices"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def symbol_for(ticker: str) -> str:
    return f"{ticker.upper()}.IS"


def _ist_date(epoch_s: float) -> date:
    return datetime.fromtimestamp(epoch_s, tz=UTC).astimezone(IST).date()


def parse_chart(data: bytes, start: date | None = None, end: date | None = None) -> list[Bar]:
    doc = json.loads(data)
    chart = doc.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"chart error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        return []
    res = results[0]
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    splits = []
    for sp in ((res.get("events") or {}).get("splits") or {}).values():
        num, den = float(sp["numerator"]), float(sp["denominator"])
        if num > 0 and den > 0:
            splits.append((_ist_date(sp["date"]), num / den))

    def col(name: str, i: int) -> float | None:
        values = quote.get(name) or []
        v = values[i] if i < len(values) else None
        return None if v is None else float(v)

    bars: dict[date, Bar] = {}
    for i, epoch in enumerate(stamps):
        close = col("close", i)
        if close is None:
            continue
        ts = _ist_date(epoch)
        if (start and ts < start) or (end and ts > end):
            continue
        factor = 1.0
        for split_date, ratio in splits:
            if ts < split_date:
                factor *= ratio

        def px(v: float | None, f: float = factor) -> float | None:
            return None if v is None else v * f

        vol = col("volume", i)
        bars[ts] = Bar(
            ts=ts,
            close=close * factor,
            open=px(col("open", i)),
            high=px(col("high", i)),
            low=px(col("low", i)),
            volume=None if vol is None else vol / factor,
        )
    return [bars[k] for k in sorted(bars)]


def fetch(
    client: httpx.Client, throttle: Throttle, ticker: str, *, is_index: bool, start: date, end: date
) -> list[Fetched]:
    """One request from ``start`` to now (to see later splits); bars filtered to ``[start, end]``."""
    del is_index  # same symbol scheme for indices (XU100.IS)
    period1 = int(datetime.combine(start, time(0, 0), tzinfo=IST).timestamp())
    period2 = int((now_utc() + timedelta(minutes=1)).timestamp())
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    url = CHART_URL.format(symbol=symbol_for(ticker))
    parse = partial(parse_chart, start=start, end=end)
    return [
        http_get(
            client, throttle, source_code=SOURCE_CODE, ticker=ticker, url=url, params=params, parse=parse
        )
    ]
