"""Server-Sent Events: tails ``event_log`` (≈1 s poll) for feed-relevant event types.

``/stream`` (dashboard) applies the page's feed filters and ships rendered feed-item HTML;
``/v1/stream`` ships the raw CloudEvent data. Both resume from ``Last-Event-ID`` (the ``event_log.id``) or,
for a fresh EventSource opened by the page, from ``?after=<event_log.id>``. One connection lives at most
``stream_max_seconds`` (default 60 s); the browser then reconnects with ``Last-Event-ID``.

Ids are UUIDv7 minted by several processes, so commit order can differ from id order. The tail therefore
re-reads a short overlap window and de-duplicates by id instead of trusting ``id > last``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, Request
from sse_starlette import EventSourceResponse, ServerSentEvent

from papi.core import db
from papi.core.timeutil import iso_utc, now_utc, parse_iso
from papi.web import queries
from papi.web.deps import parse_feed_filters, templates

router = APIRouter()

OVERLAP = timedelta(seconds=10)
BATCH = 1000
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

    def poll(self, conn: Any) -> list[Any]:
        assert self.cursor is not None
        self.polled_at = now_utc()
        rows = queries.stream_rows(conn, self.cursor, limit=BATCH)
        fresh = [r for r in rows if r.id not in self.seen]
        for r in fresh:
            self.seen[r.id] = r.time
        if rows:
            latest = rows[-1].time
            self.cursor = latest if len(rows) == BATCH else max(self.cursor, latest - OVERLAP)
            self.seen = {k: t for k, t in self.seen.items() if t >= self.cursor}
        return fresh

    def token(self) -> str:
        """Resume point covering everything this tail has handled (sent or filtered out)."""
        if self.seen:
            return max(self.seen.items(), key=lambda kv: (kv[1], kv[0]))[0]
        assert self.polled_at is not None
        return "t:" + iso_utc(self.polled_at)


def _payloads(conn: Any, rows: list[Any], filters: queries.FeedFilters | None) -> list[ServerSentEvent]:
    """SSE messages for new rows; with ``filters`` (dashboard mode) attach rendered feed items."""
    out: list[ServerSentEvent] = []
    scope = queries.load_scope(conn) if filters is not None and rows else None
    tpl = templates.get_template("_feed_item.html") if filters is not None else None
    for r in rows:
        data = r.data or {}
        body: dict[str, Any] = {
            "type": r.type,
            "subject": r.subject,
            "time": r.time.isoformat(),
            "event_id": data.get("event_id"),
            "data": data,
        }
        dashboard = filters is not None and tpl is not None and scope is not None
        if dashboard and r.type in ITEM_TYPES and data.get("event_id"):
            items = queries.feed_items(conn, filters, scope, event_ids=[data["event_id"]])
            if items:
                body["html"] = tpl.render(it=items[0])
                body["event_id"] = items[0]["id"]
            elif r.type == "event.classified.v1":
                continue  # outside the page's filters
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
    filters: queries.FeedFilters | None,
    poll_seconds: float,
    max_seconds: float,
) -> AsyncIterator[ServerSentEvent]:
    tail = Tail(last_event_id)

    def _start() -> None:
        with db.connect() as conn:
            tail.start(conn)

    def _poll() -> list[ServerSentEvent]:
        with db.connect() as conn:
            return _payloads(conn, tail.poll(conn), filters)

    def cursor() -> ServerSentEvent:
        # Dispatched (not a comment) so both the browser's Last-Event-ID and the page's `?after=`
        # resume point advance even when every row was filtered out or none arrived.
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


def _response(request: Request, last_event_id: str | None, *, dashboard: bool) -> EventSourceResponse:
    """Resume point: the ``Last-Event-ID`` header (native EventSource reconnect) wins over ``?after=``
    (a fresh EventSource opened by the page after pagehide/visibility changes cannot set headers)."""
    params = request.query_params
    filters = parse_feed_filters(params) if dashboard else None
    gen = sse_events(
        request,
        last_event_id=last_event_id or params.get("after") or params.get("last_event_id"),
        filters=filters,
        poll_seconds=request.app.state.stream_poll_seconds,
        max_seconds=request.app.state.stream_max_seconds,
    )
    # Comment ping every 15 s so half-open connections are detected and closed.
    return EventSourceResponse(
        gen, ping=PING_SECONDS, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


@router.get("/stream")
async def dashboard_stream(request: Request, last_event_id: str | None = Header(None)) -> EventSourceResponse:
    """Feed live updates (rendered items, filtered like the page that opened the stream)."""
    return _response(request, last_event_id, dashboard=True)


@router.get("/v1/stream")
async def api_stream(request: Request, last_event_id: str | None = Header(None)) -> EventSourceResponse:
    """SSE: event.classified, analysis.completed, forecast.created, disclosure.revised (08 §1)."""
    return _response(request, last_event_id, dashboard=False)
