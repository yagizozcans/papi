"""``papi telegram`` CLI against the fake Bot API (no network)."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from sqlalchemy import Connection, select
from typer.testing import CliRunner

from papi.core import db as core_db
from papi.core.config import get_settings, reset_settings
from papi.core.schema import alert_rule
from papi.core.state import get_state
from papi.telegram.cli import app
from papi.telegram.poller import OFFSET_KEY
from papi.telegram.setup import update_config_text
from tests.telegram.helpers import TOKEN, FakeTelegram, message_update

runner = CliRunner()


@pytest.fixture
def router(db: Connection) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as r:
        yield r


def test_setup_captures_ids_writes_config_and_seeds_rules(
    router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTelegram(router)
    fake.updates = [message_update(41, "/start", user_id=555, chat_id=555)]
    stored: dict[str, Any] = {}
    monkeypatch.setattr("papi.telegram.cli.set_secret", lambda k, v: stored.update({k: v}))
    result = runner.invoke(app, ["setup", "--token", TOKEN, "--yes", "--wait", "5"])
    assert result.exit_code == 0, result.output
    assert "Bot: @papi_test_bot" in result.output
    assert "kullanıcı id 555, sohbet id 555" in result.output
    assert stored == {"telegram_token": TOKEN}
    reset_settings()
    cfg = tomllib.loads(get_settings().config_path.read_text(encoding="utf-8"))
    assert cfg["telegram"]["enabled"] is True
    assert cfg["telegram"]["allowed_user_id"] == 555 and cfg["telegram"]["chat_id"] == 555
    assert cfg["telegram"]["poll_timeout_s"] == 30  # other keys kept
    with core_db.connect() as conn:
        names = conn.execute(select(alert_rule.c.name)).scalars().all()
        assert get_state(conn, OFFSET_KEY) == 42
    assert len(names) == 4
    welcome = fake.bodies()[-1]
    assert welcome["chat_id"] == 555 and "papi bağlandı" in welcome["text"]


def test_setup_rejects_bad_token(router: respx.MockRouter) -> None:
    import httpx

    router.post(url__regex=r".*/getMe$").mock(
        return_value=httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
    )
    result = runner.invoke(app, ["setup", "--token", "bad", "--yes"])
    assert result.exit_code == 1 and "Token doğrulanamadı" in result.output


def test_setup_declined(router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTelegram(router)
    fake.updates = [message_update(1, "hi", user_id=555, chat_id=555)]
    monkeypatch.setattr("papi.telegram.cli.set_secret", lambda k, v: None)
    result = runner.invoke(app, ["setup", "--token", TOKEN, "--wait", "5"], input="n\n")
    assert result.exit_code == 1
    reset_settings()
    assert get_settings().telegram.allowed_user_id is None


def test_test_send_and_poll_once(db: Connection, fake_tg: FakeTelegram) -> None:
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0, result.output
    assert "papi test mesajı" in fake_tg.last_text()
    result = runner.invoke(app, ["send", "<b>x</b> & y"])
    assert result.exit_code == 0
    assert fake_tg.last_text() == "&lt;b&gt;x&lt;/b&gt; &amp; y"
    fake_tg.updates = [message_update(1, "/yardim")]
    result = runner.invoke(app, ["poll-once"])
    assert result.exit_code == 0, result.output
    assert "mesaj 1" in result.output and "işlenen mesaj: 1" in result.output
    assert "/mutabakat" in fake_tg.last_text()


def test_test_without_token(db: Connection) -> None:
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 1 and "Gönderilemedi" in result.output


def test_rules_commands(db: Connection) -> None:
    assert (
        runner.invoke(app, ["rules", "add", "Savunma", 'sector = "savunma" AND materiality >= 60']).exit_code
        == 0
    )
    bad = runner.invoke(app, ["rules", "add", "Kötü", "foo = 1"])
    assert bad.exit_code == 1 and "bilinmeyen alan" in bad.output
    dup = runner.invoke(app, ["rules", "add", "Savunma", 'scope = "all"'])
    assert dup.exit_code == 1 and "zaten var" in dup.output
    quiet = runner.invoke(
        app, ["rules", "add", "Gece", 'scope = "all"', "-c", "telegram", "--quiet", "22:00-08:00"]
    )
    assert quiet.exit_code == 0, quiet.output
    assert runner.invoke(app, ["rules", "disable", "Savunma"]).exit_code == 0
    assert runner.invoke(app, ["rules", "disable", "yok"]).exit_code == 1
    listing = runner.invoke(app, ["rules", "list"])
    assert "Savunma" in listing.output and "kapalı" in listing.output and "22:00-08:00" in listing.output
    assert runner.invoke(app, ["rules", "enable", "Savunma"]).exit_code == 0
    with core_db.connect() as conn:
        rows = {r["name"]: r for r in conn.execute(select(alert_rule)).mappings()}
    assert rows["Savunma"]["enabled"] is True
    assert rows["Gece"]["channels"] == ["telegram"] and rows["Gece"]["quiet_hours"] == {
        "start": "22:00",
        "end": "08:00",
    }


def test_update_config_text() -> None:
    text = "[web]\nport = 1\n\n[telegram]\nenabled = false  # x\n# allowed_user_id =  # only me\n\n[alerts]\n"
    out = update_config_text(text, "telegram", {"enabled": True, "allowed_user_id": 5, "chat_id": 6})
    data = tomllib.loads(out)
    assert data["telegram"] == {"enabled": True, "allowed_user_id": 5, "chat_id": 6}
    assert data["web"]["port"] == 1 and "alerts" in data
    assert tomllib.loads(update_config_text("", "telegram", {"chat_id": 1}))["telegram"]["chat_id"] == 1
