from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from papi.core.config import get_settings
from papi.llm.backend import (
    ClaudeCodeCliBackend,
    check_environment,
    classify_output,
    get_backend,
    parse_reset_time,
)

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _complete(**kw):
    args = {
        "task": "analyst",
        "prompt": "BAĞLAM paketi",
        "system_prompt": "Sen bir analistsin.",
        "schema": SCHEMA,
    }
    args.update(kw)
    return ClaudeCodeCliBackend(grace_s=0.5).complete(**args)


def test_argv_env_cwd_and_stdin(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PAPI_PROBE", "kept")
    res = _complete()
    assert res.error_kind is None, res.error
    (call,) = fake_claude.calls()
    s = get_settings()
    assert call["argv"] == [
        "-p",
        "--model",
        "sonnet",
        "--system-prompt-file",
        call["argv"][4],
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers": {}}',
        "--setting-sources",
        "project",
        "--no-session-persistence",
        "--max-turns",
        "3",
        "--output-format",
        "json",
        "--verbose",
        "--json-schema",
        json.dumps(SCHEMA),
    ]
    prompt_file = call["argv"][4]
    assert prompt_file.startswith(str(s.paths.home / "tmp" / "llm"))
    assert not prompt_file.startswith(str(s.paths.claude_sandbox))
    assert call["system_prompt"] == "Sen bir analistsin."
    assert call["stdin"] == "BAĞLAM paketi"
    assert call["cwd"] == str(s.paths.claude_sandbox.resolve())
    assert call["sandbox_entries"] == []
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert "CLAUDECODE" not in call["env"]
    assert call["env"]["PAPI_PROBE"] == "kept"
    # temp system prompt file is removed after the call
    assert not list((s.paths.home / "tmp" / "llm").iterdir())


def test_argv_options_model_turns_mcp_tools_and_no_schema(fake_claude, llm_config):
    llm_config(raw='[llm.models]\nchat = "haiku"')
    fake_claude.mode("ok")
    res = _complete(
        task="chat",
        schema=None,
        mcp_config={"mcpServers": {"papi": {"command": "papi", "args": ["mcp"]}}},
        allowed_tools=["mcp__papi__search", "mcp__papi__lookup"],
    )
    assert res.error_kind is None
    argv = fake_claude.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--max-turns") + 1] == "8"  # chat_max_turns
    assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["papi"]["command"] == "papi"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__papi__search,mcp__papi__lookup"
    assert "--json-schema" not in argv
    assert "--strict-mcp-config" in argv

    _complete(model="opus", max_turns=5)
    argv = fake_claude.calls()[1]["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-turns") + 1] == "5"


def test_success_parsing(fake_claude):
    res = _complete()
    assert res.ok
    assert res.structured_output == {"ok": True}
    assert res.text == '{"ok":true}'
    assert res.model_ids == ["claude-sonnet-5-5"]
    assert res.cost_usd_est == pytest.approx(0.0018)
    assert res.exit_code == 0
    assert res.raw_json["subtype"] == "success"
    assert res.rate_limit["unifiedWindows"]["five_hour"]["utilization"] == 0.2
    assert res.duration_ms >= 0


def test_single_object_output_also_parsed(fake_claude):
    fake_claude.mode("ok_single")
    res = _complete()
    assert res.ok and res.structured_output == {"ok": True} and res.rate_limit is None


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        ("no_structured", "schema"),
        ("invalid_structured", "schema"),
        ("schema_retries", "schema"),
        ("limit", "limit"),
        ("limit_rejected", "limit"),
        ("auth", "auth"),
        ("max_turns", "error"),
        ("garbage", "error"),
    ],
)
def test_error_classification(fake_claude, mode, kind):
    fake_claude.mode(mode)
    res = _complete()
    assert res.error_kind == kind, res.error
    assert res.structured_output is None
    assert res.error


def test_limit_reset_times(fake_claude):
    fake_claude.mode("limit")
    assert _complete().limit_reset_at == datetime.fromtimestamp(1793158200, UTC)
    fake_claude.mode("limit_rejected")
    assert _complete().limit_reset_at == datetime.fromtimestamp(1793160000, UTC)


def test_invalid_structured_message_names_path(fake_claude):
    fake_claude.mode("invalid_structured")
    res = _complete()
    assert "ok" in res.error and "şema" in res.error


def test_garbage_output_keeps_stderr(fake_claude):
    fake_claude.mode("garbage")
    res = _complete()
    assert res.exit_code == 2 and "boom" in res.error and res.raw_json is None


def test_timeout_sends_sigint(fake_claude):
    fake_claude.mode("hang")
    res = _complete(timeout_s=1.0)
    assert res.error_kind == "timeout"
    assert fake_claude.signals.read_text().split() == ["READY", "SIGINT"]


def test_timeout_escalates_to_sigterm(fake_claude):
    fake_claude.mode("hang_ignore_int")
    res = _complete(timeout_s=1.0)
    assert res.error_kind == "timeout"
    assert fake_claude.signals.read_text().split() == ["READY", "SIGINT", "SIGTERM"]


def test_missing_binary_is_error(llm_config, tmp_path):
    llm_config(claude_bin=str(tmp_path / "nope" / "claude"))
    res = _complete()
    assert res.error_kind == "error" and "çalıştırılamadı" in res.error


def test_get_backend():
    assert isinstance(get_backend(), ClaudeCodeCliBackend)


def test_classify_success_without_schema_ok():
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "merhaba"})
    res = classify_output(exit_code=0, stdout=out, stderr="", schema=None, duration_ms=5)
    assert res.ok and res.text == "merhaba" and res.cost_usd_est == 0.0


def test_classify_cost_falls_back_to_model_usage():
    out = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "modelUsage": {"a": {"costUSD": 0.25}, "b": {"costUSD": 0.5}},
        }
    )
    res = classify_output(exit_code=0, stdout=out, stderr="", schema=None, duration_ms=5)
    assert res.cost_usd_est == pytest.approx(0.75) and res.model_ids == ["a", "b"]


def test_classify_limit_from_stderr_text():
    res = classify_output(
        exit_code=1, stdout="", stderr="5-hour limit reached ∙ resets 3pm", schema=None, duration_ms=1
    )
    assert res.error_kind == "limit"


def test_parse_reset_time_variants():
    now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)  # 12:00 Istanbul
    ist = ZoneInfo("Europe/Istanbul")
    assert parse_reset_time("usage limit reached|1793158200", now=now) == datetime.fromtimestamp(
        1793158200, UTC
    )
    assert parse_reset_time("resets 3pm (Europe/Istanbul)", now=now) == datetime(
        2026, 10, 28, 15, 0, tzinfo=ist
    )
    # already past today → tomorrow
    assert parse_reset_time("resets 11am", now=now, local_tz=ist) == datetime(2026, 10, 29, 11, 0, tzinfo=ist)
    assert parse_reset_time("resets 9:30pm", now=now, local_tz=ist) == datetime(
        2026, 10, 28, 21, 30, tzinfo=ist
    )
    assert parse_reset_time("Weekly limit reached ∙ resets Oct 30, 9am", now=now, local_tz=ist) == datetime(
        2026, 10, 30, 9, 0, tzinfo=ist
    )
    assert parse_reset_time("no time here", now=now) is None


def test_check_environment(fake_claude, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert check_environment() == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    problems = check_environment()
    assert len(problems) == 1 and "ANTHROPIC_API_KEY" in problems[0]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    (get_settings().paths.claude_sandbox / "CLAUDE.md").write_text("x")
    problems = check_environment()
    assert len(problems) == 1 and "boş değil" in problems[0]


def test_check_environment_missing_binary(llm_config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm_config(claude_bin="definitely-not-a-claude-binary")
    problems = check_environment()
    assert len(problems) == 1 and "bulunamadı" in problems[0]
