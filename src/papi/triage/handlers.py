"""Triage background work: classify every normalised / revised disclosure (PRD 04 §3, C5)."""

from __future__ import annotations

from typing import Any

from papi.core import jobs
from papi.core.db import transaction
from papi.core.events import (
    DISCLOSURE_NORMALIZED,
    DISCLOSURE_REVISED,
    CloudEvent,
    DisclosureNormalized,
    DisclosureRevised,
)
from papi.core.logging import get_logger
from papi.core.registry import handles
from papi.triage.classify import DisclosureNotFound, retriage_disclosure, triage_disclosure

log = get_logger("triage")

RECLASSIFY_KINDS = frozenset({"reparse", "refresh"})  # same disclosure re-parsed / re-fetched


def _job_priority(ev: CloudEvent) -> int:
    """Live disclosures are triaged ahead of analysis work (≤ 2 s target); catch-up floods yield."""
    return jobs.PRIORITY_BACKFILL if ev.data.get("is_catchup") else jobs.PRIORITY_P1


def _run(disclosure_id: str, *, is_catchup: bool, retriage: bool = False) -> list[str]:
    fn = retriage_disclosure if retriage else triage_disclosure
    try:
        with transaction() as conn:
            return fn(conn, disclosure_id, is_catchup=is_catchup)
    except DisclosureNotFound as exc:
        raise jobs.PermanentError(f"disclosure {exc} not found") from exc


@handles(DISCLOSURE_NORMALIZED, priority=_job_priority, name="triage.disclosure_normalized")
def on_disclosure_normalized(ev: CloudEvent, ctx: Any) -> None:
    data = ev.data_as(DisclosureNormalized)
    ids = _run(data.disclosure_id, is_catchup=data.is_catchup)
    log.debug("triaged disclosure %s → %d event(s)", data.disclosure_id, len(ids))


@handles(DISCLOSURE_REVISED, priority=_job_priority, name="triage.disclosure_revised")
def on_disclosure_revised(ev: CloudEvent, ctx: Any) -> None:
    """``correction`` / ``update``: a new disclosure (a correction supersedes the original's events).
    ``reparse`` / ``refresh`` (``revision_of == disclosure_id``): same disclosure, content re-read →
    re-classify and replace its events when the result changed."""
    data = ev.data_as(DisclosureRevised)
    same = data.revision_of == data.disclosure_id or data.revision_kind in RECLASSIFY_KINDS
    ids = _run(data.disclosure_id, is_catchup=bool(ev.data.get("is_catchup")), retriage=same)
    log.debug(
        "triaged %s revision %s of %s → %d event(s)",
        data.revision_kind,
        data.disclosure_id,
        data.revision_of,
        len(ids),
    )
