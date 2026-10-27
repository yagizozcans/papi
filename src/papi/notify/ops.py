"""Ops/health alerts (C4): ``ops_alert(conn, text, level=…, dedupe_key=…)`` enqueues ``notify:ops``.

Repeats of the same ``dedupe_key`` (default: the text) within one hour are suppressed at enqueue time
(``kv_state`` key ``notify:ops:<key>``), in the caller's transaction.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import Connection

from papi.core.jobs import PRIORITY_P2, enqueue
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc, parse_iso

OPS_JOB_KIND = "notify:ops"
OPS_DEDUPE_WINDOW = timedelta(hours=1)
LEVELS = ("info", "warning", "error", "critical")


def _state_key(key: str) -> str:
    return f"notify:ops:{key}"


def ops_alert(conn: Connection, text: str, *, level: str = "warning", dedupe_key: str | None = None) -> None:
    """Queue an ops alert to Telegram/macOS; the same ``dedupe_key`` is sent at most once per hour."""
    if level not in LEVELS:
        raise ValueError(f"unknown ops level {level!r} (one of {', '.join(LEVELS)})")
    key = dedupe_key or hashlib.sha1(text.encode()).hexdigest()[:16]
    now = now_utc()
    last = get_state(conn, _state_key(key))
    if last and now - parse_iso(last) < OPS_DEDUPE_WINDOW:
        return
    set_state(conn, _state_key(key), now.isoformat())
    enqueue(
        conn,
        OPS_JOB_KIND,
        {"text": text, "level": level, "key": key, "at": now.isoformat()},
        priority=PRIORITY_P2,
    )
