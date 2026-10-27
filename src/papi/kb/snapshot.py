"""KB snapshots (KB-3): immutable published layers of provision versions.

``provision_version`` rows are append-only and tagged with the snapshot that introduced them. A
snapshot's content is every version introduced by it or by an earlier published snapshot, so
publishing never rewrites history. New parse results always land in the single open ``draft``
snapshot; ``publish`` turns it into the current published snapshot (the previous one becomes
``superseded``) and emits ``kb.version.published.v1``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, func, select

from papi.core import events
from papi.core.ids import uuid7
from papi.core.schema import kb_snapshot, provision, provision_version, regulation
from papi.core.timeutil import now_utc

EVENT_SOURCE = "papi.kb"


class NothingToPublish(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishResult:
    snapshot_id: str
    label: str
    stats: dict[str, Any]
    event_id: str


def draft_snapshot(conn: Connection, *, create: bool = True) -> str | None:
    """Id of the open draft snapshot (created on demand when ``create``)."""
    sid = conn.execute(
        select(kb_snapshot.c.id).where(kb_snapshot.c.status == "draft").order_by(kb_snapshot.c.created_at)
    ).scalar()
    if sid is None and create:
        sid = uuid7()
        conn.execute(kb_snapshot.insert().values(id=sid, status="draft", created_at=now_utc(), stats={}))
    return sid


def current_snapshot(conn: Connection) -> str | None:
    """Latest published snapshot id, or ``None`` before the first publish."""
    return conn.execute(
        select(kb_snapshot.c.id)
        .where(kb_snapshot.c.status == "published")
        .order_by(kb_snapshot.c.published_at.desc())
        .limit(1)
    ).scalar()


def visible_snapshot_ids(
    conn: Connection, snapshot_id: str | None = None, *, include_draft: bool = False
) -> list[str]:
    """Snapshots whose versions make up ``snapshot_id`` (default: the current published state)."""
    published = conn.execute(
        select(kb_snapshot.c.id, kb_snapshot.c.published_at).where(
            kb_snapshot.c.status.in_(("published", "superseded"))
        )
    ).all()
    if snapshot_id is not None:
        row = conn.execute(select(kb_snapshot).where(kb_snapshot.c.id == snapshot_id)).first()
        if row is None:
            raise KeyError(f"unknown kb_snapshot {snapshot_id}")
        if row.status == "draft":
            return [r.id for r in published] + [row.id]
        return [r.id for r in published if r.published_at <= row.published_at]
    ids = [r.id for r in published]
    if include_draft:
        draft = draft_snapshot(conn, create=False)
        if draft:
            ids.append(draft)
    return ids


def snapshot_stats(conn: Connection, snapshot_id: str) -> dict[str, Any]:
    """Per-regulation counts of versions introduced by ``snapshot_id``."""
    rows = conn.execute(
        select(regulation.c.official_code, func.count(provision_version.c.id))
        .select_from(provision_version.join(provision).join(regulation))
        .where(provision_version.c.kb_snapshot_id == snapshot_id)
        .group_by(regulation.c.official_code)
    ).all()
    per_reg = {code: n for code, n in rows}
    return {"versions_added": sum(per_reg.values()), "per_regulation": per_reg}


def _content_hash(conn: Connection, snapshot_ids: list[str]) -> str:
    h = hashlib.sha256()
    rows = conn.execute(
        select(provision_version.c.id, provision_version.c.text)
        .where(provision_version.c.kb_snapshot_id.in_(snapshot_ids))
        .order_by(provision_version.c.id)
    )
    for vid, text in rows:
        h.update(vid.encode())
        h.update(hashlib.sha256(text.encode()).digest())
    return h.hexdigest()


def publish(conn: Connection, *, note: str | None = None) -> PublishResult:
    """Publish the draft snapshot (caller commits). Raises ``NothingToPublish`` without a draft."""
    draft = draft_snapshot(conn, create=False)
    if draft is None:
        raise NothingToPublish("taslak KB anlık görüntüsü yok (önce `papi kb parse`)")
    stats = snapshot_stats(conn, draft)
    if stats["versions_added"] == 0:
        raise NothingToPublish("taslakta yeni hüküm sürümü yok")
    n_published = conn.execute(
        select(func.count())
        .select_from(kb_snapshot)
        .where(kb_snapshot.c.status.in_(("published", "superseded")))
    ).scalar_one()
    label = f"kb-{n_published + 1:03d}"
    previous = [
        r.id for r in conn.execute(select(kb_snapshot.c.id).where(kb_snapshot.c.status == "published"))
    ]
    totals = conn.execute(
        select(func.count(func.distinct(provision.c.id)), func.count(func.distinct(regulation.c.id)))
        .select_from(provision_version.join(provision).join(regulation))
        .where(provision_version.c.kb_snapshot_id.in_([*visible_snapshot_ids(conn), draft]))
    ).one()
    stats.update({"label": label, "provisions": totals[0], "regulations": totals[1]})
    now = now_utc()
    content_hash = _content_hash(conn, [*visible_snapshot_ids(conn), draft])
    if previous:
        conn.execute(kb_snapshot.update().where(kb_snapshot.c.id.in_(previous)).values(status="superseded"))
    conn.execute(
        kb_snapshot.update()
        .where(kb_snapshot.c.id == draft)
        .values(
            status="published", published_at=now, stats=stats, content_hash=content_hash, note=note or label
        )
    )
    event_id = events.publish(conn, events.KB_VERSION_PUBLISHED, draft, {"kb_snapshot": draft}, EVENT_SOURCE)
    return PublishResult(snapshot_id=draft, label=label, stats=stats, event_id=event_id)


def list_snapshots(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(select(kb_snapshot).order_by(kb_snapshot.c.created_at)).all()
    return [dict(r._mapping) for r in rows]
