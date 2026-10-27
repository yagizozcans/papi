"""Background work of the prices slice.

- periodic ``prices:daily`` (checked every 10 min): once per trading day after ``prices.fetch_time_ist``
  sync holdings + watchlist + XU100 for the last 10 days.
- job kind ``prices:sync``: payload ``{"instrument_ids": [...], "tickers": [...], "start": "YYYY-MM-DD",
  "end": "YYYY-MM-DD"|null}`` for on-demand syncs/backfills enqueued by other slices.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from papi.core import db
from papi.core.config import get_settings
from papi.core.jobs import PermanentError
from papi.core.logging import get_logger
from papi.core.registry import job_kind, periodic
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_ist
from papi.sources.prices import service

log = get_logger("prices.handlers")

DAILY_CHECK_SECONDS = 600.0
SYNC_JOB_KIND = "prices:sync"


@periodic("prices:daily", DAILY_CHECK_SECONDS)
def daily_prices(ctx: Any) -> None:
    now = now_ist()
    today = now.date()
    s = get_settings()
    with db.transaction() as conn:
        last = get_state(conn, service.DAILY_STATE_KEY)
        last_done = date.fromisoformat(last) if last else None
        if not service.daily_due(now, s.prices.fetch_time_ist, last_done, service.is_closed_day(conn, today)):
            return
        ids = service.daily_instrument_ids(conn, today, s)
    start = today - timedelta(days=service.DAILY_WINDOW_DAYS)
    result = service.sync_prices_isolated(ids, start, today)
    if not any(result["rows"].values()):
        # nothing stored (sources down / not yet published): retry at the next check
        log.warning("daily price sync stored no rows", extra={"errors": result["errors"]})
        return
    with db.transaction() as conn:
        set_state(conn, service.DAILY_STATE_KEY, today.isoformat())
    log.info("daily price sync", extra={"rows": result["rows"], "errors": len(result["errors"])})


@job_kind(SYNC_JOB_KIND, max_attempts=3)
def sync_job(payload: dict[str, Any], ctx: Any) -> None:
    try:
        start = date.fromisoformat(payload["start"])
        end = date.fromisoformat(payload["end"]) if payload.get("end") else None
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"bad prices:sync payload: {exc}") from exc
    ids = list(payload.get("instrument_ids") or [])
    tickers = list(payload.get("tickers") or [])
    if tickers:
        with db.transaction() as conn:
            found, unknown = service.resolve_tickers(conn, tickers)
        if unknown:
            log.warning("unknown tickers skipped", extra={"tickers": unknown})
        ids += list(found.values())
    if not ids:
        raise PermanentError("prices:sync payload has no instruments")
    service.sync_prices_isolated(ids, start, end)
