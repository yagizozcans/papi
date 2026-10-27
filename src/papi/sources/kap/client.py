"""KAP access layer: the ``KapClient`` protocol and ``KapPublicWebClient`` (kap.org.tr JSON endpoints).

Pipeline code depends only on ``KapClient``. The public-web client is polite by construction:

* **one request at a time** process-wide (a lock) with at least ``kap.min_request_interval`` seconds
  between requests, an honest User-Agent, and bounded retries with exponential backoff + jitter;
* a **circuit breaker** (state in ``kv_state`` ``kap:circuit`` so every process honours it) that opens
  immediately on WAF-style answers (non-standard status such as 666, 403, 429) and on a spike of 5xx /
  network failures; the triggering fetch is logged with ``fetch_log.outcome = 'blocked'``;
* **every response body is archived** through ``papi.core.rawstore.put_bytes`` (source ``kap``) in its
  own short transaction, so bytes survive any later parse failure. Callers must not hold an open
  write transaction on the same database while calling the client.

Endpoints (verified live 2026-10-28; unofficial, may change — see drift checks in ``drift.py``):

* ``POST /tr/api/disclosure/members/byCriteria`` ``{"fromDate","toDate","mkkMemberOidList",
  "subjectList"}`` → JSON array, newest first, **capped at 2000 rows** (windows are split).
  Fund disclosures (``disclosureType`` ``FON``) are not included.
* ``GET /tr/api/notification/attachment-detail/<index>`` → ``[{disclosure, disclosureBody, attachments}]``;
  an unknown/non-public index answers HTTP 500 ``{"success":false,...}``.
* ``GET /tr/api/file/download/<objId>`` → Java-serialised ``byte[]`` wrapping the file.
* ``GET /tr/api/BildirimPdf/<index>`` → KAP-rendered PDF (plain).
* ``GET /tr/api/company/items/<memberType>/A`` → member list (``IGS`` = listed companies).
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

from papi.core import db, metrics, rawstore
from papi.core.config import get_settings
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc, parse_iso

log = logging.getLogger("papi.kap.client")

SOURCE_CODE = "kap"
FETCHER_VERSION = "kap-public-web/1"
LIST_CAP = 2000
CIRCUIT_KEY = "kap:circuit"

# Statuses a normal web stack returns; anything else (KAP's WAF answers 666) is treated as a block.
_STANDARD_STATUSES = frozenset(
    {200, 201, 202, 204, 301, 302, 303, 304, 307, 308, 400, 401, 404, 405, 406, 408, 409, 410, 413, 414}
    | {415, 422, 500, 502, 503, 504}
)
_BLOCK_STATUSES = frozenset({403, 429})
_RETRY_STATUSES = frozenset({408, 500, 502, 503, 504})

FAILURE_THRESHOLD = 5  # consecutive 5xx/network failures that open the circuit
OPEN_BASE_S = 300.0
OPEN_MAX_S = 3600.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0

JAVA_STREAM_MAGIC = b"\xac\xed\x00\x05"


# ------------------------------------------------------------------------------------------ errors


class KapError(Exception):
    """Base error of the KAP adapter."""


class KapNotFound(KapError):
    """The requested disclosure index / file does not exist (or is not public)."""


class KapHttpError(KapError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class KapBlocked(KapError):
    """Circuit open (WAF block or failure spike); retry after ``until``."""

    def __init__(self, message: str, until: datetime) -> None:
        super().__init__(message)
        self.until = until


# ------------------------------------------------------------------------------------------ results


@dataclass(frozen=True)
class KapJson:
    data: Any
    raw_sha256: str
    url: str


@dataclass(frozen=True)
class KapFile:
    content: bytes  # unwrapped file bytes
    raw_sha256: str  # sha of the archived response body (as served)
    url: str
    content_type: str | None = None
    file_name: str | None = None


@dataclass
class KapListResult:
    items: list[dict[str, Any]]
    raw_sha256s: list[str] = field(default_factory=list)
    truncated_days: list[date] = field(default_factory=list)  # single days still at the cap
    requests: int = 0


@runtime_checkable
class KapClient(Protocol):
    """What the ingestion pipeline needs from KAP (public web today, licensed API later)."""

    def list_disclosures(
        self, from_date: date, to_date: date, *, member_oids: Sequence[str] = ()
    ) -> KapListResult:
        """All disclosures published in ``[from_date, to_date]`` (Istanbul dates), de-duplicated and
        sorted by index; ``member_oids`` (``mkkMemberOid``) restricts to those filers."""
        ...

    def disclosure_detail(self, index: int) -> KapJson:
        """Detail of one disclosure: ``data`` is the single ``{disclosure, disclosureBody, attachments}``."""
        ...

    def download_attachment(self, obj_id: str, *, index: int | None = None) -> KapFile: ...

    def disclosure_pdf(self, index: int) -> KapFile: ...

    def companies(self, member_type: str = "IGS") -> KapJson: ...


# ------------------------------------------------------------------------------------------ helpers


def unwrap_java_bytes(raw: bytes) -> bytes:
    """Return the payload of a Java-serialised ``byte[]`` (``AC ED 00 05 75 72 … 78 70 <len> <bytes>``).

    Bytes that are not a Java stream (already a raw PDF, etc.) are returned unchanged.
    """
    if not raw.startswith(JAVA_STREAM_MAGIC):
        return raw
    pos = 4
    if raw[pos] != 0x75:  # TC_ARRAY
        raise ValueError("java stream does not start with an array")
    pos += 1
    marker = raw[pos]
    if marker == 0x72:  # TC_CLASSDESC
        pos += 1
        name_len = int.from_bytes(raw[pos : pos + 2], "big")
        pos += 2
        name = raw[pos : pos + name_len]
        if name != b"[B":
            raise ValueError(f"java array of {name!r}, expected byte[]")
        pos += name_len + 8  # serialVersionUID
        pos += 1  # classDescFlags
        field_count = int.from_bytes(raw[pos : pos + 2], "big")
        pos += 2
        if field_count:
            raise ValueError("byte[] class descriptor with fields")
        if raw[pos] != 0x78:  # TC_ENDBLOCKDATA (no annotations expected)
            raise ValueError("unexpected class annotation in byte[] descriptor")
        pos += 1
        if raw[pos] != 0x70:  # TC_NULL superclass
            raise ValueError("unexpected superclass in byte[] descriptor")
        pos += 1
    elif marker == 0x71:  # TC_REFERENCE to an earlier descriptor
        pos += 5
    else:
        raise ValueError(f"unexpected java class marker 0x{marker:02x}")
    length = int.from_bytes(raw[pos : pos + 4], "big", signed=True)
    pos += 4
    if length < 0:
        raise ValueError("negative java array length")
    payload = raw[pos : pos + length]
    if len(payload) != length:
        raise ValueError(f"truncated java byte[]: expected {length}, got {len(payload)}")
    return payload


def split_window(from_date: date, to_date: date) -> tuple[tuple[date, date], tuple[date, date]] | None:
    """Halve an inclusive date window; ``None`` for a single day."""
    days = (to_date - from_date).days
    if days <= 0:
        return None
    mid = from_date + timedelta(days=days // 2)
    return (from_date, mid), (mid + timedelta(days=1), to_date)


class _Gate:
    """Process-wide serialisation + minimum spacing between requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self, interval: float, sleep: Callable[[float], None]) -> None:
        delay = interval - (time.monotonic() - self.last)
        if delay > 0:
            sleep(delay)

    def mark(self) -> None:
        self.last = time.monotonic()


_GATE = _Gate()


class CircuitBreaker:
    """Persisted breaker: ``closed`` → ``open`` (until a time) → closed again after the cool-down."""

    def __init__(self, *, failure_threshold: int = FAILURE_THRESHOLD) -> None:
        self.failure_threshold = failure_threshold
        self.failures = 0

    @staticmethod
    def state() -> dict[str, Any]:
        with db.connect() as conn:
            return get_state(conn, CIRCUIT_KEY, {}) or {}

    def check(self, now: datetime | None = None) -> None:
        st = self.state()
        until = st.get("open_until")
        if st.get("state") == "open" and until:
            until_dt = parse_iso(until)
            if (now or now_utc()) < until_dt:
                raise KapBlocked(f"KAP circuit open: {st.get('reason')}", until_dt)

    def success(self) -> None:
        self.failures = 0
        st = self.state()
        if st.get("state") == "open" or st.get("opens"):
            with db.transaction() as conn:
                set_state(
                    conn, CIRCUIT_KEY, {"state": "closed", "opens": 0, "closed_at": now_utc().isoformat()}
                )

    def failure(self, reason: str) -> datetime | None:
        """Count a 5xx/network failure; returns ``open_until`` if this opened the circuit."""
        self.failures += 1
        if self.failures >= self.failure_threshold:
            return self.open(f"{self.failures} consecutive failures: {reason}")
        return None

    def open(self, reason: str) -> datetime:
        from papi.sources.kap import deps

        st = self.state()
        opens = int(st.get("opens") or 0) + 1
        seconds = min(OPEN_BASE_S * 2 ** (opens - 1), OPEN_MAX_S)
        until = now_utc() + timedelta(seconds=seconds)
        self.failures = 0
        with db.transaction() as conn:
            set_state(
                conn,
                CIRCUIT_KEY,
                {"state": "open", "open_until": until.isoformat(), "reason": reason[:300], "opens": opens},
            )
            metrics.record("kap.circuit.open", 1, conn=conn, reason=reason[:80])
            deps.ops_alert(
                conn,
                f"KAP bağlantı kesici açıldı ({reason[:120]}); istekler {int(seconds // 60)} dk durduruldu.",
                level="error",
                dedupe_key="kap:circuit",
            )
        log.warning("KAP circuit opened for %.0fs: %s", seconds, reason)
        return until


# ------------------------------------------------------------------------------------------ client


class KapPublicWebClient:
    """``KapClient`` over the kap.org.tr front-end JSON endpoints."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        min_interval: float | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        cfg = get_settings().kap
        self.base_url = (base_url or cfg.base_url).rstrip("/")
        self.min_interval = cfg.min_request_interval if min_interval is None else min_interval
        self.max_retries = cfg.max_retries if max_retries is None else max_retries
        self.sleep = sleep
        self.breaker = breaker or CircuitBreaker()
        self._http = httpx.Client(
            transport=transport,
            timeout=timeout or cfg.request_timeout,
            headers={
                "User-Agent": cfg.resolved_user_agent,
                "Accept": "application/json, application/pdf, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.5",
            },
            follow_redirects=False,
        )
        self.request_count = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KapPublicWebClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport -----------------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        referer: str | None = None,
        not_found_on_500: bool = False,
    ) -> tuple[httpx.Response, str]:
        """One logical request (with retries). Returns the response and the archived sha256."""
        url = f"{self.base_url}{path}"
        headers = {"Referer": referer or f"{self.base_url}/tr/bildirim-sorgu"}
        content = None
        if body is not None:
            content = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request_meta = {"method": method, "body": body} if body is not None else {"method": method}
        attempt = 0
        while True:
            self.breaker.check()
            started = now_utc()
            with _GATE.lock:
                _GATE.wait(self.min_interval, self.sleep)
                try:
                    resp = self._http.request(method, url, headers=headers, content=content)
                    error: Exception | None = None
                except httpx.TimeoutException as exc:
                    resp, error = None, exc
                except httpx.TransportError as exc:
                    resp, error = None, exc
                finally:
                    _GATE.mark()
                    self.request_count += 1
            if resp is None:
                assert error is not None
                outcome = "timeout" if isinstance(error, httpx.TimeoutException) else "error"
                rawstore.log_fetch_failure(
                    source_code=SOURCE_CODE,
                    url=url,
                    outcome=outcome,
                    error=f"{type(error).__name__}: {error}"[:500],
                    fetcher_version=FETCHER_VERSION,
                    started_at=started,
                )
                until = self.breaker.failure(f"{outcome}: {url}")
                if until is not None:
                    raise KapBlocked(f"KAP circuit opened after network failures ({url})", until)
                attempt = self._backoff_or_raise(attempt, KapHttpError(f"{outcome} for {url}: {error}"))
                continue
            status = resp.status_code
            if status not in _STANDARD_STATUSES or status in _BLOCK_STATUSES:
                rawstore.log_fetch_failure(
                    source_code=SOURCE_CODE,
                    url=url,
                    outcome="blocked",
                    error=f"WAF/blocked status {status}",
                    http_status=status,
                    fetcher_version=FETCHER_VERSION,
                    started_at=started,
                )
                until = self.breaker.open(f"HTTP {status} from {path}")
                raise KapBlocked(f"KAP answered HTTP {status} (blocked) for {url}", until)
            if 200 <= status < 300:
                sha = rawstore.put_bytes(
                    resp.content,
                    source_code=SOURCE_CODE,
                    url=url,
                    http_status=status,
                    content_type=resp.headers.get("content-type"),
                    request_meta=request_meta,
                    response_headers=_kept_headers(resp.headers),
                    fetcher_version=FETCHER_VERSION,
                    started_at=started,
                )
                self.breaker.success()
                return resp, sha
            if status == 404 or (not_found_on_500 and status == 500 and _is_not_found_body(resp)):
                rawstore.log_fetch_failure(
                    source_code=SOURCE_CODE,
                    url=url,
                    outcome="http_error",
                    error="not found",
                    http_status=status,
                    fetcher_version=FETCHER_VERSION,
                    started_at=started,
                )
                raise KapNotFound(f"not found: {url}")
            rawstore.log_fetch_failure(
                source_code=SOURCE_CODE,
                url=url,
                outcome="http_error",
                error=resp.text[:300],
                http_status=status,
                fetcher_version=FETCHER_VERSION,
                started_at=started,
            )
            if status in _RETRY_STATUSES:
                until = self.breaker.failure(f"HTTP {status}: {path}")
                if until is not None:
                    raise KapBlocked(f"KAP circuit opened after HTTP {status} spike", until)
                attempt = self._backoff_or_raise(attempt, KapHttpError(f"HTTP {status} for {url}", status))
                continue
            raise KapHttpError(f"HTTP {status} for {url}", status)

    def _backoff_or_raise(self, attempt: int, exc: KapError) -> int:
        if attempt >= self.max_retries:
            raise exc
        delay = min(BACKOFF_BASE_S * 2**attempt, BACKOFF_MAX_S) * (1 + random.random() * 0.25)
        self.sleep(delay)
        return attempt + 1

    def _json(self, method: str, path: str, **kw: Any) -> KapJson:
        resp, sha = self._request(method, path, **kw)
        try:
            data = resp.json()
        except ValueError as exc:
            raise KapHttpError(
                f"non-JSON response from {path}: {resp.text[:120]!r}", resp.status_code
            ) from exc
        return KapJson(data=data, raw_sha256=sha, url=str(resp.request.url))

    # -- endpoints -----------------------------------------------------------------------------
    def list_once(self, from_date: date, to_date: date, *, member_oids: Sequence[str] = ()) -> KapJson:
        """Single list request (no splitting)."""
        body = {
            "fromDate": from_date.isoformat(),
            "toDate": to_date.isoformat(),
            "mkkMemberOidList": list(member_oids),
            "subjectList": [],
        }
        res = self._json("POST", "/tr/api/disclosure/members/byCriteria", body=body)
        if not isinstance(res.data, list):
            raise KapHttpError(f"list response is {type(res.data).__name__}, expected array")
        return res

    def list_disclosures(
        self, from_date: date, to_date: date, *, member_oids: Sequence[str] = ()
    ) -> KapListResult:
        out = KapListResult(items=[])
        seen: set[int] = set()
        stack = [(from_date, to_date)]
        while stack:
            lo, hi = stack.pop()
            res = self.list_once(lo, hi, member_oids=member_oids)
            out.requests += 1
            out.raw_sha256s.append(res.raw_sha256)
            if len(res.data) >= LIST_CAP:
                halves = split_window(lo, hi)
                if halves is not None:
                    # Newer half first so live items are handled before older ones.
                    stack.extend([halves[0], halves[1]])
                    continue
                out.truncated_days.append(lo)
            for item in res.data:
                idx = item.get("disclosureIndex") if isinstance(item, dict) else None
                if isinstance(idx, int) and idx not in seen:
                    seen.add(idx)
                    out.items.append(item)
        out.items.sort(key=lambda x: x["disclosureIndex"])
        return out

    def disclosure_detail(self, index: int) -> KapJson:
        res = self._json(
            "GET",
            f"/tr/api/notification/attachment-detail/{int(index)}",
            referer=f"{self.base_url}/tr/Bildirim/{int(index)}",
            not_found_on_500=True,
        )
        data = res.data
        if isinstance(data, list):
            if not data:
                raise KapNotFound(f"empty detail for {index}")
            data = data[0]
        if not isinstance(data, dict):
            raise KapHttpError(f"detail for {index} is {type(data).__name__}")
        return KapJson(data=data, raw_sha256=res.raw_sha256, url=res.url)

    def download_attachment(self, obj_id: str, *, index: int | None = None) -> KapFile:
        referer = f"{self.base_url}/tr/Bildirim/{index}" if index else None
        resp, sha = self._request("GET", f"/tr/api/file/download/{obj_id}", referer=referer)
        return KapFile(
            content=unwrap_java_bytes(resp.content),
            raw_sha256=sha,
            url=str(resp.request.url),
            content_type=resp.headers.get("content-type"),
            file_name=_disposition_name(resp.headers.get("content-disposition")),
        )

    def disclosure_pdf(self, index: int) -> KapFile:
        resp, sha = self._request(
            "GET", f"/tr/api/BildirimPdf/{int(index)}", referer=f"{self.base_url}/tr/Bildirim/{int(index)}"
        )
        return KapFile(
            content=unwrap_java_bytes(resp.content),
            raw_sha256=sha,
            url=str(resp.request.url),
            content_type=resp.headers.get("content-type"),
            file_name=f"{index}.pdf",
        )

    def companies(self, member_type: str = "IGS") -> KapJson:
        res = self._json("GET", f"/tr/api/company/items/{member_type}/A")
        if not isinstance(res.data, list):
            raise KapHttpError(f"company list is {type(res.data).__name__}, expected array")
        return res


def _kept_headers(headers: httpx.Headers) -> dict[str, str]:
    keep = ("content-type", "content-length", "content-disposition", "date", "last-modified", "etag")
    return {k: v for k, v in headers.items() if k.lower() in keep}


def _is_not_found_body(resp: httpx.Response) -> bool:
    try:
        data = resp.json()
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("success") is False


def _disposition_name(value: str | None) -> str | None:
    if not value:
        return None
    from urllib.parse import unquote

    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            enc = part.split("=", 1)[1]
            return unquote(enc.split("''", 1)[-1])
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


def default_client() -> KapClient:
    """The configured client (public web; a licensed-API client would be selected here)."""
    return KapPublicWebClient()


__all__ = [
    "LIST_CAP",
    "KapBlocked",
    "KapClient",
    "KapError",
    "KapFile",
    "KapHttpError",
    "KapJson",
    "KapListResult",
    "KapNotFound",
    "KapPublicWebClient",
    "default_client",
    "split_window",
    "unwrap_java_bytes",
]
