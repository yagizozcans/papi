"""Server-Sent Events: tails ``event_log`` (≈1 s poll) for feed-relevant event types.

``/stream`` (dashboard) applies the page's feed filters and ships rendered feed-item HTML;
``/v1/stream`` ships the raw CloudEvent data. Both resume from ``Last-Event-ID`` (the ``event_log.id``).

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
from papi.core.timeutil import now_utc
from papi.web import queries
from papi.web.deps import parse_feed_filters, templates

router = APIRouter()

OVERLAP = timedelta(seconds=10)
BATCH = 1000
ITEM_TYPES = ("event.classified.v1", "analysis.completed.v1", "forecast.created.v1")


def short_type(t: str) -> str:
    return t.rsplit(".v", 1)[0]


class Tail:
    """Cursor over ``event_log`` with an overlap window and id de-duplication."""

    def __init__(self, last_event_id: str | None) -> None:
        self.last_event_id = last_event_id
        self.cursor: datetime | None = None
        self.seen: dict[str, datetime] = {}

    def start(self, conn: Any) -> None:
        anchor = queries.event_log_time(conn, self.last_event_id) if self.last_event_id else None
        if anchor is None:
            anchor = now_utc()
            self.cursor = anchor - OVERLAP
            self.seen = {r.id: r.time for r in queries.stream_rows(conn, self.cursor, limit=BATCH)}
            return
        self.cursor = anchor - OVERLAP
        self.seen = {
            r.id: r.time
            for r in queries.stream_rows(conn, self.cursor, limit=BATCH)
            if (r.time, r.id) <= (anchor, self.last_event_id)
        }

    def poll(self, conn: Any) -> list[Any]:
        assert self.cursor is not None
        rows = queries.stream_rows(conn, self.cursor, limit=BATCH)
        fresh = [r for r in rows if r.id not in self.seen]
        for r in fresh:
            self.seen[r.id] = r.time
        if rows:
            latest = rows[-1].time
            self.cursor = latest if len(rows) == BATCH else max(self.cursor, latest - OVERLAP)
            self.seen = {k: t for k, t in self.seen.items() if t >= self.cursor}
        return fresh


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

    await asyncio.to_thread(_start)
    yield ServerSentEvent(comment="papi stream", retry=1000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    while loop.time() < deadline:
        if await request.is_disconnected():
            return
        for msg in await asyncio.to_thread(_poll):
            yield msg
        await asyncio.sleep(poll_seconds)


def _response(request: Request, last_event_id: str | None, *, dashboard: bool) -> EventSourceResponse:
    params = request.query_params
    filters = parse_feed_filters(params) if dashboard else None
    gen = sse_events(
        request,
        last_event_id=last_event_id or params.get("last_event_id"),
        filters=filters,
        poll_seconds=request.app.state.stream_poll_seconds,
        max_seconds=request.app.state.stream_max_seconds,
    )
    return EventSourceResponse(gen, ping=15, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/stream")
async def dashboard_stream(request: Request, last_event_id: str | None = Header(None)) -> EventSourceResponse:
    """Feed live updates (rendered items, filtered like the page that opened the stream)."""
    return _response(request, last_event_id, dashboard=True)


@router.get("/v1/stream")
async def api_stream(request: Request, last_event_id: str | None = Header(None)) -> EventSourceResponse:
    """SSE: event.classified, analysis.completed, forecast.created, disclosure.revised (08 §1)."""
    return _response(request, last_event_id, dashboard=False)
