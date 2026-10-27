from __future__ import annotations

from datetime import date, timedelta

import pytest

from papi.core.ids import uuid7
from papi.core.schema import event
from papi.core.timeutil import now_utc, to_utc
from papi.triage import materiality as M
from papi.triage import priority as PR
from papi.triage import taxonomy

from .helpers import seed_company, seed_holding, seed_price, seed_watch

AT = to_utc("2026-10-27T15:30:00+03:00")


@pytest.fixture
def tax():
    return taxonomy.load()


def _compute(db, tax, code, **kw):
    args = dict(
        payload=None, issuer_ids=[], instrument_ids=[], announced_at=AT, is_late=False,
        revision_kind="original", is_catchup=False,
    )  # fmt: skip
    args.update(kw)
    return M.compute(db, tax, code, **args)


def test_bands():
    assert [M.band_for(s) for s in (0, 29, 30, 59, 60, 79, 80, 100)] == [
        "low", "low", "medium", "medium", "high", "high", "critical", "critical",
    ]  # fmt: skip


def test_prior_only_and_flags(db, tax):
    m = _compute(db, tax, "governance.board.change")
    assert (m.score, m.size, m.novelty, m.flags) == (30, 1.0, 1.0, 1.0)  # no issuers → no novelty
    m = _compute(db, tax, "governance.board.change", is_late=True)
    assert m.score == 33 and m.flag_names == ("late",)
    m = _compute(db, tax, "governance.board.change", revision_kind="correction", is_catchup=True)
    assert m.score == 24 and m.catchup is True and m.flag_names == ("correction",)


def test_novelty_first_of_family_in_365_days(db, tax):
    iss, inst = seed_company(db, "NOVA")
    m = _compute(db, tax, "business.contract.award", issuer_ids=[iss], instrument_ids=[inst])
    assert m.novelty == M.NOVELTY_MULT
    assert m.score == round(50 * 1.15)
    db.execute(
        event.insert().values(
            id=uuid7(), disclosure_ids=[], code="business.capex", taxonomy_version="t", issuer_ids=[iss],
            announced_at=AT - timedelta(days=100), status="provisional", created_at=now_utc(),
        )
    )  # fmt: skip
    m = _compute(db, tax, "business.contract.award", issuer_ids=[iss], instrument_ids=[inst])
    assert m.novelty == 1.0  # same family (business) within a year
    m = _compute(db, tax, "business.contract.award", issuer_ids=[iss], announced_at=AT + timedelta(days=300))
    assert m.novelty == M.NOVELTY_MULT  # the earlier event is now > 365 days old
    # recurring codes never get the novelty boost
    assert _compute(db, tax, "report.financial.periodic", issuer_ids=[iss]).novelty == 1.0


def test_size_signal_value_to_market_cap(db, tax):
    iss, inst = seed_company(db, "BIGC", shares=1_000_000)
    seed_price(db, inst, date(2026, 10, 26), 100.0)  # market cap 100 M TRY
    payload = {"value": 20_000_000.0, "currency": "TRY"}
    m = _compute(db, tax, "business.contract.award", payload=payload, issuer_ids=[iss], instrument_ids=[inst])
    assert m.size == 1.5 and m.size_basis == "value/mcap=0.20000"
    assert m.score == 86 and m.band == "critical"  # 50 × 1.5 × 1.15
    small = {"value": 50_000.0, "currency": "TRY"}
    assert _compute(db, tax, "business.contract.award", payload=small, instrument_ids=[inst]).size == 0.6
    usd = {"value": 20_000_000.0, "currency": "USD"}
    assert _compute(db, tax, "business.contract.award", payload=usd, instrument_ids=[inst]).size == 1.0
    # price after the announcement is not used
    _, inst2 = seed_company(db, "LATE", shares=1_000)
    seed_price(db, inst2, date(2026, 10, 28), 10.0)
    assert _compute(db, tax, "business.contract.award", payload=payload, instrument_ids=[inst2]).size == 1.0


def test_size_signal_yield_rate_and_nominal(db, tax):
    _, inst = seed_company(db, "DIVY", shares=10_000_000)
    seed_price(db, inst, date(2026, 10, 24), 50.0)
    dividend = {"gross_per_share": 4.0, "currency": "TRY"}
    m = _compute(db, tax, "distribution.dividend.cash", payload=dividend, instrument_ids=[inst])
    assert m.size == 1.4 and m.size_basis == "yield=0.0800"
    m = _compute(db, tax, "capital.increase.bonus", payload={"bonus_rate_pct": 400.0})
    assert m.size == 1.5 and m.size_basis == "rate=400%"
    # insider trade without a money value: nominal × last close vs market cap (500 M)
    trade = {"total_shares_nominal": 600_000.0, "currency": "TRY"}
    m = _compute(db, tax, "ownership.insider_trade", payload=trade, instrument_ids=[inst])
    assert m.size == 1.3  # 30 M / 500 M = 6 % → [5 %, 10 %)
    # capital-event payload supplies the share count when the issuer has none
    _, inst3 = seed_company(db, "CAPX")
    seed_price(db, inst3, date(2026, 10, 24), 2.0)
    assert M.market_cap(db, inst3, AT, {"current_capital": 1000.0}) == (2000.0, 2.0)
    assert M.market_cap(db, inst3, AT) == (None, 2.0)


def test_priority_rules_with_monkeypatched_holdings(db, monkeypatch, settings):
    calls = []

    def fake(conn, ids):
        calls.append(ids)
        return {"held": "P1", "watched": "P2"}.get(ids[0]) if ids else None

    monkeypatch.setattr(PR, "holding_priority", fake)
    assert PR.decide(db, "business.capex", ["held"], 10).label == "P1"
    assert PR.decide(db, "business.capex", ["watched"], 10).label == "P2"
    d = PR.decide(db, "market.measure", ["other"], 10)
    assert (d.label, d.reason) == ("P3", "always_tell")
    d = PR.decide(db, "business.capex", ["other"], settings.triage.p3_materiality_threshold)
    assert (d.label, d.reason) == ("P3", "materiality")
    assert PR.decide(db, "business.capex", ["other"], 59).label == "P4"
    assert PR.decide(db, "business.capex", [], 10).label == "P4"
    assert [] not in calls  # no lookup without instruments
    assert PR.always_tell("market.measure.short_sale_ban", ["market.measure"])
    assert not PR.always_tell("market.measurement", ["market.measure"])


def test_priority_from_seeded_txn_and_watchlist_rows(db):
    pytest.importorskip("papi.portfolio.service")
    _, held = seed_company(db, "HELD")
    _, watched = seed_company(db, "WTCH")
    _, other = seed_company(db, "OTHR")
    seed_holding(db, held)
    seed_watch(db, [watched])
    assert PR.decide(db, "business.capex", [held], 0).label == "P1"
    assert PR.decide(db, "business.capex", [other, held], 0).label == "P1"
    assert PR.decide(db, "business.capex", [watched], 0).label == "P2"
    assert PR.decide(db, "business.capex", [other], 0).label == "P4"
