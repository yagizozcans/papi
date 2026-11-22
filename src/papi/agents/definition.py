"""Agent definitions: ``agents/<name>/agent.toml`` + ``system.md`` (+ optional ``output.schema.json``).

An agent names its skills; the skills bind the tools (``allowed-tools``): papi tools from the registry
or Claude Code's built-in ``WebSearch`` / ``WebFetch``. The agent's tool set is the union over its
skills, validated at load time. ``prompt`` is the user message, ``{ref}`` the run subject (a KAP
disclosure number, a thesis name …). Cost is bounded per run by ``max_budget_usd``
(``claude --max-budget-usd``) and ``max_turns``.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from papi.agents.registry import ToolDef, discover
from papi.agents.skills import Skill, load_skill
from papi.core.config import resource_dir

ALLOWED_KEYS = {
    "name",
    "description",
    "model",
    "max_turns",
    "max_budget_usd",
    "timeout_s",
    "priority",
    "task",
    "prompt",
    "skills",
}
# Claude Code's own read-only tools a skill may bind next to papi tools (``allowed-tools``). They run
# inside claude, so their calls are not in the run transcript; the agent cites their URLs instead.
BUILTIN_TOOLS = ("WebSearch", "WebFetch")


DEFAULT_TIMEOUT_S = 600.0


class AgentError(ValueError):
    pass


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    model: str
    max_turns: int
    max_budget_usd: float
    timeout_s: float
    priority: str  # governor priority label: CHAT | P1..P4 | BACKLOG
    task: str  # ledger task label (≤ 16 chars)
    prompt: str  # user message template; ``{ref}`` is the run subject
    system_prompt: str
    schema: dict[str, Any] | None
    skills: tuple[Skill, ...]
    tools: tuple[ToolDef, ...]
    builtin_tools: tuple[str, ...]
    root: Path

    @property
    def agent_version(self) -> str:
        """Hash of everything that shapes behaviour: prompt, schema, model, skills, bound tool docs."""
        h = hashlib.sha256()
        parts = [self.system_prompt, self.prompt, self.model, json.dumps(self.schema, sort_keys=True)]
        for s in self.skills:
            parts += [s.name, s.description, s.body, *(s.read_file(f) for f in s.files())]
        for t in self.tools:
            parts += [t.name, t.description]
        parts += self.builtin_tools
        for part in parts:
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()[:16]

    def skill(self, name: str) -> Skill | None:
        return next((s for s in self.skills if s.name == name), None)

    def skill_index(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills)


def agents_root() -> Path:
    return resource_dir("agents")


def list_agents() -> list[str]:
    root = agents_root()
    return sorted(p.name for p in root.iterdir() if (p / "agent.toml").is_file())


def load_agent(name: str) -> AgentDef:
    root = agents_root() / name
    path = root / "agent.toml"
    if not path.is_file():
        raise AgentError(f"agent {name!r} not found ({path}); available: {', '.join(list_agents()) or '-'}")
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = set(cfg) - ALLOWED_KEYS
    if unknown:
        raise AgentError(f"{path}: unknown keys {sorted(unknown)}")
    if cfg.get("name") != name:
        raise AgentError(f"{path}: name {cfg.get('name')!r} != folder {name!r}")
    for key in ("description", "model", "max_turns", "max_budget_usd", "priority", "prompt", "skills"):
        if key not in cfg:
            raise AgentError(f"{path}: {key} is required")
    if "{ref}" not in str(cfg["prompt"]):
        raise AgentError(f"{path}: prompt must contain {{ref}}")
    task = str(cfg.get("task") or name)[:16]
    skills = tuple(load_skill(s) for s in cfg["skills"])
    registry = discover()
    bound: dict[str, ToolDef] = {}
    builtins: list[str] = []
    for s in skills:
        for tool_name in s.allowed_tools:
            if tool_name in BUILTIN_TOOLS:
                if tool_name not in builtins:
                    builtins.append(tool_name)
                continue
            tool = registry.get(tool_name)
            if tool is None:
                raise AgentError(f"skill {s.name}: allowed-tools names unknown tool {tool_name!r}")
            if tool.effect != "read":
                raise AgentError(
                    f"skill {s.name}: tool {tool_name!r} has effect {tool.effect!r}; "
                    "only read tools are bindable"
                )
            bound[tool_name] = tool
    schema_path = root / "output.schema.json"
    return AgentDef(
        name=name,
        description=str(cfg["description"]),
        model=str(cfg["model"]),
        max_turns=int(cfg["max_turns"]),
        max_budget_usd=float(cfg["max_budget_usd"]),
        timeout_s=float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S)),
        priority=str(cfg["priority"]),
        task=task,
        prompt=str(cfg["prompt"]),
        system_prompt=(root / "system.md").read_text(encoding="utf-8"),
        schema=json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else None,
        skills=skills,
        tools=tuple(bound.values()),
        builtin_tools=tuple(builtins),
        root=root,
    )
