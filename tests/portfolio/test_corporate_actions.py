from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, select

from papi.core import schema as s
from papi.core.events import PORTFOLIO_CHANGED
from papi.core.ids import uuid7
from papi.core.timeutil import ist_date, now_utc
from papi.portfolio import service
from papi.portfolio.service import ProposedTxn
from tests.portfolio.conftest import add_corporate_action, add_instrument

D = Decimal


def _trade(
    conn: Connection, iid: str, typ: str, qty: str, price: str, d: date, portfolio_id: str | None = None
) -> str:
    return service.record_transaction(
        conn,
        ProposedTxn(
            portfolio_id=portfolio_id,
            instrument_id=iid,
            type=typ,
            quantity=D(qty),
            price=D(price),
            trade_date=d,
        ),
    )


def _holding(conn: Connection, iid: str, pid: str | None = None) -> service.Holding:
    pid = pid or service.default_portfolio_id(conn)
    return service.ledger(conn, pid)[(pid, iid)]


@pytest.fixture
def xyz(db: Connection) -> str:
    iid = add_instrument(db, "XYZ", "XYZ SANAYİ A.Ş.")
    _trade(db, iid, "buy", "1200", "10", date(2026, 9, 1))
    return iid


def test_bonus_issue_pending_action_and_cost_basis(db: Connection, xyz: str) -> None:
    ca = add_corporate_action(db, xyz, "bonus", date(2026, 11, 5), factor="1")
    _trade(db, xyz, "buy", "100", "12", date(2026, 11, 5))  # bought on ex-date: no entitlement

    [pa_id] = service.propose_for_corporate_action(db, ca)
    assert service.propose_for_corporate_action(db, ca) == [pa_id]  # idempotent
    [pa] = service.pending_actions(db)
    assert (
        pa["id"] == pa_id and pa["ticker"] == "XYZ" and pa["kind"] == "bonus" and pa["status"] == "expected"
    )
    assert pa["ex_date"] == date(2026, 11, 5)
    prop = pa["proposal"]
    assert prop["held"] == "1200" and prop["quantity"] == "1200" and prop["quantity_after"] == "2400"
    assert prop["txn_type"] == "bonus_shares" and D(prop["avg_cost_after"]) == D(5)
    assert "1.200 → 2.400 pay" in prop["text_tr"] and "10,00 → 5,00" in prop["text_tr"]

    tid = service.apply_pending_action(db, pa_id)
    txn = db.execute(select(s.txn).where(s.txn.c.id == tid)).one()
    assert txn.type == "bonus_shares" and txn.quantity == 1200 and txn.source == f"derived:{ca}"
    assert txn.trade_date == date(2026, 11, 5)
    h = _holding(db, xyz)
    assert h.quantity == 2500 and h.cost_total == D(13200)
    row = db.execute(select(s.pending_action).where(s.pending_action.c.id == pa_id)).one()
    assert row.status == "applied" and row.transaction_id == tid and row.resolved_at is not None
    assert service.pending_actions(db) == []
    with pytest.raises(ValueError, match="sonuçlanmış"):
        service.apply_pending_action(db, pa_id)
    last = db.execute(
        select(s.event_log.c.data)
        .where(s.event_log.c.type == PORTFOLIO_CHANGED)
        .order_by(s.event_log.c.time.desc())
    ).first()
    assert last.data["reason"] == "corporate_action"

    # undoing the derived transaction re-opens the pending action
    service.reverse_transaction(db, tid)
    assert [a["id"] for a in service.pending_actions(db)] == [pa_id]
    assert _holding(db, xyz).quantity == 1300


def test_bonus_fraction_and_quantity_override(db: Connection) -> None:
    iid = add_instrument(db, "ABC", "ABC A.Ş.")
    _trade(db, iid, "buy", "333", "3", date(2026, 1, 2))
    ca = add_corporate_action(db, iid, "bonus", date(2026, 2, 1), factor="0.25")
    [pa_id] = service.propose_for_corporate_action(db, ca)
    prop = service.pending_actions(db)[0]["proposal"]
    assert prop["quantity"] == "83" and prop["fraction"] == "0.25" and "Kesir" in prop["text_tr"]
    tid = service.apply_pending_action(db, pa_id, quantity_override=D(84))  # İşCep shows 417
    assert db.execute(select(s.txn.c.quantity).where(s.txn.c.id == tid)).scalar_one() == 84
    h = _holding(db, iid)
    assert h.quantity == 417 and h.cost_total == D(999)


def test_rights_issue_subscribe_and_dismiss(db: Connection, xyz: str) -> None:
    pid2 = uuid7()
    db.execute(s.portfolio.insert().values(id=pid2, name="İkinci", base_currency="TRY", created_at=now_utc()))
    _trade(db, xyz, "buy", "100", "20", date(2026, 9, 2), portfolio_id=pid2)
    ca = add_corporate_action(db, xyz, "rights", date(2026, 10, 1), factor="0.5", cash_amount="1.00")
    ids = service.propose_for_corporate_action(db, ca)
    assert len(ids) == 2
    by_pf = {a["portfolio_id"]: a for a in service.pending_actions(db)}
    main = by_pf[service.default_portfolio_id(db)]
    prop = main["proposal"]
    assert (
        prop["quantity"] == "600"
        and D(prop["cash_needed"]) == D(600)
        and prop["price_assumed_nominal"] is False
    )
    assert prop["choices"] == ["subscribe", "sell_rights", "lapse"]
    assert "600 yeni pay için 600,00 TL" in prop["text_tr"]

    tid = service.apply_pending_action(db, main["id"])
    txn = db.execute(select(s.txn).where(s.txn.c.id == tid)).one()
    assert txn.type == "rights_subscription" and txn.price == D("1.00")
    h = _holding(db, xyz)
    assert h.quantity == 1800 and h.cost_total == D(12600)  # 12 000 + 600·1

    service.dismiss_pending_action(db, by_pf[pid2]["id"])
    assert service.pending_actions(db) == []
    assert {a["status"] for a in service.pending_actions(db, "all")} == {"applied", "dismissed"}
    assert _holding(db, xyz, pid2).quantity == 100
    with pytest.raises(ValueError):
        service.dismiss_pending_action(db, by_pf[pid2]["id"])


def test_rights_without_price_assumes_nominal(db: Connection, xyz: str) -> None:
    ca = add_corporate_action(db, xyz, "rights", date(2026, 10, 1), factor="1")
    service.propose_for_corporate_action(db, ca)
    prop = service.pending_actions(db)[0]["proposal"]
    assert (
        prop["price"] == "1" and prop["price_assumed_nominal"] is True and "nominal 1 TL" in prop["text_tr"]
    )


@pytest.mark.parametrize(
    ("ca_type", "factor", "after", "avg_after"),
    [("split", "2", 2400, D(5)), ("reverse_split", "0.1", 120, D(100))],
)
def test_split_and_reverse_split(
    db: Connection, xyz: str, ca_type: str, factor: str, after: int, avg_after: Decimal
) -> None:
    ca = add_corporate_action(db, xyz, ca_type, date(2026, 10, 1), factor=factor)
    [pa_id] = service.propose_for_corporate_action(db, ca)
    tid = service.apply_pending_action(db, pa_id)
    txn = db.execute(select(s.txn).where(s.txn.c.id == tid)).one()
    assert txn.type == "split" and txn.quantity == after - 1200
    h = _holding(db, xyz)
    assert h.quantity == after and h.cost_total == D(12000) and h.avg_cost == avg_after


def test_cash_dividend_with_withholding(db: Connection, xyz: str) -> None:
    ca = add_corporate_action(
        db, xyz, "dividend_cash", date(2026, 10, 1), cash_amount="2.50", details={"withholding_rate": "0.15"}
    )
    [pa_id] = service.propose_for_corporate_action(db, ca)
    prop = service.pending_actions(db)[0]["proposal"]
    assert D(prop["gross"]) == D(3000) and D(prop["tax"]) == D(450) and D(prop["net"]) == D(2550)
    assert "stopaj 450,00 TL, net 2.550,00 TL" in prop["text_tr"]
    tid = service.apply_pending_action(db, pa_id)
    txn = db.execute(select(s.txn).where(s.txn.c.id == tid)).one()
    assert (
        txn.type == "dividend_cash" and txn.quantity == 1200 and txn.price == D("2.50") and txn.tax == D(450)
    )
    h = _holding(db, xyz)
    assert h.quantity == 1200 and h.cost_total == D(12000) and h.income == D(2550)


def test_cash_dividend_net_amount_and_override(db: Connection, xyz: str) -> None:
    ca = add_corporate_action(
        db, xyz, "dividend_cash", date(2026, 10, 1), cash_amount="1", details={"net_amount": "0.9"}
    )
    [pa_id] = service.propose_for_corporate_action(db, ca)
    tid = service.apply_pending_action(db, pa_id, quantity_override=D(600))
    txn = db.execute(select(s.txn).where(s.txn.c.id == tid)).one()
    assert txn.quantity == 600 and txn.tax == D(60)


def test_no_proposal_when_not_held_cancelled_or_incomplete(db: Connection, xyz: str) -> None:
    other = add_instrument(db, "QQQ", "QQQ A.Ş.")
    assert (
        service.propose_for_corporate_action(
            db, add_corporate_action(db, other, "bonus", date(2026, 10, 1), factor="1")
        )
        == []
    )
    early = add_corporate_action(db, xyz, "bonus", date(2026, 8, 1), factor="1")  # before the buy
    assert service.propose_for_corporate_action(db, early) == []
    cancelled = add_corporate_action(db, xyz, "bonus", date(2026, 10, 1), factor="1", status="cancelled")
    assert service.propose_for_corporate_action(db, cancelled) == []
    assert (
        service.propose_for_corporate_action(db, add_corporate_action(db, xyz, "bonus", date(2026, 10, 1)))
        == []
    )
    assert (
        service.propose_for_corporate_action(db, add_corporate_action(db, xyz, "merger", date(2026, 10, 1)))
        == []
    )
    with pytest.raises(ValueError):
        service.propose_for_corporate_action(db, "nope")


def test_scan_corporate_actions_window(db: Connection) -> None:
    iid = add_instrument(db, "XYZ", "XYZ A.Ş.")
    today = ist_date()
    _trade(db, iid, "buy", "10", "1", today - timedelta(days=200))
    recent = add_corporate_action(db, iid, "bonus", today - timedelta(days=3), factor="1")
    add_corporate_action(db, iid, "bonus", today + timedelta(days=3), factor="1")  # holders not fixed yet
    add_corporate_action(db, iid, "bonus", today - timedelta(days=100), factor="1")  # too old
    ids = service.scan_corporate_actions(db)
    assert len(ids) == 1
    assert service.pending_actions(db)[0]["corporate_action_id"] == recent
    assert service.scan_corporate_actions(db) == ids


def test_reconcile_hint_points_to_pending_action(db: Connection, xyz: str) -> None:
    ca = add_corporate_action(db, xyz, "bonus", date(2026, 10, 1), factor="1")
    [pa_id] = service.propose_for_corporate_action(db, ca)
    [diff] = service.reconcile(db, {"XYZ": D(2400)})
    assert diff.delta == 1200 and pa_id in diff.hint
