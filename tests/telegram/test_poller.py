"""getUpdates poller: auth filter, offset handling, message storage + event publication."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Connection, select

from papi.core import db as core_db
from papi.core.events import CHAT_MESSAGE_RECEIVED
from papi.core.schema import chat_message, event_log
from papi.core.state import get_state
from papi.telegram.poller import OFFSET_KEY, poll_once
from tests.telegram.helpers import CHAT_ID, USER_ID, FakeTelegram, callback_update, message_update


def _rows(direction: str = "in") -> list[dict]:
    with core_db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(chat_message)
                .where(chat_message.c.direction == direction)
                .order_by(chat_message.c.created_at)
            ).mappings()
        ]


def test_accepts_only_my_private_chat(
    db: Connection, fake_tg: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    fake_tg.updates = [
        message_update(1, "THYAO 100 lot 285,40'tan aldım", user_id=999, chat_id=999),  # stranger
        message_update(2, "selam", chat_type="group", chat_id=-100),  # me, but in a group
        message_update(3, "selam", chat_id=12345),  # me, other private chat id
        callback_update(4, "t:ok:x", message_id=1, user_id=999),  # stranger pressing a button
        message_update(5, "merhaba"),
    ]
    with caplog.at_level(logging.WARNING, logger="papi.telegram"):
        result = poll_once(timeout=0)
    assert (result.received, result.messages, result.ignored, result.callbacks) == (5, 1, 4, 0)
    rows = _rows()
    assert [r["text"] for r in rows] == ["merhaba"]
    assert fake_tg.methods() == ["getUpdates"]  # no reply to anyone, no callback answer
    assert sum("unauthorised" in r.getMessage() for r in caplog.records) == 4


def test_nobody_is_accepted_before_setup(db: Connection, fake_tg: FakeTelegram, settings) -> None:
    settings.telegram.allowed_user_id = None
    fake_tg.updates = [message_update(1, "merhaba")]
    result = poll_once(timeout=0)
    assert result.ignored == 1 and _rows() == []


def test_message_row_event_and_offset(db: Connection, fake_tg: FakeTelegram) -> None:
    sent = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=40)
    fake_tg.updates = [message_update(10, "merhaba", date_=sent), message_update(11, "ikinci", message_id=12)]
    poll_once(timeout=0)
    rows = _rows()
    assert [r["telegram_message_id"] for r in rows] == [10, 12]
    first = rows[0]
    assert first["telegram_chat_id"] == CHAT_ID and first["direction"] == "in"
    assert first["sent_at"] == sent  # Telegram time, not processing time
    assert first["processed_at"] is None
    assert first["raw_update"]["message"]["from"]["id"] == USER_ID
    with core_db.connect() as conn:
        assert get_state(conn, OFFSET_KEY) == 12
        events = (
            conn.execute(select(event_log).where(event_log.c.type == CHAT_MESSAGE_RECEIVED)).mappings().all()
        )
    assert [e["data"]["chat_message_id"] for e in events] == [r["id"] for r in rows]
    assert events[0]["data"]["telegram_message_id"] == 10

    # next poll asks for updates after the stored offset; a re-delivered update is not duplicated
    fake_tg.updates.append(message_update(12, "üçüncü", message_id=13))
    poll_once(timeout=0)
    assert fake_tg.bodies("getUpdates")[-1]["offset"] == 12
    assert [r["text"] for r in _rows()] == ["merhaba", "ikinci", "üçüncü"]


def test_redelivered_update_is_stored_once(db: Connection, fake_tg: FakeTelegram) -> None:
    fake_tg.updates = [message_update(1, "merhaba")]
    poll_once(timeout=0)
    with core_db.transaction() as conn:
        conn.execute(event_log.delete())
        from papi.core.state import delete_state

        delete_state(conn, OFFSET_KEY)  # e.g. state lost: Telegram re-sends update 1
    poll_once(timeout=0)
    assert len(_rows()) == 1
    with core_db.connect() as conn:
        assert conn.execute(select(event_log)).first() is None  # no second event either


def test_reply_links_to_my_outgoing_message(db: Connection, fake_tg: FakeTelegram) -> None:
    from papi.telegram.outbox import send

    with core_db.transaction() as conn:
        out = send(conn, "uyarı")
    fake_tg.updates = [message_update(1, "bu ne demek", reply_to=out.message_id)]
    poll_once(timeout=0)
    assert _rows()[0]["reply_to_id"] == out.chat_message_id


def test_failing_update_is_skipped_not_stuck(db: Connection, fake_tg: FakeTelegram, monkeypatch) -> None:
    import papi.telegram.poller as poller

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(poller, "handle_callback", boom)
    fake_tg.updates = [callback_update(1, "t:ok:x", message_id=5), message_update(2, "merhaba")]
    result = poll_once(timeout=0)
    assert result.messages == 1
    with core_db.connect() as conn:
        assert get_state(conn, OFFSET_KEY) == 3
