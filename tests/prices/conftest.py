from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection

from papi.core.ids import uuid7
from papi.core.rawstore import source_id_for
from papi.core.schema import OPEN_START, identifier, instrument, price_bar
from papi.core.timeutil import now_utc
from papi.sources.prices import service
from papi.sources.prices.base import Throttle

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def add_instrument(conn: Connection, ticker: str, type_: str = "equity") -> str:
    iid = uuid7()
    conn.execute(
        instrument.insert().values(id=iid, type=type_, currency="TRY", status="active", created_at=now_utc())
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=iid,
            scheme="TICKER",
            value=ticker,
            valid_from=OPEN_START,
            recorded_at=now_utc(),
        )
    )
    return iid


def add_bar(conn: Connection, iid: str, ts: date, source_code: str, close: float, **extra: Any) -> None:
    conn.execute(
        price_bar.insert().values(
            instrument_id=iid,
            interval="1d",
            ts=ts,
            source_id=source_id_for(conn, source_code),
            close=close,
            recorded_at=now_utc(),
            **extra,
        )
    )


@pytest.fixture
def no_throttle() -> Throttle:
    return Throttle(0.0)


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake(conn: Connection, text: str, *, level: str, dedupe_key: str) -> None:
        sent.append({"text": text, "level": level, "dedupe_key": dedupe_key})

    monkeypatch.setattr(service, "_ops_alert", fake)
    return sent
