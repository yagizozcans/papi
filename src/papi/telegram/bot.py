"""Inbound processing: messages (in Telegram order) and button presses."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, update

from papi.core.config import get_settings
from papi.core.schema import chat_message
from papi.core.text import fold_diacritics, tr_lower
from papi.core.timeutil import now_utc
from papi.telegram import commands, trade_flow
from papi.telegram.api import TelegramClient, TelegramError
from papi.telegram.conversation import Turn, clear_pending, delay_note, get_pending
from papi.telegram.outbox import require_client
from papi.telegram.trade_parser import NeedsInfo, NotATrade, ParsedTrade, parse

log = logging.getLogger("papi.telegram")


def _command(text: str) -> tuple[str, str] | None:
    if not text.startswith("/"):
        return None
    head, _, arg = text.partition(" ")
    name = fold_diacritics(tr_lower(head[1:].split("@", 1)[0]))
    return name, arg.strip()


def _set(conn: Connection, msg_id: str, **values: Any) -> None:
    conn.execute(update(chat_message).where(chat_message.c.id == msg_id).values(**values))


def process_message(
    conn: Connection, row: Any, client: TelegramClient, *, now: datetime | None = None
) -> None:
    """Handle one inbound message row (commands, trades, dialog replies, help)."""
    now = now or now_utc()
    turn = Turn(
        conn, client, now, msg=row, chat=row["telegram_chat_id"], note=delay_note(row["sent_at"], now)
    )
    text = (row["text"] or "").strip()
    try:
        if not text:
            turn.reply("Şimdilik yalnız metin mesajlarını anlayabiliyorum. /yardim")
            return
        cmd = _command(text)
        if cmd is not None:
            name, arg = cmd
            if name in {"al", "sat", "buy", "sell"}:
                clear_pending(conn)
                _set(conn, row["id"], intent="trade")
                _handle_trade_text(turn, text)
                return
            _set(conn, row["id"], intent="command", status="answered")
            if name != "iptal":
                clear_pending(conn)
            handler = commands.COMMANDS.get(name)
            if handler is None:
                turn.reply(f"Bilinmeyen komut: /{name}\n\n{commands.HELP_TEXT}")
            else:
                handler(turn, arg)
            return

        pending = get_pending(conn)
        result = parse(text, now=row["sent_at"] or now)
        if pending is not None:
            if trade_flow.is_cancel_word(text):
                clear_pending(conn)
                if pending.get("msg"):
                    _set(conn, pending["msg"], status="cancelled")
                turn.reply("Tamam, iptal edildi.")
                return
            if isinstance(result, ParsedTrade):
                clear_pending(conn)  # a complete new trade wins over the open question
            else:
                _dispatch_pending(turn, pending, text)
                return
        _handle_parsed(turn, result)
    finally:
        _set(conn, row["id"], processed_at=now)


def _dispatch_pending(turn: Turn, pending: dict[str, Any], text: str) -> None:
    kind = pending.get("kind")
    if kind == "needs_info":
        trade_flow.fill_missing(turn, pending, text)
    elif kind == "edit":
        trade_flow.apply_edit(turn, pending, text)
    elif kind in {"reconcile", "reconcile_extra", "reconcile_confirm"}:
        commands.reconcile_reply(turn, pending, text)
    else:
        clear_pending(turn.conn)
        _handle_parsed(turn, parse(text, now=turn.now))


def _handle_trade_text(turn: Turn, text: str) -> None:
    _handle_parsed(turn, parse(text, now=turn.msg["sent_at"] or turn.now))


def _handle_parsed(turn: Turn, result: ParsedTrade | NeedsInfo | NotATrade) -> None:
    if isinstance(result, ParsedTrade):
        _set(turn.conn, turn.msg_id, intent="trade")  # type: ignore[arg-type]
        trade_flow.start_trade(turn, result)
    elif isinstance(result, NeedsInfo):
        trade_flow.ask_missing(turn, result.draft, result.missing)
    else:
        if result.reason in {"what_if", "question"}:
            _set(turn.conn, turn.msg_id, intent="question", status="answered")  # type: ignore[arg-type]
            turn.reply(commands.QUESTION_REPLY)
        else:
            _set(turn.conn, turn.msg_id, status="answered")  # type: ignore[arg-type]
            turn.reply(commands.SHORT_HELP)


def pending_inbound(conn: Connection) -> list[Any]:
    chat = get_settings().telegram.chat_id
    q = select(chat_message).where(chat_message.c.direction == "in", chat_message.c.processed_at.is_(None))
    if chat is not None:
        q = q.where(chat_message.c.telegram_chat_id == chat)
    return list(
        conn.execute(q.order_by(chat_message.c.sent_at, chat_message.c.telegram_message_id)).mappings()
    )


def process_inbox(
    conn: Connection, *, client: TelegramClient | None = None, now: datetime | None = None
) -> int:
    """Process every unprocessed inbound message in Telegram order. Returns how many were handled."""
    rows = pending_inbound(conn)
    if not rows:
        return 0
    client = client or require_client()
    try:
        client.send_chat_action(rows[0]["telegram_chat_id"])
    except TelegramError as exc:
        log.debug("sendChatAction failed: %s", exc)
    for row in rows:
        process_message(conn, row, client, now=now)
    return len(rows)


def handle_callback(
    conn: Connection, cq: dict[str, Any], client: TelegramClient, *, now: datetime | None = None
) -> str:
    """Execute a button press and answer the callback query. Returns the answer text."""
    now = now or now_utc()
    message = cq.get("message") or {}
    chat = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    turn = Turn(conn, client, now, chat=chat)
    data = str(cq.get("data") or "")
    parts = data.split(":")
    answer = "?"
    try:
        if parts[0] == "t" and len(parts) >= 3:
            action, ref = parts[1], parts[2]
            if action in {"ok", "ed", "eb", "ef"}:
                card = trade_flow.latest_card(conn, ref)
                if card is None or card["telegram_message_id"] != message_id:
                    client.edit_message_reply_markup(chat, message_id, None)
                    answer = "Bu kart artık geçerli değil."
                    return answer
            if action == "ok":
                answer = trade_flow.confirm(turn, ref)
            elif action == "no":
                answer = trade_flow.cancel(turn, ref)
                if message_id is not None:
                    client.edit_message_reply_markup(chat, message_id, None)
            elif action == "ed":
                answer = trade_flow.show_edit_menu(turn, ref, message_id)
            elif action == "eb":
                client.edit_message_reply_markup(chat, message_id, trade_flow.card_keyboard(ref))
                answer = "Tamam"
            elif action == "ef" and len(parts) == 4:
                answer = trade_flow.edit_field(turn, ref, parts[3], message_id)
            elif action == "pk" and len(parts) == 4 and parts[3].isdigit():
                client.edit_message_reply_markup(chat, message_id, None)
                answer = trade_flow.pick_choice(turn, ref, int(parts[3]))
            elif action == "un":
                client.edit_message_reply_markup(chat, message_id, None)
                answer = trade_flow.undo(turn, ref)
        elif parts[0] == "rc" and len(parts) == 2:
            answer = commands.reconcile_callback(turn, parts[1], message_id)
        elif parts[0] == "pa" and len(parts) == 3:
            answer = commands.pending_action_callback(turn, parts[1], parts[2], message_id)
        else:
            log.warning("unknown callback data %r", data)
            answer = "Bilinmeyen düğme"
        return answer
    finally:
        try:
            client.answer_callback_query(cq["id"], answer[:190])
        except TelegramError as exc:  # expired queries (>15 min, e.g. after sleep) cannot be answered
            log.info("answerCallbackQuery failed: %s", exc)
