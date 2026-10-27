"""SSE tail of ``event_log``: new events, Last-Event-ID resume, page filters."""

from __future__ import annotations

import json
import threading
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import events as core_events
from papi.web.stream import Tail
from tests.web.sample import publish_classified


def _messages(text: str) -> list[dict[str, Any]]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            fields[k] = fields.get(k, "") + v.lstrip(" ")
        if "data" in fields:
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
