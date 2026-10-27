from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, select

from papi.core import db as core_db
from papi.core import registry
from papi.core.ids import uuid7
from papi.core.schema import portfolio, trading_calendar, txn, watchlist
from papi.core.state import get_state
from papi.core.timeutil import IST, now_utc
from papi.sources.prices import service
from tests.prices.conftest import add_instrument

TODAY = date(2026, 10, 27)  # Tuesday


def _txn(conn: Connection, iid: str, type_: str, qty: str, day: date, reverses: str | None = None) -> str:
    pid = conn.execute(select(portfolio.c.id).limit(1)).scalar_one()
    tid = uuid7()
    conn.execute(
        txn.insert().values(
            id=tid,
            portfolio_id=pid,
            instrument_id=iid,
            type=type_,
            trade_date=day,
            quantity=Decimal(qty),
            currency="TRY",
            source="manual",
            reverses_id=reverses,
            created_at=now_utc(),
        )
    )
    return tid


@pytest.mark.parametrize(
    ("hhmm", "weekday_date", "last", "closed", "due"),
    [
        ("18:44", TODAY, None, False, False),
        ("18:45", TODAY, None, False, True),
        ("23:59", TODAY, None, False, True),
        ("19:00", TODAY, TODAY, False, False),
        ("19:00", TODAY, TODAY - timedelta(days=1), False, True),
        ("19:00", TODAY, None, True, False),
        ("09:00", TODAY, None, False, False),
    ],
)
def test_daily_due_grid(hhmm: str, weekday_date: date, last: date | None, closed: bool, due: bool) -> None:
    h, m = map(int, hhmm.split(":"))
    now = datetime(weekday_date.year, weekday_date.month, weekday_date.day, h, m, tzinfo=IST)
    assert service.daily_due(now, "18:45", last, closed) is due


def test_is_closed_day(db: Connection) -> None:
    assert service.is_closed_day(db, date(2026, 10, 31))  # Saturday
    assert not service.is_closed_day(db, date(2026, 10, 27))
    db.execute(
        trading_calendar.insert().values(
            id=uuid7(),
            calendar_id="XIST",
            date=date(2026, 10, 29),
            session_type="closed",
            note="Cumhuriyet Bayramı",
        )
    )
    assert service.is_closed_day(db, date(2026, 10, 29))


def test_daily_selection_holdings_watchlist_index(db: Connection) -> None:
    held = add_instrument(db, "THYAO")
    sold_long_ago = add_instrument(db, "ASELS")
    sold_recently = add_instrument(db, "EREGL")
    reversed_buy = add_instrument(db, "SISE")
    watched = add_instrument(db, "KCHOL")
    bonus = add_instrument(db, "BIMAS")
    _txn(db, held, "buy", "100", date(2025, 1, 2))
    _txn(db, held, "sell", "-40", date(2025, 3, 2))  # sign convention irrelevant: magnitude used
    _txn(db, sold_long_ago, "buy", "10", date(2024, 1, 2))
    _txn(db, sold_long_ago, "sell", "10", date(2024, 2, 2))
    _txn(db, sold_recently, "buy", "10", date(2026, 9, 1))
    _txn(db, sold_recently, "sell", "10", TODAY - timedelta(days=5))
    orig = _txn(db, reversed_buy, "buy", "10", date(2025, 1, 2))
    _txn(db, reversed_buy, "sell", "10", date(2025, 1, 2), reverses=orig)
    _txn(db, bonus, "opening_balance", "5", date(2024, 1, 1))
    _txn(db, bonus, "dividend_cash", "0", date(2024, 6, 1))
    db.execute(
        watchlist.insert().values(
            id=uuid7(), name="Ana", instrument_ids=[watched, held], created_at=now_utc()
        )
    )
    ids = service.daily_instrument_ids(db, TODAY)
    xu = service.instrument_for_ticker(db, "XU100")
    assert xu is not None
    assert set(ids) == {held, sold_recently, bonus, watched, xu}
    assert len(ids) == len(set(ids))


def test_periodic_runs_once_after_fetch_time(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    held = add_instrument(db, "THYAO")
    _txn(db, held, "buy", "10", date(2025, 1, 2))
    db.commit()
    calls: list[tuple[list[str], date, date]] = []

    def fake_sync(ids: list[str], start: date, end: date | None = None, **kw: Any) -> dict:
        calls.append((ids, start, end))
        return {"rows": {"isyatirim_prices": len(ids)}, "errors": []}

    monkeypatch.setattr(service, "sync_prices_isolated", fake_sync)
    from papi.sources.prices import handlers

    clock = {"now": datetime(2026, 10, 27, 18, 0, tzinfo=IST)}
    monkeypatch.setattr(handlers, "now_ist", lambda: clock["now"])
    handlers.daily_prices(None)
    assert calls == []  # before 18:45
    clock["now"] = datetime(2026, 10, 27, 18, 50, tzinfo=IST)
    monkeypatch.setattr(handlers, "now_ist", lambda: clock["now"])
    handlers.daily_prices(None)
    ((ids, start, end),) = calls
    assert held in ids and len(ids) == 2  # + XU100
    assert (start, end) == (date(2026, 10, 17), date(2026, 10, 27))
    handlers.daily_prices(None)
    assert len(calls) == 1  # already done today
    with core_db.connect() as c:
        assert get_state(c, service.DAILY_STATE_KEY) == "2026-10-27"


def test_periodic_retries_when_nothing_stored(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from papi.sources.prices import handlers

    monkeypatch.setattr(handlers, "now_ist", lambda: datetime(2026, 10, 27, 19, 0, tzinfo=IST))
    monkeypatch.setattr(
        service, "sync_prices_isolated", lambda *a, **k: {"rows": {"isyatirim_prices": 0}, "errors": [{}]}
    )
    handlers.daily_prices(None)
    with core_db.connect() as c:
        assert get_state(c, service.DAILY_STATE_KEY) is None


def test_sync_job_resolves_tickers(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from papi.core.jobs import PermanentError
    from papi.sources.prices import handlers

    thy = add_instrument(db, "THYAO")
    db.commit()
    seen: list[Any] = []
    monkeypatch.setattr(service, "sync_prices_isolated", lambda ids, s, e=None, **k: seen.append((ids, s, e)))
    handlers.sync_job({"tickers": ["THYAO", "XU100"], "start": "2026-10-01"}, None)
    ((ids, s, e),) = seen
    assert ids[0] == thy and len(ids) == 2 and s == date(2026, 10, 1) and e is None
    with pytest.raises(PermanentError):
        handlers.sync_job({"start": "nope"}, None)
    with pytest.raises(PermanentError):
        handlers.sync_job({"start": "2026-10-01", "tickers": ["NOPE"]}, None)


def test_registrations() -> None:
    import importlib

    from papi.sources.prices import handlers

    handlers = importlib.reload(handlers)

    assert registry.REGISTRY.periodics["prices:daily"].func is handlers.daily_prices
    assert registry.REGISTRY.job_kinds[handlers.SYNC_JOB_KIND].func is handlers.sync_job
