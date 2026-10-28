from __future__ import annotations

from datetime import UTC, date, datetime

from papi.core import db as core_db
from papi.core.ids import uuid7
from papi.core.schema import trading_calendar
from papi.run.caffeinate import Caffeinator, market_open
from papi.run.config import run_config


def test_market_open_uses_calendar_then_default_session(db, settings):
    cfg = run_config()
    wed_10 = datetime(2026, 10, 28, 7, 0, tzinfo=UTC)  # Wednesday 10:00 Istanbul
    wed_20 = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)  # 20:00 Istanbul
    sat_10 = datetime(2026, 10, 31, 7, 0, tzinfo=UTC)
    assert market_open(wed_10, cfg)
    assert not market_open(wed_20, cfg)
    assert not market_open(sat_10, cfg)
    with core_db.transaction() as conn:
        conn.execute(
            trading_calendar.insert().values(
                id=uuid7(),
                calendar_id="XIST",
                date=date(2026, 10, 28),
                session_type="closed",
                is_half_day=False,
            )
        )
    assert not market_open(wed_10, cfg)


def test_caffeinator_starts_and_stops_child(tmp_path):
    fake = tmp_path / "caffeinate"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    c = Caffeinator(binary=str(fake))
    c.update(True)
    assert c.active
    pid = c.proc.pid
    c.update(True)  # idempotent
    assert c.proc.pid == pid
    c.update(False)
    assert not c.active and c.proc is None
    c.stop()


def test_caffeinator_missing_binary_is_a_noop(tmp_path):
    c = Caffeinator(binary=str(tmp_path / "nope"))
    c.update(True)
    assert not c.active
