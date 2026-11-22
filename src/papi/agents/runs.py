"""Run records under ``$PAPI_HOME/agent-runs/``: ``<agent>-<run>.meta.json`` (status, cost, answer),
``.jsonl`` (tool-call transcript, appended live by the MCP server) and ``.state.json`` (tool state).

The dashboard reads these files, so a run started from the CLI or the page looks the same and a running
run can be watched: the transcript grows with every tool call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from papi.core.config import papi_home

ACTIVE = ("starting", "running", "resuming")


def runs_dir() -> Path:
    path = papi_home() / "agent-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def base_path(agent: str, run_id: str) -> Path:
    return runs_dir() / f"{agent}-{run_id}"


def transcript_path(agent: str, run_id: str) -> Path:
    return base_path(agent, run_id).with_suffix(".jsonl")


def state_path(agent: str, run_id: str) -> Path:
    return base_path(agent, run_id).with_suffix(".state.json")


def _meta_path(agent: str, run_id: str) -> Path:
    return base_path(agent, run_id).with_suffix(".meta.json")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_meta(agent: str, run_id: str, **fields: Any) -> dict[str, Any]:
    """Create or update the run's meta file (atomic replace); returns the merged record."""
    path = _meta_path(agent, run_id)
    data = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"run_id": run_id, "agent": agent}
    )
    data.update(fields)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
    return data


def read_transcript(agent: str, run_id: str) -> list[dict[str, Any]]:
    path = transcript_path(agent, run_id)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # a line being written right now
            continue
    return out


def blocking(entries: list[dict[str, Any]]) -> str | None:
    """The gate: the newest tool result that carries a ``blocking`` key decides. A non-null value means
    the agent left required work undone (e.g. unread document text)."""
    for e in reversed(entries):
        result = e.get("result") or {}
        if "blocking" in result:
            return result["blocking"] or None
    return None


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    files = sorted(runs_dir().glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def load_run(run_id: str) -> dict[str, Any] | None:
    """Meta plus ``calls`` (transcript) for one run id, or None."""
    for p in runs_dir().glob(f"*-{run_id}.meta.json"):
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["calls"] = read_transcript(meta["agent"], run_id)
        return meta
    return None
