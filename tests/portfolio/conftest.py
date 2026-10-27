from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Connection

from papi.core import schema as s
from papi.core.ids import uuid7
from papi.core.rawstore import source_id_for
from papi.core.registry import Registry
from papi.core.timeutil import now_utc


@pytest.fixture(autouse=True)
def _hermetic_registry(clean_registry: Registry) -> Iterator[None]:
    """publish() must not import other slices' handlers in these tests."""
    yield


def add_instrument(
    conn: Connection,
    ticker: str,
    legal_name: str | None = None,
    short_name: str | None = None,
    *,
    sector: str | None = None,
    itype: str = "equity",
    valid_from: date = s.OPEN_START,
    valid_to: date | None = None,
) -> str:
    now = now_utc()
    issuer_id = None
    if legal_name is not None:
        issuer_id = uuid7()
        conn.execute(
            s.issuer.insert().values(
                id=issuer_id,
                legal_name=legal_name,
                short_name=short_name,
                sector_code=sector,
                kind="company",
                created_at=now,
            )
        )
    iid = uuid7()
    conn.execute(s.instrument.insert().values(id=iid, issuer_id=issuer_id, type=itype, created_at=now))
    add_ticker(conn, iid, ticker, valid_from=valid_from, valid_to=valid_to)
    return iid


def add_ticker(
    conn: Connection, iid: str, ticker: str, *, valid_from: date = s.OPEN_START, valid_to: date | None = None
) -> None:
    conn.execute(
        s.identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=iid,
            scheme="TICKER",
            value=ticker,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_at=now_utc(),
        )
    )


def add_close(conn: Connection, iid: str, d: date, close: float, source: str = "isyatirim_prices") -> None:
    conn.execute(
        s.price_bar.insert().values(
            instrument_id=iid,
            interval="1d",
            ts=d,
            source_id=source_id_for(conn, source),
            close=close,
            recorded_at=now_utc(),
        )
    )


def add_corporate_action(
    conn: Connection,
    iid: str,
    ca_type: str,
    ex_date: date | None,
    *,
    factor: str | None = None,
    cash_amount: str | None = None,
    details: dict | None = None,
    status: str = "approved",
) -> str:
    ca_id = uuid7()
    conn.execute(
        s.corporate_action.insert().values(
            id=ca_id,
            instrument_id=iid,
            type=ca_type,
            ex_date=ex_date,
            factor=factor,
            cash_amount=cash_amount,
            currency="TRY",
            status=status,
            details=details,
            created_at=now_utc(),
        )
    )
    return ca_id
