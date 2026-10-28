from __future__ import annotations

import copy
import importlib
import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from papi.core import events, fts, jobs
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.metrics import series
from papi.core.schema import disclosure, document, document_span, event_log, identifier, job, trading_calendar
from papi.core.state import get_state, set_state
from papi.core.timeutil import parse_iso
from papi.sources.kap import cadence, gaps, normalize
from papi.sources.kap.client import KapBlocked, KapHttpError
from papi.sources.kap.common import JOB_FETCH, JOB_PROBE, kap_source_id
from papi.sources.kap.companies import apply_companies, resolve_tickers
from papi.sources.kap.discovery import BACKFILL_KEY, LAST_POLL_OK_KEY, backfill, poll_once, reconcile
from papi.sources.kap.drift import DRIFT_KEY, UNKNOWN_TEMPLATES_KEY
from papi.sources.kap.ingest import insert_seen, process_disclosure, record_listing, reprioritize_queued
from papi.sources.kap.reprocess import refresh_disclosure, reparse

from .conftest import LISTED, detail_json, fixture_list, fund_items, list_items, load_json

NOW = parse_iso("2026-10-28T08:00:00Z")  # Wednesday 11:00 Istanbul
D0 = date(2026, 10, 28)
TICKERS = ["PASEU", "MTRYO", "METRO", "SAHOL", "FROTO", "MHRGY", "ANSGR", "KORDS", "TATEN", "CEMAS", "PSGYO",
           "MEKAG", "KONKA"]  # fmt: skip


def seed_companies(db: Any) -> None:
    items = [
        c
        for c in load_json("companies_IGS.json.gz")
        if any(t in [s.strip() for s in (c.get("stockCode") or "").split(",")] for t in TICKERS)
    ]
    apply_companies(db, items, today=D0)
    db.commit()


def inst(db: Any, ticker: str) -> str:
    return resolve_tickers(db, [ticker], D0)[ticker][0]


def row(db: Any, ref: int) -> Any:
    db.rollback()
    return db.execute(select(disclosure).where(disclosure.c.source_ref == str(ref))).one()


def events_of(db: Any, etype: str) -> list[Any]:
    db.rollback()
    return db.execute(select(event_log).where(event_log.c.type == etype).order_by(event_log.c.time)).all()


def fetch_jobs(db: Any) -> dict[str, Any]:
    db.rollback()
    rows = db.execute(select(job).where(job.c.kind == JOB_FETCH)).all()
    return {r.payload["ref"]: r for r in rows}


# ------------------------------------------------------------------------------------------ discovery


def polled_recently(db: Any) -> None:
    set_state(db, LAST_POLL_OK_KEY, (NOW - timedelta(seconds=60)).isoformat())
    db.commit()


def test_first_poll_ever_treats_window_as_backlog(db, kap_deps, client) -> None:
    report = poll_once(db, client, now=NOW)
    assert report.mode == "catchup" and report.catchup_gap_s is None
    assert report.window == (date(2026, 10, 27), D0)
    assert all(n["is_catchup"] for n in report.new)
    assert series(db, "kap.latency_s") == []


def test_poll_diffs_and_is_idempotent_with_holdings_priority(db, kap_deps, client) -> None:
    seed_companies(db)
    polled_recently(db)
    kap_deps.held = {inst(db, "METRO"): "P1", inst(db, "FROTO"): "P2"}
    report = poll_once(db, client, now=NOW)
    assert report.mode == "live" and report.window == (date(2026, 10, 27), D0)
    assert report.listed == len(LISTED) and len(report.new) == len(LISTED)
    js = fetch_jobs(db)
    assert set(js) == {str(i) for i in LISTED}
    assert js["1688533"].priority == jobs.PRIORITY_P1  # MTRYO sold METRO shares → METRO held
    assert js["1688533"].payload["priority"] == "P1"
    assert js["1688363"].priority == js["1688368"].priority == jobs.PRIORITY_P2  # FROTO watched
    assert js["1688616"].priority == 50
    assert len(events_of(db, events.DISCLOSURE_SEEN)) == len(LISTED)
    r = row(db, 1688616)
    assert (
        r.ingest_status == "seen" and r.source_class == "ODA" and r.published_at_raw == "28.10.2026 01:39:36"
    )
    assert r.published_at == parse_iso("2026-10-27T22:39:36Z")
    assert get_state(db, LAST_POLL_OK_KEY) == NOW.isoformat()
    assert get_state(db, "kap:last_index") == max(LISTED)
    assert series(db, "kap.latency_s")

    again = poll_once(db, client, now=NOW + timedelta(seconds=30))
    assert again.mode == "live" and again.new == [] and again.listed == len(LISTED)
    db.rollback()
    assert db.execute(select(func.count()).select_from(disclosure)).scalar() == len(LISTED)
    assert len(fetch_jobs(db)) == len(LISTED)
    assert len(events_of(db, events.DISCLOSURE_SEEN)) == len(LISTED)


def test_wake_catchup_marks_items_and_lowers_priority(db, kap_deps, client) -> None:
    set_state(db, LAST_POLL_OK_KEY, (NOW - timedelta(hours=8)).isoformat())
    db.commit()
    report = poll_once(db, client, now=NOW)
    assert report.mode == "catchup" and report.catchup_gap_s == 8 * 3600
    assert all(n["is_catchup"] for n in report.new)
    js = fetch_jobs(db)
    assert {j.priority for j in js.values()} == {55}
    assert all(j.payload["is_catchup"] for j in js.values())
    assert row(db, 1688616).extra["is_catchup"] is True
    out = process_disclosure(db, client, "1688616")
    (ev,) = events_of(db, events.DISCLOSURE_NORMALIZED)
    assert ev.data["is_catchup"] is True and out.result.event_id == ev.id


def test_live_items_published_after_last_poll_are_not_catchup(db, kap_deps, client, fake_kap) -> None:
    set_state(db, LAST_POLL_OK_KEY, (NOW - timedelta(minutes=20)).isoformat())
    db.commit()
    fresh = copy.deepcopy(fixture_list([1688616])[0])
    fresh["disclosureIndex"] = 1688700
    fresh["publishDate"] = "28.10.2026 10:59:30"  # 30 s before NOW (Istanbul time)
    fake_kap.items.append(fresh)
    report = poll_once(db, client, now=NOW)
    assert report.mode == "catchup"
    by_ref = {n["ref"]: n for n in report.new}
    assert by_ref["1688700"]["is_catchup"] is False
    assert by_ref["1688616"]["is_catchup"] is True


# ------------------------------------------------------------------------------------------ fetch/parse


def test_full_fixture_set_normalizes_with_documents_and_events(db, kap_deps, client, fake_kap) -> None:
    seed_companies(db)
    polled_recently(db)
    poll_once(db, client, now=NOW)
    for ref in sorted(LISTED):
        out = process_disclosure(db, client, str(ref))
        assert out.status == "normalized", ref
    db.rollback()
    statuses = db.execute(
        select(disclosure.c.ingest_status, func.count()).group_by(disclosure.c.ingest_status)
    ).all()
    assert statuses == [("normalized", len(LISTED))]

    paseu = row(db, 1688616)
    assert paseu.languages == ["tr", "en"] and paseu.title == "Özel Durum Açıklaması (Genel)"
    assert paseu.summary == "Yatırımcılarımızı Bilgilendirme"
    assert paseu.source_uid == "4028328ca17e4f5e01a17f5dd70c0caa"
    assert paseu.extra["issuer_resolution"] == "resolved" and paseu.extra["tickers"] == ["PASEU"]
    assert paseu.filer_issuer_id == resolve_tickers(db, ["PASEU"], D0)["PASEU"][1]
    docs = db.execute(select(document).where(document.c.disclosure_id == paseu.id)).all()
    by_lang = {d.language: d for d in docs if d.kind == "disclosure_body"}
    assert set(by_lang) == {"tr", "en"} and paseu.body_document_id == by_lang["tr"].id
    assert by_lang["en"].extra["translation_of"] == by_lang["tr"].id
    assert by_lang["tr"].structure["template_id"] == "tbl_oda-10000_Material-Event-Disclosure-General"
    assert by_lang["tr"].raw_sha256 == paseu.extra["raw"]["detail"]
    spans = db.execute(
        select(document_span)
        .where(document_span.c.document_id == by_lang["tr"].id)
        .order_by(document_span.c.ordinal)
    ).all()
    assert [s.ordinal for s in spans] == list(range(1, len(spans) + 1))
    assert all(by_lang["tr"].text[s.char_start : s.char_end] == s.text for s in spans)

    mtryo = row(db, 1688533)
    issuers = resolve_tickers(db, ["MTRYO", "METRO"], D0)
    assert mtryo.filer_issuer_id == issuers["MTRYO"][1]
    assert mtryo.related_issuer_ids == [issuers["MTRYO"][1], issuers["METRO"][1]]
    assert set(mtryo.extra["instrument_ids"]) == {issuers["MTRYO"][0], issuers["METRO"][0]}

    dkb = row(db, 1688272)  # KAP-relayed insider trade with a Java-wrapped PDF
    assert dkb.source_class == "DKB" and dkb.extra["detail_class"] == "DUY"
    assert dkb.filer_issuer_id is None and dkb.related_issuer_ids == [issuers_of(db, "MHRGY")]
    assert dkb.extra["issuer_resolution"] == "resolved"
    (att,) = db.execute(select(document).where(document.c.id.in_(dkb.attachment_document_ids))).all()
    assert att.kind == "attachment" and att.mime == "application/pdf" and att.language == "tr"
    assert att.extra["obj_id"] == "4028328ca16c7de401a17de5e8772c7f"
    assert att.extra["file_name"] == "MHRGY_56659.pdf"
    page_spans = db.execute(select(document_span).where(document_span.c.document_id == att.id)).all()
    assert page_spans and all(s.page == 1 and s.field_path.startswith("pdf:1:") for s in page_spans)

    normalized = {e.data["source_ref"]: e for e in events_of(db, events.DISCLOSURE_NORMALIZED)}
    revised = {e.data["source_ref"]: e for e in events_of(db, events.DISCLOSURE_REVISED)}
    assert set(normalized) | set(revised) == {str(i) for i in LISTED}
    assert set(revised) == {"1688576"}  # ANSGR financial report correction of 1688379
    assert normalized["1688616"].data["related_issuer_ids"] == [paseu.filer_issuer_id]
    assert normalized["1688616"].data["is_catchup"] is False

    hits = fts.search(db, "disclosure_fts", "Corpus Sigorta")
    assert dkb.id in {h.id for h in hits}
    assert series(db, "kap.normalize_latency_s")


def issuers_of(db: Any, ticker: str) -> str:
    return resolve_tickers(db, [ticker], D0)[ticker][1]


def test_revision_linkage(db, kap_deps, client) -> None:
    seed_companies(db)
    record_listing(db, fixture_list([1688379, 1688576, 1688545, 1688504]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688379")
    process_disclosure(db, client, "1688576")
    orig, corr = row(db, 1688379), row(db, 1688576)
    assert corr.revision_of == orig.id and corr.revision_kind == "correction"
    assert corr.extra["revision"]["related_uid"] == orig.source_uid
    assert corr.extra["revision"]["related_ref"] == "1688379"
    assert orig.extra["superseded_by"] == corr.id
    assert orig.revision_kind == "original" and orig.extra["revision"]["corrected_later"] is True
    (ev,) = events_of(db, events.DISCLOSURE_REVISED)
    assert ev.data["revision_of"] == orig.id and ev.data["revision_kind"] == "correction"

    process_disclosure(db, client, "1688545")  # TATEN correction of a disclosure we never saw
    taten = row(db, 1688545)
    assert taten.revision_kind == "correction" and taten.revision_of is None
    assert taten.extra["revision_pending"] is True and taten.extra["revision"]["correction_flag"] is True
    process_disclosure(db, client, "1688504")  # SAHOL daily buyback: update of the programme announcement
    sahol = row(db, 1688504)
    assert sahol.revision_kind == "update" and sahol.extra["revision"]["update_flag"] is True
    ev = {e.data["source_ref"]: e for e in events_of(db, events.DISCLOSURE_NORMALIZED)}
    assert ev["1688545"].data["revision_kind"] == "correction"
    assert ev["1688545"].data["revision_of_uid"] == "4028328da0b8438901a1642d0ac94c2a"


def test_isin_harvested_from_body(db, kap_deps, client) -> None:
    seed_companies(db)
    record_listing(db, fixture_list([1688504]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688504")
    db.rollback()
    isin = db.execute(select(identifier).where(identifier.c.scheme == "ISIN")).one()
    assert isin.value == "TRASAHOL91Q5" and isin.entity_id == inst(db, "SAHOL")


def test_blocked_disclosure_and_takedown_on_refresh(db, kap_deps, client, fake_kap) -> None:
    blocked = copy.deepcopy(detail_json(1688616))
    blocked[0]["disclosure"]["disclosureBasic"]["isBlocked"] = True
    fake_kap.details[1688616] = blocked
    record_listing(db, fixture_list([1688616, 1688011]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688616")
    r = row(db, 1688616)
    assert r.revision_kind == "blocked" and r.extra["blocked"] is True
    (ev,) = events_of(db, events.DISCLOSURE_BLOCKED)
    assert ev.data["disclosure_id"] == r.id

    process_disclosure(db, client, "1688011")
    assert refresh_disclosure(db, client, "1688011") == "unchanged"
    fake_kap.details[1688011] = None  # vanished from KAP (HTTP 500 success:false)
    assert refresh_disclosure(db, client, "1688011") == "removed"
    assert row(db, 1688011).extra["blocked_reason"] == "removed"
    assert len(events_of(db, events.DISCLOSURE_BLOCKED)) == 2


def test_attachment_policy_prioritized(db, kap_deps, client, fake_kap, monkeypatch) -> None:
    monkeypatch.setattr(get_settings().kap, "attachments_fetch_policy", "prioritized")
    seed_companies(db)
    record_listing(db, fixture_list([1688272, 1687750]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688272")
    assert not [c for c in fake_kap.calls if c.startswith("file:")]
    assert row(db, 1688272).extra["raw"]["files"][0]["skipped"] == "policy"
    process_disclosure(db, client, "1687750", label="P1")
    assert [c for c in fake_kap.calls if c.startswith("file:")] == ["file:4028328da16c7fe601a17c0e2c30564f"]


def test_unknown_template_is_flagged(db, kap_deps, client, fake_kap) -> None:
    d = copy.deepcopy(detail_json(1688616))
    d[0]["disclosureBody"] = [
        d[0]["disclosureBody"][0].replace(
            "tbl_oda-10000_Material-Event-Disclosure-General", "tbl_oda-99999_Brand-New"
        )
    ]
    fake_kap.details[1688616] = d
    record_listing(db, fixture_list([1688616]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688616")
    r = row(db, 1688616)
    assert r.extra["template_unknown"] is True and r.extra["template_id"] == "tbl_oda-99999_Brand-New"
    unknown = get_state(db, UNKNOWN_TEMPLATES_KEY)
    assert unknown["tbl_oda-99999_Brand-New"]["count"] == 1
    assert unknown["tbl_oda-99999_Brand-New"]["example_ref"] == "1688616"
    assert series(db, "kap.template_unknown")[0]["labels"] == {"template_id": "tbl_oda-99999_Brand-New"}
    body = db.execute(select(document.c.extra).where(document.c.id == r.body_document_id)).scalar_one()
    assert body["template_known"] is False


def test_schema_drift_alerts(db, kap_deps) -> None:
    items = fixture_list([1688616, 1688533])
    broken = copy.deepcopy(items)
    del broken[0]["subject"]
    broken[1]["brandNewKey"] = 1
    record_listing(db, broken, mode="live", now=NOW)
    db.commit()
    st = get_state(db, DRIFT_KEY)
    assert st["list_item"]["missing"] == ["subject"] and st["list_item"]["unknown"] == ["brandNewKey"]
    assert [a["dedupe_key"] for a in kap_deps.alerts] == ["kap:drift:list_item:subject"]
    assert series(db, "kap.drift")[0]["labels"] == {"kind": "list_item", "missing": "subject"}


def test_reparse_emits_revised_only_when_output_changes(db, kap_deps, client, monkeypatch) -> None:
    record_listing(db, fixture_list([1688616, 1688504]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688616")
    process_disclosure(db, client, "1688504")
    stats = reparse(db, date(2026, 10, 27), D0)
    assert stats["checked"] == 2 and stats["changed"] == 0 and stats["unchanged"] == 2
    assert events_of(db, events.DISCLOSURE_REVISED) == []

    real = normalize.parse_body

    def patched(parts: list[str], *, language: str = "tr") -> Any:
        doc = real(parts, language=language)
        if "PASEU" in doc.text:
            doc.structure["paragraphs"].append({"text": "yeni ayrıştırıcı", "span_ordinal": None})
        return doc

    monkeypatch.setattr(normalize, "parse_body", patched)
    stats = reparse(db, date(2026, 10, 27), D0)
    assert stats["changed"] == 1 and stats["unchanged"] == 1
    (ev,) = events_of(db, events.DISCLOSURE_REVISED)
    r = row(db, 1688616)
    assert ev.data["disclosure_id"] == r.id and ev.data["revision_kind"] == "reparse"
    docs = db.execute(select(document.c.id, document.c.extra).where(document.c.disclosure_id == r.id)).all()
    superseded = [d for d in docs if d.extra.get("superseded_at")]
    assert len(superseded) == 2 and len(docs) == 4  # old TR+EN superseded, new TR+EN live
    assert r.body_document_id not in {d.id for d in superseded}
    only = reparse(db, date(2026, 10, 27), D0, template="tbl_GAI_Report_ID")
    assert only["checked"] == 1


# ------------------------------------------------------------------------------------------ gaps


def _seed_refs(db: Any, refs: list[int]) -> None:
    sid = kap_source_id(db)
    for ref in refs:
        insert_seen(
            db,
            sid,
            {"disclosureIndex": ref, "publishDate": "27.10.2026 20:00:00", "subject": "x"},
            now=NOW,
            is_catchup=False,
            mode="live",
        )
    db.commit()


def test_gap_detection_probe_skip_and_give_up(db, kap_deps, client) -> None:
    _seed_refs(db, [1688551, 1688554])
    rep = gaps.detect_gaps(db, now=NOW)
    db.commit()
    assert rep.new_missing == [1688552, 1688553] and rep.open == 2 and rep.enqueued == 2
    (ev,) = events_of(db, events.INGEST_GAP_DETECTED)
    assert ev.data["missing_refs"] == ["1688552", "1688553"]
    probes = db.execute(select(job.c.payload).where(job.c.kind == JOB_PROBE)).scalars().all()
    assert sorted(p["index"] for p in probes) == [1688552, 1688553]

    assert gaps.probe_index(db, client, 1688553, now=NOW) == "skipped"  # fund disclosure
    t = NOW
    outcomes = []
    for _ in range(gaps.MAX_TRIES):
        outcomes.append(gaps.probe_index(db, client, 1688552, now=t))
        t += timedelta(hours=6)
    assert outcomes == ["missing"] * (gaps.MAX_TRIES - 1) + ["dead"]
    rep = gaps.detect_gaps(db, now=t)
    db.commit()
    assert rep.open == 0 and rep.watermark == 1688554 and rep.new_missing == []
    st = gaps.load_state(db)
    assert st["skipped"] == [] and st["dead"] == []  # pruned below the watermark
    assert series(db, "kap.gaps.open")[0]["value"] == 0


def test_gap_probe_ingests_in_scope_disclosure_as_catchup(db, kap_deps, client) -> None:
    _seed_refs(db, [1688614, 1688617])
    rep = gaps.detect_gaps(db, now=NOW)
    db.commit()
    assert rep.new_missing == [1688615, 1688616]
    assert gaps.probe_index(db, client, 1688616, now=NOW) == "ingested"
    r = row(db, 1688616)
    assert (
        r.ingest_status == "normalized"
        and r.extra["discovered_by"] == "gap"
        and r.extra["is_catchup"] is True
    )
    assert r.published_at_raw == "2026.10.28 01:39:36"
    (ev,) = events_of(db, events.DISCLOSURE_NORMALIZED)
    assert ev.data["is_catchup"] is True
    rep = gaps.detect_gaps(db, now=NOW)
    assert rep.open == 1  # 1688616 already closed by the probe; 1688615 still open
    assert "1688616" not in gaps.load_state(db)["open"]


def test_gap_retry_spacing(db, kap_deps, client) -> None:
    _seed_refs(db, [1688551, 1688553])
    gaps.detect_gaps(db, now=NOW)
    db.commit()
    assert gaps.probe_index(db, client, 1688552, now=NOW) == "missing"
    rep = gaps.detect_gaps(db, now=NOW + timedelta(minutes=5))
    assert rep.enqueued == 0  # next probe only after 10 min
    rep = gaps.detect_gaps(db, now=NOW + timedelta(minutes=11))
    assert rep.enqueued == 1


def test_gap_scan_resolves_fund_indices_with_one_list_request(db, kap_deps, client, fake_kap) -> None:
    # Live 2026-10-27/28: the members list skipped 219 indices, all fund disclosures (FON) that the
    # fund list returns. They must not cost 219 detail probes.
    fake_kap.items = list_items()
    fake_kap.fund_items = fund_items()
    record_listing(db, fake_kap.items, mode="catchup", now=NOW)
    db.commit()
    rep = gaps.scan_gaps(db, client, now=NOW)
    assert (rep.funds_skipped, rep.new_missing, rep.open, rep.enqueued) == (219, [], 0, 0)
    assert rep.watermark == 1688616
    assert fake_kap.calls == ["funds"]
    assert db.execute(select(func.count()).select_from(job).where(job.c.kind == JOB_PROBE)).scalar() == 0
    assert events_of(db, events.INGEST_GAP_DETECTED) == []
    rep = gaps.scan_gaps(db, client, now=NOW + timedelta(minutes=5))
    assert fake_kap.calls == ["funds"]  # nothing unresolved → no further request


def test_gap_scan_lists_funds_once_then_probes_the_rest(db, kap_deps, client, fake_kap, monkeypatch) -> None:
    _seed_refs(db, [1688551, 1688554])
    fake_kap.fund_items = [it for it in fund_items() if it["disclosureIndex"] == 1688553]
    rep = gaps.scan_gaps(db, client, now=NOW)
    assert (rep.funds_skipped, rep.new_missing, rep.open, rep.enqueued) == (1, [1688552], 1, 1)
    assert gaps.load_state(db)["open"]["1688552"]["fund_checked"] is True
    gaps.scan_gaps(db, client, now=NOW + timedelta(minutes=5))
    assert fake_kap.calls == ["funds"]  # 1688552 already checked against the fund list
    # A probe queued before the fund list resolved its index costs no request.
    assert gaps.probe_index(db, client, 1688553, now=NOW) == "skipped"
    assert "detail:1688553" not in fake_kap.calls


def test_gap_scan_falls_back_to_probes_when_fund_list_fails(db, kap_deps, client, monkeypatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise KapHttpError("down", 503)

    monkeypatch.setattr(client, "list_fund_disclosures", boom)
    _seed_refs(db, [1688551, 1688554])
    rep = gaps.scan_gaps(db, client, now=NOW)
    assert (rep.funds_skipped, rep.new_missing, rep.open, rep.enqueued) == (0, [1688552, 1688553], 2, 2)
    assert not any(e.get("fund_checked") for e in gaps.load_state(db)["open"].values())


def test_reprioritize_raises_queued_fetches_of_newly_held_issuers(db, kap_deps) -> None:
    seed_companies(db)
    record_listing(db, fixture_list(), mode="catchup", now=NOW)
    db.commit()
    before = fetch_jobs(db)
    assert {r.priority for r in before.values()} == {55}
    assert reprioritize_queued(db) == 0
    kap_deps.held[inst(db, "FROTO")] = "P1"
    kap_deps.held[inst(db, "MEKAG")] = "P2"
    assert reprioritize_queued(db) == 3  # FROTO 1688363 + 1688368, MEKAG 1688011
    db.commit()
    after = fetch_jobs(db)
    for ref in ("1688363", "1688368"):
        assert (after[ref].priority, after[ref].payload["priority"]) == (jobs.PRIORITY_P1, "P1")
    assert (after["1688011"].priority, after["1688011"].payload["priority"]) == (jobs.PRIORITY_P2, "P2")
    assert {r.priority for k, r in after.items() if k not in {"1688363", "1688368", "1688011"}} == {55}
    assert reprioritize_queued(db) == 0  # idempotent; never demotes


# ------------------------------------------------------------------------------------------ reconcile


def test_reconcile_inserts_late_items_and_refreshes_changed(db, kap_deps, client, fake_kap) -> None:
    fake_kap.items = fixture_list([1688616, 1688533])
    poll_once(db, client, now=NOW)
    process_disclosure(db, client, "1688616")
    process_disclosure(db, client, "1688533")
    late = fixture_list([1688011])[0]
    changed = copy.deepcopy(fixture_list([1688533])[0])
    changed["modifyStatus"] = "DUZELTILEN"
    fake_kap.items = [fixture_list([1688616])[0], changed, late]
    summary = reconcile(db, client, now=NOW + timedelta(hours=4))
    assert summary["new"] == 1 and summary["changed"] == 1
    r = row(db, 1688533)
    assert r.extra["corrected_later"] is True and r.source_payload["list"]["modifyStatus"] == "DUZELTILEN"
    assert row(db, 1688011).extra["is_catchup"] is True
    refresh = db.execute(select(job.c.payload).where(job.c.kind == "kap:refresh")).scalars().all()
    assert [p["ref"] for p in refresh] == ["1688533"]


def test_backfill_is_resumable_and_low_priority(db, kap_deps, client, fake_kap) -> None:
    seed_companies(db)
    kap_deps.held = {inst(db, "FROTO"): "P1"}
    totals = backfill(db, client, date(2026, 10, 20), D0, max_windows=1)
    assert totals["windows"] == 1 and totals["new"] == len(LISTED) and not totals["done"]
    cursor = get_state(db, BACKFILL_KEY)
    assert cursor["next_to"] == "2026-10-25" and cursor["done"] is False
    js = fetch_jobs(db)
    assert js["1688363"].priority == 75 and js["1688616"].priority == jobs.PRIORITY_BACKFILL
    assert all(j.payload["mode"] == "backfill" and j.payload["is_catchup"] for j in js.values())
    assert events_of(db, events.DISCLOSURE_SEEN) == []
    assert series(db, "kap.latency_s") == []
    lists_before = fake_kap.calls.count("list")
    totals = backfill(db, client, date(2026, 10, 20), D0)  # resumes at 2026-10-25
    assert totals["done"] and totals["windows"] == 2
    assert fake_kap.calls.count("list") - lists_before == 2
    assert get_state(db, BACKFILL_KEY)["done"] is True

    out = process_disclosure(db, client, "1688616", mode="backfill")  # not held → no attachments
    assert out.status == "normalized"
    (ev,) = events_of(db, events.DISCLOSURE_NORMALIZED)
    assert ev.data["is_backfill"] is True and ev.data["is_catchup"] is True


def test_backfill_with_tickers_filters_by_member(db, kap_deps, client, kap_http) -> None:
    seed_companies(db)
    backfill(db, client, date(2026, 10, 27), D0, tickers=["froto"])
    body = json.loads(kap_http.calls.last.request.content)
    assert body["mkkMemberOidList"] == ["4028e4a140f2ed71014106890fae0138"]
    with pytest.raises(ValueError, match="sync-companies"):
        backfill(db, client, date(2026, 10, 27), D0, tickers=["NOSUCH"])


# ------------------------------------------------------------------------------------------ cadence/handlers


def test_session_aware_cadence(db, kap_deps) -> None:
    ist = lambda s: parse_iso(s)  # noqa: E731
    assert cadence.poll_interval_seconds(ist("2026-10-28T07:00:00Z")) == 30  # Wed 10:00
    assert cadence.poll_interval_seconds(ist("2026-10-28T19:30:00Z")) == 30  # Wed 22:30 evening peak
    assert cadence.poll_interval_seconds(ist("2026-10-28T20:30:00Z")) == 180  # Wed 23:30
    assert cadence.poll_interval_seconds(ist("2026-10-28T06:00:00Z")) == 180  # Wed 09:00
    assert cadence.poll_interval_seconds(ist("2026-10-31T09:00:00Z")) == 180  # Saturday
    db.execute(
        trading_calendar.insert().values(
            id=uuid7(), calendar_id="XIST", date=date(2026, 10, 29), session_type="closed", is_half_day=False
        )
    )
    db.commit()
    cadence.clear_cache()
    assert cadence.poll_interval_seconds(ist("2026-10-29T09:00:00Z")) == 180  # Republic Day holiday


def test_handlers_registered_and_circuit_defers(db, kap_deps, monkeypatch) -> None:
    from papi.core.registry import REGISTRY
    from papi.sources.kap import handlers

    importlib.reload(handlers)
    assert {"kap:poll", "kap:gaps", "kap:reconcile", "kap:companies", "kap:reprioritize"} <= set(
        REGISTRY.periodics
    )
    assert "kap.reprioritize_on_portfolio_change" in {
        h.name for h in REGISTRY.handlers_for(events.PORTFOLIO_CHANGED)
    }
    assert {"kap:fetch", "kap:probe", "kap:refresh"} <= set(REGISTRY.job_kinds)
    assert REGISTRY.periodics["kap:poll"].seconds(NOW) == 30

    class Blocked:
        def disclosure_detail(self, index: int) -> Any:
            raise KapBlocked("open", NOW + timedelta(minutes=5))

    monkeypatch.setattr(handlers, "_client", lambda: Blocked())
    ctx = SimpleNamespace(attempt=1, job=SimpleNamespace(max_attempts=5))
    with pytest.raises(jobs.Deferred) as exc:
        handlers.fetch_job({"ref": "1688616"}, ctx)
    assert exc.value.until == NOW + timedelta(minutes=5)


def test_cli_show_and_stats(db, kap_deps, client) -> None:
    from typer.testing import CliRunner

    from papi.sources.kap.cli import app

    record_listing(db, fixture_list([1688616]), mode="live", now=NOW)
    db.commit()
    process_disclosure(db, client, "1688616")
    runner = CliRunner()
    res = runner.invoke(app, ["show", "1688616"])
    assert res.exit_code == 0, res.output
    assert "Yatırımcılarımızı Bilgilendirme" in res.output and "disclosure.normalized.v1" in res.output
    res = runner.invoke(app, ["stats"])
    assert res.exit_code == 0, res.output
    assert "normalized: 1" in res.output
