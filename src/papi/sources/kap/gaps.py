"""Gap detection on the monotonic ``disclosureIndex`` (ING-G1) and probing of missing indices.

State (kv_state ``kap:gaps``)::

    {"watermark": 1688500,            # every index ≤ watermark is resolved
     "open": {"1688553": {"tries": 1, "last": "<iso>", "since": "<iso>"}},
     "skipped": [1688612],            # exists but out of scope (fund disclosures; > watermark only)
     "dead": [1688599]}               # never answered after MAX_TRIES (non-public / never published)

Many integers are legitimately absent from the company list: fund disclosures (``disclosureType``
``FON``) share the sequence (about a third of all indices). ``scan_gaps`` resolves them with one
request to KAP's fund-disclosure list for the gap's days and records them as skipped; only the rest
are probed: ``attachment-detail/<index>``; in-scope hits are ingested (``discovered_by = gap``), fund
hits are skipped, unknown indices (HTTP 500 ``success:false``) are retried with growing spacing
(10 min doubling) and given up after ``MAX_TRIES``; at most ``PROBES_PER_RUN`` probes are enqueued per
scan, at ``PRIO_GAP`` (behind live and catch-up fetches).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Integer, cast, select

from papi.core import events, jobs, metrics
from papi.core.schema import disclosure
from papi.core.state import get_state, set_state
from papi.core.timeutil import ist_date, now_utc, parse_iso
from papi.sources.kap.client import KapClient, KapError, KapJson, KapNotFound
from papi.sources.kap.common import EVENT_SOURCE, JOB_PROBE, PRIO_GAP, kap_source_id, published_at, release
from papi.sources.kap.ingest import FetchOutcome, process_disclosure

log = logging.getLogger("papi.kap.gaps")

GAPS_KEY = "kap:gaps"
MAX_TRIES = 5
RETRY_BASE_S = 600.0  # 10 min, doubling per try
PROBES_PER_RUN = 50
CATCHUP_AGE = timedelta(minutes=15)
FUND_LIST_MAX_DAYS = 31


@dataclass
class GapReport:
    watermark: int | None
    open: int
    new_missing: list[int] = field(default_factory=list)
    enqueued: int = 0
    closed: int = 0
    funds_skipped: int = 0


def load_state(conn: Connection) -> dict[str, Any]:
    st = get_state(conn, GAPS_KEY, {}) or {}
    st.setdefault("watermark", None)
    st.setdefault("open", {})
    st.setdefault("skipped", [])
    st.setdefault("dead", [])
    return st


def _known(conn: Connection, sid: str, above: int) -> set[int]:
    idx = cast(disclosure.c.source_ref, Integer)
    rows = conn.execute(select(idx).where(disclosure.c.source_id == sid, idx > above)).all()
    return {int(r[0]) for r in rows}


def _advance(st: dict[str, Any], known: set[int]) -> None:
    resolved = known | set(st["skipped"]) | set(st["dead"])
    wm = st["watermark"]
    while wm is not None and (wm + 1) in resolved:
        wm += 1
    st["watermark"] = wm
    st["skipped"] = sorted(i for i in st["skipped"] if wm is None or i > wm)
    st["dead"] = sorted(i for i in st["dead"] if wm is None or i > wm)


def detect_gaps(
    conn: Connection, *, now: datetime | None = None, enqueue: bool = True, client: KapClient | None = None
) -> GapReport:
    """Find missing indices between the watermark and the highest known ref; enqueue probes (caller
    commits). With ``client``, open indices never probed yet that KAP lists as fund disclosures are
    skipped without probing (one list request; the open transaction is committed first)."""
    now = now or now_utc()
    sid = kap_source_id(conn)
    st = load_state(conn)
    if st["watermark"] is None:
        idx = cast(disclosure.c.source_ref, Integer)
        lo = conn.execute(select(idx).where(disclosure.c.source_id == sid).order_by(idx).limit(1)).scalar()
        if lo is None:
            return GapReport(watermark=None, open=0)
        st["watermark"] = int(lo) - 1
    known = _known(conn, sid, st["watermark"])
    report = GapReport(watermark=st["watermark"], open=0)
    if not known:
        set_state(conn, GAPS_KEY, st)
        return report
    hi = max(known)
    skip = set(st["skipped"]) | set(st["dead"])
    open_: dict[str, Any] = st["open"]
    for key in [k for k in open_ if int(k) in known]:
        del open_[key]
        report.closed += 1
    for i in range(st["watermark"] + 1, hi):
        if i in known or i in skip or str(i) in open_:
            continue
        open_[str(i)] = {"tries": 0, "since": now.isoformat(), "last": None}
        report.new_missing.append(i)
    _advance(st, known)
    st["open"] = {k: v for k, v in open_.items() if st["watermark"] is None or int(k) > st["watermark"]}
    unprobed = {int(k) for k, v in st["open"].items() if not v.get("tries") and not v.get("fund_checked")}
    if client is not None and unprobed:
        set_state(conn, GAPS_KEY, st)
        funds = _listed_funds(conn, client, st["watermark"], now)
        st = load_state(conn)
        if funds is not None:
            for i in funds:
                st["open"].pop(str(i), None)
            for i in unprobed - funds:
                if str(i) in st["open"]:
                    st["open"][str(i)]["fund_checked"] = True
            st["skipped"] = sorted(set(st["skipped"]) | funds)
            _advance(st, known)
            report.new_missing = [i for i in report.new_missing if i not in funds]
            report.funds_skipped = len(unprobed & funds)
    report.watermark = st["watermark"]
    report.open = len(st["open"])
    if report.new_missing:
        events.publish(
            conn,
            events.INGEST_GAP_DETECTED,
            f"source/{sid}",
            {"source_id": sid, "missing_refs": [str(i) for i in report.new_missing]},
            EVENT_SOURCE,
        )
    if enqueue:
        report.enqueued = _enqueue_due(conn, st, now)
    set_state(conn, GAPS_KEY, st)
    metrics.record("kap.gaps.open", report.open, conn=conn)
    return report


def scan_gaps(conn: Connection, client: KapClient, *, now: datetime | None = None) -> GapReport:
    """Periodic gap scan: ``detect_gaps`` with fund-list resolution, committed."""
    report = detect_gaps(conn, now=now, client=client)
    conn.commit()
    return report


def _listed_funds(
    conn: Connection, client: KapClient, watermark: int | None, now: datetime
) -> set[int] | None:
    """Fund-disclosure indices above ``watermark`` from KAP's fund list, for the days since the
    watermark's disclosure (at most ``FUND_LIST_MAX_DAYS``). ``None`` on any KAP error (the indices
    are then probed one by one)."""
    sid = kap_source_id(conn)
    idx = cast(disclosure.c.source_ref, Integer)
    base = select(disclosure.c.published_at).where(
        disclosure.c.source_id == sid, disclosure.c.published_at.is_not(None)
    )
    pub = None
    if watermark is not None:
        pub = conn.execute(base.where(idx <= watermark).order_by(idx.desc()).limit(1)).scalar()
        if pub is None:
            pub = conn.execute(base.where(idx > watermark).order_by(idx).limit(1)).scalar()
    today = ist_date(now)
    start = max(ist_date(pub) if pub is not None else today, today - timedelta(days=FUND_LIST_MAX_DAYS))
    release(conn)
    try:
        res = client.list_fund_disclosures(min(start, today), today)
    except KapError as exc:
        log.warning("kap gaps: fund list unavailable (%s); probing instead", exc)
        return None
    return {
        i
        for it in res.items
        if isinstance(i := it.get("disclosureIndex"), int) and (watermark is None or i > watermark)
    }


def _due(entry: dict[str, Any], now: datetime) -> bool:
    if not entry.get("last"):
        return True
    wait = RETRY_BASE_S * 2 ** max(int(entry.get("tries", 0)) - 1, 0)
    return now >= parse_iso(entry["last"]) + timedelta(seconds=wait)


def _enqueue_due(conn: Connection, st: dict[str, Any], now: datetime) -> int:
    n = 0
    for key in sorted(st["open"], key=int, reverse=True):
        entry = st["open"][key]
        if not _due(entry, now):
            continue
        jobs.enqueue(
            conn,
            JOB_PROBE,
            {"index": int(key)},
            priority=PRIO_GAP,
            dedupe_key=f"{JOB_PROBE}:{key}:{entry.get('tries', 0)}",
            max_attempts=3,
        )
        n += 1
        if n >= PROBES_PER_RUN:
            break
    return n


def is_out_of_scope(detail: dict[str, Any]) -> bool:
    basic = (detail.get("disclosure") or {}).get("disclosureBasic") or {}
    return basic.get("disclosureType") == "FON" or bool(basic.get("fundType"))


def probe_index(conn: Connection, client: KapClient, index: int, *, now: datetime | None = None) -> str:
    """Probe one missing index. Returns ``ingested`` | ``skipped`` | ``missing`` | ``dead`` | ``known``."""
    now = now or now_utc()
    sid = kap_source_id(conn)
    ref = str(index)
    exists = conn.execute(
        select(disclosure.c.id).where(disclosure.c.source_id == sid, disclosure.c.source_ref == ref)
    ).first()
    if exists is not None:
        _close(conn, index, None)
        conn.commit()
        return "known"
    st = load_state(conn)
    if index in st["skipped"] or (st["watermark"] is not None and index <= st["watermark"]):
        return "skipped"  # resolved meanwhile (fund list), no request needed
    if index in st["dead"]:
        return "dead"
    release(conn)
    try:
        detail: KapJson = client.disclosure_detail(index)
    except KapNotFound:
        st = load_state(conn)
        entry = st["open"].get(ref) or {"tries": 0, "since": now.isoformat()}
        entry["tries"] = int(entry.get("tries", 0)) + 1
        entry["last"] = now.isoformat()
        outcome = "missing"
        if entry["tries"] >= MAX_TRIES:
            st["open"].pop(ref, None)
            st["dead"] = sorted(set(st["dead"]) | {index})
            metrics.record("kap.gaps.dead", 1, conn=conn)
            outcome = "dead"
        else:
            st["open"][ref] = entry
        set_state(conn, GAPS_KEY, st)
        conn.commit()
        return outcome
    if is_out_of_scope(detail.data):
        _close(conn, index, "skipped")
        conn.commit()
        return "skipped"
    basic = (detail.data.get("disclosure") or {}).get("disclosureBasic") or {}
    pub = published_at(basic)
    is_catchup = pub is None or pub < now - CATCHUP_AGE
    outcome: FetchOutcome = process_disclosure(
        conn, client, ref, mode="gap", is_catchup=is_catchup, detail=detail
    )
    _close(conn, index, None)
    conn.commit()
    return "ingested" if outcome.status == "normalized" else outcome.status


def _close(conn: Connection, index: int, as_: str | None) -> None:
    st = load_state(conn)
    st["open"].pop(str(index), None)
    if as_ == "skipped":
        st["skipped"] = sorted(set(st["skipped"]) | {index})
    set_state(conn, GAPS_KEY, st)
