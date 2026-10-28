"""Live updates: the dashboard's short-poll endpoint and the ``/v1/stream`` SSE for API clients."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import events as core_events
from papi.web import stream as live
from papi.web.app import create_app
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


def _publish(world: dict[str, Any], key: str, code: str, prio: str) -> str:
    with core_db.transaction() as conn:
        return publish_classified(conn, world[key], code, prio)


# ------------------------------------------------------------------------------------------ tail


def test_tail_skips_history_and_dedupes(clean_registry: Any, world: dict[str, Any], db: Connection) -> None:
    old = publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")
    db.commit()
    tail = Tail(None)
    tail.start(db)
    assert tail.poll(db) == ([], False)  # existing rows are not replayed
    new = publish_classified(db, world["e_thy"], "business.contract.award", "P3")
    db.commit()
    assert [r.id for r in tail.poll(db)[0]] == [new]
    assert tail.poll(db) == ([], False)
    resumed = Tail(old)
    resumed.start(db)
    assert [r.id for r in resumed.poll(db)[0]] == [new]


# ------------------------------------------------------------------------------------------ /stream/poll


def _poll(client: TestClient, after: str | None, **params: Any) -> dict[str, Any]:
    if after is not None:
        params["after"] = after
    r = client.get("/stream/poll", params=params)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    return r.json()


def test_poll_without_after_returns_cursor_only(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    _publish(world, "e_div", "distribution.dividend.cash", "P1")
    body = _poll(client, None, scope="all")
    assert body["items"] == [] and body["more"] is False and body["cursor"]
    # the feed page renders the same kind of cursor so its first poll misses nothing
    html = client.get("/?scope=all").text
    assert 'data-poll="/stream/poll?scope=all"' in html and 'data-cursor="' in html
    assert "EventSource" not in client.get("/static/app.js").text


def test_poll_resumes_without_gaps_or_duplicates(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    cursor = _poll(client, None, scope="mine")["cursor"]
    assert _poll(client, cursor, scope="mine")["items"] == []
    a = _publish(world, "e_thy", "business.contract.award", "P3")
    b = _publish(world, "e_gar", "reporting.annual_report", "P4")  # outside "mine": consumed silently
    first = _poll(client, cursor, scope="mine")
    assert [(i["id"], i["event_id"], i["type"]) for i in first["items"]] == [
        (a, world["e_thy"], "event.classified")
    ]
    item = first["items"][0]
    assert f'id="ev-{world["e_thy"]}"' in item["html"] and 'badge watch">WATCH' in item["html"]
    assert item["code"] == "business.contract.award" and item["mat"] == 65 and item["ref"] == 1500002
    assert item["published"].endswith("Z") and f'data-published="{item["published"]}"' in item["html"]
    # repeat with the same cursor: same answer (idempotent), with the new cursor: nothing
    assert [i["id"] for i in _poll(client, cursor, scope="mine")["items"]] == [a]
    assert _poll(client, first["cursor"], scope="mine")["items"] == []
    c = _publish(world, "e_div", "distribution.dividend.cash", "P1")
    second = _poll(client, first["cursor"], scope="mine")
    assert [i["id"] for i in second["items"]] == [c]
    assert b not in {i["id"] for i in first["items"] + second["items"]}


def test_poll_applies_filters(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    cursor = _poll(client, None)["cursor"]
    publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")  # 75
    publish_classified(db, world["e_thy"], "business.contract.award", "P3")  # 65
    analysed = core_events.publish(
        db,
        core_events.ANALYSIS_COMPLETED,
        f"event/{world['e_fix']}",
        {"event_id": world["e_fix"], "analysis_ids": [world["analysis"]]},
        "llm/analyst",
    )
    db.commit()
    holdings = _poll(client, cursor, scope="holdings", sort="materiality")["items"]
    assert [i["event_id"] for i in holdings] == [world["e_div"], world["e_fix"]]
    assert holdings[1]["id"] == analysed and holdings[1]["type"] == "analysis.completed"
    assert {i["event_id"] for i in _poll(client, cursor, scope="all", band="critical")["items"]} == set()
    assert {i["event_id"] for i in _poll(client, cursor, scope="all", ticker="THYAO")["items"]} == {
        world["e_thy"]
    }
    assert _poll(client, cursor, scope="all", **{"from": "2000-01-01", "to": "2000-01-02"})["items"] == []


def test_poll_cap_and_more(
    clean_registry: Any, client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "POLL_CAP", 2)
    cursor = _poll(client, None, scope="all")["cursor"]
    ids = [
        _publish(world, k, "business.contract.award", "P3")
        for k in ("e_thy", "e_gar", "e_div", "e_fix", "e_thy")
    ]
    got: list[str] = []
    page = _poll(client, cursor, scope="all")
    assert page["more"] is True and len(page["items"]) == 2
    while True:
        got += [i["id"] for i in page["items"]]
        if not page["more"]:
            break
        page = _poll(client, page["cursor"], scope="all")
    assert got == ids  # every row exactly once, in order


def test_poll_time_cursor_when_window_empty(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    token = _poll(client, None, scope="all")["cursor"]
    assert token.startswith("t:")
    assert _poll(client, token, scope="all")["cursor"].startswith("t:")
    missed = _publish(world, "e_thy", "business.contract.award", "P3")
    assert [i["id"] for i in _poll(client, token, scope="all")["items"]] == [missed]


def test_poll_while_rows_arrive_concurrently(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    """Rows committed by another process between polls are delivered exactly once."""
    cursor = _poll(client, None, scope="all")["cursor"]
    published: list[str] = []

    def writer() -> None:
        for key in ("e_thy", "e_gar", "e_div", "e_fix") * 3:
            published.append(_publish(world, key, "business.contract.award", "P3"))
            time.sleep(0.01)

    t = threading.Thread(target=writer)
    t.start()
    got: list[str] = []
    while t.is_alive():
        page = _poll(client, cursor, scope="all")
        got += [i["id"] for i in page["items"]]
        cursor = page["cursor"]
    t.join()
    page = _poll(client, cursor, scope="all")
    got += [i["id"] for i in page["items"]]
    assert got == published


# ------------------------------------------------------------------------------------------ /v1/stream


def test_api_stream_resumes_from_last_event_id(
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
    # ?after= works like the header; the header wins over a stale ?after=
    assert [m["id"] for m in _messages(client.get("/v1/stream", params={"after": first}).text)] == [
        second,
        revised,
    ]
    msgs = _messages(
        client.get("/v1/stream", params={"after": first}, headers={"Last-Event-ID": second}).text
    )
    assert [m["id"] for m in msgs] == [revised]


def test_api_stream_yields_new_events_live(
    clean_registry: Any, client: TestClient, world: dict[str, Any]
) -> None:
    timer = threading.Timer(0.3, _publish, (world, "e_thy", "business.contract.award", "P3"))
    timer.start()
    try:
        r = client.get("/v1/stream")
    finally:
        timer.join()
    msgs = _messages(r.text)
    assert [m["data"]["event_id"] for m in msgs] == [world["e_thy"]]
    cursors = _messages(r.text, cursors=True)
    assert cursors[0]["event"] == CURSOR_EVENT and cursors[-1]["event"] == CURSOR_EVENT
    # reconnecting from the final cursor yields nothing again
    assert _messages(client.get("/v1/stream", headers={"Last-Event-ID": cursors[-1]["id"]}).text) == []


def test_dashboard_sse_route_removed(client: TestClient) -> None:
    assert client.get("/stream").status_code in (404, 405)


def test_api_stream_max_lifetime_and_default(world: dict[str, Any]) -> None:
    assert create_app().state.stream_max_seconds == 60.0
    with TestClient(create_app(stream_poll_seconds=0.05, stream_max_seconds=0.3)) as c:
        t0 = time.monotonic()
        r = c.get("/v1/stream")
        assert time.monotonic() - t0 < 3
    assert "retry: 1000" in r.text
    assert _messages(r.text, cursors=True)[-1]["event"] == CURSOR_EVENT


async def test_generator_stops_on_client_disconnect(clean_registry: Any, world: dict[str, Any]) -> None:
    import asyncio

    class FakeRequest:
        def __init__(self) -> None:
            self.checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 2

    req = FakeRequest()

    async def drain() -> list[Any]:
        gen = live.sse_events(req, last_event_id=None, poll_seconds=0.01, max_seconds=60)  # type: ignore[arg-type]
        return [m async for m in gen]

    out = await asyncio.wait_for(drain(), timeout=5)
    assert req.checks == 3  # stopped on the first disconnected check, long before the 60 s lifetime
    assert out[0].comment == "papi stream"
