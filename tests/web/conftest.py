"""Dashboard fixtures: a seeded DB, other slices replaced by monkeypatches, a TestClient."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.web import integrations
from papi.web.app import create_app
from tests.web import sample


@pytest.fixture
def world(db: Connection, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ids = sample.seed(db)
    db.commit()
    monkeypatch.setattr(integrations, "_taxonomy", lambda: None)
    monkeypatch.setattr(integrations, "positions", lambda conn: list(ids["positions"]))
    monkeypatch.setattr(integrations, "pending_actions", lambda conn, status="expected": [])
    monkeypatch.setattr(integrations, "governor_status", lambda conn: None)
    return ids


@pytest.fixture
def client(world: dict[str, Any]) -> Iterator[TestClient]:
    with TestClient(create_app(stream_poll_seconds=0.05, stream_max_seconds=1.0)) as c:
        yield c
