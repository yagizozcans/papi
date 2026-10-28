"""Live updates from ``event_log`` for feed-relevant event types.

- ``GET /stream/poll`` (dashboard): short polling. Returns at once with the rows handled since ``?after=``
  (rendered feed items matching the page's filters) and the next cursor. The page polls every few seconds
  with plain ``fetch``: no long-lived connections, so it can never exhaust the browser's per-host
  connection limit.
- ``GET /v1/stream`` (API clients): Server-Sent Events with the raw CloudEvent data, resumable from
  ``Last-Event-ID`` or ``?after=``; one connection lives at most ``stream_max_seconds`` (default 60 s).

Cursors are an ``event_log.id`` or ``t:<ISO UTC>`` (see :meth:`Tail.token`). Ids are UUIDv7 minted by
several processes, so commit order can differ from id order; the tail re-reads a short overlap window and
de-duplicates by id instead of trusting ``id > last``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sse_starlette import EventSourceResponse, ServerSentEvent

from papi.core import db
from papi.core.timeutil import iso_utc, now_utc, parse_iso
from papi.web import queries
from papi.web.deps import parse_feed_filters, templates

router = APIRouter()

OVERLAP = timedelta(seconds=10)
BATCH = 1000
POLL_CAP = 100
PING_SECONDS = 15
CURSOR_SECONDS = 15.0
CURSOR_EVENT = "papi.cursor"
ITEM_TYPES = ("event.classified.v1", "analysis.completed.v1", "forecast.created.v1")


def short_type(t: str) -> str:
    return t.rsplit(".v", 1)[0]


class Tail:
    """Cursor over ``event_log`` with an overlap window and id de-duplication.

    Resume tokens (``Last-Event-ID`` / ``?after=``) are either an ``event_log.id`` or ``t:<ISO UTC>``,
    a time cursor issued when no streamed row was in the window (see :meth:`token`).
    """

    def __init__(self, last_event_id: str | None) -> None:
        self.last_event_id = last_event_id
        self.cursor: datetime | None = None
        self.seen: dict[str, datetime] = {}
        self.polled_at: datetime | None = None

    def start(self, conn: Any) -> None:
        self.polled_at = now_utc()
        token = self.last_event_id or ""
        if token.startswith("t:"):
            try:
                at = parse_iso(token[2:])
            except ValueError:
                at = None
            if at is not None:
                # No streamed row existed in (at - OVERLAP, at] when the token was issued: anything there
                # now committed later and has not been sent.
                self.cursor = at - OVERLAP
                self.seen = {}
                return
        anchor = queries.event_log_time(conn, token) if token and not token.startswith("t:") else None
        if anchor is None:
            anchor = self.polled_at
            self.cursor = anchor - OVERLAP
            self.seen = {r.id: r.time for r in queries.stream_rows(conn, self.cursor, limit=BATCH)}
            return
        self.cursor = anchor - OVERLAP
        self.seen = {
            r.id: r.time
            for r in queries.stream_rows(conn, self.cursor, limit=BATCH)
            if (r.time, r.id) <= (anchor, token)
        }

    def poll(self, conn: Any, limit: int | None = None) -> tuple[list[Any], bool]:
        """New rows since the last poll (time, id order). With ``limit`` only the first ``limit`` are
        handed out (and marked seen); the flag says whether more were waiting."""
        assert self.cursor is not None
        self.polled_at = now_utc()
        rows = queries.stream_rows(conn, self.cursor, limit=BATCH)
        fresh = [r for r in rows if r.id not in self.seen]
        more = limit is not None and len(fresh) > limit
        if more:
            fresh = fresh[:limit]
        for r in fresh:
            self.seen[r.id] = r.time
        if rows and not more:
            latest = rows[-1].time
            self.cursor = latest if len(rows) == BATCH else max(self.cursor, latest - OVERLAP)
            self.seen = {k: t for k, t in self.seen.items() if t >= self.cursor}
        return fresh, more

    def token(self) -> str:
        """Resume point covering everything this tail has handled (sent or filtered out)."""
        if self.seen:
            return max(self.seen.items(), key=lambda kv: (kv[1], kv[0]))[0]
        assert self.polled_at is not None
        return "t:" + iso_utc(self.polled_at)


def current_cursor(conn: Any) -> str:
    """Cursor for "now" (rendered into the feed page so its first poll misses nothing)."""
    tail = Tail(None)
    tail.start(conn)
    return tail.token()


def feed_poll(
    conn: Any, after: str | None, filters: queries.FeedFilters, *, cap: int = POLL_CAP
) -> dict[str, Any]:
    """Rendered feed items for rows handled since ``after`` (at most ``cap`` rows; ``more`` if capped).

    Rows outside the filters (or without a feed item) are consumed silently, so the cursor still advances.
    """
    tail = Tail(after)
    tail.start(conn)
    if not after:
        return {"cursor": tail.token(), "items": [], "more": False}
    rows, more = tail.poll(conn, limit=cap)
    items: list[dict[str, Any]] = []
    wanted = [r for r in rows if r.type in ITEM_TYPES and (r.data or {}).get("event_id")]
    if wanted:
        scope = queries.load_scope(conn)
        tpl = templates.get_template("_feed_item.html")
        for r in wanted:
            found = queries.feed_items(conn, filters, scope, event_ids=[r.data["event_id"]])
            if not found:
                continue
            it = found[0]
            items.append(
                {
                    "id": r.id,
                    "type": short_type(r.type),
                    "event_id": it["id"],
                    "html": tpl.render(it=it),
                    "published": it["published_key"],
                    "ref": it["kap_index"],
                    "mat": it["materiality"] if it["materiality"] is not None else -1,
                    "code": it["code"],
                }
            )
    return {"cursor": tail.token(), "items": items, "more": more}


@router.get("/stream/poll")
def stream_poll(request: Request) -> JSONResponse:
    """Dashboard short poll: ``?after=<cursor>`` plus the feed's filter/sort parameters."""
    params = request.query_params
    filters = parse_feed_filters(params)
    with db.connect() as conn:
        body = feed_poll(conn, params.get("after") or None, filters, cap=POLL_CAP)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _payloads(rows: list[Any]) -> list[ServerSentEvent]:
    """SSE messages (raw CloudEvent data) for new rows."""
    out: list[ServerSentEvent] = []
    for r in rows:
        data = r.data or {}
        body: dict[str, Any] = {
            "type": r.type,
            "subject": r.subject,
            "time": r.time.isoformat(),
            "event_id": data.get("event_id"),
            "data": data,
        }
        out.append(
            ServerSentEvent(
                data=json.dumps(body, ensure_ascii=False, default=str), event=short_type(r.type), id=r.id
            )
        )
    return out


async def sse_events(
    request: Request,
    *,
    last_event_id: str | None,
    poll_seconds: float,
    max_seconds: float,
) -> AsyncIterator[ServerSentEvent]:
    tail = Tail(last_event_id)

    def _start() -> None:
        with db.connect() as conn:
            tail.start(conn)

    def _poll() -> list[ServerSentEvent]:
        with db.connect() as conn:
            return _payloads(tail.poll(conn)[0])

    def cursor() -> ServerSentEvent:
        # Dispatched (not a comment) so the client's Last-Event-ID advances even when nothing arrived.
        return ServerSentEvent(data="{}", event=CURSOR_EVENT, id=tail.token())

    await asyncio.to_thread(_start)
    yield ServerSentEvent(comment="papi stream", retry=1000)
    yield cursor()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    next_cursor = loop.time() + CURSOR_SECONDS
    while loop.time() < deadline:
        if await request.is_disconnected():
            return
        for msg in await asyncio.to_thread(_poll):
            yield msg
        if loop.time() >= next_cursor:
            next_cursor = loop.time() + CURSOR_SECONDS
            yield cursor()
        await asyncio.sleep(max(0.0, min(poll_seconds, deadline - loop.time())))
    yield cursor()  # lifetime over: the client reconnects from here


@router.get("/v1/stream")
async def api_stream(request: Request, last_event_id: str | None = Header(None)) -> EventSourceResponse:
    """SSE: event.classified, analysis.completed, forecast.created, disclosure.revised (08 §1).

    Resume point: the ``Last-Event-ID`` header wins over ``?after=``. A comment ping every 15 s detects
    half-open connections."""
    params = request.query_params
    gen = sse_events(
        request,
        last_event_id=last_event_id or params.get("after") or params.get("last_event_id"),
        poll_seconds=request.app.state.stream_poll_seconds,
        max_seconds=request.app.state.stream_max_seconds,
    )
    return EventSourceResponse(
        gen, ping=PING_SECONDS, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )
