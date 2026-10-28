from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from papi.core import db as core_db
from papi.core.state import get_state, set_state


def test_deferred_read_then_write_fails_once_another_writer_commits(db):
    # The failure seen live in the analyst worker (inflight lock after build_context reads): the
    # upgrade of a stale deferred snapshot is refused at once, busy_timeout does not help.
    with core_db.connect() as a, core_db.connect() as b:
        get_state(a, "x")
        set_state(b, "y", 1)
        b.commit()
        with pytest.raises(OperationalError, match="locked"):
            set_state(a, "x", 1)
        a.rollback()


def test_immediate_connection_takes_the_write_lock_on_first_read(db):
    with core_db.connect(immediate=True) as a, core_db.connect() as b:
        b.exec_driver_sql("PRAGMA busy_timeout=50")
        get_state(a, "x")
        with pytest.raises(OperationalError, match="locked"):
            set_state(b, "y", 1)  # waits for a's lock instead of overtaking a's snapshot
        b.rollback()
        set_state(a, "x", 1)
        a.commit()
        assert get_state(b, "x") == 1
        b.exec_driver_sql("PRAGMA busy_timeout=5000")
        b.rollback()
