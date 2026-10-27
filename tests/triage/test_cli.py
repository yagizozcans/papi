from __future__ import annotations

import json

from typer.testing import CliRunner

from papi.triage.classify import triage_disclosure
from papi.triage.cli import app

from .helpers import form_field, seed_company, seed_disclosure

runner = CliRunner()
KP = "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim"


def _structure() -> dict:
    return {
        "form_fields": [form_field("Para Birimi", "TRY", 1)],
        "tables": [
            {
                "header": [
                    "Pay Grup Bilgileri",
                    "1 TL Nominal Değerli Paya Ödenecek Nakit Kar Payı - Brüt(TL)",
                ],
                "rows": [["CLI", "1,25"]],
            }
        ],
    }


def test_classify_dry_run_by_source_ref(db):
    iss, _ = seed_company(db, "CLI")
    did = seed_disclosure(db, subject=KP, issuer_ids=[iss], structure=_structure(), source_ref="777")
    db.commit()
    res = runner.invoke(app, ["classify", "--source-ref", "777"])
    assert res.exit_code == 0, res.output
    assert f"bildirim {did}: 1 olay" in res.output
    assert "distribution.dividend.cash" in res.output
    assert "CLI: Nakit kâr payı — brüt 1,25 TL/pay" in res.output
    res = runner.invoke(app, ["classify", did, "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out[0]["payload"]["gross_per_share"] == 1.25
    db.rollback()
    from sqlalchemy import func, select

    from papi.core.schema import event

    assert db.execute(select(func.count()).select_from(event)).scalar_one() == 0  # dry run


def test_classify_unknown_source_ref(db):
    res = runner.invoke(app, ["classify", "--source-ref", "404404"])
    assert res.exit_code != 0
    assert "bulunamadı" in res.output


def test_stats_and_subjects(db):
    iss, _ = seed_company(db, "STA")
    for i, (subject, summary) in enumerate(
        [
            (KP, None),
            ("Özel Durum Açıklaması (Genel)", "Zyxel performans ödülü"),
            ("Tamamen Yeni Bir Konu", "x"),
        ]
    ):
        did = seed_disclosure(
            db, subject=subject, summary=summary, issuer_ids=[iss], source_ref=f"88{i}",
            published_at="2026-10-27T10:00:00+03:00",
        )  # fmt: skip
        triage_disclosure(db, did)
    db.commit()
    res = runner.invoke(app, ["stats", "--days", "0"])
    assert res.exit_code == 0, res.output
    assert "other.unclassified: 2 olay (66.7%)" in res.output
    assert "distribution.dividend.cash" in res.output
    res = runner.invoke(app, ["subjects", "--days", "0"])
    assert res.exit_code == 0, res.output
    assert "Tamamen Yeni Bir Konu" in res.output
    assert "Özel Durum Açıklaması (Genel)" in res.output  # mapped, but fell to other.unclassified
    assert KP not in res.output


def test_subjects_from_harvest_csv(db):
    res = runner.invoke(app, ["subjects", "--from-csv"])
    assert res.exit_code == 0, res.output
    assert "toplam 7220 bildirim; haritada olmayan konu hacmi 0 (0.0%)" in res.output
