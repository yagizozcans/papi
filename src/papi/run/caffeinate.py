"""Optional stay-awake (09 §4): ``caffeinate -s`` while the BIST session is open, if
``[run].prevent_sleep_on_ac`` is true. ``-s`` only prevents system sleep on AC power; ``-w <pid>``
makes the child exit with papi even after a crash."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime

from sqlalchemy import select

from papi.core import db
from papi.core.schema import trading_calendar
from papi.core.timeutil import to_ist
from papi.run.config import RunConfig

log = logging.getLogger("papi.run.caffeinate")

CALENDAR_ID = "XIST"


def market_open(now: datetime, cfg: RunConfig) -> bool:
    """``trading_calendar`` (XIST) says the session is open at ``now``; days without a row use
    weekdays ``[run].session_start``–``session_end`` (Istanbul)."""
    ist = to_ist(now)
    with db.connect() as conn:
        rows = conn.execute(
            select(
                trading_calendar.c.session_type, trading_calendar.c.open_utc, trading_calendar.c.close_utc
            ).where(trading_calendar.c.calendar_id == CALENDAR_ID, trading_calendar.c.date == ist.date())
        ).all()
    if any(r.session_type == "closed" for r in rows):
        return False
    for r in rows:
        if r.open_utc is not None and r.close_utc is not None:
            return r.open_utc <= now < r.close_utc
    return ist.weekday() < 5 and cfg.session_start <= ist.time() < cfg.session_end


class Caffeinator:
    """Keeps one ``caffeinate`` child alive while ``want`` is true (call ``update`` periodically)."""

    def __init__(self, binary: str = "caffeinate") -> None:
        self.binary = binary
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def update(self, want: bool) -> None:
        if want and not self.active:
            if not self.available():
                log.warning("%s not found; cannot prevent sleep", self.binary)
                return
            self.proc = subprocess.Popen(
                [self.binary, "-s", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("caffeinate started (pid %s): market session open", self.proc.pid)
        elif not want and self.proc is not None:
            self.stop()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.info("caffeinate stopped")
