"""Alert engine: instant alerts, threading, revisions, quiet hours, catch-up / rate-limit batching,
ops alerts, digest, macOS guard."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

import pytest
import time_machine
from sqlalchemy import Connection, select

from papi.core import db as core_db
from papi.core import schema as s
from papi.core.events import ALERT_TRIGGERED
from papi.core.ids import uuid7
from papi.core.timeutil import IST, now_utc
from papi.notify import alerts, digest, macos
from papi.notify.ops import OPS_JOB_KIND, ops_alert
from papi.notify.store import add_rule, seed_default_rules, set_enabled
from papi.portfolio.service import watch
from tests.telegram.helpers import FakeTelegram, add_disclosure, add_event, add_instrument, buy


@pytest.fixture
def world(db: Connection, fake_tg: FakeTelegram) -> dict[str, str]:
    ids = {
        "THYAO": add_instrument(db, "THYAO", "Türk Hava Yolları", sector="ulastirma"),
        "ASELS": add_instrument(db, "ASELS", "Aselsan Elektronik", sector="savunma"),
        "EREGL": add_instrument(db, "EREGL", "Ereğli Demir Çelik", sector="metal"),
    }
    buy(db, ids["THYAO"], 100, "250")
    watch(db, "ASELS")
    assert seed_default_rules(db) == 4
    assert seed_default_rules(db) == 0  # idempotent
    db.commit()
    return ids


def tx(fn: Any, *args: Any, **kw: Any) -> Any:
    with core_db.transaction() as conn:
        return fn(conn, *args, **kw)


def notifications(**where: Any) -> list[dict[str, Any]]:
    with core_db.connect() as conn:
        q = select(s.notification).order_by(s.notification.c.created_at)
        for k, v in where.items():
            q = q.where(s.notification.c[k] == v)
        return [dict(r) for r in conn.execute(q).mappings()]


def jobs(kind: str) -> list[dict[str, Any]]:
    with core_db.connect() as conn:
        return [dict(r) for r in conn.execute(select(s.job).where(s.job.c.kind == kind)).mappings()]


def seed_event(conn: Connection, ref: str, iid: str, **kw: Any) -> tuple[str, str]:
    disc_kw = {
        k: kw.pop(k)
        for k in list(kw)
        if k
        in {"published_at", "first_seen_at", "revision_of", "revision_kind", "subject", "summary", "title"}
    }
    did = add_disclosure(conn, ref, **disc_kw)
    eid = add_event(conn, did, [iid], **kw)
    conn.commit()
    return did, eid


# ---------------------------------------------------------------------------------------- instant


def test_holding_alert(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    did, eid = seed_event(
        db, "1500001", world["THYAO"], headline="Uçak alım sözleşmesi", summary="10 adet A321neo"
    )
    nid = tx(alerts.on_event_classified, eid)
    text = fake_tg.last_text()
    assert "<b>THYAO</b> · 💼 HOLDING · P2" in text
    assert "<b>Uçak alım sözleşmesi</b>" in text
    assert "KAP: Özel Durum Açıklaması (Genel)" in text and "10 adet A321neo" in text
    assert "Önem: yüksek (72/100)" in text
    assert '<a href="https://www.kap.org.tr/tr/Bildirim/1500001">KAP bildirimi</a>' in text
    assert "late (catch-up)" not in text
    row = notifications(id=nid)[0]
    assert row["delivered"] is True and row["kind"] == "instant" and row["channel"] == "telegram"
    assert row["external_id"] == str(fake_tg.next_message_id) and row["disclosure_id"] == did
    assert row["alert_rule_id"] is not None
    with core_db.connect() as conn:
        ev = conn.execute(select(s.event_log).where(s.event_log.c.type == ALERT_TRIGGERED)).mappings().one()
    assert ev["data"]["event_id"] == eid and ev["data"]["notification_id"] == nid
    # at-least-once delivery: a repeated event is a no-op
    assert tx(alerts.on_event_classified, eid) is None
    assert len(fake_tg.texts()) == 1


def test_watchlist_needs_medium_materiality(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    _, low = seed_event(db, "1", world["ASELS"], band="low", materiality=20)
    _, med = seed_event(db, "2", world["ASELS"], band="medium", materiality=45, headline="Sözleşme")
    _, other = seed_event(db, "3", world["EREGL"], band="critical", materiality=95)
    assert tx(alerts.on_event_classified, low) is None
    assert tx(alerts.on_event_classified, other) is None
    assert tx(alerts.on_event_classified, med) is not None
    assert len(fake_tg.texts()) == 1 and "👁 WATCH" in fake_tg.last_text()


def test_market_measure_rule_and_disabled_rule(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    _, eid = seed_event(db, "9", world["ASELS"], code="market.measure", band="low", materiality=10)
    assert tx(alerts.on_event_classified, eid) is not None
    tx(set_enabled, "Piyasa tedbirleri (portföy/izleme)", False)
    _, eid2 = seed_event(db, "10", world["ASELS"], code="market.measure", band="low", materiality=10)
    assert tx(alerts.on_event_classified, eid2) is None


def test_superseded_events_are_skipped(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    _, old = seed_event(db, "1", world["THYAO"])
    _, new = seed_event(db, "2", world["THYAO"])
    db.execute(s.event.update().where(s.event.c.id == old).values(superseded_by=new))
    db.commit()
    assert tx(alerts.on_event_classified, old) is None


def test_telegram_not_ready_sends_nothing(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.telegram.enabled = False
    _, eid = seed_event(db, "1", world["THYAO"])
    assert tx(alerts.on_event_classified, eid) is None
    assert fake_tg.calls == []


# ---------------------------------------------------------------------------------------- threading


def _analysis(conn: Connection, eid: str, *, summary: bool = True) -> str:
    aid = uuid7()
    conn.execute(
        s.analysis.insert().values(
            id=aid, event_id=eid, agent="analyst", backend="claude_code_cli", created_at=now_utc(),
            output={"headline_tr": "Çıktı başlığı", "summary_tr": "Çıktı özeti",
                    "claims": [{"kind": "inference", "text_tr": "yorum"}]},
        )
    )  # fmt: skip
    if summary:
        conn.execute(
            s.summary.insert().values(
                id=uuid7(), event_id=eid, lang="tr", headline="Sözleşme cironun %12'si",
                body="Şirket 2,1 milyar TL'lik sözleşme imzaladı.", analysis_id=aid, created_at=now_utc(),
            )
        )  # fmt: skip
    for text, kind in (
        ("Tutar 2,1 milyar TL", "fact"),
        ("Ciroya oranı %12", "computation"),
        ("Olumlu", "inference"),
    ):
        conn.execute(s.claim.insert().values(id=uuid7(), analysis_id=aid, text_tr=text, kind=kind))
    conn.commit()
    return aid


def test_follow_up_replies_to_alert(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    _, eid = seed_event(db, "1500001", world["THYAO"])
    orig = tx(alerts.on_event_classified, eid)
    alert_msg = fake_tg.next_message_id
    aid = _analysis(db, eid)
    nid = tx(alerts.on_analysis_completed, eid, [aid])
    body = fake_tg.bodies()[-1]
    assert body["reply_parameters"]["message_id"] == alert_msg
    text = body["text"]
    assert "🧠 <b>Analiz</b> · THYAO" in text and "Sözleşme cironun %12'si" in text
    assert "• Tutar 2,1 milyar TL" in text and "• Ciroya oranı %12" in text and "Olumlu" not in text
    row = notifications(id=nid)[0]
    assert row["kind"] == "follow_up" and row["reply_to_notification_id"] == orig
    assert tx(alerts.on_analysis_completed, eid, [aid]) is None  # deduped


def test_follow_up_falls_back_to_analysis_output(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    _, eid = seed_event(db, "1", world["THYAO"])
    tx(alerts.on_event_classified, eid)
    aid = _analysis(db, eid, summary=False)
    tx(alerts.on_analysis_completed, eid, [aid])
    assert "Çıktı başlığı" in fake_tg.last_text() and "Çıktı özeti" in fake_tg.last_text()


def test_payload_rules_reevaluated_on_analysis(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    add_rule(
        db, "Büyük içeriden alım", 'code = "ownership.insider_trade" AND payload.net_value_try >= 5_000_000'
    )
    db.commit()
    _, eid = seed_event(db, "7", world["EREGL"], code="ownership.insider_trade", band="low", materiality=30)
    assert tx(alerts.on_event_classified, eid) is None  # payload not known yet
    db.execute(s.event.update().where(s.event.c.id == eid).values(payload={"net_value_try": 7_500_000}))
    db.commit()
    aid = _analysis(db, eid)
    nid = tx(alerts.on_analysis_completed, eid, [aid])
    assert nid is not None
    text = fake_tg.last_text()
    assert "EREGL" in text and "🧠 <b>Analiz</b>" in text
    assert notifications(id=nid)[0]["kind"] == "instant"


def test_revision_notice_threads_to_original(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    did, eid = seed_event(db, "1500001", world["THYAO"])
    orig = tx(alerts.on_event_classified, eid)
    alert_msg = fake_tg.next_message_id
    new_did = add_disclosure(
        db,
        "1500050",
        revision_of=did,
        revision_kind="correction",
        subject="Düzeltme",
        title="Tutar düzeltildi",
    )
    new_eid = add_event(db, new_did, [world["THYAO"]])
    db.commit()
    nid = tx(alerts.on_disclosure_revised, new_did, did, "correction")
    body = fake_tg.bodies()[-1]
    assert body["reply_parameters"]["message_id"] == alert_msg
    assert "🔁 <b>Düzeltme</b> · <b>THYAO</b>" in body["text"] and "Tutar düzeltildi" in body["text"]
    assert "Bildirim/1500050" in body["text"]
    row = notifications(id=nid)[0]
    assert (
        row["kind"] == "revision"
        and row["reply_to_notification_id"] == orig
        and row["disclosure_id"] == new_did
    )
    # the revised disclosure's own classification does not produce a second top-level alert
    assert tx(alerts.on_event_classified, new_eid) is None
    assert tx(alerts.on_disclosure_revised, new_did, did, "correction") is None
    assert len(fake_tg.texts()) == 2


def test_revision_without_original_alert_is_silent(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    did = add_disclosure(db, "1")
    new_did = add_disclosure(db, "2", revision_of=did, revision_kind="update")
    db.commit()
    assert tx(alerts.on_disclosure_revised, new_did, did, "update") is None
    assert fake_tg.calls == []


# ---------------------------------------------------------------------------------------- quiet hours


def test_quiet_hours_hold_and_digest(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.alerts.quiet_hours_enabled = True
    night = datetime(2026, 10, 28, 2, 0, tzinfo=IST)
    with time_machine.travel(night, tick=False):
        _, watch_ev = seed_event(db, "100", world["ASELS"], band="medium", headline="Gece haberi")
        _, p1 = seed_event(db, "101", world["THYAO"], priority="P1", headline="Acil portföy haberi")
        _, p2 = seed_event(db, "102", world["THYAO"], priority="P2", headline="Portföy P2")
        held = tx(alerts.on_event_classified, watch_ev)
        assert fake_tg.texts() == []
        assert notifications(id=held)[0]["error"] == alerts.HELD_QUIET
        tx(alerts.on_event_classified, p1)  # holdings P1 overrides quiet hours
        assert "Acil portföy haberi" in fake_tg.last_text()
        tx(alerts.on_event_classified, p2)
        assert len(fake_tg.texts()) == 1
        # follow-ups for held or quiet-time alerts wait as well
        aid = _analysis(db, p1)
        assert tx(alerts.on_analysis_completed, p1, [aid]) is not None  # P1 holding thread continues
        assert tx(digest.send_digest) is None  # not 08:00 yet
    with time_machine.travel(datetime(2026, 10, 28, 8, 5, tzinfo=IST), tick=False):
        nid = tx(digest.send_digest)
        text = fake_tg.last_text()
        assert text.startswith("🗞 <b>Günlük özet</b> · 28.10.2026")
        assert "Portföy olayları" in text and "Acil portföy haberi" in text and "Portföy P2" in text
        assert "Sessiz saatlerde bekletilenler" in text and "Gece haberi" in text
        delivered = notifications(id=held)[0]
        assert delivered["delivered"] is True and delivered["reply_to_notification_id"] == nid
        assert tx(digest.send_digest) is None  # once a day


def test_rule_specific_quiet_hours(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    assert alerts.quiet_until(
        datetime(2026, 10, 28, 12, 0, tzinfo=IST), {"start": "11:00", "end": "13:00"}
    ) == datetime(2026, 10, 28, 13, 0, tzinfo=IST)
    assert alerts.quiet_until(
        datetime(2026, 10, 28, 23, 30, tzinfo=IST), {"start": "23:00", "end": "07:30"}
    ) == datetime(2026, 10, 29, 7, 30, tzinfo=IST)
    assert (
        alerts.quiet_until(datetime(2026, 10, 28, 8, 0, tzinfo=IST), {"start": "23:00", "end": "07:30"})
        is None
    )


def test_follow_up_deferred_in_quiet_hours(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    from papi.core.jobs import Deferred

    with time_machine.travel(datetime(2026, 10, 28, 22, 50, tzinfo=IST), tick=False):
        _, eid = seed_event(db, "1", world["THYAO"])
        tx(alerts.on_event_classified, eid)
    settings.alerts.quiet_hours_enabled = True
    with time_machine.travel(datetime(2026, 10, 28, 23, 10, tzinfo=IST), tick=False):
        aid = _analysis(db, eid)
        with pytest.raises(Deferred) as info:
            tx(alerts.on_analysis_completed, eid, [aid])
    assert info.value.until == datetime(2026, 10, 29, 7, 30, tzinfo=IST)


# ---------------------------------------------------------------------------------------- batching


def test_catchup_batch_above_threshold(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.alerts.catchup_batch_threshold = 2
    seen = now_utc()
    events = []
    for i in range(3):
        _, eid = seed_event(
            db, f"20{i}", world["THYAO"], headline=f"Gece {i}",
            published_at=seen - timedelta(hours=5), first_seen_at=seen,
        )  # fmt: skip
        events.append(eid)
        tx(alerts.on_event_classified, eid)
    assert fake_tg.texts() == []
    assert len(jobs(alerts.FLUSH_JOB_KIND)) == 1  # one flush scheduled, not three
    assert jobs(alerts.FLUSH_JOB_KIND)[0]["not_before"] > seen
    counts = tx(alerts.flush)
    assert counts["catchup"] == 3
    assert len(fake_tg.texts()) == 1
    text = fake_tg.last_text()
    assert "late (catch-up)" in text and "3 bildirim" in text
    assert all(f"Gece {i}" in text for i in range(3)) and "yayın" in text
    rows = notifications(channel="telegram")
    assert {r["external_id"] for r in rows} == {str(fake_tg.next_message_id)} and all(
        r["delivered"] for r in rows
    )


def test_catchup_individual_at_or_below_threshold(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.alerts.catchup_batch_threshold = 5
    seen = now_utc()
    for i in range(2):
        _, eid = seed_event(
            db, f"30{i}", world["THYAO"], published_at=seen - timedelta(hours=2), first_seen_at=seen
        )
        tx(alerts.on_event_classified, eid)
    tx(alerts.flush)
    texts = fake_tg.texts()
    assert len(texts) == 2 and all("⏰ late (catch-up) · yayın:" in t for t in texts)


def test_rate_limit_overflow_is_batched(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.telegram.max_messages_per_minute = 2
    start = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    with time_machine.travel(start, tick=False):
        ids = []
        for i in range(4):
            _, eid = seed_event(db, f"40{i}", world["THYAO"], headline=f"Haber {i}")
            ids.append(tx(alerts.on_event_classified, eid))
        assert len(fake_tg.texts()) == 2
        held = [n for n in notifications(channel="telegram") if n["error"] == alerts.HELD_RATE]
        assert len(held) == 2
        assert tx(alerts.flush) == {"catchup": 0, "rate": 0}  # window still full → rescheduled
        assert len(fake_tg.texts()) == 2
    with time_machine.travel(start + timedelta(seconds=62), tick=False):
        assert tx(alerts.flush)["rate"] == 2
    texts = fake_tg.texts()
    assert len(texts) == 3 and "2 bildirim" in texts[-1] and "Haber 2" in texts[-1] and "Haber 3" in texts[-1]
    assert all(n["delivered"] for n in notifications(channel="telegram"))


def test_flood_control_retries(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    from papi.core.jobs import RetryLater

    fake_tg.fail["sendMessage"] = (
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 9},
        },
    )
    _, eid = seed_event(db, "1", world["THYAO"])
    with pytest.raises(RetryLater) as info:
        tx(alerts.on_event_classified, eid)
    assert info.value.retry_in == 9
    assert notifications() == []  # rolled back; the job retry sends it
    assert tx(alerts.on_event_classified, eid) is not None


# ---------------------------------------------------------------------------------------- ops


def test_ops_alert_dedupe_and_send(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    t0 = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    with time_machine.travel(t0, tick=False):
        tx(ops_alert, "KAP 3 kez zaman aşımı", dedupe_key="kap:timeout")
        tx(ops_alert, "KAP 4 kez zaman aşımı", dedupe_key="kap:timeout")  # suppressed (same key, < 1 h)
        tx(ops_alert, "Disk dolmak üzere", level="error")
        tx(ops_alert, "Disk dolmak üzere", level="error")  # same text = same default key
    assert len(jobs(OPS_JOB_KIND)) == 2
    with time_machine.travel(t0 + timedelta(minutes=61), tick=False):
        tx(ops_alert, "KAP yine zaman aşımı", dedupe_key="kap:timeout")
    ops_jobs = jobs(OPS_JOB_KIND)
    assert len(ops_jobs) == 3
    nid = tx(alerts.send_ops, ops_jobs[0]["payload"], job_id=ops_jobs[0]["id"])
    assert fake_tg.last_text() == "⚠️ <b>papi ops</b> · warning\nKAP 3 kez zaman aşımı"
    assert notifications(id=nid)[0]["kind"] == "ops"
    with pytest.raises(ValueError):
        tx(ops_alert, "x", level="fatal")


def test_ops_rule_disabled_drops(world: dict[str, str], db: Connection, fake_tg: FakeTelegram) -> None:
    tx(set_enabled, "Operasyon sağlığı", False)
    assert tx(alerts.send_ops, {"text": "x", "level": "warning"}, job_id="j1") is None
    assert fake_tg.calls == []


def test_ops_in_quiet_hours_goes_to_digest_unless_critical(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings
) -> None:
    settings.alerts.quiet_hours_enabled = True
    with time_machine.travel(datetime(2026, 10, 28, 3, 0, tzinfo=IST), tick=False):
        tx(alerts.send_ops, {"text": "Yedekleme başarısız", "level": "error"}, job_id="j1")
        assert fake_tg.calls == []
        tx(alerts.send_ops, {"text": "Veritabanı bozuk", "level": "critical"}, job_id="j2")
        assert "Veritabanı bozuk" in fake_tg.last_text()
    with time_machine.travel(datetime(2026, 10, 28, 8, 1, tzinfo=IST), tick=False):
        tx(digest.send_digest)
    text = fake_tg.last_text()
    assert "⚠️ <b>Operasyon</b>" in text and "Yedekleme başarısız" in text and "Veritabanı bozuk" in text


# ---------------------------------------------------------------------------------------- digest / macOS


def test_digest_quiet_day_and_pending_actions(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram
) -> None:
    with time_machine.travel(datetime(2026, 10, 28, 9, 0, tzinfo=IST), tick=False):
        tx(digest.send_digest)
        assert "yeni olay yok" in fake_tg.last_text()
    from papi.portfolio.service import default_portfolio_id

    ca = uuid7()
    db.execute(
        s.corporate_action.insert().values(
            id=ca, instrument_id=world["THYAO"], type="bonus", status="executed", created_at=now_utc()
        )
    )
    db.execute(
        s.pending_action.insert().values(
            id=uuid7(), portfolio_id=default_portfolio_id(db), instrument_id=world["THYAO"],
            corporate_action_id=ca, kind="bonus", status="expected", created_at=now_utc(),
            proposal={"txn_type": "bonus_shares", "quantity": "100", "text_tr": "Bedelsiz: 100 → 200"},
        )
    )  # fmt: skip
    db.commit()
    with time_machine.travel(datetime(2026, 10, 29, 9, 0, tzinfo=IST), tick=False):
        tx(digest.send_digest)
    assert (
        "Bekleyen kurumsal işlemler" in fake_tg.last_text() and "Bedelsiz: 100 → 200" in fake_tg.last_text()
    )


def test_macos_notification(
    world: dict[str, str], db: Connection, fake_tg: FakeTelegram, settings, monkeypatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        macos.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None
    )
    monkeypatch.setattr(macos.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    _, eid = seed_event(db, "1", world["THYAO"], headline='Yeni "sözleşme"')
    tx(alerts.on_event_classified, eid)
    assert calls == []  # alerts.macos_notifications is off in this test setup
    settings.alerts.macos_notifications = True
    _, eid2 = seed_event(db, "2", world["THYAO"], headline='Yeni "sözleşme"')
    tx(alerts.on_event_classified, eid2)
    assert len(calls) == 1 and calls[0][0] == "/usr/bin/osascript"
    assert 'display notification "Yeni \\"sözleşme\\"" with title "papi · THYAO"' in calls[0][2]
    mac = notifications(channel="macos")
    assert len(mac) == 1 and mac[0]["delivered"] is True and mac[0]["event_id"] == eid2


def test_macos_disabled_off_platform(monkeypatch, settings) -> None:
    settings.alerts.macos_notifications = True
    monkeypatch.setattr(sys, "platform", "linux")
    assert macos.notify("t", "m") is False


def test_handlers_registered() -> None:
    import importlib

    from papi.core.registry import REGISTRY

    importlib.import_module("papi.notify.handlers")
    importlib.reload(importlib.import_module("papi.notify.handlers"))
    kinds = REGISTRY.all_kinds()
    for kind in (
        "event:notify.instant_alert",
        "event:notify.follow_up",
        "event:notify.revision",
        "notify:ops",
        "notify:flush",
        "periodic:notify:digest",
        "periodic:notify:seed_rules",
    ):
        assert kind in kinds
