"""Analyst background work (PRD 04 §2).

- ``event.classified.v1`` → ``event:analyst.analyse``: analyst call for P1–P3 events (P4, superseded,
  retracted and already-analysed events are skipped). Job priority = the publisher's priority label;
  governor deferrals (``Deferred``) re-queue the job without using an attempt.
- ``analyst:check`` {analysis_id}: a checker call the governor deferred.
"""

from __future__ import annotations

from typing import Any

from papi.analyst import checker
from papi.analyst.context import EventNotFound
from papi.analyst.pipeline import ANALYSED_PRIORITIES, analyse_event
from papi.core import jobs
from papi.core.db import connect
from papi.core.events import EVENT_CLASSIFIED, CloudEvent, EventClassified
from papi.core.logging import get_logger
from papi.core.registry import handles, job_kind

log = get_logger("analyst")

HANDLER_NAME = "analyst.analyse"


@handles(EVENT_CLASSIFIED, name=HANDLER_NAME, max_attempts=3)
def on_event_classified(ev: CloudEvent, ctx: Any) -> None:
    data = ev.data_as(EventClassified)
    if data.priority not in ANALYSED_PRIORITIES:
        return
    # IMMEDIATE: the pipeline reads then writes (inflight lock, analysis rows) on this connection; a
    # deferred snapshot overtaken by the busy KAP/triage writers fails with "database is locked".
    # The pipeline commits before every Claude call, so the write lock is never held across one.
    with connect(immediate=True) as conn:
        try:
            out = analyse_event(conn, data.event_id, ctx=ctx)
        except EventNotFound as exc:
            raise jobs.PermanentError(f"event {exc} not found") from exc
        conn.commit()
    log.info("analyst %s event %s: %s %s", out.status, data.event_id, out.reason or "", out.analysis_ids)


@job_kind(checker.CHECK_JOB, max_attempts=3)
def run_check(payload: dict[str, Any], ctx: Any) -> None:
    with connect(immediate=True) as conn:
        state = checker.run_for_call(conn, [payload["analysis_id"]], raise_deferred=True)
        conn.commit()
    log.info("checker for analysis %s: %s", payload["analysis_id"], state)
