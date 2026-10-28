from __future__ import annotations

import json

from sqlalchemy import func, select
from typer.testing import CliRunner

from papi.analyst.cli import app
from papi.core.schema import analysis, llm_usage_ledger
from papi.llm import call as call_mod
from tests.analyst.helpers import ScriptedBackend, contract_output

runner = CliRunner()


def test_dry_run_prints_pack_without_calling(db, world, monkeypatch):
    monkeypatch.setattr(call_mod, "get_backend", lambda: ScriptedBackend())
    res = runner.invoke(app, ["run", world.event_id, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "PAPI BAĞLAM PAKETİ" in res.output and "[S14]" in res.output
    assert "Bağlam paketi ≈ " in res.output and "input_hash " in res.output
    db.rollback()
    assert db.execute(select(func.count()).select_from(llm_usage_ledger)).scalar_one() == 0


def test_run_show_verify_pending(db, world, monkeypatch):
    res = runner.invoke(app, ["pending"])
    assert res.exit_code == 0, res.output
    assert world.event_id in res.output and "Governor: aktif" in res.output

    backend = ScriptedBackend(analyst=[contract_output()])
    monkeypatch.setattr(call_mod, "get_backend", lambda: backend)
    res = runner.invoke(app, ["run", "1688011"])  # KAP disclosure index resolves to its event
    assert res.exit_code == 0, res.output
    assert "Durum: analysed" in res.output and "Doğrulayıcı: temiz" in res.output
    assert "maliyet ≈ $0.0500" in res.output and "Özet (TR): MEKAG, yurt içi" in res.output

    res = runner.invoke(app, ["run", world.event_id])
    assert "Durum: skipped (already_analysed)" in res.output

    res = runner.invoke(app, ["show", world.event_id])
    assert res.exit_code == 0, res.output
    assert "(fact, doğrulama pass, denetim —) Şirket yurt içi" in res.output
    data = json.loads(runner.invoke(app, ["show", world.event_id, "--json"]).output)
    assert data["analyses"][0]["claims"][0]["citations"][0]["target_type"] == "document_span"

    db.rollback()
    aid = db.execute(select(analysis.c.id)).scalar_one()
    res = runner.invoke(app, ["verify", aid])
    assert res.exit_code == 0, res.output
    assert "Doğrulayıcı: temiz · review_state auto" in res.output

    res = runner.invoke(app, ["pending"])
    assert world.event_id not in res.output


def test_run_reports_deferral(db, world, monkeypatch):
    from datetime import timedelta

    from papi.core.timeutil import now_utc
    from papi.llm import governor

    governor.pause(db, now_utc() + timedelta(hours=1))
    db.commit()
    monkeypatch.setattr(call_mod, "get_backend", lambda: ScriptedBackend())
    res = runner.invoke(app, ["run", world.event_id])
    assert res.exit_code == 2 and "Ertelendi (llm:paused:manual)" in res.output


def test_unknown_ref(db, world):
    res = runner.invoke(app, ["show", "nope"])
    assert res.exit_code != 0 and "olay bulunamadı" in res.output
