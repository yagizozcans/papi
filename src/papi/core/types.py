"""Column types shared by the schema.

- ``UtcDateTime``: stored as fixed-width UTC ISO text ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (sorts lexically);
  accepts aware ``datetime`` or ISO strings, returns aware UTC ``datetime``. Naive datetimes are rejected.
- ``DecimalText``: exact decimals stored as TEXT, returned as ``Decimal`` (SQLite has no decimal type;
  SQL arithmetic/SUM still works because SQLite coerces numeric text).
- ``Json``: JSON text; Python ``None`` is stored as SQL NULL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Text
from sqlalchemy.types import TypeDecorator

from papi.core.timeutil import iso_utc, parse_iso


class UtcDateTime(TypeDecorator[datetime]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = parse_iso(value)
        if not isinstance(value, datetime):
            raise TypeError(f"UtcDateTime expects datetime or ISO str, got {type(value).__name__}")
        return iso_utc(value)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return parse_iso(value)


class DecimalText(TypeDecorator[Decimal]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            value = Decimal(repr(value))
        return str(Decimal(value))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))


def Json() -> JSON:
    return JSON(none_as_null=True)
