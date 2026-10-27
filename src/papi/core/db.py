"""SQLite engine, connections and migrations.

- One engine per database path (cached). Pragmas on every connection: ``journal_mode=WAL``,
  ``foreign_keys=ON``, ``busy_timeout=5000``, ``synchronous=NORMAL``.
- pysqlite's implicit transaction handling is disabled; SQLAlchemy's ``begin`` emits ``BEGIN``
  (deferred) and ``transaction()`` emits ``BEGIN IMMEDIATE`` so writers take the write lock up front and
  never hit SQLITE_BUSY on a read→write upgrade.
- ``connect()``: a plain connection; the first statement auto-begins a deferred transaction, so call
  ``conn.commit()`` after writes (reads may simply close).
- ``transaction()``: ``BEGIN IMMEDIATE``; commits on success, rolls back on exception.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event

from papi.core.config import REPO_ROOT, get_settings, resource_dir

_engines: dict[Path, Engine] = {}

_IMMEDIATE_OPTION = "papi_begin_immediate"


def _install_hooks(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, _record: Any) -> None:
        dbapi_conn.isolation_level = None  # we issue BEGIN ourselves
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        if conn.get_execution_options().get(_IMMEDIATE_OPTION):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")


def get_engine(path: Path | None = None) -> Engine:
    """Engine for ``path`` (default: ``settings.paths.db``); created once per path."""
    db_path = (path or get_settings().paths.db).resolve()
    engine = _engines.get(db_path)
    if engine is None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 5.0},
        )
        _install_hooks(engine)
        _engines[db_path] = engine
    return engine


def reset_engines() -> None:
    """Dispose and forget all cached engines (tests, or after PAPI_HOME changes)."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


@contextmanager
def connect(path: Path | None = None) -> Iterator[Connection]:
    """Connection that auto-begins a deferred transaction; commit explicitly after writes."""
    with get_engine(path).connect() as conn:
        yield conn


@contextmanager
def transaction(path: Path | None = None) -> Iterator[Connection]:
    """``BEGIN IMMEDIATE`` transaction; commit on success, rollback on error."""
    with get_engine(path).connect() as conn:
        conn = conn.execution_options(**{_IMMEDIATE_OPTION: True})
        with conn.begin():
            yield conn


def alembic_config(path: Path | None = None) -> Any:
    """Alembic ``Config`` pointing at the repo's ``alembic/`` scripts and the given database."""
    from alembic.config import Config

    ini = REPO_ROOT / "alembic.ini"
    cfg = Config(str(ini)) if ini.exists() else Config()
    cfg.set_main_option("script_location", str(resource_dir("alembic")))
    db_path = (path or get_settings().paths.db).resolve()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def migrate(path: Path | None = None, revision: str = "head") -> None:
    """Upgrade the database to ``revision`` (default head).

    Foreign keys are switched off for the duration (batch migrations recreate tables) and checked
    afterwards; any violation aborts with ``RuntimeError``.
    """
    from alembic import command

    cfg = alembic_config(path)
    engine = get_engine(path)
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection  # pragmas must run outside any transaction
        assert raw is not None
        raw.execute("PRAGMA foreign_keys=OFF")
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, revision)
            if conn.in_transaction():
                conn.commit()
            problems = raw.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key violations after migration: {problems[:5]}")
        finally:
            if conn.in_transaction():
                conn.rollback()
            raw.execute("PRAGMA foreign_keys=ON")


def current_revision(path: Path | None = None) -> str | None:
    """Alembic revision currently stamped in the database (None if unmigrated)."""
    from alembic.runtime.migration import MigrationContext

    with get_engine(path).connect() as conn:
        rev = MigrationContext.configure(conn).get_current_revision()
        if conn.in_transaction():
            conn.rollback()
        return rev
