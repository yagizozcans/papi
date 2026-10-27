"""Parse fetched legal documents into regulation / provision / provision_version rows (KB-2, KB-6).

``ingest_entry`` takes the last fetched raw object of a manifest document, extracts and parses it,
dates every provision (``papi.kb.versioning``) and writes, in one transaction:

* the ``regulation`` row (``extra`` keeps the manifest id, header, amendment list, dating basis,
  low-confidence structure and provisions missing from the latest text — the review queue);
* a ``document`` (kind ``regulation``) with the extracted text and the parse structure, plus one
  ``document_span`` per new provision version (provenance);
* ``provision`` rows (stable ``citation_id`` = manifest prefix + path) and a new
  ``provision_version`` in the open draft snapshot whenever text or dating changed, indexed in
  ``provision_fts``.

Historical texts from the manifest add versions for amended provisions, valid from the historical
date until the first amendment that touched the provision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select

from papi.core import db, fts, rawstore
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.logging import get_logger
from papi.core.schema import document, document_span, jurisdiction, provision, provision_version, regulation
from papi.core.state import get_state, set_state
from papi.core.timeutil import iso_utc, now_utc
from papi.kb.snapshot import draft_snapshot
from papi.kb.versioning import DocumentDating, date_document, renumbering_risk
from papi.sources.legal.extract import EXTRACTOR_VERSION, ExtractionError, extract
from papi.sources.legal.fetch import document_state, history_state
from papi.sources.legal.manifest import ManifestEntry
from papi.sources.legal.structure import PARSER_VERSION, ParsedDocument, ParsedProvision, parse

PARSER_NAME = "papi.sources.legal.structure"
FULL_PARSER_VERSION = f"{PARSER_VERSION}.{EXTRACTOR_VERSION}"
PARSE_STATE = "kb:parse:"

log = get_logger("kb.ingest")

_WS = re.compile(r"\s+")


def citation_for(entry: ManifestEntry, path: str) -> str:
    return f"{entry.citation_prefix}:{path.replace('/', ':')}"


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass
class IngestReport:
    entry_id: str
    status: str  # parsed | unchanged | not_fetched | failed
    regulation_id: str | None = None
    document_id: str | None = None
    snapshot_id: str | None = None
    provisions: int = 0
    new_provisions: int = 0
    new_versions: int = 0
    history_versions: int = 0
    low_confidence: int = 0
    missing: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float | None = None


def _jurisdiction_id(conn: Connection) -> str:
    code = get_settings().packs.jurisdiction.upper()
    jid = conn.execute(select(jurisdiction.c.id).where(jurisdiction.c.code == code)).scalar()
    if jid is None:
        raise RuntimeError(f"jurisdiction {code} not seeded (run `papi init`)")
    return jid


def _line_offsets(doc: ParsedDocument) -> list[int]:
    offsets, pos = [], 0
    for ln in doc.lines:
        offsets.append(pos)
        pos += len(ln.text) + 1
    return offsets


def _parse_raw(sha: str, content_type: str | None, shape: str) -> ParsedDocument:
    data = rawstore.get_bytes(sha)
    ex = extract(data, content_type)
    return parse(ex.lines, shape)  # type: ignore[arg-type]


def _upsert_regulation(conn: Connection, entry: ManifestEntry, jid: str) -> tuple[str, dict[str, Any]]:
    row = conn.execute(
        select(regulation.c.id, regulation.c.extra).where(
            regulation.c.jurisdiction_id == jid,
            regulation.c.kind == entry.kind,
            regulation.c.official_code == entry.official_code,
        )
    ).first()
    if row is not None:
        conn.execute(
            regulation.update()
            .where(regulation.c.id == row.id)
            .values(title=entry.title, issuer_authority=entry.authority)
        )
        return row.id, dict(row.extra or {})
    rid = uuid7()
    conn.execute(
        regulation.insert().values(
            id=rid,
            jurisdiction_id=jid,
            kind=entry.kind,
            official_code=entry.official_code,
            title=entry.title,
            issuer_authority=entry.authority,
            extra={},
            created_at=now_utc(),
        )
    )
    return rid, {}


def _current_versions(conn: Connection, regulation_id: str) -> dict[str, dict[str, Any]]:
    """Latest version from a *current* (non-historical) text per citation id."""
    role = func.json_extract(document.c.extra, "$.role")
    rows = conn.execute(
        select(
            provision.c.citation_id,
            provision_version.c.id,
            provision_version.c.text,
            provision_version.c.effective_from,
            provision_version.c.amended_by,
            provision_version.c.kb_snapshot_id,
            provision_version.c.source_span_id,
        )
        .select_from(
            provision_version.join(provision)
            .outerjoin(document_span, provision_version.c.source_span_id == document_span.c.id)
            .outerjoin(document, document_span.c.document_id == document.c.id)
        )
        .where(provision.c.regulation_id == regulation_id, func.coalesce(role, "current") == "current")
        .order_by(provision_version.c.recorded_at)
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r.citation_id] = {
            "id": r.id,
            "text": r.text,
            "effective_from": r.effective_from,
            "amended_by": r.amended_by,
            "kb_snapshot_id": r.kb_snapshot_id,
            "source_span_id": r.source_span_id,
        }
    return out


def _drop_draft_version(conn: Connection, version_id: str, span_id: str | None) -> None:
    """Replace a not-yet-published version (re-parse before publish) instead of stacking a second one."""
    fts.delete_text(conn, "provision_fts", version_id)
    conn.execute(provision_version.delete().where(provision_version.c.id == version_id))
    if span_id:
        conn.execute(document_span.delete().where(document_span.c.id == span_id))


def _existing_history(conn: Connection, regulation_id: str) -> set[tuple[str, str, date | None]]:
    role = func.json_extract(document.c.extra, "$.role")
    rows = conn.execute(
        select(provision.c.citation_id, provision_version.c.text, provision_version.c.effective_from)
        .select_from(
            provision_version.join(provision)
            .join(document_span, provision_version.c.source_span_id == document_span.c.id)
            .join(document, document_span.c.document_id == document.c.id)
        )
        .where(provision.c.regulation_id == regulation_id, role != "current")
    ).all()
    return {(r.citation_id, _norm(r.text), r.effective_from) for r in rows}


def _insert_document(
    conn: Connection,
    *,
    sha: str,
    doc: ParsedDocument,
    content_type: str | None,
    role: str,
    entry: ManifestEntry,
    url: str | None,
    structure: dict[str, Any],
) -> str:
    did = uuid7()
    conn.execute(
        document.insert().values(
            id=did,
            raw_sha256=sha,
            kind="regulation",
            mime=content_type,
            language="tr",
            text="\n".join(ln.text for ln in doc.lines),
            structure=structure,
            parser=PARSER_NAME,
            parser_version=FULL_PARSER_VERSION,
            parsed_at=now_utc(),
            extra={"manifest_id": entry.id, "role": role, "source_url": url},
        )
    )
    return did


def _insert_span(
    conn: Connection,
    document_id: str,
    ordinal: int,
    p: ParsedProvision,
    doc: ParsedDocument,
    offsets: list[int],
) -> str:
    sid = uuid7()
    end_line = doc.lines[p.line_end].text
    conn.execute(
        document_span.insert().values(
            id=sid,
            document_id=document_id,
            ordinal=ordinal,
            page=p.page,
            char_start=offsets[p.line_start],
            char_end=offsets[p.line_end] + len(end_line),
            field_path=f"prov:{p.path}",
            text=p.text,
        )
    )
    return sid


def _ensure_provision(
    conn: Connection, regulation_id: str, citation: str, p: ParsedProvision, ordinal: int
) -> tuple[str, bool]:
    row = conn.execute(
        select(provision.c.id, provision.c.heading).where(provision.c.citation_id == citation)
    ).first()
    if row is not None:
        if p.heading and p.heading != row.heading:
            conn.execute(provision.update().where(provision.c.id == row.id).values(heading=p.heading))
        return row.id, False
    pid = uuid7()
    conn.execute(
        provision.insert().values(
            id=pid,
            regulation_id=regulation_id,
            path=p.path,
            citation_id=citation,
            heading=p.heading,
            ordinal=ordinal,
        )
    )
    return pid, True


def _insert_version(
    conn: Connection,
    *,
    provision_id: str,
    text: str,
    heading: str | None,
    effective_from: date | None,
    effective_to: date | None,
    amended_by: str | None,
    span_id: str,
    snapshot_id: str,
) -> str:
    vid = uuid7()
    conn.execute(
        provision_version.insert().values(
            id=vid,
            provision_id=provision_id,
            text=text,
            effective_from=effective_from,
            effective_to=effective_to,
            amended_by=amended_by,
            source_span_id=span_id,
            kb_snapshot_id=snapshot_id,
            recorded_at=now_utc(),
        )
    )
    fts.index_text(conn, "provision_fts", vid, f"{heading or ''}\n{text}")
    return vid


def _structure_json(doc: ParsedDocument, dating: DocumentDating, entry: ManifestEntry) -> dict[str, Any]:
    provs = []
    for p in doc.provisions:
        d = dating.provisions[p.path]
        item = p.to_json()
        item.update(
            citation_id=citation_for(entry, p.path),
            effective_from=d.effective_from.isoformat() if d.effective_from else None,
            amended_by=d.amended_by,
            dating_basis=d.basis,
            unresolved_amendments=d.unresolved,
            repealed=d.repealed,
        )
        provs.append(item)
    return {
        "shape": doc.shape,
        "header": doc.header.to_json(),
        "amendment_list": [a.to_json() for a in doc.amendment_list],
        "outline": doc.outline,
        "flags": doc.flags,
        "base_effective_from": dating.base.isoformat() if dating.base else None,
        "base_basis": dating.base_basis,
        "revisions": dating.revisions,
        "appendix_lines": len(doc.appendix_lines),
        "footnotes": doc.footnotes,
        "provisions": provs,
    }


def _low_confidence(
    doc: ParsedDocument, dating: DocumentDating, entry: ManifestEntry
) -> list[dict[str, Any]]:
    out = []
    for p in doc.provisions:
        flags = list(p.flags)
        d = dating.provisions[p.path]
        if d.unresolved:
            flags.append("amendment_date_unresolved")
        if flags:
            out.append(
                {"citation_id": citation_for(entry, p.path), "flags": flags, "unresolved": d.unresolved[:3]}
            )
    return out


def ingest_entry(entry: ManifestEntry, *, force: bool = False) -> IngestReport:
    """Parse the last fetched text of ``entry`` into the draft snapshot (own transaction)."""
    import time

    t0 = time.monotonic()
    report = IngestReport(entry_id=entry.id, status="failed")
    with db.connect() as conn:
        st = document_state(conn, entry.id)
        hist_states = [history_state(conn, entry.id, n) for n in range(len(entry.history))]
        conn.rollback()
    if not st.get("sha"):
        report.status, report.error = "not_fetched", "önce `papi kb fetch` çalıştırın"
        return report
    try:
        doc = _parse_raw(st["sha"], st.get("content_type"), entry.shape)
        hist_docs = []
        for n, hs in enumerate(hist_states):
            if hs.get("sha"):
                hist_docs.append((n, hs, _parse_raw(hs["sha"], hs.get("content_type"), entry.shape)))
    except (ExtractionError, FileNotFoundError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        with db.transaction() as conn:
            set_state(
                conn,
                PARSE_STATE + entry.id,
                {"error": report.error, "sha": st["sha"], "at": iso_utc(now_utc())},
            )
        return report
    dating = date_document(doc)
    with db.transaction() as conn:
        jid = _jurisdiction_id(conn)
        reg_id, extra = _upsert_regulation(conn, entry, jid)
        report.regulation_id = reg_id
        hist_keys = sorted(f"{n}:{hs['sha']}" for n, hs, _ in hist_docs)
        if (
            not force
            and extra.get("parsed_sha") == st["sha"]
            and extra.get("parser_version") == FULL_PARSER_VERSION
            and extra.get("history_shas", []) == hist_keys
        ):
            report.status = "unchanged"
            report.provisions = extra.get("counts", {}).get("provisions", 0)
            report.low_confidence = len(extra.get("low_confidence", []))
            return report
        snapshot_id = draft_snapshot(conn)
        report.snapshot_id = snapshot_id
        offsets = _line_offsets(doc)
        structure = _structure_json(doc, dating, entry)
        doc_id = _insert_document(
            conn,
            sha=st["sha"],
            doc=doc,
            content_type=st.get("content_type"),
            role="current",
            entry=entry,
            url=st.get("url"),
            structure=structure,
        )
        report.document_id = doc_id
        conn.execute(regulation.update().where(regulation.c.id == reg_id).values(source_document_id=doc_id))
        current = _current_versions(conn, reg_id)
        prov_ids: dict[str, str] = {}
        for ordinal, p in enumerate(doc.provisions, start=1):
            cit = citation_for(entry, p.path)
            pid, created = _ensure_provision(conn, reg_id, cit, p, ordinal)
            prov_ids[p.path] = pid
            report.new_provisions += int(created)
            d = dating.provisions[p.path]
            prev = current.get(cit)
            if prev and _norm(prev["text"]) == _norm(p.text) and prev["effective_from"] == d.effective_from:
                continue
            if prev and prev["kb_snapshot_id"] == snapshot_id:
                _drop_draft_version(conn, prev["id"], prev["source_span_id"])  # drafts stay mutable
            span = _insert_span(conn, doc_id, ordinal, p, doc, offsets)
            _insert_version(
                conn,
                provision_id=pid,
                text=p.text,
                heading=p.heading,
                effective_from=d.effective_from,
                effective_to=None,
                amended_by=d.amended_by,
                span_id=span,
                snapshot_id=snapshot_id,
            )
            report.new_versions += 1
        seen = {citation_for(entry, p.path) for p in doc.provisions}
        report.missing = sorted(c for c in current if c not in seen)
        report.provisions = len(doc.provisions)

        history_notes: list[dict[str, Any]] = []
        if hist_docs:
            existing = _existing_history(conn, reg_id)
            cur_by_path = doc.by_path()
            for n, hs, hdoc in hist_docs:
                hist = entry.history[n]
                risky = renumbering_risk(doc, dating, hist.effective_from)
                h_offsets = _line_offsets(hdoc)
                h_doc_id = None
                for ordinal, hp in enumerate(hdoc.provisions, start=1):
                    cur = cur_by_path.get(hp.path)
                    if cur is None:
                        history_notes.append({"path": hp.path, "note": "not in current text"})
                        continue
                    d = dating.provisions[hp.path]
                    if d.first_change is None or d.first_change <= hist.effective_from:
                        continue  # unchanged since the historical text (or changed before it)
                    if hp.path in risky:
                        history_notes.append({"path": hp.path, "note": "renumbered since historical text"})
                        continue
                    if _norm(hp.text) == _norm(cur.text):
                        continue
                    cit = citation_for(entry, hp.path)
                    key = (cit, _norm(hp.text), hist.effective_from)
                    if key in existing:
                        continue
                    if h_doc_id is None:
                        h_doc_id = _insert_document(
                            conn,
                            sha=hs["sha"],
                            doc=hdoc,
                            content_type=hs.get("content_type"),
                            role=f"history:{n}",
                            entry=entry,
                            url=hist.url,
                            structure={
                                "shape": hdoc.shape,
                                "header": hdoc.header.to_json(),
                                "flags": hdoc.flags,
                                "history_label": hist.label,
                            },
                        )
                    span = _insert_span(conn, h_doc_id, ordinal, hp, hdoc, h_offsets)
                    _insert_version(
                        conn,
                        provision_id=prov_ids[hp.path],
                        text=hp.text,
                        heading=hp.heading or cur.heading,
                        effective_from=hist.effective_from,
                        effective_to=d.first_change,
                        amended_by=hist.label,
                        span_id=span,
                        snapshot_id=snapshot_id,
                    )
                    existing.add(key)
                    report.history_versions += 1

        low = _low_confidence(doc, dating, entry)
        report.low_confidence = len(low)
        report.flags = list(doc.flags)
        extra.update(
            manifest_id=entry.id,
            priority=entry.priority,
            citation_prefix=entry.citation_prefix,
            shape=entry.shape,
            source_url=st.get("url"),
            parsed_sha=st["sha"],
            parser_version=FULL_PARSER_VERSION,
            parsed_at=iso_utc(now_utc()),
            history_shas=hist_keys,
            header=doc.header.to_json(),
            amendment_list=[a.to_json() for a in doc.amendment_list],
            base_effective_from=dating.base.isoformat() if dating.base else None,
            base_basis=dating.base_basis,
            doc_flags=doc.flags,
            low_confidence=low,
            missing_provisions=report.missing,
            history_notes=history_notes[:200],
            counts={
                "provisions": report.provisions,
                "articles": sum(1 for p in doc.provisions if p.depth == 0),
                "dated": sum(1 for d in dating.provisions.values() if d.effective_from),
            },
        )
        conn.execute(regulation.update().where(regulation.c.id == reg_id).values(extra=extra))
        set_state(conn, PARSE_STATE + entry.id, {"sha": st["sha"], "at": iso_utc(now_utc()), "error": None})
    report.status = "parsed"
    report.elapsed_s = round(time.monotonic() - t0, 2)
    log.info(
        "kb parse %s: %d provisions, %d new versions, %d history versions",
        entry.id,
        report.provisions,
        report.new_versions,
        report.history_versions,
    )
    return report


def parse_error(conn: Connection, entry_id: str) -> str | None:
    return (get_state(conn, PARSE_STATE + entry_id) or {}).get("error")
