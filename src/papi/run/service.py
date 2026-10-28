"""``papi run``: scheduler, worker lanes, web server, wake watchdog and stay-awake in one asyncio process.

Startup (``main``): logging → home check → single-instance lock → ``migrate()`` → ``load_plugins()`` →
Claude environment check (warn + ops alert; refuse only if ``ANTHROPIC_API_KEY`` is set) → release
leases orphaned by a dead previous process on this host → ``Runner.serve()``.

Shutdown (SIGINT/SIGTERM): stop leasing, give running jobs ``[run].shutdown_grace_s`` to finish
(a second signal skips the wait), then hand back the leases still held (``jobs.defer``: re-queued
now, attempt refunded) so the next start re-runs them at once.

kv_state: ``run:heartbeat`` (pid, worker id, lanes, web URL, last scheduler tick, …; written every
``[run].watchdog_interval_s``), ``run:last_wake``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from sqlalchemy import select

from papi.core import db, jobs
from papi.core.config import Settings, get_settings
from papi.core.logging import setup_logging
from papi.core.registry import load_plugins
from papi.core.runtime import default_worker_id, run_worker, schedule_due
from papi.core.schema import job as job_table
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc
from papi.run import watchdog
from papi.run.caffeinate import Caffeinator, market_open
from papi.run.config import RunConfig, run_config
from papi.run.lanes import Lane, partition
from papi.run.lock import AlreadyRunning, InstanceLock

log = logging.getLogger("papi.run")

HEARTBEAT_KEY = "run:heartbeat"
LAST_WAKE_KEY = "run:last_wake"
CAFFEINATE_CHECK_S = 60.0
WORKER_POLL_S = 1.0
TELEGRAM_POLL_S = 0.5
API_KEY_ENV = "ANTHROPIC_API_KEY"


class StartupError(RuntimeError):
    """``papi run`` must not start (message is user-facing)."""


def _ops_alert(text: str, *, level: str = "warning", dedupe_key: str) -> None:
    from papi.notify.ops import ops_alert

    with db.transaction() as conn:
        ops_alert(conn, text, level=level, dedupe_key=dedupe_key)


def startup_checks(settings: Settings | None = None) -> list[str]:
    """Claude environment problems (logged + ops alert).

    Raises ``StartupError`` if ``ANTHROPIC_API_KEY`` is set."""
    from papi.llm.backend import check_environment

    problems = check_environment(settings)
    if problems:
        for p in problems:
            log.warning("startup check: %s", p)
        _ops_alert(
            "papi run başlangıç kontrolü:\n" + "\n".join(f"• {p}" for p in problems),
            dedupe_key="run:startup:env",
        )
    if os.environ.get(API_KEY_ENV):
        raise StartupError(
            f"{API_KEY_ENV} ortamda tanımlı: Claude Code abonelik yerine API'den ücretlendirir. "
            "Değişkeni kaldırıp yeniden başlatın."
        )
    return problems


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def release_orphans(host: str | None = None) -> int:
    """Re-queue jobs leased by a previous, now dead ``papi`` process on this host (no attempt used).

    Worker ids look like ``<host>:<pid>:…`` (``default_worker_id``)."""
    host = host or socket.gethostname()
    with db.connect() as conn:
        rows = conn.execute(
            select(job_table.c.id, job_table.c.lease_owner).where(
                job_table.c.status == "running", job_table.c.lease_owner.like(f"{host}:%")
            )
        ).all()
    released = 0
    for row in rows:
        parts = row.lease_owner.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        pid = int(parts[1])
        if pid == os.getpid() or _pid_alive(pid):
            continue
        if jobs.defer(row.id, now_utc(), reason="released: owner process gone", worker_id=row.lease_owner):
            released += 1
    if released:
        log.info("re-queued %d jobs orphaned by a previous run", released)
    return released


def release_leases(worker_prefix: str) -> list[str]:
    """Hand back every running job leased by ``<worker_prefix>:…`` (re-queued now, attempt refunded)."""
    with db.connect() as conn:
        rows = conn.execute(
            select(job_table.c.id, job_table.c.lease_owner, job_table.c.kind).where(
                job_table.c.status == "running", job_table.c.lease_owner.like(f"{worker_prefix}:%")
            )
        ).all()
    released = []
    for row in rows:
        if jobs.defer(row.id, now_utc(), reason="released at shutdown", worker_id=row.lease_owner):
            released.append(row.id)
            log.info("released lease of %s (%s) at shutdown", row.id, row.kind)
    return released


def _lease_seconds(lane: Lane, settings: Settings) -> float:
    if lane.name == "llm":
        # a call may be retried once with a repair prompt; each may take llm.timeout_s
        return max(jobs.DEFAULT_LEASE_S, 2 * float(settings.llm.timeout_s) + 60.0)
    return jobs.DEFAULT_LEASE_S


class Runner:
    """One ``papi run`` instance. ``serve()`` returns after a stop request and a clean shutdown."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        web: bool = True,
        host: str | None = None,
        port: int | None = None,
        worker_id: str | None = None,
        install_signals: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg: RunConfig = run_config(self.settings)
        self.web = web
        self.host = host or self.settings.web.host
        self.port = port or self.settings.web.port
        self.worker_id = worker_id or default_worker_id()
        self.install_signals = install_signals
        self.lanes: list[Lane] = []
        self.started_at: datetime | None = None
        self.last_tick: datetime | None = None
        self.web_url: str | None = None
        self.server: Any = None
        self.caffeinator = Caffeinator()
        self.watchdog = watchdog.ClockWatchdog(self.cfg.wake_jump_s)
        self.stop_event: asyncio.Event | None = None
        self._force: asyncio.Event | None = None

    # ------------------------------------------------------------------ control

    def request_stop(self) -> None:
        assert self.stop_event is not None and self._force is not None
        if self.stop_event.is_set():
            log.warning("second stop request: not waiting for running jobs")
            self._force.set()
        else:
            log.info("stop requested; finishing running jobs")
            self.stop_event.set()
        if self.server is not None:
            self.server.should_exit = True

    @contextlib.contextmanager
    def _signals(self, loop: asyncio.AbstractEventLoop) -> Iterator[None]:
        if not self.install_signals or threading.current_thread() is not threading.main_thread():
            yield
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_stop)
        try:
            yield
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    # ------------------------------------------------------------------ tasks

    async def _scheduler(self) -> None:
        assert self.stop_event is not None
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(schedule_due)
                self.last_tick = now_utc()
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), self.cfg.scheduler_tick_s)

    def heartbeat(self, *, stopped: bool = False) -> dict[str, Any]:
        value = {
            "pid": os.getpid(),
            "worker_id": self.worker_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "heartbeat_at": now_utc().isoformat(),
            "last_scheduler_tick": self.last_tick.isoformat() if self.last_tick else None,
            "lanes": [
                {"name": ln.name, "concurrency": ln.concurrency, "kinds": ln.kinds} for ln in self.lanes
            ],
            "web": self.web_url,
            "caffeinate": self.caffeinator.active,
            "stopped_at": now_utc().isoformat() if stopped else None,
        }
        with db.transaction() as conn:
            set_state(conn, HEARTBEAT_KEY, value)
        return value

    def _wake(self, jump: float) -> None:
        job_id = watchdog.on_wake(jump)
        with db.transaction() as conn:
            set_state(conn, LAST_WAKE_KEY, {"at": now_utc().isoformat(), "jump_s": jump, "poll_job": job_id})

    async def _monitor(self) -> None:
        assert self.stop_event is not None
        loop = asyncio.get_running_loop()
        next_caffeinate = 0.0
        self.watchdog.check()
        while not self.stop_event.is_set():
            try:
                jump = self.watchdog.check()
                if jump is not None:
                    await asyncio.to_thread(self._wake, jump)
                if self.cfg.prevent_sleep_on_ac and loop.time() >= next_caffeinate:
                    want = await asyncio.to_thread(market_open, now_utc(), self.cfg)
                    self.caffeinator.update(want)
                    next_caffeinate = loop.time() + CAFFEINATE_CHECK_S
                await asyncio.to_thread(self.heartbeat)
            except Exception:
                log.exception("monitor tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), self.cfg.watchdog_interval_s)

    async def _web(self) -> None:
        import uvicorn

        from papi.web.app import create_app

        class _Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self) -> Iterator[None]:  # the runner owns SIGINT/SIGTERM
                yield

        config = uvicorn.Config(
            create_app(host=self.host),
            host=self.host,
            port=self.port,
            log_config=None,
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
        self.server = _Server(config)
        url = f"http://{self.host}:{self.port}"
        self.web_url = url
        try:
            await self.server.serve()
        except SystemExit:  # uvicorn exits on bind/startup failure
            self.web_url = None
            self.server = None
            log.error("web server could not start on %s; continuing without web", url)
            await asyncio.to_thread(
                _ops_alert,
                f"papi web {url} adresinde başlatılamadı (port kullanımda mı?).",
                dedupe_key="run:web",
            )

    # ------------------------------------------------------------------ main

    async def serve(self) -> None:
        self.stop_event = asyncio.Event()
        self._force = asyncio.Event()
        self.started_at = now_utc()
        self.lanes = partition(settings=self.settings)
        loop = asyncio.get_running_loop()
        with self._signals(loop):
            lane_tasks = [
                asyncio.create_task(
                    run_worker(
                        self.stop_event,
                        kinds=lane.kinds,
                        concurrency=lane.concurrency,
                        worker_id=f"{self.worker_id}:{lane.name}",
                        poll_interval=TELEGRAM_POLL_S if lane.name == "telegram" else WORKER_POLL_S,
                        lease_seconds=_lease_seconds(lane, self.settings),
                    ),
                    name=f"lane:{lane.name}",
                )
                for lane in self.lanes
            ]
            for lane in self.lanes:
                log.info("lane %s ×%d: %s", lane.name, lane.concurrency, ", ".join(lane.kinds))
            aux = [
                asyncio.create_task(self._scheduler(), name="scheduler"),
                asyncio.create_task(self._monitor(), name="monitor"),
            ]
            if self.web:
                aux.append(asyncio.create_task(self._web(), name="web"))
            log.info("papi run started (pid %s, web %s)", os.getpid(), "on" if self.web else "off")
            await self.stop_event.wait()
            await self._shutdown(lane_tasks, aux)

    async def _shutdown(self, lane_tasks: list[asyncio.Task[Any]], aux: list[asyncio.Task[Any]]) -> None:
        assert self._force is not None
        if lane_tasks:
            lanes_done = asyncio.gather(*lane_tasks, return_exceptions=True)
            force = asyncio.create_task(self._force.wait())
            await asyncio.wait(
                [lanes_done, force], timeout=self.cfg.shutdown_grace_s, return_when=asyncio.FIRST_COMPLETED
            )
            force.cancel()
            for t in lane_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*lane_tasks, return_exceptions=True)
        released = await asyncio.to_thread(release_leases, self.worker_id)
        if released:
            log.warning("released %d unfinished jobs; they re-run on next start", len(released))
        if self.server is not None:
            self.server.should_exit = True
        _done, pending = await asyncio.wait(aux, timeout=10)
        for t in pending:
            t.cancel()
        await asyncio.gather(*aux, return_exceptions=True)
        self.caffeinator.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.heartbeat, stopped=True)
        log.info("papi run stopped")


def ensure_initialised(settings: Settings) -> None:
    if not settings.paths.db.exists():
        raise StartupError(f"veritabanı yok ({settings.paths.db}); önce `papi init` çalıştırın.")


def run_loop(runner: Runner) -> None:
    """Run ``runner.serve()`` on a fresh loop without waiting for job threads that outlive the shutdown
    (their leases were already handed back; ``asyncio.run`` would block on them)."""
    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(thread_name_prefix="papi-run")
    loop.set_default_executor(executor)
    try:
        loop.run_until_complete(runner.serve())
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        loop.close()


def lingering_threads(timeout: float = 3.0) -> list[threading.Thread]:
    """Non-daemon threads still alive after up to ``timeout`` s (idle pool threads exit within ms)."""
    deadline = time.monotonic() + timeout
    while True:
        alive = [
            t
            for t in threading.enumerate()
            if t is not threading.main_thread() and t.is_alive() and not t.daemon
        ]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)


def configure_logging(settings: Settings) -> None:
    """papi JSON log (+ console); uvicorn warnings go to the same handlers."""
    setup_logging(settings)
    for name in ("uvicorn", "uvicorn.error"):
        ulog = logging.getLogger(name)
        ulog.handlers = list(logging.getLogger("papi").handlers)
        ulog.setLevel(logging.WARNING)
        ulog.propagate = False


def main(*, web: bool = True, host: str | None = None, port: int | None = None) -> int:
    """Entry point of ``papi run``. Returns the process exit code."""
    settings = get_settings()
    configure_logging(settings)
    lock = InstanceLock()
    try:
        ensure_initialised(settings)
        lock.acquire()
    except (StartupError, AlreadyRunning) as exc:
        log.error("%s", exc)
        return 1
    try:
        db.migrate()
        load_plugins()
        try:
            startup_checks(settings)
        except StartupError as exc:
            log.error("%s", exc)
            return 2
        release_orphans()
        run_loop(Runner(settings=settings, web=web, host=host, port=port))
        return 0
    finally:
        lock.release()


def last_heartbeat(conn: Any) -> dict[str, Any] | None:
    return get_state(conn, HEARTBEAT_KEY)
