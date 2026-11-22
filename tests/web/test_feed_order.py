"""Feed ordering (publish time, never processing time), date / band / sort filters, and the stream."""

from __future__ import annotations

import itertools
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy import Connection, update

from papi.core import db as core_db
from papi.core.schema import event
from papi.web import integrations, queries
from papi.web.deps import parse_feed_filters
from tests.web.sample import _disclosure, _event, publish_classified

ISTANBUL_MIDNIGHT_28 = datetime(2026, 10, 27, 21, 0, tzinfo=UTC)  # 28.10.2026 00:00 Europe/Istanbul


def _published(html: str) -> list[str]:
    return re.findall(r'<article class="item[^"]*"[^>]*data-published="([^"]*)"', html)


def _mats(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'<article class="item[^"]*"[^>]*data-mat="(-?\d+)"', html)]


def _hide_seeded(db: Connection) -> None:
    """Take the sample events out of the feed (self-superseded) so a test controls every row."""
    db.execute(update(event).where(event.c.superseded_by.is_(None)).values(superseded_by=event.c.id))


def _add(
    db: Connection,
    world: dict[str, Any],
    *,
    ref: str,
    published: datetime,
    created: datetime,
    materiality: int = 50,
    code: str = "business.contract.award",
    headline: str | None = None,
) -> str:
    d = _disclosure(
        db,
        kap=world["kap"],
        ref=ref,
        iss=world["asels_issuer"],
        published=published,
        seen=created,
        title=headline or ref,
    )
    eid = _event(
        db,
        disc=d,
        iss=world["asels_issuer"],
        ins=world["asels"],
        ticker="ASELS",
        code=code,
        at=published,
        materiality=materiality,
        priority="P2",
        headline=headline or f"olay {ref}",
    )
    db.execute(update(event).where(event.c.id == eid).values(created_at=created))
    return eid


@pytest.fixture
def backlog(world: dict[str, Any], db: Connection) -> dict[str, str]:
    """Only these events exist: processed in the reverse order of publication (catch-up)."""
    _hide_seeded(db)
    base = datetime(2026, 10, 28, 6, 17, tzinfo=UTC)  # 09:17 Istanbul
    now = datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    ids = {
        # processed last, published first (27.10 18:36 Istanbul)
        "old": _add(
            db,
            world,
            ref="1700001",
            published=datetime(2026, 10, 27, 15, 36, tzinfo=UTC),
            created=now + timedelta(minutes=5),
            materiality=90,
        ),
        "mid": _add(
            db,
            world,
            ref="1700002",
            published=base - timedelta(hours=1),
            created=now + timedelta(minutes=2),
            materiality=40,
        ),
        # two disclosures with the same publish second: KAP index breaks the tie
        "new_a": _add(db, world, ref="1700003", published=base, created=now, materiality=60),
        "new_b": _add(
            db, world, ref="1700004", published=base, created=now - timedelta(minutes=9), materiality=60
        ),
    }
    db.commit()
    return ids


# ------------------------------------------------------------------------------------------ ordering


def test_query_orders_by_publish_time_not_processing_time(
    backlog: dict[str, str], world: dict[str, Any], db: Connection
) -> None:
    scope = queries.load_scope(db)
    items = queries.feed_items(db, queries.FeedFilters(scope="all"), scope)
    assert [i["id"] for i in items] == [backlog["new_b"], backlog["new_a"], backlog["mid"], backlog["old"]]
    asc = queries.feed_items(db, queries.FeedFilters(scope="all", sort="time_asc"), scope)
    assert [i["id"] for i in asc] == [backlog["old"], backlog["mid"], backlog["new_a"], backlog["new_b"]]
    by_mat = queries.feed_items(db, queries.FeedFilters(scope="all", sort="materiality"), scope)
    assert [i["id"] for i in by_mat] == [backlog["old"], backlog["new_b"], backlog["new_a"], backlog["mid"]]


@pytest.mark.parametrize("sort", ["time_desc", "time_asc", "materiality"])
def test_rendered_data_published_in_order(client: TestClient, backlog: dict[str, str], sort: str) -> None:
    """JS-free check: the server-rendered ``data-*`` sort keys follow the selected order."""
    html = client.get(f"/legacy/?scope=all&sort={sort}").text
    pub, mats = _published(html), _mats(html)
    assert len(pub) == 4 and all(pub)
    assert f'data-sort="{sort}"' in html
    if sort == "time_desc":
        assert pub == sorted(pub, reverse=True)
    elif sort == "time_asc":
        assert pub == sorted(pub)
    else:
        assert mats == sorted(mats, reverse=True)
        pairs = list(zip(mats, pub, strict=True))
        assert all(a[1] >= b[1] for a, b in itertools.pairwise(pairs) if a[0] == b[0])


def test_api_sort_options(client: TestClient, backlog: dict[str, str]) -> None:
    def ids(q: str) -> list[str]:
        return [i["id"] for i in client.get(f"/v1/events?{q}").json()["items"]]

    assert ids("") == [backlog["new_b"], backlog["new_a"], backlog["mid"], backlog["old"]]
    assert ids("sort=time_asc")[0] == backlog["old"]
    assert ids("sort=materiality")[0] == backlog["old"]
    assert ids("sort=bogus") == ids("")  # unknown sort falls back to newest first


# ------------------------------------------------------------------------------------------ dates


def test_istanbul_day_boundaries(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    _hide_seeded(db)
    late_27 = _add(
        db,
        world,
        ref="1800001",
        published=ISTANBUL_MIDNIGHT_28 - timedelta(seconds=1),
        created=ISTANBUL_MIDNIGHT_28,
    )
    early_28 = _add(db, world, ref="1800002", published=ISTANBUL_MIDNIGHT_28, created=ISTANBUL_MIDNIGHT_28)
    db.commit()

    def ids(q: str) -> set[str]:
        return {i["id"] for i in client.get(f"/v1/events?{q}").json()["items"]}

    assert ids("from=2026-10-28&to=2026-10-28") == {early_28}
    assert ids("from=2026-10-27&to=2026-10-27") == {late_27}
    assert ids("to=2026-10-27") == {late_27}
    assert ids("from=2026-10-28") == {early_28}
    with time_machine.travel(datetime(2026, 10, 28, 6, 0, tzinfo=UTC), tick=True):  # 09:00 Istanbul
        assert ids("range=today") == {early_28}
        assert ids("range=yesterday") == {late_27}
        assert ids("range=7d") == {late_27, early_28}
        # custom dates override a quick range
        assert ids("range=today&from=2026-10-27&to=2026-10-27") == {late_27}
        html = client.get("/legacy/?scope=all&range=yesterday").text
        assert 'class="qbtn on" href="/legacy/?scope=all&amp;range=yesterday">Dün' in html
        assert '<input type="hidden" name="range" value="yesterday">' in html
    # 23:30 UTC on the 27th is already the 28th in Istanbul
    with time_machine.travel(datetime(2026, 10, 27, 23, 30, tzinfo=UTC), tick=True):
        assert ids("range=today") == {early_28}


def test_quick_range_dates() -> None:
    today = date(2026, 10, 28)
    f = parse_feed_filters({"range": "7d"})
    assert f.dates(today) == (date(2026, 10, 22), today)
    assert parse_feed_filters({"range": "yesterday"}).dates(today) == (date(2026, 10, 27), date(2026, 10, 27))
    assert parse_feed_filters({"range": "nope"}).dates(today) == (None, None)
    custom = parse_feed_filters({"range": "today", "from": "2026-10-01"})
    assert custom.range is None and custom.dates(today) == (date(2026, 10, 1), None)


# ------------------------------------------------------------------------------------------ bands


def test_band_floors_follow_triage() -> None:
    pytest.importorskip("papi.triage.materiality")
    assert integrations.band_floors() == {"low": 0, "medium": 30, "high": 60, "critical": 80}


@pytest.mark.parametrize("use_triage", [True, False])
def test_band_filter(
    client: TestClient,
    world: dict[str, Any],
    db: Connection,
    monkeypatch: pytest.MonkeyPatch,
    use_triage: bool,
) -> None:
    if use_triage:
        monkeypatch.setattr(
            integrations, "band_floors", lambda: {"low": 0, "medium": 30, "high": 60, "critical": 80}
        )
    else:  # triage missing → fall back to the stored band labels
        monkeypatch.setattr(integrations, "band_floors", lambda: None)
    _hide_seeded(db)
    t = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    by_score = {}
    for i, score in enumerate((29, 30, 59, 60, 79, 80)):
        eid = _add(
            db, world, ref=f"190000{i}", published=t - timedelta(minutes=i), created=t, materiality=score
        )
        band = "low" if score < 30 else "medium" if score < 60 else "high" if score < 80 else "critical"
        db.execute(update(event).where(event.c.id == eid).values(materiality_band=band))
        by_score[score] = eid
    db.commit()

    def scores(q: str) -> set[int]:
        items = client.get(f"/v1/events?{q}").json()["items"]
        return {i["materiality"] for i in items}

    assert scores("") == {29, 30, 59, 60, 79, 80}
    assert scores("band=low") == {29, 30, 59, 60, 79, 80}
    assert scores("band=medium") == {30, 59, 60, 79, 80}
    assert scores("band=high") == {60, 79, 80}
    assert scores("band=critical") == {80}
    assert scores("band=bogus") == {29, 30, 59, 60, 79, 80}


def test_filter_bar(client: TestClient, world: dict[str, Any]) -> None:
    html = client.get("/legacy/?scope=all").text
    assert 'name="materiality_gte"' not in html  # replaced by the band select
    for label in (
        "Önemlilik: Tümü",
        "Düşük+",
        "Orta+",
        "Yüksek+",
        "Kritik",
        "Bugün",
        "Dün",
        "Son 7 gün",
        "Başlangıç",
        "Bitiş",
        "Yayın zamanı (yeni→eski)",
        "Yayın zamanı (eski→yeni)",
        "Önemlilik (yüksek→düşük, sonra zaman)",
    ):
        assert label in html
    assert "filtre etkin" not in html and "data-autosubmit" in html and 'class="js-hide"' in html
    html = client.get(
        "/legacy/?scope=all&band=high&hide_routine=1&family=distribution&sort=materiality&range=7d"
    ).text
    assert "4 filtre etkin" in html
    assert 'href="/legacy/?scope=all&amp;sort=materiality">Filtreleri temizle' in html
    assert '<option value="high" selected>' in html and '<option value="materiality" selected>' in html
    # every filter and the sort travel to the poll URL and the load-more links
    stream = re.search(r'data-poll="([^"]+)"', html)
    assert stream is not None
    for part in ("band=high", "hide_routine=1", "family=distribution", "sort=materiality", "range=7d"):
        assert part in stream.group(1)
    # quick-range links keep the other filters and drop custom dates
    assert (
        'href="/legacy/?scope=all&amp;family=distribution&amp;band=high&amp;range=today&amp;hide_routine=1&amp;sort=materiality"'
        in html
    )
    legacy = client.get("/v1/events?scope=all&materiality_gte=70").json()["items"]
    assert {i["materiality"] for i in legacy} == {75, 70}


# ------------------------------------------------------------------------------------------ stream


def _poll(client: TestClient, after: str, **params: Any) -> list[dict[str, Any]]:
    return client.get("/legacy/stream/poll", params={"after": after, **params}).json()["items"]


def test_poll_honours_band_and_date_filters(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    first = publish_classified(db, world["e_gar"], "reporting.annual_report", "P4")
    publish_classified(db, world["e_thy"], "business.contract.award", "P3")  # 65 → high
    publish_classified(db, world["e_div"], "distribution.dividend.cash", "P1")  # 75 → high
    db.commit()
    items = _poll(client, first, scope="all", band="high", sort="materiality")
    assert [i["event_id"] for i in items] == [world["e_thy"], world["e_div"]]
    assert all('data-published="' in i["html"] and 'data-mat="' in i["html"] for i in items)
    assert [i["mat"] for i in items] == [65, 75]
    assert _poll(client, first, scope="all", band="critical") == []
    assert _poll(client, first, scope="all", **{"from": "2000-01-01", "to": "2000-01-02"}) == []
    with time_machine.travel(datetime(2000, 1, 1, 12, 0, tzinfo=UTC), tick=True):
        assert _poll(client, first, scope="all", range="today") == []


def test_poll_catchup_item_carries_its_publish_time(
    clean_registry: Any, client: TestClient, world: dict[str, Any], db: Connection
) -> None:
    """A backlog item processed now is delivered with its old publish time (the client places it)."""
    old = _add(
        db,
        world,
        ref="1990001",
        published=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        created=datetime(2026, 10, 28, 7, 0, tzinfo=UTC),
    )
    db.commit()
    cursor = client.get("/legacy/stream/poll").json()["cursor"]
    with core_db.transaction() as conn:
        publish_classified(conn, old, "business.contract.award", "P2")
    items = _poll(client, cursor, scope="all")
    assert len(items) == 1
    assert items[0]["published"] == "2026-10-01T09:00:00.000000Z" and items[0]["ref"] == 1990001
    assert 'data-published="2026-10-01T09:00:00.000000Z"' in items[0]["html"]
    assert 'data-ref="1990001"' in items[0]["html"]
