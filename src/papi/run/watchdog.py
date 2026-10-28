"""Sleep/wake detection (09 §4, 02 §3.1).

macOS suspends the process during sleep: ``time.monotonic()`` stops while the wall clock keeps going.
``ClockWatchdog.check()`` compares both between ticks; a wall-clock lead larger than
``[run].wake_jump_s`` means the machine slept (or the clock was set forward) and triggers
``on_wake``: an immediate KAP poll (``poll_once`` sees the gap and lists the missed window in
catch-up mode) and a fresh Telegram client (the long-poll connection did not survive the sleep).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select

from papi.core import db, jobs, metrics
from papi.core.registry import PERIODIC_JOB_PREFIX, REGISTRY
from papi.core.schema import job as job_table
from papi.core.timeutil import now_utc

log = logging.getLogger("papi.run.watchdog")

KAP_POLL_PERIODIC = "kap:poll"
KAP_POLL_KIND = f"{PERIODIC_JOB_PREFIX}{KAP_POLL_PERIODIC}"
KAP_POLL_FALLBACK_PRIORITY = 12


@dataclass
class ClockWatchdog:
    threshold_s: float
    wall: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    _last: tuple[float, float] | None = field(default=None, init=False)

    def check(self) -> float | None:
        """Seconds the wall clock advanced beyond the monotonic clock since the last call, if that
        exceeds the threshold; else ``None``."""
        now = (self.wall(), self.mono())
        last, self._last = self._last, now
        if last is None:
            return None
        jump = (now[0] - last[0]) - (now[1] - last[1])
        return jump if jump > self.threshold_s else None


def enqueue_kap_poll(reason: str) -> str | None:
    """Queue an immediate ``periodic:kap:poll`` job unless one is already queued or running.

    Returns the new job id (``None`` if one was pending)."""
    spec = REGISTRY.periodics.get(KAP_POLL_PERIODIC)
    with db.transaction() as conn:
        active = conn.execute(
            select(job_table.c.id).where(
                job_table.c.kind == KAP_POLL_KIND, job_table.c.status.in_(("queued", "running"))
            )
        ).first()
        if active is not None:
            return None
        return jobs.enqueue(
            conn,
            KAP_POLL_KIND,
            {"scheduled_at": now_utc().isoformat(), "reason": reason},
            priority=spec.priority if spec else KAP_POLL_FALLBACK_PRIORITY,
            max_attempts=spec.max_attempts if spec else 1,
        )


def reset_telegram() -> None:
    """Drop the cached Telegram client so the next long poll opens a fresh connection."""
    from papi.telegram.api import reset_client

    reset_client()


def on_wake(jump_s: float) -> str | None:
    """Wake handling: log + metric, immediate KAP catch-up poll, Telegram reset. Returns the poll job id."""
    log.warning("wall clock jumped %.0f s ahead of the monotonic clock (sleep/wake); catching up", jump_s)
    metrics.record("run.wake", jump_s)
    job_id = enqueue_kap_poll(f"wake:{int(jump_s)}s")
    try:
        reset_telegram()
    except Exception:
        log.exception("telegram client reset after wake failed")
    return job_id
