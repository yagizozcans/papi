"""Metrics as rows in the ``metric`` table (no metrics stack; surfaced on the Ops page).

Naming: ``<area>.<what>[_<unit>]``, e.g. ``kap.latency_s``, ``kap.poll.ok``, ``jobs.dead``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select

from papi.core import db
from papi.core.schema import metric
from papi.core.timeutil import now_utc


def record(
    name: str, value: float, /, *, conn: Connection | None = None, at: datetime | None = None, **labels: Any
) -> None:
    """Append one sample. Uses ``conn`` if given (caller commits), else its own transaction."""
    values = {"name": name, "value": float(value), "labels": labels or None, "at": at or now_utc()}
    if conn is not None:
        conn.execute(metric.insert().values(**values))
        return
    with db.transaction() as tx:
        tx.execute(metric.insert().values(**values))


def series(
    conn: Connection, name: str, *, since: datetime | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    """Samples of ``name`` (newest first) as dicts ``{value, labels, at}``."""
    stmt = select(metric.c.value, metric.c.labels, metric.c.at).where(metric.c.name == name)
    if since is not None:
        stmt = stmt.where(metric.c.at >= since)
    rows = conn.execute(stmt.order_by(metric.c.at.desc()).limit(limit)).all()
    return [{"value": r.value, "labels": r.labels or {}, "at": r.at} for r in rows]
