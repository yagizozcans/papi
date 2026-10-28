"""SSE tail of ``event_log``: new events, Last-Event-ID resume, page filters."""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import events as core_events
from papi.web.stream import CURSOR_EVENT, Tail
from tests.web.sample import publish_classified


def _messages(text: str, *, cursors: bool = False) -> list[dict[str, Any]]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            fields[k] = fields.get(k, "") + v.lstrip(" ")
        if "data" in fields and (cursors or fields.get("event") != CURSOR_EVENT):
            out.append(
                {"event": fields.get("event"), "id": fields.get("id"), "data": json.loads(fields["data"])}
            )
    return out


def test_tail_skips_history_and_dedupes(clean_registry: Any, world: dict[str, Any], db: Connection) -> None:
    old = publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")
    db.commit()
    tail = Tail(None)
    tail.start(db)
    assert tail.poll(db) == []  # existing rows are not replayed
    new = publish_classified(db, world["e_thy"], "business.contract.award", "P3")
    db.commit()
    assert [r.id for r in tail.poll(db)] == [new]
    assert tail.poll(db) == []
    resumed = Tail(old)
    resumed.start(db)
    assert [r.id for r in resumed.poll(db)] == [new]


def test_stream_resumes_from_last_event_id(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    first = publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")
    second = publish_classified(db, world["e_thy"], "business.contract.award", "P3")
    revised = core_events.publish(
        db,
        core_events.DISCLOSURE_REVISED,
        f"disclosure/{world['d_fix']}",
        {"disclosure_id": world["d_fix"], "revision_of": world["d_div"], "revision_kind": "correction"},
        "ingest/kap",
    )
    db.commit()
    r = client.get("/v1/stream", headers={"Last-Event-ID": first})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    msgs = _messages(r.text)
    assert [(m["event"], m["id"]) for m in msgs] == [
        ("event.classified", second),
        ("disclosure.revised", revised),
    ]
    assert msgs[0]["data"]["event_id"] == world["e_thy"] and "html" not in msgs[0]["data"]
    assert msgs[1]["data"]["data"]["revision_of"] == world["d_div"]


def test_dashboard_stream_yields_new_events_live(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    def later() -> None:
        with core_db.transaction() as conn:
            publish_classified(conn, world["e_thy"], "business.contract.award", "P3")
            publish_classified(
                conn, world["e_gar"], "reporting.annual_report", "P4"
            )  # filtered out (P4, not mine)

    timer = threading.Timer(0.3, later)
    timer.start()
    try:
        r = client.get("/stream?scope=mine")
    finally:
        timer.join()
    msgs = _messages(r.text)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["event"] == "event.classified" and m["data"]["event_id"] == world["e_thy"]
    assert f'id="ev-{world["e_thy"]}"' in m["data"]["html"] and 'badge watch">WATCH' in m["data"]["html"]


def test_dashboard_stream_applies_filters(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    first = publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")
    publish_classified(db, world["e_thy"], "business.contract.award", "P3")
    analysed = core_events.publish(
        db,
        core_events.ANALYSIS_COMPLETED,
        f"event/{world['e_fix']}",
        {"event_id": world["e_fix"], "analysis_ids": [world["analysis"]]},
        "llm/analyst",
    )
    db.commit()
    msgs = _messages(client.get("/stream?scope=holdings", headers={"Last-Event-ID": first}).text)
    assert [(m["event"], m["id"]) for m in msgs] == [("analysis.completed", analysed)]
    assert f'id="ev-{world["e_fix"]}"' in msgs[0]["data"]["html"]


def _last_id(text: str) -> str:
    return _messages(text, cursors=True)[-1]["id"]


def test_after_param_resumes_like_last_event_id(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    first = publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")
    second = publish_classified(db, world["e_thy"], "business.contract.award", "P3")
    third = publish_classified(db, world["e_gar"], "reporting.annual_report", "P4")
    db.commit()
    msgs = _messages(client.get("/v1/stream", params={"after": first}).text)
    assert [m["id"] for m in msgs] == [second, third]
    # the header (browser auto-reconnect) wins over a stale ?after= in the URL
    msgs = _messages(
        client.get("/v1/stream", params={"after": first}, headers={"Last-Event-ID": second}).text
    )
    assert [m["id"] for m in msgs] == [third]


def test_reconnect_with_after_has_no_gaps_or_duplicates(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    def publish(event_key: str, code: str, prio: str) -> str:
        with core_db.transaction() as conn:
            return publish_classified(conn, world[event_key], code, prio)

    timer = threading.Timer(0.3, publish, ("e_div", "distribution.dividend.cash", "P1"))
    timer.start()
    try:
        r1 = client.get("/stream?scope=holdings")
    finally:
        timer.join()
    seen1 = [m["data"]["event_id"] for m in _messages(r1.text)]
    assert seen1 == [world["e_div"]]
    resume = _last_id(r1.text)  # lifetime ended with a cursor
    # while the page was hidden: one filtered-out and one matching event
    publish("e_thy", "business.contract.award", "P3")
    fix = publish("e_fix", "distribution.dividend.cash", "P1")
    r2 = client.get("/stream", params={"scope": "holdings", "after": resume})
    msgs2 = _messages(r2.text)
    assert [(m["id"], m["data"]["event_id"]) for m in msgs2] == [(fix, world["e_fix"])]
    # and a third connection from the second's cursor gets nothing again
    assert (
        _messages(client.get("/stream", params={"scope": "holdings", "after": _last_id(r2.text)}).text) == []
    )


def test_time_cursor_when_window_empty(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    r1 = client.get("/v1/stream")
    cursors = _messages(r1.text, cursors=True)
    assert cursors and all(m["event"] == CURSOR_EVENT for m in cursors)
    token = cursors[-1]["id"]
    assert token.startswith("t:")
    with core_db.transaction() as conn:
        missed = publish_classified(conn, world["e_thy"], "business.contract.award", "P3")
    msgs = _messages(client.get("/v1/stream", params={"after": token}).text)
    assert [m["id"] for m in msgs] == [missed]


def test_max_lifetime_and_default(world: dict[str, Any]) -> None:
    import time

    from papi.web.app import create_app

    assert create_app().state.stream_max_seconds == 60.0
    with TestClient(create_app(stream_poll_seconds=0.05, stream_max_seconds=0.3)) as c:
        t0 = time.monotonic()
        r = c.get("/stream")
        assert time.monotonic() - t0 < 3
    assert "retry: 1000" in r.text
    assert _messages(r.text, cursors=True)[-1]["event"] == CURSOR_EVENT


async def test_generator_stops_on_client_disconnect(clean_registry: Any, world: dict[str, Any]) -> None:
    import asyncio

    from papi.web.stream import sse_events

    class FakeRequest:
        def __init__(self) -> None:
            self.checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 2

    req = FakeRequest()

    async def drain() -> list[Any]:
        return [
            m
            async for m in sse_events(
                req,
                last_event_id=None,
                filters=None,
                poll_seconds=0.01,  # type: ignore[arg-type]
                max_seconds=60,
            )
        ]

    out = await asyncio.wait_for(drain(), timeout=5)
    assert req.checks == 3  # stopped on the first disconnected check, long before the 60 s lifetime
    assert out[0].comment == "papi stream"
