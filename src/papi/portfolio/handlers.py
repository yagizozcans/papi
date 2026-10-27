"""Portfolio background work: position snapshots and corporate-action pending actions (12 §3–§4)."""

from __future__ import annotations

from typing import Any

from papi.core import db
from papi.core.events import PORTFOLIO_CHANGED, CloudEvent, PortfolioChanged
from papi.core.registry import handles, periodic
from papi.core.timeutil import ist_date
from papi.portfolio import service


@handles(PORTFOLIO_CHANGED, name="portfolio.snapshot_on_change")
def snapshot_on_change(event: CloudEvent, ctx: Any) -> None:
    """Refresh today's ``position_snapshot`` rows for the changed portfolio."""
    data = event.data_as(PortfolioChanged)
    with db.transaction() as conn:
        service.snapshot_positions(conn, ist_date(), portfolio_id=data.portfolio_id)


@periodic("portfolio.daily_snapshot", 3 * 3600, run_on_start=True)
def daily_snapshot(ctx: Any) -> None:
    """Point-in-time valuation for every portfolio (re-run after the EOD price fetch)."""
    with db.transaction() as conn:
        service.snapshot_positions(conn, ist_date())


@periodic("portfolio.corporate_actions", 3600, run_on_start=True)
def corporate_actions(ctx: Any) -> None:
    """Turn recent corporate actions (ex_date reached) into pending actions for holders."""
    with db.transaction() as conn:
        service.scan_corporate_actions(conn)
