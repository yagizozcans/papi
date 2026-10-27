"""Per-message processing context and the single pending-dialog state (papi has one user).

The pending dialog lives in ``kv_state`` under ``telegram:pending`` and is one of::

    {"kind": "needs_info", "msg": <chat_message id>, "draft": {...}, "missing": [...]}
    {"kind": "edit", "msg": <chat_message id>, "field": "q|p|d|f|i"}
    {"kind": "reconcile", "items": [{"ticker", "quantity"}], "idx": n, "observed": {ticker: qty}}
    {"kind": "reconcile_extra", "observed": {...}}
    {"kind": "reconcile_confirm", "observed": {...}}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection

from papi.core.state import delete_state, get_state, set_state
from papi.core.timeutil import to_ist
from papi.telegram import outbox
from papi.telegram.api import TelegramClient

PENDING_KEY = "telegram:pending"
DELAY_NOTE_AFTER = timedelta(minutes=2)


def get_pending(conn: Connection) -> dict[str, Any] | None:
    return get_state(conn, PENDING_KEY)


def set_pending(conn: Connection, value: dict[str, Any]) -> None:
    set_state(conn, PENDING_KEY, value)


def clear_pending(conn: Connection) -> None:
    delete_state(conn, PENDING_KEY)


def delay_note(received: datetime | None, processed: datetime) -> str | None:
    """ "alındı 14:02, işlendi 14:35" when a message waited more than two minutes (Mac asleep)."""
    if received is None or processed - received <= DELAY_NOTE_AFTER:
        return None
    fmt = "%H:%M" if to_ist(received).date() == to_ist(processed).date() else "%d.%m %H:%M"
    return f"<i>alındı {to_ist(received).strftime(fmt)}, işlendi {to_ist(processed).strftime('%H:%M')}</i>"


@dataclass
class Turn:
    """Replying to one inbound message (``msg`` is its ``chat_message`` row, or None for callbacks)."""

    conn: Connection
    client: TelegramClient
    now: datetime
    msg: Any = None
    chat: int | None = None
    note: str | None = None
    _noted: bool = False

    @property
    def msg_id(self) -> str | None:
        return self.msg["id"] if self.msg is not None else None

    def reply(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        intent: str | None = None,
        quote: bool = False,
        reply_to_message_id: int | None = None,
    ) -> outbox.Sent:
        if self.note and not self._noted:
            text = f"{text}\n\n{self.note}"
            self._noted = True
        if reply_to_message_id is None and quote and self.msg is not None:
            reply_to_message_id = self.msg["telegram_message_id"]
        return outbox.send(
            self.conn,
            text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            reply_to_id=self.msg_id,
            intent=intent,
            client=self.client,
        )
