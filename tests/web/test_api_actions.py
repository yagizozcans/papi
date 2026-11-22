"""P2 slice A endpoints (``api_actions``): trades, undo, pending actions, watchlist, LLM, alert rules."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection

from papi.core.timeutil import ist_date
from papi.portfolio import service
from papi.telegram import outbox
from papi.telegram.api import TelegramError
from papi.web import integrations
from tests.portfolio.conftest import add_corporate_action

NEW_ROUTES = {
    ("post", "/v1/portfolio/transactions"): "TxnResultOut",
    ("post", "/v1/portfolio/transactions/undo"): "UndoOut",
    ("post", "/v1/portfolio/pending/{pending_id}/apply"): "PendingApplyOut",
    ("post", "/v1/portfolio/pending/{pending_id}/dismiss"): "PendingDismissOut",
    ("post", "/v1/watchlist"): "WatchOut",
    ("delete", "/v1/watchlist"): "UnwatchOut",
    ("get", "/v1/llm/status"): "LlmStatusOut",
    ("post", "/v1/llm/governor/pause"): "LlmStatusOut",
    ("post", "/v1/llm/governor/resume"): "LlmStatusOut",
    ("get", "/v1/alerts/rules"): "AlertRulesOut",
    ("post", "/v1/alerts/rules"): "AlertRuleOut",
    ("patch", "/v1/alerts/rules/{rule_id}"): "AlertRuleOut",
    ("delete", "/v1/alerts/rules/{rule_id}"): "RuleDeletedOut",
    ("post", "/v1/alerts/test"): "AlertTestOut",
}


@pytest.fixture(autouse=True)
def real_portfolio(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared ``world`` fixture freezes positions/pending; these tests need the live ledger."""
    monkeypatch.setattr(integrations, "positions", lambda conn: service.positions(conn))
    monkeypatch.setattr(
        integrations, "pending_actions", lambda conn, status="expected": service.pending_actions(conn, status)
    )


def test_openapi_has_named_schemas(client: TestClient) -> None:
    spec = client.get("/v1/openapi.json").json()
    for (method, path), name in NEW_ROUTES.items():
        ref = spec["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith(f"/{name}"), (path, ref)
    # the old untyped governor routes stay
    assert "/v1/llm/pause" in spec["paths"]


# ------------------------------------------------------------------------------------------ trades


def _trade(**kw: Any) -> dict[str, Any]:
    return {"type": "buy", "ticker": "ASELS", "quantity": 50, "price": 70.0, **kw}


def test_trade_preview_writes_nothing_then_commit(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/portfolio/transactions", json=_trade(dry_run=True, fees=2.5))
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["committed"] is False and prev["txn_id"] is None and prev["ticker"] == "ASELS"
    assert prev["before"]["quantity"] == 100 and prev["after"]["quantity"] == 150
    # (100 x 62.50 + 50 x 70 + 2.5) / 150
    assert prev["after"]["avg_cost"] == pytest.approx((6250 + 3500 + 2.5) / 150)
    assert prev["gross"] == 3500 and prev["trade_date"] == ist_date().isoformat()
    ov = client.get("/v1/portfolio/overview").json()
    assert ov["positions"][0]["quantity"] == 100 and len(ov["transactions"]) == 1  # nothing written

    r = client.post("/v1/portfolio/transactions", json=_trade(fees=2.5, note="deneme"))
    assert r.status_code == 200
    done = r.json()
    assert done["committed"] is True and done["txn_id"] and done["after"]["quantity"] == 150
    ov = client.get("/v1/portfolio/overview").json()
    assert ov["positions"][0]["quantity"] == 150 and len(ov["transactions"]) == 2


def test_trade_sell_opening_and_lowercase_ticker(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/portfolio/transactions", json=_trade(type="sell", ticker="asels", quantity=40))
    assert r.status_code == 200 and r.json()["after"]["quantity"] == 60
    r = client.post(
        "/v1/portfolio/transactions",
        json=_trade(type="opening_balance", ticker="THYAO", quantity=10, price=300.5),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["before"]["quantity"] == 0 and body["after"]["quantity"] == 10 and body["name"]


def test_trade_oversell_is_a_warning_not_an_error(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/portfolio/transactions", json=_trade(type="sell", quantity=500, dry_run=True))
    assert r.status_code == 200
    assert any("fazla" in w for w in r.json()["warnings"])


def test_trade_validation_errors(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/portfolio/transactions", json=_trade(ticker="NOPE1"))
    assert r.status_code == 422 and r.json()["detail"].startswith("Bilinmeyen hisse kodu")
    r = client.post("/v1/portfolio/transactions", json=_trade(quantity=0))
    assert r.status_code == 422 and "Miktar" in r.json()["detail"]
    r = client.post("/v1/portfolio/transactions", json=_trade(price=None))
    assert r.status_code == 422 and "fiyat" in r.json()["detail"].lower()
    r = client.post("/v1/portfolio/transactions", json=_trade(price=-1))
    assert r.status_code == 422
    r = client.post("/v1/portfolio/transactions", json=_trade(type="bogus"))
    assert r.status_code == 422  # pydantic: unknown type
    assert len(client.get("/v1/portfolio/overview").json()["transactions"]) == 1


def test_undo_last_and_specific(client: TestClient, world: dict[str, Any]) -> None:
    first = client.get("/v1/portfolio/overview").json()["transactions"][0]["id"]
    new_id = client.post("/v1/portfolio/transactions", json=_trade()).json()["txn_id"]
    r = client.post("/v1/portfolio/transactions/undo", json={})
    assert r.status_code == 200
    out = r.json()
    assert out["undone"]["id"] == new_id and out["undone"]["reversed"] is True and out["reversal_id"]
    pos = client.get("/v1/portfolio/overview").json()["positions"][0]
    assert pos["quantity"] == 100
    # the next "last" is the original buy; a reversal row can't be undone
    assert (
        client.post("/v1/portfolio/transactions/undo", json={"txn_id": out["reversal_id"]}).status_code == 409
    )
    assert client.post("/v1/portfolio/transactions/undo", json={"txn_id": new_id}).status_code == 409
    assert client.post("/v1/portfolio/transactions/undo", json={"txn_id": "nope"}).status_code == 404
    r = client.post("/v1/portfolio/transactions/undo", json={"txn_id": first, "reason": "yanlış"})
    assert r.status_code == 200 and r.json()["undone"]["id"] == first
    assert client.post("/v1/portfolio/transactions/undo", json={}).status_code == 404  # nothing left


# ------------------------------------------------------------------------------------------ pending actions


def _pending(db: Connection, world: dict[str, Any]) -> str:
    ca = add_corporate_action(db, world["asels"], "bonus", ist_date(), factor="1")
    [pa] = service.propose_for_corporate_action(db, ca)
    db.commit()
    return pa


def test_pending_apply_and_dismiss(client: TestClient, db: Connection, world: dict[str, Any]) -> None:
    pa = _pending(db, world)
    assert [a["id"] for a in client.get("/v1/portfolio/overview").json()["pending_actions"]] == [pa]
    r = client.post(f"/v1/portfolio/pending/{pa}/apply", json={"quantity_override": 80})
    assert r.status_code == 200 and r.json()["pending_id"] == pa and r.json()["txn_id"]
    assert client.post(f"/v1/portfolio/pending/{pa}/apply").status_code == 409  # already applied
    assert client.post(f"/v1/portfolio/pending/{pa}/dismiss").status_code == 409
    undone = client.post("/v1/portfolio/transactions/undo", json={}).json()
    assert undone["undone"]["source"].startswith("derived:")  # undo re-opens the pending action
    assert [a["id"] for a in client.get("/v1/portfolio/overview").json()["pending_actions"]] == [pa]
    assert client.post(f"/v1/portfolio/pending/{pa}/dismiss").status_code == 200
    assert client.get("/v1/portfolio/overview").json()["pending_actions"] == []
    assert client.post("/v1/portfolio/pending/missing/apply").status_code == 404
    assert client.post("/v1/portfolio/pending/missing/dismiss").status_code == 404


def test_pending_apply_without_body(client: TestClient, db: Connection, world: dict[str, Any]) -> None:
    pa = _pending(db, world)
    assert client.post(f"/v1/portfolio/pending/{pa}/apply").status_code == 200
    assert client.get("/v1/portfolio/overview").json()["positions"][0]["quantity"] == 200


# ------------------------------------------------------------------------------------------ watchlist


def test_watch_and_unwatch(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/watchlist", json={"ref": "garan"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and set(body["watchlist"]["tickers"]) == {"THYAO", "GARAN"}
    assert client.post("/v1/watchlist", json={"ref": "GARAN"}).json()["changed"] is False
    inst = client.get("/v1/instruments/GARAN").json()
    assert inst["watch"] is True and "Ana" in inst["watchlists"]

    r = client.post("/v1/watchlist", json={"ref": "sektor:bankacilik", "watchlist": "Banka"})
    assert r.status_code == 200 and r.json()["watchlist"]["sectors"] == ["bankacilik"]
    r = client.post("/v1/watchlist", json={"ref": "endeks:XU030", "watchlist": "Banka"})
    assert r.json()["watchlist"]["indices"] == ["XU030"]

    r = client.delete("/v1/watchlist", params={"ref": "GARAN"})
    assert r.status_code == 200 and r.json()["removed"] is True
    assert r.json()["watchlist"]["tickers"] == ["THYAO"]
    assert client.delete("/v1/watchlist", params={"ref": "GARAN"}).json()["removed"] is False
    assert client.delete("/v1/watchlist", params={"ref": "endeks:XU030", "watchlist": "Banka"}).json()[
        "removed"
    ]
    assert client.get("/v1/instruments/GARAN").json()["watch"] is False


def test_watch_unknown_ticker_is_422(client: TestClient, world: dict[str, Any]) -> None:
    r = client.post("/v1/watchlist", json={"ref": "ZZZZZ"})
    assert r.status_code == 422 and "Bilinmeyen" in r.json()["detail"]
    assert client.delete("/v1/watchlist", params={"ref": "ZZZZZ"}).status_code == 422
    assert client.post("/v1/watchlist", json={"ref": ""}).status_code == 422


# ------------------------------------------------------------------------------------------ LLM


def test_llm_status_pause_resume(client: TestClient, world: dict[str, Any]) -> None:
    st = client.get("/v1/llm/status").json()
    assert st["paused"] is False and st["cap_5h"] > 0 and st["cap_week"] > 0
    assert set(st["queue"]) == {"deferred", "deferred_total"} and st["subscription_bars"] is None

    st = client.post("/v1/llm/governor/pause", json={"minutes": 60}).json()
    assert st["paused"] is True and st["paused_until"] and st["pause_reason"] == "manual"
    assert client.get("/v1/llm/status").json()["paused"] is True

    st = client.post("/v1/llm/governor/pause").json()  # no body: until resumed
    assert st["paused"] is True and st["paused_until"] is None
    assert client.post("/v1/llm/governor/pause", json={"minutes": 0}).status_code == 422

    st = client.post("/v1/llm/governor/resume").json()
    assert st["paused"] is False and st["paused_until"] is None


# ------------------------------------------------------------------------------------------ alert rules


def test_alert_rules_crud(client: TestClient, world: dict[str, Any]) -> None:
    listing = client.get("/v1/alerts/rules").json()
    assert listing["rules"] == [] and "materiality" in listing["fields"]

    new = {"name": "Savunma", "filter": 'sector = "savunma" AND materiality >= 60'}
    r = client.post("/v1/alerts/rules", json={**new, "quiet_hours": {"start": "23:00", "end": "07:30"}})
    assert r.status_code == 200
    rule = r.json()
    assert rule["enabled"] is True and rule["channels"] == ["telegram", "macos"]
    assert rule["quiet_hours"] == {"start": "23:00", "end": "07:30"}
    rid = rule["id"]
    assert [x["id"] for x in client.get("/v1/alerts/rules").json()["rules"]] == [rid]

    assert client.post("/v1/alerts/rules", json=new).status_code == 409  # duplicate name
    bad = client.post("/v1/alerts/rules", json={"name": "x", "filter": "materiality >>"})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("Filtre hatalı")
    assert client.post("/v1/alerts/rules", json={**new, "name": "y", "channels": ["sms"]}).status_code == 422
    assert (
        client.post(
            "/v1/alerts/rules", json={**new, "name": "z", "quiet_hours": {"start": "25:00", "end": "07:00"}}
        ).status_code
        == 422
    )

    off = client.patch(f"/v1/alerts/rules/{rid}", json={"enabled": False}).json()
    assert off["enabled"] is False and off["updated_at"] and off["filter"] == new["filter"]
    upd = client.patch(
        f"/v1/alerts/rules/{rid}",
        json={
            "name": "Savunma 2",
            "filter": 'scope = "holdings"',
            "channels": ["telegram"],
            "quiet_hours": None,
        },
    ).json()
    assert upd["name"] == "Savunma 2" and upd["channels"] == ["telegram"] and upd["quiet_hours"] is None
    assert client.patch(f"/v1/alerts/rules/{rid}", json={"filter": "((("}).status_code == 422
    other = client.post("/v1/alerts/rules", json={"name": "Diğer", "filter": 'scope = "all"'}).json()
    assert client.patch(f"/v1/alerts/rules/{other['id']}", json={"name": "Savunma 2"}).status_code == 409
    assert client.patch("/v1/alerts/rules/nope", json={"enabled": True}).status_code == 404

    assert client.delete(f"/v1/alerts/rules/{rid}").json() == {"id": rid}
    assert client.delete(f"/v1/alerts/rules/{rid}").status_code == 404
    assert [x["id"] for x in client.get("/v1/alerts/rules").json()["rules"]] == [other["id"]]


def test_alert_rule_validate(client: TestClient, world: dict[str, Any]) -> None:
    ok = client.post("/v1/alerts/rules/validate", json={"filter": 'scope = "holdings"'}).json()
    assert ok == {"ok": True, "error": None}
    bad = client.post("/v1/alerts/rules/validate", json={"filter": "foo = 1"}).json()
    assert bad["ok"] is False and "bilinmeyen alan" in bad["error"]


def test_alert_test_notification(
    client: TestClient, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def fake_send(conn: Connection, text: str, **kw: Any) -> outbox.Sent:
        sent.append(text)
        return outbox.Sent("row", 42)

    monkeypatch.setattr(outbox, "send", fake_send)
    r = client.post("/v1/alerts/test", json={})
    assert r.status_code == 200 and r.json() == {"ok": True, "channel": "telegram", "detail": "mesaj #42"}
    assert len(sent) == 1

    def boom(conn: Connection, text: str, **kw: Any) -> outbox.Sent:
        raise TelegramError("sendMessage", "ağ yok")

    monkeypatch.setattr(outbox, "send", boom)
    r = client.post("/v1/alerts/test")
    assert r.status_code == 200 and r.json()["ok"] is False and "ağ yok" in r.json()["detail"]

    r = client.post("/v1/alerts/test", json={"channel": "macos"})
    assert (
        r.status_code == 200 and r.json()["channel"] == "macos"
    )  # disabled in tests -> ok false, not an error
