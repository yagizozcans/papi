"""Analyst pipeline for one event (PRD 04 §2): context → analyst call → verifier → persist → checker →
``analysis.completed.v1``.

One analyst call per disclosure: the pack covers every live event of the disclosure and the result is
stored as one ``analysis`` row per event (claims, citations and summaries per event). The first row
carries the call's cost and is the ledger's ``analysis_id``; the others point at it
(``output._papi.call_analysis_id``).

Persistence rules:
- ``analysis.output`` is the analyst output *as shown*: summaries/headline with unverifiable sentences
  dropped, plus ``_papi`` (verifier report, pack manifest, checker state). ``raw_output`` keeps the
  structured output exactly as returned and the CLI's raw JSON.
- Event payload: rules (form-field) values stay authoritative; analyst values fill only fields the
  rules left empty and only when the verifier traced them to their spans. ``payload_source`` becomes
  ``analyst`` (no rules payload) or ``form_fields+analyst``.
- Materiality: the analyst's score replaces the rules score (the rules breakdown stays in
  ``extra.rules``); priority is not changed.
- Event codes (only with ``confidence ≥ CODE_OVERRIDE_MIN_CONFIDENCE`` and known taxonomy codes): new
  codes create new events (``extra.created_by = "analyst"``, published as ``event.classified.v1``); an
  ``other.unclassified`` rules event is superseded by the first new event; other rules codes the
  analyst did not confirm are only marked ``extra.analyst.code_disputed``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from papi import kb
from papi.analyst import checker as checker_mod
from papi.analyst.context import ContextPack, EventNotFound, build_context, live_events
from papi.analyst.spec import AgentSpec, analyst_spec
from papi.core.config import get_settings
from papi.core.events import ANALYSIS_COMPLETED, EVENT_CLASSIFIED, publish
from papi.core.ids import uuid7
from papi.core.jobs import PermanentError, RetryLater, priority_for
from papi.core.logging import get_logger
from papi.core.schema import analysis, citation, claim, event, summary
from papi.core.state import delete_state, get_state, set_state
from papi.core.timeutil import now_utc, parse_iso
from papi.llm import call as llm_call
from papi.llm.backend import LlmBackend
from papi.llm.governor import link_ledger
from papi.llm.types import LlmResult
from papi.triage import taxonomy as tax_mod
from papi.triage.materiality import band_for
from papi.verify import VerifyReport, flatten, verify

log = get_logger("analyst")

SOURCE = "analyst"
ANALYSED_PRIORITIES = ("P1", "P2", "P3")
CODE_OVERRIDE_MIN_CONFIDENCE = 0.6
HISTORICAL_CATCHUP_AGE = timedelta(days=3)
TIMEOUT_RETRY_S = 600
LOCK_PREFIX = "analyst:inflight:"
CALL_PREFIX = "analyst:disclosure:"


@dataclass
class Outcome:
    status: str  # analysed | reused | invalid | skipped
    reason: str | None = None
    analysis_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    pack: ContextPack | None = None
    report: VerifyReport | None = None
    result: LlmResult | None = None
    checker: str | None = None
    created_event_ids: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------------- gating


def skip_reason(ev: Any, now: datetime | None = None) -> str | None:
    """Why an event is not analysed (``None`` = analyse)."""
    if ev["superseded_by"]:
        return "superseded"
    if ev["status"] == "retracted":
        return "retracted"
    if ev["priority"] not in ANALYSED_PRIORITIES:
        return f"priority:{ev['priority'] or 'none'}"
    extra = ev["extra"] or {}
    announced = ev["announced_at"]
    cutoff = (now or now_utc()) - HISTORICAL_CATCHUP_AGE
    if extra.get("is_catchup") and announced is not None and announced < cutoff:
        return "historical_catchup"
    return None


def _analyst_rows(conn: Connection, event_id: str) -> list[Any]:
    return list(
        conn.execute(
            select(analysis)
            .where(analysis.c.event_id == event_id, analysis.c.agent == "analyst")
            .order_by(analysis.c.created_at.desc(), analysis.c.id.desc())
        )
        .mappings()
        .all()
    )


def _lock_ttl() -> timedelta:
    return timedelta(seconds=2 * get_settings().llm.timeout_s + 300)


def _acquire(conn: Connection, disclosure_id: str, event_id: str, now: datetime) -> None:
    key = LOCK_PREFIX + disclosure_id
    held = get_state(conn, key)
    if held and held.get("event_id") != event_id:
        at = parse_iso(held["at"]) if held.get("at") else None
        if at is not None and at > now - _lock_ttl():
            raise RetryLater(
                60, f"analyst call for disclosure {disclosure_id} in flight ({held['event_id']})"
            )
    set_state(conn, key, {"event_id": event_id, "at": now.isoformat()})
    conn.commit()


def _release(conn: Connection, disclosure_id: str) -> None:
    conn.rollback()
    delete_state(conn, LOCK_PREFIX + disclosure_id)
    conn.commit()


# ------------------------------------------------------------------------------------- entry point


def analyse_event(
    conn: Connection,
    event_id: str,
    *,
    backend: LlmBackend | None = None,
    force: bool = False,
    ctx: Any = None,
    now: datetime | None = None,
) -> Outcome:
    """Analyse the disclosure of ``event_id`` once (idempotent); ``Deferred`` from the governor
    propagates so the job is re-queued. Commits ``conn``."""
    now = now or now_utc()
    ev = conn.execute(select(event).where(event.c.id == event_id)).mappings().first()
    if ev is None:
        raise EventNotFound(event_id)
    reason = skip_reason(ev, now)
    if reason and not (force and reason.startswith(("priority", "historical"))):
        return Outcome("skipped", reason, event_ids=[event_id])

    existing = _analyst_rows(conn, event_id)
    if existing and not force:
        return Outcome("skipped", "already_analysed", [existing[0]["id"]], [event_id])

    pack = build_context(conn, event_id)
    if not force:
        same = (
            conn.execute(
                select(analysis)
                .where(analysis.c.input_hash == pack.input_hash, analysis.c.agent == "analyst")
                .order_by(analysis.c.created_at)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if same is not None:
            aid = _copy_analysis(conn, same, event_id, pack)
            conn.commit()
            return Outcome("reused", None, [aid], [event_id], pack=pack)

    spec = analyst_spec()
    label = pack.priority if pack.priority in ANALYSED_PRIORITIES else "P3"
    primary_id = uuid7()
    _acquire(conn, pack.disclosure_id, event_id, now)
    try:
        if ctx is not None:
            ctx.heartbeat(2 * get_settings().llm.timeout_s + 120)
        result = llm_call.call(
            conn,
            task="analyst",
            priority=label,
            prompt=pack.text,
            system_prompt=spec.system_prompt,
            schema=spec.wire_schema(),
            backend=backend,
            model=spec.model,
        )
        if not result.ok:
            if result.error_kind == "timeout":
                raise RetryLater(TIMEOUT_RETRY_S, f"analyst timeout: {result.error}")
            raise PermanentError(f"analyst call failed ({result.error_kind}): {(result.error or '')[:500]}")
        out = record_result(conn, pack, spec, result, primary_id=primary_id, label=label, backend=backend)
        conn.commit()
    finally:
        _release(conn, pack.disclosure_id)
    return out


# ------------------------------------------------------------------------------------- persistence


def _raw_output(result: LlmResult) -> dict[str, Any]:
    return {"structured_output": result.structured_output, "cli": result.raw_json}


def _analysis_row(
    *,
    aid: str,
    event_id: str,
    spec: AgentSpec,
    pack: ContextPack,
    result: LlmResult | None,
    output: dict[str, Any],
    raw_output: dict[str, Any] | None,
    review_state: str,
    label: str | None,
    cost: float | None,
    kb_snapshot: str | None,
) -> dict[str, Any]:
    so = (result.structured_output if result else None) or {}
    conf = so.get("confidence")
    return {
        "id": aid,
        "event_id": event_id,
        "agent": "analyst",
        "agent_version": spec.agent_version,
        "backend": get_settings().llm.backend,
        "model_ids": (result.model_ids if result else None) or [spec.model],
        "prompt_version": spec.version,
        "schema_version": spec.version,
        "kb_snapshot": kb_snapshot,
        "input_hash": pack.input_hash,
        "output": output,
        "raw_output": raw_output,
        "confidence": float(conf) if isinstance(conf, int | float) and not isinstance(conf, bool) else None,
        "latency_ms": result.duration_ms if result else None,
        "cost_usd_est": cost,
        "priority": label,
        "created_at": now_utc(),
        "review_state": review_state,
    }


def _curated(so: dict[str, Any], rep: VerifyReport, fallback_headline: str | None) -> dict[str, Any]:
    """Output as shown: unverifiable sentences dropped, claims annotated with their verify status."""
    out = copy.deepcopy(so)
    for name, chk in rep.texts.items():
        out[name] = chk.kept
    if not out.get("headline_tr") or (rep.texts.get("headline_tr") and rep.texts["headline_tr"].dropped):
        out["headline_tr"] = fallback_headline or ""
    out["watch_next"] = [w["text"] for w in rep.watch_next if w["kept"]]
    for chk in rep.claims:
        c = out["claims"][chk.index]
        c["verify_status"] = chk.status
        if chk.issues:
            c["verify_issues"] = chk.issues
    return out


def _clean_payload(code: str, payload: dict[str, Any] | None, rep: VerifyReport) -> dict[str, Any] | None:
    """Analyst payload with untraced fields nulled; ``None`` when schema-invalid or empty."""
    if not payload or code in rep.payload_errors:
        return None
    bad = rep.payload_fields.get(code) or {}
    clean = copy.deepcopy(payload)
    for path in bad:
        _set_path(clean, path, None)
    return clean if any(v is not None for v in flatten(clean).values()) else None


def _set_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[int(p)] if isinstance(obj, list) else obj.get(p)
        if obj is None:
            return
    last = parts[-1]
    if isinstance(obj, list):
        obj[int(last)] = value
    else:
        obj[last] = value


def unflatten(payload: dict[str, Any]) -> dict[str, Any]:
    """Expand dotted keys (``{"counterparty.name": x}`` → ``{"counterparty": {"name": x}}``); a dict
    whose keys are all ``0..n-1`` becomes a list."""
    if not any("." in k for k in payload):
        return payload
    out: dict[str, Any] = {}
    for key, value in payload.items():
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            child = node.get(p)
            if not isinstance(child, dict):
                child = {}
                node[p] = child
            node = child
        node[parts[-1]] = value

    def lists(obj: Any) -> Any:
        if isinstance(obj, dict):
            obj = {k: lists(v) for k, v in obj.items()}
            if obj and all(k.isdigit() for k in obj) and sorted(map(int, obj)) == list(range(len(obj))):
                return [obj[str(i)] for i in range(len(obj))]
        return obj

    return lists(out)


def normalize_output(so: dict[str, Any]) -> dict[str, Any]:
    """Mechanical echo fixes, applied before verification (``raw_output`` keeps the original):
    per-code payloads with dotted keys are nested, and citation ids copied with the pack's ``KB:``
    marker prefix (``[KB:tr:spk:…]``) lose the prefix."""
    out = copy.deepcopy(so)
    payload = out.get("payload")
    if isinstance(payload, dict):
        out["payload"] = {c: unflatten(p) if isinstance(p, dict) else p for c, p in payload.items()}
    for c in out.get("claims") or []:
        if isinstance(c, dict) and isinstance(c.get("citation_ids"), list):
            c["citation_ids"] = [
                x.removeprefix("[").removesuffix("]").removeprefix("KB:") if isinstance(x, str) else x
                for x in c["citation_ids"]
            ]
    return out


def merge_payload(rules: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rules values win where set; analyst values fill nulls/missing keys (recursively for objects)."""
    if rules is None:
        return copy.deepcopy(extra)
    if extra is None:
        return copy.deepcopy(rules)
    out = copy.deepcopy(rules)
    for k, v in extra.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = merge_payload(cur, v)
        elif cur is None or (isinstance(cur, list) and not cur and v):
            out[k] = copy.deepcopy(v)
    return out


def _payload_valid(payload: dict[str, Any], schema: dict[str, Any] | None) -> bool:
    if schema is None:
        return True
    return not list(jsonschema.Draft202012Validator(schema).iter_errors(payload))


def _insert_claims(
    conn: Connection, analysis_id: str, so: dict[str, Any], rep: VerifyReport, pack: ContextPack
) -> list[str]:
    ids: list[str] = []
    claims = so.get("claims") or []
    for chk in rep.claims:
        c = claims[chk.index]
        cid = uuid7()
        ids.append(cid)
        conn.execute(
            claim.insert().values(
                id=cid,
                analysis_id=analysis_id,
                text_tr=c.get("text_tr"),
                text_en=c.get("text_en"),
                kind=c.get("kind"),
                value=c.get("value"),
                unit=c.get("unit"),
                formula_id=c.get("formula_id"),
                verify_status=chk.status,
                check_status=None,
            )
        )
        rows = []
        for o in chk.spans:
            s = pack.spans[o]
            rows.append(
                {"id": uuid7(), "claim_id": cid, "target_type": "document_span", "target_id": s.span_id,
                 "quote": s.text[:500]}
            )  # fmt: skip
        for k in chk.citations:
            ref = pack.kb[k]
            rows.append(
                {"id": uuid7(), "claim_id": cid, "target_type": "provision_version",
                 "target_id": ref.provision_version_id, "quote": f"{ref.label} ({k})"[:500]}
            )  # fmt: skip
        if rows:
            conn.execute(citation.insert(), rows)
    return ids


def upsert_summary(
    conn: Connection, event_id: str, lang: str, headline: str | None, body: str | None, aid: str
) -> None:
    stmt = sqlite_insert(summary).values(
        id=uuid7(),
        event_id=event_id,
        lang=lang,
        headline=headline,
        body=body,
        analysis_id=aid,
        created_at=now_utc(),
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["event_id", "lang"],
            set_={"headline": headline, "body": body, "analysis_id": aid, "created_at": now_utc()},
        )
    )


def _body_ordinals(pack: ContextPack, ords: list[int]) -> list[int]:
    body = next((d for d in pack.documents if d.role == "body"), None)
    if body is None:
        return []
    return [
        pack.spans[o].native_ordinal
        for o in ords
        if o in pack.spans and pack.spans[o].document_id == body.document_id
    ]


def _apply_to_event(
    conn: Connection,
    ev: Any,
    so: dict[str, Any],
    rep: VerifyReport,
    pack: ContextPack,
    aid: str,
    tax: tax_mod.Taxonomy,
) -> None:
    code = ev["code"]
    extra = dict(ev["extra"] or {})
    values: dict[str, Any] = {}
    analyst_codes = list(so.get("event_codes") or [])
    a_extra: dict[str, Any] = {
        "analysis_id": aid,
        "code_confirmed": code in analyst_codes,
        "materiality": so.get("materiality"),
        "fundamental_direction": so.get("fundamental_direction"),
        "fundamental_channels": so.get("fundamental_channels"),
    }
    if code not in analyst_codes and code != tax_mod.UNCLASSIFIED:
        a_extra["code_disputed"] = True
        a_extra["suggested_codes"] = analyst_codes

    clean = _clean_payload(code, (so.get("payload") or {}).get(code), rep)
    rules_payload = (
        ev["payload"]
        if ev["payload_source"] == "form_fields"
        else extra.get("analyst", {}).get("rules_payload")
    )
    if clean is not None:
        merged = merge_payload(rules_payload, clean)
        if merged != ev["payload"] and _payload_valid(merged, tax.schema_for(code)):
            values["payload"] = merged
            values["payload_source"] = "form_fields+analyst" if rules_payload else "analyst"
            ec = tax.get(code)
            if ec is not None and ec.schema and not ev["payload_schema"]:
                values["payload_schema"] = ec.schema
            if rules_payload:
                a_extra["rules_payload"] = rules_payload
            spans = (so.get("payload_spans") or {}).get(code) or {}
            rules_flat = flatten(rules_payload or {})
            added = {
                p: o
                for p, o in spans.items()
                if rules_flat.get(p) is None and p not in (rep.payload_fields.get(code) or {})
            }
            if added:
                ps = dict(extra.get("payload_spans") or {})
                for p, ords in added.items():
                    native = _body_ordinals(pack, ords)
                    if native:
                        ps.setdefault(p, native)
                extra["payload_spans"] = ps
                extra["payload_span_ids"] = {
                    **(extra.get("payload_span_ids") or {}),
                    **{
                        p: [pack.spans[o].span_id for o in ords if o in pack.spans]
                        for p, ords in added.items()
                    },
                }
    m = so.get("materiality")
    if isinstance(m, int) and m != ev["materiality"]:
        values["materiality"] = m
        values["materiality_band"] = band_for(m)
        a_extra["rules_materiality"] = ev["materiality"]
        extra["materiality_source"] = "analyst"
    extra["analyst"] = a_extra
    values["extra"] = extra
    values["updated_at"] = now_utc()
    conn.execute(update(event).where(event.c.id == ev["id"]).values(**values))


def _new_events(
    conn: Connection,
    pack: ContextPack,
    so: dict[str, Any],
    rep: VerifyReport,
    tax: tax_mod.Taxonomy,
    headline: str,
) -> list[tuple[str, Any]]:
    """Events for analyst codes the rules missed (see module docstring)."""
    conf = so.get("confidence")
    if not isinstance(conf, int | float) or conf < CODE_OVERRIDE_MIN_CONFIDENCE:
        return []
    rules_codes = {e.code for e in pack.events}
    new_codes = [
        c
        for c in dict.fromkeys(so.get("event_codes") or [])
        if c in tax.codes and c not in rules_codes and c != tax_mod.UNCLASSIFIED
    ]
    if not new_codes:
        return []
    base = conn.execute(select(event).where(event.c.id == pack.events[0].id)).mappings().first()
    created: list[tuple[str, Any]] = []
    now = now_utc()
    m = so.get("materiality") if isinstance(so.get("materiality"), int) else base["materiality"]
    for code in new_codes:
        payload = _clean_payload(code, (so.get("payload") or {}).get(code), rep)
        ec = tax.get(code)
        if payload is not None and not _payload_valid(payload, tax.schema_for(code)):
            payload = None
        eid = uuid7()
        conn.execute(
            event.insert().values(
                id=eid,
                disclosure_ids=base["disclosure_ids"],
                primary_disclosure_id=base["primary_disclosure_id"],
                code=code,
                taxonomy_version=tax.version,
                issuer_ids=base["issuer_ids"],
                instrument_ids=base["instrument_ids"],
                occurred_at=None,
                announced_at=base["announced_at"],
                payload=payload,
                payload_source="analyst" if payload is not None else None,
                payload_schema=ec.schema if ec else None,
                materiality=m,
                materiality_band=band_for(m) if m is not None else None,
                priority=base["priority"],
                status="provisional",
                extra={
                    "headline_tr": headline,
                    "tickers": (base["extra"] or {}).get("tickers") or [],
                    "created_by": "analyst",
                    **({"is_catchup": True} if (base["extra"] or {}).get("is_catchup") else {}),
                },
                created_at=now,
            )
        )
        created.append((eid, code))
    unclassified = [e for e in pack.events if e.code == tax_mod.UNCLASSIFIED]
    for e in unclassified:
        conn.execute(
            update(event).where(event.c.id == e.id).values(superseded_by=created[0][0], updated_at=now)
        )
    return created


def record_invalid(
    conn: Connection,
    pack: ContextPack,
    spec: AgentSpec,
    result: LlmResult,
    rep: VerifyReport,
    *,
    primary_id: str,
    label: str,
) -> Outcome:
    """Schema-invalid output: one flagged analysis row (review list), nothing applied or published."""
    output = {"_papi": {"verify": rep.to_json(), "context": pack.manifest(), "call_analysis_id": primary_id}}
    conn.execute(
        analysis.insert().values(
            **_analysis_row(
                aid=primary_id, event_id=pack.event_id, spec=spec, pack=pack, result=result, output=output,
                raw_output=_raw_output(result), review_state="flagged", label=label,
                cost=result.cost_usd_est, kb_snapshot=kb.current_snapshot(conn),
            )
        )
    )  # fmt: skip
    link_ledger(conn, result.ledger_ids, primary_id)
    log.warning(
        "analyst output schema-invalid for event %s: %s", pack.event_id, "; ".join(rep.schema_errors[:3])
    )
    return Outcome("invalid", "schema", [primary_id], [pack.event_id], pack=pack, report=rep, result=result)


def record_result(
    conn: Connection,
    pack: ContextPack,
    spec: AgentSpec,
    result: LlmResult,
    *,
    primary_id: str,
    label: str,
    backend: LlmBackend | None = None,
) -> Outcome:
    """Verify + persist one analyst result for every live event of the pack's disclosure, run the
    checker when required and publish ``analysis.completed.v1``. The call's ledger rows
    (``result.ledger_ids``) are linked to the first analysis row."""
    so = normalize_output(result.structured_output or {})
    rep = verify(so, pack.sources(), schema=spec.schema)
    if rep.schema_errors:
        return record_invalid(conn, pack, spec, result, rep, primary_id=primary_id, label=label)

    tax = tax_mod.load()
    triage_headline = next((e.headline_tr for e in pack.events if e.headline_tr), None)
    curated = _curated(so, rep, triage_headline)
    kb_snap = kb.current_snapshot(conn)
    review = "auto" if rep.ok else "flagged"
    created = _new_events(conn, pack, so, rep, tax, curated["headline_tr"])

    live = {r["id"]: r for r in live_events(conn, pack.disclosure_id)}
    targets = [live[e.id] for e in pack.events if e.id in live]
    if not any(r["id"] == pack.event_id for r in targets):
        ev = conn.execute(select(event).where(event.c.id == pack.event_id)).mappings().first()
        targets.insert(0, ev)
    else:
        targets.sort(key=lambda r: r["id"] != pack.event_id)
    created_rows = [live[eid] for eid, _ in created if eid in live]

    ids: list[str] = []
    needs_check = checker_mod.required(label, rep, so, pack, curated_materiality=so.get("materiality"))
    for i, ev in enumerate([*targets, *created_rows]):
        aid = primary_id if i == 0 else uuid7()
        papi_meta = {
            "verify": rep.to_json(),
            "context": pack.manifest(),
            "call_analysis_id": primary_id,
            "checker": {"state": "pending" if needs_check else "not_required"},
            "created_event_ids": [eid for eid, _ in created],
        }
        conn.execute(
            analysis.insert().values(
                **_analysis_row(
                    aid=aid, event_id=ev["id"], spec=spec, pack=pack, result=result,
                    output={**curated, "_papi": papi_meta},
                    raw_output=_raw_output(result) if i == 0 else None, review_state=review, label=label,
                    cost=result.cost_usd_est if i == 0 else 0.0, kb_snapshot=kb_snap,
                )
            )
        )  # fmt: skip
        if i == 0:
            link_ledger(conn, result.ledger_ids, aid)
        _insert_claims(conn, aid, so, rep, pack)
        upsert_summary(
            conn, ev["id"], "tr", curated["headline_tr"] or None, curated.get("summary_tr") or None, aid
        )
        upsert_summary(conn, ev["id"], "en", None, curated.get("summary_en") or None, aid)
        if ev["id"] not in {eid for eid, _ in created}:
            _apply_to_event(conn, ev, so, rep, pack, aid, tax)
        ids.append(aid)

    event_ids = [r["id"] for r in [*targets, *created_rows]]
    set_state(
        conn,
        CALL_PREFIX + pack.disclosure_id,
        {
            "input_hash": pack.input_hash,
            "analysis_ids": dict(zip(event_ids, ids, strict=True)),
            "at": now_utc().isoformat(),
        },
    )
    for eid, code in created:
        row = live.get(eid)
        publish(
            conn, EVENT_CLASSIFIED, f"event/{eid}",
            {"event_id": eid, "code": code, "materiality_band": row["materiality_band"] if row else None,
             "priority": row["priority"] if row else label, "disclosure_id": pack.disclosure_id},
            SOURCE, priority=priority_for(label),
        )  # fmt: skip
    conn.commit()

    check_state = None
    if needs_check:
        check_state = checker_mod.run_for_call(conn, ids, backend=backend)
    for eid, aid in zip(event_ids, ids, strict=True):
        publish(
            conn, ANALYSIS_COMPLETED, f"event/{eid}", {"event_id": eid, "analysis_ids": [aid]}, SOURCE,
            priority=priority_for(label),
        )  # fmt: skip
    conn.commit()
    log.info(
        "analysed disclosure %s → %d analysis row(s), review=%s, issues=%d, checker=%s",
        pack.disclosure_id, len(ids), review, len(rep.issues), check_state,
    )  # fmt: skip
    return Outcome(
        "analysed", None, ids, event_ids, pack=pack, report=rep, result=result, checker=check_state,
        created_event_ids=[eid for eid, _ in created],
    )  # fmt: skip


def _copy_analysis(conn: Connection, src: Any, event_id: str, pack: ContextPack) -> str:
    """Attach an existing call's analysis (same input hash) to another event of the disclosure."""
    aid = uuid7()
    skip = ("id", "event_id", "created_at", "raw_output", "cost_usd_est")
    values = {k: v for k, v in src.items() if k not in skip}
    conn.execute(
        analysis.insert().values(
            id=aid, event_id=event_id, created_at=now_utc(), raw_output=None, cost_usd_est=0.0, **values
        )
    )
    for c in (
        conn.execute(select(claim).where(claim.c.analysis_id == src["id"]).order_by(claim.c.id))
        .mappings()
        .all()
    ):
        cid = uuid7()
        conn.execute(claim.insert().values(**{**dict(c), "id": cid, "analysis_id": aid}))
        cits = conn.execute(select(citation).where(citation.c.claim_id == c["id"])).mappings().all()
        if cits:
            conn.execute(citation.insert(), [{**dict(x), "id": uuid7(), "claim_id": cid} for x in cits])
    out = src["output"] or {}
    upsert_summary(conn, event_id, "tr", out.get("headline_tr") or None, out.get("summary_tr") or None, aid)
    upsert_summary(conn, event_id, "en", None, out.get("summary_en") or None, aid)
    publish(
        conn, ANALYSIS_COMPLETED, f"event/{event_id}", {"event_id": event_id, "analysis_ids": [aid]}, SOURCE,
        priority=priority_for(pack.priority or "P3"),
    )  # fmt: skip
    return aid


def reverify(conn: Connection, analysis_id: str) -> VerifyReport:
    """Re-run the verifier on a stored analysis (raw structured output + pack manifest) and update
    ``claim.verify_status`` and ``review_state`` (``approved``/``rejected`` are kept)."""
    from papi.analyst.context import sources_from_manifest

    row = conn.execute(select(analysis).where(analysis.c.id == analysis_id)).mappings().first()
    if row is None:
        raise LookupError(analysis_id)
    meta = (row["output"] or {}).get("_papi") or {}
    call_id = meta.get("call_analysis_id") or analysis_id
    raw = row["raw_output"]
    if raw is None and call_id != analysis_id:
        raw = conn.execute(select(analysis.c.raw_output).where(analysis.c.id == call_id)).scalar_one_or_none()
    so = (raw or {}).get("structured_output")
    so = normalize_output(so) if isinstance(so, dict) else so
    if not isinstance(so, dict):
        raise ValueError(f"analysis {analysis_id} has no structured output")
    spec = analyst_spec()
    rep = verify(so, sources_from_manifest(conn, meta.get("context") or {}), schema=spec.schema)
    claim_ids = (
        conn.execute(select(claim.c.id).where(claim.c.analysis_id == analysis_id).order_by(claim.c.id))
        .scalars()
        .all()
    )
    for cid, chk in zip(claim_ids, rep.claims, strict=False):
        conn.execute(update(claim).where(claim.c.id == cid).values(verify_status=chk.status))
    unsupported = conn.execute(
        select(claim.c.id).where(claim.c.analysis_id == analysis_id, claim.c.check_status == "unsupported")
    ).first()
    if row["review_state"] in ("auto", "flagged"):
        state = "auto" if rep.ok and unsupported is None else "flagged"
        output = dict(row["output"] or {})
        output["_papi"] = {**meta, "verify": rep.to_json()}
        conn.execute(
            update(analysis).where(analysis.c.id == analysis_id).values(review_state=state, output=output)
        )
    return rep


__all__ = [
    "ANALYSED_PRIORITIES",
    "Outcome",
    "analyse_event",
    "merge_payload",
    "record_result",
    "reverify",
    "skip_reason",
    "upsert_summary",
]
