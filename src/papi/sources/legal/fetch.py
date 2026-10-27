"""Polite fetcher for the legal corpus (source ``legal_corpus``, KB-1).

Each manifest document is downloaded from its first working URL, archived in the raw store (every
fetch is logged in ``fetch_log``) and compared with the last archived sha256: unchanged content is
reported as such and not re-parsed. Requests are sequential, at least ``min_request_interval``
seconds apart, with the honest User-Agent from settings, and capped per run (``max_requests``).

State per document in ``kv_state``: ``legal:doc:<id>`` → ``{sha, url, content_type, fetched_at,
changed_at, checked_at}``; historical texts (manifest ``history``) under ``legal:hist:<id>:<n>``
(fetched once: they never change).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from papi.core import db, rawstore
from papi.core.config import get_settings
from papi.core.logging import get_logger
from papi.core.state import get_state, set_state
from papi.core.timeutil import iso_utc, now_utc, parse_iso
from papi.sources.legal.extract import HTML, ExtractionError, extract, sniff_mime
from papi.sources.legal.manifest import ManifestEntry

SOURCE_CODE = "legal_corpus"
FETCHER_VERSION = "papi-legal-fetch/1"
DOC_STATE = "legal:doc:"
HIST_STATE = "legal:hist:"

log = get_logger("sources.legal.fetch")

_KEEP_HEADERS = ("content-type", "content-length", "last-modified", "etag", "content-disposition", "date")


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class FetchOutcome:
    entry_id: str
    role: str  # current | history:<n>
    status: str  # changed | unchanged | failed | skipped
    url: str | None = None
    sha: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


def legal_settings() -> dict[str, Any]:
    raw = get_settings().section("legal")
    return {
        "min_request_interval": float(raw.get("min_request_interval", 1.5)),
        "request_timeout": float(raw.get("request_timeout", 60.0)),
        "max_requests": int(raw.get("max_requests", 60)),
    }


class PoliteClient:
    """Sequential HTTP GETs with a minimum gap between request starts and a request budget."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        min_interval: float | None = None,
        timeout: float | None = None,
        max_requests: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        cfg = legal_settings()
        self.min_interval = cfg["min_request_interval"] if min_interval is None else min_interval
        self.max_requests = cfg["max_requests"] if max_requests is None else max_requests
        ua = user_agent or get_settings().kap.resolved_user_agent
        self._client = httpx.Client(
            headers={"User-Agent": ua, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"},
            timeout=cfg["request_timeout"] if timeout is None else timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.requests_made = 0

    def get(self, url: str) -> httpx.Response:
        if self.requests_made >= self.max_requests:
            raise BudgetExhausted(f"request budget of {self.max_requests} used up")
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.requests_made += 1
        try:
            return self._client.get(url)
        finally:
            self._last = self._clock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _validate(data: bytes, content_type: str | None) -> str:
    """Return the document mime, or raise ``ExtractionError`` for non-documents (SPA shells, errors)."""
    if not data:
        raise ExtractionError("empty body")
    mime = sniff_mime(data, content_type)
    if mime == HTML:
        lines = extract(data, content_type).lines
        if len(lines) < 20:
            raise ExtractionError(f"HTML page with only {len(lines)} text lines (not a legal text)")
    return mime


def _download(client: PoliteClient, entry_id: str, urls: list[str], outcome: FetchOutcome) -> bytes | None:
    for url in urls:
        started = now_utc()
        try:
            resp = client.get(url)
        except BudgetExhausted:
            raise
        except httpx.TimeoutException as exc:
            rawstore.log_fetch_failure(
                source_code=SOURCE_CODE,
                url=url,
                outcome="timeout",
                error=str(exc),
                fetcher_version=FETCHER_VERSION,
                started_at=started,
            )
            outcome.attempts.append({"url": url, "error": f"timeout: {exc}"})
            continue
        except httpx.HTTPError as exc:
            rawstore.log_fetch_failure(
                source_code=SOURCE_CODE,
                url=url,
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
                fetcher_version=FETCHER_VERSION,
                started_at=started,
            )
            outcome.attempts.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        ctype = resp.headers.get("content-type")
        if resp.status_code != 200:
            rawstore.log_fetch_failure(
                source_code=SOURCE_CODE,
                url=url,
                outcome="http_error",
                http_status=resp.status_code,
                error=f"HTTP {resp.status_code}",
                fetcher_version=FETCHER_VERSION,
                started_at=started,
            )
            outcome.attempts.append({"url": url, "http_status": resp.status_code})
            continue
        data = resp.content
        try:
            mime = _validate(data, ctype)
        except ExtractionError as exc:
            rawstore.log_fetch_failure(
                source_code=SOURCE_CODE,
                url=url,
                outcome="error",
                http_status=resp.status_code,
                error=f"not a document: {exc}",
                fetcher_version=FETCHER_VERSION,
                started_at=started,
            )
            outcome.attempts.append({"url": url, "http_status": resp.status_code, "error": str(exc)})
            continue
        headers = {k: v for k, v in resp.headers.items() if k.lower() in _KEEP_HEADERS}
        sha = rawstore.put_bytes(
            data,
            source_code=SOURCE_CODE,
            url=url,
            http_status=resp.status_code,
            content_type=mime,
            request_meta={"manifest_id": entry_id, "role": outcome.role},
            response_headers=headers,
            fetcher_version=FETCHER_VERSION,
            started_at=started,
        )
        outcome.url, outcome.sha, outcome.http_status, outcome.content_type = url, sha, resp.status_code, mime
        outcome.attempts.append({"url": url, "http_status": resp.status_code, "sha": sha})
        return data
    return None


def fetch_entry(
    entry: ManifestEntry, client: PoliteClient, *, include_history: bool = True
) -> list[FetchOutcome]:
    """Fetch the current text (and missing historical texts) of one manifest document."""
    outcomes = []
    cur = FetchOutcome(entry_id=entry.id, role="current", status="failed")
    outcomes.append(cur)
    try:
        data = _download(client, entry.id, entry.source_urls, cur)
    except BudgetExhausted as exc:
        cur.status, cur.error = "skipped", str(exc)
        return outcomes
    now = now_utc()
    with db.transaction() as conn:
        key = DOC_STATE + entry.id
        prev = get_state(conn, key) or {}
        if data is None:
            cur.error = "all source URLs failed"
            set_state(conn, key, {**prev, "checked_at": iso_utc(now), "last_error": cur.error})
        else:
            changed = prev.get("sha") != cur.sha
            cur.status = "changed" if changed else "unchanged"
            set_state(
                conn,
                key,
                {
                    "sha": cur.sha,
                    "url": cur.url,
                    "content_type": cur.content_type,
                    "fetched_at": iso_utc(now),
                    "changed_at": iso_utc(now) if changed else prev.get("changed_at"),
                    "checked_at": iso_utc(now),
                    "previous_sha": prev.get("sha") if changed else prev.get("previous_sha"),
                    "last_error": None,
                },
            )
    if cur.status == "failed":
        log.warning("legal fetch failed", extra={"manifest_id": entry.id, "attempts": cur.attempts})
    if not include_history:
        return outcomes
    for n, hist in enumerate(entry.history):
        out = FetchOutcome(entry_id=entry.id, role=f"history:{n}", status="failed")
        outcomes.append(out)
        hkey = f"{HIST_STATE}{entry.id}:{n}"
        with db.connect() as conn:
            existing = get_state(conn, hkey)
        if existing and existing.get("url") == hist.url and rawstore.exists(existing["sha"]):
            out.status, out.sha, out.url = "unchanged", existing["sha"], hist.url
            out.content_type = existing.get("content_type")
            continue
        try:
            data = _download(client, entry.id, [hist.url], out)
        except BudgetExhausted as exc:
            out.status, out.error = "skipped", str(exc)
            continue
        if data is None:
            out.error = "historical source failed"
            continue
        out.status = "changed"
        with db.transaction() as conn:
            set_state(
                conn,
                hkey,
                {
                    "sha": out.sha,
                    "url": hist.url,
                    "content_type": out.content_type,
                    "fetched_at": iso_utc(now_utc()),
                },
            )
    return outcomes


def fetch_entries(
    entries: list[ManifestEntry], *, client: PoliteClient | None = None, include_history: bool = True
) -> list[FetchOutcome]:
    """Fetch several documents sequentially, sharing one rate-limited client and request budget."""
    own = client is None
    client = client or PoliteClient()
    try:
        out: list[FetchOutcome] = []
        for entry in entries:
            out.extend(fetch_entry(entry, client, include_history=include_history))
        return out
    finally:
        if own:
            client.close()


def due_entries(entries: list[ManifestEntry], now: datetime | None = None) -> list[ManifestEntry]:
    """Documents whose last check is older than their manifest ``refresh`` interval (or never checked)."""
    now = now or now_utc()
    due = []
    with db.connect() as conn:
        for e in entries:
            st = get_state(conn, DOC_STATE + e.id) or {}
            checked = st.get("checked_at")
            if checked is None or (now - parse_iso(checked)).total_seconds() >= e.refresh_seconds:
                due.append(e)
    return due


def document_state(conn: Any, entry_id: str) -> dict[str, Any]:
    return get_state(conn, DOC_STATE + entry_id) or {}


def history_state(conn: Any, entry_id: str, n: int) -> dict[str, Any]:
    return get_state(conn, f"{HIST_STATE}{entry_id}:{n}") or {}
