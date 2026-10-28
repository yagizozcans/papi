from __future__ import annotations

import gzip
import importlib
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from papi.core.metrics import series
from papi.core.schema import instrument, issuer, kv_state
from papi.core.state import get_state
from papi.core.timeutil import parse_iso
from papi.sources.kap import capital
from papi.sources.kap.capital import CapitalSectionMissing, CompanyCapital, parse_company_general
from papi.sources.kap.companies import apply_companies, resolve_tickers

from .conftest import FIXTURES, load_json

NOW = parse_iso("2026-10-28T08:00:00Z")
D0 = date(2026, 10, 28)


def page(name: str) -> bytes:
    return gzip.decompress((FIXTURES / f"company_genel_{name}.html.gz").read_bytes())


def seed(db: Any, codes: tuple[str, ...] = ("FROTO", "ISCTR", "PASEU")) -> None:
    items = [
        c
        for c in load_json("companies_IGS.json.gz")
        if any(code in [s.strip() for s in (c.get("stockCode") or "").split(",")] for code in codes)
    ]
    apply_companies(db, items, today=D0)
    db.commit()


def ids(db: Any, ticker: str) -> tuple[str, str]:
    inst_id, issuer_id = resolve_tickers(db, [ticker], D0)[ticker]
    return inst_id, issuer_id


def iextra(db: Any, ticker: str) -> dict:
    db.rollback()
    return db.execute(select(instrument.c.extra).where(instrument.c.id == ids(db, ticker)[0])).scalar_one()


def isextra(db: Any, ticker: str) -> dict:
    db.rollback()
    return db.execute(select(issuer.c.extra).where(issuer.c.id == ids(db, ticker)[1])).scalar_one()


def test_parse_single_traded_class_with_non_traded_groups() -> None:
    cap = parse_company_general(page("FROTO"))
    assert cap.paid_in_capital == Decimal("3509100000")
    assert cap.registered_capital_ceiling == Decimal("10000000000")
    assert [(g["group"], g["nominal_total"], g["traded"]) for g in cap.share_groups] == [
        ("A", "689257737.7", True),
        ("B", "1379871894.7", False),
        ("C", "1439970367.6", False),
    ]
    ff = cap.free_float["FROTO"]
    assert ff["total_shares"] == Decimal("3509100000.00")  # includes the non-traded B/C groups
    assert ff["traded_shares"] == Decimal("625114841.90")
    assert ff["free_float_pct"] == Decimal("17.48")


def test_parse_multi_class_issuer_per_code() -> None:
    cap = parse_company_general(page("ISCTR"))
    assert cap.paid_in_capital == Decimal("25000000000")
    totals = {k: v["total_shares"] for k, v in cap.free_float.items()}
    assert totals == {
        "ISATR": Decimal("1000.00"),
        "ISKUR": Decimal("2458.00"),
        "ISBTR": Decimal("29000.00"),
        "ISCTR": Decimal("24999970000.00"),
    }
    assert totals["ISATR"] + totals["ISBTR"] + totals["ISCTR"] == cap.paid_in_capital
    assert [g["group"] for g in cap.share_groups] == ["A", "B", "C"]  # KAP serves group C truncated
    assert cap.share_groups[2]["nominal_total"] is None


def test_parse_rejects_page_without_capital_section() -> None:
    with pytest.raises(CapitalSectionMissing):
        parse_company_general(page("bad_956"))


def test_held_issuers_first_then_cached_for_seven_days(db, kap_deps, client, fake_kap) -> None:
    seed(db)
    kap_deps.held = {ids(db, "FROTO")[0]: "P1"}
    assert capital.due_issuers(db, now=NOW, held_only=True) == [ids(db, "FROTO")[1]]
    due = capital.due_issuers(db, now=NOW)
    assert due[0] == ids(db, "FROTO")[1] and len(due) == 3

    counts = capital.refresh_batch(db, client, limit=50, held_only=True, now=NOW)
    assert counts == {"ok": 1, "missing": 0, "no_permalink": 0, "error": 0}
    assert fake_kap.calls == ["filter:FROTO", "genel:956-ford-otomotiv-sanayi-a-s"]
    ex = isextra(db, "FROTO")
    assert ex["paid_in_capital"] == "3509100000" and ex["paid_in_capital_as_of"] == "2026-10-28"
    assert ex["registered_capital_ceiling"] == "10000000000"
    assert ex["kap_permalink"] == "956-ford-otomotiv-sanayi-a-s"
    assert ex["kap"]["paidCapital"] == 350910000  # stale company-list value is not used
    inst = iextra(db, "FROTO")
    assert Decimal(inst["shares_outstanding"]) == Decimal("3509100000")
    assert inst["shares_as_of"] == "2026-10-28" and inst["shares_source"] == "free_float_table"
    assert inst["free_float_pct"] == "17.48"
    st = get_state(db, f"{capital.STATE_PREFIX}{ids(db, 'FROTO')[1]}")
    assert st["status"] == "ok" and st["instruments"] == 1

    assert ids(db, "FROTO")[1] not in capital.due_issuers(db, now=NOW + timedelta(days=6))
    assert ids(db, "FROTO")[1] in capital.due_issuers(db, now=NOW + timedelta(days=8))
    fake_kap.calls.clear()
    capital.refresh_batch(db, client, limit=0, issuer_ids=[ids(db, "FROTO")[1]], now=NOW + timedelta(days=8))
    assert fake_kap.calls == ["genel:956-ford-otomotiv-sanayi-a-s"]  # permaLink cached
    assert iextra(db, "FROTO")["shares_as_of"] == "2026-11-05"


def test_multi_class_shares_per_instrument(db, kap_deps, client) -> None:
    seed(db)
    capital.refresh_batch(db, client, limit=0, issuer_ids=[ids(db, "ISCTR")[1]], now=NOW)
    shares = {t: Decimal(iextra(db, t)["shares_outstanding"]) for t in ("ISATR", "ISBTR", "ISCTR", "ISKUR")}
    assert shares == {
        "ISATR": Decimal("1000"),
        "ISBTR": Decimal("29000"),
        "ISCTR": Decimal("24999970000"),
        "ISKUR": Decimal("2458"),
    }
    assert isextra(db, "ISCTR")["paid_in_capital"] == "25000000000"


def test_single_class_falls_back_to_paid_capital_multi_class_does_not(db, kap_deps) -> None:
    seed(db)
    paseu = CompanyCapital(paid_in_capital=Decimal("672000000"))
    capital.apply_capital(db, ids(db, "PASEU")[1], paseu, as_of=D0, url=None)
    db.commit()
    inst = iextra(db, "PASEU")
    assert inst["shares_outstanding"] == "672000000" and inst["shares_source"] == "paid_in_capital"
    isbank = CompanyCapital(paid_in_capital=Decimal("25000000000"))
    n = capital.apply_capital(db, ids(db, "ISCTR")[1], isbank, as_of=D0, url=None)
    db.commit()
    assert n == 0 and "shares_outstanding" not in iextra(db, "ISCTR")


def test_stale_permalink_is_forgotten_and_retried_next_day(db, kap_deps, client, fake_kap) -> None:
    seed(db)
    _, paseu = ids(db, "PASEU")
    ex = dict(isextra(db, "PASEU"))
    ex["kap_permalink"] = "956"
    db.execute(issuer.update().where(issuer.c.id == paseu).values(extra=ex))
    db.commit()
    counts = capital.refresh_batch(db, client, limit=0, issuer_ids=[paseu], now=NOW)
    assert counts["missing"] == 1
    assert "kap_permalink" not in isextra(db, "PASEU")
    assert series(db, "kap.capital.missing")
    assert paseu not in capital.due_issuers(db, now=NOW + timedelta(hours=12))
    assert paseu in capital.due_issuers(db, now=NOW + timedelta(days=1, minutes=1))
    fake_kap.calls.clear()
    assert (
        capital.refresh_batch(db, client, limit=0, issuer_ids=[paseu], now=NOW + timedelta(days=1))["ok"] == 1
    )
    assert fake_kap.calls == ["filter:PASEU", "genel:5826-pasifik-eurasia-lojistik-dis-ticaret-a-s"]
    assert iextra(db, "PASEU")["shares_outstanding"] == "672000000.00"
    db.rollback()
    keys = db.execute(select(kv_state.c.key).where(kv_state.c.key.like("kap:capital:%"))).scalars().all()
    assert keys == [f"kap:capital:{paseu}"]


def test_capital_periodic_registered(kap_deps) -> None:
    from papi.core.registry import REGISTRY
    from papi.sources.kap import handlers

    importlib.reload(handlers)
    assert REGISTRY.periodics["kap:capital"].seconds(NOW) == 3600
    assert "kap:companies" in REGISTRY.periodics
