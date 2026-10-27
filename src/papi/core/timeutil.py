"""Time helpers. Storage is UTC ISO-8601 text; presentation is Europe/Istanbul.

KAP publishes local Istanbul time as ``DD.MM.YYYY HH:MM:SS`` (and sometimes ``YYYY.MM.DD HH:MM:SS``);
``parse_kap_datetime`` converts those to aware UTC datetimes.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

IST: tzinfo = ZoneInfo("Europe/Istanbul")
UTC_TZ: tzinfo = UTC

ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def now_ist() -> datetime:
    """Current time as an aware Europe/Istanbul datetime."""
    return datetime.now(IST)


def to_utc(value: datetime | str, *, assume_tz: tzinfo = IST) -> datetime:
    """Convert to an aware UTC datetime.

    Strings are parsed as ISO-8601 (a trailing ``Z`` is accepted). Naive values are interpreted in
    ``assume_tz`` (Istanbul by default, since that is what Turkish sources publish).
    """
    if isinstance(value, str):
        value = parse_iso(value, assume_tz=assume_tz)
    if value.tzinfo is None:
        value = value.replace(tzinfo=assume_tz)
    return value.astimezone(UTC)


def to_ist(value: datetime | str) -> datetime:
    """Convert to an aware Europe/Istanbul datetime (naive input is taken as UTC)."""
    if isinstance(value, str):
        value = parse_iso(value, assume_tz=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def parse_iso(value: str, *, assume_tz: tzinfo = UTC) -> datetime:
    """Parse an ISO-8601 string to an aware datetime; naive strings get ``assume_tz``."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt


def iso_utc(value: datetime) -> str:
    """Canonical storage string: ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC, fixed width, sorts lexically).

    Naive datetimes are rejected to avoid silently mixing time zones.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime; use to_utc() with an explicit assume_tz")
    return value.astimezone(UTC).strftime(ISO_UTC_FORMAT)


def ist_date(value: datetime | None = None) -> date:
    """Calendar date in Istanbul for ``value`` (default: now)."""
    return to_ist(value or now_utc()).date()


_KAP_DMY = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_KAP_YMD = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def parse_kap_datetime(value: str) -> datetime:
    """Parse a KAP local timestamp to an aware UTC datetime.

    Accepted (Istanbul local time): ``DD.MM.YYYY HH:MM:SS``, ``YYYY.MM.DD HH:MM:SS``; seconds and the
    whole time part are optional (a missing time means 00:00:00). Raises ``ValueError`` otherwise.
    """
    text = " ".join(value.split())
    m = _KAP_DMY.match(text)
    if m:
        day, month, year = int(m[1]), int(m[2]), int(m[3])
    else:
        m = _KAP_YMD.match(text)
        if not m:
            raise ValueError(f"unrecognised KAP datetime: {value!r}")
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    hour = int(m[4]) if m[4] else 0
    minute = int(m[5]) if m[5] else 0
    second = int(m[6]) if m[6] else 0
    local = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return local.astimezone(UTC)
