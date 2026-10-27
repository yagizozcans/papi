"""Shared LLM result type (contract C6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ErrorKind = Literal[None, "limit", "schema", "timeout", "auth", "error"]


@dataclass
class LlmResult:
    """Outcome of one backend call. ``error_kind is None`` means success."""

    structured_output: dict[str, Any] | None
    text: str | None
    model_ids: list[str]
    cost_usd_est: float
    duration_ms: int
    exit_code: int
    raw_json: dict[str, Any] | None
    error_kind: ErrorKind
    error: str | None
    # Reset time reported with a usage-limit error (``error_kind == "limit"``), when parseable.
    limit_reset_at: datetime | None = None
    # Last ``rate_limit_info`` from the CLI (status, resetsAt, unifiedWindows.{five_hour,seven_day}
    # .utilization 0..1): the subscription bars, shared with interactive use.
    rate_limit: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error_kind is None
