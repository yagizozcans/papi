"""SQLite-backed job queue.

Status flow: ``queued`` → (lease) ``running`` → ``done`` | back to ``queued`` (retry, ``not_before`` set)
| ``dead`` (max attempts or permanent error) | ``cancelled``. A ``running`` job whose ``lease_until`` has
passed is leasable again (crash recovery). Ordering: ``priority`` ascending (lower = more urgent), then
``created_at``, then ``id``. Jobs with ``not_before`` in the future are skipped.

Handlers can steer the outcome by raising ``RetryLater(seconds)``, ``PermanentError`` or
``Deferred(until)`` (defer without consuming an attempt — used by the usage governor).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from papi.core import db
from papi.core.ids import uuid7
from papi.core.registry import DEFAULT_MAX_ATTEMPTS, REGISTRY, job_kind
from papi.core.schema import job
from papi.core.timeutil import now_utc

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "PRIORITY_BACKFILL",
    "PRIORITY_CHAT",
    "PRIORITY_P1",
    "PRIORITY_P2",
    "PRIORITY_P3",
    "PRIORITY_P4",
    "Deferred",
    "Job",
    "PermanentError",
    "RetryLater",
    "backoff_seconds",
    "cancel",
    "complete",
    "counts",
    "defer",
    "enqueue",
    "extend_lease",
    "fail",
    "get_job",
    "job_kind",
    "lease",
    "priority_for",
]

PRIORITY_CHAT = 5
PRIORITY_P1 = 10
PRIORITY_P2 = 20
PRIORITY_P3 = 30
PRIORITY_P4 = 40
PRIORITY_BACKFILL = 80

_PRIORITY_BY_LABEL = {
    "chat": PRIORITY_CHAT,
    "P1": PRIORITY_P1,
    "P2": PRIORITY_P2,
    "P3": PRIORITY_P3,
    "P4": PRIORITY_P4,
    "backfill": PRIORITY_BACKFILL,
}

BACKOFF_BASE_S = 10.0
BACKOFF_MAX_S = 3600.0
DEFAULT_LEASE_S = 300.0


def priority_for(label: str | None, default: int = 50) -> int:
    """Map ``P1``..``P4`` / ``chat`` / ``backfill`` to the numeric job priority."""
    return _PRIORITY_BY_LABEL.get(label or "", default)


class RetryLater(Exception):
    """Raise from a handler to retry after ``retry_in`` seconds (consumes an attempt)."""

    def __init__(self, retry_in: float, message: str = "") -> None:
        super().__init__(message or f"retry in {retry_in}s")
        self.retry_in = retry_in


class PermanentError(Exception):
    """Raise from a handler to mark the job ``dead`` immediately."""


class Deferred(Exception):
    """Raise from a handler to re-queue at ``until`` without consuming an attempt."""

    def __init__(self, until: datetime, reason: str = "") -> None:
        super().__init__(reason or f"deferred until {until.isoformat()}")
        self.until = until
        self.reason = reason


class Job(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any] | None
    priority: int
    not_before: datetime | None
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_until: datetime | None
    status: str
    last_error: str | None
    dedupe_key: str | None
    event_id: str | None
    created_at: datetime
    updated_at: datetime | None
    finished_at: datetime | None


def _row_to_job(row: Any) -> Job:
    return Job.model_validate(dict(row._mapping))


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    not_before: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
    event_id: str | None = None,
) -> str:
    """Insert a job in the caller's transaction; returns its id.

    If ``dedupe_key`` already exists (any status) nothing is inserted and the existing job id is
    returned. ``max_attempts`` defaults to the registered job kind's setting, else 5.
    """
    if max_attempts is None:
        spec = REGISTRY.job_kinds.get(kind)
        max_attempts = spec.max_attempts if spec else DEFAULT_MAX_ATTEMPTS
    jid = uuid7()
    now = now_utc()
    stmt = insert(job).values(
        id=jid,
        kind=kind,
        payload=payload,
        priority=priority,
        not_before=not_before,
        attempts=0,
        max_attempts=max_attempts,
        status="queued",
        dedupe_key=dedupe_key,
        event_id=event_id,
        created_at=now,
        updated_at=now,
    )
    if dedupe_key is None:
        conn.execute(stmt)
        return jid
    result = conn.execute(stmt.on_conflict_do_nothing(index_elements=[job.c.dedupe_key]))
    if result.rowcount == 1:
        return jid
    return conn.execute(select(job.c.id).where(job.c.dedupe_key == dedupe_key)).scalar_one()


def _available(now: datetime, kinds: list[str] | None) -> Any:
    cond = and_(
        or_(
            job.c.status == "queued",
            and_(job.c.status == "running", job.c.lease_until < now),
        ),
        or_(job.c.not_before.is_(None), job.c.not_before <= now),
    )
    if kinds is not None:
        cond = and_(cond, job.c.kind.in_(kinds))
    return cond


def lease(
    worker_id: str, kinds: list[str] | None = None, lease_seconds: float = DEFAULT_LEASE_S
) -> Job | None:
    """Atomically claim the most urgent available job (own ``BEGIN IMMEDIATE`` transaction).

    Increments ``attempts``. A job whose lease expired after already using all attempts is marked
    ``dead`` instead of being handed out. ``kinds=[]`` leases nothing; ``None`` means any kind.
    """
    if kinds is not None and not kinds:
        return None
    while True:
        with db.transaction() as conn:
            now = now_utc()
            row = conn.execute(
                select(job)
                .where(_available(now, kinds))
                .order_by(job.c.priority, job.c.created_at, job.c.id)
                .limit(1)
            ).first()
            if row is None:
                return None
            if row.attempts >= row.max_attempts:
                conn.execute(
                    update(job)
                    .where(job.c.id == row.id)
                    .values(
                        status="dead",
                        lease_owner=None,
                        lease_until=None,
                        updated_at=now,
                        finished_at=now,
                        last_error=(row.last_error or "") + "\nlease expired after final attempt",
                    )
                )
                continue
            conn.execute(
                update(job)
                .where(job.c.id == row.id)
                .values(
                    status="running",
                    attempts=row.attempts + 1,
                    lease_owner=worker_id,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            )
            leased = conn.execute(select(job).where(job.c.id == row.id)).one()
            return _row_to_job(leased)


def _owned(job_id: str, worker_id: str | None) -> Any:
    cond = job.c.id == job_id
    if worker_id is not None:
        cond = and_(cond, job.c.lease_owner == worker_id)
    return cond


def _run(conn: Connection | None, fn: Any) -> Any:
    if conn is not None:
        return fn(conn)
    with db.transaction() as tx:
        return fn(tx)


def complete(job_id: str, *, worker_id: str | None = None, conn: Connection | None = None) -> bool:
    """Mark ``done``. With ``worker_id`` only if still leased by that worker. Returns success.

    Pass ``conn`` to complete inside the handler's own transaction (exactly-once state change).
    """

    def _do(c: Connection) -> bool:
        now = now_utc()
        res = c.execute(
            update(job)
            .where(_owned(job_id, worker_id))
            .values(status="done", lease_owner=None, lease_until=None, finished_at=now, updated_at=now)
        )
        return res.rowcount == 1

    return _run(conn, _do)


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff: 10 s, 20 s, 40 s, … capped at 1 h (``attempts`` = attempts used so far)."""
    return min(BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), BACKOFF_MAX_S)


def fail(
    job_id: str,
    error: str,
    *,
    retry_in: float | None = None,
    permanent: bool = False,
    worker_id: str | None = None,
    conn: Connection | None = None,
) -> str:
    """Record a failure. Re-queues with backoff (``retry_in`` overrides) unless attempts are exhausted
    or ``permanent`` → ``dead``. Returns the new status (``queued`` | ``dead``), or ``""`` if not owned.
    """

    def _do(c: Connection) -> str:
        row = c.execute(select(job).where(_owned(job_id, worker_id))).first()
        if row is None:
            return ""
        now = now_utc()
        if permanent or row.attempts >= row.max_attempts:
            c.execute(
                update(job)
                .where(job.c.id == job_id)
                .values(
                    status="dead",
                    last_error=error,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                )
            )
            return "dead"
        delay = retry_in if retry_in is not None else backoff_seconds(row.attempts)
        c.execute(
            update(job)
            .where(job.c.id == job_id)
            .values(
                status="queued",
                last_error=error,
                lease_owner=None,
                lease_until=None,
                not_before=now + timedelta(seconds=delay),
                updated_at=now,
            )
        )
        return "queued"

    return _run(conn, _do)


def defer(
    job_id: str,
    until: datetime,
    *,
    reason: str | None = None,
    worker_id: str | None = None,
    conn: Connection | None = None,
) -> bool:
    """Re-queue at ``until`` and give back the attempt consumed by the lease."""

    def _do(c: Connection) -> bool:
        res = c.execute(
            update(job)
            .where(_owned(job_id, worker_id))
            .values(
                status="queued",
                attempts=func.max(job.c.attempts - 1, 0),
                not_before=until,
                lease_owner=None,
                lease_until=None,
                last_error=reason,
                updated_at=now_utc(),
            )
        )
        return res.rowcount == 1

    return _run(conn, _do)


def extend_lease(job_id: str, worker_id: str, seconds: float) -> bool:
    """Heartbeat: push ``lease_until`` to now + ``seconds`` if still owned."""
    with db.transaction() as c:
        res = c.execute(
            update(job)
            .where(and_(_owned(job_id, worker_id), job.c.status == "running"))
            .values(lease_until=now_utc() + timedelta(seconds=seconds), updated_at=now_utc())
        )
        return res.rowcount == 1


def cancel(conn: Connection, job_id: str) -> bool:
    """Cancel a queued job (running jobs are left alone)."""
    now = now_utc()
    res = conn.execute(
        update(job)
        .where(and_(job.c.id == job_id, job.c.status == "queued"))
        .values(status="cancelled", finished_at=now, updated_at=now)
    )
    return res.rowcount == 1


def get_job(conn: Connection, job_id: str) -> Job | None:
    row = conn.execute(select(job).where(job.c.id == job_id)).first()
    return _row_to_job(row) if row else None


def counts(conn: Connection) -> dict[tuple[str, int], int]:
    """Queue depth: ``{(status, priority): n}`` for ops views."""
    rows = conn.execute(
        select(job.c.status, job.c.priority, func.count()).group_by(job.c.status, job.c.priority)
    ).all()
    return {(r[0], r[1]): r[2] for r in rows}
