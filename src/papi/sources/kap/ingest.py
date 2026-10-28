"""Discovery bookkeeping (list items → ``disclosure`` rows + fetch jobs) and the fetch stage.

``record_listing`` diffs a list response against known ``source_ref``s inside the caller's
transaction (idempotent: unique ``(source_id, source_ref)``) and enqueues ``kap:fetch`` jobs
prioritised by holdings (P1/P2) — the outbox guarantees row + job + ``disclosure.seen`` commit
together. ``process_disclosure`` fetches detail + attachments (network, no DB lock held), archives
them, then runs parse/normalize from raw.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, select, update
from sqlalchemy.dialects.sqlite import insert

from papi.core import events, jobs, metrics
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.schema import disclosure, job
from papi.core.timeutil import ist_date, now_utc
from papi.sources.kap import companies, deps, drift
from papi.sources.kap.client import KapClient, KapFile, KapHttpError, KapJson, KapNotFound
from papi.sources.kap.common import (
    EVENT_SOURCE,
    JOB_FETCH,
    is_db_busy,
    item_hash,
    item_tickers,
    kap_source_id,
    priority_value,
    published_at,
    release,
)
from papi.sources.kap.normalize import NormalizeResult, parse_and_normalize

log = logging.getLogger("papi.kap.ingest")

_CHUNK = 500


@dataclass
class ListingDiff:
    new: list[dict[str, Any]] = field(default_factory=list)  # {"id","ref","priority","is_catchup","job_id"}
    changed: list[str] = field(default_factory=list)  # refs whose list item changed
    unchanged: int = 0


def holding_label(
    conn: Connection, item: dict[str, Any], at: datetime | None
) -> tuple[str | None, list[str]]:
    filer, related = item_tickers(item)
    day = ist_date(at) if at else ist_date()
    resolved = companies.resolve_tickers(conn, filer + related, day)
    inst = [v[0] for v in resolved.values()]
    return deps.holding_priority(conn, inst, at), inst


def known_refs(conn: Connection, source_id: str, refs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i in range(0, len(refs), _CHUNK):
        chunk = refs[i : i + _CHUNK]
        rows = conn.execute(
            select(
                disclosure.c.id, disclosure.c.source_ref, disclosure.c.extra, disclosure.c.source_payload
            ).where(disclosure.c.source_id == source_id, disclosure.c.source_ref.in_(chunk))
        ).all()
        out.update({r.source_ref: r for r in rows})
    return out


def insert_seen(
    conn: Connection,
    source_id: str,
    item: dict[str, Any],
    *,
    now: datetime,
    is_catchup: bool,
    mode: str,
) -> str | None:
    """Insert a ``seen`` row; returns its id, or ``None`` if the ref already existed."""
    ref = str(item["disclosureIndex"])
    pub = published_at(item)
    did = uuid7()
    stmt = (
        insert(disclosure)
        .values(
            id=did,
            source_id=source_id,
            source_ref=ref,
            revision_kind="original",
            published_at=pub,
            published_at_raw=item.get("publishDate"),
            first_seen_at=now,
            source_class=item.get("disclosureClass"),
            source_type=item.get("disclosureType"),
            source_category=item.get("disclosureCategory"),
            source_subject=item.get("subject") or item.get("title"),
            title=item.get("subject") or item.get("title"),
            summary=(item.get("summary") or "").strip() or None,
            is_late=bool(item.get("isLate")),
            source_payload={"list": item},
            ingest_status="seen",
            extra={"is_catchup": is_catchup, "discovered_by": mode, "list_hash": item_hash(item)},
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[disclosure.c.source_id, disclosure.c.source_ref])
    )
    if conn.execute(stmt).rowcount != 1:
        return None
    if pub is not None and not is_catchup and mode != "backfill":
        metrics.record(
            "kap.latency_s", (now - pub).total_seconds(), conn=conn, cls=item.get("disclosureClass")
        )
    return did


def enqueue_fetch(
    conn: Connection,
    ref: str,
    *,
    mode: str,
    is_catchup: bool,
    label: str | None,
    generation: str | None = None,
) -> str:
    key = f"{JOB_FETCH}:{ref}" + (f":{generation}" if generation else "")
    return jobs.enqueue(
        conn,
        JOB_FETCH,
        {"ref": ref, "mode": mode, "is_catchup": is_catchup, "priority": label},
        priority=priority_value(label, mode),
        dedupe_key=key,
        max_attempts=get_settings().kap.max_retries,
    )


def reprioritize_queued(conn: Connection) -> int:
    """Raise queued ``kap:fetch`` jobs to the P1/P2 lane when their issuer is now held or watched.

    A disclosure listed before the owner bought or watched its issuer was enqueued at the plain
    live/catch-up priority and would wait behind the whole backlog. Only upgrades (selling does not
    demote queued work). Caller commits; returns the number of jobs raised.
    """
    queued = conn.execute(
        select(job.c.id, job.c.payload, job.c.priority).where(
            and_(job.c.kind == JOB_FETCH, job.c.status == "queued")
        )
    ).all()
    if not queued:
        return 0
    rows = known_refs(conn, kap_source_id(conn), sorted({str((q.payload or {}).get("ref")) for q in queued}))
    labels: dict[tuple[str, ...], str | None] = {}
    raised = 0
    for q in queued:
        payload = dict(q.payload or {})
        row = rows.get(str(payload.get("ref")))
        item = ((row.source_payload or {}).get("list") if row is not None else None) or {}
        filer, related = item_tickers(item)
        key = tuple(filer + related)
        if not key:
            continue
        if key not in labels:
            labels[key] = holding_label(conn, item, None)[0]
        label = labels[key]
        if label is None:
            continue
        prio = priority_value(label, str(payload.get("mode") or "live"))
        if prio >= q.priority:
            continue
        payload["priority"] = label
        conn.execute(
            update(job)
            .where(job.c.id == q.id, job.c.status == "queued")
            .values(priority=prio, payload=payload, updated_at=now_utc())
        )
        raised += 1
    if raised:
        log.info("kap: %d queued fetch job(s) raised to holdings/watchlist priority", raised)
    return raised


def record_listing(
    conn: Connection,
    items: list[dict[str, Any]],
    *,
    mode: str,
    now: datetime | None = None,
    catchup_before: datetime | None = None,
    publish_seen: bool = True,
) -> ListingDiff:
    """Diff ``items`` against known refs; insert new rows + enqueue fetch jobs (caller commits).

    ``catchup_before``: new items published before this instant are flagged ``is_catchup``.
    """
    now = now or now_utc()
    sid = kap_source_id(conn)
    drift.check(conn, "list_item", items)
    valid = [it for it in items if isinstance(it.get("disclosureIndex"), int)]
    existing = known_refs(conn, sid, [str(it["disclosureIndex"]) for it in valid])
    diff = ListingDiff()
    for item in valid:
        ref = str(item["disclosureIndex"])
        if ref in existing:
            row = existing[ref]
            h = item_hash(item)
            ex = dict(row.extra or {})
            if ex.get("list_hash") != h:
                payload = dict(row.source_payload or {})
                old = payload.get("list") or {}
                payload["list"] = item
                ex["list_hash"] = h
                if item.get("modifyStatus") and item.get("modifyStatus") != old.get("modifyStatus"):
                    ex.setdefault("revision", {})["modify_status"] = item.get("modifyStatus")
                    ex["corrected_later"] = item.get("modifyStatus") == "DUZELTILEN"
                conn.execute(
                    update(disclosure)
                    .where(disclosure.c.id == row.id)
                    .values(source_payload=payload, extra=ex, updated_at=now)
                )
                diff.changed.append(ref)
            else:
                diff.unchanged += 1
            continue
        pub = published_at(item)
        is_catchup = mode in ("catchup", "reconcile", "backfill") and (
            catchup_before is None or pub is None or pub < catchup_before
        )
        did = insert_seen(conn, sid, item, now=now, is_catchup=is_catchup, mode=mode)
        if did is None:
            diff.unchanged += 1
            continue
        label, _ = holding_label(conn, item, pub)
        job_id = enqueue_fetch(conn, ref, mode=mode, is_catchup=is_catchup, label=label)
        if publish_seen and mode != "backfill":
            events.publish(
                conn,
                events.DISCLOSURE_SEEN,
                f"disclosure/{did}",
                {
                    "source_ref": ref,
                    "published_at": pub,
                    "source_id": sid,
                    "disclosure_id": did,
                    "is_catchup": is_catchup,
                    "holding_priority": label,
                },
                EVENT_SOURCE,
                priority=priority_value(label, mode),
            )
        diff.new.append(
            {"id": did, "ref": ref, "priority": label, "is_catchup": is_catchup, "job_id": job_id}
        )
    return diff


# ----------------------------------------------------------------------------------------- fetch


class ParseFailed(Exception):
    """Parse/normalize failed (deterministic: retrying the same raw will not help)."""


@dataclass
class FetchOutcome:
    ref: str
    status: str  # normalized | skipped | failed
    result: NormalizeResult | None = None
    attachments_fetched: int = 0
    attachments_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _should_fetch_attachments(policy: str, mode: str, label: str | None) -> bool:
    if mode == "backfill" or policy == "prioritized":
        return label is not None
    return True


def fetch_raw(
    client: KapClient,
    ref: str,
    *,
    detail: KapJson | None = None,
    fetch_attachments: bool = True,
    kap_pdf: bool = False,
    tolerate_errors: bool = False,
) -> tuple[KapJson, list[dict[str, Any]], KapFile | None, list[str]]:
    """Network phase: detail (unless given), attachments, optional KAP PDF. Bytes are archived by the
    client. Returns (detail, file records, kap pdf, errors)."""
    detail = detail or client.disclosure_detail(int(ref))
    data = detail.data
    errors: list[str] = []
    files: list[dict[str, Any]] = []
    for att in data.get("attachments") or []:
        obj_id = att.get("objId")
        rec = {
            "obj_id": obj_id,
            "file_name": att.get("fileName"),
            "ext": att.get("fileExtension"),
            "sha256": None,
        }
        if obj_id and fetch_attachments:
            try:
                f = client.download_attachment(obj_id, index=int(ref))
                rec["sha256"] = f.raw_sha256
                rec["content_type"] = f.content_type
            except KapNotFound as exc:
                rec["error"] = str(exc)
                errors.append(f"attachment {obj_id}: not found")
            except (KapHttpError, ValueError) as exc:
                if not tolerate_errors:
                    raise
                rec["error"] = str(exc)[:300]
                errors.append(f"attachment {obj_id}: {exc}")
        elif obj_id:
            rec["skipped"] = "policy"
        files.append(rec)
    pdf = None
    body = [p for p in (data.get("disclosureBody") or []) if isinstance(p, str) and p.strip()]
    if kap_pdf or not body:
        try:
            pdf = client.disclosure_pdf(int(ref))
        except (KapNotFound, KapHttpError) as exc:
            if not tolerate_errors and not isinstance(exc, KapNotFound):
                raise
            errors.append(f"kap pdf: {exc}")
    return detail, files, pdf, errors


def store_fetched(
    conn: Connection,
    ref: str,
    detail: KapJson,
    files: list[dict[str, Any]],
    pdf: KapFile | None,
    *,
    mode: str,
    is_catchup: bool,
) -> str:
    """Record archived shas on the row (creating it from the detail when unknown) → ``fetched``."""
    now = now_utc()
    sid = kap_source_id(conn)
    data = detail.data
    basic = (data.get("disclosure") or {}).get("disclosureBasic") or {}
    drift.check(conn, "detail", [data], ref=ref)
    drift.check(conn, "detail_basic", [basic], ref=ref)
    drift.check(
        conn, "detail_detail", [(data.get("disclosure") or {}).get("disclosureDetail") or {}], ref=ref
    )
    if data.get("attachments"):
        drift.check(conn, "attachment", data["attachments"], ref=ref)
    row = conn.execute(
        select(disclosure.c.id, disclosure.c.extra).where(
            disclosure.c.source_id == sid, disclosure.c.source_ref == ref
        )
    ).first()
    if row is None:
        did = insert_seen(
            conn, sid, {**basic, "disclosureIndex": int(ref)}, now=now, is_catchup=is_catchup, mode=mode
        )
        assert did is not None
        extra: dict[str, Any] = {"is_catchup": is_catchup, "discovered_by": mode}
    else:
        did = row.id
        extra = dict(row.extra or {})
    raw = {"detail": detail.raw_sha256, "files": files}
    if pdf is not None:
        raw["kap_pdf"] = pdf.raw_sha256
    extra["raw"] = raw
    extra["fetched_at"] = now.isoformat()
    conn.execute(
        update(disclosure)
        .where(disclosure.c.id == did)
        .values(
            source_uid=basic.get("disclosureId"),
            extra=extra,
            ingest_status="fetched",
            ingest_error=None,
            updated_at=now,
        )
    )
    return did


def mark_failed(conn: Connection, ref: str, stage: str, error: str) -> None:
    sid = kap_source_id(conn)
    conn.execute(
        update(disclosure)
        .where(disclosure.c.source_id == sid, disclosure.c.source_ref == ref)
        .values(ingest_status=f"failed:{stage}", ingest_error=error[:2000], updated_at=now_utc())
    )
    metrics.record("kap.fetch.fail", 1, conn=conn, stage=stage)


def process_disclosure(
    conn: Connection,
    client: KapClient,
    ref: str,
    *,
    mode: str = "live",
    is_catchup: bool | None = None,
    label: str | None = None,
    force: bool = False,
    kap_pdf: bool = False,
    tolerate_errors: bool = False,
    detail: KapJson | None = None,
) -> FetchOutcome:
    """Fetch → archive → parse → normalize → publish for one ``disclosureIndex`` (commits in phases).

    Raises ``KapBlocked`` (caller defers), ``KapNotFound`` (row marked ``failed:fetch``) and transient
    ``KapError``s (caller retries). Parse/normalize errors mark ``failed:parse`` and re-raise.
    """
    sid = kap_source_id(conn)
    row = conn.execute(
        select(disclosure.c.id, disclosure.c.ingest_status, disclosure.c.extra).where(
            disclosure.c.source_id == sid, disclosure.c.source_ref == ref
        )
    ).first()
    if row is not None and row.ingest_status == "normalized" and not force:
        release(conn)
        return FetchOutcome(ref, "skipped")
    if is_catchup is None:
        is_catchup = bool((row.extra or {}).get("is_catchup")) if row is not None else False
    policy = get_settings().kap.attachments_fetch_policy
    release(conn)
    try:
        det, files, pdf, errors = fetch_raw(
            client,
            ref,
            detail=detail,
            fetch_attachments=_should_fetch_attachments(policy, mode, label),
            kap_pdf=kap_pdf,
            tolerate_errors=tolerate_errors,
        )
    except KapNotFound as exc:
        if row is not None:
            mark_failed(conn, ref, "fetch", f"not found: {exc}")
            conn.commit()
        raise
    did = store_fetched(conn, ref, det, files, pdf, mode=mode, is_catchup=is_catchup)
    conn.commit()
    outcome = FetchOutcome(ref, "normalized", errors=errors)
    outcome.attachments_fetched = sum(1 for f in files if f.get("sha256"))
    outcome.attachments_skipped = sum(1 for f in files if f.get("skipped"))
    try:
        outcome.result = parse_and_normalize(
            conn, did, reason="refresh" if force else "ingest", is_catchup=is_catchup
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if is_db_busy(exc):
            raise  # contention: the job retries (raw is archived, status stays ``fetched``)
        mark_failed(conn, ref, "parse", f"{type(exc).__name__}: {exc}")
        conn.commit()
        raise ParseFailed(f"{ref}: {type(exc).__name__}: {exc}") from exc
    return outcome
