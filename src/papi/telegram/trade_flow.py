"""Trade recording over Telegram (08 §4.3): parse → validate → card [✅ Kaydet / ✏️ Düzelt / ❌ İptal] →
record (only on ✅) → [↩︎ Geri al] within ``telegram.undo_hours``.

The proposal is stored on the inbound ``chat_message`` row (``proposed_transaction`` = ``ProposedTxn``
JSON, plus ``choices``/``fraction`` while an instrument choice is pending). Cards are outbound rows
with ``intent = 'trade_card'`` and ``reply_to_id`` = the inbound row; only the newest card of a
proposal accepts button presses.

Callback data (≤ 64 bytes): ``t:ok|no|ed|eb:<msg>``, ``t:ef:<msg>:<q|p|d|f|i|s>``, ``t:pk:<msg>:<n>``,
``t:un:<txn>``.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, select, update

from papi.core.config import get_settings
from papi.core.schema import chat_message, txn
from papi.core.text import normalize_tr, parse_tr_number
from papi.core.timeutil import to_ist
from papi.portfolio.fmt import fmt_date, fmt_num, fmt_pct, fmt_qty
from papi.portfolio.service import (
    InstrumentMatch,
    ProposedTxn,
    holding,
    record_transaction,
    resolve_instrument,
    reverse_transaction,
    ticker_for,
    validate_proposal,
)
from papi.telegram.api import TelegramClient, button, esc, keyboard
from papi.telegram.conversation import Turn, clear_pending, get_pending, set_pending
from papi.telegram.trade_parser import (
    NeedsInfo,
    NotATrade,
    ParsedTrade,
    TradeDraft,
    complete,
    parse,
    parse_date_phrase,
    parse_side_word,
)

AUTO_PICK_SCORE = 0.9
SIDE_TR = {"buy": "ALIŞ", "sell": "SATIŞ"}
FIELD_TR = {
    "side": "işlem yönü (al/sat)",
    "instrument": "hisse",
    "quantity": "adet (lot)",
    "price": "fiyat",
    "date": "tarih",
}
EDIT_FIELDS = {"q": "Adet", "p": "Fiyat", "d": "Tarih", "f": "Komisyon", "i": "Hisse", "s": "Yön"}
EDIT_PROMPTS = {
    "q": "Yeni adedi yaz (ör. 100):",
    "p": "Yeni fiyatı yaz (ör. 285,40):",
    "d": "Yeni tarihi yaz (ör. dün, 27.10.2026):",
    "f": "Komisyon tutarını yaz (TL, ör. 12,50; yoksa 0):",
    "i": "Hisse kodunu yaz (ör. THYAO):",
}
CANCEL_WORDS = {"iptal", "vazgec", "vazgectim", "cancel", "bosver"}


# ---------------------------------------------------------------------------------------- storage


def _row(conn: Connection, msg_id: str) -> Any:
    return conn.execute(select(chat_message).where(chat_message.c.id == msg_id)).mappings().first()


def _store(
    conn: Connection,
    msg_id: str,
    p: ProposedTxn,
    *,
    status: str,
    choices: list[InstrumentMatch] | None = None,
    fraction: Decimal | None = None,
) -> None:
    data: dict[str, Any] = p.model_dump(mode="json")
    if choices is not None:
        data["choices"] = [
            {"instrument_id": c.instrument_id, "ticker": c.ticker, "issuer_name": c.issuer_name}
            for c in choices
        ]
    if fraction is not None:
        data["fraction"] = str(fraction)
    conn.execute(
        update(chat_message)
        .where(chat_message.c.id == msg_id)
        .values(proposed_transaction=data, status=status, intent="trade")
    )


def _load(row: Any) -> tuple[ProposedTxn, list[dict[str, Any]], Decimal | None]:
    data = dict(row["proposed_transaction"] or {})
    choices = data.pop("choices", None) or []
    fraction = data.pop("fraction", None)
    return ProposedTxn.model_validate(data), choices, Decimal(fraction) if fraction else None


def latest_card(conn: Connection, msg_id: str) -> Any:
    return (
        conn.execute(
            select(chat_message)
            .where(
                chat_message.c.reply_to_id == msg_id,
                chat_message.c.direction == "out",
                chat_message.c.intent == "trade_card",
            )
            .order_by(chat_message.c.created_at.desc(), chat_message.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


# ---------------------------------------------------------------------------------------- rendering


def card_text(p: ProposedTxn, *, issuer_name: str | None = None, fraction_note: str | None = None) -> str:
    icon = "🟢" if p.type == "buy" else "🔴"
    head = f"{icon} <b>{SIDE_TR.get(p.type, p.type.upper())}</b> · <b>{esc(p.ticker or '?')}</b>"
    if issuer_name:
        head += f" ({esc(issuer_name)})"
    qty = f"{fmt_qty(p.quantity)} lot"
    if fraction_note:
        qty += f" ({esc(fraction_note)})"
    line = qty
    if p.price is not None:
        line += f" @ {fmt_num(p.price, 2, trim=False)} ₺ = {fmt_num(p.quantity * p.price)} ₺"
    when = fmt_date(p.trade_date)
    if p.traded_at is not None:
        when += " " + to_ist(p.traded_at).strftime("%H:%M")
    lines = [head, line, f"Tarih: {when}"]
    lines.append(f"Komisyon: {fmt_num(p.fees)} ₺" if p.fees else "Komisyon: — (✏️ ile eklenebilir)")
    lines.extend(f"⚠️ {esc(w)}" for w in p.warnings)
    return "\n".join(lines)


def card_keyboard(msg_id: str) -> dict[str, Any]:
    return keyboard(
        [
            button("✅ Kaydet", f"t:ok:{msg_id}"),
            button("✏️ Düzelt", f"t:ed:{msg_id}"),
            button("❌ İptal", f"t:no:{msg_id}"),
        ]
    )


def edit_keyboard(msg_id: str) -> dict[str, Any]:
    b = [button(label, f"t:ef:{msg_id}:{code}") for code, label in EDIT_FIELDS.items()]
    return keyboard(b[:3], b[3:], [button("↩︎ Geri", f"t:eb:{msg_id}")])


def describe_draft(d: TradeDraft) -> str:
    parts = [SIDE_TR.get(d.side or "", "?"), d.instrument_ref or "?"]
    if d.quantity is not None:
        parts.append(f"{fmt_qty(d.quantity)} lot")
    elif d.fraction is not None:
        parts.append(f"%{fmt_num(d.fraction * 100, 0)} pozisyon")
    else:
        parts.append("? lot")
    parts.append(f"@ {fmt_num(d.price)}" if d.price is not None else "@ ?")
    if d.trade_date is not None:
        parts.append(fmt_date(d.trade_date))
    return " · ".join(esc(p) for p in parts)


def ask_missing(
    turn: Turn,
    draft: TradeDraft,
    missing: tuple[str, ...] | list[str],
    *,
    intro: str = "📝 İşlemi kaydetmek için bilgi eksik.",
) -> None:
    """Remember the draft and ask for the missing fields."""
    set_pending(
        turn.conn,
        {"kind": "needs_info", "msg": turn.msg_id, "draft": draft.as_json(), "missing": list(missing)},
    )
    if turn.msg_id:
        turn.conn.execute(
            update(chat_message)
            .where(chat_message.c.id == turn.msg_id)
            .values(intent="trade", status="needs_info")
        )
    if set(missing) >= {"quantity", "price"} and draft.quantity is None and draft.price is None:
        question = "Adet ve fiyatı birlikte yazar mısın? (ör. <code>100 lot 285,40</code>)"
    else:
        question = "Eksik: " + ", ".join(FIELD_TR.get(m, m) for m in missing) + "?"
        if "price" in missing:
            question += " (ör. fiyat <code>285,40</code>)"
        if "date" in missing:
            question += " (ör. <code>dün</code>, <code>27.10.2026</code>)"
    turn.reply(
        f"{intro}\nAnladığım: {describe_draft(draft)}\n{question}\n<i>'iptal' ile vazgeçebilirsin.</i>",
        quote=True,
    )


# ---------------------------------------------------------------------------------------- flow


def start_trade(turn: Turn, parsed: ParsedTrade) -> None:
    """Resolve the instrument (choice buttons if ambiguous), size fractions, validate, show the card."""
    clear_pending(turn.conn)
    d = parsed.draft
    matches = resolve_instrument(turn.conn, d.instrument_ref or "", at=d.trade_date)
    exact = [m for m in matches if m.score >= 1.0]
    pick: InstrumentMatch | None = None
    if exact:
        pick = exact[0]
    elif len(matches) == 1 and matches[0].score >= AUTO_PICK_SCORE:
        pick = matches[0]
    p = ProposedTxn(
        type=d.side,
        quantity=d.quantity if d.quantity is not None else Decimal(0),
        price=d.price,
        fees=d.fees or Decimal(0),
        trade_date=d.trade_date,
        traded_at=d.traded_at,
        source="telegram",
        chat_message_id=turn.msg_id,
        ticker=d.instrument_ref,
    )
    if pick is None:
        if not matches:
            intro = f"🔎 “{esc(d.instrument_ref)}” adlı bir hisse bulamadım."
            ask_missing(turn, replace(d, instrument_ref=None), ("instrument",), intro=intro)
            return
        assert turn.msg_id is not None
        _store(turn.conn, turn.msg_id, p, status="needs_choice", choices=matches, fraction=d.fraction)
        rows = [
            [button(f"{m.ticker} — {(m.issuer_name or '')[:30]}".rstrip(" —"), f"t:pk:{turn.msg_id}:{i}")]
            for i, m in enumerate(matches)
        ]
        rows.append([button("❌ İptal", f"t:no:{turn.msg_id}")])
        turn.reply(
            f"🔎 “{esc(d.instrument_ref)}” için birden fazla olasılık var. Hangisi?",
            reply_markup=keyboard(*rows),
            quote=True,
        )
        return
    _continue_with(turn, turn.msg_id, p, pick.instrument_id, pick.ticker, pick.issuer_name, d.fraction)


def _continue_with(
    turn: Turn,
    msg_id: str | None,
    p: ProposedTxn,
    instrument_id: str,
    ticker: str,
    issuer_name: str | None,
    fraction: Decimal | None,
) -> None:
    assert msg_id is not None
    p = p.model_copy(update={"instrument_id": instrument_id, "ticker": ticker})
    fraction_note = None
    if fraction is not None:
        pos = holding(turn.conn, instrument_id)
        if pos is None or pos.quantity <= 0:
            intro = f"{esc(ticker)} için kayıtlı pozisyon yok; adet belirtir misin?"
            ask_missing(turn, _draft_from(p, quantity=None), ("quantity",), intro=intro)
            return
        qty = (pos.quantity * fraction).to_integral_value(rounding=ROUND_DOWN)
        if qty <= 0:
            ask_missing(turn, _draft_from(p, quantity=None), ("quantity",))
            return
        p = p.model_copy(update={"quantity": qty})
        fraction_note = f"eldeki {fmt_qty(pos.quantity)} lotun %{fmt_num(fraction * 100, 0)}'i"
    p = validate_proposal(turn.conn, p)
    _store(turn.conn, msg_id, p, status="proposed")
    clear_pending(turn.conn)
    send_card(turn, msg_id, p, issuer_name=issuer_name, fraction_note=fraction_note)


def _draft_from(p: ProposedTxn, **override: Any) -> TradeDraft:
    values = {
        "side": p.type if p.type in ("buy", "sell") else None,
        "instrument_ref": p.ticker,
        "quantity": p.quantity if p.quantity else None,
        "price": p.price,
        "fees": p.fees or None,
        "trade_date": p.trade_date,
        "traded_at": p.traded_at,
    }
    values.update(override)
    return TradeDraft(**values)


def send_card(
    turn: Turn,
    msg_id: str,
    p: ProposedTxn,
    *,
    issuer_name: str | None = None,
    fraction_note: str | None = None,
) -> None:
    sent = turn.reply(
        card_text(p, issuer_name=issuer_name, fraction_note=fraction_note),
        reply_markup=card_keyboard(msg_id),
        intent="trade_card",
        quote=True,
    )
    # the card row points at the proposal's inbound message (callbacks may come from a different turn)
    turn.conn.execute(
        update(chat_message).where(chat_message.c.id == sent.chat_message_id).values(reply_to_id=msg_id)
    )


def _retire_card(client: TelegramClient, card: Any, suffix: str) -> None:
    if card is None or card["telegram_message_id"] is None:
        return
    client.edit_message_text(
        card["telegram_chat_id"], card["telegram_message_id"], f"{card['text']}\n\n{suffix}"
    )


def confirm(turn: Turn, msg_id: str) -> str:
    """✅ — record the transaction. Returns the callback answer text."""
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] != "proposed":
        return "Bu kart artık geçerli değil."
    p, _, _ = _load(row)
    try:
        txn_id = record_transaction(turn.conn, p, dedupe_key=f"telegram:{msg_id}")
    except ValueError as exc:
        turn.reply(f"❗ Kaydedilemedi: {esc(exc)}\n✏️ Düzelt ile değiştirebilirsin.")
        return "Kaydedilemedi"
    turn.conn.execute(update(chat_message).where(chat_message.c.id == msg_id).values(status="confirmed"))
    _retire_card(turn.client, latest_card(turn.conn, msg_id), "✅ Kaydedildi")
    turn.reply(
        f"✅ Kaydedildi. {position_line(turn.conn, p.instrument_id, p.ticker)}",
        reply_markup=keyboard([button("↩︎ Geri al", f"t:un:{txn_id}")]),
    )
    return "Kaydedildi"


def position_line(conn: Connection, instrument_id: str | None, ticker: str | None) -> str:
    pos = holding(conn, instrument_id) if instrument_id else None
    if pos is None:
        return f"{esc(ticker or '')}: pozisyon kapandı."
    parts = [f"{esc(pos.ticker)}: {fmt_qty(pos.quantity)} lot"]
    if pos.avg_cost is not None:
        parts.append(f"ort. maliyet {fmt_num(pos.avg_cost)}")
    if pos.weight is not None:
        parts.append(f"portföy ağırlığı {fmt_pct(pos.weight * 100, 0)}")
    return ", ".join(parts) + "."


def cancel(turn: Turn, msg_id: str) -> str:
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] not in ("proposed", "needs_choice"):
        return "Bu kart artık geçerli değil."
    turn.conn.execute(update(chat_message).where(chat_message.c.id == msg_id).values(status="cancelled"))
    pending = get_pending(turn.conn)
    if pending and pending.get("msg") == msg_id:
        clear_pending(turn.conn)
    card = latest_card(turn.conn, msg_id)
    if card is not None:
        _retire_card(turn.client, card, "❌ İptal edildi")
    return "İptal edildi"


def show_edit_menu(turn: Turn, msg_id: str, card_message_id: int) -> str:
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] != "proposed":
        return "Bu kart artık geçerli değil."
    turn.client.edit_message_reply_markup(turn.chat, card_message_id, edit_keyboard(msg_id))
    return "Hangi alan?"


def edit_field(turn: Turn, msg_id: str, field: str, card_message_id: int) -> str:
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] != "proposed":
        return "Bu kart artık geçerli değil."
    if field == "s":
        p, _, _ = _load(row)
        if p.type not in ("buy", "sell"):
            return "Yön değiştirilemez."
        p = validate_proposal(
            turn.conn, p.model_copy(update={"type": "sell" if p.type == "buy" else "buy", "warnings": []})
        )
        _store(turn.conn, msg_id, p, status="proposed")
        turn.client.edit_message_text(
            turn.chat, card_message_id, card_text(p), reply_markup=card_keyboard(msg_id)
        )
        turn.conn.execute(
            update(chat_message)
            .where(chat_message.c.telegram_message_id == card_message_id, chat_message.c.direction == "out")
            .values(text=card_text(p))
        )
        return "Yön değiştirildi"
    if field not in EDIT_PROMPTS:
        return "Bilinmeyen alan"
    set_pending(turn.conn, {"kind": "edit", "msg": msg_id, "field": field})
    turn.reply(
        EDIT_PROMPTS[field] + "\n<i>'iptal' ile vazgeçebilirsin.</i>", reply_to_message_id=card_message_id
    )
    return EDIT_FIELDS[field]


def apply_edit(turn: Turn, pending: dict[str, Any], text: str) -> None:
    """A reply to an ✏️ prompt: update the field, re-validate and send a fresh card."""
    msg_id, field = pending["msg"], pending["field"]
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] != "proposed":
        clear_pending(turn.conn)
        turn.reply("Bu işlem kartı artık geçerli değil.")
        return
    p, _, _ = _load(row)
    upd: dict[str, Any] = {"warnings": []}
    try:
        if field in ("q", "p", "f"):
            value = parse_tr_number(text)
            if field == "q":
                if value <= 0 or value != value.to_integral_value():
                    raise ValueError
                upd["quantity"] = value
            elif field == "p":
                if value <= 0:
                    raise ValueError
                upd["price"] = value
            else:
                if value < 0:
                    raise ValueError
                upd["fees"] = value
        elif field == "d":
            d = parse_date_phrase(
                text, now=turn.now if turn.msg is None else (turn.msg["sent_at"] or turn.now)
            )
            if d is None:
                raise ValueError
            upd["trade_date"], upd["traded_at"] = d, None
        elif field == "i":
            matches = resolve_instrument(turn.conn, text.strip(), at=p.trade_date)
            exact = [m for m in matches if m.score >= 1.0]
            pick = (
                exact[0]
                if exact
                else (matches[0] if len(matches) == 1 and matches[0].score >= AUTO_PICK_SCORE else None)
            )
            if pick is None:
                if not matches:
                    turn.reply(f"“{esc(text.strip())}” bulunamadı. Hisse kodunu tekrar yazar mısın?")
                    return
                _store(turn.conn, msg_id, p, status="needs_choice", choices=matches)
                clear_pending(turn.conn)
                _retire_card(turn.client, latest_card(turn.conn, msg_id), "✏️ Düzeltiliyor…")
                rows = [
                    [button(f"{m.ticker} — {(m.issuer_name or '')[:30]}".rstrip(" —"), f"t:pk:{msg_id}:{i}")]
                    for i, m in enumerate(matches)
                ]
                turn.reply(
                    "🔎 Hangisi?", reply_markup=keyboard(*rows, [button("❌ İptal", f"t:no:{msg_id}")])
                )
                return
            upd["instrument_id"], upd["ticker"] = pick.instrument_id, pick.ticker
    except (ValueError, InvalidOperation):
        turn.reply(f"Anlayamadım. {EDIT_PROMPTS[field]}")
        return
    p = validate_proposal(turn.conn, p.model_copy(update=upd))
    _store(turn.conn, msg_id, p, status="proposed")
    clear_pending(turn.conn)
    _retire_card(turn.client, latest_card(turn.conn, msg_id), "✏️ Düzeltildi — güncel kart aşağıda")
    send_card(turn, msg_id, p)


def pick_choice(turn: Turn, msg_id: str, index: int) -> str:
    row = _row(turn.conn, msg_id)
    if row is None or row["status"] != "needs_choice":
        return "Bu seçim artık geçerli değil."
    p, choices, fraction = _load(row)
    if not 0 <= index < len(choices):
        return "Geçersiz seçim."
    c = choices[index]
    _continue_with(turn, msg_id, p, c["instrument_id"], c["ticker"], c.get("issuer_name"), fraction)
    return c["ticker"]


def fill_missing(turn: Turn, pending: dict[str, Any], text: str) -> None:
    """A reply to a "bilgi eksik" question: merge what it says into the draft."""
    draft = TradeDraft.from_json(pending["draft"])
    missing = list(pending["missing"])
    msg_time = turn.msg["sent_at"] if turn.msg is not None and turn.msg["sent_at"] else turn.now
    upd: dict[str, Any] = {}
    stripped = text.strip()
    number = None
    with contextlib.suppress(ValueError, InvalidOperation):
        number = parse_tr_number(stripped)
    if number is not None:
        targets = [m for m in missing if m in ("quantity", "price")]
        if len(targets) == 1:
            upd[targets[0]] = number
    elif "side" in missing and (side := parse_side_word(stripped)) and len(stripped.split()) <= 2:
        upd["side"] = side
    elif "date" in missing and (d := parse_date_phrase(stripped, now=msg_time)) is not None:
        upd["trade_date"], upd["traded_at"], upd["date_explicit"] = d, None, True
    elif "instrument" in missing and len(stripped.split()) <= 4 and not any(ch.isdigit() for ch in stripped):
        upd["instrument_ref"] = stripped
    if not upd:
        result = parse(stripped, now=msg_time)
        fields = ("side", "instrument_ref", "quantity", "fraction", "price", "fees")
        if isinstance(result, NotATrade) and result.reason == "no_trade_signal":
            # a fragment like "100 lot 285,40": read it in command order (quantity, price); its side
            # is the placeholder we added, not information from the user
            result = parse(f"/al {stripped}", now=msg_time)
            fields = fields[1:]
        r_draft = result.draft if isinstance(result, NeedsInfo | ParsedTrade) else None
        if r_draft is not None:
            for name in fields:
                value = getattr(r_draft, name)
                if value is not None:
                    upd[name] = value
            if r_draft.date_explicit:
                upd["trade_date"], upd["traded_at"], upd["date_explicit"] = (
                    r_draft.trade_date,
                    r_draft.traded_at,
                    True,
                )
            if "quantity" in upd:
                upd["fraction"] = None
            elif "fraction" in upd:
                upd["quantity"] = None
    if not upd:
        turn.reply(
            "Anlayamadım. "
            + ", ".join(FIELD_TR.get(m, m) for m in missing)
            + " yazar mısın? ('iptal' ile vazgeç)"
        )
        return
    if "quantity" in upd and (upd["quantity"] <= 0 or upd["quantity"] != upd["quantity"].to_integral_value()):
        turn.reply("Adet pozitif bir tam sayı olmalı (1 lot = 1 pay).")
        return
    if "price" in upd and upd["price"] <= 0:
        turn.reply("Fiyat sıfırdan büyük olmalı.")
        return
    draft = replace(draft, **upd)
    result = complete(draft)
    if isinstance(result, ParsedTrade):
        start_trade(turn, result)
    else:
        ask_missing(turn, result.draft, result.missing)


# ---------------------------------------------------------------------------------------- undo


def undo(turn: Turn, txn_id: str) -> str:
    """↩︎ / ``/geri`` — append the reversing entry within ``telegram.undo_hours``."""
    row = turn.conn.execute(select(txn).where(txn.c.id == txn_id)).mappings().first()
    if row is None:
        return "İşlem bulunamadı."
    hours = get_settings().telegram.undo_hours
    if turn.now - row["created_at"] > timedelta(hours=hours):
        msg = (
            f"Geri alma süresi ({hours} saat) doldu. "
            "Düzeltme için panel ya da <code>papi portfolio</code> kullan."
        )
        turn.reply(msg)
        return "Süre doldu"
    try:
        reverse_transaction(turn.conn, txn_id, reason="Telegram geri al")
    except ValueError as exc:
        turn.reply(f"❗ {esc(exc)}")
        return "Geri alınamadı"
    if row["chat_message_id"]:
        turn.conn.execute(
            update(chat_message).where(chat_message.c.id == row["chat_message_id"]).values(status="undone")
        )
    ticker = ticker_for(turn.conn, row["instrument_id"]) if row["instrument_id"] else None
    turn.reply(f"↩︎ Geri alındı. {position_line(turn.conn, row['instrument_id'], ticker)}")
    return "Geri alındı"


def last_undoable(conn: Connection) -> str | None:
    """Newest Telegram transaction that is neither a reversal nor reversed."""
    reversed_ids = select(txn.c.reverses_id).where(txn.c.reverses_id.is_not(None))
    return conn.execute(
        select(txn.c.id)
        .where(txn.c.source == "telegram", txn.c.reverses_id.is_(None), txn.c.id.not_in(reversed_ids))
        .order_by(txn.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def is_cancel_word(text: str) -> bool:
    return normalize_tr(text) in CANCEL_WORDS


__all__ = [
    "apply_edit",
    "ask_missing",
    "cancel",
    "card_text",
    "confirm",
    "edit_field",
    "fill_missing",
    "is_cancel_word",
    "last_undoable",
    "pick_choice",
    "show_edit_menu",
    "start_trade",
    "undo",
]
