from __future__ import annotations

import copy
from datetime import date

from sqlalchemy import func, select

from papi.core.schema import OPEN_START, identifier, instrument, issuer, listing
from papi.sources.kap import companies
from papi.sources.kap.companies import apply_companies, resolve_member_oid, resolve_name, resolve_tickers

from .conftest import load_json

D0 = date(2026, 10, 28)
D1 = date(2026, 11, 3)


def _company(items: list[dict], ticker_substr: str) -> dict:
    return next(i for i in items if ticker_substr in (i.get("stockCode") or ""))


def test_split_codes() -> None:
    assert companies.split_codes("ISATR, ISBTR, ISCTR, ISKUR, TIB") == (
        ["ISATR", "ISBTR", "ISCTR", "ISKUR"],
        ["TIB"],
    )
    assert companies.split_codes("AKM, AKMEN") == (["AKMEN"], ["AKM"])
    assert companies.split_codes("A1CAP, ACP") == (["A1CAP"], ["ACP"])
    assert companies.split_codes(None) == ([], [])


def test_sync_creates_issuers_instruments_identifiers(db, kap_deps, client) -> None:
    report = companies.sync_companies(db, client, today=D0)
    db.commit()
    assert report.companies == 756
    assert report.issuers_created == 756
    assert report.instruments_created >= 750
    isbank = db.execute(select(issuer).where(issuer.c.legal_name == "TÜRKİYE İŞ BANKASI A.Ş.")).one()
    assert isbank.kind == "company" and isbank.short_name == "TÜRKİYE İŞ BANKASI"
    assert isbank.extra["kap"]["mkkMemberOid"] == "4028e4a140f2ed7201411682b0cb05c6"
    assert isbank.extra["kap_member_codes"] == ["TIB"]
    res = resolve_tickers(db, ["ISCTR", "ISATR", "ISKUR", "TIB", "NOPE"], D0)
    assert set(res) == {"ISCTR", "ISATR", "ISKUR"}
    assert {v[1] for v in res.values()} == {isbank.id}
    assert len({v[0] for v in res.values()}) == 3  # one instrument per share class
    assert resolve_member_oid(db, "4028e4a140f2ed7201411682b0cb05c6", D0) == isbank.id
    schemes = (
        db.execute(
            select(identifier.c.scheme)
            .where(identifier.c.entity_id == isbank.id)
            .order_by(identifier.c.scheme)
        )
        .scalars()
        .all()
    )
    assert schemes == ["KAP_COMPANY_OID", "KAP_MEMBER_OID", "VKN"]
    tick = db.execute(
        select(identifier).where(identifier.c.scheme == "TICKER", identifier.c.value == "ISCTR")
    ).one()
    assert tick.valid_from == OPEN_START and tick.valid_to is None
    inst = db.execute(select(instrument).where(instrument.c.id == tick.entity_id)).one()
    assert inst.type == "equity" and inst.currency == "TRY"
    assert (
        db.execute(
            select(func.count()).select_from(listing).where(listing.c.instrument_id == inst.id)
        ).scalar()
        == 1
    )


def test_sync_is_idempotent(db, kap_deps, client) -> None:
    companies.sync_companies(db, client, today=D0)
    db.commit()
    counts = [
        db.execute(select(func.count()).select_from(t)).scalar() for t in (issuer, instrument, identifier)
    ]
    report = companies.sync_companies(db, client, today=D0)
    db.commit()
    assert report.issuers_created == report.instruments_created == report.tickers_added == 0
    assert report.issuers_updated == 0
    assert [
        db.execute(select(func.count()).select_from(t)).scalar() for t in (issuer, instrument, identifier)
    ] == counts


def test_ticker_change_is_bitemporal(db, kap_deps) -> None:
    items = load_json("companies_IGS.json.gz")
    apply_companies(db, items, today=D0)
    old = resolve_tickers(db, ["PASEU"], D0)["PASEU"]
    renamed = copy.deepcopy(items)
    target = _company(renamed, "PASEU")
    target["stockCode"] = "PSEUX"
    target["kapMemberTitle"] = "PASİFİK EURASİA LOJİSTİK A.Ş."
    report = apply_companies(db, renamed, today=D1)
    assert report.renames == 1
    assert resolve_tickers(db, ["PASEU"], D0)["PASEU"] == old  # history still resolves
    assert "PASEU" not in resolve_tickers(db, ["PASEU"], D1)
    assert resolve_tickers(db, ["PSEUX"], D1)["PSEUX"] == old  # same instrument, new code
    assert "PSEUX" not in resolve_tickers(db, ["PSEUX"], D0)
    iss = db.execute(select(issuer).where(issuer.c.id == old[1])).one()
    assert iss.legal_name == "PASİFİK EURASİA LOJİSTİK A.Ş."
    assert iss.extra["name_history"][0]["name"] == "PASİFİK EURASİA LOJİSTİK DIŞ TİCARET A.Ş."
    inst = db.execute(select(instrument.c.extra).where(instrument.c.id == old[0])).scalar_one()
    assert inst["ticker_history"] == [{"ticker": "PASEU", "until": D1.isoformat()}]


def test_delisting_and_short_list_guard(db, kap_deps) -> None:
    items = load_json("companies_IGS.json.gz")
    apply_companies(db, items, today=D0)
    gone = _company(items, "PASEU")
    rest = [i for i in items if i is not gone]
    report = apply_companies(db, rest, today=D1)
    assert report.delisted == 1
    iss = db.execute(
        select(issuer.c.status).where(issuer.c.legal_name == gone["kapMemberTitle"])
    ).scalar_one()
    assert iss == "delisted"
    assert "PASEU" not in resolve_tickers(db, ["PASEU"], D1)
    # A truncated response must not mass-delist.
    report = apply_companies(db, rest[:100], today=D1)
    assert report.delisted == 0
    assert any(a["dedupe_key"] == "kap:companies:short" for a in kap_deps.alerts)


def test_name_resolution_threshold(db, kap_deps) -> None:
    apply_companies(db, load_json("companies_IGS.json.gz"), today=D0)
    exact = resolve_name(db, "Türkiye İş Bankası A.Ş.")
    assert exact is not None
    assert resolve_name(db, "TURKIYE IS BANKASI AS") == exact
    assert resolve_name(db, "Bambaşka Bir Şirket Holding") is None
