"""``/v1`` JSON API."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_events_list_and_filters(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get("/v1/events")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "all" and len(body["items"]) == 4
    item = next(i for i in body["items"] if i["id"] == world["e_div"])
    assert item["kap_url"] == "https://www.kap.org.tr/tr/Bildirim/1500001"
    assert item["tickers"] == [{"instrument_id": world["asels"], "ticker": "ASELS", "badge": "HOLDING"}]
    assert item["payload"]["gross_per_share"] == "2,50"
    holdings = client.get("/v1/events?scope=holdings").json()["items"]
    assert {i["id"] for i in holdings} == {world["e_div"], world["e_fix"]}
    assert [i["id"] for i in client.get("/v1/events?code=business.contract.award").json()["items"]] == [
        world["e_thy"]
    ]
    assert client.get("/v1/events?ticker=GARAN&materiality_gte=50").json()["items"] == []
    assert client.get("/v1/events?as_of=2000-01-01T00:00:00Z").json()["items"] == []


def test_event_detail_json(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get(f"/v1/events/{world['e_div']}")
    assert r.status_code == 200
    d = r.json()
    facts = {f["path"]: f for f in d["key_facts"]}
    assert (
        facts["gross_per_share"]["anchors"] == ["d0s1"]
        and facts["gross_per_share"]["provenance"] == "payload_spans"
    )
    assert facts["payment_date"]["provenance"] == "form_field"
    assert d["analyses"][0]["claims"][0]["citations"][0]["anchor"] == "d0s1"
    assert d["for_me"]["holding"] is True and d["for_me"]["positions"][0]["quantity"] == "100"
    assert [r["revision_kind"] for r in d["revision_chain"]] == ["original", "correction"]
    assert client.get("/v1/events/missing").status_code == 404


def test_disclosure_and_issuer(client: TestClient, world: dict[str, Any]) -> None:
    d = client.get(f"/v1/disclosures/{world['d_fix']}").json()
    assert d["revision_of"] == world["d_div"] and d["revision_kind"] == "correction"
    assert {r["id"] for r in d["revisions"]} == {world["d_div"], world["d_fix"]}
    body = client.get(f"/v1/disclosures/{world['d_div']}").json()
    assert body["documents"][0]["spans"][0]["anchor"] == "d0s1" and body["latency_s"] == 40.0
    assert client.get("/v1/disclosures/missing").status_code == 404
    iss = client.get("/v1/issuers", params={"ticker": "asels"}).json()
    assert iss["id"] == world["asels_issuer"] and iss["instruments"][0]["ticker"] == "ASELS"
    assert {i["scheme"] for i in iss["identifiers"]} == {"TICKER", "KAP_MEMBER_OID"}
    assert {e["id"] for e in iss["recent_events"]} == {world["e_div"], world["e_fix"]}
    assert client.get(f"/v1/issuers/{world['asels_issuer']}").json()["legal_name"].startswith("Aselsan")
    assert client.get("/v1/issuers", params={"ticker": "NOPE"}).status_code == 404


def test_portfolio_routes(client: TestClient) -> None:
    p = client.get("/v1/portfolio").json()
    assert p["total_value"] == "6900" and p["total_pnl"] == "650"
    pos = client.get("/v1/portfolio/positions").json()["positions"]
    assert pos[0]["ticker"] == "ASELS" and pos[0]["pnl"] == "650"
    txns = client.get("/v1/portfolio/transactions").json()["transactions"]
    assert txns[0]["type"] == "buy" and txns[0]["price"] == "62.50"


def test_usage_and_governor_control(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    u = client.get("/v1/usage").json()
    assert u["window_5h"]["calls"] == 3 and abs(u["window_5h"]["cost"] - 0.15) < 1e-9
    assert u["window_week"]["calls"] == 4 and u["governor"] is None
    monkeypatch.setitem(sys.modules, "papi.llm.governor", None)
    assert client.post("/v1/llm/pause").status_code == 503
    calls: list[str] = []
    mod = types.ModuleType("papi.llm.governor")
    mod.pause = lambda conn, until=None, reason="manual": calls.append("pause")  # type: ignore[attr-defined]
    mod.resume = lambda conn: calls.append("resume")  # type: ignore[attr-defined]
    mod.status = lambda conn: {"paused": calls[-1] == "pause"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "papi.llm.governor", mod)
    from papi.web import integrations

    monkeypatch.setattr(integrations, "governor_status", lambda conn: mod.status(conn))
    assert client.post("/v1/llm/pause").json() == {"ok": True, "governor": {"paused": True}}
    assert client.post("/v1/llm/resume").json() == {"ok": True, "governor": {"paused": False}}
    assert calls == ["pause", "resume"]
