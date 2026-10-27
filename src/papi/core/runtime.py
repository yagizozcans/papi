"""Async worker and scheduler loops over the job queue.

- ``run_worker(stop_event)``: leases jobs of the registered kinds (or ``kinds``) and executes them;
  sync callables run in a thread, ``async`` ones are awaited. Exceptions are recorded on the job and
  never stop the loop.
- ``run_scheduler(stop_event)``: enqueues ``periodic:<name>`` jobs when due.
- ``execute_job(job, worker_id)``: run one leased job and settle it (used by the worker and tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import socket
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, select

from papi.core import db, jobs
from papi.core.events import get_event
from papi.core.registry import (
    EVENT_JOB_PREFIX,
    PERIODIC_JOB_PREFIX,
    REGISTRY,
    load_plugins,
    periodic,
)
from papi.core.schema import job as job_table
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc, parse_iso

__all__ = ["JobContext", "default_worker_id", "execute_job", "periodic", "run_scheduler", "run_worker"]

log = logging.getLogger("papi.runtime")


@dataclass
class JobContext:
    job: jobs.Job
    worker_id: str
    lease_seconds: float

    @property
    def attempt(self) -> int:
        return self.job.attempts

    def heartbeat(self, seconds: float | None = None) -> bool:
        """Extend the lease (call from long-running handlers)."""
        return jobs.extend_lease(self.job.id, self.worker_id, seconds or self.lease_seconds)


def default_worker_id(suffix: str = "") -> str:
    base = f"{socket.gethostname()}:{os.getpid()}"
    return f"{base}:{suffix}" if suffix else base


class _UnknownKind(Exception):
    pass


def _resolve(job: jobs.Job) -> tuple[Callable[..., Any], tuple[Any, ...]]:
    """Return (callable, positional args before ctx) for a job."""
    if job.kind.startswith(EVENT_JOB_PREFIX):
        spec = REGISTRY.handler_by_kind(job.kind)
        if spec is None:
            raise _UnknownKind(job.kind)
        event_id = (job.payload or {}).get("event_id")
        with db.connect() as conn:
            event = get_event(conn, event_id) if event_id else None
        if event is None:
            raise jobs.PermanentError(f"event {event_id!r} not found")
        return spec.func, (event,)
    if job.kind.startswith(PERIODIC_JOB_PREFIX):
        pspec = REGISTRY.periodics.get(job.kind.removeprefix(PERIODIC_JOB_PREFIX))
        if pspec is None:
            raise _UnknownKind(job.kind)
        return pspec.func, ()
    kspec = REGISTRY.job_kinds.get(job.kind)
    if kspec is None:
        raise _UnknownKind(job.kind)
    return kspec.func, (job.payload or {},)


async def execute_job(job: jobs.Job, worker_id: str, *, lease_seconds: float = jobs.DEFAULT_LEASE_S) -> str:
    """Run one leased job and settle it. Returns ``done``/``queued``/``dead`` (``lost`` if the lease was
    taken over before completion)."""
    ctx = JobContext(job=job, worker_id=worker_id, lease_seconds=lease_seconds)
    try:
        fn, args = _resolve(job)
        if inspect.iscoroutinefunction(fn):
            await fn(*args, ctx)
        else:
            await asyncio.to_thread(fn, *args, ctx)
    except jobs.Deferred as exc:
        await asyncio.to_thread(jobs.defer, job.id, exc.until, reason=str(exc), worker_id=worker_id)
        return "queued"
    except jobs.RetryLater as exc:
        return await asyncio.to_thread(
            jobs.fail, job.id, str(exc), retry_in=exc.retry_in, worker_id=worker_id
        )
    except jobs.PermanentError as exc:
        log.error("job %s (%s) failed permanently: %s", job.id, job.kind, exc)
        return await asyncio.to_thread(
            jobs.fail, job.id, f"permanent: {exc}", permanent=True, worker_id=worker_id
        )
    except _UnknownKind as exc:
        return await asyncio.to_thread(
            jobs.fail, job.id, f"no handler registered for kind {exc}", worker_id=worker_id
        )
    except Exception as exc:
        log.exception("job %s (%s) failed", job.id, job.kind)
        err = "".join(traceback.format_exception(exc))[-4000:]
        return await asyncio.to_thread(jobs.fail, job.id, err, worker_id=worker_id)
    ok = await asyncio.to_thread(jobs.complete, job.id, worker_id=worker_id)
    return "done" if ok else "lost"


async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


async def run_worker(
    stop_event: asyncio.Event,
    *,
    kinds: list[str] | None = None,
    concurrency: int = 1,
    worker_id: str | None = None,
    poll_interval: float = 0.5,
    lease_seconds: float = jobs.DEFAULT_LEASE_S,
) -> None:
    """Process jobs until ``stop_event`` is set. ``kinds=None`` → every registered kind.

    Each of the ``concurrency`` lanes leases one job at a time, so priority order is respected
    globally. Lanes finish their current job before exiting.
    """
    load_plugins()
    base_id = worker_id or default_worker_id()

    async def lane(n: int) -> None:
        wid = f"{base_id}:{n}"
        while not stop_event.is_set():
            try:
                lane_kinds = kinds if kinds is not None else REGISTRY.all_kinds()
                job = await asyncio.to_thread(jobs.lease, wid, lane_kinds, lease_seconds)
                if job is None:
                    await _wait(stop_event, poll_interval)
                    continue
                await execute_job(job, wid, lease_seconds=lease_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker lane %s error", wid)
                await _wait(stop_event, max(poll_interval, 1.0))

    await asyncio.gather(*(lane(i) for i in range(max(concurrency, 1))))


def _periodic_state_key(name: str) -> str:
    return f"periodic:{name}"


def schedule_due(now: datetime | None = None) -> list[str]:
    """Enqueue every due periodic task (one scheduler tick). Returns names enqueued."""
    now = now or now_utc()
    enqueued: list[str] = []
    for spec in list(REGISTRY.periodics.values()):
        with db.transaction() as conn:
            state = get_state(conn, _periodic_state_key(spec.name), {}) or {}
            last = state.get("last_enqueued_at")
            if last is None:
                due = spec.run_on_start
                if not due:
                    set_state(conn, _periodic_state_key(spec.name), {"last_enqueued_at": now.isoformat()})
                    continue
            else:
                due = now >= parse_iso(last) + timedelta(seconds=spec.seconds(now))
            if not due:
                continue
            active = conn.execute(
                select(job_table.c.id).where(
                    and_(job_table.c.kind == spec.job_kind, job_table.c.status.in_(("queued", "running")))
                )
            ).first()
            if active is not None:
                continue
            jobs.enqueue(
                conn,
                spec.job_kind,
                {"scheduled_at": now.isoformat()},
                priority=spec.priority,
                max_attempts=spec.max_attempts,
            )
            set_state(conn, _periodic_state_key(spec.name), {"last_enqueued_at": now.isoformat()})
            enqueued.append(spec.name)
    return enqueued


async def run_scheduler(stop_event: asyncio.Event, *, tick: float = 1.0) -> None:
    """Enqueue due periodic jobs every ``tick`` seconds until ``stop_event`` is set."""
    load_plugins()
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(schedule_due)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await _wait(stop_event, tick)
