"""Fixtures for the LLM slice: a fake ``claude`` executable and config helpers."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from papi.core.config import config_path, reset_settings
from papi.llm.tomlconf import update_llm_config

FAKE_CLAUDE = r'''#!{python}
"""Fake Claude Code CLI. Mode from $FAKE_CLAUDE_MODE; records argv/env/cwd/stdin to $FAKE_CLAUDE_RECORD."""
import json, os, signal, sys, time

argv = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
record = os.environ.get("FAKE_CLAUDE_RECORD")


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


if argv == ["--version"]:
    print("2.1.303 (Claude Code)")
    sys.exit(0)

stdin = sys.stdin.read()
spf = flag("--system-prompt-file")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": stdin,
            "system_prompt": open(spf, encoding="utf-8").read() if spf and os.path.exists(spf) else None,
            "sandbox_entries": sorted(os.listdir(os.getcwd())),
        }) + "\n")

init = {"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": [],
        "model": "claude-sonnet-5-5", "apiKeySource": "none"}
rate = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1793158200,
        "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1793158200},
                           "seven_day": {"utilization": 0.35, "resetsAt": 1793512800}}}}
usage = {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 52, "costUSD": 0.0018}}


def result(**kw):
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
            "duration_ms": 1367, "result": "{\"ok\":true}", "total_cost_usd": 0.0018,
            "modelUsage": usage, "api_error_status": None, "session_id": "s"}
    base.update(kw)
    return base


def emit(res, code=0, rate_msg=rate):
    print(json.dumps([init, rate_msg, res]))
    sys.exit(code)


if mode == "ok":
    emit(result(structured_output={"ok": True}))
if mode == "ok_single":
    print(json.dumps(result(structured_output={"ok": True})))
    sys.exit(0)
if mode == "no_structured":
    emit(result())
if mode == "invalid_structured":
    emit(result(structured_output={"ok": "yes"}))
if mode == "schema_retries":
    emit(result(subtype="error_max_structured_output_retries", is_error=True, result=None), code=1)
if mode == "limit":
    emit(result(is_error=True, result="Claude AI usage limit reached|1793158200", total_cost_usd=0,
                modelUsage={}), code=1)
if mode == "limit_rejected":
    rejected = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1793160000}}
    emit(result(is_error=True, result="API Error", api_error_status=None, total_cost_usd=0, modelUsage={}),
         code=1, rate_msg=rejected)
if mode == "auth":
    emit(result(is_error=True, result="Invalid API key · Please run /login", total_cost_usd=0, modelUsage={}),
         code=1)
if mode == "max_turns":
    emit(result(subtype="error_max_turns", is_error=True, result=None), code=1)
if mode == "garbage":
    print("not json")
    sys.stderr.write("boom")
    sys.exit(2)
if mode in ("hang", "hang_ignore_int"):
    marker = os.environ["FAKE_CLAUDE_SIGNALS"]

    def on_signal(signum, frame):
        with open(marker, "a") as fh:
            fh.write(signal.Signals(signum).name + "\n")
        if mode == "hang" or signum == signal.SIGTERM:
            sys.exit(128 + signum)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    with open(marker, "a") as fh:
        fh.write("READY\n")
    while True:
        time.sleep(0.05)
sys.stderr.write("unknown mode " + mode)
sys.exit(3)
'''


@dataclass
class FakeClaude:
    path: Path
    record: Path
    signals: Path
    monkeypatch: pytest.MonkeyPatch

    def mode(self, mode: str) -> None:
        self.monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)

    def calls(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


@pytest.fixture
def llm_config(papi_home: Path) -> Callable[..., None]:
    """Set ``[llm]`` scalar keys (and optional raw TOML appended, e.g. ``[[llm.focus]]``)."""

    def apply(raw: str = "", **values: Any) -> None:
        if values:
            update_llm_config(values)
        if raw:
            path = config_path()
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n" + raw + "\n")
        reset_settings()

    return apply


@pytest.fixture
def fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llm_config: Callable[..., None]
) -> FakeClaude:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE.replace("{python}", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    record = tmp_path / "claude-calls.jsonl"
    signals = tmp_path / "claude-signals.txt"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_SIGNALS", str(signals))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    llm_config(claude_bin=str(path))
    return FakeClaude(path=path, record=record, signals=signals, monkeypatch=monkeypatch)
