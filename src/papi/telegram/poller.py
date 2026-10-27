"""Long-polling ``getUpdates`` receiver (08 §4.1).

Only ``telegram.allowed_user_id`` in a private chat (and, once set, ``telegram.chat_id``) is accepted;
every other update is logged and ignored without a reply. Each accepted message becomes a
``chat_message`` row (``direction='in'``, ``sent_at`` = Telegram's message date) and publishes
``chat.message.received.v1`` (processed by ``telegram.process_message``). Button presses are handled
right away. The offset is stored in ``kv_state`` (``telegram:offset``) in the same transaction as each
update, so an update is acknowledged exactly when its effects are committed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert

from papi.core import db
from papi.core.config import get_settings
from papi.core.events import CHAT_MESSAGE_RECEIVED, publish
from papi.core.ids import uuid7
from papi.core.jobs import PRIORITY_CHAT
from papi.core.schema import chat_message
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc
from papi.telegram.api import TelegramClient, get_client
from papi.telegram.bot import handle_callback
from papi.telegram.outbox import require_client

log = logging.getLogger("papi.telegram")

OFFSET_KEY = "telegram:offset"


@dataclass
class PollResult:
    received: int = 0
    messages: int = 0
    callbacks: int = 0
    ignored: int = 0


def authorized(user: dict[str, Any] | None, chat: dict[str, Any] | None) -> bool:
    s = get_settings().telegram
    if s.allowed_user_id is None or not user or not chat:
        return False
    if user.get("id") != s.allowed_user_id or chat.get("type") != "private":
        return False
    return s.chat_id is None or chat.get("id") == s.chat_id


def _store_message(conn: Connection, msg: dict[str, Any], update: dict[str, Any]) -> str | None:
    chat_id = msg["chat"]["id"]
    reply_to_id = None
    if reply := msg.get("reply_to_message"):
        reply_to_id = conn.execute(
            select(chat_message.c.id)
            .where(
                chat_message.c.telegram_chat_id == chat_id,
                chat_message.c.telegram_message_id == reply["message_id"],
            )
            .order_by(chat_message.c.direction.desc())  # prefer 'out' (my reply to a papi message)
            .limit(1)
        ).scalar_one_or_none()
    row_id = uuid7()
    res = conn.execute(
        insert(chat_message)
        .values(
            id=row_id,
            telegram_chat_id=chat_id,
            telegram_message_id=msg["message_id"],
            reply_to_id=reply_to_id,
            direction="in",
            text=msg.get("text") or msg.get("caption"),
            sent_at=datetime.fromtimestamp(int(msg["date"]), UTC),
            raw_update=update,
            created_at=now_utc(),
        )
        .on_conflict_do_nothing(
            index_elements=[
                chat_message.c.telegram_chat_id,
                chat_message.c.telegram_message_id,
                chat_message.c.direction,
            ]
        )
    )
    if res.rowcount != 1:
        return None  # already stored (update re-delivered)
    publish(
        conn,
        CHAT_MESSAGE_RECEIVED,
        f"chat_message/{row_id}",
        {"chat_message_id": row_id, "telegram_chat_id": chat_id, "telegram_message_id": msg["message_id"]},
        "telegram",
        priority=PRIORITY_CHAT,
    )
    return row_id


def handle_update(
    conn: Connection, update: dict[str, Any], client: TelegramClient, result: PollResult
) -> None:
    if msg := update.get("message"):
        if not authorized(msg.get("from"), msg.get("chat")):
            result.ignored += 1
            sender, chat = msg.get("from") or {}, msg.get("chat") or {}
            log.warning(
                "telegram: ignored message from unauthorised sender",
                extra={"user_id": sender.get("id"), "chat_id": chat.get("id"), "chat_type": chat.get("type")},
            )
            return
        if _store_message(conn, msg, update):
            result.messages += 1
        return
    if cq := update.get("callback_query"):
        message = cq.get("message") or {}
        if not authorized(cq.get("from"), message.get("chat")):
            result.ignored += 1
            log.warning(
                "telegram: ignored button press from unauthorised sender",
                extra={"user_id": (cq.get("from") or {}).get("id")},
            )
            return
        handle_callback(conn, cq, client)
        result.callbacks += 1
        return
    if edited := update.get("edited_message"):
        if authorized(edited.get("from"), edited.get("chat")):
            log.info(
                "telegram: edited message %s ignored (send a new message instead)", edited.get("message_id")
            )
        else:
            log.warning("telegram: ignored edited message from unauthorised sender")
        result.ignored += 1
        return
    result.ignored += 1
    log.info("telegram: ignored update without message/callback (keys: %s)", sorted(update))


def poll_once(*, timeout: int | None = None, client: TelegramClient | None = None) -> PollResult:
    """One ``getUpdates`` round (long poll up to ``timeout`` s, default ``telegram.poll_timeout_s``)."""
    client = client or require_client()
    with db.connect() as conn:
        offset = get_state(conn, OFFSET_KEY)
    timeout = get_settings().telegram.poll_timeout_s if timeout is None else timeout
    updates = client.get_updates(offset, timeout=timeout)
    result = PollResult(received=len(updates))
    for upd in sorted(updates, key=lambda u: u["update_id"]):
        try:
            with db.transaction() as conn:
                current = get_state(conn, OFFSET_KEY)
                if current is not None and upd["update_id"] < current:
                    continue  # processed by a concurrent poll
                handle_update(conn, upd, client, result)
                set_state(conn, OFFSET_KEY, upd["update_id"] + 1)
        except Exception:
            # A failing update must not block the queue forever: skip it (effects rolled back).
            log.exception("telegram: update %s failed and was skipped", upd["update_id"])
            with db.transaction() as conn:
                set_state(conn, OFFSET_KEY, upd["update_id"] + 1)
    return result


def ready_to_poll() -> bool:
    s = get_settings().telegram
    return bool(s.enabled and s.allowed_user_id is not None and get_client() is not None)
