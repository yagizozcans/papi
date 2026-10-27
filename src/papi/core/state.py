"""Small persisted key/value state (``kv_state`` table): cursors, last-poll times, governor state.

Key naming: ``<slice>:<name>``, e.g. ``kap:last_index``, ``kap:last_poll_at``, ``llm:paused_until``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert

from papi.core.schema import kv_state
from papi.core.timeutil import now_utc


def get_state(conn: Connection, key: str, default: Any = None) -> Any:
    value = conn.execute(select(kv_state.c.value).where(kv_state.c.key == key)).scalar_one_or_none()
    return default if value is None else value


def set_state(conn: Connection, key: str, value: Any) -> None:
    """Upsert a JSON-serialisable value."""
    stmt = insert(kv_state).values(key=key, value=value, updated_at=now_utc())
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[kv_state.c.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
    )


def delete_state(conn: Connection, key: str) -> None:
    conn.execute(kv_state.delete().where(kv_state.c.key == key))
