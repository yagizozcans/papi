"""Skills: ``SKILL.md`` + ``references/`` (+ ``assets/``, ``scripts/`` — readable, never executed).

Frontmatter: ``name`` (= folder name), ``description`` (what the agent sees every turn, ≤ 1024 chars),
``allowed-tools`` (tool names the skill binds; the agent's tool set is the union over its skills).
Progressive disclosure: the agent sees names + descriptions; ``use_skill`` loads the body and
``read_skill_file`` the files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from papi.core.config import resource_dir

DESCRIPTION_MAX = 1024
BODY_MAX = 100_000
FILE_MAX = 100_000
FILE_DIRS = ("references", "assets", "scripts")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    body: str
    root: Path

    def files(self) -> list[str]:
        out: list[str] = []
        for d in FILE_DIRS:
            base = self.root / d
            if base.is_dir():
                out += sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())
        return out

    def read_file(self, rel: str) -> str:
        """Text of a skill file; only paths from :meth:`files` are readable (no traversal)."""
        if rel not in self.files():
            raise SkillError(
                f"{self.name}: no such file {rel!r}; available: {', '.join(self.files()) or '-'}"
            )
        text = (self.root / rel).read_text(encoding="utf-8")
        return text[:FILE_MAX]


def skills_root() -> Path:
    return resource_dir("agents") / "skills"


def load_skill(name: str, root: Path | None = None) -> Skill:
    folder = (root or skills_root()) / name
    path = folder / "SKILL.md"
    if not path.is_file():
        raise SkillError(f"skill {name!r} not found ({path})")
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not m:
        raise SkillError(f"{path}: missing frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if meta.get("name") != name:
        raise SkillError(f"{path}: frontmatter name {meta.get('name')!r} != folder {name!r}")
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SkillError(f"{path}: description is required")
    if len(description) > DESCRIPTION_MAX:
        raise SkillError(f"{path}: description is {len(description)} chars (max {DESCRIPTION_MAX})")
    tools = meta.get("allowed-tools") or []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SkillError(f"{path}: allowed-tools must be a list of tool names")
    body = m.group(2).strip()
    if len(body) > BODY_MAX:
        raise SkillError(f"{path}: body is {len(body)} chars (max {BODY_MAX})")
    return Skill(name, description, tuple(tools), body, folder)
