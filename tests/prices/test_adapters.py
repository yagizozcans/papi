from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from papi.sources.prices import isyatirim, yahoo
from papi.sources.prices.base import Throttle
from tests.prices.conftest import fixture_bytes


def test_isyatirim_equity_parses_raw_fields() -> None:
    bars = isyatirim.parse_equity(fixture_bytes("isyatirim_thyao_2026-10.json"))
    assert len(bars) == 11
    first = bars[0]
    assert first.ts == date(2026, 10, 13)
    assert (first.close, first.high, first.low) == (286.25, 287.25, 284.75)
    assert first.value_traded == pytest.approx(5372546660.0)
    assert first.open is None and first.volume is None
    assert bars[-1].ts == date(2026, 10, 27) and bars[-1].close == 253.0


def test_isyatirim_equity_uses_unadjusted_hg_fields() -> None:
    # ASELS %100 bedelsiz, ex 2023-08-25: HG_KAPANIS raw 73.85 vs HGDG_KAPANIS adjusted 36.75
    bars = {b.ts: b for b in isyatirim.parse_equity(fixture_bytes("isyatirim_asels_2023-08_bonus.json"))}
    assert bars[date(2023, 8, 24)].close == 74.0
    assert bars[date(2023, 8, 25)].close == 37.78


def test_isyatirim_equity_unknown_ticker_is_empty() -> None:
    assert isyatirim.parse_equity(fixture_bytes("isyatirim_unknown.json")) == []


def test_isyatirim_equity_error_response_raises() -> None:
    with pytest.raises(ValueError):
        isyatirim.parse_equity(b'{"ok": false, "errorDescription": "x", "value": null}')


def test_isyatirim_index_dates_are_istanbul_sessions() -> None:
    bars = isyatirim.parse_index(fixture_bytes("isyatirim_index_xu100_2026-10.json"))
    assert [b.ts for b in bars][:2] == [date(2026, 10, 13), date(2026, 10, 14)]
    assert bars[0].close == pytest.approx(12250.67969)
    assert bars[-1].ts == date(2026, 10, 27)
    assert all(b.ts.weekday() < 5 for b in bars)


def test_yahoo_skips_null_close_and_keeps_ohlcv() -> None:
    bars = yahoo.parse_chart(fixture_bytes("yahoo_thyao_2026-10.json"))
    assert len(bars) == 10  # 2026-10-27 still had close=null
    b = bars[0]
    assert b.ts == date(2026, 10, 13)
    assert (b.open, b.high, b.low, b.close, b.volume) == (286.25, 287.25, 284.75, 286.25, 18782604)
    assert bars[-1].ts == date(2026, 10, 26)


def test_yahoo_index() -> None:
    bars = yahoo.parse_chart(fixture_bytes("yahoo_xu100_2026-10.json"))
    assert bars[0].close == pytest.approx(12250.7001953125)


def test_yahoo_unadjusts_splits_to_raw() -> None:
    bars = {b.ts: b for b in yahoo.parse_chart(fixture_bytes("yahoo_asels_2023-08_split.json"))}
    # matches İş Yatırım's raw closes (73.85 before the 200:100 split, 37.78 on the ex day)
    assert bars[date(2023, 8, 21)].close == pytest.approx(73.85, abs=1e-4)
    assert bars[date(2023, 8, 24)].close == pytest.approx(74.0, abs=1e-4)
    assert bars[date(2023, 8, 25)].close == pytest.approx(37.78, abs=1e-4)
    assert bars[date(2023, 8, 21)].high > bars[date(2023, 8, 25)].high


def test_yahoo_window_filter() -> None:
    bars = yahoo.parse_chart(
        fixture_bytes("yahoo_asels_2023-08_split.json"), date(2023, 8, 22), date(2023, 8, 24)
    )
    assert [b.ts for b in bars] == [date(2023, 8, 22), date(2023, 8, 23), date(2023, 8, 24)]


def test_yahoo_error_raises() -> None:
    with pytest.raises(ValueError):
        yahoo.parse_chart(fixture_bytes("yahoo_unknown_404.json"))


@respx.mock
def test_isyatirim_fetch_chunks_and_params() -> None:
    route = respx.get(url__startswith=isyatirim.EQUITY_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("isyatirim_thyao_2026-10.json"))
    )
    with httpx.Client() as client:
        out = isyatirim.fetch(
            client, Throttle(0), "THYAO", is_index=False, start=date(2024, 1, 1), end=date(2026, 10, 27)
        )
    assert len(out) == 3 and route.call_count == 3
    q = route.calls[0].request.url.params
    assert (q["hisse"], q["startdate"], q["enddate"]) == ("THYAO", "01-01-2024", "31-12-2024")
    assert route.calls[2].request.url.params["enddate"] == "27-10-2026"
    assert all(f.ok for f in out)


@respx.mock
def test_isyatirim_index_fetch_params() -> None:
    route = respx.get(url__startswith=isyatirim.INDEX_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("isyatirim_index_xu100_2026-10.json"))
    )
    with httpx.Client() as client:
        (f,) = isyatirim.fetch(
            client, Throttle(0), "XU100", is_index=True, start=date(2026, 10, 14), end=date(2026, 10, 20)
        )
    q = route.calls[0].request.url.params
    assert (q["endeks"], q["from"], q["to"], q["period"]) == (
        "XU100",
        "20261014000000",
        "20261020235959",
        "1440",
    )
    assert [b.ts.day for b in f.bars] == [14, 15, 16, 19, 20]


@respx.mock
def test_yahoo_fetch_http_error_captured() -> None:
    respx.get(url__startswith="https://query1.finance.yahoo.com/v8/finance/chart/ZZZZQ.IS").mock(
        return_value=httpx.Response(404, content=fixture_bytes("yahoo_unknown_404.json"))
    )
    with httpx.Client() as client:
        (f,) = yahoo.fetch(
            client, Throttle(0), "ZZZZQ", is_index=False, start=date(2026, 10, 1), end=date(2026, 10, 2)
        )
    assert f.outcome == "http_error" and f.http_status == 404 and f.body is None and not f.bars


@respx.mock
def test_transport_error_captured() -> None:
    respx.get(url__startswith=isyatirim.EQUITY_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with httpx.Client() as client:
        (f,) = isyatirim.fetch(
            client, Throttle(0), "THYAO", is_index=False, start=date(2026, 10, 1), end=date(2026, 10, 2)
        )
    assert f.outcome == "timeout" and f.http_status is None


def test_throttle_spaces_requests() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    t = Throttle(1.0, clock=lambda: now[0], sleep=sleep)
    t.wait()
    now[0] += 0.3
    t.wait()
    now[0] += 2.0
    t.wait()
    assert slept == [pytest.approx(0.7)]


@respx.mock
def test_transient_failure_retried_once() -> None:
    route = respx.get(url__startswith=isyatirim.INDEX_URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, content=fixture_bytes("isyatirim_index_xu100_2026-10.json")),
        ]
    )
    with httpx.Client() as client:
        (f,) = isyatirim.fetch(
            client, Throttle(0), "XU100", is_index=True, start=date(2026, 10, 13), end=date(2026, 10, 27)
        )
    assert route.call_count == 2 and f.ok and len(f.bars) == 11 and f.request_meta["attempt"] == 2


@respx.mock
def test_client_errors_not_retried() -> None:
    route = respx.get(url__startswith=yahoo.CHART_URL.format(symbol="ZZZZQ.IS")).mock(
        return_value=httpx.Response(404, content=fixture_bytes("yahoo_unknown_404.json"))
    )
    with httpx.Client() as client:
        yahoo.fetch(
            client, Throttle(0), "ZZZZQ", is_index=False, start=date(2026, 10, 1), end=date(2026, 10, 2)
        )
    assert route.call_count == 1
