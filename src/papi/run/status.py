"""``papi run status`` data: instance lock, heartbeat (last scheduler tick, lanes, web), job counts per
lane, last KAP poll, governor pause, Telegram, last backup and wake, launchd service."""

from __future__ import annotations

import shutil
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from papi.core import jobs
from papi.core.config import Settings, get_settings
from papi.core.registry import load_plugins
from papi.core.schema import job
from papi.core.state import get_state
from papi.core.timeutil import now_utc, parse_iso
from papi.run import launchd
from papi.run.backup import LAST_BACKUP_KEY
from papi.run.config import run_config
from papi.run.lanes import lane_for
from papi.run.lock import holder
from papi.run.service import HEARTBEAT_KEY, LAST_WAKE_KEY

ACTIVE_STATUSES = ("queued", "running", "dead")


def _lane_counts(conn: Connection, settings: Settings) -> dict[str, dict[str, int]]:
    cfg = run_config(settings)
    out: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        select(job.c.kind, job.c.status, func.count())
        .where(job.c.status.in_(ACTIVE_STATUSES))
        .group_by(job.c.kind, job.c.status)
    )
    for kind, status, n in rows:
        lane = out.setdefault(lane_for(kind, cfg), {})
        lane[status] = lane.get(status, 0) + n
    return out


def collect(
    conn: Connection, *, settings: Settings | None = None, include_service: bool = True
) -> dict[str, Any]:
    s = settings or get_settings()
    load_plugins()
    cfg = run_config(s)
    lock = holder()
    hb = get_state(conn, HEARTBEAT_KEY) or None
    stale = None
    if hb and hb.get("heartbeat_at"):
        age = now_utc() - parse_iso(hb["heartbeat_at"])
        stale = age > timedelta(seconds=3 * cfg.watchdog_interval_s + 10)
    totals: dict[str, int] = {}
    for (status, _prio), n in jobs.counts(conn).items():
        totals[status] = totals.get(status, 0) + n

    from papi.llm.governor import pause_state
    from papi.telegram.poller import ready_to_poll

    pause = pause_state(conn)
    info: dict[str, Any] = {
        "running": lock.held,
        "pid": lock.pid,
        "heartbeat": hb,
        "heartbeat_stale": stale,
        "jobs": totals,
        "lanes": _lane_counts(conn, s),
        "kap": {
            "last_poll_at": get_state(conn, "kap:last_poll_at"),
            "last_poll_ok_at": get_state(conn, "kap:last_poll_ok_at"),
        },
        "governor": {
            "paused": pause is not None,
            "until": pause["until"].isoformat() if pause and pause["until"] else None,
            "reason": pause["reason"] if pause else None,
        },
        "telegram": {"enabled": s.telegram.enabled, "ready": ready_to_poll()},
        "last_backup": get_state(conn, LAST_BACKUP_KEY),
        "last_wake": get_state(conn, LAST_WAKE_KEY),
        "prevent_sleep_on_ac": cfg.prevent_sleep_on_ac,
    }
    if include_service and shutil.which("launchctl"):
        info["service"] = launchd.status()
    return info
