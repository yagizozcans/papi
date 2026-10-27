from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import time_machine
from pydantic import ValidationError
from sqlalchemy import func, select

from papi.core import db as core_db
from papi.core import events, jobs, runtime
from papi.core.events import DISCLOSURE_NORMALIZED, CloudEvent, DisclosureNormalized, handles, publish
from papi.core.registry import job_kind, periodic
from papi.core.schema import event_log, job
from papi.core.timeutil import now_utc


def _count(conn, table):
    return conn.execute(select(func.count()).select_from(table)).scalar()


def test_publish_fans_out_one_job_per_handler(db, clean_registry):
    @handles(DISCLOSURE_NORMALIZED, name="a")
    def a(event, ctx):
        pass

    @handles(DISCLOSURE_NORMALIZED, name="b", priority=jobs.PRIORITY_P1)
    def b(event, ctx):
        pass

    @handles("other.thing.v1", name="c")
    def c(event, ctx):
        pass

    with core_db.transaction() as tx:
        eid = publish(
            tx,
            DISCLOSURE_NORMALIZED,
            "disclosure/1",
            {"disclosure_id": "d1"},
            "ingest/kap",
            priority=jobs.PRIORITY_P2,
        )
    rows = db.execute(select(job).order_by(job.c.kind)).all()
    assert [r.kind for r in rows] == ["event:a", "event:b"]
    assert {r.kind: r.priority for r in rows} == {"event:a": jobs.PRIORITY_P2, "event:b": jobs.PRIORITY_P1}
    assert all(r.event_id == eid and r.payload["event_id"] == eid for r in rows)
    assert rows[0].dedupe_key == f"a:{eid}"
    ev = events.get_event(db, eid)
    assert ev.type == DISCLOSURE_NORMALIZED and ev.subject == "disclosure/1" and ev.specversion == "1.0"
    assert ev.data_as(DisclosureNormalized).disclosure_id == "d1"


def test_publish_rollback_leaves_nothing(db, clean_registry):
    @handles(DISCLOSURE_NORMALIZED, name="a")
    def a(event, ctx):
        pass

    with pytest.raises(RuntimeError), core_db.transaction() as tx:
        publish(tx, DISCLOSURE_NORMALIZED, "disclosure/1", {"disclosure_id": "d1"}, "ingest/kap")
        raise RuntimeError("state change failed")
    assert _count(db, event_log) == 0
    assert _count(db, job) == 0


def test_publish_validates_known_types(db, clean_registry):
    with pytest.raises(ValidationError):
        publish(db, DISCLOSURE_NORMALIZED, None, {"wrong": 1}, "x")
    eid = publish(db, "custom.unknown.v1", None, {"anything": [1, 2]}, "x")
    assert events.get_event(db, eid).data == {"anything": [1, 2]}


def test_callable_priority(db, clean_registry):
    @handles(
        events.EVENT_CLASSIFIED, name="alerts", priority=lambda ev: jobs.priority_for(ev.data["priority"])
    )
    def alerts(event, ctx):
        pass

    publish(db, events.EVENT_CLASSIFIED, None, {"event_id": "e", "code": "x", "priority": "P1"}, "triage")
    assert db.execute(select(job.c.priority)).scalar_one() == jobs.PRIORITY_P1


def test_event_types_cover_prd():
    expected = {
        "source.object.fetched.v1",
        "disclosure.seen.v1",
        "disclosure.normalized.v1",
        "disclosure.revised.v1",
        "disclosure.blocked.v1",
        "ingest.gap.detected.v1",
        "event.classified.v1",
        "analysis.completed.v1",
        "forecast.created.v1",
        "outcome.recorded.v1",
        "alert.triggered.v1",
        "kb.version.published.v1",
        "chat.message.received.v1",
        "portfolio.changed.v1",
    }
    assert set(events.ALL_EVENT_TYPES) == expected


async def test_worker_runs_sync_and_async_handlers_and_survives_errors(db, clean_registry):
    seen: list[str] = []

    @handles(DISCLOSURE_NORMALIZED, name="sync_ok")
    def sync_ok(event: CloudEvent, ctx: runtime.JobContext):
        seen.append(f"sync:{event.data['disclosure_id']}:{ctx.attempt}")

    @handles(DISCLOSURE_NORMALIZED, name="async_ok")
    async def async_ok(event, ctx):
        seen.append(f"async:{event.data['disclosure_id']}")

    @handles(DISCLOSURE_NORMALIZED, name="broken", max_attempts=1)
    def broken(event, ctx):
        raise ValueError("handler bug")

    @job_kind("custom.kind")
    def custom(payload, ctx):
        seen.append(f"custom:{payload['x']}")

    with core_db.transaction() as tx:
        publish(tx, DISCLOSURE_NORMALIZED, None, {"disclosure_id": "d1"}, "ingest/kap")
        jobs.enqueue(tx, "custom.kind", {"x": 7})

    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run_worker(stop, concurrency=2, poll_interval=0.05))
    for _ in range(200):
        await asyncio.sleep(0.02)
        statuses = dict(db.execute(select(job.c.kind, job.c.status)).all())
        db.rollback()
        if all(s in ("done", "dead") for s in statuses.values()):
            break
    stop.set()
    await asyncio.wait_for(task, 5)

    assert sorted(seen) == ["async:d1", "custom:7", "sync:d1:1"]
    assert statuses == {
        "event:sync_ok": "done",
        "event:async_ok": "done",
        "event:broken": "dead",
        "custom.kind": "done",
    }
    err = db.execute(select(job.c.last_error).where(job.c.kind == "event:broken")).scalar_one()
    assert "handler bug" in err


async def test_control_flow_exceptions(db, clean_registry):
    until = now_utc() + timedelta(hours=1)

    @job_kind("defer.me")
    def defer_me(payload, ctx):
        raise jobs.Deferred(until, "limit")

    @job_kind("retry.me")
    def retry_me(payload, ctx):
        raise jobs.RetryLater(120)

    @job_kind("perm.me")
    def perm_me(payload, ctx):
        raise jobs.PermanentError("bad input")

    ids = {}
    for kind in ("defer.me", "retry.me", "perm.me"):
        ids[kind] = jobs.enqueue(db, kind, {})
    db.commit()
    for _ in range(3):
        leased = jobs.lease("w")
        await runtime.execute_job(leased, "w")
    d, r, p = (jobs.get_job(db, ids[k]) for k in ("defer.me", "retry.me", "perm.me"))
    assert d.status == "queued" and d.attempts == 0 and d.not_before == until
    assert r.status == "queued" and r.attempts == 1 and r.not_before > now_utc() + timedelta(seconds=100)
    assert p.status == "dead" and "bad input" in p.last_error


def test_scheduler_enqueues_due_periodics_once(db, clean_registry):
    calls = []

    @periodic("poll", 30, priority=jobs.PRIORITY_P1)
    def poll(ctx):
        calls.append(1)

    @periodic("nightly", lambda now: 3600, run_on_start=False)
    def nightly(ctx):
        calls.append(2)

    t0 = now_utc()
    with time_machine.travel(t0, tick=False) as clock:
        assert runtime.schedule_due() == ["poll"]
        assert runtime.schedule_due() == []  # not due yet and one already queued
        clock.shift(31)
        assert runtime.schedule_due() == []  # due, but previous job still queued
        leased = jobs.lease("w")
        assert leased.kind == "periodic:poll" and leased.priority == jobs.PRIORITY_P1
        asyncio.run(runtime.execute_job(leased, "w"))
        assert runtime.schedule_due() == ["poll"]
        clock.shift(3601)
        assert runtime.schedule_due() == ["nightly"]  # poll is due but its last job is still queued
    assert calls == [1]
