"""KB background work: parse a changed legal document into the draft snapshot and notify (KB-4)."""

from __future__ import annotations

from typing import Any

from papi.core import jobs
from papi.core.db import transaction
from papi.core.logging import get_logger
from papi.core.registry import job_kind
from papi.kb.ingest import ingest_entry
from papi.kb.obligation_store import load_obligations
from papi.sources.legal.manifest import load_manifest

PARSE_JOB = "kb:parse"

log = get_logger("kb")


@job_kind(PARSE_JOB, max_attempts=2)
def parse_job(payload: dict[str, Any], ctx: Any) -> None:
    try:
        entry = load_manifest().get(payload["id"])
    except KeyError as exc:
        raise jobs.PermanentError(str(exc)) from exc
    report = ingest_entry(entry)
    if report.status in ("failed", "not_fetched"):
        raise jobs.PermanentError(f"{entry.id}: {report.error}")
    with transaction() as conn:
        load_obligations(conn)
        if report.new_versions or report.history_versions:
            from papi.notify.ops import ops_alert

            ops_alert(
                conn,
                f"Mevzuat değişti: {entry.title} ({entry.official_code}) — {report.new_versions} yeni hüküm "
                f"sürümü taslakta, {report.low_confidence} düşük güvenli yapı. İnceleme: `papi kb review`, "
                f"yayım: `papi kb publish`.",
                level="info",
                dedupe_key=f"kb:draft:{entry.id}:{report.document_id}",
            )
    log.info("kb parse job %s: %s", entry.id, report.status)
