"""SQLite contention under ``papi run`` (other lanes writing concurrently)."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from papi.core import db as core_db
from papi.core.config import get_settings
from papi.core.schema import disclosure
from papi.core.timeutil import parse_iso
from papi.sources.kap import normalize
from papi.sources.kap.common import is_db_busy, writer
from papi.sources.kap.ingest import ParseFailed, process_disclosure, record_listing

from .conftest import fixture_list

NOW = parse_iso("2026-10-28T08:00:00Z")


def _other() -> sqlite3.Connection:
    return sqlite3.connect(get_settings().paths.db, timeout=0.2, isolation_level=None)


def _ins(key: str) -> str:
    return (
        f"INSERT INTO kv_state(key, value, updated_at) VALUES ('{key}', '1', '2026-10-28T00:00:00.000000Z')"
    )


def test_deferred_read_then_write_fails_fast_but_writer_waits(db, kap_deps) -> None:
    db.commit()
    other = _other()
    # The original failure: a deferred read snapshot, then another connection commits, then our write
    # fails at once with "database is locked" (SQLITE_BUSY_SNAPSHOT, busy_timeout does not apply).
    with core_db.connect() as deferred:
        deferred.execute(text("SELECT count(*) FROM kv_state")).scalar()
        other.execute(_ins("t:a"))
        with pytest.raises(OperationalError) as exc:
            deferred.execute(text(_ins("t:b")))
        assert is_db_busy(exc.value)
        deferred.rollback()
    # With ``writer`` the transaction takes the write lock up front: other writers wait for us instead.
    with core_db.connect() as raw:
        conn = writer(raw)
        conn.execute(text("SELECT count(*) FROM kv_state")).scalar()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        conn.execute(text(_ins("t:c")))
        conn.commit()
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()


def test_lock_error_during_normalize_is_retryable_not_parse_failure(
    db, kap_deps, client, monkeypatch
) -> None:
    record_listing(db, fixture_list([1688616]), mode="live", now=NOW)
    db.commit()

    def locked(*a: Any, **kw: Any) -> Any:
        raise OperationalError("INSERT INTO document ...", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr("papi.sources.kap.ingest.parse_and_normalize", locked)
    with pytest.raises(OperationalError):
        process_disclosure(db, client, "1688616")
    db.rollback()
    row = db.execute(select(disclosure.c.ingest_status, disclosure.c.ingest_error)).one()
    assert row.ingest_status == "fetched" and row.ingest_error is None  # retried later, raw kept

    monkeypatch.setattr("papi.sources.kap.ingest.parse_and_normalize", normalize.parse_and_normalize)
    out = process_disclosure(db, client, "1688616")  # the retry succeeds without re-marking anything
    assert out.status == "normalized"


def test_real_parse_errors_stay_permanent(db, kap_deps, client, monkeypatch) -> None:
    record_listing(db, fixture_list([1688616]), mode="live", now=NOW)
    db.commit()

    def broken(*a: Any, **kw: Any) -> Any:
        raise ValueError("unparseable body")

    monkeypatch.setattr("papi.sources.kap.ingest.parse_and_normalize", broken)
    with pytest.raises(ParseFailed):
        process_disclosure(db, client, "1688616")
    db.rollback()
    assert db.execute(select(disclosure.c.ingest_status)).scalar_one() == "failed:parse"


def test_parsing_runs_outside_any_transaction(db, kap_deps, client, monkeypatch) -> None:
    record_listing(db, fixture_list([1688549]), mode="live", now=NOW)  # 2 MB financial report body
    db.commit()
    seen: list[bool] = []
    real = normalize.plan_documents

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append(db.in_transaction())
        return real(*a, **kw)

    monkeypatch.setattr(normalize, "plan_documents", spy)
    assert process_disclosure(db, client, "1688549").status == "normalized"
    assert seen == [False]
