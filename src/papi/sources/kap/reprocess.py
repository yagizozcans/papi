"""Re-processing: ``refresh`` (re-fetch a known disclosure to catch in-place edits and takedowns) and
``reparse`` (re-run parse/normalize from raw; ING-N8 — emits ``disclosure.revised`` only for changed
outputs)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, select

from papi.core.schema import disclosure
from papi.core.timeutil import IST
from papi.sources.kap.client import KapClient, KapNotFound
from papi.sources.kap.common import kap_source_id, release
from papi.sources.kap.ingest import mark_failed, process_disclosure
from papi.sources.kap.normalize import mark_blocked, parse_and_normalize

log = logging.getLogger("papi.kap.reprocess")


def refresh_disclosure(conn: Connection, client: KapClient, ref: str) -> str:
    """Returns ``blocked`` | ``removed`` | ``updated`` | ``unchanged`` | ``unknown``."""
    sid = kap_source_id(conn)
    row = conn.execute(
        select(disclosure.c.id, disclosure.c.extra).where(
            disclosure.c.source_id == sid, disclosure.c.source_ref == ref
        )
    ).first()
    if row is None:
        release(conn)
        return "unknown"
    release(conn)
    try:
        detail = client.disclosure_detail(int(ref))
    except KapNotFound:
        mark_blocked(conn, row.id, reason="removed")
        conn.commit()
        return "removed"
    basic = (detail.data.get("disclosure") or {}).get("disclosureBasic") or {}
    if basic.get("isBlocked"):
        desc = ((detail.data.get("disclosure") or {}).get("disclosureDetail") or {}).get("blockedDescription")
        mark_blocked(conn, row.id, reason="kap_blocked", description=desc)
        conn.commit()
        return "blocked"
    if ((row.extra or {}).get("raw") or {}).get("detail") == detail.raw_sha256:
        return "unchanged"
    process_disclosure(conn, client, ref, mode="reconcile", force=True, detail=detail, tolerate_errors=True)
    return "updated"


def _ist_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=IST)


def reparse(
    conn: Connection, start: date, end: date, *, template: str | None = None, publish: bool = True
) -> dict[str, Any]:
    """Re-run parse/normalize from archived raw for disclosures published in ``[start, end]``."""
    sid = kap_source_id(conn)
    rows = conn.execute(
        select(disclosure.c.id, disclosure.c.source_ref, disclosure.c.extra)
        .where(
            disclosure.c.source_id == sid,
            disclosure.c.published_at >= _ist_start(start),
            disclosure.c.published_at < _ist_start(end + timedelta(days=1)),
        )
        .order_by(disclosure.c.published_at)
    ).all()
    stats: dict[str, Any] = {
        "checked": 0,
        "changed": 0,
        "unchanged": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    for r in rows:
        ex = r.extra or {}
        if not (ex.get("raw") or {}).get("detail"):
            stats["skipped"] += 1
            continue
        if template and ex.get("template_id") != template:
            continue
        stats["checked"] += 1
        try:
            res = parse_and_normalize(conn, r.id, reason="reparse", publish=publish)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            mark_failed(conn, r.source_ref, "parse", f"{type(exc).__name__}: {exc}")
            conn.commit()
            stats["failed"] += 1
            stats["errors"].append(f"{r.source_ref}: {exc}")
            continue
        stats["changed" if res.changed else "unchanged"] += 1
    return stats
