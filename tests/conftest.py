"""Shared fixtures. Every test runs with a temporary ``PAPI_HOME`` (autouse) — never ``~/.papi``.

Fixtures
- ``papi_home``: temp home dir; env ``PAPI_HOME`` set, settings cache + engines reset, keyring disabled.
- ``settings``: ``get_settings()`` for that home.
- ``db``: migrated + seeded database (copied from a per-session template); yields an open
  ``Connection`` (deferred transaction; call ``db.commit()`` after writes you want other connections
  to see).
- ``clean_registry``: empty handler/job/periodic registry for the test, restored afterwards; plugin
  autoloading disabled.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import registry
from papi.core.config import Settings, get_settings, reset_settings


def _reset() -> None:
    core_db.reset_engines()
    reset_settings()


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated + seeded database built once per session."""
    home = tmp_path_factory.mktemp("papi_template")
    mp = pytest.MonkeyPatch()
    mp.setenv("PAPI_HOME", str(home))
    mp.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    _reset()
    try:
        from papi.core.bootstrap import init_home

        init_home()
        db_path = get_settings().paths.db
        with core_db.get_engine().connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
    finally:
        _reset()
        mp.undo()
    return db_path


@pytest.fixture(autouse=True)
def papi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "papi_home"
    home.mkdir()
    monkeypatch.setenv("PAPI_HOME", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.delenv("PAPI_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("PAPI_WEB_TOKEN", raising=False)
    _reset()
    yield home
    _reset()


@pytest.fixture
def settings(papi_home: Path) -> Settings:
    return get_settings()


@pytest.fixture
def db(papi_home: Path, _template_db: Path) -> Iterator[Connection]:
    s = get_settings()
    for d in (s.paths.raw, s.paths.logs, s.paths.backups, s.paths.claude_sandbox):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_template_db, s.paths.db)
    with core_db.connect() as conn:
        yield conn
        if conn.in_transaction():
            conn.rollback()


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[registry.Registry]:
    saved = registry.snapshot()
    monkeypatch.setattr(registry, "autoload_plugins", False)
    registry.clear()
    yield registry.REGISTRY
    registry.restore(saved)
