"""``papi web serve``."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from papi.cli import app


def test_serve_runs_uvicorn_with_config_defaults(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    calls: list[dict[str, Any]] = []
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda application, **kw: calls.append({"app": application, **kw}))
    result = CliRunner().invoke(app, ["web", "serve"])
    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8765
    assert "http://127.0.0.1:8765/" in result.output
    result = CliRunner().invoke(app, ["web", "serve", "--port", "9000", "--host", "localhost"])
    assert result.exit_code == 0 and calls[1]["port"] == 9000 and calls[1]["host"] == "localhost"


def test_serve_refuses_public_bind_without_token(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("must not start"))
    result = CliRunner().invoke(app, ["web", "serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2 and "token" in result.output


def test_serve_public_bind_with_token(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    import uvicorn

    calls: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kw: calls.append(kw))
    monkeypatch.setenv("PAPI_WEB_TOKEN", "t")
    result = CliRunner().invoke(app, ["web", "serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0 and "bearer token zorunlu" in result.output and calls[0]["host"] == "0.0.0.0"
