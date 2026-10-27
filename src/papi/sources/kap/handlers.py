"""Background work of the KAP slice (auto-imported by ``papi.core.registry.load_plugins``).

Periodics: ``kap:poll`` (session-aware cadence), ``kap:gaps`` (5 min), ``kap:reconcile``
(``kap.reconcile_every_hours``), ``kap:companies`` (daily).
Job kinds: ``kap:fetch`` {ref, mode, is_catchup, priority}, ``kap:probe`` {index},
``kap:refresh`` {ref, reason}.
A circuit-open answer defers the job (no attempt used); not-found is permanent.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from papi.core import db, jobs
from papi.core.config import get_settings
from papi.core.registry import job_kind, periodic
from papi.sources.kap import cadence
from papi.sources.kap.client import KapBlocked, KapClient, KapNotFound, default_client
from papi.sources.kap.common import JOB_FETCH, JOB_PROBE, JOB_REFRESH
from papi.sources.kap.companies import sync_companies
from papi.sources.kap.discovery import poll_once, reconcile
from papi.sources.kap.gaps import detect_gaps, probe_index
from papi.sources.kap.ingest import ParseFailed, mark_failed, process_disclosure
from papi.sources.kap.reprocess import refresh_disclosure

log = logging.getLogger("papi.kap.handlers")

POLL_PRIORITY = 12  # ahead of everything but P1 work and chat
GAPS_INTERVAL_S = 300.0
COMPANIES_INTERVAL_S = 86400.0


def _client() -> KapClient:
    return default_client()


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    client = _client()
    try:
        with db.connect() as conn:
            result = fn(conn, client, *args, **kwargs)
            if conn.in_transaction():
                conn.commit()
            return result
    except KapBlocked as exc:
        raise jobs.Deferred(exc.until, reason=str(exc)) from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _reconcile_interval(now: datetime) -> float:
    return float(get_settings().kap.reconcile_every_hours) * 3600.0


@periodic("kap:poll", cadence.poll_interval_seconds, priority=POLL_PRIORITY)
def poll_job(ctx: Any) -> None:
    report = _run(poll_once)
    log.info("kap poll: %s", report.summary())


@periodic("kap:gaps", GAPS_INTERVAL_S, priority=60)
def gaps_job(ctx: Any) -> None:
    with db.connect() as conn:
        report = detect_gaps(conn)
        conn.commit()
    if report.new_missing:
        log.info("kap gaps: %d new, %d open", len(report.new_missing), report.open)


@periodic("kap:reconcile", _reconcile_interval, priority=60, run_on_start=False)
def reconcile_job(ctx: Any) -> None:
    summary = _run(reconcile)
    log.info("kap reconcile: %s", summary)


@periodic("kap:companies", COMPANIES_INTERVAL_S, priority=70)
def companies_job(ctx: Any) -> None:
    report = _run(sync_companies)
    log.info("kap companies: %s", report.as_dict())


@job_kind(JOB_FETCH, max_attempts=5)
def fetch_job(payload: dict[str, Any], ctx: Any) -> None:
    last_attempt = ctx.attempt >= ctx.job.max_attempts
    try:
        _run(
            process_disclosure,
            str(payload["ref"]),
            mode=payload.get("mode") or "live",
            is_catchup=payload.get("is_catchup"),
            label=payload.get("priority"),
            tolerate_errors=last_attempt,
        )
    except KapNotFound as exc:
        raise jobs.PermanentError(str(exc)) from exc
    except ParseFailed as exc:
        raise jobs.PermanentError(str(exc)) from exc
    except jobs.Deferred:
        raise
    except Exception:
        if last_attempt:
            with db.connect() as conn:
                mark_failed(conn, str(payload["ref"]), "fetch", f"gave up after {ctx.attempt} attempts")
                conn.commit()
        raise


@job_kind(JOB_PROBE, max_attempts=3)
def probe_job(payload: dict[str, Any], ctx: Any) -> None:
    outcome = _run(probe_index, int(payload["index"]))
    log.debug("kap probe %s: %s", payload["index"], outcome)


@job_kind(JOB_REFRESH, max_attempts=3)
def refresh_job(payload: dict[str, Any], ctx: Any) -> None:
    outcome = _run(refresh_disclosure, str(payload["ref"]))
    log.debug("kap refresh %s: %s", payload["ref"], outcome)
