"""Follow-up text after analysis.completed: only verified claims, summary wins, checker ignored,
flagged analyses get a warning line."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection

from papi.core import db as core_db
from papi.core import schema as s
from papi.core.ids import uuid7
from papi.core.timeutil import now_utc
from papi.notify import alerts
from papi.notify.context import load_event_facts
from papi.notify.store import seed_default_rules
from tests.telegram.helpers import FakeTelegram, add_disclosure, add_event, add_instrument, buy


@pytest.fixture
def event_id(db: Connection, fake_tg: FakeTelegram) -> str:
    iid = add_instrument(db, "THYAO", "Türk Hava Yolları")
    buy(db, iid, 100, "250")
    seed_default_rules(db)
    eid = add_event(db, add_disclosure(db, "1700001"), [iid])
    db.commit()
    return eid


def add_analysis(
    conn: Connection,
    eid: str,
    *,
    agent: str = "analyst",
    review_state: str = "auto",
    output: dict[str, Any] | None = None,
    claims: list[tuple[str, str, str | None, str | None]] = (),  # (text, kind, verify, check)
) -> str:
    aid = uuid7()
    conn.execute(
        s.analysis.insert().values(
            id=aid, event_id=eid, agent=agent, backend="claude_code_cli", created_at=now_utc(),
            review_state=review_state, output=output,
        )
    )  # fmt: skip
    for text, kind, verify, check in claims:
        conn.execute(
            s.claim.insert().values(
                id=uuid7(), analysis_id=aid, text_tr=text, kind=kind, verify_status=verify, check_status=check
            )
        )
    return aid


def add_summary(conn: Connection, eid: str, aid: str, headline: str | None, body: str | None) -> None:
    conn.execute(
        s.summary.insert().values(
            id=uuid7(),
            event_id=eid,
            lang="tr",
            headline=headline,
            body=body,
            analysis_id=aid,
            created_at=now_utc(),
        )
    )


def follow_up(eid: str, ids: list[str]) -> str | None:
    with core_db.connect() as conn:
        f = load_event_facts(conn, eid)
        assert f is not None
        return alerts.format_follow_up(conn, f, ids)


def test_failed_and_unsupported_claims_are_dropped(db: Connection, event_id: str) -> None:
    aid = add_analysis(
        db,
        event_id,
        claims=[
            ("Tutar 2,1 milyar TL", "fact", "pass", "supported"),
            ("Yanlış tutar 9 milyar", "fact", "fail", None),
            ("Desteksiz oran %40", "computation", "pass", "unsupported"),
            ("Henüz denetlenmemiş oran %12", "computation", None, None),
            ("Yorum", "inference", "pass", None),
        ],
    )
    add_summary(db, event_id, aid, "Başlık", "Gövde")
    db.commit()
    text = follow_up(event_id, [aid])
    assert text is not None
    assert "• Tutar 2,1 milyar TL" in text and "• Henüz denetlenmemiş oran %12" in text
    assert "Yanlış tutar" not in text and "Desteksiz" not in text and "Yorum" not in text


def test_filtered_claims_do_not_come_back_from_output(db: Connection, event_id: str) -> None:
    out = {"claims": [{"kind": "fact", "text_tr": "Yanlış tutar 9 milyar", "verify_status": "fail"}]}
    aid = add_analysis(db, event_id, output=out, claims=[("Yanlış tutar 9 milyar", "fact", "fail", None)])
    add_summary(db, event_id, aid, "Başlık", None)
    db.commit()
    text = follow_up(event_id, [aid])
    assert text is not None and "Yanlış tutar" not in text and "Öne çıkanlar" not in text


def test_output_claims_fallback_respects_statuses(db: Connection, event_id: str) -> None:
    out = {
        "headline_tr": "Çıktı başlığı",
        "claims": [
            {"kind": "fact", "text_tr": "Doğru", "verify_status": "pass"},
            {"kind": "fact", "text_tr": "Hatalı", "verify_status": "fail"},
            {"kind": "fact", "text_tr": "Desteksiz", "check_status": "unsupported"},
        ],
    }
    aid = add_analysis(db, event_id, output=out)
    db.commit()
    text = follow_up(event_id, [aid])
    assert text is not None and "• Doğru" in text and "Hatalı" not in text and "Desteksiz" not in text


def test_summary_row_wins_without_output_fallback(db: Connection, event_id: str) -> None:
    out = {"headline_tr": "Çıktı başlığı", "summary_tr": "Doğrulanmamış çıktı metni"}
    aid = add_analysis(db, event_id, output=out, claims=[("Tutar 5 milyon", "fact", "pass", None)])
    add_summary(db, event_id, aid, "Özet başlığı", None)  # body deliberately empty
    db.commit()
    text = follow_up(event_id, [aid])
    assert text is not None and "Özet başlığı" in text
    assert "Çıktı başlığı" not in text and "Doğrulanmamış çıktı metni" not in text


def test_checker_analysis_is_not_a_source(db: Connection, event_id: str) -> None:
    checker = add_analysis(
        db,
        event_id,
        agent="checker",
        output={"headline_tr": "Denetçi başlığı", "summary_tr": "Denetçi metni"},
        claims=[("Denetçi iddiası", "fact", "pass", None)],
    )
    db.commit()
    assert follow_up(event_id, [checker]) is None
    add_summary(db, event_id, checker, "Denetçi özeti", "x")
    analyst = add_analysis(db, event_id, output={"headline_tr": "Analist başlığı"})
    db.commit()
    text = follow_up(event_id, [analyst, checker])
    assert text is not None and "Analist başlığı" in text
    assert "Denetçi" not in text


def test_flagged_analysis_gets_note(db: Connection, event_id: str) -> None:
    aid = add_analysis(
        db, event_id, review_state="flagged", claims=[("Tutar 2,1 milyar TL", "fact", "pass", None)]
    )
    add_summary(db, event_id, aid, "Başlık", "Gövde")
    db.commit()
    text = follow_up(event_id, [aid])
    assert text is not None and "Başlık" in text and "Tutar 2,1 milyar TL" in text
    assert text.endswith(alerts.FLAGGED_NOTE)
    clean = add_analysis(db, event_id, review_state="approved")
    db.commit()
    assert alerts.FLAGGED_NOTE not in (follow_up(event_id, [clean]) or "")


def test_flagged_note_in_sent_reply(db: Connection, event_id: str, fake_tg: FakeTelegram) -> None:
    with core_db.transaction() as conn:
        alerts.on_event_classified(conn, event_id)
    alert_msg = fake_tg.next_message_id
    aid = add_analysis(db, event_id, review_state="flagged")
    add_summary(db, event_id, aid, "Başlık", "Gövde")
    db.commit()
    with core_db.transaction() as conn:
        assert alerts.on_analysis_completed(conn, event_id, [aid]) is not None
    body = fake_tg.bodies()[-1]
    assert body["reply_parameters"]["message_id"] == alert_msg
    assert body["text"].endswith(alerts.FLAGGED_NOTE)
