"""Calls into other slices (contracts C3/C4), kept in one place so tests can monkeypatch them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import Connection


def ops_alert(conn: Connection, text: str, *, level: str = "warning", dedupe_key: str | None = None) -> None:
    """Health problem → ``papi.notify.ops.ops_alert`` (C4)."""
    from papi.notify.ops import ops_alert as _ops_alert

    _ops_alert(conn, text, level=level, dedupe_key=dedupe_key)


def holding_priority(
    conn: Connection, instrument_ids: list[str], at: datetime | None = None
) -> Literal["P1", "P2"] | None:
    """``P1`` held / ``P2`` watched or recently sold / ``None`` — ``papi.portfolio.service`` (C3)."""
    if not instrument_ids:
        return None
    from papi.portfolio.service import holding_priority as _holding_priority

    return _holding_priority(conn, instrument_ids, at)
