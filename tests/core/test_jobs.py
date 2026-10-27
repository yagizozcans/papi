from __future__ import annotations

from datetime import timedelta

import time_machine

from papi.core import jobs
from papi.core.timeutil import now_utc


def _enqueue(db, kind="k", **kw):
    jid = jobs.enqueue(db, kind, {"n": kw.pop("n", 0)}, **kw)
    db.commit()
    return jid


def test_priority_then_fifo(db):
    a = _enqueue(db, priority=jobs.PRIORITY_P3)
    b = _enqueue(db, priority=jobs.PRIORITY_P1)
    c = _enqueue(db, priority=jobs.PRIORITY_P1)
    d = _enqueue(db, priority=jobs.PRIORITY_CHAT)
    order = [jobs.lease("w").id for _ in range(4)]
    assert order == [d, b, c, a]
    assert jobs.lease("w") is None


def test_kind_filter(db):
    a = _enqueue(db, kind="a")
    b = _enqueue(db, kind="b", priority=1)
    assert jobs.lease("w", kinds=["a"]).id == a
    assert jobs.lease("w", kinds=[]) is None
    assert jobs.lease("w", kinds=["b"]).id == b


def test_not_before(db):
    later = _enqueue(db, not_before=now_utc() + timedelta(minutes=5), priority=1)
    now = _enqueue(db, priority=50)
    assert jobs.lease("w").id == now
    assert jobs.lease("w") is None
    with time_machine.travel(now_utc() + timedelta(minutes=6), tick=False):
        assert jobs.lease("w").id == later


def test_lease_fields_and_complete(db):
    jid = _enqueue(db)
    leased = jobs.lease("worker-1", lease_seconds=60)
    assert leased.id == jid and leased.status == "running" and leased.attempts == 1
    assert leased.lease_owner == "worker-1" and leased.lease_until > now_utc()
    assert not jobs.complete(jid, worker_id="someone-else")
    assert jobs.complete(jid, worker_id="worker-1")
    done = jobs.get_job(db, jid)
    assert done.status == "done" and done.finished_at is not None and done.lease_owner is None


def test_retry_with_backoff_then_dead(db):
    jid = _enqueue(db, max_attempts=3)
    t0 = now_utc()
    with time_machine.travel(t0, tick=False) as clock:
        assert jobs.lease("w").attempts == 1
        assert jobs.fail(jid, "boom") == "queued"
        job = jobs.get_job(db, jid)
        db.rollback()
        assert job.not_before == t0 + timedelta(seconds=10)
        assert jobs.lease("w") is None  # backing off

        clock.shift(11)
        assert jobs.lease("w").attempts == 2
        assert jobs.fail(jid, "boom2") == "queued"
        assert jobs.get_job(db, jid).not_before == now_utc() + timedelta(seconds=20)
        db.rollback()

        clock.shift(21)
        assert jobs.lease("w").attempts == 3
        assert jobs.fail(jid, "boom3") == "dead"
        job = jobs.get_job(db, jid)
        assert job.status == "dead" and job.last_error == "boom3"
        db.rollback()
        clock.shift(3600)
        assert jobs.lease("w") is None


def test_backoff_formula():
    assert [jobs.backoff_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert jobs.backoff_seconds(30) == jobs.BACKOFF_MAX_S


def test_explicit_retry_in_and_permanent(db):
    a = _enqueue(db)
    jobs.lease("w")
    jobs.fail(a, "later", retry_in=0)
    assert jobs.lease("w").id == a
    assert jobs.fail(a, "nope", permanent=True) == "dead"


def test_expired_lease_is_reclaimed_and_dead_after_max(db):
    jid = _enqueue(db, max_attempts=2)
    t0 = now_utc()
    with time_machine.travel(t0, tick=False) as clock:
        assert jobs.lease("crashed", lease_seconds=30).attempts == 1
        assert jobs.lease("other") is None  # still leased
        clock.shift(31)
        again = jobs.lease("other", lease_seconds=30)
        assert again.id == jid and again.attempts == 2 and again.lease_owner == "other"
        clock.shift(31)
        assert jobs.lease("third") is None  # attempts exhausted → dead, not handed out
        assert jobs.get_job(db, jid).status == "dead"


def test_defer_does_not_consume_attempt(db):
    jid = _enqueue(db)
    jobs.lease("w")
    until = now_utc() + timedelta(hours=1)
    assert jobs.defer(jid, until, reason="governor")
    job = jobs.get_job(db, jid)
    assert job.attempts == 0 and job.status == "queued" and job.not_before == until


def test_dedupe_key(db):
    a = jobs.enqueue(db, "k", {}, dedupe_key="x")
    b = jobs.enqueue(db, "k", {"other": 1}, dedupe_key="x")
    db.commit()
    assert a == b
    assert sum(jobs.counts(db).values()) == 1


def test_cancel(db):
    jid = _enqueue(db)
    assert jobs.cancel(db, jid)
    db.commit()
    assert jobs.lease("w") is None


def test_priority_labels():
    assert jobs.priority_for("P1") == 10 and jobs.priority_for("chat") == 5
    assert jobs.priority_for("backfill") == 80 and jobs.priority_for(None) == 50
