from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from typer.testing import CliRunner

from papi.core.config import config_path, get_settings, reset_settings, write_default_config
from papi.core.db import connect
from papi.core.schema import llm_usage_ledger
from papi.llm import calibration, governor
from papi.llm.cli import app, parse_duration
from papi.llm.tomlconf import update_llm_config, update_section
from papi.llm.types import LlmResult

runner = CliRunner()
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def spend(conn, cost: float, at: datetime) -> None:
    r = LlmResult({"ok": True}, None, ["m"], cost, 10, 0, None, None, None)
    governor.record_usage(conn, r, task="analyst", priority="P1", now=at)


def test_update_section_preserves_comments_and_replaces_commented_defaults():
    text = (
        "# top\n[llm]\nshare_5h = 2.0  # Cap on rolling 5h\n# calibrated_at =  # When\n\n"
        '[llm.models]\nanalyst = "sonnet"\n'
    )
    out = update_section(
        text, "llm", {"share_5h": 0.75, "calibrated_at": datetime(2026, 10, 28).date(), "new": True}
    )
    assert "share_5h = 0.75  # Cap on rolling 5h" in out
    assert "calibrated_at = 2026-10-28  # When" in out
    assert out.index("new = true") < out.index("[llm.models]")
    assert out.startswith("# top\n")
    data = tomllib.loads(out)
    assert data["llm"]["models"]["analyst"] == "sonnet"


def test_update_llm_config_on_default_config_validates():
    write_default_config()
    update_llm_config({"share_5h": 1.5, "calibration_mode": False})
    s = get_settings()
    assert s.llm.share_5h == 1.5 and s.llm.calibration_mode is False
    assert "# papi configuration" in config_path().read_text()
    with pytest.raises(ValidationError):
        update_llm_config({"concurrency": "many"})
    reset_settings()
    assert get_settings().llm.share_5h == 1.5  # invalid update left the file untouched


def test_calibration_compute_and_apply(db):
    write_default_config()
    reset_settings()
    with pytest.raises(calibration.CalibrationError):
        calibration.compute(db, bar_5h_pct=5, bar_week_pct=1, now=NOW)
    calibration.start(db, now=NOW - timedelta(hours=3))
    spend(db, 9.0, NOW - timedelta(hours=4))  # before marker: ignored
    spend(db, 0.3, NOW - timedelta(hours=2))
    spend(db, 0.2, NOW - timedelta(hours=1))
    cal = calibration.compute(db, bar_5h_pct=5, bar_week_pct=1, now=NOW)
    assert cal.ledger_cost == pytest.approx(0.5) and cal.calls == 2
    assert cal.cost_per_pct_5h == pytest.approx(0.1)
    assert cal.cost_per_pct_week == pytest.approx(0.5)
    assert cal.share_5h == pytest.approx(1.5)  # 15 % of the 5h bar
    assert cal.share_week == pytest.approx(10.0)  # 20 % of the weekly bar
    calibration.apply(cal)
    llm = get_settings().llm
    assert llm.calibration_mode is False
    assert llm.share_5h == pytest.approx(1.5) and llm.share_week == pytest.approx(10.0)
    assert llm.calibration_cost_per_pct_5h == pytest.approx(0.1)
    assert str(llm.calibrated_at) == "2026-10-28"


def test_calibration_rejects_bad_input(db):
    calibration.start(db, now=NOW - timedelta(hours=1))
    with pytest.raises(calibration.CalibrationError):
        calibration.compute(db, bar_5h_pct=None, bar_week_pct=None, now=NOW)
    with pytest.raises(calibration.CalibrationError):
        calibration.compute(db, bar_5h_pct=150, bar_week_pct=None, now=NOW)
    with pytest.raises(calibration.CalibrationError):  # no usage since marker
        calibration.compute(db, bar_5h_pct=5, bar_week_pct=None, now=NOW)


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("3h", timedelta(hours=3)),
        ("90m", timedelta(minutes=90)),
        ("1h30m", timedelta(minutes=90)),
        ("2d", timedelta(days=2)),
    ],
)
def test_parse_duration(text, delta):
    assert parse_duration(text) == delta


def test_cli_pause_status_resume(db):
    db.close()
    r = runner.invoke(app, ["pause", "--for", "3h"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["status", "--json"])
    st = json.loads(r.output)
    assert st["paused"] is True and st["pause_reason"] == "manual"
    assert "DURAKLATILDI" in runner.invoke(app, ["status"]).output
    assert runner.invoke(app, ["resume"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["status", "--json"]).output)["paused"] is False
    assert runner.invoke(app, ["pause", "--for", "soon"]).exit_code != 0


def test_cli_check(fake_claude, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0, r.output
    assert "2.1.303" in r.output and "Tamam" in r.output
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1 and "ANTHROPIC_API_KEY" in r.output


def test_cli_test_command(db, fake_claude):
    db.close()
    r = runner.invoke(app, ["test"])
    assert r.exit_code == 0, r.output
    assert 'structured_output: {"ok": true}' in r.output
    assert "claude-sonnet-5-5" in r.output
    call = fake_claude.calls()[0]
    assert json.loads(call["argv"][call["argv"].index("--json-schema") + 1]) == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    with connect() as conn:
        row = conn.execute(select(llm_usage_ledger)).mappings().one()
    assert row["task"] == "test" and row["outcome"] == "ok"
    fake_claude.mode("auth")
    assert runner.invoke(app, ["test"]).exit_code == 1


def test_cli_calibrate_flow(db, fake_claude):
    write_default_config()
    update_llm_config({"claude_bin": str(fake_claude.path)})
    db.close()
    r = runner.invoke(app, ["calibrate", "--bar-5h-pct", "2"])
    assert r.exit_code == 1 and "--start" in r.output
    assert runner.invoke(app, ["calibrate", "--start"]).exit_code == 0
    assert runner.invoke(app, ["test"]).exit_code == 0
    r = runner.invoke(app, ["calibrate", "--bar-5h-pct", "2", "--bar-week-pct", "0.5", "--dry-run"])
    assert r.exit_code == 0 and "--dry-run" in r.output
    assert get_settings().llm.calibration_mode is True
    r = runner.invoke(app, ["calibrate", "--bar-5h-pct", "2", "--bar-week-pct", "0.5"])
    assert r.exit_code == 0, r.output
    llm = get_settings().llm
    assert llm.calibration_mode is False
    assert llm.calibration_cost_per_pct_5h == pytest.approx(0.0018 / 2)
    assert llm.share_week == pytest.approx(20 * 0.0018 / 0.5)
