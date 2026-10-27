"""Outgoing Telegram messages: every message papi sends is logged as a ``chat_message`` row
(``direction = 'out'``), which also drives the per-minute rate limit (``telegram.max_messages_per_minute``).

Interactive replies (answers to my own messages and button presses) are always sent; alert senders
check :func:`rate_budget` first and batch the overflow (see ``papi.notify.alerts``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.jobs import RetryLater
from papi.core.schema import chat_message
from papi.core.timeutil import now_utc
from papi.telegram.api import TelegramClient, TelegramError, chunk_html, get_client


class TelegramUnavailable(RuntimeError):
    """Telegram is disabled or not set up (no token / chat id)."""


@dataclass(frozen=True)
class Sent:
    chat_message_id: str
    message_id: int


def chat_id() -> int | None:
    return get_settings().telegram.chat_id


def is_ready() -> bool:
    """Telegram alerts can be delivered: enabled, chat id configured and a token present."""
    s = get_settings().telegram
    return bool(s.enabled and s.chat_id and get_client() is not None)


def require_client() -> TelegramClient:
    client = get_client()
    if client is None:
        raise TelegramUnavailable("Telegram token yok (papi telegram setup)")
    return client


def rate_budget(conn: Connection, now: datetime | None = None) -> int:
    """Messages that may still be sent in the current 60-second window."""
    now = now or now_utc()
    sent = conn.execute(
        select(func.count())
        .select_from(chat_message)
        .where(chat_message.c.direction == "out", chat_message.c.sent_at > now - timedelta(seconds=60))
    ).scalar_one()
    return max(get_settings().telegram.max_messages_per_minute - int(sent), 0)


def window_frees_at(conn: Connection, now: datetime | None = None) -> datetime:
    """When the oldest message of the current window drops out (≥ now + 1 s)."""
    now = now or now_utc()
    oldest = conn.execute(
        select(func.min(chat_message.c.sent_at)).where(
            chat_message.c.direction == "out", chat_message.c.sent_at > now - timedelta(seconds=60)
        )
    ).scalar_one()
    if oldest is None:
        return now + timedelta(seconds=1)
    return max(oldest + timedelta(seconds=61), now + timedelta(seconds=1))


def send(
    conn: Connection,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
    reply_to_id: str | None = None,
    related_event_id: str | None = None,
    intent: str | None = None,
    status: str | None = None,
    proposed_transaction: dict[str, Any] | None = None,
    client: TelegramClient | None = None,
    to_chat: int | None = None,
) -> Sent:
    """Send ``text`` (HTML) to my chat and log it. Long texts are split; the keyboard goes on the last
    part and the returned :class:`Sent` refers to the first part (used for reply threading).

    Telegram flood control (HTTP 429) becomes ``RetryLater`` so the job is retried after the advised
    delay.
    """
    client = client or require_client()
    target = to_chat if to_chat is not None else chat_id()
    if target is None:
        raise TelegramUnavailable("telegram.chat_id ayarlı değil (papi telegram setup)")
    parts = chunk_html(text)
    first: Sent | None = None
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        try:
            msg = client.send_message(
                target,
                part,
                reply_to_message_id=reply_to_message_id if i == 0 else None,
                reply_markup=reply_markup if last else None,
            )
        except TelegramError as exc:
            if exc.retry_after:
                raise RetryLater(float(exc.retry_after), str(exc)) from exc
            raise
        row_id = uuid7()
        conn.execute(
            chat_message.insert().values(
                id=row_id,
                telegram_chat_id=target,
                telegram_message_id=msg["message_id"],
                reply_to_id=reply_to_id,
                direction="out",
                text=part,
                sent_at=now_utc(),
                processed_at=now_utc(),
                intent=intent,
                related_event_id=related_event_id,
                proposed_transaction=proposed_transaction if last else None,
                status=status,
                created_at=now_utc(),
            )
        )
        if first is None:
            first = Sent(row_id, int(msg["message_id"]))
    assert first is not None
    return first
