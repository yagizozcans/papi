"""Session-aware poll cadence (02 §3.1).

``kap.session_poll_seconds`` on Istanbul weekdays from 09:30 until 23:00 (trading session 09:30–19:30
plus the evening disclosure peak), unless ``trading_calendar`` marks the day ``closed`` (calendar
``XIST``); otherwise ``kap.offhours_poll_seconds``.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime, time

from sqlalchemy import select

from papi.core import db
from papi.core.config import get_settings
from papi.core.schema import trading_calendar
from papi.core.timeutil import to_ist

SESSION_START = time(9, 30)
EVENING_END = time(23, 0)
CALENDAR_ID = "XIST"
_CACHE_TTL_S = 3600.0

_closed_cache: dict[date, tuple[float, bool]] = {}
_cache_lock = threading.Lock()


def is_closed_day(day: date) -> bool:
    """``trading_calendar`` says the exchange is closed (holiday). Unknown days count as open."""
    with _cache_lock:
        hit = _closed_cache.get(day)
        if hit is not None and _time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
    with db.connect() as conn:
        closed = (
            conn.execute(
                select(trading_calendar.c.id).where(
                    trading_calendar.c.calendar_id == CALENDAR_ID,
                    trading_calendar.c.date == day,
                    trading_calendar.c.session_type == "closed",
                )
            ).first()
            is not None
        )
    with _cache_lock:
        _closed_cache[day] = (_time.monotonic(), closed)
    return closed


def clear_cache() -> None:
    with _cache_lock:
        _closed_cache.clear()


def in_session_window(now: datetime) -> bool:
    ist = to_ist(now)
    if ist.weekday() >= 5:
        return False
    if not (SESSION_START <= ist.time() < EVENING_END):
        return False
    return not is_closed_day(ist.date())


def poll_interval_seconds(now: datetime) -> float:
    cfg = get_settings().kap
    return float(cfg.session_poll_seconds if in_session_window(now) else cfg.offhours_poll_seconds)
