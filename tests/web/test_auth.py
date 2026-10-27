"""Token enforcement for non-loopback binds; Host / Origin guards for loopback."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from papi.web.app import TOKEN_COOKIE, create_app, is_loopback


def test_is_loopback() -> None:
    assert (
        is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1") and is_loopback("[::1]")
    )
    assert not is_loopback("0.0.0.0") and not is_loopback("100.64.1.2") and not is_loopback("myhost")


def test_non_loopback_requires_configured_token() -> None:
    with pytest.raises(RuntimeError, match="token"):
        create_app(host="0.0.0.0")


@pytest.mark.parametrize("path", ["/", "/ops", "/v1/events", "/static/app.css", "/v1/stream", "/stream"])
def test_token_enforced_on_all_routes(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("PAPI_WEB_TOKEN", "s3cret")
    with TestClient(create_app(host="100.64.0.5", stream_max_seconds=0.1)) as c:
        r = c.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_query_token_sets_cookie(world: dict[str, Any]) -> None:
    with TestClient(create_app(host="0.0.0.0", token="tok")) as c:
        r = c.get("/?scope=all&token=tok", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?scope=all"
        assert TOKEN_COOKIE in r.cookies
        assert c.get("/?scope=all").status_code == 200  # cookie now carried
        c.cookies.clear()
        assert c.get("/?token=nope", follow_redirects=False).status_code == 401
        assert c.post("/events/x/feedback", data={"kind": "helpful"}).status_code == 401


def test_loopback_needs_no_token_but_guards_host_and_origin(world: dict[str, Any]) -> None:
    with TestClient(create_app(host="127.0.0.1", token="ignored")) as c:
        assert c.get("/").status_code == 200
        assert c.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200
        assert c.get("/", headers={"Host": "evil.example"}).status_code == 400  # DNS rebinding
        r = c.post(
            f"/events/{world['e_div']}/feedback",
            data={"kind": "helpful"},
            headers={"Origin": "http://evil.example"},
        )
        assert r.status_code == 403  # cross-site POST
        r = c.post(
            f"/events/{world['e_div']}/feedback",
            data={"kind": "helpful"},
            headers={"Origin": "http://testserver", "HX-Request": "true"},
        )
        assert r.status_code == 200
