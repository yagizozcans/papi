from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import Connection, func, select

from papi.core import metrics
from papi.core.config import reset_settings
from papi.core.schema import identifier, instrument, price_bar, raw_object, source
from papi.sources.prices import isyatirim, last_close, service, sync_prices, sync_prices_isolated, tefas
from papi.sources.prices.base import Throttle
from tests.prices.conftest import add_bar, add_instrument, fixture_bytes

START, END = date(2026, 10, 13), date(2026, 10, 27)
YAHOO_PREFIX = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _mock_sources(yahoo_thyao: bytes | None = None) -> None:
    respx.get(url__startswith=isyatirim.EQUITY_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("isyatirim_thyao_2026-10.json"))
    )
    respx.get(url__startswith=isyatirim.INDEX_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("isyatirim_index_xu100_2026-10.json"))
    )
    respx.get(url__startswith=YAHOO_PREFIX + "THYAO.IS").mock(
        return_value=httpx.Response(200, content=yahoo_thyao or fixture_bytes("yahoo_thyao_2026-10.json"))
    )
    respx.get(url__startswith=YAHOO_PREFIX + "XU100.IS").mock(
        return_value=httpx.Response(200, content=fixture_bytes("yahoo_xu100_2026-10.json"))
    )


def _statuses(conn: Connection, iid: str) -> dict[tuple[date, str], str | None]:
    rows = conn.execute(
        select(price_bar.c.ts, source.c.code, price_bar.c.cross_check)
        .join(source, source.c.id == price_bar.c.source_id)
        .where(price_bar.c.instrument_id == iid)
    ).all()
    return {(r.ts, r.code): r.cross_check for r in rows}


@respx.mock
def test_sync_upserts_and_cross_checks(db: Connection, alerts: list) -> None:
    _mock_sources()
    thy = add_instrument(db, "THYAO")
    xu = service.ensure_index_instrument(db)
    with httpx.Client() as client:
        res = sync_prices(db, [thy, xu], START, END, client=client, throttle=Throttle(0))
    assert res["rows"] == {"isyatirim_prices": 22, "yahoo_prices": 20}
    assert res["errors"] == [] and res["unresolved"] == []
    cc = res["cross_check"]
    assert cc["counts"] == {"ok": 20, "mismatch": 0, "single_source": 2}
    assert {(x["ticker"], x["date"]) for x in cc["missing_secondary"]} == {
        ("THYAO", "2026-10-27"),
        ("XU100", "2026-10-27"),
    }
    st = _statuses(db, thy)
    assert st[(date(2026, 10, 13), "isyatirim_prices")] == "ok"
    assert st[(date(2026, 10, 13), "yahoo_prices")] == "ok"
    assert st[(date(2026, 10, 27), "isyatirim_prices")] == "single_source"
    assert alerts == []  # newest-day gap only, no mismatch
    # raw responses stored with provenance on the bars
    assert db.execute(select(func.count()).select_from(raw_object)).scalar_one() == 4
    shas = set(db.execute(select(price_bar.c.raw_sha256).where(price_bar.c.instrument_id == thy)).scalars())
    assert None not in shas and len(shas) == 2
    assert last_close(db, thy) == (date(2026, 10, 27), 253.0)
    assert metrics.series(db, "prices.cross_check.mismatch")[0]["value"] == 0


@respx.mock
def test_resync_is_idempotent(db: Connection, alerts: list) -> None:
    _mock_sources()
    thy = add_instrument(db, "THYAO")
    for _ in range(2):
        with httpx.Client() as client:
            sync_prices(db, [thy], START, END, client=client, throttle=Throttle(0))
    n = db.execute(
        select(func.count()).select_from(price_bar).where(price_bar.c.instrument_id == thy)
    ).scalar_one()
    assert n == 21


@respx.mock
def test_mismatch_sets_status_metric_and_alert(db: Connection, alerts: list) -> None:
    doc = json.loads(fixture_bytes("yahoo_thyao_2026-10.json"))
    closes = doc["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    closes[1] = 281.0  # 2026-10-14: İş Yatırım 279.25 → 0.63 % off (> 0.5 % tolerance)
    closes[2] = 277.5  # 2026-10-15: 276.75 → 0.27 %, within tolerance
    _mock_sources(json.dumps(doc).encode())
    thy = add_instrument(db, "THYAO")
    with httpx.Client() as client:
        res = sync_prices(db, [thy], START, END, client=client, throttle=Throttle(0))
    cc = res["cross_check"]
    assert cc["counts"] == {"ok": 9, "mismatch": 1, "single_source": 1}
    (m,) = cc["mismatches"]
    assert (m["ticker"], m["date"], m["primary_close"], m["secondary_close"]) == (
        "THYAO",
        "2026-10-14",
        279.25,
        281.0,
    )
    assert m["diff_pct"] == pytest.approx(0.6267, abs=1e-4)
    st = _statuses(db, thy)
    assert st[(date(2026, 10, 14), "isyatirim_prices")] == "mismatch"
    assert st[(date(2026, 10, 14), "yahoo_prices")] == "mismatch"
    assert st[(date(2026, 10, 15), "yahoo_prices")] == "ok"
    assert metrics.series(db, "prices.cross_check.mismatch")[0]["value"] == 1
    (a,) = alerts
    assert "THYAO 2026-10-14" in a["text"] and "279,25 / 281,00" in a["text"]
    assert a["dedupe_key"].startswith("prices:cross_check:")


@respx.mock
def test_source_errors_logged_and_alerted(db: Connection, alerts: list) -> None:
    respx.get(url__startswith=isyatirim.EQUITY_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("isyatirim_thyao_2026-10.json"))
    )
    respx.get(url__startswith=YAHOO_PREFIX).mock(return_value=httpx.Response(500, text="boom"))
    thy = add_instrument(db, "THYAO")
    with httpx.Client() as client:
        res = sync_prices(db, [thy], START, END, client=client, throttle=Throttle(0))
    assert res["rows"] == {"isyatirim_prices": 11, "yahoo_prices": 0}
    assert res["errors"][0]["outcome"] == "http_error"
    assert res["cross_check"]["counts"]["single_source"] == 11
    assert len(alerts) == 1 and "indirme hatası" in alerts[0]["text"]
    from papi.core.schema import fetch_log

    outcomes = sorted(db.execute(select(fetch_log.c.outcome)).scalars())
    assert outcomes == ["http_error", "ok"]


def test_cross_check_stale_gap_alerts(db: Connection, alerts: list) -> None:
    thy = add_instrument(db, "THYAO")
    add_bar(db, thy, date(2026, 10, 20), "isyatirim_prices", 100.0)
    add_bar(db, thy, date(2026, 10, 21), "isyatirim_prices", 101.0)
    add_bar(db, thy, date(2026, 10, 21), "yahoo_prices", 101.2)
    rep = service.cross_check(db, [thy], date(2026, 10, 1), date(2026, 10, 31))
    assert rep.counts == {"ok": 1, "mismatch": 0, "single_source": 1}
    assert rep.missing_secondary == [("THYAO", date(2026, 10, 20))]
    assert service._stale_missing(rep) == [("yahoo_prices", "THYAO", date(2026, 10, 20))]


@pytest.mark.parametrize(
    ("p", "s", "status"),
    [
        (100.0, 100.0, "ok"),
        (100.0, 100.5, "ok"),
        (100.0, 99.5, "ok"),
        (100.0, 100.51, "mismatch"),
        (100.0, 90, "mismatch"),
    ],
)
def test_cross_check_tolerance_grid(db: Connection, p: float, s: float, status: str) -> None:
    thy = add_instrument(db, "THYAO")
    add_bar(db, thy, date(2026, 10, 20), "isyatirim_prices", p)
    add_bar(db, thy, date(2026, 10, 20), "yahoo_prices", s)
    rep = service.cross_check(db, [thy], date(2026, 10, 20), date(2026, 10, 20))
    assert rep.counts[status] == 1
    assert set(_statuses(db, thy).values()) == {status}


def test_single_source_when_secondary_disabled(db: Connection, papi_home, monkeypatch) -> None:
    (papi_home / "config.toml").write_text('[prices]\nsecondary = ""\n')
    reset_settings()
    thy = add_instrument(db, "THYAO")
    add_bar(db, thy, date(2026, 10, 20), "isyatirim_prices", 100.0)
    add_bar(db, thy, date(2026, 10, 20), "yahoo_prices", 120.0)
    rep = service.cross_check(db, [thy], date(2026, 10, 20), date(2026, 10, 20))
    assert rep.counts == {"ok": 0, "mismatch": 0, "single_source": 1}
    assert rep.missing_primary == rep.missing_secondary == []
    assert _statuses(db, thy)[(date(2026, 10, 20), "isyatirim_prices")] == "single_source"


def test_last_close_prefers_primary_on_newest_day(db: Connection) -> None:
    thy = add_instrument(db, "THYAO")
    assert last_close(db, thy) is None
    add_bar(db, thy, date(2026, 10, 20), "yahoo_prices", 99.0)
    add_bar(db, thy, date(2026, 10, 20), "isyatirim_prices", 100.0)
    add_bar(db, thy, date(2026, 10, 21), "yahoo_prices", 102.0)
    assert last_close(db, thy, date(2026, 10, 20)) == (date(2026, 10, 20), 100.0)
    assert last_close(db, thy, date(2026, 10, 21)) == (date(2026, 10, 21), 102.0)  # only secondary has it
    add_bar(db, thy, date(2026, 10, 21), "isyatirim_prices", 101.0)
    assert last_close(db, thy, date(2026, 10, 25)) == (date(2026, 10, 21), 101.0)
    assert last_close(db, thy, date(2026, 10, 19)) is None


def test_ensure_index_instrument_idempotent(db: Connection) -> None:
    a = service.ensure_index_instrument(db, "XU100")
    b = service.ensure_index_instrument(db, "xu100")
    assert a == b
    row = db.execute(select(instrument).where(instrument.c.id == a)).one()
    assert row.type == "index" and row.issuer_id is None and row.extra["name"] == "BIST 100"
    n = db.execute(
        select(func.count()).select_from(identifier).where(identifier.c.value == "XU100")
    ).scalar_one()
    assert n == 1


def test_resolve_tickers(db: Connection) -> None:
    thy = add_instrument(db, "THYAO")
    found, unknown = service.resolve_tickers(db, ["thyao", " XU030 ", "NOPE", ""])
    assert found["THYAO"] == thy and "XU030" in found and unknown == ["NOPE"]
    found, unknown = service.resolve_tickers(db, ["XBANK"], create_indices=False)
    assert found == {} and unknown == ["XBANK"]


def test_unresolved_instruments_reported(db: Connection, alerts: list) -> None:
    res = sync_prices(db, ["no-such-id"], START, END, client=httpx.Client(), throttle=Throttle(0))
    assert res["unresolved"] == ["no-such-id"] and res["instruments"] == []


@respx.mock
def test_sync_prices_isolated_commits(db: Connection, alerts: list) -> None:
    _mock_sources()
    thy = add_instrument(db, "THYAO")
    db.commit()
    with httpx.Client() as client:
        res = sync_prices_isolated([thy], START, END, client=client, throttle=Throttle(0))
    assert res["rows"]["isyatirim_prices"] == 11
    db.rollback()
    assert last_close(db, thy) == (date(2026, 10, 27), 253.0)


def test_alert_failure_keeps_data(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("notify down")

    monkeypatch.setattr(service, "_ops_alert", boom)
    thy = add_instrument(db, "THYAO")
    add_bar(db, thy, date(2026, 10, 20), "isyatirim_prices", 100.0)
    add_bar(db, thy, date(2026, 10, 20), "yahoo_prices", 110.0)
    res = service.store(db, [service.Target(thy, "THYAO", False)], [], date(2026, 10, 20), date(2026, 10, 20))
    assert res["cross_check"]["counts"]["mismatch"] == 1
    assert metrics.series(db, "prices.alert_failed")[0]["value"] == 1


@respx.mock
def test_fund_priced_from_tefas_only_and_not_cross_checked(db: Connection, alerts: list) -> None:
    _mock_sources()
    tefas_route = respx.post(tefas.PRICE_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("tefas_ti1_2026-10.json"))
    )
    thy = add_instrument(db, "THYAO")
    ti1 = service.ensure_fund_instrument(db, "TI1", "İŞ PORTFÖY PARA PİYASASI (TL) FONU")
    with httpx.Client() as client:
        res = sync_prices(db, [thy, ti1], START, END, client=client, throttle=Throttle(0))
    assert tefas_route.call_count == 1
    assert json.loads(tefas_route.calls[0].request.content)["fonKodu"] == "TI1"
    # BIST sources were asked for THYAO only
    asked = [str(c.request.url) for c in respx.calls if c.request.method == "GET"]
    assert asked and not any("TI1" in u for u in asked)
    assert res["rows"] == {"isyatirim_prices": 11, "yahoo_prices": 10, "tefas_prices": 11}
    assert res["errors"] == []
    assert res["cross_check"]["counts"] == {"ok": 10, "mismatch": 0, "single_source": 1}
    assert set(_statuses(db, ti1).values()) == {None}
    assert alerts == []
    assert last_close(db, ti1) == (date(2026, 10, 27), 1779.66176)


def test_ensure_fund_refuses_existing_equity_code(db: Connection) -> None:
    add_instrument(db, "THYAO")
    with pytest.raises(ValueError):
        service.ensure_fund_instrument(db, "THYAO", "x")
