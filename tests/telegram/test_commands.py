"""Deterministic commands: /yardim /portfoy /izle /birak /mutabakat /bugun /bekleyen and fallbacks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, select

from papi.core import db as core_db
from papi.core import schema as s
from papi.core.ids import uuid7
from papi.core.timeutil import now_utc
from papi.portfolio.service import default_portfolio_id
from tests.telegram.helpers import FakeTelegram, add_close, add_disclosure, add_event, add_instrument, buy
from tests.telegram.test_trade_flow import Chat


@pytest.fixture
def chat(db: Connection, fake_tg: FakeTelegram) -> Chat:
    return Chat(fake_tg)


@pytest.fixture
def book(db: Connection) -> dict[str, str]:
    ids = {
        "THYAO": add_instrument(db, "THYAO", "Türk Hava Yolları"),
        "ASELS": add_instrument(db, "ASELS", "Aselsan Elektronik"),
        "EREGL": add_instrument(db, "EREGL", "Ereğli Demir Çelik"),
    }
    add_close(db, ids["THYAO"], date(2026, 10, 1), 300.0)
    add_close(db, ids["ASELS"], date(2026, 10, 1), 50.0)
    buy(db, ids["THYAO"], 100, "250")
    buy(db, ids["ASELS"], 300, "40")
    db.commit()
    return ids


def test_help_and_unknown(chat: Chat) -> None:
    assert "/mutabakat" in chat.say("/yardim")
    assert "Bilinmeyen komut: /durdur" in chat.say("/durdur")
    assert "işlem olarak anlayamadım" in chat.say("merhaba")
    assert "henüz açık değil" in chat.say("ASELS alsam ne olur?")


def test_portfolio_table(chat: Chat, book: dict[str, str]) -> None:
    text = chat.say("/portfoy")
    assert "<pre>" in text
    assert "THYAO" in text and "ASELS" in text
    assert "%66,7" in text and "%33,3" in text  # 30.000 vs 15.000 market value
    assert "Piyasa değeri: 45.000,00 ₺" in text


def test_watch_and_unwatch(chat: Chat, book: dict[str, str]) -> None:
    assert "izleme listesine eklendi" in chat.say("/izle EREGL")
    with core_db.connect() as conn:
        wl = conn.execute(select(s.watchlist)).mappings().one()
    assert wl["instrument_ids"] == [book["EREGL"]]
    assert "çıkarıldı" in chat.say("/birak EREGL")
    assert "değildi" in chat.say("/birak EREGL")
    assert "Bilinmeyen hisse kodu" in chat.say("/izle ZZZZZ")
    assert "Kullanım" in chat.say("/izle")


def test_today_lists_holding_events(chat: Chat, book: dict[str, str], db: Connection) -> None:
    assert "bugün yeni bildirim yok" in chat.say("/bugun")
    d1 = add_disclosure(db, "1500001")
    add_event(db, d1, [book["THYAO"]], headline="Uçak kiralama sözleşmesi", priority="P1")
    d2 = add_disclosure(db, "1500002")
    add_event(db, d2, [book["EREGL"]], headline="Başka şirket")  # not held/watched
    d3 = add_disclosure(db, "1499999", published_at=now_utc() - timedelta(days=2))
    add_event(db, d3, [book["THYAO"]], headline="Eski olay", announced_at=now_utc() - timedelta(days=2))
    db.commit()
    text = chat.say("/bugun")
    assert "Uçak kiralama sözleşmesi" in text and "HOLDING" in text
    assert "https://www.kap.org.tr/tr/Bildirim/1500001" in text
    assert "Başka şirket" not in text and "Eski olay" not in text


def test_pending_actions_apply_and_dismiss(chat: Chat, book: dict[str, str], db: Connection, fake_tg) -> None:
    assert "Bekleyen kurumsal işlem yok" in chat.say("/bekleyen")
    pid = default_portfolio_id(db)
    pas = []
    for kind, qty in (("bonus", "300"), ("rights", "30")):
        ca = uuid7()
        db.execute(
            s.corporate_action.insert().values(
                id=ca, instrument_id=book["ASELS"], type=kind, ex_date=date(2026, 10, 20), status="executed",
                created_at=now_utc(),
            )
        )  # fmt: skip
        pa = uuid7()
        txn_type = "bonus_shares" if kind == "bonus" else "rights_subscription"
        db.execute(
            s.pending_action.insert().values(
                id=pa, portfolio_id=pid, instrument_id=book["ASELS"], corporate_action_id=ca, kind=kind,
                status="expected", created_at=now_utc(),
                proposal={"txn_type": txn_type, "quantity": qty, "price": "1" if kind == "rights" else None,
                          "text_tr": f"{kind}: 300 → +{qty}"},
            )
        )  # fmt: skip
        pas.append(pa)
    db.commit()
    chat.say("/bekleyen")
    texts = fake_tg.texts()[-2:]
    assert "bonus: 300 → +300" in texts[0] and "Hak kullanım: 20.10.2026" in texts[0]
    first_msg = chat.last_message_id() - 1
    assert chat.press(f"pa:ok:{pas[0]}", first_msg) == "Uygulandı"
    assert "ASELS: 600 lot" in fake_tg.last_text()
    assert chat.press(f"pa:no:{pas[1]}", chat.last_message_id()) == "Yoksayıldı"
    with core_db.connect() as conn:
        rows = {r["id"]: r["status"] for r in conn.execute(select(s.pending_action)).mappings()}
    assert rows == {pas[0]: "applied", pas[1]: "dismissed"}
    assert "zaten sonuçlanmış" in chat.press(f"pa:ok:{pas[1]}", first_msg)


def test_reconcile_dialog(chat: Chat, book: dict[str, str], fake_tg: FakeTelegram) -> None:
    first = chat.say("/mutabakat")
    assert "1/2" in first and "ASELS" in first and "papi 300 lot" in first
    assert "Anlayamadım" in chat.say("bilmiyorum")
    assert "2/2" in chat.say("250") and "THYAO" in fake_tg.last_text()
    assert "papi'de olmayan" in chat.say("ok")
    assert "Eklendi" in chat.say("EREGL 10, ZZZZ 5")
    summary = chat.say("yok")
    assert "ASELS</b>: papi 300 → İşCep 250 (fark -50)" in summary
    assert "EREGL</b>: papi 0 → İşCep 10 (fark +10)" in summary
    assert "ZZZZ" in summary and "uygulanamaz" in summary
    assert "THYAO" not in summary  # quantities matched → no diff
    assert chat.press("rc:ok", chat.last_message_id()) == "Uygulandı"
    assert "2 düzeltme kaydedildi" in fake_tg.last_text()
    with core_db.connect() as conn:
        adj = conn.execute(select(s.txn).where(s.txn.c.type == "reconcile_adjustment")).mappings().all()
    assert sorted(Decimal(r["quantity"]) for r in adj) == [Decimal(-50), Decimal(10)]
    assert chat.press("rc:ok", chat.last_message_id()) == "Bu mutabakat artık geçerli değil."


def test_reconcile_all_match_and_cancel(chat: Chat, book: dict[str, str]) -> None:
    chat.say("/mutabakat")
    chat.say("ok")
    chat.say("tamam")
    assert "Mutabakat tamam" in chat.say("yok")
    chat.say("/mutabakat")
    assert chat.say("iptal") == "Tamam, iptal edildi."
    assert "Yarım kalan bir işlem yok" in chat.say("/iptal")


def test_command_clears_open_dialog(chat: Chat, book: dict[str, str]) -> None:
    chat.say("THYAO 100 lot aldım")  # asks for the price
    chat.say("/portfoy")
    assert "işlem olarak anlayamadım" in chat.say("merhaba")


def test_non_text_message(chat: Chat, fake_tg: FakeTelegram) -> None:
    from papi.telegram.bot import process_inbox
    from papi.telegram.poller import poll_once

    upd = {
        "update_id": 900,
        "message": {
            "message_id": 900,
            "from": {"id": 777, "is_bot": False, "first_name": "Y"},
            "chat": {"id": 777, "type": "private"},
            "date": int(datetime.now(UTC).timestamp()),
            "sticker": {"file_id": "x"},
        },
    }
    fake_tg.updates.append(upd)
    poll_once(timeout=0)
    with core_db.transaction() as conn:
        process_inbox(conn)
    assert "yalnız metin" in fake_tg.last_text()
