"""End-to-end trade recording over (fake) Telegram: card → ✅/✏️/❌ → record → ↩︎ undo."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, select, update

from papi.core import db as core_db
from papi.core.schema import chat_message, txn
from papi.core.timeutil import IST
from papi.telegram.bot import process_inbox
from papi.telegram.poller import poll_once
from tests.telegram.helpers import (
    FakeTelegram,
    add_close,
    add_instrument,
    buy,
    callback_update,
    message_update,
)


class Chat:
    """Drives the bot like a user: send texts, press buttons."""

    def __init__(self, fake: FakeTelegram) -> None:
        self.fake = fake
        self.update_id = 100
        self.message_id = 10

    def say(self, text: str, *, at: datetime | None = None) -> str:
        self.update_id += 1
        self.message_id += 1
        self.fake.updates.append(message_update(self.update_id, text, message_id=self.message_id, date_=at))
        poll_once(timeout=0)
        with core_db.transaction() as conn:
            process_inbox(conn)
        return self.fake.last_text()

    def press(self, data: str, message_id: int) -> str:
        self.update_id += 1
        self.fake.updates.append(callback_update(self.update_id, data, message_id))
        poll_once(timeout=0)
        return self.fake.bodies("answerCallbackQuery")[-1]["text"]

    def last_message_id(self) -> int:
        return self.fake.next_message_id

    def button(self, label_prefix: str) -> str:
        for row in self.fake.last_keyboard():
            for b in row:
                if b["text"].startswith(label_prefix):
                    return b["callback_data"]
        raise AssertionError(f"no button {label_prefix!r} in {self.fake.last_keyboard()}")


@pytest.fixture
def chat(db: Connection, fake_tg: FakeTelegram) -> Chat:
    return Chat(fake_tg)


@pytest.fixture
def market(db: Connection) -> dict[str, str]:
    ids = {
        "THYAO": add_instrument(db, "THYAO", "Türk Hava Yolları"),
        "THTEK": add_instrument(db, "THTEK", "Türk Havacılık Teknik"),
        "ASELS": add_instrument(db, "ASELS", "Aselsan Elektronik"),
    }
    add_close(db, ids["THYAO"], date(2026, 10, 1), 280.0)
    add_close(db, ids["ASELS"], date(2026, 10, 1), 60.0)
    db.commit()
    return ids


def _txns() -> list[dict[str, Any]]:
    with core_db.connect() as conn:
        return [dict(r) for r in conn.execute(select(txn).order_by(txn.c.created_at)).mappings()]


def _inbound(text: str) -> dict[str, Any]:
    with core_db.connect() as conn:
        return dict(
            conn.execute(
                select(chat_message).where(chat_message.c.direction == "in", chat_message.c.text == text)
            )
            .mappings()
            .one()
        )


def test_confirm_then_undo(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    sent_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
    card = chat.say("THYAO 100 lot 285,40'tan aldım", at=sent_at)
    assert "ALIŞ" in card and "THYAO" in card and "100 lot @ 285,40 ₺ = 28.540,00 ₺" in card
    assert "Türk Hava Yolları" in card
    assert "alındı" not in card  # processed promptly → no delay note
    card_id = chat.last_message_id()
    assert [b["text"] for b in fake_tg.last_keyboard()[0]] == ["✅ Kaydet", "✏️ Düzelt", "❌ İptal"]
    row = _inbound("THYAO 100 lot 285,40'tan aldım")
    assert row["status"] == "proposed" and row["intent"] == "trade"
    assert row["proposed_transaction"]["ticker"] == "THYAO"
    assert Decimal(row["proposed_transaction"]["price"]) == Decimal("285.40")
    assert _txns() == []  # nothing is written without ✅

    assert chat.press(chat.button("✅"), card_id) == "Kaydedildi"
    txns = _txns()
    assert len(txns) == 1
    t = txns[0]
    assert (t["type"], t["quantity"], t["price"], t["source"]) == (
        "buy",
        Decimal(100),
        Decimal("285.40"),
        "telegram",
    )
    assert t["chat_message_id"] == row["id"]
    assert t["traded_at"] == sent_at
    assert t["trade_date"] == sent_at.astimezone(IST).date()
    assert "Kaydedildi. THYAO: 100 lot, ort. maliyet 285,40, portföy ağırlığı %100." in fake_tg.last_text()
    edits = fake_tg.bodies("editMessageText")
    assert edits[-1]["message_id"] == card_id and edits[-1]["text"].endswith("✅ Kaydedildi")
    assert _inbound("THYAO 100 lot 285,40'tan aldım")["status"] == "confirmed"

    # a second ✅ on the same card writes nothing
    assert chat.press(f"t:ok:{row['id']}", card_id) == "Bu kart artık geçerli değil."
    assert len(_txns()) == 1

    undo_data = chat.button("↩︎")
    assert undo_data == f"t:un:{t['id']}"
    assert chat.press(undo_data, chat.last_message_id()) == "Geri alındı"
    txns = _txns()
    assert len(txns) == 2 and txns[1]["reverses_id"] == t["id"] and txns[1]["quantity"] == Decimal(-100)
    assert "Geri alındı. THYAO: pozisyon kapandı." in fake_tg.last_text()
    assert chat.press(undo_data, chat.last_message_id()) == "Geri alınamadı"  # already reversed


def test_cancel_writes_nothing(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    chat.say("/sat ASELS 50 62,5")
    card_id = chat.last_message_id()
    assert "SATIŞ" in fake_tg.last_text()
    assert "eldeki adetten" in fake_tg.last_text()  # selling more than held → warning on the card
    assert chat.press(chat.button("❌"), card_id) == "İptal edildi"
    assert _txns() == []
    assert _inbound("/sat ASELS 50 62,5")["status"] == "cancelled"
    assert fake_tg.bodies("editMessageText")[-1]["text"].endswith("❌ İptal edildi")


def test_edit_price_then_confirm(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    chat.say("THYAO 100 lot 285,40'tan aldım")
    old_card = chat.last_message_id()
    msg_id = _inbound("THYAO 100 lot 285,40'tan aldım")["id"]
    assert chat.press(f"t:ed:{msg_id}", old_card) == "Hangi alan?"
    menu = fake_tg.bodies("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for row in menu for b in row] == [
        "Adet",
        "Fiyat",
        "Tarih",
        "Komisyon",
        "Hisse",
        "Yön",
        "↩︎ Geri",
    ]
    assert chat.press(f"t:ef:{msg_id}:p", old_card) == "Fiyat"
    assert "Yeni fiyatı yaz" in fake_tg.last_text()
    new = chat.say("290,10")
    assert "100 lot @ 290,10 ₺" in new
    new_card = chat.last_message_id()
    assert fake_tg.bodies("editMessageText")[-1]["text"].endswith("güncel kart aşağıda")

    chat.press(f"t:ef:{msg_id}:f", new_card)
    newer = chat.say("12,5")
    assert "Komisyon: 12,50 ₺" in newer
    newest_card = chat.last_message_id()

    assert chat.press(f"t:ok:{msg_id}", old_card) == "Bu kart artık geçerli değil."
    assert _txns() == []
    assert chat.press(f"t:ok:{msg_id}", newest_card) == "Kaydedildi"
    t = _txns()[0]
    assert (t["price"], t["fees"]) == (Decimal("290.10"), Decimal("12.5"))


def test_edit_quantity_date_ticker_and_side(
    chat: Chat, market: dict[str, str], fake_tg: FakeTelegram
) -> None:
    chat.say("THYAO 100 lot 285,40'tan aldım")
    msg_id = _inbound("THYAO 100 lot 285,40'tan aldım")["id"]
    card = chat.last_message_id()
    chat.press(f"t:ef:{msg_id}:q", card)
    assert "Anlayamadım" in chat.say("çok")  # invalid value → asks again, keeps the dialog
    assert "200 lot" in chat.say("200")
    card = chat.last_message_id()
    chat.press(f"t:ef:{msg_id}:d", card)
    assert "Tarih: 27.10.2026" in chat.say("27.10.2026")
    card = chat.last_message_id()
    chat.press(f"t:ef:{msg_id}:i", card)
    assert "ASELS" in chat.say("ASELS")
    card = chat.last_message_id()
    assert chat.press(f"t:ef:{msg_id}:s", card) == "Yön değiştirildi"
    assert "SATIŞ" in fake_tg.bodies("editMessageText")[-1]["text"]
    chat.press(f"t:ok:{msg_id}", card)
    t = _txns()[0]
    assert (t["type"], t["quantity"], t["trade_date"], t["instrument_id"]) == (
        "sell",
        Decimal(200),
        date(2026, 10, 27),
        market["ASELS"],
    )


def test_ambiguous_ticker_offers_choices(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    reply = chat.say("Türk Hava 10 lot 280'den aldım")
    assert "birden fazla" in reply
    kb = fake_tg.last_keyboard()
    labels = [row[0]["text"] for row in kb]
    assert any(label.startswith("THYAO") for label in labels) and any(
        label.startswith("THTEK") for label in labels
    )
    assert _inbound("Türk Hava 10 lot 280'den aldım")["status"] == "needs_choice"
    pick = next(row[0]["callback_data"] for row in kb if row[0]["text"].startswith("THTEK"))
    assert chat.press(pick, chat.last_message_id()) == "THTEK"
    assert "THTEK" in fake_tg.last_text() and "ALIŞ" in fake_tg.last_text()
    msg_id = _inbound("Türk Hava 10 lot 280'den aldım")["id"]
    chat.press(f"t:ok:{msg_id}", chat.last_message_id())
    assert _txns()[0]["instrument_id"] == market["THTEK"]


def test_unknown_ticker_asks(chat: Chat, market: dict[str, str]) -> None:
    reply = chat.say("ZZZZZ 10 lot 5'ten aldım")
    assert "bulamadım" in reply or "Eksik: hisse" in reply
    card = chat.say("THYAO")
    assert "ALIŞ" in card and "THYAO" in card and "10 lot @ 5,00" in card


def test_needs_info_dialog(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    ask = chat.say("THYAO 100 lot aldım")
    assert "Eksik: fiyat" in ask
    assert "ALIŞ · THYAO · 100 lot" in ask
    card = chat.say("285,40")
    assert "100 lot @ 285,40" in card
    ask = chat.say("THYAO 100 aldım")
    assert "Adet ve fiyatı birlikte" in ask
    card = chat.say("100 lot 285,40")
    assert "100 lot @ 285,40" in card
    chat.say("ASELS 10 lot aldım")
    assert chat.say("iptal") == "Tamam, iptal edildi."


def test_new_trade_overrides_pending_question(chat: Chat, market: dict[str, str]) -> None:
    chat.say("THYAO 100 lot aldım")
    card = chat.say("ASELS 10 lot 61'den aldım")
    assert "ASELS" in card and "10 lot @ 61,00" in card


def test_fraction_uses_current_holding(chat: Chat, market: dict[str, str], db: Connection) -> None:
    buy(db, market["ASELS"], 301, "50")
    db.commit()
    card = chat.say("ASELS'in yarısını 62'den sattım")
    assert "150 lot (eldeki 301 lotun %50'i)" in card
    assert "SATIŞ" in card
    reply = chat.say("THYAO'nun hepsini 300'den sattım")
    assert "pozisyon yok" in reply


def test_offline_message_note_and_message_time(chat: Chat, market: dict[str, str]) -> None:
    sent_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=33)
    card = chat.say("THYAO 100 lot 285,40'tan aldım", at=sent_at)
    assert "alındı" in card and "işlendi" in card
    msg_id = _inbound("THYAO 100 lot 285,40'tan aldım")["id"]
    chat.press(f"t:ok:{msg_id}", chat.last_message_id())
    assert _txns()[0]["traded_at"] == sent_at  # message time, not processing time


def test_offline_messages_processed_in_order(
    chat: Chat, market: dict[str, str], fake_tg: FakeTelegram
) -> None:
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    fake_tg.updates = [
        message_update(1, "THYAO 100 lot aldım", message_id=1, date_=base),
        message_update(2, "285,40", message_id=2, date_=base + timedelta(minutes=1)),
    ]
    poll_once(timeout=0)
    with core_db.transaction() as conn:
        assert process_inbox(conn) == 2
    texts = fake_tg.texts()
    assert "Eksik: fiyat" in texts[0] and "100 lot @ 285,40" in texts[1]
    assert all("alındı" in t for t in texts)
    assert fake_tg.methods().count("sendChatAction") == 1
    with core_db.transaction() as conn:
        assert process_inbox(conn) == 0  # all marked processed


def test_geri_command_and_undo_window(chat: Chat, market: dict[str, str], fake_tg: FakeTelegram) -> None:
    assert chat.say("/geri") == "Geri alınacak Telegram işlemi yok."
    chat.say("THYAO 100 lot 285,40'tan aldım")
    msg_id = _inbound("THYAO 100 lot 285,40'tan aldım")["id"]
    chat.press(f"t:ok:{msg_id}", chat.last_message_id())
    t = _txns()[0]
    with core_db.transaction() as conn:
        conn.execute(
            update(txn).where(txn.c.id == t["id"]).values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
    assert "süresi (24 saat) doldu" in chat.say("/geri")
    with core_db.transaction() as conn:
        conn.execute(update(txn).where(txn.c.id == t["id"]).values(created_at=datetime.now(UTC)))
    assert chat.say("/geri").startswith("↩︎ Geri alındı")
    assert len(_txns()) == 2
    assert chat.say("/geri") == "Geri alınacak Telegram işlemi yok."


def test_record_failure_keeps_card(
    chat: Chat, market: dict[str, str], fake_tg: FakeTelegram, monkeypatch
) -> None:
    import papi.telegram.trade_flow as tf

    def fail(*a: Any, **k: Any) -> str:
        raise ValueError("Miktar sıfırdan büyük olmalı.")

    monkeypatch.setattr(tf, "record_transaction", fail)
    chat.say("THYAO 100 lot 285,40'tan aldım")
    msg_id = _inbound("THYAO 100 lot 285,40'tan aldım")["id"]
    assert chat.press(f"t:ok:{msg_id}", chat.last_message_id()) == "Kaydedilemedi"
    assert "Kaydedilemedi" in fake_tg.last_text()
    assert _inbound("THYAO 100 lot 285,40'tan aldım")["status"] == "proposed"
