from __future__ import annotations

import json

from typer.testing import CliRunner

from papi.core import db as core_db
from papi.core import jobs
from papi.core.state import set_state
from papi.run import status as run_status
from papi.run.cli import app
from papi.run.lock import InstanceLock

runner = CliRunner()


def test_status_collects_lock_heartbeat_lanes_and_kap(db, settings):
    with core_db.transaction() as conn:
        jobs.enqueue(conn, "periodic:telegram:poll", {}, priority=5)
        jobs.enqueue(conn, "kap:fetch", {"ref": "1"}, priority=50)
        jobs.enqueue(conn, "kap:fetch", {"ref": "2"}, priority=50)
        set_state(conn, "kap:last_poll_at", "2026-10-28T07:00:00+00:00")
        set_state(
            conn,
            "run:heartbeat",
            {"pid": 1, "heartbeat_at": "2026-01-01T07:00:00+00:00", "lanes": [], "last_scheduler_tick": None},
        )
    with InstanceLock(), core_db.connect() as conn:
        info = run_status.collect(conn, include_service=False)
    assert info["running"] is True
    assert info["lanes"]["telegram"] == {"queued": 1}
    assert info["lanes"]["io"] == {"queued": 2}
    assert info["jobs"]["queued"] == 3
    assert info["kap"]["last_poll_at"] == "2026-10-28T07:00:00+00:00"
    assert info["heartbeat_stale"] is True
    assert info["governor"]["paused"] is False
    assert info["telegram"]["enabled"] is False


def test_status_cli_json_and_text(db, settings, monkeypatch):
    monkeypatch.setattr(run_status.shutil, "which", lambda name: None)
    res = runner.invoke(app, ["status", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["running"] is False
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "Çalışıyor: hayır" in res.output
    assert "Son KAP yoklaması" in res.output


def test_backup_now_and_restore_cli(db, settings):
    res = runner.invoke(app, ["backup-now"])
    assert res.exit_code == 0, res.output
    assert "papi-" in res.output
    res = runner.invoke(app, ["backups"])
    assert "papi-" in res.output
    name = next(settings.paths.backups.glob("papi-*.db")).name
    with InstanceLock():
        res = runner.invoke(app, ["restore", "--from", name, "--yes"])
    assert res.exit_code == 1
    assert "zaten çalışıyor" in res.output
    res = runner.invoke(app, ["restore", "--from", name, "--yes"])
    assert res.exit_code == 0, res.output
    assert "Geri yüklendi" in res.output
