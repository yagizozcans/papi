"""Read models for the dashboard pages and the ``/v1`` API (SQLAlchemy Core over ``papi.core.schema``).

Every function takes an open connection and returns plain dicts/lists, so HTML pages and JSON routes
share one data layer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    Connection,
    and_,
    exists,
    false,
    func,
    literal,
    or_,
    select,
)

from papi.core import jobs as core_jobs
from papi.core import metrics as core_metrics
from papi.core.schema import (
    analysis,
    citation,
    claim,
    disclosure,
    document,
    document_span,
    event,
    event_log,
    feedback,
    fetch_log,
    forecast,
    identifier,
    index_membership,
    instrument,
    issuer,
    job,
    llm_usage_ledger,
    metric,
    outcome,
    price_bar,
    raw_object,
    source,
    summary,
    txn,
    watchlist,
)
from papi.core.state import get_state
from papi.core.text import normalize_tr, parse_tr_number, tr_upper
from papi.core.timeutil import IST, ist_date, now_utc, to_utc
from papi.web import integrations

KAP_URL = "https://www.kap.org.tr/tr/Bildirim/{ref}"
PRIORITIES = ("P1", "P2", "P3", "P4")
SCOPES = ("mine", "holdings", "watchlist", "all")
FEEDBACK_KINDS = {
    "wrong_fact": "Yanlış bilgi",
    "wrong_type": "Yanlış tür",
    "not_material": "Önemsiz",
    "helpful": "Faydalı",
}
REVISION_LABELS = {"correction": "Düzeltme", "update": "Güncelleme", "blocked": "Engellendi"}
CATCHUP_LATENCY_S = 600.0
STREAM_TYPES = (
    "event.classified.v1",
    "analysis.completed.v1",
    "forecast.created.v1",
    "disclosure.revised.v1",
)


def kap_url(source_ref: str | None) -> str | None:
    return KAP_URL.format(ref=source_ref) if source_ref else None


# ------------------------------------------------------------------------------------------ identity


def tickers_for(conn: Connection, instrument_ids: list[str] | set[str]) -> dict[str, str]:
    """Current ``TICKER`` per instrument (open validity preferred, else the latest one)."""
    ids = [i for i in instrument_ids if i]
    if not ids:
        return {}
    rows = conn.execute(
        select(identifier.c.entity_id, identifier.c.value, identifier.c.valid_from, identifier.c.valid_to)
        .where(
            identifier.c.entity_type == "instrument",
            identifier.c.scheme == "TICKER",
            identifier.c.entity_id.in_(ids),
        )
        .order_by(identifier.c.valid_from)
    ).all()
    out: dict[str, tuple[bool, date, str]] = {}
    for r in rows:
        key = (r.valid_to is None, r.valid_from, r.value)
        if r.entity_id not in out or key > out[r.entity_id]:
            out[r.entity_id] = key
    return {k: v[2] for k, v in out.items()}


def instruments_for_ticker(conn: Connection, ticker: str) -> list[str]:
    """Instrument ids that ever carried ``ticker`` (current holder first)."""
    rows = conn.execute(
        select(identifier.c.entity_id, identifier.c.valid_to, identifier.c.valid_from)
        .where(
            identifier.c.entity_type == "instrument",
            identifier.c.scheme == "TICKER",
            identifier.c.value == tr_upper(ticker.strip()),
        )
        .order_by(identifier.c.valid_to.is_(None).desc(), identifier.c.valid_from.desc())
    ).all()
    seen: list[str] = []
    for r in rows:
        if r.entity_id not in seen:
            seen.append(r.entity_id)
    return seen


def issuer_names(conn: Connection, issuer_ids: list[str] | set[str]) -> dict[str, str]:
    ids = [i for i in issuer_ids if i]
    if not ids:
        return {}
    rows = conn.execute(
        select(issuer.c.id, issuer.c.short_name, issuer.c.legal_name).where(issuer.c.id.in_(ids))
    ).all()
    return {r.id: r.short_name or r.legal_name for r in rows}


# ------------------------------------------------------------------------------------------ scope


@dataclass
class Scope:
    """What 'mine' means right now: held instruments, watched instruments, positions."""

    holdings: set[str] = field(default_factory=set)
    watch: set[str] = field(default_factory=set)
    positions: list[Any] = field(default_factory=list)
    portfolio_available: bool = True

    @property
    def mine(self) -> set[str]:
        return self.holdings | self.watch

    def badge(self, instrument_id: str) -> str | None:
        if instrument_id in self.holdings:
            return "HOLDING"
        if instrument_id in self.watch:
            return "WATCH"
        return None


def watch_ids(conn: Connection) -> set[str]:
    """Instruments on any watchlist, directly or via a watched sector / index (current membership)."""
    rows = conn.execute(select(watchlist.c.instrument_ids, watchlist.c.sectors, watchlist.c.indices)).all()
    ids: set[str] = set()
    sectors: set[str] = set()
    indices: set[str] = set()
    for r in rows:
        ids.update(r.instrument_ids or [])
        sectors.update(r.sectors or [])
        indices.update(r.indices or [])
    if sectors:
        ids.update(
            conn.execute(
                select(instrument.c.id)
                .join(issuer, issuer.c.id == instrument.c.issuer_id)
                .where(issuer.c.sector_code.in_(sorted(sectors)))
            ).scalars()
        )
    if indices:
        today = ist_date()
        ids.update(
            conn.execute(
                select(index_membership.c.instrument_id).where(
                    index_membership.c.index_code.in_(sorted(indices)),
                    index_membership.c.valid_from <= today,
                    or_(index_membership.c.valid_to.is_(None), index_membership.c.valid_to >= today),
                )
            ).scalars()
        )
    return ids


def load_scope(conn: Connection) -> Scope:
    pos = integrations.positions(conn)
    if pos is None:
        return Scope(watch=watch_ids(conn), portfolio_available=False)
    held = [p for p in pos if p.quantity and p.quantity > 0]
    return Scope(holdings={p.instrument_id for p in held}, watch=watch_ids(conn), positions=held)


# ------------------------------------------------------------------------------------------ feed


@dataclass
class FeedFilters:
    scope: str = "mine"
    ticker: str | None = None
    family: str | None = None
    code: str | None = None
    materiality_gte: int | None = None
    priority: str | None = None  # P1..P4 → that priority or more urgent
    hide_routine: bool = False
    date_from: date | None = None
    date_to: date | None = None
    as_of: datetime | None = None  # point-in-time: only events papi knew by then
    limit: int = 100

    def query_string(self) -> str:
        from urllib.parse import urlencode

        params: dict[str, Any] = {}
        for key in (
            "scope",
            "ticker",
            "family",
            "code",
            "materiality_gte",
            "priority",
            "date_from",
            "date_to",
        ):
            val = getattr(self, key)
            if val not in (None, ""):
                params[key] = val
        if self.hide_routine:
            params["hide_routine"] = "1"
        return urlencode(params)


def _instrument_overlap(ids: set[str]) -> Any:
    if not ids:
        return false()
    je = func.json_each(event.c.instrument_ids).table_valued("value").alias("je")
    return exists(select(literal(1)).select_from(je).where(je.c.value.in_(sorted(ids))))


def _event_select() -> Any:
    """Events outer-joined with their primary disclosure (columns used by feed items)."""
    sort_ts = func.coalesce(event.c.announced_at, event.c.created_at)
    return (
        select(
            event,
            disclosure.c.source_ref,
            disclosure.c.published_at,
            disclosure.c.first_seen_at,
            disclosure.c.revision_kind,
            disclosure.c.revision_of,
            disclosure.c.is_late,
            disclosure.c.title.label("disclosure_title"),
            disclosure.c.extra.label("disclosure_extra"),
            disclosure.c.filer_issuer_id,
        )
        .select_from(event.outerjoin(disclosure, disclosure.c.id == event.c.primary_disclosure_id))
        .order_by(sort_ts.desc(), event.c.id.desc())
    )


def _feed_stmt(conn: Connection, f: FeedFilters, scope: Scope) -> Any:
    sort_ts = func.coalesce(event.c.announced_at, event.c.created_at)
    stmt = _event_select().where(event.c.superseded_by.is_(None))
    if f.scope == "holdings":
        stmt = stmt.where(_instrument_overlap(scope.holdings))
    elif f.scope == "watchlist":
        stmt = stmt.where(_instrument_overlap(scope.watch))
    elif f.scope == "mine":
        stmt = stmt.where(or_(event.c.priority.in_(("P1", "P2", "P3")), _instrument_overlap(scope.mine)))
    if f.ticker:
        stmt = stmt.where(_instrument_overlap(set(instruments_for_ticker(conn, f.ticker))))
    if f.family:
        codes = integrations.family_codes(f.family)
        fam_cond = or_(event.c.code == f.family, event.c.code.like(f"{f.family}.%"))
        stmt = stmt.where(or_(fam_cond, event.c.code.in_(codes)) if codes else fam_cond)
    if f.code:
        stmt = stmt.where(event.c.code == f.code)
    if f.materiality_gte is not None:
        stmt = stmt.where(event.c.materiality >= f.materiality_gte)
    if f.priority in PRIORITIES:
        allowed = PRIORITIES[: PRIORITIES.index(f.priority) + 1]
        stmt = stmt.where(event.c.priority.in_(allowed))
    if f.hide_routine:
        stmt = stmt.where(or_(event.c.priority.is_(None), event.c.priority != "P4"))
    if f.date_from:
        stmt = stmt.where(sort_ts >= to_utc(datetime.combine(f.date_from, time.min), assume_tz=IST))
    if f.date_to:
        end = datetime.combine(f.date_to + timedelta(days=1), time.min)
        stmt = stmt.where(sort_ts < to_utc(end, assume_tz=IST))
    if f.as_of:
        stmt = stmt.where(event.c.created_at <= f.as_of)
    return stmt


def _latency_s(published: datetime | None, first_seen: datetime | None) -> float | None:
    if published is None or first_seen is None:
        return None
    return max((first_seen - published).total_seconds(), 0.0)


def feed_items(
    conn: Connection, f: FeedFilters, scope: Scope, *, event_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    stmt = _feed_stmt(conn, f, scope)
    if event_ids is not None:
        stmt = stmt.where(event.c.id.in_(event_ids))
    rows = conn.execute(stmt.limit(max(1, min(f.limit, 500)))).all()
    return _decorate(conn, rows, scope)


def _decorate(conn: Connection, rows: list[Any], scope: Scope) -> list[dict[str, Any]]:
    ids = [r.id for r in rows]
    instr_ids: set[str] = set()
    iss_ids: set[str] = set()
    for r in rows:
        instr_ids.update(r.instrument_ids or [])
        iss_ids.update(r.issuer_ids or [])
        if r.filer_issuer_id:
            iss_ids.add(r.filer_issuer_id)
    tick = tickers_for(conn, instr_ids)
    names = issuer_names(conn, iss_ids)
    headlines: dict[str, dict[str, str | None]] = defaultdict(dict)
    analysed: set[str] = set()
    if ids:
        for s in conn.execute(
            select(summary.c.event_id, summary.c.lang, summary.c.headline).where(summary.c.event_id.in_(ids))
        ):
            headlines[s.event_id][s.lang] = s.headline
        analysed = set(
            conn.execute(select(analysis.c.event_id).where(analysis.c.event_id.in_(ids)).distinct()).scalars()
        )
    items = []
    for r in rows:
        extra = r.extra or {}
        dextra = r.disclosure_extra or {}
        tickers = []
        for iid in r.instrument_ids or []:
            t = tick.get(iid)
            tickers.append({"instrument_id": iid, "ticker": t or "?", "badge": scope.badge(iid)})
        if not tickers:
            tickers = [
                {"instrument_id": None, "ticker": t, "badge": None} for t in extra.get("tickers") or []
            ]
        issuer_ids = list(r.issuer_ids or []) or ([r.filer_issuer_id] if r.filer_issuer_id else [])
        latency = _latency_s(r.published_at, r.first_seen_at)
        catchup = bool(dextra.get("is_catchup")) or (latency is not None and latency > CATCHUP_LATENCY_S)
        headline = headlines[r.id].get("tr") or extra.get("headline_tr") or r.disclosure_title or r.code or ""
        items.append(
            {
                "id": r.id,
                "code": r.code,
                "code_label": integrations.code_label(r.code),
                "family": integrations.family_of(r.code),
                "materiality": r.materiality,
                "materiality_band": r.materiality_band,
                "priority": r.priority,
                "status": r.status,
                "stage": "analysed" if r.id in analysed else "rules",
                "announced_at": r.announced_at or r.published_at or r.created_at,
                "published_at": r.published_at,
                "first_seen_at": r.first_seen_at,
                "latency_s": latency,
                "catchup": catchup,
                "is_late": bool(r.is_late),
                "revision_kind": r.revision_kind
                if r.revision_kind and r.revision_kind != "original"
                else None,
                "revision_label": REVISION_LABELS.get(r.revision_kind or ""),
                "headline": headline,
                "headline_en": headlines[r.id].get("en"),
                "tickers": tickers,
                "holding": any(t["badge"] == "HOLDING" for t in tickers),
                "watch": any(t["badge"] == "WATCH" for t in tickers),
                "issuers": [{"id": i, "name": names.get(i, "")} for i in issuer_ids],
                "disclosure_id": r.primary_disclosure_id,
                "source_ref": r.source_ref,
                "kap_url": kap_url(r.source_ref),
                "payload": r.payload,
            }
        )
    return items


def group_bursts(items: list[dict[str, Any]], *, window_s: float = 300.0, min_size: int = 5) -> list[dict]:
    """Collapse runs of ≥ ``min_size`` same-code items within ``window_s`` into one burst group."""
    groups: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if len(run) >= min_size:
            groups.append(
                {"burst": True, "code": run[0]["code"], "label": run[0]["code_label"], "items": run[:]}
            )
        else:
            groups.extend({"burst": False, "item": it} for it in run)
        run.clear()

    for it in items:
        if (
            run
            and it["code"] == run[0]["code"]
            and abs((run[0]["announced_at"] - it["announced_at"]).total_seconds()) <= window_s
        ):
            run.append(it)
            continue
        flush()
        run.append(it)
    flush()
    return groups


def feed_families(conn: Connection) -> list[tuple[str, str]]:
    fams = integrations.families()
    if fams is not None:
        return fams
    codes = conn.execute(select(event.c.code).distinct()).scalars()
    return [(f, f) for f in sorted({integrations.family_of(c) for c in codes})]


# ------------------------------------------------------------------------------------------ event detail


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for k, v in value.items():
            out.extend(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list) and any(isinstance(v, dict | list) for v in value):
        out = []
        for i, v in enumerate(value):
            out.extend(_flatten(v, f"{prefix}.{i}" if prefix else str(i)))
        return out
    return [(prefix, value)]


def _norm_value(value: Any) -> str:
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    try:
        return str(parse_tr_number(text).normalize())
    except (ValueError, InvalidOperation, ArithmeticError):
        return normalize_tr(text)


def _documents(conn: Connection, doc_ids: list[str]) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    rows = {r.id: r for r in conn.execute(select(document).where(document.c.id.in_(doc_ids)))}
    spans: dict[str, list[Any]] = defaultdict(list)
    for s in conn.execute(
        select(document_span)
        .where(document_span.c.document_id.in_(list(rows)))
        .order_by(document_span.c.document_id, document_span.c.ordinal)
    ):
        spans[s.document_id].append(s)
    shas = [r.raw_sha256 for r in rows.values() if r.raw_sha256]
    raws = (
        {r.sha256: r for r in conn.execute(select(raw_object).where(raw_object.c.sha256.in_(shas)))}
        if shas
        else {}
    )
    docs = []
    for idx, did in enumerate(d for d in doc_ids if d in rows):
        d = rows[did]
        raw = raws.get(d.raw_sha256) if d.raw_sha256 else None
        docs.append(
            {
                "index": idx,
                "id": d.id,
                "kind": d.kind,
                "mime": d.mime or (raw.content_type if raw else None),
                "text": d.text or "",
                "structure": d.structure or {},
                "raw_sha256": d.raw_sha256,
                "raw_url": raw.url if raw else None,
                "byte_size": raw.byte_size if raw else None,
                "filename": (d.extra or {}).get("filename") or (d.extra or {}).get("name"),
                "spans": [
                    {
                        "id": s.id,
                        "ordinal": s.ordinal,
                        "anchor": f"d{idx}s{s.ordinal}",
                        "char_start": s.char_start,
                        "char_end": s.char_end,
                        "page": s.page,
                        "field_path": s.field_path,
                        "text": s.text,
                    }
                    for s in spans.get(d.id, [])
                ],
            }
        )
    return docs


def _revision_chain(conn: Connection, disclosure_id: str | None) -> list[dict[str, Any]]:
    """Every disclosure linked by ``revision_of`` to this one (ancestors and descendants), oldest first."""
    if not disclosure_id:
        return []
    root = disclosure_id
    seen = {root}
    while True:
        parent = conn.execute(select(disclosure.c.revision_of).where(disclosure.c.id == root)).scalar()
        if not parent or parent in seen:
            break
        seen.add(parent)
        root = parent
    chain_ids = [root]
    frontier = [root]
    while frontier:
        children = (
            conn.execute(select(disclosure.c.id).where(disclosure.c.revision_of.in_(frontier)))
            .scalars()
            .all()
        )
        frontier = [c for c in children if c not in chain_ids]
        chain_ids.extend(frontier)
    if len(chain_ids) == 1:
        return []
    rows = conn.execute(
        select(
            disclosure.c.id,
            disclosure.c.source_ref,
            disclosure.c.revision_kind,
            disclosure.c.published_at,
            disclosure.c.title,
        )
        .where(disclosure.c.id.in_(chain_ids))
        .order_by(disclosure.c.published_at, disclosure.c.id)
    ).all()
    ev_by_disc = {
        r.primary_disclosure_id: r.id
        for r in conn.execute(
            select(event.c.id, event.c.primary_disclosure_id).where(
                event.c.primary_disclosure_id.in_(chain_ids)
            )
        )
    }
    return [
        {
            "id": r.id,
            "source_ref": r.source_ref,
            "kap_url": kap_url(r.source_ref),
            "revision_kind": r.revision_kind,
            "revision_label": REVISION_LABELS.get(r.revision_kind, "Asıl"),
            "published_at": r.published_at,
            "title": r.title,
            "event_id": ev_by_disc.get(r.id),
            "current": r.id == disclosure_id,
        }
        for r in rows
    ]


def _analyses(conn: Connection, event_id: str, span_anchor: dict[str, str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(analysis).where(analysis.c.event_id == event_id).order_by(analysis.c.created_at.desc())
    ).all()
    if not rows:
        return []
    claims_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    crow = conn.execute(select(claim).where(claim.c.analysis_id.in_([r.id for r in rows]))).all()
    cits: dict[str, list[Any]] = defaultdict(list)
    if crow:
        for c in conn.execute(select(citation).where(citation.c.claim_id.in_([c.id for c in crow]))):
            cits[c.claim_id].append(c)
    for c in crow:
        claims_by[c.analysis_id].append(
            {
                "id": c.id,
                "kind": c.kind,
                "text_tr": c.text_tr,
                "text_en": c.text_en,
                "value": c.value,
                "unit": c.unit,
                "check_status": c.check_status,
                "verify_status": c.verify_status,
                "citations": [
                    {
                        "target_type": ct.target_type,
                        "target_id": ct.target_id,
                        "quote": ct.quote,
                        "anchor": span_anchor.get(ct.target_id)
                        if ct.target_type == "document_span"
                        else None,
                    }
                    for ct in cits.get(c.id, [])
                ],
            }
        )
    return [
        {
            "id": r.id,
            "agent": r.agent,
            "backend": r.backend,
            "model_ids": r.model_ids or [],
            "prompt_version": r.prompt_version,
            "created_at": r.created_at,
            "review_state": r.review_state,
            "confidence": r.confidence,
            "latency_ms": r.latency_ms,
            "cost_usd_est": r.cost_usd_est,
            "output": r.output,
            "claims": claims_by.get(r.id, []),
        }
        for r in rows
    ]


def _key_facts(ev: Any, docs: list[dict[str, Any]], analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ev.payload:
        return []
    body = docs[0] if docs else None
    by_ordinal = {s["ordinal"]: s["anchor"] for s in body["spans"]} if body else {}
    form_by_value: dict[str, list[int]] = defaultdict(list)
    if body:
        for ff in body["structure"].get("form_fields") or []:
            if ff.get("span_ordinal") is not None and ff.get("value") not in (None, ""):
                form_by_value[_norm_value(ff["value"])].append(ff["span_ordinal"])
    claim_by_value: dict[str, list[str]] = defaultdict(list)
    for a in analyses:
        for c in a["claims"]:
            if c["value"] is None:
                continue
            anchors = [ct["anchor"] for ct in c["citations"] if ct["anchor"]]
            claim_by_value[_norm_value(c["value"])].extend(anchors)
    explicit: dict[str, list[int]] = (ev.extra or {}).get("payload_spans") or {}
    facts = []
    for path, value in _flatten(ev.payload):
        anchors: list[str] = []
        provenance = None
        if path in explicit:
            anchors = [by_ordinal[o] for o in explicit[path] if o in by_ordinal]
            provenance = "payload_spans" if anchors else None
        if not anchors and value not in (None, ""):
            key = _norm_value(value)
            anchors = [by_ordinal[o] for o in form_by_value.get(key, []) if o in by_ordinal]
            provenance = "form_field" if anchors else None
            if not anchors:
                anchors = list(dict.fromkeys(claim_by_value.get(key, [])))
                provenance = "claim" if anchors else None
        facts.append({"path": path, "value": value, "anchors": anchors, "provenance": provenance})
    return facts


def _for_me(conn: Connection, ev: Any, scope: Scope, tick: dict[str, str]) -> dict[str, Any]:
    instr = list(ev.instrument_ids or [])
    impacts = [
        {
            "forecast_id": f.id,
            "instrument_id": f.instrument_id,
            "ticker": tick.get(f.instrument_id, "?"),
            "horizon": f.horizon,
            "target": f.target,
            "model": f"{f.model} {f.model_version}",
            "distribution": f.distribution,
            "position_impact": f.position_impact,
        }
        for f in conn.execute(
            select(forecast)
            .where(forecast.c.event_id == ev.id, forecast.c.position_impact.is_not(None))
            .order_by(forecast.c.horizon)
        )
    ]
    held = [p for p in scope.positions if p.instrument_id in instr]
    pos = [
        {
            "ticker": p.ticker,
            "quantity": p.quantity,
            "avg_cost": p.avg_cost,
            "last_close": p.last_close,
            "last_close_date": p.last_close_date,
            "market_value": p.market_value,
            "weight": p.weight,
        }
        for p in held
    ]
    reason = None
    if not scope.portfolio_available and not impacts:
        reason = "Portföy modülü yüklü değil; pozisyon etkisi hesaplanamıyor."
    elif not held and not impacts:
        reason = "Bu bildirim portföyünüzdeki bir hisseyi ilgilendirmiyor."
    elif not impacts:
        if any(p.last_close is None for p in held):
            reason = "Fiyat verisi yok; değer etkisi hesaplanamıyor."
        else:
            reason = "Bu olay için henüz senaryo / pozisyon etkisi hesaplanmadı."
    return {"holding": bool(held), "positions": pos, "impacts": impacts, "reason": reason}


def event_detail(conn: Connection, event_id: str, scope: Scope) -> dict[str, Any] | None:
    ev = conn.execute(select(event).where(event.c.id == event_id)).first()
    if ev is None:
        return None
    rows = conn.execute(_event_select().where(event.c.id == event_id)).all()
    item = _decorate(conn, rows, scope)[0]
    disc_ids = list(ev.disclosure_ids or [])
    discs = {r.id: r for r in conn.execute(select(disclosure).where(disclosure.c.id.in_(disc_ids)))}
    primary = discs.get(ev.primary_disclosure_id) if ev.primary_disclosure_id else None
    doc_ids: list[str] = []
    if primary is not None:
        if primary.body_document_id:
            doc_ids.append(primary.body_document_id)
        doc_ids.extend(primary.attachment_document_ids or [])
        doc_ids.extend(
            d
            for d in conn.execute(
                select(document.c.id).where(document.c.disclosure_id == primary.id).order_by(document.c.id)
            ).scalars()
            if d not in doc_ids
        )
    docs = _documents(conn, doc_ids)
    span_anchor = {s["id"]: s["anchor"] for d in docs for s in d["spans"]}
    analyses = _analyses(conn, event_id, span_anchor)
    summaries = {
        s.lang: {"headline": s.headline, "body": s.body, "created_at": s.created_at}
        for s in conn.execute(select(summary).where(summary.c.event_id == event_id))
    }
    tick = tickers_for(conn, ev.instrument_ids or [])
    superseded = conn.execute(
        select(event.c.id, event.c.code, event.c.created_at).where(event.c.superseded_by == event_id)
    ).all()
    fb = conn.execute(
        select(feedback.c.kind, func.count())
        .where(feedback.c.target_type == "event", feedback.c.target_id == event_id)
        .group_by(feedback.c.kind)
    ).all()
    frows = conn.execute(
        select(forecast, outcome.c.realised_value, outcome.c.scores, outcome.c.id.label("outcome_id"))
        .select_from(forecast.outerjoin(outcome, outcome.c.forecast_id == forecast.c.id))
        .where(forecast.c.event_id == event_id)
        .order_by(forecast.c.horizon)
    ).all()
    forecasts = [
        {
            "id": f.id,
            "ticker": tick.get(f.instrument_id, "?"),
            "horizon": f.horizon,
            "target": f.target,
            "model": f"{f.model} {f.model_version}",
            "distribution": f.distribution,
            "scenarios": f.scenarios,
            "outcome": {"realised_value": f.realised_value, "scores": f.scores} if f.outcome_id else None,
        }
        for f in frows
    ]
    return {
        "item": item,
        "event": {
            "id": ev.id,
            "code": ev.code,
            "taxonomy_version": ev.taxonomy_version,
            "status": ev.status,
            "superseded_by": ev.superseded_by,
            "payload": ev.payload,
            "payload_source": ev.payload_source,
            "payload_schema": ev.payload_schema,
            "rules": (ev.extra or {}).get("rules"),
            "created_at": ev.created_at,
        },
        "disclosures": [
            {
                "id": d.id,
                "source_ref": d.source_ref,
                "kap_url": kap_url(d.source_ref),
                "title": d.title,
                "subject": d.source_subject,
                "published_at": d.published_at,
                "first_seen_at": d.first_seen_at,
                "revision_kind": d.revision_kind,
                "is_late": d.is_late,
            }
            for d in (discs[i] for i in disc_ids if i in discs)
        ],
        "revision_chain": _revision_chain(conn, ev.primary_disclosure_id),
        "superseded": [{"id": s.id, "code": s.code, "created_at": s.created_at} for s in superseded],
        "summaries": summaries,
        "analyses": analyses,
        "key_facts": _key_facts(ev, docs, analyses),
        "documents": docs,
        "forecasts": forecasts,
        "for_me": _for_me(conn, ev, scope, tick),
        "feedback_counts": {k: n for k, n in fb},
    }


# ------------------------------------------------------------------------------------------ portfolio


def recent_txns(
    conn: Connection, *, instrument_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = select(txn).order_by(txn.c.trade_date.desc(), txn.c.created_at.desc()).limit(limit)
    if instrument_id:
        stmt = stmt.where(txn.c.instrument_id == instrument_id)
    rows = conn.execute(stmt).all()
    tick = tickers_for(conn, {r.instrument_id for r in rows if r.instrument_id})
    reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
    return [
        {
            "id": r.id,
            "instrument_id": r.instrument_id,
            "ticker": tick.get(r.instrument_id) if r.instrument_id else None,
            "type": r.type,
            "trade_date": r.trade_date,
            "quantity": r.quantity,
            "price": r.price,
            "fees": r.fees,
            "tax": r.tax,
            "currency": r.currency,
            "source": r.source,
            "note": r.note,
            "reverses_id": r.reverses_id,
            "reversed": r.id in reversed_ids,
        }
        for r in rows
    ]


def position_row(p: Any) -> dict[str, Any]:
    pnl = p.market_value - p.cost_total if p.market_value is not None else None
    pnl_pct = float(pnl / p.cost_total * 100) if pnl is not None and p.cost_total else None
    return {
        "instrument_id": p.instrument_id,
        "ticker": p.ticker,
        "quantity": p.quantity,
        "avg_cost": p.avg_cost,
        "cost_total": p.cost_total,
        "last_close": p.last_close,
        "last_close_date": p.last_close_date,
        "market_value": p.market_value,
        "weight": p.weight,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    }


def portfolio_view(conn: Connection, scope: Scope) -> dict[str, Any]:
    rows = [position_row(p) for p in scope.positions]
    rows.sort(key=lambda r: r["market_value"] or Decimal(0), reverse=True)
    valued = [r for r in rows if r["market_value"] is not None]
    total_value = sum((r["market_value"] for r in valued), Decimal(0))
    total_cost = sum((r["cost_total"] for r in valued), Decimal(0))
    pending = integrations.pending_actions(conn) if scope.portfolio_available else None
    timeline = feed_items(conn, FeedFilters(scope="holdings", limit=50), scope) if scope.holdings else []
    last_event: dict[str, dict[str, Any]] = {}
    for it in timeline:
        for t in it["tickers"]:
            if t["instrument_id"] and t["instrument_id"] not in last_event:
                last_event[t["instrument_id"]] = it
    for r in rows:
        r["last_event"] = last_event.get(r["instrument_id"])
    return {
        "available": scope.portfolio_available,
        "positions": rows,
        "total_value": total_value if valued else None,
        "total_cost": total_cost if valued else None,
        "total_pnl": total_value - total_cost if valued else None,
        "unvalued": [r["ticker"] for r in rows if r["market_value"] is None],
        "pending": pending,
        "txns": recent_txns(conn, limit=30),
        "timeline": timeline,
    }


# ------------------------------------------------------------------------------------------ company / issuer


def price_series(conn: Connection, instrument_id: str, primary_source: str) -> list[tuple[date, float]]:
    """Daily closes; the configured primary source wins where several sources have a bar."""
    rows = conn.execute(
        select(price_bar.c.ts, price_bar.c.close, source.c.code)
        .join(source, source.c.id == price_bar.c.source_id)
        .where(
            price_bar.c.instrument_id == instrument_id,
            price_bar.c.interval == "1d",
            price_bar.c.close.is_not(None),
        )
        .order_by(price_bar.c.ts)
    ).all()
    by_day: dict[date, tuple[bool, float]] = {}
    for r in rows:
        cand = (r.code == primary_source, r.close)
        if r.ts not in by_day or (cand[0] and not by_day[r.ts][0]):
            by_day[r.ts] = cand
    return [(d, v[1]) for d, v in sorted(by_day.items())]


def price_chart(series: list[tuple[date, float]], txns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """uPlot data: x (unix s), close, buy/sell marker series aligned to the next bar on/after trade date."""
    if not series:
        return None
    import bisect

    days = [d for d, _ in series]
    xs = [int(datetime.combine(d, time(15, 0), tzinfo=IST).timestamp()) for d in days]
    close = [v for _, v in series]
    buy: list[float | None] = [None] * len(days)
    sell: list[float | None] = [None] * len(days)
    for t in txns:
        if t["reverses_id"] or t["reversed"] or t["type"] not in ("buy", "sell", "opening_balance"):
            continue
        i = bisect.bisect_left(days, t["trade_date"])
        if i >= len(days):
            continue
        value = float(t["price"]) if t["price"] is not None else close[i]
        (sell if t["type"] == "sell" else buy)[i] = value
    return {"x": xs, "close": close, "buy": buy, "sell": sell}


def _issuer_disclosures(conn: Connection, issuer_id: str, limit: int = 100) -> list[dict[str, Any]]:
    je = func.json_each(disclosure.c.related_issuer_ids).table_valued("value").alias("rel")
    rows = conn.execute(
        select(disclosure)
        .where(
            or_(
                disclosure.c.filer_issuer_id == issuer_id,
                exists(select(literal(1)).select_from(je).where(je.c.value == issuer_id)),
            )
        )
        .order_by(disclosure.c.published_at.desc(), disclosure.c.id.desc())
        .limit(limit)
    ).all()
    ev_rows = conn.execute(
        select(event.c.id, event.c.primary_disclosure_id, event.c.code, event.c.materiality).where(
            event.c.primary_disclosure_id.in_([r.id for r in rows]), event.c.superseded_by.is_(None)
        )
    ).all()
    evs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ev_rows:
        evs[e.primary_disclosure_id].append(
            {
                "id": e.id,
                "code": e.code,
                "label": integrations.code_label(e.code),
                "materiality": e.materiality,
            }
        )
    return [
        {
            "id": r.id,
            "source_ref": r.source_ref,
            "kap_url": kap_url(r.source_ref),
            "published_at": r.published_at,
            "first_seen_at": r.first_seen_at,
            "title": r.title,
            "subject": r.source_subject,
            "revision_kind": r.revision_kind,
            "revision_label": REVISION_LABELS.get(r.revision_kind),
            "is_late": r.is_late,
            "ingest_status": r.ingest_status,
            "events": evs.get(r.id, []),
        }
        for r in rows
    ]


def _identifiers(conn: Connection, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    conds = [and_(identifier.c.entity_type == t, identifier.c.entity_id == i) for t, i in pairs if i]
    if not conds:
        return []
    rows = conn.execute(
        select(identifier).where(or_(*conds)).order_by(identifier.c.scheme, identifier.c.valid_from)
    ).all()
    return [
        {
            "entity_type": r.entity_type,
            "scheme": r.scheme,
            "value": r.value,
            "valid_from": None if r.valid_from <= date(1900, 1, 1) else r.valid_from,
            "valid_to": r.valid_to,
        }
        for r in rows
    ]


def issuer_detail(conn: Connection, issuer_id: str, scope: Scope) -> dict[str, Any] | None:
    iss = conn.execute(select(issuer).where(issuer.c.id == issuer_id)).first()
    if iss is None:
        return None
    instruments = conn.execute(select(instrument).where(instrument.c.issuer_id == issuer_id)).all()
    tick = tickers_for(conn, [i.id for i in instruments])
    je = func.json_each(event.c.issuer_ids).table_valued("value").alias("iss")
    ev_rows = conn.execute(
        _event_select()
        .where(event.c.superseded_by.is_(None))
        .where(exists(select(literal(1)).select_from(je).where(je.c.value == issuer_id)))
        .limit(50)
    ).all()
    return {
        "id": iss.id,
        "legal_name": iss.legal_name,
        "short_name": iss.short_name,
        "sector_code": iss.sector_code,
        "status": iss.status,
        "kind": iss.kind,
        "instruments": [
            {"id": i.id, "type": i.type, "currency": i.currency, "status": i.status, "ticker": tick.get(i.id)}
            for i in instruments
        ],
        "identifiers": _identifiers(conn, [("issuer", iss.id)] + [("instrument", i.id) for i in instruments]),
        "recent_events": _decorate(conn, ev_rows, scope),
    }


def company_view(conn: Connection, ticker: str, scope: Scope, primary_source: str) -> dict[str, Any] | None:
    ids = instruments_for_ticker(conn, ticker)
    if not ids:
        return None
    inst = conn.execute(select(instrument).where(instrument.c.id == ids[0])).first()
    if inst is None:
        return None
    iss = (
        conn.execute(select(issuer).where(issuer.c.id == inst.issuer_id)).first() if inst.issuer_id else None
    )
    today = ist_date()
    indices = (
        conn.execute(
            select(index_membership.c.index_code)
            .where(
                index_membership.c.instrument_id == inst.id,
                index_membership.c.valid_from <= today,
                or_(index_membership.c.valid_to.is_(None), index_membership.c.valid_to >= today),
            )
            .order_by(index_membership.c.index_code)
        )
        .scalars()
        .all()
    )
    txns = recent_txns(conn, instrument_id=inst.id, limit=500)
    series = price_series(conn, inst.id, primary_source)
    pos = next((p for p in scope.positions if p.instrument_id == inst.id), None)
    return {
        "ticker": tr_upper(ticker.strip()),
        "instrument": {"id": inst.id, "type": inst.type, "currency": inst.currency, "status": inst.status},
        "issuer": (
            {
                "id": iss.id,
                "legal_name": iss.legal_name,
                "short_name": iss.short_name,
                "sector_code": iss.sector_code,
                "status": iss.status,
            }
            if iss
            else None
        ),
        "badge": scope.badge(inst.id),
        "position": position_row(pos) if pos else None,
        "indices": indices,
        "identifiers": _identifiers(conn, [("instrument", inst.id), ("issuer", iss.id if iss else "")]),
        "disclosures": _issuer_disclosures(conn, iss.id) if iss else [],
        "events": feed_items(conn, FeedFilters(scope="all", ticker=ticker, limit=50), scope),
        "txns": txns,
        "last_close": series[-1] if series else None,
        "chart": price_chart(series, txns),
    }


def disclosure_view(conn: Connection, disclosure_id: str) -> dict[str, Any] | None:
    d = conn.execute(select(disclosure).where(disclosure.c.id == disclosure_id)).first()
    if d is None:
        return None
    doc_ids = ([d.body_document_id] if d.body_document_id else []) + list(d.attachment_document_ids or [])
    doc_ids += [
        x
        for x in conn.execute(select(document.c.id).where(document.c.disclosure_id == d.id)).scalars()
        if x not in doc_ids
    ]
    events = conn.execute(
        select(event.c.id, event.c.code, event.c.superseded_by).where(event.c.primary_disclosure_id == d.id)
    ).all()
    return {
        "id": d.id,
        "source_ref": d.source_ref,
        "source_uid": d.source_uid,
        "kap_url": kap_url(d.source_ref),
        "revision_of": d.revision_of,
        "revision_kind": d.revision_kind,
        "published_at": d.published_at,
        "first_seen_at": d.first_seen_at,
        "latency_s": _latency_s(d.published_at, d.first_seen_at),
        "source_class": d.source_class,
        "source_type": d.source_type,
        "source_category": d.source_category,
        "source_subject": d.source_subject,
        "title": d.title,
        "summary": d.summary,
        "filer_issuer_id": d.filer_issuer_id,
        "related_issuer_ids": d.related_issuer_ids or [],
        "is_late": d.is_late,
        "ingest_status": d.ingest_status,
        "ingest_error": d.ingest_error,
        "extra": d.extra,
        "documents": _documents(conn, doc_ids),
        "revisions": _revision_chain(conn, d.id),
        "events": [{"id": e.id, "code": e.code, "superseded_by": e.superseded_by} for e in events],
    }


# ------------------------------------------------------------------------------------------ ops


LATENCY_BINS: tuple[tuple[float, str], ...] = (
    (30, "≤30 sn"),
    (60, "30–60 sn"),
    (120, "1–2 dk"),
    (300, "2–5 dk"),
    (900, "5–15 dk"),
    (3600, "15–60 dk"),
    (float("inf"), ">1 sa"),
)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[k]


def latency_stats(conn: Connection, *, days: int = 7) -> dict[str, Any]:
    """KAP ingest latency from the ``kap.latency_s`` metric (live polls), else from disclosure rows."""
    since = now_utc() - timedelta(days=days)
    samples = core_metrics.series(conn, "kap.latency_s", since=since, limit=5000)
    live = [s for s in samples if not (s["labels"] or {}).get("catchup")]
    origin = "metric kap.latency_s"
    values = [s["value"] for s in live]
    points = [(s["at"], s["value"]) for s in live]
    if not samples:
        origin = "disclosure (first_seen_at − published_at)"
        rows = conn.execute(
            select(disclosure.c.published_at, disclosure.c.first_seen_at, disclosure.c.extra).where(
                disclosure.c.first_seen_at >= since, disclosure.c.published_at.is_not(None)
            )
        ).all()
        points = [
            (r.first_seen_at, lat)
            for r in rows
            if not (r.extra or {}).get("is_catchup")
            and (lat := _latency_s(r.published_at, r.first_seen_at)) is not None
        ]
        values = [v for _, v in points]
    hist = []
    lo = 0.0
    for hi, label in LATENCY_BINS:
        hist.append({"label": label, "n": sum(1 for v in values if lo < v <= hi or (lo == 0 and v == 0))})
        lo = hi
    peak = max((h["n"] for h in hist), default=0)
    for h in hist:
        h["pct"] = round(100 * h["n"] / peak) if peak else 0
    points.sort()
    return {
        "origin": origin,
        "n": len(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
        "hist": hist,
        "chart": {"x": [int(t.timestamp()) for t, _ in points], "y": [v for _, v in points]}
        if points
        else None,
    }


def _metric_sum(conn: Connection, name: str, since: datetime) -> tuple[int, float]:
    row = conn.execute(
        select(func.count(), func.coalesce(func.sum(metric.c.value), 0.0)).where(
            metric.c.name == name, metric.c.at >= since
        )
    ).one()
    return int(row[0]), float(row[1])


def ops_view(conn: Connection) -> dict[str, Any]:
    now = now_utc()
    day = now - timedelta(hours=24)
    kap_source = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar()
    last_fetch = (
        conn.execute(
            select(fetch_log)
            .where(fetch_log.c.source_id == kap_source)
            .order_by(fetch_log.c.started_at.desc())
            .limit(1)
        ).first()
        if kap_source
        else None
    )
    counts = core_jobs.counts(conn)
    statuses = sorted({s for s, _ in counts})
    prios = sorted({p for _, p in counts})
    dead = conn.execute(
        select(job).where(job.c.status == "dead").order_by(job.c.updated_at.desc()).limit(50)
    ).all()
    failed = conn.execute(
        select(disclosure)
        .where(disclosure.c.ingest_status.like("failed:%"))
        .order_by(disclosure.c.first_seen_at.desc())
        .limit(50)
    ).all()
    unknown_kv = get_state(conn, "kap:unknown_templates") or {}
    unknown_rows = conn.execute(
        select(disclosure.c.id, disclosure.c.source_ref, disclosure.c.title, disclosure.c.first_seen_at)
        .where(func.json_extract(disclosure.c.extra, "$.template_unknown") == 1)
        .order_by(disclosure.c.first_seen_at.desc())
        .limit(20)
    ).all()
    gaps = get_state(conn, "kap:gaps") or {}
    gap_events = conn.execute(
        select(func.count()).where(event_log.c.type == "ingest.gap.detected.v1", event_log.c.time >= day)
    ).scalar()
    flagged = conn.execute(
        select(analysis.c.id, analysis.c.event_id, analysis.c.agent, analysis.c.created_at, event.c.code)
        .join(event, event.c.id == analysis.c.event_id)
        .where(analysis.c.review_state == "flagged")
        .order_by(analysis.c.created_at.desc())
        .limit(50)
    ).all()
    bad_claims = conn.execute(
        select(claim.c.id, claim.c.text_tr, claim.c.check_status, claim.c.verify_status, analysis.c.event_id)
        .join(analysis, analysis.c.id == claim.c.analysis_id)
        .where(or_(claim.c.check_status == "unsupported", claim.c.verify_status == "fail"))
        .order_by(analysis.c.created_at.desc())
        .limit(50)
    ).all()
    fb = conn.execute(select(feedback).order_by(feedback.c.created_at.desc()).limit(50)).all()
    polls_ok = _metric_sum(conn, "kap.poll.ok", day)
    polls_fail = _metric_sum(conn, "kap.poll.fail", day)
    return {
        "now": now,
        "last_poll_at": get_state(conn, "kap:last_poll_at"),
        "last_poll_ok_at": get_state(conn, "kap:last_poll_ok_at"),
        "circuit": get_state(conn, "kap:circuit"),
        "drift": get_state(conn, "kap:drift"),
        "last_fetch": (
            {
                "started_at": last_fetch.started_at,
                "outcome": last_fetch.outcome,
                "http_status": last_fetch.http_status,
                "url": last_fetch.url,
                "error": last_fetch.error,
            }
            if last_fetch
            else None
        ),
        "polls_24h": {"ok": polls_ok[0], "new_rows": int(polls_ok[1]), "fail": polls_fail[0]},
        "latency": latency_stats(conn),
        "gaps": {
            "open": list(gaps.get("open") or []),
            "dead": list(gaps.get("dead") or []),
            "tries": gaps.get("tries") or {},
            "events_24h": gap_events or 0,
        },
        "jobs": {
            "statuses": statuses,
            "priorities": prios,
            "counts": {f"{s}:{p}": n for (s, p), n in counts.items()},
            "totals": {s: sum(n for (st, _), n in counts.items() if st == s) for s in statuses},
        },
        "dead_jobs": [
            {
                "id": j.id,
                "kind": j.kind,
                "priority": j.priority,
                "attempts": j.attempts,
                "last_error": j.last_error,
                "updated_at": j.updated_at or j.finished_at or j.created_at,
            }
            for j in dead
        ],
        "failed_disclosures": [
            {
                "id": d.id,
                "source_ref": d.source_ref,
                "kap_url": kap_url(d.source_ref),
                "title": d.title,
                "ingest_status": d.ingest_status,
                "ingest_error": d.ingest_error,
                "first_seen_at": d.first_seen_at,
            }
            for d in failed
        ],
        "unknown_templates": [
            {"template_id": k, **(v if isinstance(v, dict) else {"count": v})}
            for k, v in sorted(unknown_kv.items(), key=lambda kv: -(kv[1] or {}).get("count", 0))
        ],
        "unknown_template_disclosures": [
            {
                "id": r.id,
                "source_ref": r.source_ref,
                "kap_url": kap_url(r.source_ref),
                "title": r.title,
                "first_seen_at": r.first_seen_at,
            }
            for r in unknown_rows
        ],
        "review": {
            "flagged": [
                {
                    "id": a.id,
                    "event_id": a.event_id,
                    "agent": a.agent,
                    "created_at": a.created_at,
                    "code": a.code,
                }
                for a in flagged
            ],
            "claims": [
                {
                    "id": c.id,
                    "text": c.text_tr,
                    "check_status": c.check_status,
                    "verify_status": c.verify_status,
                    "event_id": c.event_id,
                }
                for c in bad_claims
            ],
            "feedback": [
                {
                    "id": f.id,
                    "target_type": f.target_type,
                    "target_id": f.target_id,
                    "kind": f.kind,
                    "label": FEEDBACK_KINDS.get(f.kind, f.kind),
                    "comment": f.comment,
                    "created_at": f.created_at,
                }
                for f in fb
            ],
        },
    }


# ------------------------------------------------------------------------------------------ Claude usage


def _ledger_window(conn: Connection, since: datetime) -> dict[str, Any]:
    L = llm_usage_ledger
    rows = conn.execute(
        select(L.c.task, L.c.outcome, func.count(), func.coalesce(func.sum(L.c.cost_usd_est), 0.0))
        .where(L.c.at >= since)
        .group_by(L.c.task, L.c.outcome)
    ).all()
    by_task: dict[str, dict[str, Any]] = {}
    for task, outc, n, cost in rows:
        t = by_task.setdefault(task, {"task": task, "calls": 0, "cost": 0.0, "outcomes": {}})
        t["calls"] += n
        t["cost"] += float(cost)
        t["outcomes"][outc] = n
    chat_cost = conn.execute(
        select(func.coalesce(func.sum(L.c.cost_usd_est), 0.0)).where(L.c.at >= since, L.c.priority == "chat")
    ).scalar_one()
    total = sum(t["cost"] for t in by_task.values())
    return {
        "calls": sum(t["calls"] for t in by_task.values()),
        "cost": total,
        "chat_cost": float(chat_cost),
        "pipeline_cost": total - float(chat_cost),
        "by_task": sorted(by_task.values(), key=lambda t: -t["cost"]),
    }


def usage_view(conn: Connection) -> dict[str, Any]:
    L = llm_usage_ledger
    now = now_utc()
    week_ago = now - timedelta(days=7)
    hour_key = func.substr(L.c.at, 1, 13)
    hourly = conn.execute(
        select(hour_key, func.sum(L.c.cost_usd_est), func.count())
        .where(L.c.at >= week_ago)
        .group_by(hour_key)
        .order_by(hour_key)
    ).all()
    chart = None
    if hourly:
        xs, cost, calls = [], [], []
        for key, c, n in hourly:
            xs.append(int(datetime.fromisoformat(f"{key}:00:00+00:00").timestamp()))
            cost.append(float(c or 0.0))
            calls.append(int(n))
        chart = {"x": xs, "cost": cost, "calls": calls}
    recent = conn.execute(select(L).order_by(L.c.at.desc()).limit(100)).all()
    queued = conn.execute(
        select(job.c.priority, func.count()).where(job.c.status == "queued").group_by(job.c.priority)
    ).all()
    return {
        "governor": integrations.governor_status(conn),
        "window_5h": _ledger_window(conn, now - timedelta(hours=5)),
        "window_week": _ledger_window(conn, week_ago),
        "chart": chart,
        "recent": [
            {
                "id": r.id,
                "at": r.at,
                "task": r.task,
                "priority": r.priority,
                "model_ids": r.model_ids or [],
                "cost_usd_est": r.cost_usd_est,
                "duration_ms": r.duration_ms,
                "outcome": r.outcome,
                "error": r.error,
                "analysis_id": r.analysis_id,
            }
            for r in recent
        ],
        "queue": [{"priority": p, "n": n} for p, n in sorted(queued)],
    }


# ------------------------------------------------------------------------------------------ stream


def stream_rows(conn: Connection, since: datetime, *, limit: int = 200) -> list[Any]:
    """``event_log`` rows of the streamed types at or after ``since`` (time, id ascending)."""
    return conn.execute(
        select(event_log.c.id, event_log.c.type, event_log.c.subject, event_log.c.time, event_log.c.data)
        .where(event_log.c.type.in_(STREAM_TYPES), event_log.c.time >= since)
        .order_by(event_log.c.time, event_log.c.id)
        .limit(limit)
    ).all()


def event_log_time(conn: Connection, event_log_id: str) -> datetime | None:
    return conn.execute(select(event_log.c.time).where(event_log.c.id == event_log_id)).scalar()
