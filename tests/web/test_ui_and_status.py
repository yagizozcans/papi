"""``GET /v1/status`` (typed), the ``/app`` SPA host and ``papi web openapi``."""

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
        r = c.get("/app", follow_redirects=False)
        assert r.status_code in (301, 307) and r.headers["location"] == "/app/"
        r = c.get("/app/")
        assert r.status_code == 200 and "spa" in r.text and r.headers["cache-control"] == "no-cache"
        r = c.get("/app/feed")  # client-side route -> fallback
        assert r.status_code == 200 and "spa" in r.text
        r = c.get("/app/assets/a.js")
        assert "javascript" in r.headers["content-type"] and "max-age=31536000" in r.headers["cache-control"]
        assert c.get("/app/favicon.svg").headers["content-type"].startswith("image/svg")
        r = c.get("/app/assets/missing.js")
        assert r.status_code == 404  # missing asset: no HTML fallback


def test_spa_path_traversal(world: Any, ui: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    secret = ui.parent / "secret.txt"
    secret.write_text("TOP-SECRET")
    with TestClient(create_app(ui_dir=ui)) as c:
        for p in (
            "/app/../secret.txt",
            "/app/%2e%2e/secret.txt",
            "/app/..%2fsecret.txt",
            "/app/assets/../../secret.txt",
        ):
            assert "TOP-SECRET" not in c.get(p).text


def test_spa_missing_build(world: Any, tmp_path: Path) -> None:
    for d in (tmp_path / "nope", tmp_path):
        with TestClient(create_app(ui_dir=d)) as c:
            for p in ("/app/", "/app/feed"):
                r = c.get(p)
                assert r.status_code == 503
                assert (
                    "npm run build" in r.text
                    and 'href="/"' in r.text
                    and "http" not in r.text.replace('href="/"', "")
                )


def test_spa_requires_token_on_public_bind(world: Any, ui: Path) -> None:
    with TestClient(create_app(host="0.0.0.0", token="tok", ui_dir=ui)) as c:
        assert c.get("/app/").status_code == 401
        assert c.get("/app/feed").status_code == 401
        assert c.get("/app/", headers={"Authorization": "Bearer tok"}).status_code == 200


def test_openapi_cli(world: Any, tmp_path: Path) -> None:
    r = CliRunner().invoke(cli_app, ["web", "openapi"])
    assert r.exit_code == 0, r.output
    assert r.output.endswith("\n") and "/v1/status" in json.loads(r.output)["paths"]
    out = tmp_path / "o.json"
    r2 = CliRunner().invoke(cli_app, ["web", "openapi", "--out", str(out)])
    assert r2.exit_code == 0 and out.read_text() == r.output
