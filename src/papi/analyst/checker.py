"""Checker call (PRD 04 §4.3): an independent entailment check of the analyst's verified fact /
computation claims against their cited source text, for P1/P2 events with materiality band high or
critical. Results go to ``claim.check_status`` on every analysis row of the call; an ``unsupported``
claim flags the analyses. The call itself is stored as an ``analysis`` row with ``agent = "checker"``.

When the governor defers the checker, the analyst result is published anyway and an
``analyst:check`` job (``{"analysis_id": <call analysis id>}``) runs the check at the deferral time.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import Connection, select, update

from papi.core import jobs
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.jobs import Deferred, priority_for
from papi.core.logging import get_logger
from papi.core.schema import analysis, claim
from papi.core.timeutil import now_utc
from papi.llm import call as llm_call
from papi.llm.backend import LlmBackend
from papi.llm.governor import link_ledger
from papi.triage.materiality import band_for
from papi.verify import VerifyReport

log = get_logger("analyst.checker")

CHECK_JOB = "analyst:check"
CHECKED_PRIORITIES = ("P1", "P2")
CHECKED_BANDS = ("high", "critical")
CHECKED_KINDS = ("fact", "computation")
SOURCE_OPEN = "<<<UNTRUSTED_SOURCE"
SOURCE_CLOSE = "UNTRUSTED_SOURCE>>>"


def _band(so: dict[str, Any], pack: Any) -> str | None:
    scores = [e.materiality for e in pack.events if isinstance(e.materiality, int)]
    m = so.get("materiality")
    if isinstance(m, int):
        scores.append(m)
    return band_for(max(scores)) if scores else None


def required(label: str | None, rep: VerifyReport, so: dict[str, Any], pack: Any, **_: Any) -> bool:
    """Checker gate: P1/P2, band high/critical (analyst or rules score), ≥1 verified fact/computation."""
    if label not in CHECKED_PRIORITIES or _band(so, pack) not in CHECKED_BANDS:
        return False
    return any(c.status == "pass" and c.kind in CHECKED_KINDS for c in rep.claims)


def build_prompt(
    claims: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    spans: dict[int, str],
    provisions: dict[str, str],
) -> tuple[str, list[int]]:
    """Checker input and the claim indices it covers (verified fact/computation claims only)."""
    lines = [
        "Aşağıdaki her iddiayı yalnızca altında verilen kaynak metne göre değerlendir.",
        "Evaluate each claim only against the source text given under it.",
        "",
    ]
    indices: list[int] = []
    for chk in checks:
        if chk.get("status") != "pass" or chk.get("kind") not in CHECKED_KINDS:
            continue
        i = chk["index"]
        c = claims[i]
        indices.append(i)
        lines.append(f"### İddia {i} ({c.get('kind')})")
        lines.append(f"TR: {c.get('text_tr')}")
        if c.get("text_en"):
            lines.append(f"EN: {c['text_en']}")
        if c.get("kind") == "computation":
            lines.append(f"Hesap: {c.get('formula_id')} = {c.get('value')} {c.get('unit') or ''}".rstrip())
        lines.append("Kaynak:")
        lines.append(SOURCE_OPEN)
        for o in chk.get("spans") or []:
            if o in spans:
                lines.append(f"[S{o}] {' '.join(spans[o].split())}")
        for cid in chk.get("citations") or []:
            if cid in provisions:
                lines.append(f"[KB:{cid}] {' '.join(provisions[cid].split())}")
        lines.append(SOURCE_CLOSE)
        lines.append("")
    return "\n".join(lines), indices


def _group(conn: Connection, call_id: str) -> list[Any]:
    """The call's analysis row plus rows attached to other events (same input hash)."""
    primary = conn.execute(select(analysis).where(analysis.c.id == call_id)).mappings().first()
    if primary is None:
        return []
    rows = (
        conn.execute(
            select(analysis)
            .where(analysis.c.agent == "analyst", analysis.c.input_hash == primary["input_hash"])
            .order_by(analysis.c.created_at, analysis.c.id)
        )
        .mappings()
        .all()
    )
    return sorted(rows, key=lambda r: r["id"] != call_id)


def _set_state(conn: Connection, rows: list[Any], state: dict[str, Any], *, flag: bool = False) -> None:
    for r in rows:
        output = dict(r["output"] or {})
        meta = dict(output.get("_papi") or {})
        meta["checker"] = state
        output["_papi"] = meta
        values: dict[str, Any] = {"output": output}
        if flag and r["review_state"] == "auto":
            values["review_state"] = "flagged"
        conn.execute(update(analysis).where(analysis.c.id == r["id"]).values(**values))


def run_for_call(
    conn: Connection,
    analysis_ids: list[str],
    *,
    backend: LlmBackend | None = None,
    raise_deferred: bool = False,
) -> str:
    """Run the checker for one analyst call; returns ``done`` | ``pending`` | ``error`` | ``skipped``.

    A governor deferral enqueues an ``analyst:check`` job (``pending``), or re-raises ``Deferred``
    when ``raise_deferred`` (inside that job). A call whose check is already done is not re-checked.
    Commits ``conn``."""
    from papi.analyst.context import sources_from_manifest
    from papi.analyst.spec import checker_spec

    rows = _group(conn, analysis_ids[0])
    if not rows:
        return "skipped"
    primary = rows[0]
    meta = (primary["output"] or {}).get("_papi") or {}
    if (meta.get("checker") or {}).get("state") == "done":
        return "done"
    so = (primary["raw_output"] or {}).get("structured_output") or {}
    src = sources_from_manifest(conn, meta.get("context") or {})
    prompt, indices = build_prompt(
        so.get("claims") or [], (meta.get("verify") or {}).get("claims") or [], src.spans, src.provisions
    )
    if not indices:
        _set_state(conn, rows, {"state": "skipped", "reason": "no verified claims"})
        conn.commit()
        return "skipped"
    spec = checker_spec()
    label = primary["priority"] or "P2"
    checker_id = uuid7()
    try:
        result = llm_call.call(
            conn,
            task="checker",
            priority=label,
            prompt=prompt,
            system_prompt=spec.system_prompt,
            schema=spec.wire_schema(),
            backend=backend,
            model=spec.model,
        )
    except Deferred as exc:
        state = {"state": "pending", "deferred_until": exc.until.isoformat(), "reason": exc.reason}
        _set_state(conn, rows, state)
        if raise_deferred:
            conn.commit()
            raise
        jobs.enqueue(
            conn,
            CHECK_JOB,
            {"analysis_id": primary["id"]},
            priority=priority_for(label),
            not_before=exc.until,
            dedupe_key=f"{CHECK_JOB}:{primary['id']}",
        )
        conn.commit()
        return "pending"
    out = result.structured_output if result.ok else None
    errors = []
    if out is not None:
        import jsonschema

        errors = [e.message for e in jsonschema.Draft202012Validator(spec.schema).iter_errors(out)]
    if out is None or errors:
        detail = (result.error or "; ".join(errors[:3]) or "")[:500]
        _set_state(conn, rows, {"state": "error", "error": f"{result.error_kind or 'schema'}: {detail}"})
        conn.commit()
        log.warning("checker failed for analysis %s: %s", primary["id"], detail)
        return "error"

    conn.execute(
        analysis.insert().values(
            id=checker_id,
            event_id=primary["event_id"],
            agent="checker",
            agent_version=spec.agent_version,
            backend=get_settings().llm.backend,
            model_ids=result.model_ids or [spec.model],
            prompt_version=spec.version,
            schema_version=spec.version,
            kb_snapshot=primary["kb_snapshot"],
            input_hash=hashlib.sha256(f"{spec.agent_version}\n{prompt}".encode()).hexdigest(),
            output=out,
            raw_output={"structured_output": out, "cli": result.raw_json},
            confidence=None,
            latency_ms=result.duration_ms,
            cost_usd_est=result.cost_usd_est,
            priority=label,
            created_at=now_utc(),
            review_state="auto",
        )
    )
    link_ledger(conn, result.ledger_ids, checker_id)
    status_by_index = {
        r["claim_index"]: r["status"] for r in out.get("results") or [] if r.get("claim_index") in indices
    }
    missing = [i for i in indices if i not in status_by_index]
    checks = (meta.get("verify") or {}).get("claims") or []
    position = {c["index"]: n for n, c in enumerate(checks)}  # claim rows are inserted in claims order
    unsupported = False
    for r in rows:
        claim_ids = (
            conn.execute(select(claim.c.id).where(claim.c.analysis_id == r["id"]).order_by(claim.c.id))
            .scalars()
            .all()
        )
        for idx, status in status_by_index.items():
            n = position.get(idx)
            if n is not None and n < len(claim_ids):
                conn.execute(update(claim).where(claim.c.id == claim_ids[n]).values(check_status=status))
        unsupported = unsupported or any(s == "unsupported" for s in status_by_index.values())
    _set_state(
        conn,
        rows,
        {
            "state": "done",
            "analysis_id": checker_id,
            "results": out.get("results") or [],
            "unchecked": missing,
        },
        flag=unsupported,
    )
    conn.commit()
    return "done"
