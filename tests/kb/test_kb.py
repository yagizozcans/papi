"""KB end to end on real excerpts: ingest → versions (before/after amendment) → snapshot → C8 API."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import yaml
from typer.testing import CliRunner

from papi.core import db as core_db
from papi.core.schema import event_log, kb_snapshot, obligation
from papi.kb import api
from papi.kb import obligation_store as obl
from papi.kb import snapshot as snap
from papi.kb.cli import app
from papi.kb.ingest import ingest_entry
from tests.kb.helpers import II151_RG_URL, II151_URL, LAW6362_URL, fetch_fixture, ii151_entry, law6362_entry


@pytest.fixture
def kb(db):
    """II-15.1 (consolidated + original RG text) and a 6362 excerpt parsed and published as kb-001."""
    e1, e2 = ii151_entry(), law6362_entry()
    fetch_fixture(e1, {II151_URL: "ii15_1_consolidated.txt", II151_RG_URL: "ii15_1_original_rg_excerpt.html"})
    fetch_fixture(e2, {LAW6362_URL: "law6362_excerpt.txt"})
    r1, r2 = ingest_entry(e1), ingest_entry(e2)
    assert r1.status == r2.status == "parsed"
    with core_db.transaction() as conn:
        res = snap.publish(conn)
    db.rollback()
    return {"ii151": r1, "law": r2, "published": res, "entry": e1}


def test_ingest_counts_and_stable_citations(kb, db):
    r = kb["ii151"]
    assert r.provisions == 91 and r.new_versions == 91
    assert r.history_versions > 0
    v = api.lookup(db, "tr:spk:II-15.1:m4:f1:g:2")
    assert v is not None and v.text.startswith("2) İdari sorumluluğu") and v.label_tr() == "II-15.1 m.4/1-g-2"
    assert api.lookup(db, "tr:law:6362:m107:f2").amended_by.startswith("7222 sayılı Kanun")
    # re-parse of unchanged content is skipped; forced re-parse keeps the same citation ids, no new versions
    again = ingest_entry(kb["entry"])
    assert again.status == "unchanged"
    forced = ingest_entry(kb["entry"], force=True)
    assert forced.status == "parsed" and forced.new_provisions == 0 and forced.new_versions == 0


def test_versions_before_and_after_amendment(kb, db):
    cit = "tr:spk:II-15.1:m2:f1"  # whole article replaced by II-15.1.a (RG 10/02/2017)
    before = api.lookup(db, cit, date(2015, 6, 1))
    after = api.lookup(db, cit, date(2018, 1, 1))
    assert before.effective_from == date(2014, 2, 23) and before.effective_to == date(2017, 2, 10)
    assert "bu Tebliğ hükümlerine tabidir" in before.text and "izahnamenin" not in before.text
    assert after.effective_from == date(2017, 2, 10) and "izahnamenin ya da ihraç belgesinin" in after.text
    assert api.lookup(db, cit, date(2014, 1, 30)) is None  # before entry into force (23/01/2014 + 1 ay)
    # fıkra (4) of m12 was added on 13/02/2018 and changed on 17/11/2018
    f4 = "tr:spk:II-15.1:m12:f4"
    assert api.lookup(db, f4, date(2017, 1, 1)) is None
    assert api.lookup(db, f4, date(2019, 1, 1)).effective_from == date(2018, 11, 17)
    # unamended provision: dated from the Yürürlük article; aware datetimes are read in Istanbul time
    # (Istanbul was UTC+2 in February 2014)
    m1 = api.lookup(db, "tr:spk:II-15.1:m1:f1", datetime(2014, 2, 22, 22, 30, tzinfo=UTC))
    assert m1.effective_from == date(2014, 2, 23)
    assert api.lookup(db, "tr:spk:II-15.1:m1:f1", datetime(2014, 2, 22, 21, 30, tzinfo=UTC)) is None
    # 6362: undatable amendment (act missing from the list) → effective_from unknown, still returned
    unk = api.lookup(db, "tr:law:6362:m106:f1", date(2025, 1, 1))
    assert unk.effective_from == date(2020, 2, 25)
    assert api.lookup(db, "tr:law:6362:m78:f1").effective_from == date(2016, 4, 26)


def test_refresh_with_changed_text_creates_new_version_in_draft(kb, db):
    e = kb["entry"]
    changed = (
        (__import__("tests.kb.helpers", fromlist=["FIXTURES"]).FIXTURES / "ii15_1_consolidated.txt")
        .read_text()
        .replace(
            "Bu Tebliğ hükümlerini Kurul yürütür.", "Bu Tebliğ hükümlerini Sermaye Piyasası Kurulu yürütür."
        )
        .encode()
    )
    out = fetch_fixture(e.model_copy(update={"history": []}), {II151_URL: changed})
    assert out[0].status == "changed"
    r = ingest_entry(e.model_copy(update={"history": []}))
    assert r.new_versions == 2  # m31 and m31/f1
    draft = r.snapshot_id
    assert draft != kb["published"].snapshot_id
    db.rollback()
    assert "Kurul yürütür" in api.lookup(db, "tr:spk:II-15.1:m31:f1").text  # published view unchanged
    assert "Sermaye Piyasası Kurulu" in api.lookup(db, "tr:spk:II-15.1:m31:f1", include_draft=True).text
    with core_db.transaction() as conn:
        res = snap.publish(conn)
    db.rollback()
    assert res.label == "kb-002"
    assert "Sermaye Piyasası Kurulu" in api.lookup(db, "tr:spk:II-15.1:m31:f1").text
    pinned = api.lookup(db, "tr:spk:II-15.1:m31:f1", snapshot_id=kb["published"].snapshot_id)
    assert "Kurul yürütür" in pinned.text  # an analysis pinned to kb-001 still sees its text
    statuses = dict(
        db.execute(kb_snapshot.select().with_only_columns(kb_snapshot.c.id, kb_snapshot.c.status)).all()
    )
    assert statuses[kb["published"].snapshot_id] == "superseded" and statuses[res.snapshot_id] == "published"


def test_publish_event_and_nothing_to_publish(kb, db):
    rows = db.execute(event_log.select().where(event_log.c.type == "kb.version.published.v1")).all()
    assert [r.data for r in rows] == [{"kb_snapshot": kb["published"].snapshot_id}]
    assert api.current_snapshot(db) == kb["published"].snapshot_id
    assert kb["published"].label == "kb-001"
    with pytest.raises(snap.NothingToPublish), core_db.transaction() as conn:
        snap.publish(conn)


def test_search_normalised_with_as_of(kb, db):
    hits = api.search(db, "geleceğe yönelik değerlendirmeler yılda dört", k=3)
    assert hits and hits[0].version.citation_id == "tr:spk:II-15.1:m10:f1:b"
    # diacritics/case folded, suffixes stemmed
    assert api.search(db, "IÇSEL BILGI", k=5, filters={"official_code": "II-15.1"})
    law = api.search(db, "piyasa dolandırıcılığı yanıltıcı bilgi", k=3, filters={"authority": "TBMM"})
    assert law and all(h.version.official_code == "6362" for h in law)
    # as_of: the 2015 text of m2/f1 has no "izahname" — only today's version matches
    now = {h.version.citation_id for h in api.search(db, "izahnamenin ihraç belgesinin onaylandığı", k=10)}
    old = {
        h.version.citation_id
        for h in api.search(db, "izahnamenin ihraç belgesinin onaylandığı", date(2015, 1, 1), 10)
    }
    assert "tr:spk:II-15.1:m2:f1" in now and "tr:spk:II-15.1:m2:f1" not in old
    assert api.search(db, "", k=3) == []


def _write_obligations(tmp_path, approved=False):
    data = {
        "version": 1,
        "drafted_by": "test",
        "obligations": [
            {
                "id": "insider-transaction-disclosure",
                "event_codes": ["ownership.insider_trade"],
                "title_tr": "İdari sorumluluğu bulunan kişilerin işlemleri",
                "citation": "tr:spk:II-15.1:m11:f1",
                "actor": "insider",
                "trigger": "İşlem",
                "deadline_rule": "immediately",
                "required_fields": ["işlem tarihi"],
                "approved": approved,
            },
            {
                "id": "ownership-threshold-disclosure",
                "event_codes": ["ownership.threshold_crossing"],
                "title_tr": "Eşik",
                "citation": "tr:spk:II-15.1:m12:f1:a",
                "actor": "shareholder",
                "trigger": "Eşik aşımı",
                "deadline_rule": "within_n_business_days(3)",
                "downstream_steps": [{"key": "mkk_check", "label_tr": "MKK", "deadline_rule": "immediately"}],
            },
            {
                "id": "not-parsed-yet",
                "event_codes": ["capital.increase.rights"],
                "title_tr": "x",
                "citation": "tr:spk:VII-128.1:m12:f1",
                "actor": "board",
                "trigger": "x",
                "deadline_rule": "immediately",
            },
        ],
    }
    p = tmp_path / "obligations.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p, data


def test_obligations_approval_gate_and_timeline(kb, db, tmp_path):
    path, data = _write_obligations(tmp_path)
    with core_db.transaction() as conn:
        rep = obl.load_obligations(conn, path)
    assert rep.inserted == 2 and rep.missing_provisions == [("not-parsed-yet", "tr:spk:VII-128.1:m12:f1")]
    db.rollback()
    assert api.obligations(db, "ownership.threshold_crossing") == []  # unapproved are never used
    drafts = api.obligations(db, "ownership.threshold_crossing", approved_only=False)
    assert drafts[0].deadline_rule == "within_n_business_days(3)" and drafts[0].provision.path == "m12/f1/a"
    wrapper = db.execute(obligation.select()).first().required_fields
    assert set(wrapper) == {"fields", "meta"} and wrapper["meta"]["approved"] is False
    with core_db.transaction() as conn:
        assert obl.approve(conn, "ownership-threshold-disclosure")
        assert obl.approve_all_for(conn, "ownership.insider_trade") == ["insider-transaction-disclosure"]
        assert not obl.approve(conn, "nope")
    db.rollback()
    ob = api.obligations(db, "ownership.threshold_crossing")
    assert [o.key for o in ob] == ["ownership-threshold-disclosure"] and ob[0].approved
    assert (
        api.obligations(db, "ownership.threshold_crossing", date(2010, 1, 1)) == []
    )  # provision not in force
    steps = api.timeline(db, "ownership.threshold_crossing")
    assert [(s.order, s.key, s.after) for s in steps] == [
        (1, "ownership-threshold-disclosure", None),
        (2, "mkk_check", "ownership-threshold-disclosure"),
    ]
    # editing an approved item resets its approval on reload
    data["obligations"][1]["deadline_rule"] = "immediately"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with core_db.transaction() as conn:
        rep = obl.load_obligations(conn, path)
    assert rep.approvals_reset == ["ownership-threshold-disclosure"]
    db.rollback()
    assert api.obligations(db, "ownership.threshold_crossing") == []
    with pytest.raises(ValueError):
        obl.parse_deadline_rule("someday")


def test_repo_obligations_cover_first_ten_event_types():
    spec = obl.read_obligations()
    codes = {c for o in spec.obligations for c in o.event_codes}
    assert {
        "business.contract.award",
        "capital.increase.bonus",
        "capital.increase.rights",
        "distribution.dividend.cash",
        "buyback.program.start",
        "buyback.execution",
        "ownership.insider_trade",
        "ownership.threshold_crossing",
        "mna.acquisition",
        "mna.disposal",
        "report.financial.periodic",
        "report.financial.guidance",
    } <= codes
    assert not any(o.approved for o in spec.obligations)
    rights = next(o for o in spec.obligations if o.id == "rights-issue-board-decision")
    assert [s.key for s in rights.downstream_steps][:5] == [
        "spk_application",
        "spk_approval",
        "prospectus_publication",
        "rights_period_start",
        "rights_period_end",
    ]


def test_cli_show_search_review_status(kb, tmp_path, monkeypatch):
    runner = CliRunner()
    res = runner.invoke(app, ["show", "tr:spk:II-15.1:m2:f1", "--as-of", "2015-06-01"])
    assert res.exit_code == 0 and "Asıl metin" in res.output and "2014-02-23" in res.output
    res = runner.invoke(app, ["search", "geleceğe yönelik değerlendirmeler", "-k", "2"])
    assert res.exit_code == 0 and "tr:spk:II-15.1" in res.output
    path, _ = _write_obligations(tmp_path)
    monkeypatch.setattr(obl, "obligations_path", lambda: path)
    with core_db.transaction() as conn:
        obl.load_obligations(conn)
    res = runner.invoke(app, ["review"])
    assert res.exit_code == 0 and "Onay bekleyen yükümlülükler (2)" in res.output
    assert "implicit_fikra_1" in res.output  # 6362 m78 low-confidence structure
    res = runner.invoke(app, ["approve-obligation", "--all-for", "ownership.insider_trade"])
    assert res.exit_code == 0 and "1 yükümlülük onaylandı" in res.output
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0 and "kb-001" in res.output
    res = runner.invoke(app, ["publish"])
    assert res.exit_code == 1  # nothing new in the draft
