"""Alembic environment for papi (SQLite, batch mode, FTS5 tables excluded from autogenerate)."""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.types import TypeDecorator

from papi.core.schema import FTS_TABLES, metadata

config = context.config

if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    is_fts_or_internal = bool(name) and name.startswith((*FTS_TABLES, "sqlite_"))
    return not (type_ == "table" and is_fts_or_internal)


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    # Render our TypeDecorators as their storage type so migrations don't import application code.
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{type(obj.impl).__name__}()"
    return False


def _configure(connection: Any) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,
        include_object=include_object,
        render_item=render_item,
        compare_type=True,
    )


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    from pathlib import Path

    from papi.core.db import get_engine

    url = config.get_main_option("sqlalchemy.url")
    path = Path(url.removeprefix("sqlite:///")) if url else None
    with get_engine(path).connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
        if conn.in_transaction():
            conn.commit()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url") or "sqlite://",
        target_metadata=metadata,
        literal_binds=True,
        render_as_batch=True,
        include_object=include_object,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
