"""Background refresh of the legal corpus (KB-4): check due manifest documents, fetch, queue parse."""

from __future__ import annotations

from typing import Any

from papi.core import jobs
from papi.core.db import transaction
from papi.core.logging import get_logger
from papi.core.registry import job_kind, periodic
from papi.core.timeutil import ist_date
from papi.sources.legal.fetch import due_entries, fetch_entries
from papi.sources.legal.manifest import load_manifest

FETCH_JOB = "legal:fetch"
PARSE_JOB = "kb:parse"  # executed by papi.kb.handlers
REFRESH_CHECK_SECONDS = 6 * 3600

log = get_logger("sources.legal")


@periodic("legal.refresh", REFRESH_CHECK_SECONDS, priority=jobs.PRIORITY_BACKFILL, run_on_start=False)
def refresh_due(ctx: Any) -> int:
    """Queue a fetch for every manifest document whose ``refresh`` interval has elapsed."""
    due = due_entries(load_manifest().documents)
    day = ist_date().isoformat()
    with transaction() as conn:
        for e in due:
            jobs.enqueue(
                conn,
                FETCH_JOB,
                {"id": e.id},
                priority=jobs.PRIORITY_BACKFILL,
                dedupe_key=f"{FETCH_JOB}:{e.id}:{day}",
            )
    return len(due)


@job_kind(FETCH_JOB, max_attempts=4)
def fetch_job(payload: dict[str, Any], ctx: Any) -> None:
    try:
        entry = load_manifest().get(payload["id"])
    except KeyError as exc:
        raise jobs.PermanentError(str(exc)) from exc
    outcomes = fetch_entries([entry])
    current = outcomes[0]
    if current.status == "failed":
        from papi.notify.ops import ops_alert

        with transaction() as conn:
            ops_alert(
                conn,
                f"Mevzuat belgesi çekilemedi: {entry.title} ({entry.id}) — {current.error}",
                level="warning",
                dedupe_key=f"legal:fetch_failed:{entry.id}",
            )
        raise jobs.RetryLater(3600)
    if any(o.status == "changed" for o in outcomes):
        with transaction() as conn:
            jobs.enqueue(
                conn,
                PARSE_JOB,
                {"id": entry.id},
                priority=jobs.PRIORITY_BACKFILL,
                dedupe_key=f"{PARSE_JOB}:{entry.id}:{current.sha}",
            )
    log.info("legal fetch %s: %s", entry.id, ", ".join(f"{o.role}={o.status}" for o in outcomes))
