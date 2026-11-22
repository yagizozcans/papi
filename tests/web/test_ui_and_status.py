"""``GET /v1/status`` (typed), the SPA host at ``/`` and ``papi web openapi``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from papi.cli import app as cli_app
from papi.web.app import create_app


def test_status_shape_and_schema(client: TestClient) -> None:
    r = client.get("/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "version", "running", "heartbeat_at", "heartbeat_stale", "kap", "jobs", "lanes",
        "governor", "telegram", "last_backup_at", "server_time",
    }  # fmt: skip
    assert set(body["kap"]) == {"last_poll_at", "last_poll_ok_at", "lag_seconds"}
    assert body["governor"]["paused"] is False
    assert isinstance(body["jobs"], dict) and isinstance(body["lanes"], dict)
    schemas = client.get("/v1/openapi.json").json()["components"]["schemas"]
    assert "StatusOut" in schemas and "KapStatus" in schemas


def test_status_kap_lag_and_backup(client: TestClient, db: Any) -> None:
    from papi.core.state import set_state
    from papi.core.timeutil import now_utc
    from papi.run.backup import LAST_BACKUP_KEY

    at = now_utc().isoformat()
    set_state(db, "kap:last_poll_ok_at", at)
    set_state(db, LAST_BACKUP_KEY, {"at": at, "path": "x", "bytes": 1})
    db.commit()
    body = client.get("/v1/status").json()
    assert body["kap"]["last_poll_ok_at"] == at
    assert 0 <= body["kap"]["lag_seconds"] < 60
    assert body["last_backup_at"] == at


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>spa</html>")
    (tmp_path / "assets" / "a.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


def test_spa_serving(world: Any, ui: Path) -> None:
    with TestClient(create_app(ui_dir=ui)) as c:
        for p in ("/", "/feed", "/feed/x", "/i/THYAO", "/portfolio"):  # client-side routes -> fallback
            r = c.get(p)
            assert r.status_code == 200 and "spa" in r.text and r.headers["cache-control"] == "no-cache", p
        r = c.get("/assets/a.js")
        assert "javascript" in r.headers["content-type"] and "max-age=31536000" in r.headers["cache-control"]
        assert c.get("/favicon.svg").headers["content-type"].startswith("image/svg")
        assert c.get("/assets/missing.js").status_code == 404  # missing asset: no HTML fallback


def test_spa_does_not_shadow_api_or_legacy(world: Any, ui: Path) -> None:
    with TestClient(create_app(ui_dir=ui)) as c:
        r = c.get("/v1/nope")
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
        assert "<html>spa" not in c.get("/v1/openapi.json").text
        r = c.get("/legacy/")
        assert r.status_code == 200 and "<html>spa" not in r.text and 'class="brand"' in r.text
        assert c.get("/legacy/nope").status_code in (404,) and "<html>spa" not in c.get("/legacy/nope").text
        assert c.get("/raw/" + "0" * 64).status_code == 404  # real route, not the SPA
        assert "<html>spa" not in c.get("/raw/" + "0" * 64).text


def test_old_urls_redirect(world: Any, ui: Path) -> None:
    cases = {
        "/app": "/",
        "/app/": "/",
        "/app/feed/x?a=1": "/feed/x?a=1",
        f"/events/{world['e_div']}": f"/feed/{world['e_div']}",
        "/company/THYAO": "/i/THYAO",
        "/ops": "/system",
        "/theses": "/legacy/theses",
        "/agents": "/legacy/agents",
    }
    with TestClient(create_app(ui_dir=ui)) as c:
        for old, new in cases.items():
            r = c.get(old, follow_redirects=False)
            assert r.status_code == 308 and r.headers["location"] == new, old


def test_legacy_pages_fragments_and_raw(client: TestClient, world: dict[str, Any]) -> None:
    r = client.get("/legacy/")
    assert r.status_code == 200 and f'href="/legacy/events/{world["e_thy"]}"' in r.text
    assert 'href="/"' in r.text  # link to the new UI
    assert 'data-poll="/legacy/stream/poll' in r.text and "/static/app.js" in r.text
    assert client.get("/legacy/portfolio").status_code == 200
    assert client.get(f"/legacy/events/{world['e_thy']}").status_code == 200
    assert client.get(f"/legacy/fragments/events/{world['e_thy']}").status_code == 200
    poll = client.get("/legacy/stream/poll")
    assert poll.status_code == 200 and "cursor" in poll.json()
    assert client.get("/legacy/ops").status_code == 200


def test_spa_path_traversal(world: Any, ui: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    secret = ui.parent / "secret.txt"
    secret.write_text("TOP-SECRET")
    with TestClient(create_app(ui_dir=ui)) as c:
        for p in (
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/..%2fsecret.txt",
            "/assets/../../secret.txt",
        ):
            assert "TOP-SECRET" not in c.get(p).text


def test_spa_missing_build(world: Any, tmp_path: Path) -> None:
    for d in (tmp_path / "nope", tmp_path):
        with TestClient(create_app(ui_dir=d)) as c:
            for p in ("/", "/feed"):
                r = c.get(p)
                assert r.status_code == 503
                assert (
                    "npm run build" in r.text
                    and 'href="/legacy/"' in r.text
                    and "http" not in r.text.replace('href="/legacy/"', "")
                )


def test_spa_requires_token_on_public_bind(world: Any, ui: Path) -> None:
    with TestClient(create_app(host="0.0.0.0", token="tok", ui_dir=ui)) as c:
        assert c.get("/").status_code == 401
        assert c.get("/feed").status_code == 401
        assert c.get("/", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_openapi_cli(world: Any, tmp_path: Path) -> None:
    r = CliRunner().invoke(cli_app, ["web", "openapi"])
    assert r.exit_code == 0, r.output
    assert r.output.endswith("\n") and "/v1/status" in json.loads(r.output)["paths"]
    out = tmp_path / "o.json"
    r2 = CliRunner().invoke(cli_app, ["web", "openapi", "--out", str(out)])
    assert r2.exit_code == 0 and out.read_text() == r.output
