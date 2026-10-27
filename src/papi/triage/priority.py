"""Priority label P1–P4 for an event (PRD 04 §3, contract C3 ``holding_priority``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection

from papi.core.config import get_settings

PriorityLabel = Literal["P1", "P2", "P3", "P4"]


def holding_priority(conn: Connection, instrument_ids: list[str]) -> Literal["P1", "P2"] | None:
    """C3: P1 if held, P2 if watched / recently sold / tracked index or sector, else ``None``.
    Current holdings are used (new events read current holdings; past events are not re-triaged)."""
    from papi.portfolio.service import holding_priority as _holding_priority

    return _holding_priority(conn, instrument_ids)


def always_tell(code: str, codes: list[str]) -> bool:
    """``code`` is listed in ``triage.always_tell_codes`` (a listed prefix covers its sub-codes)."""
    return any(code == c or code.startswith(f"{c}.") for c in codes)


@dataclass(frozen=True)
class PriorityDecision:
    label: PriorityLabel
    reason: str  # holding | watch | materiality | always_tell | default
    holding: str | None


def decide(conn: Connection, code: str, instrument_ids: list[str], materiality: int) -> PriorityDecision:
    cfg = get_settings().triage
    held = holding_priority(conn, instrument_ids) if instrument_ids else None
    if held == "P1":
        return PriorityDecision("P1", "holding", held)
    if held == "P2":
        return PriorityDecision("P2", "watch", held)
    if always_tell(code, list(cfg.always_tell_codes)):
        return PriorityDecision("P3", "always_tell", held)
    if materiality >= cfg.p3_materiality_threshold:
        return PriorityDecision("P3", "materiality", held)
    return PriorityDecision("P4", "default", held)
