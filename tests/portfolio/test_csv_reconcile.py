from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Connection, func, select

from papi.core import schema as s
from papi.core.events import PORTFOLIO_CHANGED
from papi.core.timeutil import ist_date
from papi.portfolio import csv_import, service
from papi.portfolio.service import ProposedTxn
from tests.portfolio.conftest import add_instrument

D = Decimal

CSV_TR = """hisse;tür;adet;fiyat;tarih;komisyon;not
THYAO;AÇILIŞ;300;279,10;02.01.2026;0;İşCep açılış
THYAO;al;100;285,40;01.10.2026;4,20;
ASELS;buy;1.000;62,5;2026-09-01;;
ASELS;SAT;400;70;2026-10-02;1,5;kısmi satış
"""


@pytest.fixture
def two(db: Connection) -> dict[str, str]:
    return {
        "THYAO": add_instrument(db, "THYAO", "TÜRK HAVA YOLLARI A.O."),
        "ASELS": add_instrument(db, "ASELS", "ASELSAN ELEKTRONİK SANAYİ VE TİCARET A.Ş."),
    }


def _count(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(s.txn)).scalar_one()


def _changed_events(conn: Connection) -> list[dict]:
    return [
        r.data
        for r in conn.execute(select(s.event_log.c.data).where(s.event_log.c.type == PORTFOLIO_CHANGED))
    ]


def test_import_turkish_csv_is_idempotent(db: Connection, two: dict[str, str]) -> None:
    preview = csv_import.import_csv(db, CSV_TR, dry_run=True)
    assert preview.ok and preview.count("new") == 4 and _count(db) == 0 and preview.inserted == []
    # the sell sees the earlier buy from the same file
    assert not any("eldeki adetten" in w for r in preview.rows for w in r.warnings)

    res = csv_import.import_csv(db, CSV_TR)
    assert res.ok and len(res.inserted) == 4 and _count(db) == 4
    pos = {p.ticker: p for p in service.positions(db, as_of=date(2026, 10, 31))}
    assert pos["THYAO"].quantity == 400
    assert pos["THYAO"].cost_total == D("300") * D("279.10") + D("100") * D("285.40") + D("4.20")
    assert pos["ASELS"].quantity == 600 and pos["ASELS"].avg_cost == D("62.5")
    rows = db.execute(select(s.txn).order_by(s.txn.c.trade_date)).all()
    assert {r.source for r in rows} == {"csv_template"}
    assert all(r.dedupe_key.startswith("csv:") for r in rows)
    assert rows[0].note == "İşCep açılış" and rows[0].type == "opening_balance"
    [event] = _changed_events(db)
    assert event["reason"] == "import" and set(event["transaction_ids"]) == set(res.inserted)

    again = csv_import.import_csv(db, CSV_TR)
    assert again.count("duplicate") == 4 and again.inserted == [] and _count(db) == 4
    assert len(_changed_events(db)) == 1


def test_identical_rows_in_one_file_are_both_kept(db: Connection, two: dict[str, str]) -> None:
    row = "THYAO,buy,10,100,2026-01-02,,\n"
    text = "ticker,type,quantity,price,date,fees,note\n" + row + row
    assert len(csv_import.import_csv(db, text).inserted) == 2
    assert csv_import.import_csv(db, text).count("duplicate") == 2
    more = text + "THYAO,buy,10,100,2026-01-02,,\n"
    res = csv_import.import_csv(db, more)
    assert res.count("duplicate") == 2 and len(res.inserted) == 1 and _count(db) == 3


def test_import_errors_abort_everything(db: Connection, two: dict[str, str]) -> None:
    bad = (
        "ticker,type,quantity,price,date,fees,note\n"
        "THYAO,buy,10,100,2026-01-02,,\n"
        "NOPE,buy,10,100,2026-01-02,,\n"
        "ASELS,yanlış,10,100,2026-01-02,,\n"
        "ASELS,buy,abc,100,2026-13-02,,\n"
        "ASELS,buy,5,,2026-01-02,,\n"
    )
    res = csv_import.import_csv(db, bad)
    assert not res.ok and res.inserted == [] and _count(db) == 0 and _changed_events(db) == []
    by_line = {r.line: r for r in res.rows}
    assert by_line[2].status == "new"
    assert "Bilinmeyen hisse kodu: NOPE" in by_line[3].errors[0]
    assert "bilinmeyen işlem türü" in by_line[4].errors[0]
    assert len(by_line[5].errors) == 2
    assert by_line[6].status == "error" and "fiyat gerekli" in by_line[6].errors[0]


def test_headerless_and_comma_csv_with_quoted_decimals(db: Connection, two: dict[str, str]) -> None:
    text = '# yorum satırı\nTHYAO,buy,100,"285,40",2026-10-01,"4,20",not\n'
    res = csv_import.import_csv(db, text)
    assert res.ok and len(res.inserted) == 1
    row = db.execute(select(s.txn)).one()
    assert row.price == D("285.40") and row.fees == D("4.20") and row.note == "not"
    assert csv_import.TEMPLATE.splitlines()[0] == "ticker,type,quantity,price,date,fees,note"
    with pytest.raises(ValueError, match="bilinmeyen sütun"):
        csv_import.read_rows("ticker,type,foo\nTHYAO,buy,1\n")


def test_template_imports_cleanly(db: Connection, two: dict[str, str]) -> None:
    res = csv_import.import_csv(db, csv_import.TEMPLATE, dry_run=True)
    assert res.ok and res.count("new") == 4
    assert not any("eldeki adetten" in w for r in res.rows for w in r.warnings)


def test_parse_quantities_formats() -> None:
    assert csv_import.parse_quantities("ticker,quantity\nTHYAO,300\nasels;1.000\n".replace(";", ",")) == {
        "THYAO": D(300),
        "ASELS": D(1000),
    }
    assert csv_import.parse_quantities("hisse;adet\nTHYAO;300\nSASA;12,5\n") == {
        "THYAO": D(300),
        "SASA": D("12.5"),
    }
    assert csv_import.parse_quantities("THYAO 300\nisctr 50\n") == {"THYAO": D(300), "ISCTR": D(50)}
    with pytest.raises(ValueError, match="birden fazla"):
        csv_import.parse_quantities("THYAO 1\nTHYAO 2\n")
    with pytest.raises(ValueError, match="adet okunamadı"):
        csv_import.parse_quantities("THYAO,x\n")


def test_reconcile_diffs_and_apply(db: Connection, two: dict[str, str]) -> None:
    today = ist_date()
    for t, q in (("THYAO", "300"), ("ASELS", "100")):
        service.record_transaction(
            db, ProposedTxn(ticker=t, type="buy", quantity=D(q), price=D(10), trade_date=date(2026, 1, 2))
        )
    sasa = add_instrument(db, "SASA", "SASA POLYESTER")
    observed = {"thyao": D(300), "ASELS": D(150), "SASA": D(20), "NOPE": D(5)}
    diffs = service.reconcile(db, observed)
    assert [(d.ticker, d.papi_quantity, d.observed_quantity, d.delta) for d in diffs] == [
        ("ASELS", 100, 150, 50),
        ("NOPE", 0, 5, 5),
        ("SASA", 0, 20, 20),
    ]
    assert diffs[1].instrument_id is None and "Bilinmeyen" in diffs[1].hint
    assert "kaydedilmemiş alış" in diffs[0].hint

    ids = service.apply_reconcile(db, diffs)
    assert len(ids) == 2
    adj = db.execute(select(s.txn).where(s.txn.c.instrument_id == sasa)).one()
    assert adj.type == "reconcile_adjustment" and adj.quantity == 20 and adj.source == "reconcile"
    assert adj.trade_date == today and "İşCep" in adj.note
    assert _changed_events(db)[-1]["reason"] == "reconcile"
    asels = {p.ticker: p for p in service.positions(db)}["ASELS"]
    assert asels.quantity == 150 and asels.avg_cost == D(10)  # added at average cost

    assert service.reconcile(db, {"THYAO": D(300), "ASELS": D(150), "SASA": D(20)}) == []
    # holdings missing from the observed list count as zero at İşCep
    [gone] = service.reconcile(db, {"THYAO": D(300), "ASELS": D(150)})
    assert gone.ticker == "SASA" and gone.delta == -20 and "kaydedilmemiş satış" in gone.hint
    service.apply_reconcile(db, [gone])
    assert "SASA" not in {p.ticker for p in service.positions(db)}
