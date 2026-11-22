"""Typed ``/v1`` endpoints for the React UI (``api_ui``)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.core import fts

NEW_ROUTES = {
    ("get", "/v1/feed"): "FeedOut",
    ("get", "/v1/events/{event_id}/detail"): "EventDetailOut",
    ("post", "/v1/events/{event_id}/feedback"): "FeedbackOut",
    ("get", "/v1/portfolio/overview"): "PortfolioOverviewOut",
    ("get", "/v1/portfolio/performance"): "PerformanceOut",
    ("get", "/v1/instruments/{ticker}"): "InstrumentOut",
    ("get", "/v1/instruments/{ticker}/prices"): "PricesOut",
    ("get", "/v1/today"): "TodayOut",
    ("get", "/v1/search"): "SearchOut",
}


def test_openapi_has_named_schemas(client: TestClient) -> None:
    spec = client.get("/v1/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for (method, path), name in NEW_ROUTES.items():
        op = spec["paths"][path][method]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith(f"/{name}"), (path, ref)
        assert name in schemas
    assert "FeedItem" in schemas and "FeedbackIn" in schemas
    assert spec["paths"]["/v1/events/{event_id}/detail"]["get"]["responses"]["404"]
    # old untyped routes are untouched
    assert "/v1/events" in spec["paths"] and "/v1/portfolio" in spec["paths"]


def test_feed_shape_filters_and_cursor(client: TestClient, world: dict[str, Any]) -> None:
    body = client.get("/v1/feed", params={"scope": "all"}).json()
    assert set(body) == {"items", "next_cursor", "facets"}
    assert body["next_cursor"] is None and len(body["items"]) == 4
    item = next(i for i in body["items"] if i["id"] == world["e_div"])
    assert "payload" not in item and item["holding"] is True
    assert item["tickers"] == [{"instrument_id": world["asels"], "ticker": "ASELS", "badge": "HOLDING"}]
    assert item["stage"] == "analysed" and item["kap_url"].endswith("1500001")
    assert "families" in body["facets"]

    # cursor pagination walks the whole list exactly once, newest first
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params: dict[str, Any] = {"scope": "all", "limit": 1}
        if cursor:
            params["before"] = cursor
        page = client.get("/v1/feed", params=params).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [i["id"] for i in body["items"]] and len(set(seen)) == 4
    asc = client.get("/v1/feed", params={"scope": "all", "sort": "time_asc", "limit": 3}).json()
    rest = client.get("/v1/feed", params={"scope": "all", "sort": "time_asc", "before": asc["next_cursor"]})
    assert [i["id"] for i in asc["items"]] + [i["id"] for i in rest.json()["items"]] == seen[::-1]
    mat = client.get("/v1/feed", params={"scope": "all", "sort": "materiality", "limit": 2}).json()
    mat2 = client.get(
        "/v1/feed", params={"scope": "all", "sort": "materiality", "limit": 2, "before": mat["next_cursor"]}
    ).json()
    assert len({i["id"] for i in mat["items"] + mat2["items"]}) == 4

    assert [i["id"] for i in client.get("/v1/feed", params={"scope": "holdings"}).json()["items"]] == [
        world["e_fix"],
        world["e_div"],
    ]
    p1 = client.get("/v1/feed", params={"scope": "all", "priority": "P1"}).json()["items"]
    assert {i["id"] for i in p1} == {world["e_div"], world["e_fix"]}
    assert client.get("/v1/feed", params={"before": "garbage"}).status_code == 400
    assert client.get("/v1/feed", params={"scope": "nope"}).status_code == 422


def test_feed_after_and_search_query(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    items = client.get("/v1/feed", params={"scope": "all"}).json()["items"]
    newest = items[0]["published_key"]
    polled = client.get("/v1/feed", params={"scope": "all", "after": newest}).json()
    assert polled["next_cursor"] is None
    assert items[0]["id"] in {i["id"] for i in polled["items"]}
    assert all(i["published_key"] >= newest for i in polled["items"])
    future = client.get("/v1/feed", params={"scope": "all", "after": "2999-01-01T00:00:00Z"}).json()
    assert future["items"] == []
    fts.index_text(db, "disclosure_fts", world["d_div"], "Kâr payı dağıtım bildirimi aselsan")
    db.commit()
    hits = client.get("/v1/feed", params={"scope": "all", "q": "kar payi"}).json()["items"]
    assert [i["id"] for i in hits] == [world["e_div"]]


def test_event_detail(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get(f"/v1/events/{world['e_div']}/detail")
    assert r.status_code == 200
    d = r.json()
    assert d["item"]["id"] == world["e_div"] and "payload" not in d["item"]
    assert d["event"]["payload"]["gross_per_share"] == "2,50"
    doc = d["documents"][0]
    assert '<span class="span" id="d0s1"' in doc["html"] and "2,50 TL" in doc["html"]
    assert doc["download_url"] == f"/raw/{world['sha']}" and doc["template_id"] == "tbl_dividend"
    assert len(doc["spans"]) == 3 and len(doc["unplaced_span_ids"]) == 1
    assert d["analyses"][0]["claims"][0]["citations"][0]["anchor"] == "d0s1"
    assert d["summaries"]["tr"]["headline"] == "Nakit temettü" and d["summaries"]["en"] is None
    assert d["for_me"]["holding"] is True and d["for_me"]["positions"][0]["quantity"] == 100.0
    assert [x["revision_kind"] for x in d["revision_chain"]] == ["original", "correction"]
    assert {f["path"] for f in d["key_facts"]} >= {"gross_per_share", "payment_date"}
    missing = client.get("/v1/events/missing/detail")
    assert missing.status_code == 404 and "detail" in missing.json()


def test_event_feedback(client: TestClient, world: dict[str, Any]) -> None:
    url = f"/v1/events/{world['e_div']}/feedback"
    r = client.post(url, json={"kind": "helpful"})
    assert r.status_code == 200 and r.json() == {
        "event_id": world["e_div"],
        "feedback_counts": {"helpful": 1},
    }
    r = client.post(url, json={"kind": "wrong_fact", "comment": " x "})
    assert r.json()["feedback_counts"] == {"helpful": 1, "wrong_fact": 1}
    detail = client.get(f"/v1/events/{world['e_div']}/detail").json()
    assert detail["feedback_counts"] == {"helpful": 1, "wrong_fact": 1}
    assert client.post(url, json={"kind": "bogus"}).status_code == 422
    assert client.post("/v1/events/missing/feedback", json={"kind": "helpful"}).status_code == 404


def test_portfolio_overview(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get("/v1/portfolio/overview")
    assert r.status_code == 200
    p = r.json()
    assert p["available"] is True and len(p["positions"]) == 1
    pos = p["positions"][0]
    assert pos["ticker"] == "ASELS" and pos["kind"] == "stock" and pos["badge"] == "HOLDING"
    assert pos["quantity"] == 100.0 and pos["market_value"] == 6900.0 and pos["pnl"] == 650.0
    assert pos["prev_close"] is not None
    assert abs(pos["day_change"] - 100 * (69 - pos["prev_close"])) < 1e-6
    t = p["totals"]
    assert t["value"] == 6900.0 and t["cost"] == 6250.0 and abs(t["pnl_pct"] - 10.4) < 1e-6
    assert t["day_change"] == pos["day_change"] and t["unvalued"] == []
    assert p["allocation"]["by_kind"][0]["key"] == "stock" and p["allocation"]["by_kind"][0]["weight"] == 1.0
    assert p["transactions"][0]["type"] == "buy" and p["transactions"][0]["price"] == 62.5
    assert p["pending_actions"] == []
    wl = p["watchlists"][0]
    assert (
        wl["name"] == "Ana" and wl["members"][0]["ticker"] == "THYAO" and wl["members"][0]["badge"] == "WATCH"
    )


def test_portfolio_performance(client: TestClient) -> None:
    r = client.get("/v1/portfolio/performance", params={"range": "1m"})
    assert r.status_code == 200
    p = r.json()
    assert p["method"] == "transactions_x_closes" and p["range"] == "1m"
    assert len(p["points"]) >= 2
    first, last = p["points"][0], p["points"][-1]
    assert first["return_index"] == 100.0 and last["value"] == 100 * 69.0
    assert last["cost"] == 6250.0 and last["pnl"] == last["value"] - 6250.0
    assert last["return_index"] > 100.0
    assert client.get("/v1/portfolio/performance", params={"range": "max"}).status_code == 200
    assert client.get("/v1/portfolio/performance", params={"range": "5y"}).status_code == 422


def test_instrument_and_prices(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get("/v1/instruments/asels")
    assert r.status_code == 200
    i = r.json()
    assert i["ticker"] == "ASELS" and i["kind"] == "stock" and i["badge"] == "HOLDING"
    assert i["issuer"]["id"] == world["asels_issuer"] and i["position"]["quantity"] == 100.0
    assert i["last_close"]["close"] == 69.0 and i["last_close"]["prev_close"] == 68.0
    assert {d["id"] for d in i["disclosures"]} == {world["e_div"], world["e_fix"]}
    assert {x["scheme"] for x in i["identifiers"]} == {"TICKER", "KAP_MEMBER_OID"}
    assert (
        set(i["share_capital"]) >= {"paid_in_capital", "market_cap"}
        and i["share_capital"]["market_cap"] is None
    )
    assert i["theses"] == [] and i["agent_runs"] == []
    assert client.get("/v1/instruments/THYAO").json()["watch"] is True
    assert client.get("/v1/instruments/NOPE").status_code == 404

    pr = client.get("/v1/instruments/ASELS/prices")
    assert pr.status_code == 200
    body = pr.json()
    assert body["ticker"] == "ASELS" and "from" in body and len(body["bars"]) == 10
    bar = body["bars"][-1]
    assert bar["close"] == 69.0 and bar["open"] is None and bar["volume"] is None and bar["source"]
    assert {m["event_id"] for m in body["markers_disclosures"]} == {world["e_div"], world["e_fix"]}
    assert body["markers_txns"][0]["side"] == "buy" and body["markers_txns"][0]["price"] == 62.5
    empty = client.get(
        "/v1/instruments/ASELS/prices", params={"from": "2000-01-01", "to": "2000-02-01"}
    ).json()
    assert empty["bars"] == [] and empty["markers_disclosures"] == []
    assert (
        client.get(
            "/v1/instruments/ASELS/prices", params={"from": "2030-01-01", "to": "2020-01-01"}
        ).status_code
        == 400
    )
    assert client.get("/v1/instruments/NOPE/prices").status_code == 404


def test_today(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get("/v1/today", params={"feed_limit": 3})
    assert r.status_code == 200
    t = r.json()
    assert t["usdtry"] is None and t["xu100"] is None  # no index / FX data seeded
    assert t["portfolio"]["value"] == 6900.0 and t["portfolio"]["positions"] == 1
    assert {i["id"] for i in t["attention"]["items"]} == {world["e_div"], world["e_fix"]}
    assert t["attention"]["window_hours"] == 48
    assert t["pending_actions"] == {"count": 0, "items": []}
    assert t["jobs"]["dead_total"] == 1 and t["jobs"]["lanes"][0]["last_error"] == "ValueError: boom"
    assert t["jobs"]["analyses_waiting"] == 0
    assert 0 < len(t["feed"]) <= 3 and isinstance(t["agent_runs"], list)


def test_search_holdings_first(client: TestClient, world: dict[str, Any], db: Connection) -> None:
    body = client.get("/v1/search", params={"q": "a"}).json()
    assert [h["ticker"] for h in body["instruments"]] == ["ASELS", "THYAO", "GARAN"]
    assert [h["badge"] for h in body["instruments"]] == ["HOLDING", "WATCH", None]
    by_name = client.get("/v1/search", params={"q": "türk hava"}).json()["instruments"]
    assert by_name == [] or by_name[0]["ticker"] == "THYAO"
    assert [h["ticker"] for h in client.get("/v1/search", params={"q": "gar"}).json()["instruments"]] == [
        "GARAN"
    ]
    fts.index_text(db, "disclosure_fts", world["d_div"], "Kâr payı dağıtım bildirimi")
    db.commit()
    hits = client.get("/v1/search", params={"q": "temettü kar pay"}).json()["disclosures"]
    assert hits == []
    hits = client.get("/v1/search", params={"q": "kar pay"}).json()["disclosures"]
    assert hits[0]["event_id"] == world["e_div"] and hits[0]["tickers"] == ["ASELS"]
    assert client.get("/v1/search", params={"q": '"'}).status_code == 200
    assert client.get("/v1/search").status_code == 422
