from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from papi.core.schema import fetch_log, raw_object
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc
from papi.sources.kap.client import (
    CIRCUIT_KEY,
    LIST_CAP,
    KapBlocked,
    KapNotFound,
    KapPublicWebClient,
    split_window,
    unwrap_java_bytes,
)

from .conftest import BASE, FIXTURES


def test_unwrap_java_serialised_byte_array() -> None:
    raw = (FIXTURES / "file_4028328ca16c7de401a17de5e8772c7f.bin").read_bytes()
    assert raw.startswith(b"\xac\xed\x00\x05ur\x00\x02[B")
    pdf = unwrap_java_bytes(raw)
    assert pdf.startswith(b"%PDF-1.4")
    assert len(pdf) == int.from_bytes(raw[23:27], "big") == len(raw) - 27
    assert pdf.rstrip().endswith(b"%%EOF")


def test_unwrap_passes_raw_pdf_through_and_rejects_truncation() -> None:
    plain = (FIXTURES / "bildirimpdf_1688616.pdf").read_bytes()
    assert unwrap_java_bytes(plain) is plain
    raw = (FIXTURES / "file_4028328ca16c7de401a17de5e8772c7f.bin").read_bytes()
    with pytest.raises(ValueError, match="truncated"):
        unwrap_java_bytes(raw[:-10])


def test_split_window() -> None:
    assert split_window(date(2026, 10, 1), date(2026, 10, 1)) is None
    assert split_window(date(2026, 10, 1), date(2026, 10, 4)) == (
        (date(2026, 10, 1), date(2026, 10, 2)),
        (date(2026, 10, 3), date(2026, 10, 4)),
    )


def _items(day: date, n: int, start: int) -> list[dict]:
    return [
        {"disclosureIndex": start + i, "publishDate": day.strftime("%d.%m.%Y") + " 10:00:00", "subject": "x"}
        for i in range(n)
    ]


def test_list_cap_splits_windows_and_flags_overfull_single_days(db, kap_deps) -> None:
    # 4 days: day 1 has 2500 items (over the cap even alone), others 900 each.
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(4)]
    per_day = {
        days[0]: _items(days[0], 2500, 1000),
        **{d: _items(d, 900, 10000 * (i + 1)) for i, d in enumerate(days[1:])},
    }
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        lo, hi = date.fromisoformat(body["fromDate"]), date.fromisoformat(body["toDate"])
        requests.append((body["fromDate"], body["toDate"]))
        out = [it for d, its in per_day.items() if lo <= d <= hi for it in its]
        out.sort(key=lambda x: -x["disclosureIndex"])
        return httpx.Response(200, json=out[:LIST_CAP])

    with respx.mock(base_url=BASE) as router:
        router.post("/tr/api/disclosure/members/byCriteria").mock(side_effect=handler)
        with KapPublicWebClient(min_interval=0, sleep=lambda s: None) as client:
            res = client.list_disclosures(days[0], days[-1])
    assert ("2026-10-01", "2026-10-04") in requests
    assert ("2026-10-01", "2026-10-01") in requests  # split down to the single over-full day
    assert res.truncated_days == [days[0]]
    idx = [it["disclosureIndex"] for it in res.items]
    assert idx == sorted(set(idx))  # de-duplicated across overlapping requests, sorted
    assert len(idx) == LIST_CAP + 3 * 900
    assert res.requests == len(requests)
    assert len(res.raw_sha256s) == len(requests)


def test_every_response_is_archived(db, kap_deps, client) -> None:
    client.disclosure_detail(1688616)
    rows = db.execute(select(raw_object.c.url, raw_object.c.http_status)).all()
    assert any(r.url.endswith("/attachment-detail/1688616") and r.http_status == 200 for r in rows)
    log = db.execute(select(fetch_log.c.outcome).where(fetch_log.c.url.like("%1688616"))).scalars().all()
    assert log == ["ok"]


def test_unknown_index_is_not_found_and_does_not_trip_breaker(db, kap_deps, client) -> None:
    for _ in range(8):
        with pytest.raises(KapNotFound):
            client.disclosure_detail(999_999_999)
    db.rollback()
    assert (get_state(db, CIRCUIT_KEY, {}) or {}).get("state") != "open"
    outcomes = db.execute(select(fetch_log.c.outcome, fetch_log.c.http_status)).all()
    assert {(o.outcome, o.http_status) for o in outcomes} == {("http_error", 500)}


def test_waf_status_opens_circuit_and_blocks_further_requests(
    db, kap_deps, fake_kap, client, kap_http
) -> None:
    fake_kap.list_status = 666
    with pytest.raises(KapBlocked) as exc:
        client.list_disclosures(date(2026, 10, 27), date(2026, 10, 28))
    assert exc.value.until > now_utc()
    db.rollback()
    st = get_state(db, CIRCUIT_KEY)
    assert st["state"] == "open" and "666" in st["reason"]
    blocked = db.execute(select(fetch_log.c.outcome, fetch_log.c.http_status)).all()
    assert ("blocked", 666) in {(b.outcome, b.http_status) for b in blocked}
    assert kap_deps.alerts and kap_deps.alerts[0]["dedupe_key"] == "kap:circuit"
    calls = len(fake_kap.calls)
    with pytest.raises(KapBlocked):
        client.disclosure_detail(1688616)
    assert len(fake_kap.calls) == calls  # short-circuited: no HTTP request made

    # Cool-down elapsed → requests flow again and a success closes the circuit.
    set_state(db, CIRCUIT_KEY, {**st, "open_until": (now_utc() - timedelta(seconds=1)).isoformat()})
    db.commit()
    client.disclosure_detail(1688616)
    db.rollback()
    assert get_state(db, CIRCUIT_KEY)["state"] == "closed"


def test_5xx_spike_opens_circuit(db, kap_deps) -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get("/tr/api/company/items/IGS/A").mock(return_value=httpx.Response(503, text="busy"))
        with (
            KapPublicWebClient(min_interval=0, sleep=lambda s: None, max_retries=10) as client,
            pytest.raises(KapBlocked),
        ):
            client.companies()
    assert route.call_count == 5  # FAILURE_THRESHOLD consecutive failures
    db.rollback()
    assert get_state(db, CIRCUIT_KEY)["state"] == "open"


def test_retries_transient_errors_then_succeeds(db, kap_deps) -> None:
    sleeps: list[float] = []
    with respx.mock(base_url=BASE) as router:
        route = router.get("/tr/api/company/items/IGS/A").mock(
            side_effect=[httpx.ConnectTimeout("t"), httpx.Response(502), httpx.Response(200, json=[])]
        )
        with KapPublicWebClient(min_interval=0, sleep=sleeps.append) as client:
            assert client.companies().data == []
    assert route.call_count == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_min_interval_between_requests(db, kap_deps) -> None:
    sleeps: list[float] = []
    with respx.mock(base_url=BASE) as router:
        router.get("/tr/api/company/items/IGS/A").mock(return_value=httpx.Response(200, json=[]))
        with KapPublicWebClient(min_interval=5.0, sleep=sleeps.append) as client:
            client.companies()
            client.companies()
        assert router.calls.last.request.headers["user-agent"].startswith("papi/0.1")
    assert sleeps and sleeps[-1] > 4.0
