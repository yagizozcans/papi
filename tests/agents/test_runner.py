"""Hard gate: an agent that leaves required work undone is resumed once in the same session."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import Connection

from papi.agents import runs
from papi.agents.definition import load_agent
from papi.agents.registry import ENV_TRANSCRIPT
from papi.agents.runner import run_agent
from papi.llm.types import LlmResult


def _blocked(msg: str | None) -> dict[str, Any]:
    return {"tool": "kap_read_document", "args": {}, "ms": 1, "result": {"success": True, "blocking": msg}}


class StubBackend:
    """Each step: (transcript entries to append, cost). Records every call's kwargs."""

    def __init__(self, steps: list[tuple[list[dict[str, Any]], float]]) -> None:
        self.steps = steps
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kw: Any) -> LlmResult:
        self.calls.append(kw)
        entries, cost = self.steps[len(self.calls) - 1]
        path = kw["mcp_config"]["mcpServers"]["papi"]["env"][ENV_TRANSCRIPT]
        with open(path, "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return LlmResult(
            structured_output={"headline_tr": "x"},
            text=None,
            model_ids=["m"],
            cost_usd_est=cost,
            duration_ms=1,
            exit_code=0,
            raw_json={"session_id": "sess-1", "num_turns": 3, "usage": {"input_tokens": 5}},
            error_kind=None,
            error=None,
        )


def test_blocking_is_decided_by_newest_result_carrying_the_key() -> None:
    assert runs.blocking([]) is None
    assert runs.blocking([_blocked("unread"), {"tool": "x", "result": {"success": True}}]) == "unread"
    assert runs.blocking([_blocked("unread"), _blocked(None)]) is None


def test_unread_text_resumes_the_same_session_once_and_completes(db: Connection) -> None:
    agent = load_agent("kap-analyst")
    stub = StubBackend([([_blocked("document 1: not opened")], 0.2), ([_blocked(None)], 0.1)])
    r = run_agent(db, agent, "Analyse KAP disclosure 1.", subject="1", priority="P1", backend=stub)
    assert r.ok and r.resumed and r.gate is None
    assert len(stub.calls) == 2
    assert stub.calls[1]["resume_session"] == "sess-1"
    assert "document 1: not opened" in stub.calls[1]["prompt"]
    assert stub.calls[1]["max_budget_usd"] == agent.max_budget_usd - 0.2  # budget is per run, not per call
    assert r.cost_usd == pytest.approx(0.3)
    assert r.meta["status"] == "ok" and r.meta["resumed"] is True


def test_still_blocked_after_the_resume_ends_incomplete_without_a_second_resume(db: Connection) -> None:
    stub = StubBackend([([_blocked("unread")], 0.1), ([_blocked("unread")], 0.1), ([], 0.1)])
    r = run_agent(db, load_agent("kap-analyst"), "p", subject="1", priority="P1", backend=stub)
    assert len(stub.calls) == 2
    assert not r.ok and r.gate == "unread" and r.meta["status"] == "incomplete"


def test_no_resume_when_the_budget_is_spent(db: Connection) -> None:
    agent = load_agent("kap-analyst")
    stub = StubBackend([([_blocked("unread")], agent.max_budget_usd - 0.01)])
    r = run_agent(db, agent, "p", subject="1", priority="P1", backend=stub)
    assert len(stub.calls) == 1 and r.meta["status"] == "incomplete"


def test_complete_run_is_not_resumed(db: Connection) -> None:
    stub = StubBackend([([_blocked(None)], 0.1)])
    r = run_agent(db, load_agent("kap-analyst"), "p", subject="1", priority="P1", backend=stub)
    assert r.ok and not r.resumed and len(stub.calls) == 1
