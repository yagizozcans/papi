from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, inspect, select

from papi.core import db as core_db
from papi.core.bootstrap import init_home, seed
from papi.core.config import get_settings
from papi.core.schema import FTS_TABLES, jurisdiction, market, metadata, portfolio, source, venue


def test_init_from_empty_creates_everything(papi_home):
    report = init_home()
    s = get_settings()
    assert report.db_created and report.config_written
    assert report.revision_before is None and report.revision_after == "0001"
    assert s.paths.db.exists() and s.config_path.exists()
    for d in (s.paths.raw, s.paths.logs, s.paths.backups, s.paths.claude_sandbox):
        assert d.is_dir()

    insp = inspect(core_db.get_engine())
    tables = set(insp.get_table_names())
    assert set(metadata.tables) <= tables
    assert set(FTS_TABLES) <= tables
    assert core_db.current_revision() == "0001"


def test_migration_matches_metadata(papi_home):
    init_home()
    with core_db.get_engine().connect() as conn:
        mc = MigrationContext.configure(
            conn,
            opts={
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    not (
                        type_ == "table"
                        and name
                        and (name.startswith(FTS_TABLES) or name.startswith("alembic"))
                    )
                ),
                "compare_type": False,
            },
        )
        diff = compare_metadata(mc, metadata)
    assert diff == []


def test_pragmas(papi_home):
    init_home()
    with core_db.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL


def test_downgrade_and_upgrade_again(papi_home):
    init_home()
    cfg = core_db.alembic_config()
    with core_db.get_engine().connect() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
        conn.commit()
    assert core_db.current_revision() is None
    names = set(inspect(core_db.get_engine()).get_table_names())
    assert not (set(metadata.tables) & names)
    core_db.migrate()
    assert core_db.current_revision() == "0001"


def test_init_is_idempotent(papi_home):
    first = init_home()
    assert first.changed
    second = init_home()
    assert not second.changed
    assert second.seeded == {"jurisdiction": 0, "market": 0, "venue": 0, "source": 0, "portfolio": 0}
    with core_db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(source)).scalar() == 5
        assert conn.execute(select(func.count()).select_from(portfolio)).scalar() == 1
        assert conn.execute(select(jurisdiction.c.code)).scalars().all() == ["TR"]
        assert conn.execute(select(market.c.code)).scalars().all() == ["BIST_EQ"]
        assert conn.execute(select(venue.c.mic)).scalars().all() == ["XIST"]
        codes = set(conn.execute(select(source.c.code)).scalars())
    assert codes == {"kap", "legal_corpus", "isyatirim_prices", "yahoo_prices", "tefas_prices"}


def test_seed_twice_in_db_fixture(db, settings):
    assert seed(db, settings) == {"jurisdiction": 0, "market": 0, "venue": 0, "source": 0, "portfolio": 0}
    row = db.execute(select(portfolio)).one()
    assert row.name == settings.portfolio.default_name and row.is_default is True


def test_existing_config_is_not_overwritten(papi_home):
    (papi_home / "config.toml").write_text('[portfolio]\ndefault_name = "Hesap"\n', encoding="utf-8")
    report = init_home()
    assert not report.config_written
    assert (papi_home / "config.toml").read_text(encoding="utf-8").startswith("[portfolio]")
    with core_db.connect() as conn:
        assert conn.execute(select(portfolio.c.name)).scalar_one() == "Hesap"
