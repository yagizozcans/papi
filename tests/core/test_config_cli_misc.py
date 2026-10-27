from __future__ import annotations

import json
import logging
import tomllib

import pytest
from typer.testing import CliRunner

from papi.core import fts, metrics
from papi.core.config import (
    Settings,
    get_settings,
    get_telegram_token,
    render_default_config,
    reset_settings,
)
from papi.core.logging import setup_logging
from papi.core.state import delete_state, get_state, set_state


def test_defaults(settings, papi_home):
    assert settings.paths.home == papi_home.resolve()
    assert settings.paths.db == papi_home.resolve() / "papi.db"
    assert settings.paths.raw == papi_home.resolve() / "raw"
    assert settings.kap.base_url == "https://www.kap.org.tr"
    assert settings.kap.session_poll_seconds == 30 and settings.kap.offhours_poll_seconds == 180
    assert "contact:" in settings.kap.resolved_user_agent
    assert settings.llm.backend == "claude_code_cli" and settings.llm.concurrency == 1
    assert settings.llm.models.chat == "sonnet"
    assert settings.web.host == "127.0.0.1" and settings.web.port == 8765
    assert settings.pack_dir.name == "tr"


def test_rendered_default_config_round_trips(papi_home):
    data = tomllib.loads(render_default_config())
    data.setdefault("paths", {})["home"] = papi_home
    parsed = Settings.model_validate(data)
    defaults = Settings(paths={"home": papi_home})
    assert parsed.model_dump(exclude={"packs"}) == defaults.model_dump(exclude={"packs"})


def test_config_file_overrides_and_extra_sections(papi_home):
    (papi_home / "config.toml").write_text(
        '[kap]\ncontact = "me@example.org"\n[llm.models]\nchecker = "haiku"\n'
        '[[llm.focus]]\ndays = ["mon"]\nstart = "09:00"\nend = "12:00"\nmax_priority = "P1"\n'
        '[paths]\nraw = "/tmp/elsewhere-raw"\n[sim]\nhorizons = [1, 5]\n',
        encoding="utf-8",
    )
    reset_settings()
    s = get_settings()
    assert s.kap.resolved_user_agent.endswith("contact: me@example.org)")
    assert s.llm.models.checker == "haiku" and s.llm.models.analyst == "sonnet"
    assert s.llm.focus[0].days == ["mon"]
    assert str(s.paths.raw) == "/tmp/elsewhere-raw"
    assert s.section("sim") == {"horizons": [1, 5]} and s.section("missing") == {}


def test_invalid_config_rejected(papi_home):
    (papi_home / "config.toml").write_text("[kap]\nunknown_key = 1\n", encoding="utf-8")
    reset_settings()
    with pytest.raises(ValueError):
        get_settings()


def test_telegram_token_env(monkeypatch):
    assert get_telegram_token() is None  # null keyring backend in tests
    monkeypatch.setenv("PAPI_TELEGRAM_TOKEN", "123:abc")
    assert get_telegram_token() == "123:abc"


def test_cli_init_twice_and_show(papi_home):
    from papi.cli import app

    runner = CliRunner()
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    assert "seeded" in first.output
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0 and "already initialised" in second.output
    shown = runner.invoke(app, ["config", "show", "--json"])
    assert shown.exit_code == 0
    data = json.loads(shown.output)
    assert data["secrets"]["telegram_token"] == "unset"
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert runner.invoke(app, ["db", "current"]).output.strip() == "0001"


def test_cli_mount_tolerates_only_missing_cli_module(monkeypatch):
    import importlib

    import typer

    from papi import cli

    real_import = importlib.import_module
    sub = typer.Typer()

    @sub.command()
    def hello() -> None:
        pass

    def fake_import(name, *a, **kw):
        if name == "papi.kb.cli":
            mod = type("M", (), {"app": sub})
            return mod
        if name == "papi.triage.cli":
            raise ModuleNotFoundError("No module named 'yaml_missing'", name="yaml_missing")
        if name.endswith(".cli"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    root = typer.Typer()
    with pytest.raises(ModuleNotFoundError):
        cli.mount_subcommands(root)  # broken dependency inside triage.cli must surface
    monkeypatch.setattr(
        cli, "SUBCOMMAND_MODULES", tuple(m for m in cli.SUBCOMMAND_MODULES if m[0] != "triage")
    )
    assert cli.mount_subcommands(typer.Typer()) == ["kb"]


def test_state_metrics_and_fts(db):
    set_state(db, "kap:last_index", {"index": 1689001})
    set_state(db, "kap:last_index", {"index": 1689002})
    assert get_state(db, "kap:last_index") == {"index": 1689002}
    delete_state(db, "kap:last_index")
    assert get_state(db, "kap:last_index", 0) == 0

    metrics.record("kap.latency_s", 12.5, conn=db, stage="normalize")
    rows = metrics.series(db, "kap.latency_s")
    assert rows[0]["value"] == 12.5 and rows[0]["labels"] == {"stage": "normalize"}

    fts.index_text(
        db, "disclosure_fts", "d1", "ASELSAN Yeni İş İlişkisi: Savunma Sanayii Başkanlığı ile sözleşme"
    )
    fts.index_text(db, "disclosure_fts", "d2", "Kâr Payı Dağıtım İşlemlerine İlişkin Bildirim")
    assert [h.id for h in fts.search(db, "disclosure_fts", "sozlesme")] == ["d1"]
    assert [h.id for h in fts.search(db, "disclosure_fts", "İLİŞKİN")] == ["d2"]
    assert [h.id for h in fts.search(db, "disclosure_fts", "kar pay")] == ["d2"]  # prefix on last token
    assert fts.search(db, "disclosure_fts", '"; DROP') == []
    fts.index_text(db, "disclosure_fts", "d1", "değişti")
    assert fts.search(db, "disclosure_fts", "aselsan") == []


def test_logging_writes_json(settings):
    setup_logging(settings, console=False)
    log = logging.getLogger("papi.test")
    log.info("hello %s", "dünya", extra={"data": {"k": 1}})
    for h in logging.getLogger("papi").handlers:
        h.flush()
    line = (settings.paths.logs / "papi.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["msg"] == "hello dünya" and rec["data"] == {"k": 1} and rec["level"] == "INFO"
    for h in list(logging.getLogger("papi").handlers):
        logging.getLogger("papi").removeHandler(h)
        h.close()
