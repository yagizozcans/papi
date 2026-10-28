"""/durdur, /devam, /kullanim — Claude governor controls over Telegram."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import jobs
from papi.core import schema as s
from papi.core.ids import uuid7
from papi.core.state import set_state
from papi.core.timeutil import now_utc
from papi.llm import governor
from papi.telegram.commands import HELP_TEXT, parse_duration
from tests.telegram.helpers import FakeTelegram
from tests.telegram.test_trade_flow import Chat


@pytest.fixture
def chat(db: Connection, fake_tg: FakeTelegram) -> Chat:
    return Chat(fake_tg)


def _pause() -> dict | None:
    with core_db.connect() as conn:
        return governor.pause_state(conn)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3s", timedelta(hours=3)),
        ("3 saat", timedelta(hours=3)),
        ("90dk", timedelta(minutes=90)),
        ("90 dakika", timedelta(minutes=90)),
        ("1s30dk", timedelta(hours=1, minutes=30)),
        ("1 saat 30 dk", timedelta(hours=1, minutes=30)),
        ("2g", timedelta(days=2)),
        ("1 gün 2 saat", timedelta(days=1, hours=2)),
        ("45m", timedelta(minutes=45)),
        ("2h", timedelta(hours=2)),
        ("3", None),
        ("0dk", None),
        ("31g", None),
        ("yarın", None),
        ("dk3", None),
    ],
)
def test_parse_duration(text: str, expected: timedelta | None) -> None:
    assert parse_duration(text) == expected


def test_pause_with_duration_and_resume(chat: Chat) -> None:
    before = now_utc()
    reply = chat.say("/durdur 1s30dk")
    assert "1 saat 30 dakika durduruldu" in reply and "/devam" in reply
    p = _pause()
    assert p is not None and p["reason"] == "telegram"
    assert timedelta(minutes=89) < p["until"] - before < timedelta(minutes=91)
    assert "durduruldu (Telegram /durdur)" in chat.say("/kullanim")
    assert "sürüyor" in chat.say("/devam")
    assert _pause() is None
    assert "zaten çalışıyor" in chat.say("/devam")


def test_pause_without_duration_until_resume(chat: Chat) -> None:
    assert "/devam yazana kadar" in chat.say("/durdur")
    p = _pause()
    assert p is not None and p["until"] is None
    assert "/devam yazılana kadar" in chat.say("/kullanım")  # Turkish ı in the command works
    with core_db.connect() as conn:
        assert governor.admit(conn, "analyst", "P1").ok is False


def test_bad_duration(chat: Chat) -> None:
    assert "Süre anlaşılamadı" in chat.say("/durdur yarın")
    assert _pause() is None


def test_usage_report(chat: Chat, db: Connection, settings) -> None:
    settings.llm.calibration_mode = False
    settings.llm.share_5h = 2.0
    settings.llm.share_week = 20.0
    now = now_utc()
    for task, prio, cost, at in (
        ("analyst", "P1", 0.5, now - timedelta(hours=1)),
        ("chat", "chat", 0.25, now - timedelta(hours=2)),
        ("analyst", "P2", 1.0, now - timedelta(days=3)),
    ):
        db.execute(
            s.llm_usage_ledger.insert().values(
                id=uuid7(), task=task, priority=prio, cost_usd_est=cost, outcome="ok", at=at
            )
        )
    set_state(
        db,
        governor.RATE_LIMIT_KEY,
        {
            "at": now.isoformat(),
            "info": {
                "status": "allowed",
                "unifiedWindows": {
                    "five_hour": {
                        "utilization": 0.2,
                        "resetsAt": int((now + timedelta(hours=2)).timestamp()),
                    },
                    "seven_day": {
                        "utilization": 0.35,
                        "resetsAt": int((now + timedelta(days=3)).timestamp()),
                    },
                },
            },
        },
    )
    jid = jobs.enqueue(db, "analyst:run", {}, priority=jobs.PRIORITY_P2)
    db.execute(s.job.update().where(s.job.c.id == jid).values(last_error="llm:cap:5h"))
    db.commit()
    text = chat.say("/kullanim")
    assert text.startswith("📊 <b>Claude kullanımı</b>")
    assert "▶️ Claude analizi çalışıyor." in text
    assert "5 saat (analiz): $0,50 / $2,00 (%25)" in text
    assert "5 saat (sohbet): $0,25 / $1,00 (%25)" in text
    assert "7 gün: $1,75 / $20,00 (%9)" in text
    assert "5 saatlik çubuk: %20" in text and "haftalık çubuk: %35" in text
    assert "Bekleyen Claude işleri: 1 (P2 1)" in text


def test_usage_without_bars_or_queue(chat: Chat) -> None:
    text = chat.say("/kullanim")
    assert "Claude aboneliği" not in text and "Bekleyen Claude işi yok." in text


def test_help_lists_new_commands(chat: Chat) -> None:
    text = chat.say("/yardim")
    for cmd in ("/durdur", "/devam", "/kullanim", "/mutabakat", "/bekleyen"):
        assert cmd in text and cmd in HELP_TEXT
    assert "haftalık değerlendirme" in text
