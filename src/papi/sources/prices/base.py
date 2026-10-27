"""Shared pieces of the EOD price adapters: bar/fetch records, HTTP client, request throttle."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from papi.core.config import Settings, get_settings
from papi.core.timeutil import now_utc

FETCHER_VERSION = "papi-prices/1"
MIN_REQUEST_INTERVAL_S = 1.0
MAX_ATTEMPTS = 2
RETRY_BACKOFF_S = 3.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Bar:
    """One raw (unadjusted) daily bar; ``ts`` is the Istanbul session date."""

    ts: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None  # shares
    value_traded: float | None = None  # TRY


@dataclass
class Fetched:
    """Outcome of one HTTP request: raw body (stored in rawstore when ``ok``) + parsed bars."""

    source_code: str
    ticker: str
    url: str
    started_at: datetime
    request_meta: dict[str, Any]
    http_status: int | None = None
    body: bytes | None = None
    content_type: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    bars: list[Bar] = field(default_factory=list)
    outcome: str = "ok"  # ok | http_error | timeout | error
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class Throttle:
    """Keeps ≥ ``interval`` seconds between consecutive requests (sequential fetching)."""

    def __init__(
        self,
        interval: float = MIN_REQUEST_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def backoff(self, seconds: float) -> None:
        """Pause before a retry (skipped when the throttle is disabled, e.g. in tests)."""
        if self.interval > 0:
            self._sleep(seconds)


def make_client(settings: Settings | None = None) -> httpx.Client:
    s = settings or get_settings()
    return httpx.Client(
        headers={"User-Agent": s.kap.resolved_user_agent, "Accept": "application/json"},
        timeout=s.prices.request_timeout,
        follow_redirects=True,
    )


def default_throttle(settings: Settings | None = None) -> Throttle:
    s = settings or get_settings()
    return Throttle(max(MIN_REQUEST_INTERVAL_S, s.kap.min_request_interval))


def _transient(f: Fetched) -> bool:
    return (f.outcome in ("timeout", "error") and f.http_status is None) or f.http_status in RETRY_STATUSES


def http_get(
    client: httpx.Client,
    throttle: Throttle,
    *,
    source_code: str,
    ticker: str,
    url: str,
    params: dict[str, Any],
    parse: Callable[[bytes], list[Bar]],
    attempts: int = MAX_ATTEMPTS,
) -> Fetched:
    """GET ``url`` (throttled) and parse a 200 body; errors are captured, never raised.

    Timeouts, transport errors, 429 and 5xx are retried (``attempts`` in total, ``RETRY_BACKOFF_S``
    apart); the returned record describes the last attempt.
    """
    f = _get_once(
        client, throttle, source_code=source_code, ticker=ticker, url=url, params=params, parse=parse
    )
    for n in range(2, attempts + 1):
        if not _transient(f):
            break
        throttle.backoff(RETRY_BACKOFF_S)
        f = _get_once(
            client, throttle, source_code=source_code, ticker=ticker, url=url, params=params, parse=parse
        )
        f.request_meta["attempt"] = n
    return f


def _get_once(
    client: httpx.Client,
    throttle: Throttle,
    *,
    source_code: str,
    ticker: str,
    url: str,
    params: dict[str, Any],
    parse: Callable[[bytes], list[Bar]],
) -> Fetched:
    throttle.wait()
    req = httpx.Request("GET", url, params=params)
    f = Fetched(
        source_code=source_code,
        ticker=ticker,
        url=str(req.url),
        started_at=now_utc(),
        request_meta={"params": {k: str(v) for k, v in params.items()}, "ticker": ticker},
    )
    try:
        resp = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        f.outcome, f.error = "timeout", f"{type(exc).__name__}: {exc}"
        return f
    except httpx.HTTPError as exc:
        f.outcome, f.error = "error", f"{type(exc).__name__}: {exc}"
        return f
    f.http_status = resp.status_code
    f.content_type = resp.headers.get("content-type")
    f.response_headers = {
        k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "date", "cache-control")
    }
    if resp.status_code != 200:
        f.outcome = "http_error"
        f.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return f
    f.body = resp.content
    try:
        f.bars = parse(resp.content)
    except (ValueError, KeyError, TypeError) as exc:
        f.outcome, f.error = "error", f"parse: {type(exc).__name__}: {exc}"
    return f
