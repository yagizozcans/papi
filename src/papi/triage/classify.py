"""Rules triage of one disclosure → ≥1 ``event`` rows + ``event.classified.v1`` (PRD 04 §3, contract C5).

``classify()`` is read-only (used by the CLI dry run); ``triage_disclosure()`` persists, supersedes
events of revised disclosures and publishes. Both run inside the caller's transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, select, update

from papi.core.events import EVENT_CLASSIFIED, publish
from papi.core.ids import uuid7
from papi.core.jobs import priority_for
from papi.core.schema import disclosure, document, event, identifier, instrument, issuer
from papi.core.text import normalize_tr, tr_upper
from papi.core.timeutil import ist_date, now_utc, to_utc
from papi.triage import headline as headline_mod
from papi.triage import materiality as mat
from papi.triage import payload as payload_mod
from papi.triage import subject_map as sm_mod
from papi.triage import taxonomy as tax_mod
from papi.triage.priority import decide

EVENT_SOURCE = "triage"
NON_SUBJECT_ISSUER_KINDS = frozenset({"exchange", "regulator"})
THRESHOLD_CODE = "ownership.threshold_crossing"
INSIDER_CODE = "ownership.insider_trade"


class DisclosureNotFound(LookupError):
    pass


@dataclass
class DisclosureContext:
    id: str
    source_ref: str | None
    subject: str | None
    summary: str | None
    source_class: str | None
    source_type: str | None
    published_at: datetime | None
    is_late: bool
    revision_of: str | None
    revision_kind: str | None
    list_meta: dict[str, Any]
    body_text: str | None
    structure: dict[str, Any] | None
    issuer_ids: list[str]
    instrument_ids: list[str]
    tickers: list[str]
    issuer_name: str | None


@dataclass
class EventDraft:
    code: str
    taxonomy_version: str
    issuer_ids: list[str]
    instrument_ids: list[str]
    tickers: list[str]
    announced_at: datetime | None
    occurred_at: datetime | None
    payload: dict[str, Any] | None
    payload_source: str | None
    payload_schema: str | None
    materiality: int
    materiality_band: str
    priority: str
    headline_tr: str
    rules: dict[str, Any] = field(default_factory=dict)
    payload_spans: dict[str, list[int]] = field(default_factory=dict)
    payload_error: str | None = None

    def extra(self, *, is_catchup: bool) -> dict[str, Any]:
        out: dict[str, Any] = {"headline_tr": self.headline_tr, "tickers": self.tickers, "rules": self.rules}
        if self.payload_spans:
            out["payload_spans"] = self.payload_spans
        if self.payload_error:
            out["payload_error"] = self.payload_error
        if is_catchup:
            out["is_catchup"] = True
        return out


# ----------------------------------------------------------------------------------------- loading


def _list_meta(source_payload: Any) -> dict[str, Any]:
    if not isinstance(source_payload, dict):
        return {}
    lst = source_payload.get("list")
    if isinstance(lst, dict):
        return lst
    return source_payload if "subject" in source_payload else {}


def _valid_ticker_rows(
    conn: Connection, scheme_values: list[str] | None, entity_ids: list[str] | None, on: date
):
    q = select(identifier.c.entity_id, identifier.c.value, identifier.c.valid_from).where(
        identifier.c.entity_type == "instrument",
        identifier.c.scheme == "TICKER",
        identifier.c.valid_from <= on,
        (identifier.c.valid_to.is_(None)) | (identifier.c.valid_to > on),
    )
    if scheme_values is not None:
        q = q.where(identifier.c.value.in_(scheme_values))
    if entity_ids is not None:
        q = q.where(identifier.c.entity_id.in_(entity_ids))
    return conn.execute(q.order_by(identifier.c.valid_from.desc())).all()


def tickers_for(conn: Connection, instrument_ids: list[str], on: date | None = None) -> list[str]:
    if not instrument_ids:
        return []
    by_inst: dict[str, str] = {}
    for inst_id, value, _ in _valid_ticker_rows(conn, None, instrument_ids, on or ist_date()):
        by_inst.setdefault(inst_id, value)
    return [by_inst[i] for i in instrument_ids if i in by_inst]


def _issuer_rows(conn: Connection, ids: list[str]) -> dict[str, Any]:
    if not ids:
        return {}
    q = select(issuer.c.id, issuer.c.kind, issuer.c.short_name, issuer.c.legal_name).where(
        issuer.c.id.in_(ids)
    )
    return {r.id: r for r in conn.execute(q)}


def _instruments_by_code(conn: Connection, stock_codes: list[str], on: date) -> list[str]:
    if not stock_codes:
        return []
    by_code: dict[str, str] = {}
    for inst_id, value, _ in _valid_ticker_rows(conn, stock_codes, None, on):
        by_code.setdefault(value, inst_id)
    return list(dict.fromkeys(by_code[c] for c in stock_codes if c in by_code))


def _equities_of(conn: Connection, issuer_ids: list[str]) -> list[str]:
    if not issuer_ids:
        return []
    rows = conn.execute(
        select(instrument.c.id, instrument.c.issuer_id)
        .where(
            instrument.c.issuer_id.in_(issuer_ids),
            instrument.c.type == "equity",
            instrument.c.status == "active",
        )
        .order_by(instrument.c.created_at)
    ).all()
    order = {iid: n for n, iid in enumerate(issuer_ids)}
    return [r.id for r in sorted(rows, key=lambda r: order.get(r.issuer_id, 0))]


def resolve_subjects(
    conn: Connection, filer: str | None, related: list[str], stock_codes: list[str], on: date
) -> tuple[list[str], list[str], str | None]:
    """(issuer_ids, instrument_ids, display name) the disclosure is about.

    Related issuers, the filer and the issuers of the list item's stock codes, minus exchanges and
    regulators (BIST/SPK notices are about the named stocks, or market-wide when none are named)."""
    code_insts = _instruments_by_code(conn, stock_codes, on)
    code_issuers = (
        conn.execute(select(instrument.c.issuer_id).where(instrument.c.id.in_(code_insts))).scalars().all()
        if code_insts
        else []
    )
    ids = list(
        dict.fromkeys([*(related or []), *([filer] if filer else []), *(i for i in code_issuers if i)])
    )
    rows = _issuer_rows(conn, ids)
    issuer_ids = [i for i in ids if i in rows and (rows[i].kind or "") not in NON_SUBJECT_ISSUER_KINDS]
    instrument_ids = list(dict.fromkeys([*code_insts, *_equities_of(conn, issuer_ids)]))
    name = None
    if issuer_ids:
        r = rows[issuer_ids[0]]
        name = r.short_name or r.legal_name
    return issuer_ids, instrument_ids, name


def _stock_codes(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("stockCodes") or ""
    parts = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
    return [tr_upper(p.strip()) for p in parts if p and p.strip()]


def load_context(conn: Connection, disclosure_id: str) -> DisclosureContext:
    d = conn.execute(select(disclosure).where(disclosure.c.id == disclosure_id)).mappings().first()
    if d is None:
        raise DisclosureNotFound(disclosure_id)
    meta = _list_meta(d["source_payload"])
    body_text, structure = None, None
    if d["body_document_id"]:
        doc = conn.execute(
            select(document.c.text, document.c.structure).where(document.c.id == d["body_document_id"])
        ).first()
        if doc is not None:
            body_text, structure = doc.text, doc.structure if isinstance(doc.structure, dict) else None
    on = ist_date(d["published_at"]) if d["published_at"] else ist_date()
    issuer_ids, instrument_ids, issuer_name = resolve_subjects(
        conn, d["filer_issuer_id"], d["related_issuer_ids"] or [], _stock_codes(meta), on
    )
    revision_kind = d["revision_kind"]
    if revision_kind in (None, "original") and _form_says_correction(structure):
        revision_kind = "correction"
    return DisclosureContext(
        id=d["id"],
        source_ref=d["source_ref"],
        subject=d["source_subject"] or meta.get("subject"),
        summary=d["summary"] or meta.get("summary") or _form_value(structure, FORM_SUMMARY_LABELS),
        source_class=d["source_class"] or meta.get("disclosureClass"),
        source_type=d["source_type"] or meta.get("disclosureType"),
        published_at=d["published_at"],
        is_late=bool(d["is_late"]),
        revision_of=d["revision_of"],
        revision_kind=revision_kind,
        list_meta=meta,
        body_text=body_text,
        structure=structure,
        issuer_ids=issuer_ids,
        instrument_ids=instrument_ids,
        tickers=tickers_for(conn, instrument_ids, on),
        issuer_name=issuer_name,
    )


FORM_SUMMARY_LABELS = ("ozet bilgi",)
FORM_CORRECTION_LABEL = "yapilan aciklama duzeltme mi"


def _form_value(structure: dict[str, Any] | None, labels: tuple[str, ...]) -> str | None:
    for f in (structure or {}).get("form_fields") or []:
        if f.get("label_norm") in labels and f.get("value") not in (None, "", "-"):
            return str(f["value"])
    return None


def _form_says_correction(structure: dict[str, Any] | None) -> bool:
    """The KAP form's own "Yapılan açıklama düzeltme mi?" is "Evet" (the list item does not always link
    the original). Only affects the materiality flag; superseding needs ``disclosure.revision_of``."""
    value = _form_value(structure, (FORM_CORRECTION_LABEL,))
    return bool(value) and normalize_tr(value).startswith("evet")


# ------------------------------------------------------------------------------------- classifying


def _occurred_at(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None
    iso = payload.get("trade_date")
    if iso is None and payload.get("trades"):
        dates = sorted(t["date"] for t in payload["trades"] if t.get("date"))
        iso = dates[-1] if dates else None
    if not iso:
        return None
    return to_utc(datetime.combine(date.fromisoformat(iso), time()))


def classify(
    conn: Connection,
    disclosure_id: str,
    *,
    is_catchup: bool = False,
    taxonomy: tax_mod.Taxonomy | None = None,
    subject_map: sm_mod.SubjectMap | None = None,
) -> list[EventDraft]:
    """Event drafts for a disclosure (no writes)."""
    tax = taxonomy or tax_mod.load()
    smap = subject_map or sm_mod.load(taxonomy=tax)
    ctx = load_context(conn, disclosure_id)
    match = smap.match(
        sm_mod.MatchInput(
            subject=ctx.subject,
            source_class=ctx.source_class,
            source_type=ctx.source_type,
            summary=ctx.summary,
            body_text=ctx.body_text,
            template_id=(ctx.structure or {}).get("template_id"),
            form_fields=(ctx.structure or {}).get("form_fields") or [],
            tables=(ctx.structure or {}).get("tables") or [],
        )
    )
    built: list[tuple[str, payload_mod.PayloadResult]] = []
    for code in match.codes:
        res = payload_mod.build(code, tax, structure=ctx.structure, meta=ctx.list_meta, summary=ctx.summary)
        built.append((code, res))
        if code == INSIDER_CODE and THRESHOLD_CODE not in match.codes:
            crossing = payload_mod.threshold_crossing(res.payload)
            if crossing is not None:
                built.append((THRESHOLD_CODE, payload_mod.build_derived(THRESHOLD_CODE, crossing, tax)))

    exclude = [ctx.id, *([ctx.revision_of] if ctx.revision_of else [])]
    drafts: list[EventDraft] = []
    for code, res in built:
        m = mat.compute(
            conn,
            tax,
            code,
            payload=res.payload,
            issuer_ids=ctx.issuer_ids,
            instrument_ids=ctx.instrument_ids,
            announced_at=ctx.published_at,
            is_late=ctx.is_late,
            revision_kind=ctx.revision_kind,
            is_catchup=is_catchup,
            exclude_disclosure_ids=exclude,
        )
        prio = decide(conn, code, ctx.instrument_ids, m.score)
        rules = {
            "subject": ctx.subject,
            "source_class": ctx.source_class,
            "map_entry": match.entry,
            "mapped_by": match.mapped_by,
            "rule_ids": list(match.rule_ids),
            "matched_on": match.matched_on,
            "derived_from": INSIDER_CODE if code == THRESHOLD_CODE and code not in match.codes else None,
            "payload_from": res.from_,
            "materiality": m.explain(),
            "priority": {"label": prio.label, "reason": prio.reason, "holding": prio.holding},
        }
        drafts.append(
            EventDraft(
                code=code,
                taxonomy_version=tax.version,
                issuer_ids=ctx.issuer_ids,
                instrument_ids=ctx.instrument_ids,
                tickers=ctx.tickers,
                announced_at=ctx.published_at,
                occurred_at=_occurred_at(res.payload),
                payload=res.payload,
                payload_source=res.source,
                payload_schema=res.schema,
                materiality=m.score,
                materiality_band=m.band,
                priority=prio.label,
                headline_tr=headline_mod.headline(
                    tax,
                    code,
                    tickers=ctx.tickers,
                    issuer_name=ctx.issuer_name,
                    payload=res.payload,
                    summary=ctx.summary,
                    subject=ctx.subject,
                ),
                rules=rules,
                payload_spans=res.spans if res.payload is not None else {},
                payload_error=res.error,
            )
        )
    return drafts


# ------------------------------------------------------------------------------------- persisting


SUPERSEDING_KINDS = frozenset({"correction"})  # "update" follow-ups (e.g. daily buyback reports) stand alone


def _live_events(conn: Connection, disclosure_id: str) -> list[Any]:
    return conn.execute(
        select(event.c.id, event.c.code, event.c.payload)
        .where(event.c.primary_disclosure_id == disclosure_id, event.c.superseded_by.is_(None))
        .order_by(event.c.created_at, event.c.id)
    ).all()


def _supersede(conn: Connection, old_events: list[Any], new_events: list[Any]) -> list[str]:
    """Point each old event at the new event with the same code (else the first new event)."""
    if not new_events:
        return []
    by_code = {e.code: e.id for e in reversed(new_events)}
    new_ids = {e.id for e in new_events}
    changed = []
    now = now_utc()
    for old in old_events:
        if old.id in new_ids:
            continue
        target = by_code.get(old.code, new_events[0].id)
        conn.execute(update(event).where(event.c.id == old.id).values(superseded_by=target, updated_at=now))
        changed.append(old.id)
    return changed


def _revision(conn: Connection, disclosure_id: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        select(disclosure.c.revision_of, disclosure.c.revision_kind).where(disclosure.c.id == disclosure_id)
    ).first()
    if row is None:
        raise DisclosureNotFound(disclosure_id)
    if row.revision_of == disclosure_id:
        return None, row.revision_kind
    return row.revision_of, row.revision_kind


def _insert(
    conn: Connection, disclosure_id: str, drafts: list[EventDraft], revision_of: str | None, is_catchup: bool
) -> list[tuple[str, EventDraft]]:
    now = now_utc()
    created: list[tuple[str, EventDraft]] = []
    for dr in drafts:
        eid = uuid7()
        conn.execute(
            event.insert().values(
                id=eid,
                disclosure_ids=[disclosure_id, *([revision_of] if revision_of else [])],
                primary_disclosure_id=disclosure_id,
                code=dr.code,
                taxonomy_version=dr.taxonomy_version,
                issuer_ids=dr.issuer_ids,
                instrument_ids=dr.instrument_ids,
                occurred_at=dr.occurred_at,
                announced_at=dr.announced_at,
                payload=dr.payload,
                payload_source=dr.payload_source,
                payload_schema=dr.payload_schema,
                materiality=dr.materiality,
                materiality_band=dr.materiality_band,
                priority=dr.priority,
                status="provisional",
                extra=dr.extra(is_catchup=is_catchup),
                created_at=now,
            )
        )
        created.append((eid, dr))
    return created


def _publish(
    conn: Connection, disclosure_id: str, created: list[tuple[str, EventDraft]], skip: set[str]
) -> None:
    for eid, dr in created:
        if eid in skip:
            continue
        publish(
            conn,
            EVENT_CLASSIFIED,
            f"event/{eid}",
            {
                "event_id": eid,
                "code": dr.code,
                "materiality_band": dr.materiality_band,
                "priority": dr.priority,
                "disclosure_id": disclosure_id,
            },
            EVENT_SOURCE,
            priority=priority_for(dr.priority),
        )


def triage_disclosure(conn: Connection, disclosure_id: str, *, is_catchup: bool = False) -> list[str]:
    """Create (once) the events of a disclosure, supersede the events of the disclosure it corrects,
    and publish ``event.classified.v1`` per new event. Returns the disclosure's live event ids."""
    revision_of, revision_kind = _revision(conn, disclosure_id)
    corrects = revision_of if revision_kind in SUPERSEDING_KINDS else None
    existing = _live_events(conn, disclosure_id)
    if existing:
        if corrects:
            _supersede(conn, _live_events(conn, corrects), existing)
        return [e.id for e in existing]

    drafts = classify(conn, disclosure_id, is_catchup=is_catchup)
    created = _insert(conn, disclosure_id, drafts, revision_of, is_catchup)
    new_events = _live_events(conn, disclosure_id)
    if corrects:
        _supersede(conn, _live_events(conn, corrects), new_events)
    # A correction of this disclosure was triaged first (out-of-order delivery): supersede now and
    # don't announce events that are already stale.
    stale: set[str] = set()
    newer_ids = (
        conn.execute(
            select(disclosure.c.id)
            .where(
                disclosure.c.revision_of == disclosure_id,
                disclosure.c.id != disclosure_id,
                disclosure.c.revision_kind.in_(SUPERSEDING_KINDS),
            )
            .order_by(disclosure.c.first_seen_at)
        )
        .scalars()
        .all()
    )
    for newer_id in newer_ids:
        newer_events = _live_events(conn, newer_id)
        if newer_events:
            stale.update(_supersede(conn, new_events, newer_events))
            break
    _publish(conn, disclosure_id, created, stale)
    return [eid for eid, _ in created]


def _signature(items: list[tuple[str, Any]]) -> list[tuple[str, str]]:
    return sorted((code, json.dumps(payload, sort_keys=True, default=str)) for code, payload in items)


def retriage_disclosure(conn: Connection, disclosure_id: str, *, is_catchup: bool = False) -> list[str]:
    """Re-classify a disclosure whose content was re-parsed / re-fetched (KAP ``reparse`` / ``refresh``).

    When codes or payloads changed, new events replace (supersede) the live ones and are published;
    otherwise nothing happens. Returns the live event ids."""
    existing = _live_events(conn, disclosure_id)
    if not existing:
        return triage_disclosure(conn, disclosure_id, is_catchup=is_catchup)
    drafts = classify(conn, disclosure_id, is_catchup=is_catchup)
    if _signature([(e.code, e.payload) for e in existing]) == _signature(
        [(d.code, d.payload) for d in drafts]
    ):
        return [e.id for e in existing]
    revision_of, _ = _revision(conn, disclosure_id)
    created = _insert(conn, disclosure_id, drafts, revision_of, is_catchup)
    created_ids = {eid for eid, _ in created}
    new_events = [e for e in _live_events(conn, disclosure_id) if e.id in created_ids]
    _supersede(conn, existing, new_events)
    _publish(conn, disclosure_id, created, set())
    return [eid for eid, _ in created]
