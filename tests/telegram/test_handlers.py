"""Registered background work: the poll periodic and the chat.message.received handler."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from sqlalchemy import Connection, select

from papi.core import db as core_db
from papi.core.events import CHAT_MESSAGE_RECEIVED, get_event
from papi.core.registry import REGISTRY
from papi.core.schema import chat_message, event_log, job
from tests.telegram.helpers import FakeTelegram, message_update


def _load() -> object:
    return importlib.reload(importlib.import_module("papi.telegram.handlers"))


def test_registration_and_poll_interval(settings, monkeypatch) -> None:
    handlers = _load()
    assert "periodic:telegram:poll" in REGISTRY.all_kinds()
    assert "event:telegram.process_message" in REGISTRY.all_kinds()
    now = datetime.now(UTC)
    assert handlers._poll_interval(now) == 300.0  # not set up → cheap no-op cadence
    monkeypatch.setenv("PAPI_TELEGRAM_TOKEN", "1:x")
    settings.telegram.enabled = True
    settings.telegram.allowed_user_id = 1
    from papi.telegram.api import reset_client

    reset_client()
    assert handlers._poll_interval(now) == 1.0


def test_poll_then_handler_processes_message(db: Connection, fake_tg: FakeTelegram) -> None:
    handlers = _load()
    fake_tg.updates = [message_update(1, "/yardim")]
    handlers.poll(None)
    with core_db.connect() as conn:
        ev_row = (
            conn.execute(select(event_log).where(event_log.c.type == CHAT_MESSAGE_RECEIVED)).mappings().one()
        )
        queued = conn.execute(select(job.c.kind, job.c.priority)).all()
        event = get_event(conn, ev_row["id"])
    assert ("event:telegram.process_message", 5) in [tuple(q) for q in queued]  # chat priority
    handlers.process_message(event, None)
    assert "/mutabakat" in fake_tg.last_text()
    with core_db.connect() as conn:
        row = conn.execute(select(chat_message).where(chat_message.c.direction == "in")).mappings().one()
    assert row["processed_at"] is not None and row["intent"] == "command"


def test_poll_is_noop_when_not_configured(db: Connection, settings) -> None:
    handlers = _load()
    settings.telegram.enabled = False
    handlers.poll(None)  # no token, no network: returns quietly
