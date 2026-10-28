from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from papi.analyst import handlers
from papi.analyst.checker import CHECK_JOB
from papi.analyst.pipeline import analyse_event, reverify
from papi.analyst.spec import analyst_spec
from papi.core.events import ANALYSIS_COMPLETED, EVENT_CLASSIFIED, CloudEvent
from papi.core.ids import uuid7
from papi.core.jobs import Deferred, PermanentError
from papi.core.schema import (
    analysis,
    citation,
    claim,
    document_span,
    event,
    event_log,
    job,
    kv_state,
    llm_usage_ledger,
    summary,
)
from papi.core.timeutil import now_utc
from papi.llm import governor
from papi.triage.classify import triage_disclosure
from tests.analyst.helpers import ScriptedBackend, contract_output, seed_disclosure


def _rows(db, table, *where):
    return db.execute(select(table).where(*where)).mappings().all()


def _published(db, type_):
    return [r.data for r in db.execute(select(event_log).where(event_log.c.type == type_)).all()]


def test_good_output_persists_analysis_claims_citations_summaries(db, world):
    backend = ScriptedBackend(analyst=[contract_output()])
    out = analyse_event(db, world.event_id, backend=backend)
    assert out.status == "analysed" and out.report.ok, out.report.issues
    assert backend.tasks() == ["analyst"]
    call = backend.calls[0]
    assert call["prompt"].startswith("PAPI BAĞLAM PAKETİ")
    assert "<<<UNTRUSTED_DISCLOSURE_TEXT" in call["prompt"] and "[S14]" in call["prompt"]
    assert call["system_prompt"] == analyst_spec().system_prompt
    assert "$schema" not in call["schema"] and call["schema"]["required"][0] == "event_codes"

    (a,) = _rows(db, analysis, analysis.c.event_id == world.event_id)
    spec = analyst_spec()
    assert a["agent"] == "analyst" and a["agent_version"] == spec.agent_version
    assert a["prompt_version"] == a["schema_version"] == "analyst.v1"
    assert a["input_hash"] == out.pack.input_hash and len(a["input_hash"]) == 64
    assert a["model_ids"] == ["claude-sonnet-test"] and a["backend"] == "claude_code_cli"
    assert a["cost_usd_est"] == pytest.approx(0.05) and a["latency_ms"] == 1500
    assert a["priority"] == "P1" and a["review_state"] == "auto" and a["confidence"] == pytest.approx(0.85)
    assert a["raw_output"]["structured_output"]["headline_tr"] == "MEKAG'a 1.325.000 EUR'luk sipariş"
    assert a["output"]["_papi"]["context"]["documents"][0]["role"] == "body"
    assert a["output"]["_papi"]["checker"]["state"] == "not_required"

    # ledger row is tied to the analysis
    (led,) = _rows(db, llm_usage_ledger)
    assert led["analysis_id"] == a["id"] and led["task"] == "analyst" and led["priority"] == "P1"

    claims = _rows(db, claim, claim.c.analysis_id == a["id"])
    assert [c["kind"] for c in claims] == ["fact", "fact", "inference"]
    assert all(c["verify_status"] == "pass" for c in claims)
    cits = _rows(db, citation, citation.c.claim_id == claims[0]["id"])
    assert [c["target_type"] for c in cits] == ["document_span"]
    span = (
        db.execute(select(document_span).where(document_span.c.id == cits[0]["target_id"])).mappings().one()
    )
    assert span["ordinal"] == 14 and "1.325.000" in span["text"]

    sums = {s["lang"]: s for s in _rows(db, summary, summary.c.event_id == world.event_id)}
    assert sums["tr"]["headline"] == "MEKAG'a 1.325.000 EUR'luk sipariş"
    assert sums["tr"]["body"].startswith("MEKAG, yurt içi") and sums["tr"]["analysis_id"] == a["id"]
    assert sums["en"]["body"].startswith("MEKAG received")

    ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert ev["payload_source"] == "analyst"  # triage had no payload for this form
    assert ev["payload"]["value"] == 1325000 and ev["payload"]["currency"] == "EUR"
    assert ev["payload"]["start_date"] == "2026-10-27"
    assert ev["materiality"] == 45 and ev["materiality_band"] == "medium"
    assert ev["extra"]["analyst"]["rules_materiality"] == 57
    assert ev["extra"]["payload_spans"]["value"] == [14]
    assert _published(db, ANALYSIS_COMPLETED) == [{"event_id": world.event_id, "analysis_ids": [a["id"]]}]


def test_idempotent_redelivery_and_same_input_hash(db, world):
    backend = ScriptedBackend(analyst=[contract_output()])
    analyse_event(db, world.event_id, backend=backend)
    again = analyse_event(db, world.event_id, backend=backend)
    assert again.status == "skipped" and again.reason == "already_analysed"
    assert backend.tasks() == ["analyst"]
    assert db.execute(select(func.count()).select_from(analysis)).scalar_one() == 1
    assert db.execute(select(func.count()).select_from(llm_usage_ledger)).scalar_one() == 1


def test_hallucinated_numbers_flag_and_drop(db, world):
    out = contract_output(
        summary_tr="MEKAG 2.000.000 EUR tutarında sipariş aldı. "
        "Şirket cironun olumlu etkileneceğini belirtti.",
        headline_tr="MEKAG'a 3 milyon EUR'luk sipariş",
        materiality_rationale="Tutar 1.325.000 EUR.",
    )
    out["claims"][0]["text_tr"] = "Sipariş tutarı 1.500.000 EUR."
    out["payload"]["business.contract.award"]["value"] = 1500000
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[out]))
    rep = res.report
    assert not rep.ok
    assert rep.claims[0].status == "fail" and "1.500.000" in rep.claims[0].issues[0]
    assert rep.payload_fields["business.contract.award"]["value"].startswith("1500000")
    (a,) = _rows(db, analysis, analysis.c.event_id == world.event_id)
    assert a["review_state"] == "flagged"
    verdicts = [c["verify_status"] for c in _rows(db, claim, claim.c.analysis_id == a["id"])]
    assert verdicts == ["fail", "pass", "pass"]
    tr = db.execute(select(summary).where(summary.c.event_id == world.event_id, summary.c.lang == "tr")).one()
    assert tr.body == "Şirket cironun olumlu etkileneceğini belirtti."  # hallucinated sentence dropped
    assert tr.headline.startswith("MEKAG")  # triage headline fallback
    assert "3 milyon" not in tr.headline
    ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert ev["payload"]["value"] is None  # untraced field not written
    assert ev["payload"]["currency"] == "EUR"
    # raw output keeps the model's text unchanged
    assert a["raw_output"]["structured_output"]["summary_tr"].startswith("MEKAG 2.000.000")
    assert a["output"]["summary_tr"] == "Şirket cironun olumlu etkileneceğini belirtti."


def test_missing_and_unresolvable_spans_fail_fact_claims(db, world):
    out = contract_output()
    out["claims"][0]["span_ordinals"] = []
    out["claims"][1]["span_ordinals"] = [999]
    out["claims"][2]["citation_ids"] = ["tr:spk:II-15.1:m99"]
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[out]))
    c0, c1, c2 = res.report.claims
    assert c0.status == "fail" and any("kaynak yok" in i for i in c0.issues)
    assert c1.status == "fail" and "çözümlenemeyen span S999" in c1.issues
    assert c2.status == "fail" and any("pakette olmayan KB" in i for i in c2.issues)
    (a,) = _rows(db, analysis, analysis.c.event_id == world.event_id)
    assert a["review_state"] == "flagged"
    cl = _rows(db, claim, claim.c.analysis_id == a["id"])
    assert not _rows(db, citation, citation.c.claim_id == cl[0]["id"])


def test_advice_language_flagged(db, world):
    out = contract_output(
        summary_tr="MEKAG 1.325.000 EUR sipariş aldı. Bu haberle hisseyi hemen satın alın.",
        summary_en="MEKAG received an order. Strong buy.",
        watch_next=["Hedef fiyat güncellemeleri"],
    )
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[out]))
    assert not res.report.ok
    phrases = {h["phrase"] for h in res.report.advice}
    assert {"satın alın", "strong buy/sell", "hedef fiyat"} <= phrases
    tr = db.execute(
        select(summary.c.body).where(summary.c.event_id == world.event_id, summary.c.lang == "tr")
    )
    assert tr.scalar_one() == "MEKAG 1.325.000 EUR sipariş aldı."
    (a,) = _rows(db, analysis, analysis.c.event_id == world.event_id)
    assert a["output"]["watch_next"] == [] and a["review_state"] == "flagged"


def test_schema_invalid_output_is_recorded_flagged_without_effects(db, world):
    bad = contract_output(materiality=150)
    del bad["summary_en"]
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[bad]))
    assert res.status == "invalid" and res.report.schema_errors
    (a,) = _rows(db, analysis, analysis.c.event_id == world.event_id)
    assert a["review_state"] == "flagged" and a["raw_output"]["structured_output"]["materiality"] == 150
    assert "headline_tr" not in a["output"] and a["output"]["_papi"]["verify"]["schema_errors"]
    assert not _rows(db, claim) and not _rows(db, summary)
    assert _published(db, ANALYSIS_COMPLETED) == []
    ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert ev["payload"] is None and ev["materiality"] == 57


def test_failed_call_is_permanent_and_timeout_retries(db, world):
    from papi.core.jobs import RetryLater
    from papi.llm.types import LlmResult

    def failed(kind):
        return LlmResult(None, None, [], 0.0, 10, 1, None, kind, f"{kind} boom")

    with pytest.raises(PermanentError):
        analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[failed("error"), failed("error")]))
    with pytest.raises(RetryLater):
        analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[failed("timeout")]))
    assert not _rows(db, analysis)
    assert not _rows(db, kv_state, kv_state.c.key.like("analyst:inflight:%"))


def test_deferred_propagates_and_releases_lock(db, world):
    governor.pause(db, now_utc() + timedelta(hours=2), reason="manual")
    db.commit()
    backend = ScriptedBackend(analyst=[contract_output()])
    with pytest.raises(Deferred) as info:
        analyse_event(db, world.event_id, backend=backend)
    assert info.value.reason.startswith("llm:paused")
    assert backend.calls == [] and not _rows(db, analysis)
    assert not _rows(db, kv_state, kv_state.c.key.like("analyst:inflight:%"))


def test_handler_skips_p4_and_superseded(db, world):
    db.execute(update(event).where(event.c.id == world.event_id).values(priority="P4"))
    db.commit()
    res = analyse_event(db, world.event_id, backend=ScriptedBackend())
    assert res.status == "skipped" and res.reason == "priority:P4"
    # the handler returns before touching the database for P4 classifications
    ev = CloudEvent(id=uuid7(), source="triage", type=EVENT_CLASSIFIED, time=now_utc(),
                    data={"event_id": world.event_id, "code": "x.y", "priority": "P4"})  # fmt: skip
    assert handlers.on_event_classified(ev, None) is None
    db.execute(
        update(event).where(event.c.id == world.event_id).values(priority="P1", superseded_by=world.event_id)
    )
    db.commit()
    assert analyse_event(db, world.event_id, backend=ScriptedBackend()).reason == "superseded"


def test_handler_registration_and_deferred_from_handler(db, world, monkeypatch):
    from papi.core import registry

    registry.restore(registry.Registry())
    import importlib

    importlib.reload(handlers)
    spec = registry.REGISTRY.handler_by_kind("event:analyst.analyse")
    assert spec is not None and spec.event_type == EVENT_CLASSIFIED and spec.max_attempts == 3
    assert CHECK_JOB in registry.REGISTRY.job_kinds
    governor.pause(db, now_utc() + timedelta(hours=1))
    db.commit()
    data = {"event_id": world.event_id, "code": "business.contract.award", "priority": "P1"}
    ev = CloudEvent(id=uuid7(), source="triage", type=EVENT_CLASSIFIED, time=now_utc(), data=data)
    with pytest.raises(Deferred):
        handlers.on_event_classified(ev, None)


def test_multi_event_disclosure_one_call_applied_to_all(db, world):
    """A second live event of the same disclosure gets its own analysis row from the same call."""
    base = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    second = uuid7()
    values = {k: v for k, v in base.items() if k not in ("id", "created_at", "code")}
    db.execute(event.insert().values(**values, id=second, code="business.capacity", created_at=now_utc()))
    db.commit()
    out = contract_output(event_codes=["business.contract.award", "business.capacity"])
    backend = ScriptedBackend(analyst=[out])
    res = analyse_event(db, world.event_id, backend=backend)
    assert res.status == "analysed" and res.event_ids == [world.event_id, second]
    rows = _rows(db, analysis)
    assert len(rows) == 2 and {r["event_id"] for r in rows} == {world.event_id, second}
    primary = next(r for r in rows if r["event_id"] == world.event_id)
    copy_ = next(r for r in rows if r["event_id"] == second)
    assert copy_["output"]["_papi"]["call_analysis_id"] == primary["id"] and copy_["cost_usd_est"] == 0.0
    assert copy_["input_hash"] == primary["input_hash"]
    assert len(_rows(db, claim, claim.c.analysis_id == copy_["id"])) == 3
    assert _rows(db, summary, summary.c.event_id == second)
    assert len(_published(db, ANALYSIS_COMPLETED)) == 2
    state = db.execute(
        select(kv_state.c.value).where(kv_state.c.key == f"analyst:disclosure:{world.disclosure_id}")
    )
    assert set(state.scalar_one()["analysis_ids"]) == {world.event_id, second}
    # the second event's own event.classified job is a no-op
    assert analyse_event(db, second, backend=backend).reason == "already_analysed"
    assert backend.tasks() == ["analyst"]
    # the analyst did not confirm business.capacity's payload, but confirmed its code
    ev2 = db.execute(select(event).where(event.c.id == second)).mappings().one()
    assert ev2["extra"]["analyst"]["code_confirmed"] is True


def test_checker_gating_and_unsupported_flags(db, world):
    out = contract_output(materiality=72)  # band high, P1 → checker
    checker_out = {"results": [
        {"claim_index": 0, "status": "supported", "reason": "S14'te aynen geçiyor"},
        {"claim_index": 1, "status": "unsupported", "reason": "tarih uyuşmuyor"},
    ]}  # fmt: skip
    backend = ScriptedBackend(analyst=[out], checker=[checker_out])
    res = analyse_event(db, world.event_id, backend=backend)
    assert backend.tasks() == ["analyst", "checker"] and res.checker == "done"
    prompt = backend.calls[1]["prompt"]
    assert (
        "### İddia 0 (fact)" in prompt and "[S14]" in prompt and "İddia 2" not in prompt
    )  # inference not checked
    a = db.execute(select(analysis).where(analysis.c.agent == "analyst")).mappings().one()
    assert a["review_state"] == "flagged" and a["output"]["_papi"]["checker"]["state"] == "done"
    statuses = [c["check_status"] for c in _rows(db, claim, claim.c.analysis_id == a["id"])]
    assert statuses == ["supported", "unsupported", None]
    chk = db.execute(select(analysis).where(analysis.c.agent == "checker")).mappings().one()
    assert chk["output"] == checker_out and chk["schema_version"] == "checker.v1"
    assert {r["task"] for r in _rows(db, llm_usage_ledger)} == {"analyst", "checker"}


@pytest.mark.parametrize("materiality", [45, 59])
def test_checker_not_called_below_high(db, world, materiality):
    backend = ScriptedBackend(analyst=[contract_output(materiality=materiality)])
    res = analyse_event(db, world.event_id, backend=backend)
    assert backend.tasks() == ["analyst"] and res.checker is None


def test_checker_not_called_for_p3(db, world, monkeypatch):
    from papi.llm import call as call_mod

    db.execute(update(event).where(event.c.id == world.event_id).values(priority="P3"))
    db.commit()
    monkeypatch.setattr(governor, "admit", lambda conn, task, priority: governor.Admission(True, None, "ok"))
    assert call_mod.governor.admit is governor.admit
    backend = ScriptedBackend(analyst=[contract_output(materiality=90)])
    res = analyse_event(db, world.event_id, backend=backend)
    assert res.status == "analysed" and backend.tasks() == ["analyst"]


def test_checker_deferred_enqueues_check_job_then_job_completes(db, world, monkeypatch):
    real_admit = governor.admit

    def admit(conn, task, priority, **kw):
        if task == "checker":
            return governor.Admission(False, now_utc() + timedelta(minutes=30), "cap:5h:P1")
        return real_admit(conn, task, priority, **kw)

    monkeypatch.setattr(governor, "admit", admit)
    backend = ScriptedBackend(analyst=[contract_output(materiality=85)])
    res = analyse_event(db, world.event_id, backend=backend)
    assert res.checker == "pending"
    assert len(_published(db, ANALYSIS_COMPLETED)) == 1  # published without waiting for the checker
    (j,) = _rows(db, job, job.c.kind == CHECK_JOB)
    assert j["payload"] == {"analysis_id": res.analysis_ids[0]} and j["priority"] == 10
    a = db.execute(select(analysis).where(analysis.c.id == res.analysis_ids[0])).mappings().one()
    assert a["output"]["_papi"]["checker"]["state"] == "pending"

    # still deferred inside the job → Deferred re-raised so the job is re-queued
    from papi.analyst.checker import run_for_call

    with pytest.raises(Deferred):
        run_for_call(db, res.analysis_ids, backend=backend, raise_deferred=True)
    monkeypatch.setattr(governor, "admit", real_admit)
    results = [
        {"claim_index": 0, "status": "supported", "reason": "ok"},
        {"claim_index": 1, "status": "partial", "reason": "kısmen"},
    ]
    backend.outputs["checker"] = [{"results": results}]
    assert run_for_call(db, res.analysis_ids, backend=backend, raise_deferred=True) == "done"
    a = db.execute(select(analysis).where(analysis.c.id == res.analysis_ids[0])).mappings().one()
    assert a["review_state"] == "auto"  # partial does not flag
    assert run_for_call(db, res.analysis_ids, backend=backend) == "done"  # not re-checked
    assert backend.tasks() == ["analyst", "checker"]


def test_code_override_replaces_unclassified(db, world):
    db.execute(update(event).where(event.c.id == world.event_id).values(code="other.unclassified"))
    db.commit()
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[contract_output()]))
    (new_id,) = res.created_event_ids
    old = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    new = db.execute(select(event).where(event.c.id == new_id)).mappings().one()
    assert old["superseded_by"] == new_id
    assert new["code"] == "business.contract.award" and new["extra"]["created_by"] == "analyst"
    assert new["payload"]["value"] == 1325000 and new["payload_source"] == "analyst"
    assert new["primary_disclosure_id"] == world.disclosure_id and new["priority"] == "P1"
    assert {r["event_id"] for r in _rows(db, analysis)} == {world.event_id, new_id}
    classified = _published(db, EVENT_CLASSIFIED)
    assert classified[-1]["event_id"] == new_id and classified[-1]["code"] == "business.contract.award"
    # the new event's own event.classified job is a no-op
    assert analyse_event(db, new_id, backend=ScriptedBackend()).reason == "already_analysed"


def test_low_confidence_codes_only_disputed(db, world):
    out = contract_output(event_codes=["business.capex"], confidence=0.4)
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[out]))
    assert res.created_event_ids == []
    ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert ev["extra"]["analyst"]["code_disputed"] is True
    assert ev["extra"]["analyst"]["suggested_codes"] == ["business.capex"]
    assert ev["superseded_by"] is None


def test_form_field_payload_stays_authoritative(db, world):
    """Dividend form (fixture 1688363): triage values win; analyst only fills empty fields."""
    did = seed_disclosure(db, index="1688363", issuer_id=world.issuer_id, source_ref="1688363")
    (eid,) = triage_disclosure(db, did)
    db.commit()
    rules = db.execute(select(event.c.payload).where(event.c.id == eid)).scalar_one()
    assert rules["gross_per_share"] == 3.07 and rules["total_gross_amount"] is None
    out = contract_output(
        event_codes=["distribution.dividend.cash"],
        payload={"distribution.dividend.cash": {"gross_per_share": 9.99, "period_year": 2025,
                                                "decision_body": "board", "total_gross_amount": None}},
        payload_spans={"distribution.dividend.cash": {"gross_per_share": [13]}},
        claims=[], summary_tr="FROTO nakit kâr payı dağıtım önerisi açıkladı.",
        summary_en="Dividend proposal.",
        headline_tr="Kâr payı önerisi", materiality_rationale="Temettü.", watch_next=[],
    )  # fmt: skip
    res = analyse_event(db, eid, backend=ScriptedBackend(analyst=[out]))
    # 9.99 is not in S13 but gross_per_share is fixed by the rules → not checked, and not applied
    assert "distribution.dividend.cash" not in res.report.payload_fields
    ev = db.execute(select(event).where(event.c.id == eid)).mappings().one()
    assert ev["payload"]["gross_per_share"] == 3.07
    assert ev["payload_source"] == "form_fields"  # nothing new from the analyst


def test_revision_creates_new_current_analysis_old_kept(db, world):
    analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[contract_output()]))
    corr = seed_disclosure(
        db, index="1688011", issuer_id=world.issuer_id, source_ref="1688999",
        revision_of=world.disclosure_id, revision_kind="correction", published_at="2026-10-27T17:00:00+03:00",
    )  # fmt: skip
    (new_eid,) = triage_disclosure(db, corr)
    db.commit()
    old_ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert old_ev["superseded_by"] == new_eid
    res = analyse_event(
        db, new_eid, backend=ScriptedBackend(analyst=[contract_output(headline_tr="Düzeltme")])
    )
    assert res.status == "analysed"
    old_a = _rows(db, analysis, analysis.c.event_id == world.event_id)
    new_a = _rows(db, analysis, analysis.c.event_id == new_eid)
    assert len(old_a) == 1 and len(new_a) == 1 and old_a[0]["input_hash"] != new_a[0]["input_hash"]
    heads = {r.event_id: r.headline for r in db.execute(select(summary).where(summary.c.lang == "tr"))}
    assert heads[new_eid] == "Düzeltme" and heads[world.event_id].startswith("MEKAG")
    # re-delivery for the superseded event does nothing
    assert analyse_event(db, world.event_id, backend=ScriptedBackend()).reason == "superseded"


def test_force_reanalysis_keeps_old_and_updates_summary(db, world):
    analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[contract_output()]))
    res = analyse_event(
        db,
        world.event_id,
        backend=ScriptedBackend(analyst=[contract_output(headline_tr="Yeni başlık")]),
        force=True,
    )
    rows = _rows(db, analysis, analysis.c.event_id == world.event_id)
    assert len(rows) == 2
    tr = db.execute(select(summary).where(summary.c.event_id == world.event_id, summary.c.lang == "tr")).one()
    assert tr.headline == "Yeni başlık" and tr.analysis_id == res.analysis_ids[0]


def test_reverify_updates_claims_from_raw_output(db, world):
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[contract_output()]))
    aid = res.analysis_ids[0]
    db.execute(update(claim).where(claim.c.analysis_id == aid).values(verify_status="fail"))
    db.execute(update(analysis).where(analysis.c.id == aid).values(review_state="flagged"))
    db.commit()
    rep = reverify(db, aid)
    assert rep.ok
    assert {c["verify_status"] for c in _rows(db, claim, claim.c.analysis_id == aid)} == {"pass"}
    assert db.execute(select(analysis.c.review_state).where(analysis.c.id == aid)).scalar_one() == "auto"


def test_dotted_payload_keys_are_normalised(db, world):
    """Models sometimes echo dotted schema paths as payload keys; they are expanded before checks."""
    out = contract_output()
    out["payload"]["business.contract.award"] = {
        "counterparty.name": "Yurt İçi Yerleşik Şirket", "counterparty.is_related_party": None,
        "value": 1325000, "currency": "EUR", "start_date": "2026-10-27", "ratios.value_to_market_cap": None,
    }  # fmt: skip
    res = analyse_event(db, world.event_id, backend=ScriptedBackend(analyst=[out]))
    assert res.report.ok, res.report.issues
    ev = db.execute(select(event).where(event.c.id == world.event_id)).mappings().one()
    assert ev["payload"]["counterparty"] == {"name": "Yurt İçi Yerleşik Şirket", "is_related_party": None}
    a = db.execute(select(analysis)).mappings().one()
    assert "counterparty.name" in a["raw_output"]["structured_output"]["payload"]["business.contract.award"]


def test_unflatten_lists():
    from papi.analyst.pipeline import unflatten

    flat = {"share_groups.0.group": "A", "share_groups.1.group": "B", "x": 1}
    assert unflatten(flat) == {"share_groups": [{"group": "A"}, {"group": "B"}], "x": 1}
