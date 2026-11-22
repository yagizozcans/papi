"""Shared helpers of the KAP tools (not a tool: modules starting with ``_`` are skipped)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, select

from papi.analyst.context import ATTACHMENT_BASE, _documents, _spans, live_events
from papi.core.schema import disclosure, event, source


class NotFound(LookupError):
    pass


def resolve_disclosure(conn: Connection, ref: str) -> Any:
    """Disclosure row for a KAP disclosure index, an event id or a disclosure id."""
    ref = str(ref).strip()
    row = (
        conn.execute(
            select(disclosure)
            .join(source, source.c.id == disclosure.c.source_id)
            .where(source.c.code == "kap", disclosure.c.source_ref == ref)
            .order_by(disclosure.c.first_seen_at.desc())
        )
        .mappings()
        .first()
    )
    if row is None:
        ev = conn.execute(select(event.c.primary_disclosure_id).where(event.c.id == ref)).first()
        if ev and ev[0]:
            row = conn.execute(select(disclosure).where(disclosure.c.id == ev[0])).mappings().first()
    if row is None:
        row = conn.execute(select(disclosure).where(disclosure.c.id == ref)).mappings().first()
    if row is None:
        raise NotFound(f"no KAP disclosure for {ref!r}")
    return row


def first_event(conn: Connection, d: Any) -> Any:
    rows = live_events(conn, d["id"])
    if not rows:
        raise NotFound(f"disclosure {d['source_ref']} has no live events")
    return rows[0]


def document_offsets(conn: Connection, d: Any) -> list[tuple[Any, int, list[Any]]]:
    """(document row, span-marker offset, spans) per document, same numbering as the context pack:
    the body keeps its ordinals, each attachment starts at the next multiple of ``ATTACHMENT_BASE``."""
    out: list[tuple[Any, int, list[Any]]] = []
    top = 0
    for k, doc in enumerate(_documents(conn, d)):
        spans = _spans(conn, doc["id"])
        offset = 0 if k == 0 else (top // ATTACHMENT_BASE + 1) * ATTACHMENT_BASE
        if spans:
            top = max(top, offset + max(s.ordinal for s in spans))
        out.append((doc, offset, spans))
    return out
