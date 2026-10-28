"""Versioned prompts + output schemas of the pipeline Claude calls (PRD 04 §8).

``agent_version`` = first 16 hex chars of sha256(system prompt, canonical schema JSON, model setting):
changing any of the three yields a new version (and a new analysis ``input_hash``).
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from papi.core.config import resource_dir
from papi.llm.backend import model_for_task

PROMPT_DIR = Path(__file__).parent / "prompts"
ANALYST_VERSION = "analyst.v1"
CHECKER_VERSION = "checker.v1"
_WIRE_DROP = ("$schema", "$id")


def schema_path(version: str) -> Path:
    return resource_dir("contracts") / "schemas" / f"{version}.json"


@functools.cache
def _load(version: str) -> tuple[str, str]:
    prompt = (PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8")
    schema_text = schema_path(version).read_text(encoding="utf-8")
    return prompt, schema_text


@dataclass(frozen=True)
class AgentSpec:
    task: str  # llm task name: analyst | checker
    version: str  # prompt + schema version, e.g. analyst.v1
    system_prompt: str
    schema: dict[str, Any]
    model: str

    @property
    def agent_version(self) -> str:
        canonical = json.dumps(self.schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.sha256()
        for part in (self.system_prompt, canonical, self.model):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()[:16]

    def wire_schema(self) -> dict[str, Any]:
        """The schema as sent to ``--json-schema`` (meta keys dropped)."""
        return {k: v for k, v in self.schema.items() if k not in _WIRE_DROP}


def load_spec(task: str, version: str) -> AgentSpec:
    prompt, schema_text = _load(version)
    return AgentSpec(
        task=task,
        version=version,
        system_prompt=prompt,
        schema=json.loads(schema_text),
        model=model_for_task(task),
    )


def analyst_spec() -> AgentSpec:
    return load_spec("analyst", ANALYST_VERSION)


def checker_spec() -> AgentSpec:
    return load_spec("checker", CHECKER_VERSION)
