from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, func, select

from papi.core import schema as s
from papi.core.events import PORTFOLIO_CHANGED
from papi.core.ids import uuid7
from papi.core.timeutil import ist_date, now_utc
from papi.portfolio import service
from papi.portfolio.service import ProposedTxn
from tests.portfolio.conftest import add_close, add_instrument, add_ticker

D = Decimal


def _buy(conn: Connection, iid: str, qty: str, price: str, d: date, fees: str = "0", typ: str = "buy") -> str:
    return service.record_transaction(
        conn,
        ProposedTxn(instrument_id=iid, type=typ, quantity=D(qty), price=D(price), fees=D(fees), trade_date=d),
    )


def _sell(conn: Connection, iid: str, qty: str, price: str, d: date, fees: str = "0") -> str:
    return _buy(conn, iid, qty, price, d, fees, typ="sell")


def _events(conn: Connection) -> list[dict]:
    return [
        r.data
        for r in conn.execute(
            select(s.event_log.c.data)
            .where(s.event_log.c.type == PORTFOLIO_CHANGED)
            .order_by(s.event_log.c.time)
        )
    ]


def _state(conn: Connection, iid: str) -> service.Holding:
    pid = service.default_portfolio_id(conn)
    return service.ledger(conn, pid)[(pid, iid)]


def test_weighted_average_cost_through_buys_sells_and_reversal(db: Connection) -> None:
    iid = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.", "TÜRK HAVA YOLLARI")
    t1 = _buy(db, iid, "100", "10", date(2026, 1, 5), fees="5")
    t2 = _buy(db, iid, "100", "20", date(2026, 1, 6))
    _sell(db, iid, "50", "30", date(2026, 1, 7), fees="3")

    h = _state(db, iid)
    assert h.quantity == 150
    # avg after buys = 3005 / 200 = 15.025; 50 sold release 751.25
    assert h.cost_total == D("2253.75")
    assert h.avg_cost == D("15.025")
    assert h.realised_pnl == D("745.75")  # 1500 − 3 − 751.25

    rid = service.reverse_transaction(db, t2, reason="yanlış giriş")
    h = _state(db, iid)
    assert h.quantity == 50
    assert h.cost_total == D("502.5")
    assert h.realised_pnl == D("994.5")  # 1500 − 3 − 502.5

    rev = db.execute(select(s.txn).where(s.txn.c.id == rid)).one()
    assert rev.reverses_id == t2 and rev.quantity == -100 and rev.type == "buy"
    assert rev.trade_date == date(2026, 1, 6) and "yanlış giriş" in rev.note
    assert db.execute(select(func.count()).select_from(s.txn)).scalar_one() == 4  # append-only

    with pytest.raises(ValueError, match="zaten geri alınmış"):
        service.reverse_transaction(db, t2)
    with pytest.raises(ValueError, match="Ters kayıt"):
        service.reverse_transaction(db, rid)

    events = _events(db)
    assert [e["reason"] for e in events] == ["trade", "trade", "trade", "undo"]
    assert events[0]["instrument_ids"] == [iid] and events[0]["transaction_ids"] == [t1]
    assert events[-1]["transaction_ids"] == [rid]


def test_sell_everything_resets_cost_and_opening_balance(db: Connection) -> None:
    iid = add_instrument(db, "ASELS", "ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş.", "ASELSAN")
    _buy(db, iid, "200", "50", date(2026, 2, 1), typ="opening_balance")
    _sell(db, iid, "200", "60", date(2026, 2, 2))
    h = _state(db, iid)
    assert h.quantity == 0 and h.cost_total == 0 and h.avg_cost is None
    assert h.realised_pnl == D("2000")
    assert service.positions(db) == []


def test_record_transaction_dedupe_key_is_idempotent(db: Connection) -> None:
    iid = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    p = ProposedTxn(
        ticker="thyao", type="buy", quantity=D(10), price=D("285.4"), trade_date=date(2026, 10, 1)
    )
    a = service.record_transaction(db, p, dedupe_key="telegram:abc")
    b = service.record_transaction(db, p, dedupe_key="telegram:abc")
    assert a == b
    rows = db.execute(select(s.txn)).all()
    assert len(rows) == 1 and rows[0].instrument_id == iid and rows[0].source == "manual"
    assert len(_events(db)) == 1


def test_record_transaction_rejects_bad_input(db: Connection) -> None:
    add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    with pytest.raises(ValueError, match="Bilinmeyen hisse kodu: THYA"):
        service.record_transaction(
            db, ProposedTxn(ticker="THYA", type="buy", quantity=D(1), price=D(1), trade_date=date(2026, 1, 1))
        )
    with pytest.raises(ValueError, match="fiyat gerekli"):
        service.record_transaction(
            db, ProposedTxn(ticker="THYAO", type="buy", quantity=D(1), trade_date=date(2026, 1, 1))
        )
    with pytest.raises(ValueError, match="sıfırdan büyük"):
        service.record_transaction(
            db,
            ProposedTxn(ticker="THYAO", type="sell", quantity=D(0), price=D(1), trade_date=date(2026, 1, 1)),
        )


def test_fee_and_tax_rows(db: Connection) -> None:
    iid = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    _buy(db, iid, "10", "100", date(2026, 1, 1))
    service.record_transaction(
        db,
        ProposedTxn(instrument_id=iid, type="tax", quantity=D(0), fees=D("7.5"), trade_date=date(2026, 1, 2)),
    )
    fee_id = service.record_transaction(
        db, ProposedTxn(type="fee", quantity=D(0), fees=D(3), trade_date=date(2026, 1, 3))
    )
    row = db.execute(select(s.txn).where(s.txn.c.type == "tax")).one()
    assert row.tax == D("7.5") and row.fees == 0
    assert db.execute(select(s.txn.c.instrument_id).where(s.txn.c.id == fee_id)).scalar_one() is None
    assert _state(db, iid).realised_pnl == D("-7.5")


def test_positions_valuation_weights_and_snapshot(db: Connection) -> None:
    a = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    b = add_instrument(db, "ASELS", "ASELSAN")
    c = add_instrument(db, "SASA", "SASA POLYESTER")
    _buy(db, a, "100", "10", date(2026, 3, 1))
    _buy(db, b, "50", "40", date(2026, 3, 1))
    _buy(db, c, "10", "5", date(2026, 3, 1))
    add_close(db, a, date(2026, 3, 2), 12.0)
    add_close(db, a, date(2026, 3, 3), 15.0, source="yahoo_prices")
    add_close(db, a, date(2026, 3, 3), 14.0)  # primary wins on the same day
    add_close(db, a, date(2026, 3, 9), 99.0)  # after as_of
    add_close(db, b, date(2026, 3, 1), 42.0)

    ps = service.positions(db, as_of=date(2026, 3, 5))
    assert [p.ticker for p in ps] == ["ASELS", "THYAO", "SASA"]
    by = {p.ticker: p for p in ps}
    assert by["THYAO"].last_close == D("14.0") and by["THYAO"].last_close_date == date(2026, 3, 3)
    assert by["THYAO"].market_value == D("1400.0")
    assert by["ASELS"].market_value == D("2100.0")
    assert by["SASA"].last_close is None and by["SASA"].market_value is None and by["SASA"].weight is None
    assert by["THYAO"].weight == pytest.approx(1400 / 3500)
    assert by["ASELS"].avg_cost == D(40) and by["ASELS"].cost_total == D(2000)

    assert service.snapshot_positions(db, date(2026, 3, 5)) == 3
    assert service.snapshot_positions(db, date(2026, 3, 5)) == 3  # rewrite, not duplicate
    snap = db.execute(select(s.position_snapshot).where(s.position_snapshot.c.instrument_id == a)).one()
    assert snap.quantity == 100 and snap.market_value_try == D("1400.0") and snap.avg_cost_try == D(10)
    assert snap.weight == pytest.approx(0.4)


# ------------------------------------------------------------------------------------------- resolution


@pytest.fixture
def universe(db: Connection) -> dict[str, str]:
    ids = {
        "THYAO": add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.", "TÜRK HAVA YOLLARI"),
        "ISCTR": add_instrument(db, "ISCTR", "TÜRKİYE İŞ BANKASI A.Ş.", "İŞ BANKASI"),
        "ISGYO": add_instrument(db, "ISGYO", "İŞ GAYRİMENKUL YATIRIM ORTAKLIĞI A.Ş.", "İŞ GYO"),
        "IEYHO": add_instrument(db, "IEYHO", "IŞIKLAR ENERJİ VE YAPI HOLDİNG A.Ş.", "IŞIKLAR ENERJİ"),
        "XU030": add_instrument(db, "XU030", None, itype="index"),
    }
    renamed = add_instrument(db, "KOZAA", "KOZA ANADOLU METAL MADENCİLİK İŞLETMELERİ A.Ş.", "KOZA ANADOLU",
                             valid_from=date(2021, 1, 1))  # fmt: skip
    add_ticker(db, renamed, "OLDKZ", valid_from=s.OPEN_START, valid_to=date(2020, 12, 31))
    ids["KOZAA"] = renamed
    return ids


def test_resolve_exact_ticker_case_and_turkish_i(db: Connection, universe: dict[str, str]) -> None:
    for q in ("THYAO", "thyao", " ThYaO "):
        top = service.resolve_instrument(db, q)[0]
        assert (top.ticker, top.score) == ("THYAO", 1.0)
    # lower-case "i" must not turn into "İ"
    assert service.resolve_instrument(db, "isctr")[0].instrument_id == universe["ISCTR"]
    assert service.resolve_instrument(db, "İSCTR")[0].instrument_id == universe["ISCTR"]
    assert service.resolve_instrument(db, "ISCTR.IS")[0].score == 1.0


def test_resolve_by_turkish_names(db: Connection, universe: dict[str, str]) -> None:
    top = service.resolve_instrument(db, "iş bankası")
    assert top[0].ticker == "ISCTR" and top[0].score >= 0.85
    assert top[0].issuer_name == "İŞ BANKASI"
    for q in ("ışıklar", "IŞIKLAR", "isiklar enerji", "ISIKLAR"):
        assert service.resolve_instrument(db, q)[0].ticker == "IEYHO", q
    assert service.resolve_instrument(db, "türk hava")[0].ticker == "THYAO"
    assert service.resolve_instrument(db, "TURK HAVA YOLLARI")[0].score == 0.95
    assert service.resolve_instrument(db, "turk hava yollar")[0].ticker == "THYAO"  # name prefix
    typo = service.resolve_instrument(db, "aselsn isiklr")
    assert all(m.score < 0.75 for m in typo)


def test_resolve_ranking_prefix_and_history(db: Connection, universe: dict[str, str]) -> None:
    res = service.resolve_instrument(db, "IS")
    tickers = [m.ticker for m in res]
    # ticker prefixes ISCTR/ISGYO and name prefixes (İŞ BANKASI, İŞ GYO) — all İş companies, no THYAO
    assert set(tickers[:2]) == {"ISCTR", "ISGYO"} and "THYAO" not in tickers
    assert [m.score for m in res] == sorted((m.score for m in res), reverse=True)

    thy = service.resolve_instrument(db, "THY")[0]
    assert thy.ticker == "THYAO" and 0.8 < thy.score < 1.0

    # old ticker: exact only when valid at that date; current ticker reported
    old_now = service.resolve_instrument(db, "OLDKZ", at=date(2026, 1, 1))[0]
    assert old_now.instrument_id == universe["KOZAA"] and old_now.score == 0.7 and old_now.ticker == "KOZAA"
    old_then = service.resolve_instrument(db, "OLDKZ", at=date(2019, 6, 1))[0]
    assert old_then.score == 1.0 and old_then.ticker == "OLDKZ"
    assert service.ticker_for(db, universe["KOZAA"], date(2019, 6, 1)) == "OLDKZ"
    assert service.ticker_for(db, universe["KOZAA"]) == "KOZAA"
    assert service.resolve_instrument(db, "") == []
    assert service.resolve_instrument(db, "ZZZZZZ") == []
    # fuzzy (difflib) with a typo
    fuzzy = service.resolve_instrument(db, "turk hawa yollari")
    assert fuzzy[0].ticker == "THYAO" and fuzzy[0].score < 0.75


# ------------------------------------------------------------------------------------------- validation


def test_validate_proposal_warnings(db: Connection, settings) -> None:
    iid = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    today = ist_date()
    _buy(db, iid, "100", "250", today - timedelta(days=10))
    add_close(db, iid, today - timedelta(days=1), 250.0)

    ok = service.validate_proposal(
        db, ProposedTxn(ticker="thyao", type="buy", quantity=D(10), price=D("260"), trade_date=today)
    )
    assert ok.warnings == [] and ok.instrument_id == iid and ok.ticker == "THYAO"
    assert ok.portfolio_id == service.default_portfolio_id(db)

    far = service.validate_proposal(
        db, ProposedTxn(ticker="THYAO", type="sell", quantity=D(150), price=D("300"), trade_date=today)
    )
    assert any("eldeki adetten" in w and "150" in w and "100" in w for w in far.warnings)
    assert any("son kapanıştan" in w and "%20,0" in w for w in far.warnings)

    fut = service.validate_proposal(
        db, ProposedTxn(ticker="THYAO", type="buy", quantity=D(1), trade_date=today + timedelta(days=3))
    )
    assert any("ileri bir tarih" in w for w in fut.warnings)
    assert "Fiyat belirtilmedi." in fut.warnings

    unknown = service.validate_proposal(
        db, ProposedTxn(ticker="THYA", type="buy", quantity=D(1), price=D(1), trade_date=today)
    )
    assert unknown.instrument_id is None
    assert unknown.warnings[0].startswith("Bilinmeyen hisse kodu: THYA.") and "THYAO" in unknown.warnings[0]

    other = add_instrument(db, "SASA", "SASA POLYESTER")
    no_close = service.validate_proposal(
        db, ProposedTxn(instrument_id=other, type="buy", quantity=D("1.5"), price=D(1), trade_date=today)
    )
    assert no_close.ticker == "SASA"
    assert any("Son kapanış fiyatı yok" in w for w in no_close.warnings)
    assert any("tam sayı değil" in w for w in no_close.warnings)

    # validation keeps caller warnings and does not duplicate on re-run
    again = service.validate_proposal(db, far)
    assert again.warnings == far.warnings


# ------------------------------------------------------------------------------------------- priority


def test_holding_priority_held_and_recently_sold(db: Connection) -> None:
    iid = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    other = add_instrument(db, "SASA", "SASA POLYESTER")
    at = lambda d: datetime(d.year, d.month, d.day, 12, tzinfo=UTC)  # noqa: E731
    assert service.holding_priority(db, [iid]) is None
    assert service.holding_priority(db, []) is None
    _buy(db, iid, "10", "1", date(2026, 5, 1))
    assert service.holding_priority(db, [other, iid], at(date(2026, 5, 1))) == "P1"
    assert service.holding_priority(db, [iid], at(date(2026, 4, 30))) is None  # point in time
    _sell(db, iid, "10", "1", date(2026, 5, 10))
    assert service.holding_priority(db, [iid], at(date(2026, 5, 9))) == "P1"
    assert service.holding_priority(db, [iid], at(date(2026, 5, 20))) == "P2"  # sold 10 days ago
    assert service.holding_priority(db, [iid], at(date(2026, 6, 8))) == "P2"  # day 29
    assert service.holding_priority(db, [iid], at(date(2026, 6, 20))) is None  # > 30 days
    assert service.holding_priority(db, [other], at(date(2026, 5, 20))) is None


def test_holding_priority_watchlists(db: Connection) -> None:
    thy = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.", sector="ULAŞTIRMA")
    asels = add_instrument(db, "ASELS", "ASELSAN", sector="Savunma")
    sasa = add_instrument(db, "SASA", "SASA POLYESTER", sector="KİMYA")
    akbnk = add_instrument(db, "AKBNK", "AKBANK", sector="BANKA")
    add_instrument(db, "XU030", None, itype="index")
    for code, iid, vf, vt in (
        ("XU030", sasa, date(2025, 1, 1), None),
        ("XU030", akbnk, date(2020, 1, 1), date(2024, 12, 31)),
    ):
        db.execute(
            s.index_membership.insert().values(
                id=uuid7(), index_code=code, instrument_id=iid, valid_from=vf, valid_to=vt
            )
        )

    assert service.holding_priority(db, [thy]) is None
    service.watch(db, "THYAO")
    assert service.holding_priority(db, [thy]) == "P2"
    assert service.holding_priority(db, [asels]) is None
    service.watch(db, "sektör:SAVUNMA", watchlist="Savunma")
    assert service.holding_priority(db, [asels]) == "P2"  # sector compare is Turkish-normalised
    assert service.holding_priority(db, [sasa]) is None
    service.watch(db, "XU030")  # index instrument → indices
    assert service.holding_priority(db, [sasa]) == "P2"
    assert service.holding_priority(db, [akbnk]) is None  # membership expired
    assert service.holding_priority(db, [akbnk], datetime(2023, 6, 1, tzinfo=UTC)) == "P2"

    _buy(db, thy, "1", "1", date(2026, 1, 1))
    assert service.holding_priority(db, [thy, sasa]) == "P1"


def test_watch_unwatch_and_listing(db: Connection) -> None:
    thy = add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O.")
    wid = service.watch(db, "thyao")
    assert service.watch(db, "THYAO") == wid  # no duplicate
    service.watch(db, "endeks:xu100")
    service.watch(db, "sector:Bankacılık")
    [wl] = service.watchlists(db)
    assert wl["name"] == "Ana" and wl["instrument_ids"] == [thy] and wl["tickers"] == ["THYAO"]
    assert wl["indices"] == ["XU100"] and wl["sectors"] == ["Bankacılık"]
    with pytest.raises(ValueError, match="Bilinmeyen hisse kodu"):
        service.watch(db, "THYA")
    assert service.unwatch(db, "THYAO") is True
    assert service.unwatch(db, "THYAO") is False
    assert service.unwatch(db, "sektor:BANKACILIK") is True
    assert service.unwatch(db, "THYAO", watchlist="Yok") is False
    assert service.watchlists(db)[0]["instrument_ids"] == []


def test_default_portfolio_created_when_missing(db: Connection) -> None:
    db.execute(s.portfolio.delete())
    pid = service.default_portfolio_id(db)
    row = db.execute(select(s.portfolio).where(s.portfolio.c.id == pid)).one()
    assert row.is_default and row.name == "Ana"
    assert row.created_at <= now_utc()
