"""Telegram background work (auto-imported by ``papi.core.registry.load_plugins``).

- periodic ``telegram:poll`` — one long-poll ``getUpdates`` round per run (every second while
  Telegram is enabled and set up; every 5 minutes otherwise, as a cheap no-op). The job blocks its
  worker lane for up to ``telegram.poll_timeout_s``, so the runner should give it a lane of its own
  (job kind ``periodic:telegram:poll``).
- ``chat.message.received.v1`` → ``telegram.process_message``: processes all unprocessed inbound
  messages in Telegram order (trade parser, commands, dialogs).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from papi.core import db
from papi.core.events import CHAT_MESSAGE_RECEIVED, CloudEvent
from papi.core.jobs import PRIORITY_CHAT
from papi.core.registry import handles, periodic
from papi.telegram.bot import process_inbox
from papi.telegram.outbox import is_ready
from papi.telegram.poller import poll_once, ready_to_poll

POLL_JOB_KIND = "periodic:telegram:poll"


def _poll_interval(now: datetime) -> float:
    return 1.0 if ready_to_poll() else 300.0


@periodic("telegram:poll", _poll_interval, priority=PRIORITY_CHAT)
def poll(ctx: Any) -> None:
    if ready_to_poll():
        poll_once()


@handles(CHAT_MESSAGE_RECEIVED, priority=PRIORITY_CHAT, name="telegram.process_message")
def process_message(event: CloudEvent, ctx: Any) -> None:
    if not is_ready():
        return
    with db.transaction() as conn:
        process_inbox(conn)
