"""Parse (raw → ``document`` + ``document_span``) and normalize (→ ``disclosure`` columns, entity
resolution, revision linkage, FTS) one disclosure, then publish its event (outbox).

Everything here reads archived raw bytes only (``disclosure.extra.raw``), so ingest, ``refresh`` and
``reparse`` share one code path and any stage can be replayed (ING-G4). Documents are versioned:
a re-parse whose output differs inserts new rows and marks the previous ones superseded
(``document.extra.superseded_at``) so citations to old spans stay valid.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select, update

from papi.core import events, fts, jobs, metrics, rawstore
from papi.core.ids import uuid7
from papi.core.schema import disclosure, document, document_span
from papi.core.timeutil import ist_date, now_utc
from papi.sources.kap import companies, deps, drift
from papi.sources.kap.client import unwrap_java_bytes
from papi.sources.kap.common import EVENT_SOURCE, item_tickers, published_at, release, split_list
from papi.sources.kap.parsing.doc import ParsedDocument
from papi.sources.kap.parsing.files import parse_file
from papi.sources.kap.parsing.html import languages, parse_body

log = logging.getLogger("papi.kap.normalize")

_ISIN_PAIR = re.compile(r"\b([A-Z][A-Z0-9]{3,5}),\s*(TR[A-Z0-9]{9}[0-9])\b")
_YES = ("evet", "yes")
_CORRECTION_LABELS = ("yapilan aciklama duzeltme mi",)
_UPDATE_LABELS = ("yapilan aciklama guncelleme mi",)


@dataclass
class PlannedDoc:
    doc: ParsedDocument
    raw_sha256: str
    role: str  # body | body_en | attachment | kap_pdf
    obj_id: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.role, self.obj_id or "", self.doc.language or "")


@dataclass
class NormalizeResult:
    disclosure_id: str
    source_ref: str
    changed: bool
    revision_kind: str
    event_type: str | None = None
    event_id: str | None = None
    priority: str | None = None
    documents: int = 0
    issuer_resolution: str = "resolved"
    warnings: list[str] = field(default_factory=list)


def load_detail(sha: str) -> dict[str, Any]:
    data = json.loads(rawstore.get_bytes(sha))
    if isinstance(data, list):
        data = data[0]
    return data


# --------------------------------------------------------------------------------------------- parse


def plan_documents(
    raw: dict[str, Any], detail: dict[str, Any], registry: drift.TemplateRegistry
) -> list[PlannedDoc]:
    planned: list[PlannedDoc] = []
    parts = [p for p in (detail.get("disclosureBody") or []) if isinstance(p, str) and p.strip()]
    if parts:
        tr = parse_body(parts, language="tr")
        meta = registry.lookup(tr.template_id)
        tr.extra["template_known"] = meta is not None
        planned.append(PlannedDoc(tr, raw["detail"], "body"))
        if "en" in languages(parts):
            en = parse_body(parts, language="en")
            en.extra["template_known"] = meta is not None
            planned.append(PlannedDoc(en, raw["detail"], "body_en"))
    for f in raw.get("files") or []:
        sha = f.get("sha256")
        if not sha:
            continue
        content = unwrap_java_bytes(rawstore.get_bytes(sha))
        doc = parse_file(content, kind="attachment", file_name=f.get("file_name"), ext=f.get("ext"))
        planned.append(PlannedDoc(doc, sha, "attachment", f.get("obj_id")))
    if raw.get("kap_pdf"):
        content = unwrap_java_bytes(rawstore.get_bytes(raw["kap_pdf"]))
        doc = parse_file(content, kind="kap_pdf", file_name="kap.pdf", ext="pdf")
        planned.append(PlannedDoc(doc, raw["kap_pdf"], "kap_pdf"))
    return planned


def _existing_documents(conn: Connection, disclosure_id: str) -> dict[tuple[str, str, str], tuple[str, str]]:
    rows = conn.execute(
        select(document.c.id, document.c.language, document.c.extra).where(
            document.c.disclosure_id == disclosure_id
        )
    ).all()
    out: dict[tuple[str, str, str], tuple[str, str]] = {}
    for r in rows:
        ex = r.extra or {}
        if ex.get("superseded_at"):
            continue
        key = (ex.get("role") or "", ex.get("obj_id") or "", r.language or "")
        out[key] = (r.id, ex.get("content_hash") or "")
    return out


def _insert_document(conn: Connection, disclosure_id: str, p: PlannedDoc, now: datetime) -> str:
    doc_id = uuid7()
    d = p.doc
    extra = {**d.extra, "role": p.role, "content_hash": d.content_hash()}
    if p.obj_id:
        extra["obj_id"] = p.obj_id
    conn.execute(
        document.insert().values(
            id=doc_id,
            raw_sha256=p.raw_sha256,
            disclosure_id=disclosure_id,
            kind=d.kind,
            mime=d.mime,
            language=d.language,
            text=d.text,
            structure=d.structure,
            parser=d.parser,
            parser_version=d.parser_version,
            parsed_at=now,
            extra=extra,
        )
    )
    if d.spans:
        conn.execute(
            document_span.insert(),
            [
                {
                    "id": uuid7(),
                    "document_id": doc_id,
                    "ordinal": s.ordinal,
                    "page": s.page,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                    "bbox": s.bbox,
                    "field_path": s.field_path,
                    "text": s.text,
                }
                for s in d.spans
            ],
        )
    return doc_id


def persist_documents(
    conn: Connection, disclosure_id: str, planned: list[PlannedDoc], now: datetime
) -> tuple[dict[tuple[str, str, str], str], bool]:
    """Insert documents whose content changed; returns ({key: document_id}, changed)."""
    existing = _existing_documents(conn, disclosure_id)
    wanted = {p.key: p.doc.content_hash() for p in planned}
    changed = set(existing) != set(wanted) or any(
        existing[k][1] != h for k, h in wanted.items() if k in existing
    )
    if not changed:
        return {k: v[0] for k, v in existing.items()}, False
    for doc_id, _ in existing.values():
        row = conn.execute(select(document.c.extra).where(document.c.id == doc_id)).scalar_one()
        conn.execute(
            update(document)
            .where(document.c.id == doc_id)
            .values(extra={**(row or {}), "superseded_at": now.isoformat()})
        )
    ids: dict[tuple[str, str, str], str] = {}
    for p in planned:
        ids[p.key] = _insert_document(conn, disclosure_id, p, now)
    tr_id = next((i for k, i in ids.items() if k[0] == "body"), None)
    en_key = next((k for k in ids if k[0] == "body_en"), None)
    if tr_id and en_key:
        en_id = ids[en_key]
        row = conn.execute(select(document.c.extra).where(document.c.id == en_id)).scalar_one()
        conn.execute(
            update(document)
            .where(document.c.id == en_id)
            .values(extra={**(row or {}), "translation_of": tr_id})
        )
    return ids, True


# ----------------------------------------------------------------------------------------- normalize


def _flag(doc: ParsedDocument | None, field_ids: tuple[str, ...], labels: tuple[str, ...]) -> bool | None:
    if doc is None:
        return None
    for f in doc.structure.get("form_fields") or []:
        fid = f.get("field_id") or ""
        if fid in field_ids or any(f.get("label_norm", "").startswith(lbl) for lbl in labels):
            return (f.get("value") or "").strip().lower().startswith(_YES)
    return None


def revision_info(
    basic: dict[str, Any], ddet: dict[str, Any], list_item: dict[str, Any], body: ParsedDocument | None
) -> dict[str, Any]:
    """KAP revision semantics (verified live): ``isChanged``/``modifyStatus`` ``DUZENLENEN`` = this
    disclosure is the edited version of ``relatedDisclosureOid``; ``DUZELTILEN`` = this one was later
    corrected; a ``relatedDisclosureOid`` without ``isChanged`` = follow-up (update flag) on a subject."""
    changed = (basic.get("isChanged") or list_item.get("modifyStatus") or "").upper() or None
    rel_oid = basic.get("relatedDisclosureOid") or None
    rel_idx = ddet.get("relatedDisclosureIndex")
    corr = _flag(body, ("oda_CorrectionAnnouncementFlag",), _CORRECTION_LABELS)
    upd = _flag(body, ("oda_UpdateAnnouncementFlag",), _UPDATE_LABELS)
    if basic.get("isBlocked"):
        kind = "blocked"
    elif changed == "DUZENLENEN" or corr:
        kind = "update" if (upd and not corr and changed != "DUZENLENEN") else "correction"
    elif rel_oid or rel_idx:
        kind = "update"
    else:
        kind = "original"
    return {
        "kind": kind,
        "modify_status": changed,
        "related_uid": rel_oid,
        "related_ref": str(rel_idx) if rel_idx else None,
        "correction_flag": corr,
        "update_flag": upd,
        "corrected_later": changed == "DUZELTILEN",
    }


def _find_original(
    conn: Connection, source_id: str, uid: str | None, ref: str | None, self_id: str
) -> str | None:
    if uid:
        hit = conn.execute(
            select(disclosure.c.id).where(
                disclosure.c.source_id == source_id,
                disclosure.c.source_uid == uid,
                disclosure.c.id != self_id,
            )
        ).scalar_one_or_none()
        if hit:
            return hit
    if ref:
        return conn.execute(
            select(disclosure.c.id).where(
                disclosure.c.source_id == source_id,
                disclosure.c.source_ref == ref,
                disclosure.c.id != self_id,
            )
        ).scalar_one_or_none()
    return None


def _related_from_body(body: ParsedDocument | None) -> list[str]:
    if body is None:
        return []
    for f in body.structure.get("form_fields") or []:
        if f.get("label_norm") in ("ilgili sirketler", "related companies"):
            return split_list(f.get("value"))
    return []


def resolve_entities(
    conn: Connection, basic: dict[str, Any], list_item: dict[str, Any], body: ParsedDocument | None, at: date
) -> dict[str, Any]:
    filer_codes, related_codes = item_tickers(list_item or {})
    b_filer, b_related = item_tickers(basic)
    for c in b_filer:
        if c not in filer_codes:
            filer_codes.append(c)
    for c in b_related + _related_from_body(body):
        if c not in related_codes and c not in filer_codes:
            related_codes.append(c)
    resolved = companies.resolve_tickers(conn, filer_codes + related_codes, at)
    filer_issuer: str | None = None
    for c in filer_codes:
        if c in resolved and resolved[c][1]:
            filer_issuer = resolved[c][1]
            break
    method = "ticker" if filer_issuer else None
    if filer_issuer is None:
        filer_issuer = companies.resolve_member_oid(conn, basic.get("mkkMemberOid"), at)
        method = "member_oid" if filer_issuer else None
    if filer_issuer is None and filer_codes:
        filer_issuer = companies.resolve_name(conn, basic.get("companyTitle") or list_item.get("kapTitle"))
        method = "name" if filer_issuer else None
    related: list[str] = [filer_issuer] if filer_issuer else []
    for c in related_codes:
        iss = resolved.get(c, (None, None))[1]
        if iss and iss not in related:
            related.append(iss)
    instrument_ids: list[str] = []
    for c in filer_codes + related_codes:
        if c in resolved and resolved[c][0] not in instrument_ids:
            instrument_ids.append(resolved[c][0])
    if filer_issuer and not any(resolved.get(c) for c in filer_codes):
        for iid in companies.issuer_instruments(conn, [filer_issuer]):
            if iid not in instrument_ids:
                instrument_ids.append(iid)
    unresolved = [
        c for c in filer_codes + related_codes if c not in resolved and companies.TICKER_RE.match(c)
    ]
    pending = bool(unresolved) or (bool(filer_codes) and filer_issuer is None)
    return {
        "filer_issuer_id": filer_issuer,
        "filer_resolution": method,
        "related_issuer_ids": related,
        "instrument_ids": instrument_ids,
        "tickers": filer_codes + related_codes,
        "filer_tickers": filer_codes,
        "unresolved_tickers": unresolved,
        "issuer_resolution": "pending" if pending else "resolved",
        "resolved": resolved,
    }


def harvest_isins(
    conn: Connection, body: ParsedDocument | None, resolved: dict[str, tuple[str, str | None]]
) -> int:
    if body is None:
        return 0
    added = 0
    for ticker, isin in set(_ISIN_PAIR.findall(body.text)):
        hit = resolved.get(ticker)
        if hit and companies.record_isin(conn, hit[0], isin):
            added += 1
    return added


_COMPARED = (
    "published_at",
    "source_uid",
    "source_class",
    "source_type",
    "source_category",
    "source_subject",
    "title",
    "summary",
    "filer_issuer_id",
    "related_issuer_ids",
    "is_late",
    "languages",
    "revision_of",
    "revision_kind",
)


def parse_and_normalize(
    conn: Connection,
    disclosure_id: str,
    *,
    reason: str = "ingest",
    is_catchup: bool | None = None,
    publish: bool = True,
) -> NormalizeResult:
    """Parse archived raw + normalize one disclosure (caller commits the write phase).

    ``conn``'s open transaction is committed first and parsing runs outside any transaction, so the
    (possibly ``BEGIN IMMEDIATE``) write phase holds the SQLite write lock only for the DB work.

    ``reason``: ``ingest`` (first pass, always publishes), ``refresh`` (KAP content re-fetched) or
    ``reparse`` (new parser) — the latter two publish ``disclosure.revised`` only if output changed.
    """
    now = now_utc()
    registry = drift.TemplateRegistry.load()
    planned_raw: dict[str, Any] | None = None
    planned: list[PlannedDoc] = []
    while True:
        row = conn.execute(select(disclosure).where(disclosure.c.id == disclosure_id)).one()
        extra: dict[str, Any] = dict(row.extra or {})
        raw = extra.get("raw") or {}
        if not raw.get("detail"):
            raise ValueError(f"disclosure {row.source_ref} has no archived detail")
        if raw == planned_raw:
            break  # row re-read inside the write transaction; parse output still matches its raw
        release(conn)
        detail = load_detail(raw["detail"])
        planned = plan_documents(raw, detail, registry)
        planned_raw = raw
    basic = (detail.get("disclosure") or {}).get("disclosureBasic") or {}
    ddet = (detail.get("disclosure") or {}).get("disclosureDetail") or {}
    payload = dict(row.source_payload or {})
    list_item = payload.get("list") or {}
    first_pass = not extra.get("normalized_at")

    doc_ids, docs_changed = persist_documents(conn, disclosure_id, planned, now)
    body = next((p.doc for p in planned if p.role == "body"), None)
    if body is not None and not body.extra.get("template_known"):
        drift.record_unknown_template(conn, body.template_id, row.source_ref)

    pub = published_at(list_item) or published_at(basic) or row.published_at
    at = ist_date(pub) if pub else ist_date(now)
    ent = resolve_entities(conn, basic, list_item, body, at)
    rev = revision_info(basic, ddet, list_item, body)
    original_id = None
    if rev["kind"] in ("correction", "update"):
        original_id = _find_original(
            conn, row.source_id, rev["related_uid"], rev["related_ref"], disclosure_id
        )
    isins = harvest_isins(conn, body, ent["resolved"])

    langs = ["tr"] + (["en"] if any(p.role == "body_en" for p in planned) else [])
    if not planned:
        langs = []
    body_id = next((i for k, i in doc_ids.items() if k[0] == "body"), None)
    attachment_ids = [i for k, i in doc_ids.items() if k[0] in ("attachment", "kap_pdf")]
    values: dict[str, Any] = {
        "published_at": pub,
        "published_at_raw": list_item.get("publishDate") or basic.get("publishDate"),
        "source_uid": basic.get("disclosureId") or row.source_uid,
        "source_class": list_item.get("disclosureClass") or basic.get("disclosureClass"),
        "source_type": list_item.get("disclosureType") or basic.get("disclosureType"),
        "source_category": list_item.get("disclosureCategory") or basic.get("disclosureCategory"),
        "source_subject": list_item.get("subject") or basic.get("title"),
        "title": basic.get("title") or list_item.get("subject"),
        "summary": (basic.get("summary") or list_item.get("summary") or "").strip() or None,
        "filer_issuer_id": ent["filer_issuer_id"],
        "related_issuer_ids": ent["related_issuer_ids"],
        "is_late": bool(basic.get("isLate") or list_item.get("isLate")),
        "languages": langs,
        "revision_of": original_id,
        "revision_kind": rev["kind"],
        "body_document_id": body_id,
        "attachment_document_ids": attachment_ids,
    }
    out_changed = any(_differs(getattr(row, k), values[k]) for k in _COMPARED)
    if is_catchup is None:
        is_catchup = bool(extra.get("is_catchup"))
    body_en_id = next((i for k, i in doc_ids.items() if k[0] == "body_en"), None)
    extra.update(
        {
            "is_catchup": is_catchup,
            "kap_title": basic.get("companyTitle") or list_item.get("kapTitle"),
            "member_oid": basic.get("mkkMemberOid"),
            "member_type": ddet.get("memberType"),
            "detail_class": basic.get("disclosureClass"),
            "tickers": ent["tickers"],
            "filer_tickers": ent["filer_tickers"],
            "instrument_ids": ent["instrument_ids"],
            "issuer_resolution": ent["issuer_resolution"],
            "filer_resolution": ent["filer_resolution"],
            "unresolved_tickers": ent["unresolved_tickers"],
            "template_id": body.template_id if body else None,
            "template_unknown": bool(body is not None and not body.extra.get("template_known")),
            "revision": {k: v for k, v in rev.items() if k != "kind"},
            "blocked": bool(basic.get("isBlocked")),
            "blocked_description": ddet.get("blockedDescription"),
            "body_document_ids": {"tr": body_id, **({"en": body_en_id} if body_en_id else {})},
            "parser_version": planned[0].doc.parser_version if planned else None,
            "normalized_at": now.isoformat(),
        }
    )
    if original_id is None and rev["kind"] in ("correction", "update"):
        extra["revision_pending"] = True
    else:
        extra.pop("revision_pending", None)
    payload["detail"] = detail.get("disclosure")
    payload["attachments"] = detail.get("attachments")
    conn.execute(
        update(disclosure)
        .where(disclosure.c.id == disclosure_id)
        .values(
            **values,
            source_payload=payload,
            extra=extra,
            ingest_status="normalized",
            ingest_error=None,
            updated_at=now,
        )
    )
    if original_id and rev["kind"] == "correction":
        _mark_superseded(conn, original_id, disclosure_id, now)
    _index_fts(conn, disclosure_id, values, extra, body)

    changed = first_pass or docs_changed or out_changed
    result = NormalizeResult(
        disclosure_id=disclosure_id,
        source_ref=row.source_ref,
        changed=changed,
        revision_kind=rev["kind"],
        documents=len(planned),
        issuer_resolution=ent["issuer_resolution"],
    )
    if isins:
        result.warnings.append(f"isin_added:{isins}")
    label = deps.holding_priority(conn, ent["instrument_ids"], pub)
    result.priority = label
    if first_pass and pub is not None and not is_catchup:
        metrics.record(
            "kap.normalize_latency_s", (now - pub).total_seconds(), conn=conn, cls=values["source_class"]
        )
    if publish and (first_pass or (reason != "ingest" and changed)):
        _publish(conn, row, values, extra, rev, original_id, label, reason, first_pass, result)
    return result


def _differs(old: Any, new: Any) -> bool:
    if isinstance(old, datetime) and isinstance(new, datetime):
        return old != new
    return (old or None) != (new or None)


def _mark_superseded(conn: Connection, original_id: str, new_id: str, now: datetime) -> None:
    row = conn.execute(select(disclosure.c.extra).where(disclosure.c.id == original_id)).scalar_one()
    extra = dict(row or {})
    if extra.get("superseded_by") == new_id:
        return
    extra["superseded_by"] = new_id
    conn.execute(update(disclosure).where(disclosure.c.id == original_id).values(extra=extra, updated_at=now))


def _index_fts(
    conn: Connection,
    disclosure_id: str,
    values: dict[str, Any],
    extra: dict[str, Any],
    body: ParsedDocument | None,
) -> None:
    parts = [
        values.get("title") or "",
        values.get("summary") or "",
        values.get("source_subject") or "",
        extra.get("kap_title") or "",
        " ".join(extra.get("tickers") or []),
        body.text if body is not None else "",
    ]
    fts.index_text(conn, "disclosure_fts", disclosure_id, "\n".join(p for p in parts if p))


def _publish(
    conn: Connection,
    row: Any,
    values: dict[str, Any],
    extra: dict[str, Any],
    rev: dict[str, Any],
    original_id: str | None,
    label: str | None,
    reason: str,
    first_pass: bool,
    result: NormalizeResult,
) -> None:
    common = {
        "disclosure_id": row.id,
        "source_ref": row.source_ref,
        "filer_issuer_id": values["filer_issuer_id"],
        "related_issuer_ids": values["related_issuer_ids"],
        "instrument_ids": extra.get("instrument_ids") or [],
        "tickers": extra.get("tickers") or [],
        "source_subject": values["source_subject"],
        "source_class": values["source_class"],
        "published_at": values["published_at"].isoformat() if values["published_at"] else None,
        "is_catchup": bool(extra.get("is_catchup")),
        "is_backfill": extra.get("discovered_by") == "backfill",
        "holding_priority": label,
        "issuer_resolution": extra.get("issuer_resolution"),
        "revision_kind": rev["kind"],
    }
    if rev["kind"] == "blocked":
        etype = events.DISCLOSURE_BLOCKED
        data = {**common, "reason": "kap_blocked"}
    elif not first_pass:
        etype = events.DISCLOSURE_REVISED
        data = {**common, "revision_of": row.id, "revision_kind": reason}
    elif original_id:
        etype = events.DISCLOSURE_REVISED
        data = {**common, "revision_of": original_id, "revision_kind": rev["kind"]}
    else:
        etype = events.DISCLOSURE_NORMALIZED
        data = {**common, "revision_of_uid": rev.get("related_uid") if rev["kind"] != "original" else None}
    prio = jobs.priority_for(label) if label else None
    result.event_type = etype
    result.event_id = events.publish(conn, etype, f"disclosure/{row.id}", data, EVENT_SOURCE, priority=prio)


def mark_blocked(
    conn: Connection, disclosure_id: str, *, reason: str, description: str | None = None
) -> str | None:
    """Takedown seen during reconcile (``isBlocked`` or the index vanished): flag + publish once."""
    now = now_utc()
    row = conn.execute(select(disclosure).where(disclosure.c.id == disclosure_id)).one()
    extra = dict(row.extra or {})
    if extra.get("blocked") and row.revision_kind == "blocked":
        return None
    extra.update({"blocked": True, "blocked_reason": reason, "blocked_description": description})
    conn.execute(
        update(disclosure)
        .where(disclosure.c.id == disclosure_id)
        .values(revision_kind="blocked", extra=extra, updated_at=now)
    )
    data = {
        "disclosure_id": disclosure_id,
        "source_ref": row.source_ref,
        "reason": reason,
        "related_issuer_ids": row.related_issuer_ids or [],
    }
    return events.publish(conn, events.DISCLOSURE_BLOCKED, f"disclosure/{disclosure_id}", data, EVENT_SOURCE)
