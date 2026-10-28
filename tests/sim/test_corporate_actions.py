"""Corporate actions from events, driven through triage on the recorded KAP fixtures."""

from __future__ import annotations

import copy
import importlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, select

from papi.core import registry
from papi.core.events import ANALYSIS_COMPLETED, EVENT_CLASSIFIED, get_event, publish
from papi.core.ids import uuid7
from papi.core.schema import analysis, corporate_action, event, pending_action, trading_calendar
from papi.core.timeutil import now_utc
from papi.sim import corporate_actions as ca
from papi.triage.classify import triage_disclosure
from tests.triage.helpers import kap_fixture_structure, seed_company, seed_disclosure, seed_holding

D = Decimal
KP = "Kar Payı Dağıtım İşlemlerine İlişkin Bildirim"
SA = "Sermaye Artırımı - Azaltımı İşlemlerine İlişkin Bildirim"
MKK = "Pay Mali Hak Kullanım İşlemi - Nakit Ödeme"


@pytest.fixture(autouse=True)
def _hermetic(clean_registry: Any) -> None:
    """No plugin fan-out from triage's publish()."""


def _events(conn: Connection, disclosure_id: str) -> list[Any]:
    return conn.execute(
        select(event).where(event.c.primary_disclosure_id == disclosure_id).order_by(event.c.created_at)
    ).all()


def _rows(conn: Connection) -> list[Any]:
    return conn.execute(select(corporate_action).order_by(corporate_action.c.created_at)).all()


def _triage(conn: Connection, **kw: Any) -> list[Any]:
    did = seed_disclosure(conn, **kw)
    triage_disclosure(conn, did)
    return _events(conn, did)


def _mkk_structure(ticker: str, isin: str, net: str, gross: str, paid: str) -> dict[str, Any]:
    s, _ = kap_fixture_structure("1688153")
    s = copy.deepcopy(s)
    row = s["tables"][0]["rows"][0]
    row[2], row[3], row[5], row[6], row[8] = isin, ticker, net, gross, paid
    return s


# ------------------------------------------------------------------------- cash dividend lifecycle (FROTO)


def test_froto_dividend_proposal_then_gm_then_mkk_payment(db: Connection) -> None:
    structure, _basic = kap_fixture_structure("1688363")
    iss, froto = seed_company(db, "FROTO")
    (ev,) = _triage(db, subject=KP, summary="Kar Dağıtım Önerisi", issuer_ids=[iss], structure=structure)
    assert ev.code == "distribution.dividend.cash"

    [cid] = ca.process_event(db, ev.id, today=date(2026, 10, 28))
    row = _rows(db)[0]
    assert (row.id, row.instrument_id, row.type, row.status) == (cid, froto, "dividend_cash", "announced")
    assert row.cash_amount == D("3.07") and row.currency == "TRY"
    assert row.ex_date == date(2026, 11, 26)
    det = row.details
    assert det["net_amount"] == "2.6095" and D(det["withholding_rate"]) == D("0.15")
    assert (det["payment_date"], det["record_date"], det["period_year"]) == ("2026-11-30", "2026-11-27", 2025)
    assert det["decision_body"] == "board" and det["ex_date_final"] is False
    assert det["event_ids"] == [ev.id] and det["ex_date_source"] == "payload"

    # re-delivery is idempotent
    assert ca.process_event(db, ev.id, today=date(2026, 10, 28)) == [cid]
    assert len(_rows(db)) == 1 and _rows(db)[0].details["event_ids"] == [ev.id]

    # general meeting approves; the final ex date moves to 2026-12-03
    gm = copy.deepcopy(structure)
    for t in gm["tables"]:
        if t["caption"] == "Kar Payı Ödeme Tarihleri":
            t["rows"] = [["Peşin", "26.11.2026", "03.12.2026", "07.12.2026", "04.12.2026"]]
    (ev2,) = _triage(
        db,
        subject=KP,
        summary="Kar Payı Dağıtımına İlişkin Genel Kurul Kararı",
        issuer_ids=[iss],
        structure=gm,
    )
    assert ca.process_event(db, ev2.id, today=date(2026, 10, 28)) == [cid]
    row = _rows(db)[0]
    assert row.status == "approved" and row.ex_date == date(2026, 12, 3)
    assert row.details["ex_date_final"] is True and row.details["record_date"] == "2026-12-04"
    assert row.details["event_ids"] == [ev.id, ev2.id] and row.source_event_id == ev2.id

    # MKK records the payment: executed; the estimated ex date (payment − 2 sessions) equals the real one
    mkk = _mkk_structure("FROTO", "TRAOTOSN91H6", "260,95", "307", "07/12/2026")
    (ev3,) = _triage(
        db, subject=MKK, summary="Hak Kullanım İşlemleri", issuer_ids=[iss], structure=mkk, source_class="DKB"
    )
    assert ev3.code == ca.NOTICE_CODE
    assert ca.process_event(db, ev3.id, today=date(2026, 12, 7)) == [cid]
    (row,) = _rows(db)
    assert row.status == "executed" and row.ex_date == date(2026, 12, 3)
    assert row.details["ex_date_source"] == "payload" and row.cash_amount == D("3.07")
    assert [h["status"] for h in row.details["status_history"]] == ["announced", "approved", "executed"]


def test_mkk_payment_without_announcement_creates_executed_row_and_proposes(db: Connection) -> None:
    structure, _ = kap_fixture_structure("1688153")
    iss, mcard = seed_company(db, "MCARD")
    seed_holding(db, mcard, qty="1000", trade_date=date(2026, 9, 1))
    (ev,) = _triage(
        db,
        subject=MKK,
        summary="Hak Kullanım İşlemleri",
        issuer_ids=[iss],
        structure=structure,
        source_class="DKB",
    )
    [cid] = ca.process_event(db, ev.id, today=date(2026, 10, 28))
    row = _rows(db)[0]
    assert (row.type, row.status, row.instrument_id) == ("dividend_cash", "executed", mcard)
    # payment Tue 2026-10-27 → ex Fri 2026-10-23 (T+2, weekend skipped)
    assert row.ex_date == date(2026, 10, 23) and row.details["ex_date_source"] == ca.EX_ESTIMATED
    assert row.cash_amount == D("2.5711011") and row.details["net_amount"] == "2.1854359"
    assert row.details["payment_date"] == "2026-10-27"
    (pa,) = db.execute(select(pending_action).where(pending_action.c.corporate_action_id == cid)).all()
    assert pa.kind == "dividend_cash" and pa.status == "expected"
    assert D(pa.proposal["gross"]) == D("2571.1011") and D(pa.proposal["net"]) == D("2185.4359")


def test_no_proposal_before_ex_date(db: Connection) -> None:
    structure, _ = kap_fixture_structure("1688363")
    iss, froto = seed_company(db, "FROTO")
    seed_holding(db, froto, qty="10", trade_date=date(2026, 9, 1))
    (ev,) = _triage(db, subject=KP, summary="Kar Dağıtım Önerisi", issuer_ids=[iss], structure=structure)
    [cid] = ca.process_event(db, ev.id, today=date(2026, 10, 28))
    assert db.execute(select(pending_action)).all() == []
    # the portfolio periodic's scan picks it up once the ex date is reached
    from papi.portfolio import service

    assert len(service.propose_for_corporate_action(db, cid)) == 1


# ------------------------------------------------------------------------- rights issue lifecycle (CEMAS)


def test_cemas_rights_board_then_gm_then_analyst_dates_then_cancel(db: Connection) -> None:
    structure, _basic = kap_fixture_structure("1687750")
    iss, _cemas = seed_company(db, "CEMAS")
    (ev,) = _triage(
        db, subject=SA, summary="Bedelli Sermaye Artırımı Hakkında.", issuer_ids=[iss], structure=structure
    )
    assert ev.code == "capital.increase.rights"
    [cid] = ca.process_event(db, ev.id)
    row = _rows(db)[0]
    assert (row.type, row.status, row.ex_date) == ("rights", "announced", None)
    assert row.factor == D("1") and row.cash_amount == D("1")
    assert row.details["subscription_price"] == "1" and row.details["board_decision_date"] == "2026-10-26"

    (ev2,) = _triage(
        db,
        subject=SA,
        summary="SERMAYE ARTIRIMINA İLİŞKİN GENEL KURUL SONUCU HAKKINDA",
        issuer_ids=[iss],
        structure=structure,
    )
    assert ca.process_event(db, ev2.id) == [cid]
    assert _rows(db)[0].status == "approved"

    aid = uuid7()
    db.execute(
        analysis.insert().values(
            id=aid,
            event_id=ev2.id,
            agent="analyst",
            backend="claude_code_cli",
            created_at=now_utc(),
            output={
                "payload": {
                    "capital.increase.rights": {
                        "rights_start_date": "2026-12-14",
                        "rights_end_date": "2026-12-28",
                        "subscription_price": 9.99,
                    }
                }
            },
        )
    )
    assert ca.process_event(db, ev2.id, analysis_ids=[aid]) == [cid]
    (row,) = _rows(db)
    assert row.ex_date == date(2026, 12, 14) and row.details["rights_end_date"] == "2026-12-28"
    assert row.cash_amount == D("1")  # deterministic form value wins over the analyst's

    (cancel,) = _triage(
        db,
        subject="Hak Kullanım Süreç İptal Bildirimi",
        summary="Bedelli sermaye artırımı hak kullanım sürecinin iptali",
        issuer_ids=[iss],
    )
    assert cancel.code == ca.CANCEL_CODE
    ca.process_event(db, cancel.id)
    (row,) = _rows(db)
    assert row.status == "cancelled" and row.details["cancel_reason"] == "hak_kullanim_iptal"


def test_correction_updates_same_row_and_superseded_event_is_skipped(db: Connection) -> None:
    structure, _ = kap_fixture_structure("1687750")
    iss, _ = seed_company(db, "CEMAS")
    did = seed_disclosure(
        db, subject=SA, summary="Bedelli Sermaye Artırımı Hakkında.", issuer_ids=[iss], structure=structure
    )
    triage_disclosure(db, did)
    (old,) = _events(db, did)
    [cid] = ca.process_event(db, old.id)

    fixed = copy.deepcopy(structure)
    fixed["tables"][0]["rows"][0][4] = "2,50"  # subscription price corrected 1,00 → 2,50
    corr = seed_disclosure(
        db,
        subject=SA,
        summary="Bedelli Sermaye Artırımı Hakkında (Düzeltme)",
        issuer_ids=[iss],
        structure=fixed,
        revision_of=did,
        revision_kind="correction",
    )
    triage_disclosure(db, corr)
    (new,) = _events(db, corr)
    assert db.execute(select(event.c.superseded_by).where(event.c.id == old.id)).scalar_one() == new.id
    assert ca.process_event(db, old.id) == []  # stale
    assert ca.process_event(db, new.id) == [cid]
    (row,) = _rows(db)
    assert row.cash_amount == D("2.5") and row.details["event_ids"] == [old.id, new.id]


def _event_row(
    conn: Connection, code: str, iid: str, payload: dict | None, superseded_by: str | None = None
) -> str:
    eid = uuid7()
    conn.execute(
        event.insert().values(
            id=eid,
            disclosure_ids=[],
            code=code,
            taxonomy_version="tr-2026.10",
            instrument_ids=[iid],
            announced_at=now_utc(),
            payload=payload,
            status="provisional",
            superseded_by=superseded_by,
            created_at=now_utc(),
        )
    )
    return eid


def test_correction_reclassified_away_cancels_row(db: Connection) -> None:
    _, inst = seed_company(db, "XYZ")
    old = _event_row(db, "capital.increase.bonus", inst, {"bonus_rate_pct": 50, "currency": "TRY"})
    [cid] = ca.process_event(db, old)
    assert _rows(db)[0].factor == D("0.5")
    new = _event_row(db, "capital.change", inst, None)
    db.execute(event.update().where(event.c.id == old).values(superseded_by=new))
    assert ca.process_event(db, new) == []
    (row,) = _rows(db)
    assert (
        row.id == cid
        and row.status == "cancelled"
        and row.details["cancel_reason"] == "superseded_by_correction"
    )


def test_bonus_and_stock_dividend_factors(db: Connection) -> None:
    _, inst = seed_company(db, "XYZ")
    b = _event_row(
        db,
        "capital.increase.bonus",
        inst,
        {"bonus_rate_pct": None, "bonus_amount": 250, "current_capital": 1000, "ex_date": "2026-11-05"},
    )
    s = _event_row(db, "distribution.dividend.stock", inst, {"rate_pct": 12.5, "ex_date": "2026-11-20"})
    ca.process_event(db, b)
    ca.process_event(db, s)
    rows = {r.type: r for r in _rows(db)}
    assert rows["bonus"].factor == D("0.25") and rows["bonus"].ex_date == date(2026, 11, 5)
    assert rows["dividend_stock"].factor == D("0.125") and rows["dividend_stock"].ex_date == date(
        2026, 11, 20
    )


def test_new_dividend_after_past_ex_date_is_a_new_row(db: Connection) -> None:
    _, inst = seed_company(db, "XYZ")
    a = _event_row(
        db,
        "distribution.dividend.cash",
        inst,
        {"gross_per_share": 1.0, "ex_date": "2025-06-02", "period_year": 2024},
    )
    ca.process_event(db, a)
    b = _event_row(
        db,
        "distribution.dividend.cash",
        inst,
        {"gross_per_share": 1.5, "ex_date": "2026-11-26", "period_year": 2025},
    )
    ca.process_event(db, b)
    assert [r.cash_amount for r in _rows(db)] == [D("1"), D("1.5")]


def test_irrelevant_event_ignored(db: Connection) -> None:
    _, inst = seed_company(db, "XYZ")
    assert ca.process_event(db, _event_row(db, "business.contract.award", inst, {"x": 1})) == []
    assert ca.process_event(db, "missing") == []
    assert _rows(db) == []


# ------------------------------------------------------------------------- pieces


@pytest.mark.parametrize(
    ("summary", "status"),
    [
        ("Kar Dağıtım Önerisi", "announced"),
        ("Bedelli Sermaye Artırımı Hakkında.", "announced"),
        ("SERMAYE ARTIRIMINA İLİŞKİN GENEL KURUL SONUCU HAKKINDA", "approved"),
        ("Sermaye artırımına ilişkin izahname SPK tarafından onaylanmıştır", "approved"),
        ("İzahnamenin onaylanması için SPK'ya başvuru yapılmıştır", "announced"),
        ("Kar dağıtım önerisi genel kurulun onayına sunulacaktır", "announced"),
        ("Sermaye artırımının tescili", "approved"),
        ("Bedelli sermaye artırımı tamamlandı", "executed"),
        ("Sermaye Artırımı Sonuçlandı", "executed"),
    ],
)
def test_status_from_text(summary: str, status: str) -> None:
    assert ca.status_from_text(ca._summary_norm(summary, None)) == status


def test_subtract_sessions_skips_weekends_and_holidays(db: Connection) -> None:
    db.execute(
        trading_calendar.insert().values(
            id=uuid7(), calendar_id="XIST", date=date(2026, 10, 29), session_type="closed"
        )
    )
    assert ca.subtract_sessions(db, date(2026, 10, 27), 2) == date(2026, 10, 23)
    assert ca.subtract_sessions(db, date(2026, 11, 2), 2) == date(2026, 10, 28)


def test_handlers_registered_and_run(db: Connection) -> None:
    import papi.sim.handlers as mod

    mod = importlib.reload(mod)
    kinds = registry.REGISTRY.all_kinds()
    assert (
        "event:sim.corporate_actions.classified" in kinds and "event:sim.corporate_actions.analysed" in kinds
    )
    _, inst = seed_company(db, "XYZ")
    eid = _event_row(db, "capital.increase.bonus", inst, {"bonus_rate_pct": 100})
    e1 = publish(
        db, EVENT_CLASSIFIED, f"event/{eid}", {"event_id": eid, "code": "capital.increase.bonus"}, "test"
    )
    e2 = publish(db, ANALYSIS_COMPLETED, f"event/{eid}", {"event_id": eid, "analysis_ids": []}, "test")
    db.commit()
    mod.on_event_classified(get_event(db, e1), None)
    mod.on_analysis_completed(get_event(db, e2), None)
    db.rollback()
    (row,) = _rows(db)
    assert row.type == "bonus" and row.factor == D("1")
