"""Pydantic models of the Research API (:mod:`papi.web.api_research`; 14 §3.5, phase P2 slice B).

Agents (catalogue, runs, live run detail) and theses (list, editor, check, upload, reviews). Free-form
JSON (an agent's structured ``output``) is ``Any``; everything else is precisely typed so the frontend's
TypeScript types come from the OpenAPI schema.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------------------ agents


class AgentSkillOut(BaseModel):
    name: str
    description: str
    tools: list[str]


class AgentDefOut(BaseModel):
    name: str
    description: str
    version: str
    model: str
    max_turns: int
    max_budget_usd: float
    timeout_s: float
    priority: str
    skills: list[AgentSkillOut]
    tools: list[str]
    builtin_tools: list[str]
    ref_kind: Literal["disclosure", "thesis", "other"]  # what the run subject is


class AgentListOut(BaseModel):
    agents: list[AgentDefOut]


class AgentRunRow(BaseModel):
    run_id: str
    agent: str
    subject: str | None
    status: str  # starting | running | resuming | ok | incomplete | failed | deferred | stale
    active: bool
    started_at: datetime | None
    finished_at: datetime | None
    elapsed_s: int | None
    cost_usd: float | None
    max_budget_usd: float | None
    turns: int | None
    error: str | None
    verdict: str | None  # strategy-analyst only


class AgentRunListOut(BaseModel):
    items: list[AgentRunRow]
    total: int
    limit: int
    offset: int


class RunCall(BaseModel):
    at: datetime | None
    tool: str
    args: dict[str, Any]
    ms: int | None
    ok: bool
    blocking: str | None
    result_excerpt: str  # trimmed summary of the result (long text fields replaced by their length)


class RunUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class AgentRunDetail(AgentRunRow):
    agent_version: str | None
    max_turns: int | None
    gate: str | None
    resumed: bool
    usage: RunUsage
    calls: list[RunCall]
    output: dict[str, Any] | None  # structured answer (kap-analyst / strategy-analyst schema)
    text: str | None  # plain answer when the agent returned no structured output


class StartRunIn(BaseModel):
    agent: str = Field(min_length=1, max_length=60)
    ref: str = Field(min_length=1, max_length=40)


class StartRunOut(BaseModel):
    run_id: str
    agent: str
    ref: str
    max_budget_usd: float


# ------------------------------------------------------------------------------------------ theses


class ThesisIssue(BaseModel):
    message: str
    line: int | None  # 1-based line of the text the issue points at, when known


class ThesisCheckOut(BaseModel):
    """Deterministic check: ``errors`` stop a review, ``gaps`` are reported but allowed."""

    ok: bool
    errors: list[ThesisIssue]
    gaps: list[str]
    sections_filled: int
    sections_total: int
    triggers: int
    kill_signals: int


class ThesisOutlineItem(BaseModel):
    line: int
    title: str
    key: str | None  # canonical section key; null for an unknown heading
    required: bool
    items: int | None  # parsed item count; null when the text does not parse
    empty: bool


class ThesisFile(BaseModel):
    name: str
    size: int
    url: str


class ThesisPage(BaseModel):
    file: str
    number: int
    method: str
    text: str
    uncertain: list[str]


class ThesisReviewRow(BaseModel):
    run_id: str
    status: str
    verdict: str | None
    confidence: float | None
    started_at: datetime | None
    cost_usd: float | None


class ThesisRow(BaseModel):
    name: str
    ticker: str | None
    date: Date | None
    direction: str | None
    horizon: str | None
    status: str  # starting | extracting | analysing | ready | done | needs_fix | failed | manual | stale
    active: bool
    error_count: int | None  # null while the file is being processed
    gap_count: int | None
    reviews: int
    last_review: ThesisReviewRow | None
    files: int


class ThesisListOut(BaseModel):
    items: list[ThesisRow]
    accept: list[str]  # accepted upload extensions


class ThesisDetail(ThesisRow):
    text: str | None  # raw Markdown; null while an upload is still being read
    check: ThesisCheckOut | None
    outline: list[ThesisOutlineItem]
    warnings: list[str]
    status_error: str | None
    files_done: int | None
    pages_done: int | None
    run_id: str | None  # the review run of an upload that is analysing
    source_files: list[ThesisFile]
    pages: list[ThesisPage]


class SaveThesisIn(BaseModel):
    content: str = Field(max_length=200_000)


class CheckThesisIn(BaseModel):
    content: str | None = Field(
        default=None, max_length=200_000
    )  # check this draft instead of the saved file


class NewThesisIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    date: Date | None = None


class UploadThesisOut(BaseModel):
    name: str
    files: list[str]
