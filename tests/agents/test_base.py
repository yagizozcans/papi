"""Agent base system: skill loading, tool guard, definition validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from papi.agents import definition, skills
from papi.agents.mcp_server import build_server
from papi.agents.registry import ENV_TRANSCRIPT, discover, guard
from papi.agents.runner import allowed_tools


def _write_skill(root: Path, name: str, tools: list[str], *, desc: str = "does things") -> None:
    folder = root / name
    (folder / "references").mkdir(parents=True)
    (folder / "references" / "a.md").write_text("ref", encoding="utf-8")
    tool_lines = "".join(f"  - {t}\n" for t in tools)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nallowed-tools:\n{tool_lines}---\nbody\n", encoding="utf-8"
    )


def test_skill_reads_only_listed_files(tmp_path: Path) -> None:
    _write_skill(tmp_path, "s", [])
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    skill = skills.load_skill("s", tmp_path)
    assert skill.read_file("references/a.md") == "ref"
    with pytest.raises(skills.SkillError):
        skill.read_file("../secret.txt")


def test_skill_name_must_match_folder(tmp_path: Path) -> None:
    _write_skill(tmp_path, "s", [])
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "SKILL.md").write_text((tmp_path / "s" / "SKILL.md").read_text(), encoding="utf-8")
    with pytest.raises(skills.SkillError, match="folder"):
        skills.load_skill("t", tmp_path)


def test_guard_turns_exceptions_and_oversize_into_error_dicts(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "t.jsonl"
    monkeypatch.setenv(ENV_TRANSCRIPT, str(log))

    def boom(x: int) -> dict:
        raise RuntimeError("nope")

    def big(x: int) -> dict:
        return {"success": True, "text": "a" * 100}

    assert guard("boom", boom)(x=1) == {"success": False, "error": "RuntimeError: nope"}
    out = guard("big", big, max_chars=50)(x=1)
    assert out["success"] is False and "max 50" in out["error"]
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["boom", "big"]


def test_agent_tool_set_is_union_of_skill_bindings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skills, "skills_root", lambda: tmp_path / "skills")
    monkeypatch.setattr(definition, "agents_root", lambda: tmp_path / "agents")
    _write_skill(tmp_path / "skills", "one", ["kb_search", "WebSearch"])
    _write_skill(tmp_path / "skills", "two", ["kb_search", "portfolio_position", "WebSearch"])
    adir = tmp_path / "agents" / "a"
    adir.mkdir(parents=True)
    (adir / "system.md").write_text("sys", encoding="utf-8")
    (adir / "agent.toml").write_text(
        'name="a"\ndescription="d"\nmodel="sonnet"\nmax_turns=3\nmax_budget_usd=0.1\n'
        'priority="P2"\nprompt="Do {ref}."\nskills=["one","two"]\n',
        encoding="utf-8",
    )
    agent = definition.load_agent("a")
    assert sorted(t.name for t in agent.tools) == ["kb_search", "portfolio_position"]
    assert agent.builtin_tools == ("WebSearch",)
    # built-ins are allowed in claude, but the MCP server exposes only the bound papi tools + skill tools
    assert "WebSearch" in allowed_tools(agent)
    import asyncio

    names = {t.name for t in asyncio.run(build_server(agent).list_tools())}
    assert names == {"use_skill", "read_skill_file", "kb_search", "portfolio_position"}


def test_skill_binding_unknown_tool_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skills, "skills_root", lambda: tmp_path / "skills")
    monkeypatch.setattr(definition, "agents_root", lambda: tmp_path / "agents")
    _write_skill(tmp_path / "skills", "one", ["no_such_tool"])
    adir = tmp_path / "agents" / "a"
    adir.mkdir(parents=True)
    (adir / "system.md").write_text("sys", encoding="utf-8")
    (adir / "agent.toml").write_text(
        'name="a"\ndescription="d"\nmodel="sonnet"\nmax_turns=3\nmax_budget_usd=0.1\n'
        'priority="P2"\nprompt="Do {ref}."\nskills=["one"]\n',
        encoding="utf-8",
    )
    with pytest.raises(definition.AgentError, match="no_such_tool"):
        definition.load_agent("a")


def test_shipped_agents_load_and_tools_are_unique() -> None:
    assert discover()  # duplicate names / missing docstrings raise
    for name in definition.list_agents():
        agent = definition.load_agent(name)
        assert agent.tools, name
