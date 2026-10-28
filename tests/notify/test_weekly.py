"""Weekly review: schedule, content, once per slot, stale slots skipped."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
import time_machine
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import schema as s
from papi.core.ids import uuid7
from papi.core.timeutil import IST, now_utc
from papi.notify import alerts, weekly
from papi.portfolio.service import default_portfolio_id
from tests.telegram.helpers import FakeTelegram, add_disclosure, add_event, add_instrument, buy

SUNDAY_1810 = datetime(2026, 11, 1, 18, 10, tzinfo=IST)  # 1 Nov 2026 is a Sunday


def tx(fn, *args, **kw):
    with core_db.transaction() as conn:
        return fn(conn, *args, **kw)


@pytest.mark.parametrize(
    ("now", "spec", "slot"),
    [
        (SUNDAY_1810, "sun 18:00", datetime(2026, 11, 1, 18, 0, tzinfo=IST)),
        (datetime(2026, 11, 1, 17, 0, tzinfo=IST), "sun 18:00", datetime(2026, 10, 25, 18, 0, tzinfo=IST)),
        (datetime(2026, 11, 4, 9, 0, tzinfo=IST), "sun 18:00", datetime(2026, 11, 1, 18, 0, tzinfo=IST)),
        (datetime(2026, 11, 4, 9, 0, tzinfo=IST), "paz 08:30", datetime(2026, 11, 1, 8, 30, tzinfo=IST)),
        (datetime(2026, 11, 4, 9, 0, tzinfo=IST), "wed 09:00", datetime(2026, 11, 4, 9, 0, tzinfo=IST)),
    ],
)
def test_last_slot(now: datetime, spec: str, slot: datetime) -> None:
    assert weekly.last_slot(now, spec) == slot


def test_bad_schedule() -> None:
    with pytest.raises(ValueError, match="weekly_review"):
        weekly.parse_schedule("someday")


def test_weekly_review_content_and_once_per_slot(db: Connection, fake_tg: FakeTelegram) -> None:
    thyao = add_instrument(db, "THYAO", "Türk Hava Yolları")
    asels = add_instrument(db, "ASELS", "Aselsan Elektronik")
    eregl = add_instrument(db, "EREGL", "Ereğli")
    buy(db, thyao, 100, "250")
    buy(db, asels, 1200, "40")
    with time_machine.travel(SUNDAY_1810 - timedelta(days=2), tick=False):
        d = add_disclosure(db, "1600001")
        add_event(db, d, [thyao], headline="Filo genişletme")
        d2 = add_disclosure(db, "1600002")
        add_event(db, d2, [eregl], headline="Başkasının haberi")
        db.execute(
            s.notification.insert().values(
                id=uuid7(), kind="ops", channel="telegram", text="⚠️ <b>papi ops</b> · warning\nKAP gecikmesi",
                delivered=True, created_at=now_utc(),
            )
        )  # fmt: skip
    with time_machine.travel(SUNDAY_1810 - timedelta(days=10), tick=False):
        d3 = add_disclosure(db, "1500000")
        add_event(db, d3, [thyao], headline="Eski haber")
    ca = uuid7()
    db.execute(
        s.corporate_action.insert().values(
            id=ca, instrument_id=asels, type="bonus", status="executed", created_at=now_utc()
        )
    )
    db.execute(
        s.pending_action.insert().values(
            id=uuid7(), portfolio_id=default_portfolio_id(db), instrument_id=asels, corporate_action_id=ca,
            kind="bonus", status="expected", created_at=now_utc(),
            proposal={"txn_type": "bonus_shares", "quantity": "1200", "text_tr": "Bedelsiz: 1.200 → 2.400"},
        )
    )  # fmt: skip
    db.commit()

    with time_machine.travel(datetime(2026, 11, 1, 17, 59, tzinfo=IST), tick=False):
        # last week's slot (25 Oct) is long past → skipped, not sent late
        assert tx(weekly.send_weekly_review) is None
        assert fake_tg.calls == []
    with time_machine.travel(SUNDAY_1810, tick=False):
        nid = tx(weekly.send_weekly_review)
        assert nid is not None
        assert tx(weekly.send_weekly_review) is None  # once per slot
    text = fake_tg.last_text()
    assert text.startswith("📅 <b>Haftalık değerlendirme</b> · 25.10.2026–01.11.2026")
    assert "Filo genişletme" in text and "Başkasının haberi" not in text and "Eski haber" not in text
    assert "Bedelsiz: 1.200 → 2.400" in text
    assert "İşCep ile mutabakat" in text and "/mutabakat" in text
    assert re.search(r"ASELS\s+1\.200\n", text) and re.search(r"THYAO\s+100</pre>", text)
    assert "Claude kullanımı" in text and "7 gün:" in text
    assert "KAP gecikmesi" in text
    assert len(fake_tg.texts()) == 1


def test_weekly_review_empty_week(db: Connection, fake_tg: FakeTelegram) -> None:
    with time_machine.travel(SUNDAY_1810, tick=False):
        tx(weekly.send_weekly_review)
    text = fake_tg.last_text()
    assert "Bu hafta portföy hisselerinde yeni olay yok." in text
    assert "papi'de açık pozisyon yok" in text and "Operasyon sorunu yok." in text


def test_weekly_sent_late_within_a_day(db: Connection, fake_tg: FakeTelegram) -> None:
    with time_machine.travel(SUNDAY_1810 + timedelta(hours=15), tick=False):  # Mac woke Monday morning
        assert tx(weekly.send_weekly_review) is not None


def test_weekly_registered() -> None:
    import importlib

    from papi.core.registry import REGISTRY

    importlib.reload(importlib.import_module("papi.notify.handlers"))
    assert "periodic:notify:weekly_review" in REGISTRY.all_kinds()
    assert alerts.FLUSH_JOB_KIND in REGISTRY.all_kinds()
