"""Background work of the KAP slice (auto-imported by ``papi.core.registry.load_plugins``).

Periodics: ``kap:poll`` (session-aware cadence), ``kap:gaps`` (5 min), ``kap:reprioritize`` (2 min),
``kap:reconcile`` (``kap.reconcile_every_hours``), ``kap:companies`` (daily; then capital of
held/watched issuers due), ``kap:capital`` (hourly; a few remaining issuers whose capital is older than
7 days). ``portfolio.changed`` (and the ``kap:reprioritize`` periodic, which also catches watchlist
edits) raises queued fetches of newly held/watched issuers to the P1/P2 lane.
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
from papi.core.events import PORTFOLIO_CHANGED, CloudEvent
from papi.core.registry import handles, job_kind, periodic
from papi.sources.kap import cadence
from papi.sources.kap.capital import HELD_BATCH, SLOW_BATCH, refresh_batch
from papi.sources.kap.client import KapBlocked, KapClient, KapNotFound, default_client
from papi.sources.kap.common import JOB_FETCH, JOB_PROBE, JOB_REFRESH, writer
from papi.sources.kap.companies import sync_companies
from papi.sources.kap.discovery import poll_once, reconcile
from papi.sources.kap.gaps import probe_index, scan_gaps
from papi.sources.kap.ingest import ParseFailed, mark_failed, process_disclosure, reprioritize_queued
from papi.sources.kap.reprocess import refresh_disclosure

log = logging.getLogger("papi.kap.handlers")

POLL_PRIORITY = 12  # ahead of everything but P1 work and chat
GAPS_INTERVAL_S = 300.0
COMPANIES_INTERVAL_S = 86400.0
CAPITAL_INTERVAL_S = 3600.0
REPRIORITIZE_INTERVAL_S = 120.0


def _client() -> KapClient:
    return default_client()


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    client = _client()
    try:
        with db.connect() as raw_conn:
            conn = writer(raw_conn)
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
    report = _run(scan_gaps)
    if report.new_missing or report.funds_skipped:
        log.info(
            "kap gaps: %d fund indices skipped, %d new missing, %d open",
            report.funds_skipped,
            len(report.new_missing),
            report.open,
        )


def _reprioritize() -> int:
    with db.connect() as raw_conn:
        conn = writer(raw_conn)
        raised = reprioritize_queued(conn)
        conn.commit()
    return raised


@periodic("kap:reprioritize", REPRIORITIZE_INTERVAL_S, priority=POLL_PRIORITY)
def reprioritize_job(ctx: Any) -> None:
    _reprioritize()


@handles(PORTFOLIO_CHANGED, name="kap.reprioritize_on_portfolio_change", priority=jobs.PRIORITY_P1)
def reprioritize_on_portfolio_change(event: CloudEvent, ctx: Any) -> None:
    _reprioritize()


@periodic("kap:reconcile", _reconcile_interval, priority=60, run_on_start=False)
def reconcile_job(ctx: Any) -> None:
    summary = _run(reconcile)
    log.info("kap reconcile: %s", summary)


@periodic("kap:companies", COMPANIES_INTERVAL_S, priority=70)
def companies_job(ctx: Any) -> None:
    report = _run(sync_companies)
    log.info("kap companies: %s", report.as_dict())
    counts = _run(refresh_batch, limit=HELD_BATCH, held_only=True)
    log.info("kap capital (held/watched): %s", counts)


@periodic("kap:capital", CAPITAL_INTERVAL_S, priority=75)
def capital_job(ctx: Any) -> None:
    counts = _run(refresh_batch, limit=SLOW_BATCH)
    log.info("kap capital: %s", counts)


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
