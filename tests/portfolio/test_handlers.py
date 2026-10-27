from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import Connection, select

from papi.core import schema as s
from papi.core.events import PORTFOLIO_CHANGED, get_event
from papi.core.registry import Registry
from papi.core.timeutil import ist_date
from papi.portfolio import handlers, service
from tests.portfolio.conftest import add_corporate_action, add_instrument


def test_handlers_register_and_run(db: Connection, clean_registry: Registry) -> None:
    importlib.reload(handlers)
    assert [h.name for h in clean_registry.handlers[PORTFOLIO_CHANGED]] == ["portfolio.snapshot_on_change"]
    assert {"portfolio.daily_snapshot", "portfolio.corporate_actions"} <= set(clean_registry.periodics)

    iid = add_instrument(db, "XYZ", "XYZ A.Ş.")
    today = ist_date()
    service.record_transaction(
        db,
        service.ProposedTxn(
            instrument_id=iid,
            type="buy",
            quantity=Decimal(10),
            price=Decimal(5),
            trade_date=today - timedelta(days=5),
        ),
    )
    add_corporate_action(db, iid, "bonus", today, factor="1")
    # the publish enqueued the snapshot job in the same transaction
    job = db.execute(select(s.job).where(s.job.c.kind == "event:portfolio.snapshot_on_change")).one()
    db.commit()

    handlers.snapshot_on_change(get_event(db, job.payload["event_id"]), None)
    db.rollback()
    snap = db.execute(select(s.position_snapshot)).one()
    assert snap.quantity == 10 and snap.as_of == today

    handlers.corporate_actions(None)
    db.rollback()
    [pa] = service.pending_actions(db)
    assert pa["proposal"]["quantity"] == "10"

    service.record_transaction(
        db,
        service.ProposedTxn(
            instrument_id=iid,
            type="sell",
            quantity=Decimal(4),
            price=Decimal(6),
            trade_date=today - timedelta(days=1),
        ),
    )
    db.commit()
    handlers.daily_snapshot(None)
    db.rollback()
    assert db.execute(select(s.position_snapshot.c.quantity)).scalar_one() == 6
