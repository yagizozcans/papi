"""Simulation-slice background work: corporate actions from classified / analysed events."""

from __future__ import annotations

from typing import Any

from papi.core import db
from papi.core.events import (
    ANALYSIS_COMPLETED,
    EVENT_CLASSIFIED,
    AnalysisCompleted,
    CloudEvent,
    EventClassified,
)
from papi.core.registry import handles
from papi.sim import corporate_actions as ca


@handles(EVENT_CLASSIFIED, name="sim.corporate_actions.classified")
def on_event_classified(event: CloudEvent, ctx: Any) -> None:
    data = event.data_as(EventClassified)
    with db.transaction() as conn:
        ca.process_event(conn, data.event_id)


@handles(ANALYSIS_COMPLETED, name="sim.corporate_actions.analysed")
def on_analysis_completed(event: CloudEvent, ctx: Any) -> None:
    data = event.data_as(AnalysisCompleted)
    with db.transaction() as conn:
        ca.process_event(conn, data.event_id, analysis_ids=data.analysis_ids)
