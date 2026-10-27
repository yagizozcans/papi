"""Discovery: live poll with wake/restart catch-up, periodic reconcile, resumable backfill.

kv_state keys: ``kap:last_poll_at`` / ``kap:last_poll_ok_at`` (ISO UTC), ``kap:last_index`` (int),
``kap:reconcile:last`` (summary), ``kap:backfill:cursor`` ({from, to, tickers, next_to, done}).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Integer, cast, func, select

from papi.core import jobs, metrics
from papi.core.config import get_settings
from papi.core.schema import disclosure, identifier
from papi.core.state import get_state, set_state
from papi.core.timeutil import ist_date, now_utc, parse_iso, to_ist
from papi.sources.kap import cadence, companies, deps
from papi.sources.kap.client import KapClient
from papi.sources.kap.common import JOB_REFRESH, PRIO_RECONCILE, kap_source_id, release
from papi.sources.kap.ingest import ListingDiff, enqueue_fetch, record_listing

log = logging.getLogger("papi.kap.discovery")

LAST_POLL_KEY = "kap:last_poll_at"
LAST_POLL_OK_KEY = "kap:last_poll_ok_at"
LAST_INDEX_KEY = "kap:last_index"
RECONCILE_KEY = "kap:reconcile:last"
BACKFILL_KEY = "kap:backfill:cursor"

CATCHUP_FACTOR = 3  # gap since last poll > 3 × cadence → catch-up mode
CATCHUP_MAX_DAYS = 31  # longer outages: list this much, alert to run a backfill for the rest
CATCHUP_CHUNK_DAYS = 3  # catch-up windows listed oldest→newest in chunks
BACKFILL_WINDOW_DAYS = 3
BACKFILL_MEMBER_WINDOW_DAYS = 365


@dataclass
class PollReport:
    mode: str
    window: tuple[date, date]
    listed: int = 0
    new: list[dict[str, Any]] = field(default_factory=list)
    changed: int = 0
    requests: int = 0
    truncated_days: list[date] = field(default_factory=list)
    catchup_gap_s: float | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "listed": self.listed,
            "new": len(self.new),
            "changed": self.changed,
            "requests": self.requests,
            "truncated_days": [d.isoformat() for d in self.truncated_days],
            "catchup_gap_s": self.catchup_gap_s,
        }


def _windows(start: date, end: date, days: int) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    lo = start
    while lo <= end:
        hi = min(lo + timedelta(days=days - 1), end)
        out.append((lo, hi))
        lo = hi + timedelta(days=1)
    return out


def _apply(conn: Connection, report: PollReport, diff: ListingDiff) -> None:
    report.new += diff.new
    report.changed += len(diff.changed)


def poll_once(conn: Connection, client: KapClient, *, now: datetime | None = None) -> PollReport:
    """List today + yesterday (or the missed window after sleep/restart) and record new items.

    Commits its own work. Raises client errors after recording ``kap.poll.fail``.
    """
    now = now or now_utc()
    interval = cadence.poll_interval_seconds(now)
    last_raw = get_state(conn, LAST_POLL_OK_KEY)
    last = parse_iso(last_raw) if last_raw else None
    today = ist_date(now)
    start = today - timedelta(days=1)
    gap = (now - last).total_seconds() if last else None
    # First run ever: the rolling window is backlog, not live traffic (no latency samples, flagged
    # catch-up so alerts carry the "late" label).
    catchup = gap is None or gap > CATCHUP_FACTOR * interval
    clamped = False
    if catchup and last is not None:
        start = min(start, ist_date(last))
        floor = today - timedelta(days=CATCHUP_MAX_DAYS)
        if start < floor:
            start, clamped = floor, True
    report = PollReport(mode="catchup" if catchup else "live", window=(start, today), catchup_gap_s=gap)
    release(conn)
    catchup_before = now - timedelta(seconds=max(2 * interval, 120))
    try:
        chunks = _windows(start, today, CATCHUP_CHUNK_DAYS) if catchup else [(start, today)]
        for lo, hi in chunks:
            res = client.list_disclosures(lo, hi)
            report.requests += res.requests
            report.listed += len(res.items)
            report.truncated_days += res.truncated_days
            diff = record_listing(
                conn, res.items, mode=report.mode, now=now, catchup_before=catchup_before if catchup else None
            )
            _apply(conn, report, diff)
            conn.commit()
    except Exception as exc:
        conn.rollback()
        set_state(conn, LAST_POLL_KEY, now.isoformat())
        metrics.record("kap.poll.fail", 1, conn=conn, error=type(exc).__name__)
        conn.commit()
        raise
    max_index = _max_index(conn)
    set_state(conn, LAST_POLL_KEY, now.isoformat())
    set_state(conn, LAST_POLL_OK_KEY, now.isoformat())
    if max_index is not None:
        set_state(conn, LAST_INDEX_KEY, max_index)
    metrics.record("kap.poll.ok", len(report.new), conn=conn, mode=report.mode)
    if report.truncated_days:
        metrics.record("kap.list.truncated", len(report.truncated_days), conn=conn)
        deps.ops_alert(
            conn,
            "KAP listesi tek gün için 2000 satır sınırına ulaştı ("
            + ", ".join(d.isoformat() for d in report.truncated_days)
            + "); eksikler boşluk taramasıyla tamamlanacak.",
            dedupe_key="kap:list:truncated",
        )
    if catchup:
        mins = int((gap or 0) // 60)
        log.info("KAP catch-up: %s min since last poll, %s new", mins, len(report.new))
        if cadence.in_session_window(now) and (gap or 0) > 600:
            deps.ops_alert(
                conn,
                f"KAP yoklaması {mins} dk aksadı; telafi taraması yapıldı ({len(report.new)} yeni bildirim).",
                level="info",
                dedupe_key="kap:poll:catchup",
            )
    if clamped:
        deps.ops_alert(
            conn,
            f"KAP yoklaması {CATCHUP_MAX_DAYS} günden uzun süre durdu; "
            "daha eskisi için `papi kap backfill` çalıştırın.",
            dedupe_key="kap:poll:clamped",
        )
    conn.commit()
    return report


def _max_index(conn: Connection) -> int | None:
    sid = kap_source_id(conn)
    return conn.execute(
        select(func.max(cast(disclosure.c.source_ref, Integer))).where(disclosure.c.source_id == sid)
    ).scalar_one_or_none()


# ----------------------------------------------------------------------------------------- reconcile


def reconcile(
    conn: Connection, client: KapClient, *, now: datetime | None = None, hours: int | None = None
) -> dict[str, Any]:
    """Re-list the last ``kap.reconcile_window_hours``: insert late items, refresh changed list items,
    refresh rows missing from the list (takedown check), re-enqueue failed fetches, and record our
    count vs KAP's per day (``kap.reconcile.diff``)."""
    now = now or now_utc()
    hours = hours or get_settings().kap.reconcile_window_hours
    since = now - timedelta(hours=hours)
    lo, hi = ist_date(since), ist_date(now)
    release(conn)
    res = client.list_disclosures(lo, hi)
    diff = record_listing(
        conn, res.items, mode="reconcile", now=now, catchup_before=now - timedelta(minutes=10)
    )
    sid = kap_source_id(conn)
    listed = {str(it["disclosureIndex"]) for it in res.items if isinstance(it.get("disclosureIndex"), int)}
    lo_dt = datetime.combine(lo, datetime.min.time(), tzinfo=to_ist(now).tzinfo)
    ours = conn.execute(
        select(
            disclosure.c.id,
            disclosure.c.source_ref,
            disclosure.c.ingest_status,
            disclosure.c.published_at,
            disclosure.c.first_seen_at,
            disclosure.c.extra,
        ).where(disclosure.c.source_id == sid, disclosure.c.published_at >= lo_dt)
    ).all()
    refresh = list(diff.changed)
    refetch: list[str] = []
    new_refs = {n["ref"] for n in diff.new}
    stale = now - timedelta(minutes=30)
    for r in ours:
        ex = r.extra or {}
        if r.source_ref not in listed and ex.get("discovered_by") != "gap" and not ex.get("blocked"):
            refresh.append(r.source_ref)
        failed = (r.ingest_status or "").startswith("failed:fetch")
        stuck = r.ingest_status in ("seen", "fetched") and r.first_seen_at < stale
        if r.source_ref not in new_refs and (failed or stuck):
            refetch.append(r.source_ref)
    gen = now.strftime("%Y%m%d%H")
    for ref in sorted(set(refresh)):
        jobs.enqueue(
            conn,
            JOB_REFRESH,
            {"ref": ref, "reason": "reconcile"},
            priority=PRIO_RECONCILE,
            dedupe_key=f"{JOB_REFRESH}:{ref}:{gen}",
        )
    for ref in refetch:
        enqueue_fetch(conn, ref, mode="reconcile", is_catchup=True, label=None, generation=f"r{gen}")
    per_day_kap: dict[str, int] = {}
    for it in res.items:
        d = str(it.get("publishDate", ""))[:10]
        per_day_kap[d] = per_day_kap.get(d, 0) + 1
    per_day_ours: dict[str, int] = {}
    for r in ours:
        if r.published_at and r.source_ref in listed:
            d = to_ist(r.published_at).strftime("%d.%m.%Y")
            per_day_ours[d] = per_day_ours.get(d, 0) + 1
    diffs = {d: per_day_kap[d] - per_day_ours.get(d, 0) for d in per_day_kap}
    for d, v in diffs.items():
        metrics.record("kap.reconcile.diff", v, conn=conn, day=d)
    summary = {
        "at": now.isoformat(),
        "window": [lo.isoformat(), hi.isoformat()],
        "listed": len(res.items),
        "new": len(diff.new),
        "changed": len(diff.changed),
        "refresh": len(set(refresh)),
        "refetch": len(refetch),
        "per_day_diff": diffs,
    }
    set_state(conn, RECONCILE_KEY, summary)
    conn.commit()
    return summary


# ----------------------------------------------------------------------------------------- backfill


def member_oids_for_tickers(conn: Connection, tickers: list[str], at: date) -> list[str]:
    resolved = companies.resolve_tickers(conn, tickers, at)
    issuers = {v[1] for v in resolved.values() if v[1]}
    if not issuers:
        return []
    rows = conn.execute(
        select(identifier.c.value).where(
            identifier.c.scheme == "KAP_MEMBER_OID",
            identifier.c.entity_type == "issuer",
            identifier.c.entity_id.in_(issuers),
            identifier.c.valid_to.is_(None),
        )
    ).all()
    return sorted({r.value for r in rows})


def backfill(
    conn: Connection,
    client: KapClient,
    start: date,
    end: date,
    *,
    tickers: list[str] | None = None,
    max_windows: int | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    """Resumable history load, newest window first; enqueues ``kap:fetch`` at backfill priority.

    The cursor (``kap:backfill:cursor``) remembers the next window end for the same
    ``(start, end, tickers)`` request, so a stopped run resumes where it left off.
    """
    tickers = sorted({t.upper() for t in tickers or []})
    cursor = get_state(conn, BACKFILL_KEY, {}) or {}
    same = (
        cursor.get("from") == start.isoformat()
        and cursor.get("to") == end.isoformat()
        and cursor.get("tickers") == tickers
    )
    next_to = date.fromisoformat(cursor["next_to"]) if same and cursor.get("next_to") and not restart else end
    oids: list[str] = []
    if tickers:
        oids = member_oids_for_tickers(conn, tickers, end)
        if not oids:
            raise ValueError(
                f"no KAP member found for tickers {tickers}; run `papi kap sync-companies` first"
            )
    width = BACKFILL_MEMBER_WINDOW_DAYS if oids else BACKFILL_WINDOW_DAYS
    totals = {"windows": 0, "listed": 0, "new": 0, "requests": 0}
    while next_to >= start:
        if max_windows is not None and totals["windows"] >= max_windows:
            break
        lo = max(start, next_to - timedelta(days=width - 1))
        release(conn)
        res = client.list_disclosures(lo, next_to, member_oids=oids)
        diff = record_listing(conn, res.items, mode="backfill", publish_seen=False)
        totals["windows"] += 1
        totals["listed"] += len(res.items)
        totals["new"] += len(diff.new)
        totals["requests"] += res.requests
        next_to = lo - timedelta(days=1)
        set_state(
            conn,
            BACKFILL_KEY,
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "tickers": tickers,
                "next_to": next_to.isoformat(),
                "done": next_to < start,
                "updated_at": now_utc().isoformat(),
                "totals": totals,
            },
        )
        conn.commit()
    totals["done"] = next_to < start
    totals["next_to"] = next_to.isoformat()
    return totals
