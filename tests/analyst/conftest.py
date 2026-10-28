from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import Connection

from papi.core.registry import Registry
from papi.triage.classify import triage_disclosure
from tests.analyst.helpers import seed_company, seed_disclosure, seed_holding, seed_price


@pytest.fixture(autouse=True)
def _hermetic(clean_registry: Registry) -> None:
    """No other slice's handlers: ``publish`` only writes ``event_log`` rows."""


@dataclass
class World:
    issuer_id: str
    instrument_id: str
    disclosure_id: str
    event_id: str


@pytest.fixture
def world(db: Connection) -> World:
    """MEKAG (held → P1) with a 'Yeni İş İlişkisi' disclosure (fixture 1688011), triaged."""
    iss, inst = seed_company(db, "MEKAG", shares=100_000_000)
    seed_price(db, inst, date(2026, 10, 24), 41.2)
    seed_holding(db, inst)
    did = seed_disclosure(db, index="1688011", issuer_id=iss)
    (eid,) = triage_disclosure(db, did)
    db.commit()
    return World(iss, inst, did, eid)
