"""Analyst extraction suite (PRD 11 §3 "Analyst extraction", "Citations", "Summaries", "Cost").

Gold items live in ``tests/golden/analyst/*.yaml`` (one per file): a recorded KAP fixture, the event code,
a gold payload (dotted paths; ``null`` = not stated) and gold pack span ordinals per field. Items are
drafted by an LLM and carry ``reviewed: false`` until the owner confirms them (``papi eval review``);
gates use reviewed items only.

With ``--allow-claude`` each item is loaded into a scratch database exactly as production stores it
(parsed body + attachments, rules triage, the fixture KB), the analyst context pack is built with
``papi.analyst.context.build_context`` and sent through the governed ``papi.llm.call.call`` (owner's
database for the governor/ledger; task ``eval.analyst``, priority ``BACKLOG`` unless overridden, no
repair retry), then verified with ``papi.verify.verify`` like ``papi.analyst.pipeline.record_result``.

Scores per item:
- field accuracy — the event payload papi would store (rules values win, analyst values fill gaps after
  the verifier nulled untraceable fields) vs gold; numbers exact, currency/dates/enums exact, names by
  normalised containment, ``null`` gold must stay null;
- span correctness — gold fields with gold spans whose supporting spans (analyst ``payload_spans``, or
  the rules spans when the rules value was kept) intersect the gold spans;
- citation coverage — fact/computation claims the verifier passed (resolvable, supporting source);
- summary traceability — no headline/summary/rationale/watch-next sentence dropped by the verifier;
- verifier-clean — ``VerifyReport.ok``; average ``cost_usd_est`` per call.

Without ``--allow-claude`` the suite only checks the gold files themselves (``check_gold``).
"""

from __future__ import annotations

import copy
import gzip
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.schema import (
    OPEN_START,
    disclosure,
    document,
    document_span,
    event,
    identifier,
    instrument,
    issuer,
    source,
)
from papi.core.text import normalize_tr
from papi.core.timeutil import iso_utc, now_utc, parse_kap_datetime
from papi.evals.core import Gate, GoldenMissing, Metric, SuiteResult, golden_dir, ratio
from papi.evals.suites.parsers import fixture_dir
from papi.sources.kap.client import unwrap_java_bytes
from papi.sources.kap.parsing.doc import ParsedDocument
from papi.sources.kap.parsing.files import parse_file
from papi.sources.kap.parsing.html import parse_body
from papi.triage import taxonomy as tax_mod
from papi.verify import VerifyReport, flatten
from papi.verify.numbers import Pool, to_decimal

SUBDIR = "analyst"
TASK = "eval.analyst"
DEFAULT_PRIORITY = "BACKLOG"
ATTACHMENT_BASE = 1000  # papi.analyst.context: attachment k's markers start at the next multiple of 1000
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXACT_SUFFIXES = ("currency", "side", "decision_body", "direction")


# ------------------------------------------------------------------------------------------- gold


@dataclass
class GoldItem:
    path: Path
    id: str
    event_code: str
    fixture: str
    attachments: list[str]
    filer: str
    related: list[str]
    reviewed: bool
    payload: dict[str, Any]
    spans: dict[str, list[int]]
    derived: list[str]
    notes: str = ""


def load_items(base: Path | None = None) -> list[GoldItem]:
    d = (base or golden_dir()) / SUBDIR
    items = []
    for p in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        gold = raw.get("gold") or {}
        items.append(
            GoldItem(
                path=p,
                id=raw["id"],
                event_code=raw["event_code"],
                fixture=raw["fixture"],
                attachments=list(raw.get("attachments") or []),
                filer=raw["filer"],
                related=list(raw.get("related") or []),
                reviewed=bool(raw.get("reviewed")),
                payload=dict(gold.get("payload") or {}),
                spans={k: list(v) for k, v in (gold.get("spans") or {}).items()},
                derived=list(gold.get("derived") or []),
                notes=(raw.get("notes") or "").strip(),
            )
        )
    return items


def _fixture_json(name: str) -> dict[str, Any]:
    path = fixture_dir() / name
    raw = path.read_bytes()
    return json.loads((gzip.decompress(raw) if path.suffix == ".gz" else raw).decode("utf-8"))[0]


def _list_item(index: int) -> dict[str, Any] | None:
    for p in fixture_dir().glob("list_*.json.gz"):
        for it in json.loads(gzip.decompress(p.read_bytes()).decode("utf-8")):
            if it.get("disclosureIndex") == index:
                return it
    return None


def _attachment_names(detail: dict[str, Any]) -> dict[str, str]:
    return {a["objId"]: a.get("fileName") or "" for a in detail.get("attachments") or [] if a.get("objId")}


def documents_for(item: GoldItem) -> tuple[dict[str, Any], ParsedDocument, list[tuple[str, ParsedDocument]]]:
    detail = _fixture_json(item.fixture)
    parts = detail["disclosureBody"]
    body = parse_body(parts if isinstance(parts, list) else [parts])
    names = _attachment_names(detail)
    atts = []
    for fname in item.attachments:
        obj = fname.removeprefix("file_").split(".")[0]
        name = names.get(obj) or fname
        content = unwrap_java_bytes((fixture_dir() / fname).read_bytes())
        atts.append((name, parse_file(content, file_name=name)))
    return detail, body, atts


def pack_span_texts(body: ParsedDocument, atts: list[tuple[str, ParsedDocument]]) -> dict[int, str]:
    """Pack ordinal → span text, numbered the way the analyst context pack numbers them."""
    out = {s.ordinal: s.text for s in body.spans}
    for _, doc in atts:
        offset = ((max(out) if out else 0) // ATTACHMENT_BASE + 1) * ATTACHMENT_BASE
        out.update({s.ordinal + offset: s.text for s in doc.spans})
    return out


def check_gold(item: GoldItem, tax: tax_mod.Taxonomy) -> list[str]:
    """Problems with a gold file: unknown code, payload not schema-valid, spans that do not exist, or a
    literal gold value that is not in its gold spans (unless listed under ``derived``)."""
    issues: list[str] = []
    if item.event_code not in tax.codes:
        return [f"unknown event code {item.event_code}"]
    schema = tax.schema_for(item.event_code)
    if schema is not None:
        nested = _unflatten(item.payload)
        full = {**{k: None for k in schema.get("required", [])}, **nested}
        for req, sub in (schema.get("properties") or {}).items():
            if sub.get("type") == "object" and full.get(req) is None:
                full[req] = {k: None for k in sub.get("required", [])} or {}
        import jsonschema

        for err in jsonschema.Draft202012Validator(schema).iter_errors(full):
            issues.append(f"payload: {err.message}")
    elif item.payload:
        issues.append(f"{item.event_code} has no payload schema but the gold has a payload")
    _, body, atts = documents_for(item)
    texts = pack_span_texts(body, atts)
    for path, ords in item.spans.items():
        if path not in item.payload:
            issues.append(f"spans for {path}, which is not a gold field")
        missing = [o for o in ords if o not in texts]
        if missing:
            issues.append(f"{path}: span(s) {missing} not in the pack")
            continue
        value = item.payload.get(path)
        if path in item.derived or value is None or isinstance(value, bool):
            continue
        pool = Pool()
        for o in ords:
            pool.add_text(texts[o])
        if isinstance(value, str) and _ISO_DATE.match(value):
            if value not in pool.dates:
                issues.append(f"{path}: date {value} not in spans {ords}")
        elif isinstance(value, int | float):
            d = to_decimal(value)
            if d is None or not pool.has_exact(d):
                issues.append(f"{path}: {value} not in spans {ords}")
    return issues


def _unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    from papi.analyst.pipeline import unflatten

    return unflatten(dict(flat)) if flat else {}


# ------------------------------------------------------------------------------------------ seeding


def _seed_company(conn: Connection, ticker: str) -> tuple[str, str]:
    now = now_utc()
    iss, inst = uuid7(), uuid7()
    conn.execute(
        issuer.insert().values(
            id=iss,
            legal_name=f"{ticker} A.Ş.",
            short_name=ticker,
            kind="company",
            status="active",
            created_at=now,
        )
    )
    conn.execute(
        instrument.insert().values(
            id=inst, issuer_id=iss, type="equity", currency="TRY", status="active", created_at=now
        )
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=inst,
            scheme="TICKER",
            value=ticker,
            valid_from=OPEN_START,
            recorded_at=now,
        )
    )
    return iss, inst


def _insert_document(
    conn: Connection, disclosure_id: str, doc: ParsedDocument, kind: str, name: str | None
) -> str:
    did = uuid7()
    conn.execute(
        document.insert().values(
            id=did,
            disclosure_id=disclosure_id,
            kind=kind,
            mime=doc.mime,
            language=doc.language,
            text=doc.text,
            structure=doc.structure,
            parser=doc.parser,
            parser_version=doc.parser_version,
            parsed_at=now_utc(),
            extra={"file_name": name} if name else {},
        )
    )
    if doc.spans:
        conn.execute(
            document_span.insert(),
            [
                {
                    "id": uuid7(),
                    "document_id": did,
                    "ordinal": s.ordinal,
                    "page": s.page,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                    "field_path": s.field_path,
                    "text": s.text,
                }
                for s in doc.spans
            ],
        )
    return did


def seed_item(conn: Connection, item: GoldItem) -> str:
    """Store the fixture as a normalised disclosure, run the rules triage; returns the gold event's id."""
    from papi.triage.classify import triage_disclosure

    detail, body, atts = documents_for(item)
    basic = detail["disclosure"]["disclosureBasic"]
    issuers = {}
    for t in dict.fromkeys([item.filer, *item.related]):
        issuers[t] = _seed_company(conn, t)[0]
    meta = _list_item(basic["disclosureIndex"]) or {
        "subject": basic.get("title"),
        "summary": basic.get("summary"),
        "disclosureClass": basic.get("disclosureClass"),
        "disclosureType": basic.get("disclosureType"),
        "stockCodes": basic.get("stockCode"),
        "relatedStocks": basic.get("relatedStocks"),
    }
    kap = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    did = uuid7()
    now = now_utc()
    conn.execute(
        disclosure.insert().values(
            id=did,
            source_id=kap,
            source_ref=str(basic["disclosureIndex"]),
            revision_kind="original",
            published_at=parse_kap_datetime(basic["publishDate"]),
            published_at_raw=basic["publishDate"],
            first_seen_at=now,
            source_class=meta.get("disclosureClass"),
            source_type=meta.get("disclosureType"),
            source_subject=meta.get("subject"),
            title=basic.get("title"),
            summary=meta.get("summary"),
            filer_issuer_id=issuers[item.filer],
            related_issuer_ids=list(issuers.values()),
            is_late=False,
            source_payload={"list": meta},
            ingest_status="normalized",
            created_at=now,
        )
    )
    body_id = _insert_document(conn, did, body, "disclosure_body", None)
    att_ids = [_insert_document(conn, did, d, "attachment", n) for n, d in atts]
    conn.execute(
        disclosure.update()
        .where(disclosure.c.id == did)
        .values(body_document_id=body_id, attachment_document_ids=att_ids)
    )
    event_ids = triage_disclosure(conn, did)
    conn.commit()
    rows = conn.execute(select(event.c.id, event.c.code).where(event.c.id.in_(event_ids))).all()
    for r in rows:
        if r.code == item.event_code:
            return r.id
    return rows[0].id


# ------------------------------------------------------------------------------------------ scoring


def _set_null(obj: Any, path: str) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        obj = (
            obj[int(p)]
            if isinstance(obj, list) and p.isdigit() and int(p) < len(obj)
            else (obj.get(p) if isinstance(obj, dict) else None)
        )
        if obj is None:
            return
    last = parts[-1]
    if isinstance(obj, list) and last.isdigit() and int(last) < len(obj):
        obj[int(last)] = None
    elif isinstance(obj, dict):
        obj[last] = None


def stored_payload(
    code: str, so: dict[str, Any], rep: VerifyReport, rules: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(flattened payload papi would store, flattened rules payload) — mirrors the analyst pipeline:
    analyst fields the verifier could not trace are nulled, then rules values win."""
    from papi.analyst.pipeline import merge_payload

    analyst = (so.get("payload") or {}).get(code)
    clean = None
    if isinstance(analyst, dict) and code not in rep.payload_errors:
        clean = copy.deepcopy(analyst)
        for path in rep.payload_fields.get(code) or {}:
            _set_null(clean, path)
    merged = merge_payload(rules, clean) or {}
    return flatten(merged), flatten(rules or {})


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def values_equal(path: str, gold: Any, got: Any) -> bool:
    if gold is None:
        return got is None or got == "" or got == []
    if isinstance(gold, bool):
        return got is gold
    if isinstance(gold, int | float):
        g, a = _num(gold), _num(got)
        return a is not None and g == a
    if got is None:
        return False
    gs, gt = str(gold), str(got)
    if _ISO_DATE.match(gs) or path.endswith(_EXACT_SUFFIXES):
        return gs.strip().upper() == gt.strip().upper()
    a, b = normalize_tr(gs), normalize_tr(gt)
    return bool(a) and bool(b) and (a in b or b in a)


@dataclass
class ItemScore:
    id: str
    reviewed: bool
    fields: int = 0
    fields_ok: int = 0
    spans: int = 0
    spans_ok: int = 0
    claims: int = 0
    claims_ok: int = 0
    summary_ok: bool = False
    clean: bool = False
    code_hit: bool = False
    cost: float = 0.0
    error: str | None = None
    misses: list[str] = field(default_factory=list)


def score_item(item: GoldItem, so: dict[str, Any], rep: VerifyReport, pack: Any, cost: float) -> ItemScore:
    sc = ItemScore(item.id, item.reviewed, cost=cost)
    ev = next((e for e in pack.events if e.code == item.event_code), None)
    rules = ev.payload if ev is not None and ev.payload_source == "form_fields" else None
    rules_spans = ev.payload_spans if ev is not None else {}
    final, rules_flat = stored_payload(item.event_code, so, rep, rules)
    analyst_spans = ((so.get("payload_spans") or {}).get(item.event_code)) or {}
    for path, gold in item.payload.items():
        sc.fields += 1
        got = final.get(path)
        if values_equal(path, gold, got):
            sc.fields_ok += 1
        else:
            sc.misses.append(f"{path}: gold {gold!r}, got {got!r}")
        gspans = set(item.spans.get(path) or [])
        if gspans:
            sc.spans += 1
            pred = set(analyst_spans.get(path) or [])
            if rules_flat.get(path) is not None:
                pred |= set(rules_spans.get(path) or [])
            if pred & gspans:
                sc.spans_ok += 1
            else:
                sc.misses.append(f"{path}: spans {sorted(pred) or '∅'} vs gold {sorted(gspans)}")
    for chk in rep.claims:
        if chk.kind in ("fact", "computation"):
            sc.claims += 1
            sc.claims_ok += chk.status == "pass"
    sc.summary_ok = not any(t.dropped for t in rep.texts.values()) and all(w["kept"] for w in rep.watch_next)
    sc.clean = rep.ok
    sc.code_hit = item.event_code in (so.get("event_codes") or [])
    if not sc.code_hit:
        sc.misses.append(f"event_codes {so.get('event_codes')} lack {item.event_code}")
    return sc


def aggregate(scores: list[ItemScore], prefix: str = "") -> dict[str, float | None]:
    ok = [s for s in scores if s.error is None]
    return {
        f"{prefix}field_accuracy": ratio(sum(s.fields_ok for s in ok), sum(s.fields for s in ok)),
        f"{prefix}span_correctness": ratio(sum(s.spans_ok for s in ok), sum(s.spans for s in ok)),
        f"{prefix}citation_coverage": ratio(sum(s.claims_ok for s in ok), sum(s.claims for s in ok)),
        f"{prefix}summary_traceable": ratio(sum(s.summary_ok for s in ok), len(ok)),
        f"{prefix}verifier_clean_rate": ratio(sum(s.clean for s in ok), len(ok)),
        f"{prefix}event_code_hit": ratio(sum(s.code_hit for s in ok), len(ok)),
    }


GATES = {
    "field_accuracy": Gate(">=", 0.95),
    "span_correctness": Gate(">=", 0.90),
    "citation_coverage": Gate(">=", 0.99),
    "summary_traceable": Gate("==", 1.0),
}


# ------------------------------------------------------------------------------------------- run


def _owner_db_ready() -> None:
    from papi.core.config import get_settings
    from papi.core.db import current_revision

    path = get_settings().paths.db
    if not path.exists() or current_revision() is None:
        raise GoldenMissing(
            f"papi database not initialised at {path} (run `papi init`); the governor needs it"
        )


def run_item(conn: Connection, owner: Connection, item: GoldItem, *, priority: str) -> ItemScore:
    from papi.analyst.context import build_context
    from papi.analyst.pipeline import normalize_output
    from papi.analyst.spec import analyst_spec
    from papi.llm import call as llm_call
    from papi.verify import verify

    event_id = seed_item(conn, item)
    pack = build_context(conn, event_id)
    spec = analyst_spec()
    result = llm_call.call(
        owner,
        task=TASK,
        priority=priority,
        prompt=pack.text,
        system_prompt=spec.system_prompt,
        schema=spec.wire_schema(),
        model=spec.model,
        repair_retry=False,
    )
    if not result.ok:
        sc = ItemScore(item.id, item.reviewed, cost=result.cost_usd_est)
        sc.error = f"{result.error_kind}: {(result.error or '')[:200]}"
        return sc
    so = normalize_output(result.structured_output or {})
    rep = verify(so, pack.sources(), schema=spec.schema)
    if rep.schema_errors:
        sc = ItemScore(item.id, item.reviewed, cost=result.cost_usd_est)
        sc.error = "schema: " + "; ".join(rep.schema_errors[:3])
        return sc
    return score_item(item, so, rep, pack, result.cost_usd_est)


def run(
    *, allow_claude: bool = False, limit: int | None = None, priority: str | None = None, **_: Any
) -> SuiteResult:
    items = load_items()
    tax = tax_mod.load()
    res = SuiteResult("analyst")
    reviewed = [i for i in items if i.reviewed]
    gold_issues = {i.id: check_gold(i, tax) for i in items}
    for iid, issues in gold_issues.items():
        res.failures += [f"gold {iid}: {msg}" for msg in issues]
    res.info = {
        "items": len(items),
        "reviewed": len(reviewed),
        "event_codes": sorted({i.event_code for i in items}),
    }
    gold_ok = ratio(sum(1 for v in gold_issues.values() if not v), len(items))
    res.metrics = [
        Metric("gold_consistency", gold_ok, Gate("==", 1.0), note="gold files valid and traceable"),
        Metric("items", len(items), kind="count"),
        Metric("reviewed_items", len(reviewed), kind="count"),
    ]
    if not allow_claude:
        res.skipped = "Claude calls disabled (use --allow-claude); gold files checked only"
        return res

    from papi.core.db import connect
    from papi.core.jobs import Deferred
    from papi.evals import kbfixture
    from papi.evals.scratch import scratch_db

    _owner_db_ready()
    selected = items[:limit] if limit else items
    prio = priority or DEFAULT_PRIORITY
    scores: list[ItemScore] = []
    with scratch_db() as conn, connect() as owner:
        kbfixture.load(conn)
        for item in selected:
            try:
                sc = run_item(conn, owner, item, priority=prio)
            except Deferred as exc:
                res.failures.append(f"{item.id}: not admitted by the governor ({exc}); run stopped")
                break
            scores.append(sc)
            if sc.error:
                res.failures.append(f"{item.id}: {sc.error}")
            for m in sc.misses:
                res.failures.append(f"{item.id}: {m}")
    rev = [s for s in scores if s.reviewed]
    rev_metrics = aggregate(rev)
    all_metrics = aggregate(scores, "all_")
    for name, gate in GATES.items():
        value = rev_metrics[name]
        res.metrics.append(
            Metric(name, value, gate if rev else None, note="" if rev else "no reviewed items run: not gated")
        )
    res.metrics.append(Metric("verifier_clean_rate", rev_metrics["verifier_clean_rate"]))
    res.metrics += [Metric(k, v, note="all items (reviewed or not)") for k, v in all_metrics.items()]
    calls = scores
    res.metrics += [
        Metric("avg_cost_usd", ratio(sum(s.cost for s in calls), len(calls)), kind="usd"),
        Metric("calls", len(calls), kind="count"),
        Metric("call_errors", sum(1 for s in calls if s.error), kind="count"),
        Metric("reviewed_run", len(rev), kind="count"),
    ]
    res.info.update(
        {
            "priority": prio,
            "run_at": iso_utc(now_utc()),
            "per_item": {
                s.id: {
                    "fields": f"{s.fields_ok}/{s.fields}",
                    "spans": f"{s.spans_ok}/{s.spans}",
                    "claims": f"{s.claims_ok}/{s.claims}",
                    "verifier_clean": s.clean,
                    "cost_usd": s.cost,
                    "error": s.error,
                }
                for s in scores
            },
        }
    )
    return res


# ------------------------------------------------------------------------------------------ review

_REVIEWED_LINE = re.compile(r"^reviewed:\s*false\s*$", re.MULTILINE)


def mark_reviewed(item: GoldItem, at: datetime | None = None) -> None:
    text = item.path.read_text(encoding="utf-8")
    stamp = iso_utc(at or now_utc())
    new, n = _REVIEWED_LINE.subn(f"reviewed: true  # confirmed by the owner {stamp}", text, count=1)
    if n != 1:
        raise ValueError(f"{item.path.name}: no 'reviewed: false' line")
    item.path.write_text(new, encoding="utf-8")
    item.reviewed = True


def review_lines(item: GoldItem) -> list[str]:
    """Item + gold for manual confirmation: each gold field with the text of its gold spans."""
    detail, body, atts = documents_for(item)
    basic = detail["disclosure"]["disclosureBasic"]
    texts = pack_span_texts(body, atts)
    out = [
        f"== {item.id}  ({item.path.name})",
        f"KAP {basic['disclosureIndex']} · {basic.get('publishDate')} · {basic.get('stockCode')} · "
        f"{basic.get('title')}",
        f"https://www.kap.org.tr/tr/Bildirim/{basic['disclosureIndex']}",
        f"olay kodu: {item.event_code}",
    ]
    if item.notes:
        out.append(f"not: {item.notes}")
    out.append("altın payload:")
    for path, value in item.payload.items():
        mark = " (türetilmiş)" if path in item.derived else ""
        out.append(f"  {path} = {json.dumps(value, ensure_ascii=False)}{mark}")
        for o in item.spans.get(path) or []:
            out.append(f"      [S{o}] {texts.get(o, '<yok>')[:220]}")
    if not item.payload:
        out.append("  (payload şeması yok; yalnızca olay kodu, atıflar ve doğrulayıcı puanlanır)")
    return out
