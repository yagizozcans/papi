"""Notify slice background work (auto-imported by ``papi.core.registry.load_plugins``).

- ``event.classified.v1`` → instant alert (``notify.instant_alert``)
- ``analysis.completed.v1`` → threaded follow-up (``notify.follow_up``)
- ``disclosure.revised.v1`` → revision notice reply (``notify.revision``)
- job kinds ``notify:ops`` (C4) and ``notify:flush`` (catch-up / rate-limit batches)
- periodics ``notify:digest`` (checks every minute; sends once a day after ``alerts.digest_time``),
  ``notify:weekly_review`` (checks every minute; sends at ``alerts.weekly_review``) and
  ``notify:seed_rules`` (default alert rules on first worker start)
"""

from __future__ import annotations

from typing import Any

from papi.core import db
from papi.core.events import ANALYSIS_COMPLETED, DISCLOSURE_REVISED, EVENT_CLASSIFIED, CloudEvent
from papi.core.registry import handles, job_kind, periodic
from papi.notify import alerts, digest, weekly
from papi.notify.ops import OPS_JOB_KIND
from papi.notify.store import seed_default_rules


@handles(EVENT_CLASSIFIED, name="notify.instant_alert")
def instant_alert(event: CloudEvent, ctx: Any) -> None:
    with db.transaction() as conn:
        alerts.on_event_classified(conn, event.data["event_id"])


@handles(ANALYSIS_COMPLETED, name="notify.follow_up")
def follow_up(event: CloudEvent, ctx: Any) -> None:
    with db.transaction() as conn:
        alerts.on_analysis_completed(conn, event.data["event_id"], list(event.data.get("analysis_ids") or []))


@handles(DISCLOSURE_REVISED, name="notify.revision")
def revision(event: CloudEvent, ctx: Any) -> None:
    d = event.data
    with db.transaction() as conn:
        alerts.on_disclosure_revised(conn, d["disclosure_id"], d["revision_of"], d.get("revision_kind") or "")


@job_kind(OPS_JOB_KIND)
def ops(payload: dict[str, Any], ctx: Any) -> None:
    with db.transaction() as conn:
        alerts.send_ops(conn, payload, job_id=ctx.job.id)


@job_kind(alerts.FLUSH_JOB_KIND)
def flush(payload: dict[str, Any], ctx: Any) -> None:
    with db.transaction() as conn:
        alerts.flush(conn)


@periodic("notify:digest", 60, priority=40)
def daily_digest(ctx: Any) -> None:
    with db.transaction() as conn:
        digest.send_digest(conn)


@periodic("notify:weekly_review", 60, priority=40)
def weekly_review(ctx: Any) -> None:
    with db.transaction() as conn:
        weekly.send_weekly_review(conn)


@periodic("notify:seed_rules", 86400, priority=40)
def seed_rules(ctx: Any) -> None:
    with db.transaction() as conn:
        seed_default_rules(conn)
