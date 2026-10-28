from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from papi.core import db as core_db
from papi.core.schema import kv_state, metric
from papi.core.state import get_state, set_state
from papi.run import backup, handlers
from papi.run.backup import BackupError, backup_now, retained
from papi.run.lock import InstanceLock


def _marker(value: str) -> None:
    with core_db.transaction() as conn:
        set_state(conn, "test:marker", value)


def _read_marker() -> str | None:
    with core_db.connect() as conn:
        return get_state(conn, "test:marker")


def test_backup_now_writes_verified_copy_and_records_state(db, settings):
    _marker("v1")
    now = datetime(2026, 10, 27, 22, 30, tzinfo=UTC)  # 01:30 on the 28th in Istanbul
    res = backup_now(now=now)
    assert res.path == settings.paths.backups / "papi-20261028.db"
    conn = sqlite3.connect(res.path)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        row = conn.execute("SELECT value FROM kv_state WHERE key = 'test:marker'").fetchone()
        assert row[0] == '"v1"'
    finally:
        conn.close()
    assert not res.path.with_name(res.path.name + ".tmp").exists()
    db.rollback()
    assert get_state(db, backup.LAST_BACKUP_KEY)["path"] == str(res.path)
    assert db.execute(select(metric.c.value).where(metric.c.name == "run.backup.ok")).scalar() == res.bytes


def test_retention_keeps_daily_and_weekly():
    days = [date(2026, 10, 28) - timedelta(days=i) for i in range(120)]
    keep = retained(days, keep_daily=14, keep_weekly=8)
    daily = set(days[:14])
    assert daily <= keep
    extra = sorted(keep - daily)
    # newest date of each of the 8 newest ISO weeks; the ones not already daily are Sundays
    assert len(keep) == 14 + len(extra)
    assert all(d.isoweekday() == 7 for d in extra)
    weeks = {d.isocalendar()[:2] for d in keep}
    assert len(weeks) == 8
    assert retained(days, 0, 0) == set()


def test_prune_removes_files_outside_retention(db, settings, write_config):
    write_config("[backup]\nkeep_daily = 2\nkeep_weekly = 1\n")
    d = settings.paths.backups
    d.mkdir(parents=True, exist_ok=True)
    for day in ("20261020", "20261025", "20261026", "20261027"):
        (d / f"papi-{day}.db").write_bytes(b"x")
    (d / "pre-restore-20261001T000000Z.db").write_bytes(b"x")
    removed = backup.prune()
    assert sorted(p.name for p in removed) == ["papi-20261020.db", "papi-20261025.db"]
    left = sorted(p.name for p in d.iterdir())
    assert left == ["papi-20261026.db", "papi-20261027.db", "pre-restore-20261001T000000Z.db"]


def test_backup_due_and_nightly_handler(db, settings, write_config, monkeypatch):
    write_config("[run]\nbackup_hour = 3\n")
    before = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)  # 02:00 Istanbul
    after = datetime(2026, 10, 28, 0, 30, tzinfo=UTC)  # 03:30 Istanbul
    assert not backup.backup_due(before, 3)
    assert backup.backup_due(after, 3)

    clock = {"now": before}
    monkeypatch.setattr(handlers, "now_utc", lambda: clock["now"])
    handlers.nightly_backup(None)
    assert not (settings.paths.backups / "papi-20261028.db").exists()
    clock["now"] = after
    handlers.nightly_backup(None)
    assert (settings.paths.backups / "papi-20261028.db").exists()
    assert not backup.backup_due(after, 3)

    calls: list[datetime] = []
    monkeypatch.setattr(handlers, "backup_now", lambda now: calls.append(now))
    handlers.nightly_backup(None)  # today's file exists → nothing to do
    assert calls == []


def test_nightly_handler_failure_raises_ops_alert(db, settings, monkeypatch):
    from papi.core.schema import job

    monkeypatch.setattr(handlers, "now_utc", lambda: datetime(2026, 10, 28, 9, 0, tzinfo=UTC))

    def boom(now):
        raise BackupError("disk full")

    monkeypatch.setattr(handlers, "backup_now", boom)
    with pytest.raises(BackupError):
        handlers.nightly_backup(None)
    db.rollback()
    texts = db.execute(select(job.c.payload).where(job.c.kind == "notify:ops")).scalars().all()
    assert any("disk full" in p["text"] for p in texts)


def test_restore_refuses_while_running_and_restores_with_safety_copy(db, settings):
    _marker("before-backup")
    res = backup_now()
    _marker("after-backup")
    with InstanceLock(), pytest.raises(BackupError, match="zaten çalışıyor"):
        backup.restore(str(res.path))
    assert _read_marker() == "after-backup"

    out = backup.restore(res.path.name)
    core_db.reset_engines()
    assert _read_marker() == "before-backup"
    assert out.safety_copy is not None and out.safety_copy.exists()
    conn = sqlite3.connect(out.safety_copy)
    try:
        row = conn.execute("SELECT value FROM kv_state WHERE key = 'test:marker'").fetchone()
        assert row[0] == '"after-backup"'
    finally:
        conn.close()
    with core_db.connect() as c:
        assert c.execute(select(kv_state.c.key).where(kv_state.c.key == "test:marker")).scalar()


def test_restore_rejects_non_papi_files(db, settings, tmp_path):
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not sqlite at all")
    with pytest.raises(BackupError, match="SQLite"):
        backup.restore(str(bogus))
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t(x)").connection.close()
    with pytest.raises(BackupError, match="alembic_version"):
        backup.restore(str(other))
    with pytest.raises(BackupError, match="not found"):
        backup.restore("19990101")
