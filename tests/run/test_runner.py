from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest
from sqlalchemy import select

from papi.core import db as core_db
from papi.core import jobs
from papi.core.registry import job_kind, periodic
from papi.core.schema import job
from papi.core.state import get_state
from papi.run import service, watchdog
from papi.run.service import HEARTBEAT_KEY, LAST_WAKE_KEY, Runner, StartupError

FAST = "[run]\nscheduler_tick_s = 0.05\nwatchdog_interval_s = 0.05\n"


@pytest.fixture(autouse=True)
def _fast_workers(monkeypatch):
    monkeypatch.setattr(service, "WORKER_POLL_S", 0.05)
    monkeypatch.setattr(service, "TELEGRAM_POLL_S", 0.05)


def _job(job_id: str):
    with core_db.connect() as conn:
        return conn.execute(select(job).where(job.c.id == job_id)).one()


async def _until(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not await asyncio.to_thread(pred):
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def _enqueue(kind: str) -> str:
    with core_db.transaction() as conn:
        return jobs.enqueue(conn, kind, {}, priority=50)


async def _start(runner: Runner) -> asyncio.Task[None]:
    task = asyncio.create_task(runner.serve())
    while runner.stop_event is None:
        await asyncio.sleep(0.01)
    return task


async def test_graceful_shutdown_finishes_short_jobs_and_releases_the_rest(db, clean_registry, write_config):
    write_config(FAST + "shutdown_grace_s = 1.0\n")

    @job_kind("test:slow")
    async def slow(payload, ctx):
        await asyncio.sleep(3600)

    @job_kind("test:quick")
    def quick(payload, ctx):
        time.sleep(0.3)

    slow_id, quick_id = _enqueue("test:slow"), _enqueue("test:quick")
    runner = Runner(web=False, install_signals=False, worker_id="testhost:1")
    task = await _start(runner)
    await _until(lambda: _job(slow_id).status == "running" and _job(quick_id).status == "running")
    runner.request_stop()
    await asyncio.wait_for(task, 10)

    q = _job(quick_id)
    assert q.status == "done"
    s = _job(slow_id)
    assert s.status == "queued"
    assert s.lease_owner is None and s.lease_until is None
    assert s.attempts == 0  # the shutdown does not use up an attempt
    assert s.last_error == "released at shutdown"
    with core_db.connect() as conn:
        hb = get_state(conn, HEARTBEAT_KEY)
    assert hb["stopped_at"] is not None
    assert {ln["name"] for ln in hb["lanes"]} == {"general"}


async def test_second_stop_request_skips_the_grace_period(db, clean_registry, write_config):
    write_config(FAST + "shutdown_grace_s = 60\n")

    @job_kind("test:slow")
    async def slow(payload, ctx):
        await asyncio.sleep(3600)

    slow_id = _enqueue("test:slow")
    runner = Runner(web=False, install_signals=False, worker_id="testhost:2")
    task = await _start(runner)
    await _until(lambda: _job(slow_id).status == "running")
    t0 = time.monotonic()
    runner.request_stop()
    runner.request_stop()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - t0 < 5
    assert _job(slow_id).status == "queued"


async def test_scheduler_and_lanes_run_periodic_jobs(db, clean_registry, write_config):
    write_config(FAST)
    ran = threading.Event()

    @periodic("test:tick", 3600.0)
    def tick(ctx):
        ran.set()

    runner = Runner(web=False, install_signals=False, worker_id="testhost:3")
    task = await _start(runner)
    await _until(ran.is_set)
    assert runner.last_tick is not None
    runner.request_stop()
    await asyncio.wait_for(task, 10)


async def test_wake_jump_enqueues_catchup_poll(db, clean_registry, write_config, monkeypatch):
    write_config(FAST)
    monkeypatch.setattr(watchdog, "reset_telegram", lambda: None)
    runner = Runner(web=False, install_signals=False, worker_id="testhost:4")
    clock = {"wall": 1000.0, "mono": 10.0}
    runner.watchdog = watchdog.ClockWatchdog(120.0, wall=lambda: clock["wall"], mono=lambda: clock["mono"])
    task = await _start(runner)
    await asyncio.sleep(0.15)
    clock["wall"] += 4 * 3600  # the Mac slept for 4 hours

    def queued_poll() -> bool:
        with core_db.connect() as conn:
            return conn.execute(select(job.c.id).where(job.c.kind == "periodic:kap:poll")).first() is not None

    await _until(queued_poll)
    runner.request_stop()
    await asyncio.wait_for(task, 10)
    with core_db.connect() as conn:
        wake = get_state(conn, LAST_WAKE_KEY)
    assert wake["jump_s"] == pytest.approx(4 * 3600)
    assert wake["poll_job"] is not None


def test_release_orphans_requeues_jobs_of_dead_processes_only(db):
    host = socket.gethostname()
    ids = {}
    with core_db.transaction() as conn:
        for name in ("dead", "alive", "other_host"):
            ids[name] = jobs.enqueue(conn, f"test:{name}", {}, priority=50)
    dead_pid = 999_999
    while True:
        try:
            os.kill(dead_pid, 0)
            dead_pid -= 1
        except ProcessLookupError:
            break
        except PermissionError:
            dead_pid -= 1
    owners = {
        "dead": f"{host}:{dead_pid}:general:0",
        "alive": f"{host}:{os.getppid()}:general:0",
        "other_host": f"elsewhere:{dead_pid}:general:0",
    }
    with core_db.transaction() as conn:
        for name, owner in owners.items():
            conn.execute(
                job.update()
                .where(job.c.id == ids[name])
                .values(status="running", attempts=1, lease_owner=owner)
            )
    assert service.release_orphans() == 1
    assert _job(ids["dead"]).status == "queued"
    assert _job(ids["dead"]).attempts == 0
    assert _job(ids["alive"]).status == "running"
    assert _job(ids["other_host"]).status == "running"


def test_startup_checks_warn_and_refuse_with_api_key(db, monkeypatch):
    import papi.llm.backend as backend

    monkeypatch.setattr(backend, "check_environment", lambda settings=None: ["claude CLI bulunamadı"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert service.startup_checks() == ["claude CLI bulunamadı"]
    with core_db.connect() as conn:
        ops = conn.execute(select(job.c.payload).where(job.c.kind == "notify:ops")).scalars().all()
    assert len(ops) == 1 and "claude CLI bulunamadı" in ops[0]["text"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(StartupError, match="ANTHROPIC_API_KEY"):
        service.startup_checks()


def test_main_refuses_second_instance_and_uninitialised_home(settings, monkeypatch):
    from papi.run.lock import InstanceLock

    monkeypatch.setattr(service, "configure_logging", lambda s: None)
    # no database yet
    assert service.main(web=False) == 1
    settings.paths.db.parent.mkdir(parents=True, exist_ok=True)
    settings.paths.db.touch()
    with InstanceLock():
        assert service.main(web=False) == 1
