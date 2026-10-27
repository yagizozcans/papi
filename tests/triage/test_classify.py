from __future__ import annotations

import importlib
from typing import Any

import pytest
from sqlalchemy import select

from papi.core import jobs, registry
from papi.core.db import transaction
from papi.core.events import DISCLOSURE_NORMALIZED, DISCLOSURE_REVISED, EVENT_CLASSIFIED, get_event, publish
from papi.core.runtime import execute_job
from papi.core.schema import disclosure, document, event, event_log, job
from papi.triage.classify import classify, retriage_disclosure, triage_disclosure

from .helpers import form_field, kap_fixture_structure, seed_company, seed_disclosure, seed_holding

KP = "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim"


def kp_structure(gross: str = "2,5000000") -> dict[str, Any]:
    return {
        "template_id": "tbl_KP_Report_ID",
        "form_fields": [
            form_field("Özet Bilgi", "Kar Dağıtım Önerisi", 1),
            form_field("Para Birimi", "TRY", 2),
        ],
        "tables": [
            {
                "caption": "Nakit Kar Payı Ödeme Tutar ve Oranları",
                "header": [
                    "Pay Grup Bilgileri",
                    "1 TL Nominal Değerli Paya Ödenecek Nakit Kar Payı - Brüt(TL)",
                ],
                "rows": [["ABC, TRAABC", gross]],
                "span_ordinals": [[3, 4]],
            }
        ],
    }


def events_of(db, disclosure_id: str) -> list[Any]:
    return db.execute(
        select(event)
        .where(event.c.primary_disclosure_id == disclosure_id)
        .order_by(event.c.created_at, event.c.id)
    ).all()


def classified_jobs(db) -> list[Any]:
    return db.execute(select(job).where(job.c.kind == "event:test.capture").order_by(job.c.created_at)).all()


@pytest.fixture
def handlers(clean_registry):
    """Triage handlers + a capture handler on event.classified.v1 (fan-out target)."""
    import papi.triage.handlers as mod

    importlib.reload(mod)

    @registry.handles(EVENT_CLASSIFIED, name="test.capture")
    def capture(ev, ctx):  # pragma: no cover - only enqueued
        pass

    return mod


def test_single_event_fields_and_c5_extra(db, handlers):
    iss, inst = seed_company(db, "ABC")
    did = seed_disclosure(
        db, subject=KP, summary=None, issuer_ids=[iss], structure=kp_structure(), source_ref="9001"
    )
    ids = triage_disclosure(db, did)
    assert len(ids) == 1
    ev = events_of(db, did)[0]
    assert ev.code == "distribution.dividend.cash"
    assert ev.taxonomy_version == "tr-2026.10"
    assert (ev.issuer_ids, ev.instrument_ids) == ([iss], [inst])
    assert ev.status == "provisional"
    assert ev.priority == "P3"  # materiality 63 ≥ triage.p3_materiality_threshold (60)
    assert ev.payload["gross_per_share"] == 2.5
    assert (ev.payload_source, ev.payload_schema) == ("form_fields", "event.distribution.dividend.cash.v1")
    assert ev.materiality == round(55 * 1.15) and ev.materiality_band == "high"
    assert ev.announced_at.isoformat() == "2026-10-27T12:30:00+00:00"
    assert ev.disclosure_ids == [did]
    extra = ev.extra
    assert extra["headline_tr"] == "ABC: Nakit kâr payı — brüt 2,5 TL/pay"
    assert extra["tickers"] == ["ABC"]
    assert extra["rules"]["rule_ids"] == ["div_cash"]
    assert extra["rules"]["materiality"]["novelty"] == 1.15
    assert extra["payload_spans"]["gross_per_share"] == [4]
    # fan-out: one event.classified.v1 per event, job priority from the label
    log = db.execute(select(event_log).where(event_log.c.type == EVENT_CLASSIFIED)).all()
    assert [r.data for r in log] == [
        {
            "event_id": ids[0],
            "code": "distribution.dividend.cash",
            "materiality_band": "high",
            "priority": "P3",
            "disclosure_id": did,
        }
    ]
    assert [(j.priority, j.payload["event_id"]) for j in classified_jobs(db)] == [
        (jobs.PRIORITY_P3, log[0].id)
    ]


def test_rerun_is_idempotent(db, handlers):
    iss, _ = seed_company(db, "ABC")
    did = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure())
    first = triage_disclosure(db, did)
    assert triage_disclosure(db, did) == first
    assert len(events_of(db, did)) == 1
    assert len(classified_jobs(db)) == 1


def test_multi_event_from_subject_rules(db, handlers):
    iss, _ = seed_company(db, "MULT")
    did = seed_disclosure(
        db, subject=KP, summary="Kâr payı dağıtımı ve pay geri alım programı hakkında", issuer_ids=[iss],
        structure=kp_structure(),
    )  # fmt: skip
    triage_disclosure(db, did)
    evs = events_of(db, did)
    assert [e.code for e in evs] == ["buyback.program.start", "distribution.dividend.cash"]
    assert all(e.primary_disclosure_id == did for e in evs)
    assert evs[0].payload is None and evs[0].extra["payload_error"] == "no_fields_matched"
    assert evs[1].payload["gross_per_share"] == 2.5
    assert len(classified_jobs(db)) == 2


def test_insider_trade_derives_threshold_crossing(db, handlers):
    iss, _ = seed_company(db, "THR")
    s = {
        "form_fields": [],
        "tables": [
            {
                "header": [
                    "İşlem Tarihi",
                    "Alım İşlemine Konu Payların Toplam Nominal Tutarı (TL)",
                    "Satım İşlemine Konu Payların Toplam Nominal Tutarı (TL)",
                    "Sahip Olunan Payların Gün Başı Bakiyesinin Sermayeye Oranı (%)",
                    "Sahip Olunan Payların Gün Sonu Bakiyesinin Sermayeye Oranı (%)",
                ],
                "rows": [["27.10.2026", "1.000.000", "0", "% 9,5", "% 10,4"]],
            }
        ],
    }
    did = seed_disclosure(
        db, subject="Pay Alım Satım Bildirimi", summary="Pay Alım Bildirimi", issuer_ids=[iss], structure=s
    )
    triage_disclosure(db, did)
    evs = events_of(db, did)
    assert [e.code for e in evs] == ["ownership.insider_trade", "ownership.threshold_crossing"]
    crossing = evs[1]
    assert crossing.payload["threshold_pct"] == 10.0 and crossing.payload["direction"] == "up"
    assert crossing.extra["rules"]["derived_from"] == "ownership.insider_trade"
    assert crossing.extra["headline_tr"] == "THR: Pay oranı eşik aşımı — %10 eşiğinin üzerine çıktı"
    assert evs[0].occurred_at.isoformat() == "2026-10-26T21:00:00+00:00"  # 27.10 00:00 Istanbul


def test_unclassified_headline_uses_kap_summary(db, handlers):
    iss, _ = seed_company(db, "UNK")
    did = seed_disclosure(
        db, subject="Özel Durum Açıklaması (Genel)", summary="Zyxel performans ödülü", issuer_ids=[iss]
    )
    triage_disclosure(db, did)
    ev = events_of(db, did)[0]
    assert ev.code == "other.unclassified"
    assert ev.payload is None and ev.payload_source is None
    assert ev.extra["headline_tr"] == "UNK: Özel Durum Açıklaması (Genel) — Zyxel performans ödülü"


def test_p1_for_held_instrument_and_catchup(db, handlers):
    pytest.importorskip("papi.portfolio.service")
    iss, inst = seed_company(db, "HOLD")
    seed_holding(db, inst)
    did = seed_disclosure(db, subject="Yeni İş İlişkisi", summary="Yeni iş ilişkisi", issuer_ids=[iss])
    triage_disclosure(db, did, is_catchup=True)
    ev = events_of(db, did)[0]
    assert (ev.code, ev.priority) == ("business.contract.award", "P1")
    assert ev.extra["rules"]["priority"] == {"label": "P1", "reason": "holding", "holding": "P1"}
    assert ev.extra["is_catchup"] is True
    assert ev.extra["rules"]["materiality"]["catchup"] is True
    assert ev.materiality == round(50 * 1.15)  # catch-up is neutral
    assert classified_jobs(db)[0].priority == jobs.PRIORITY_P1


def test_market_wide_issuer_resolution_and_stock_codes(db, handlers):
    bist, _ = seed_company(db, "BIST", kind="exchange")
    iss, inst = seed_company(db, "CIRC")
    did = seed_disclosure(
        db, subject="Pay Bazında Devre Kesici Bildirimi", summary="CIRC.E işlem sırasında devre kesici",
        filer_issuer_id=bist, issuer_ids=[], source_class="DKB", list_meta={"stockCodes": "CIRC"},
    )  # fmt: skip
    triage_disclosure(db, did)
    ev = events_of(db, did)[0]
    assert ev.code == "market.circuit_breaker"
    assert ev.instrument_ids == [inst]
    assert ev.extra["tickers"] == ["CIRC"]
    did2 = seed_disclosure(
        db,
        subject="Sermaye Piyasası Kurulu Tedbir Kararı",
        filer_issuer_id=bist,
        issuer_ids=[iss],
        source_class="DKB",
    )
    triage_disclosure(db, did2)
    ev2 = events_of(db, did2)[0]
    assert ev2.issuer_ids == [iss]  # the exchange is not the subject
    assert (ev2.code, ev2.priority) == ("market.measure", "P3")


def test_revision_supersedes_previous_events(db, handlers):
    iss, _ = seed_company(db, "REV")
    old = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure("2,5"), source_ref="100")
    old_ids = triage_disclosure(db, old)
    new = seed_disclosure(
        db, subject=KP, issuer_ids=[iss], structure=kp_structure("3,0"), source_ref="101", revision_of=old,
        revision_kind="correction",
    )  # fmt: skip
    new_ids = triage_disclosure(db, new)
    old_ev = db.execute(select(event).where(event.c.id == old_ids[0])).one()
    new_ev = db.execute(select(event).where(event.c.id == new_ids[0])).one()
    assert old_ev.superseded_by == new_ev.id
    assert new_ev.superseded_by is None
    assert new_ev.disclosure_ids == [new, old]
    assert new_ev.payload["gross_per_share"] == 3.0
    assert new_ev.extra["rules"]["materiality"]["flag_names"] == ["correction"]
    assert new_ev.extra["rules"]["materiality"]["novelty"] == 1.15  # the revised original doesn't count


def test_revision_arriving_first_supersedes_late_original(db, handlers):
    iss, _ = seed_company(db, "OOO")
    old = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure(), source_ref="200")
    new = seed_disclosure(
        db, subject=KP, issuer_ids=[iss], structure=kp_structure("3,0"), source_ref="201", revision_of=old,
        revision_kind="correction",
    )  # fmt: skip
    new_ids = triage_disclosure(db, new)
    old_ids = triage_disclosure(db, old)
    old_ev = db.execute(select(event).where(event.c.id == old_ids[0])).one()
    assert old_ev.superseded_by == new_ids[0]
    published = [j.payload for j in classified_jobs(db)]
    assert len(published) == 1  # the stale original is not announced


def test_update_follow_up_does_not_supersede(db, handlers):
    iss, _ = seed_company(db, "UPD")
    gai = "Payların Geri Alınmasına İlişkin Bildirim"
    programme = seed_disclosure(
        db, subject=gai, summary="Pay geri alım programı başlatılması", issuer_ids=[iss], source_ref="400"
    )
    prog_ids = triage_disclosure(db, programme)
    daily = seed_disclosure(
        db, subject=gai, summary="27.10.2026 tarihli pay geri alım işlemleri", issuer_ids=[iss],
        source_ref="401", revision_of=programme, revision_kind="update",
    )  # fmt: skip
    triage_disclosure(db, daily)
    assert db.execute(select(event.c.superseded_by).where(event.c.id == prog_ids[0])).scalar_one() is None
    ev = events_of(db, daily)[0]
    assert ev.code == "buyback.execution"
    assert ev.disclosure_ids == [daily, programme]
    assert ev.extra["rules"]["materiality"]["flag_names"] == []


def test_reparse_replaces_events_only_when_result_changes(db, handlers):
    iss, _ = seed_company(db, "RPS")
    did = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure("2,0"))
    first = triage_disclosure(db, did)
    assert retriage_disclosure(db, did) == first  # unchanged → nothing new
    assert len(classified_jobs(db)) == 1
    doc_id = db.execute(select(disclosure.c.body_document_id).where(disclosure.c.id == did)).scalar_one()
    db.execute(document.update().where(document.c.id == doc_id).values(structure=kp_structure("2,2")))
    second = retriage_disclosure(db, did)
    assert second != first
    old = db.execute(select(event).where(event.c.id == first[0])).one()
    assert old.superseded_by == second[0]
    live = [e for e in events_of(db, did) if e.superseded_by is None]
    assert [e.payload["gross_per_share"] for e in live] == [2.2]
    assert len(classified_jobs(db)) == 2


def test_classify_is_read_only(db):
    iss, _ = seed_company(db, "DRY")
    did = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure())
    drafts = classify(db, did)
    assert [d.code for d in drafts] == ["distribution.dividend.cash"]
    assert events_of(db, did) == []


def test_kap_fixture_rights_issue_end_to_end(db, handlers):
    structure, basic = kap_fixture_structure("1687750")
    iss, _ = seed_company(db, "CEMAS")
    did = seed_disclosure(db, subject=basic["title"], summary=None, issuer_ids=[iss], structure=structure)
    triage_disclosure(db, did)
    ev = events_of(db, did)[0]
    assert ev.code == "capital.increase.rights"
    assert ev.payload["rights_rate_pct"] == 100.0
    assert ev.extra["headline_tr"] == "CEMAS: Bedelli sermaye artırımı — %100 bedelli, 1 TL/pay"
    assert ev.extra["rules"]["materiality"]["size_basis"] == "rate=100%"


# ------------------------------------------------------------------------------------- handler / worker


async def _run_job(kind: str) -> str:
    leased = jobs.lease("test-worker", kinds=[kind])
    assert leased is not None
    return await execute_job(leased, "test-worker")


async def test_handler_via_worker_normalized_and_revised(db, handlers):
    iss, _ = seed_company(db, "WRK")
    old = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure(), source_ref="300")
    db.commit()
    with transaction() as conn:
        publish(
            conn,
            DISCLOSURE_NORMALIZED,
            f"disclosure/{old}",
            {"disclosure_id": old, "is_catchup": False},
            "ingest/kap",
        )
    j = db.execute(select(job).where(job.c.kind == "event:triage.disclosure_normalized")).one()
    assert j.priority == jobs.PRIORITY_P1
    assert await _run_job("event:triage.disclosure_normalized") == "done"
    db.rollback()  # drop the read snapshot taken before the worker wrote

    new = seed_disclosure(
        db, subject=KP, issuer_ids=[iss], structure=kp_structure("4,0"), source_ref="301", revision_of=old,
        revision_kind="correction",
    )  # fmt: skip
    db.commit()
    with transaction() as conn:
        data = {"disclosure_id": new, "revision_of": old, "revision_kind": "correction"}
        publish(conn, DISCLOSURE_REVISED, f"disclosure/{new}", data, "ingest/kap")
        publish(
            conn,
            DISCLOSURE_NORMALIZED,
            f"disclosure/{new}",
            {"disclosure_id": new, "is_catchup": True},
            "ingest/kap",
        )
    catchup_job = db.execute(
        select(job).where(job.c.kind == "event:triage.disclosure_normalized", job.c.status == "queued")
    ).one()
    assert catchup_job.priority == jobs.PRIORITY_BACKFILL
    assert await _run_job("event:triage.disclosure_revised") == "done"
    assert await _run_job("event:triage.disclosure_normalized") == "done"  # no-op, events already exist
    db.rollback()
    old_ev, new_ev = events_of(db, old)[0], events_of(db, new)[0]
    assert old_ev.superseded_by == new_ev.id
    assert len(events_of(db, new)) == 1
    assert len(classified_jobs(db)) == 2
    ev = get_event(db, classified_jobs(db)[1].payload["event_id"])
    assert ev.data["event_id"] == new_ev.id and ev.source == "triage"


async def test_handler_missing_disclosure_is_permanent(db, handlers):
    with transaction() as conn:
        publish(conn, DISCLOSURE_NORMALIZED, "disclosure/x", {"disclosure_id": "missing"}, "ingest/kap")
    assert await _run_job("event:triage.disclosure_normalized") == "dead"


async def test_handler_reparse_revision_reclassifies_same_disclosure(db, handlers):
    iss, _ = seed_company(db, "RPH")
    did = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=kp_structure("1,0"))
    first = triage_disclosure(db, did)
    doc_id = db.execute(select(disclosure.c.body_document_id).where(disclosure.c.id == did)).scalar_one()
    db.execute(document.update().where(document.c.id == doc_id).values(structure=kp_structure("1,5")))
    db.commit()
    with transaction() as conn:
        data = {"disclosure_id": did, "revision_of": did, "revision_kind": "reparse"}
        publish(conn, DISCLOSURE_REVISED, f"disclosure/{did}", data, "ingest/kap")
    assert await _run_job("event:triage.disclosure_revised") == "done"
    db.rollback()
    live = [e for e in events_of(db, did) if e.superseded_by is None]
    assert [e.payload["gross_per_share"] for e in live] == [1.5]
    assert db.execute(select(event.c.superseded_by).where(event.c.id == first[0])).scalar_one() == live[0].id
