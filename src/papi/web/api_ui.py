"""Typed ``/v1`` endpoints for the React UI (14 §6 phase P1): feed, event detail + feedback, portfolio,
performance, instrument, prices, today and search.

Thin wrappers over :mod:`papi.web.queries` (one data layer for HTML pages and JSON): every route has a
Pydantic ``response_model`` from :mod:`papi.web.schemas`, so the frontend's TypeScript types are generated
from the OpenAPI schema. The older untyped routes in :mod:`papi.web.api` are unchanged.
"""

from __future__ import annotations

import base64
import bisect
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, and_, func, literal, or_, select
from sqlalchemy.exc import OperationalError

from papi.agents import runs
from papi.core import db, fts
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.registry import load_plugins
from papi.core.schema import (
    disclosure,
    event,
    feedback,
    fx_rate,
    identifier,
    index_membership,
    instrument,
    issuer,
    job,
    macro_series,
    price_bar,
    source,
    watchlist,
)
from papi.core.text import normalize_tr, tr_upper
from papi.core.timeutil import iso_utc, ist_date, now_utc, parse_iso, to_ist
from papi.run import status as run_status
from papi.run.config import run_config
from papi.run.lanes import lane_for
from papi.sources.prices.service import INDEX_NAMES
from papi.thesis import document as thesis_doc
from papi.thesis import importer as thesis_importer
from papi.web import integrations, queries, schemas
from papi.web.deps import get_conn, parse_feed_filters, render_document
from papi.web.schemas import ErrorOut

router = APIRouter(prefix="/v1")
Conn = Annotated[Connection, Depends(get_conn)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut}}

_MIN_DT = datetime(1900, 1, 1, tzinfo=now_utc().tzinfo)
_MAX_DT = datetime(2999, 12, 31, tzinfo=now_utc().tzinfo)
_BIG = 1 << 60


def _f(value: Any) -> float | None:
    return float(value) if value is not None else None


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} bulunamadı")


def _kind(type_: str | None) -> schemas.InstrumentKind:
    if type_ == "equity":
        return "stock"
    if type_ in ("fund", "etf"):
        return "fund"
    if type_ == "index":
        return "index"
    return "other"


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------------------------------ prices helper


def _closes(
    conn: Connection, ids: list[str], since: date, until: date | None = None
) -> dict[str, list[tuple[date, float]]]:
    """Daily closes per instrument from ``since`` (one query; the primary source wins a day)."""
    if not ids:
        return {}
    primary = get_settings().prices.primary
    stmt = (
        select(price_bar.c.instrument_id, price_bar.c.ts, price_bar.c.close, source.c.code)
        .join(source, source.c.id == price_bar.c.source_id)
        .where(
            price_bar.c.instrument_id.in_(ids),
            price_bar.c.interval == "1d",
            price_bar.c.ts >= since,
            price_bar.c.close.is_not(None),
        )
    )
    if until is not None:
        stmt = stmt.where(price_bar.c.ts <= until)
    by: dict[str, dict[date, tuple[bool, float]]] = defaultdict(dict)
    for r in conn.execute(stmt):
        cand = (r.code == primary, float(r.close))
        cur = by[r.instrument_id].get(r.ts)
        if cur is None or (cand[0] and not cur[0]):
            by[r.instrument_id][r.ts] = cand
    return {i: [(d, v[1]) for d, v in sorted(days.items())] for i, days in by.items()}


def _last_two(series: list[tuple[date, float]], at_or_before: date | None = None) -> tuple[Any, Any]:
    pts = [p for p in series if at_or_before is None or p[0] <= at_or_before]
    return (pts[-1] if pts else None, pts[-2] if len(pts) > 1 else None)


# ------------------------------------------------------------------------------------------ feed


def _items(rows: list[dict[str, Any]]) -> list[schemas.FeedItem]:
    return [schemas.FeedItem.model_validate(r) for r in rows]


_LEADING_INT = re.compile(r"^\s*[+-]?\d+")


def _kap_int(ref: str | None) -> int | None:
    """What SQLite's ``CAST(source_ref AS INTEGER)`` yields (the feed's tie-break key)."""
    if ref is None:
        return None
    m = _LEADING_INT.match(ref)
    return int(m.group()) if m else 0


def _encode_cursor(payload: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> list[Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="geçersiz imleç") from exc
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="geçersiz imleç")
    return data


def _keyset(sort: str, cur: list[Any]) -> Any:
    """Rows strictly after ``cur`` = [published_iso, kap_index, event_id] in the feed's order."""
    if len(cur) != 3:
        raise HTTPException(status_code=400, detail="geçersiz imleç")
    desc = sort != "time_asc"
    ptype = queries.published_col.type
    pub = queries.published_col
    kap = queries.kap_index_col
    pub_eff = func.coalesce(pub, literal(_MIN_DT if desc else _MAX_DT, ptype))
    kap_eff = func.coalesce(kap, -1 if desc else _BIG)
    try:
        p = parse_iso(str(cur[0]))
        k = int(cur[1])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="geçersiz imleç") from exc
    i = str(cur[2])
    pv = literal(p, ptype)
    cmp = (lambda a, b: a < b) if desc else (lambda a, b: a > b)
    return or_(
        cmp(pub_eff, pv),
        and_(pub_eff == pv, or_(cmp(kap_eff, k), and_(kap_eff == k, cmp(event.c.id, i)))),
    )


@router.get("/feed", response_model=schemas.FeedOut, responses={400: {"model": ErrorOut}})
def get_feed(
    conn: Conn,
    scope: Annotated[Literal["mine", "holdings", "watchlist", "all"], Query()] = "mine",
    priority: Annotated[Literal["P1", "P2", "P3", "P4"] | None, Query()] = None,
    band: Annotated[Literal["low", "medium", "high", "critical"] | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    range_: Annotated[Literal["today", "yesterday", "7d"] | None, Query(alias="range")] = None,
    ticker: Annotated[str | None, Query()] = None,
    family: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    sort: Annotated[Literal["time_desc", "time_asc", "materiality"], Query()] = "time_desc",
    q: Annotated[str | None, Query(description="full-text search over disclosure text")] = None,
    hide_routine: Annotated[bool, Query()] = False,
    materiality_gte: Annotated[int | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(description="``next_cursor`` of the previous page")] = None,
    after: Annotated[
        str | None, Query(description="``published_key``: only items published at/after it (live polling)")
    ] = None,
) -> schemas.FeedOut:
    """Disclosure tape. Page back with ``before=<next_cursor>``; poll new items with
    ``after=<newest published_key>`` (inclusive, so the client de-duplicates by ``id``)."""
    params: dict[str, str] = {"scope": scope, "sort": sort, "limit": str(limit)}
    for key, val in (
        ("priority", priority),
        ("band", band),
        ("from", date_from.isoformat() if date_from else None),
        ("to", date_to.isoformat() if date_to else None),
        ("range", range_),
        ("ticker", ticker),
        ("family", family),
        ("code", code),
        ("materiality_gte", materiality_gte),
    ):
        if val is not None:
            params[key] = str(val)
    if hide_routine:
        params["hide_routine"] = "1"
    filters = parse_feed_filters(params)
    sc = queries.load_scope(conn)
    stmt = queries._feed_stmt(conn, filters, sc)
    if q and q.strip():
        try:
            ids = [h.id for h in fts.search(conn, "disclosure_fts", q, limit=300)]
        except OperationalError:
            ids = []
        stmt = stmt.where(event.c.primary_disclosure_id.in_(ids))
    offset = 0
    if after:
        try:
            stmt = stmt.where(queries.published_col >= parse_iso(after))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="geçersiz after") from exc
    elif before:
        cur = _decode_cursor(before)
        if sort == "materiality":
            offset = int(cur[0]) if cur and str(cur[0]).lstrip("-").isdigit() else 0
        else:
            stmt = stmt.where(_keyset(sort, cur))
    stmt = stmt.limit(limit + (0 if after else 1))
    if offset:
        stmt = stmt.offset(offset)
    rows = conn.execute(stmt).all()
    more = not after and len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        if sort == "materiality":
            next_cursor = _encode_cursor([offset + limit])
        else:
            when = last.announced_at or last.published_at
            kap = _kap_int(last.source_ref)
            desc = sort != "time_asc"
            when_eff = when or (_MIN_DT if desc else _MAX_DT)
            kap_eff = kap if kap is not None else (-1 if desc else _BIG)
            next_cursor = _encode_cursor([iso_utc(when_eff), kap_eff, last.id])
    items = _items(queries._decorate(conn, rows, sc))
    families = [schemas.FamilyOut(code=c, label=lbl) for c, lbl in queries.feed_families(conn)]
    return schemas.FeedOut(items=items, next_cursor=next_cursor, facets=schemas.FeedFacets(families=families))


# ------------------------------------------------------------------------------------------ event detail


def _feedback_counts(conn: Connection, event_id: str) -> dict[str, int]:
    rows = conn.execute(
        select(feedback.c.kind, func.count())
        .where(feedback.c.target_type == "event", feedback.c.target_id == event_id)
        .group_by(feedback.c.kind)
    ).all()
    return {k: n for k, n in rows}


def _document_out(d: dict[str, Any]) -> schemas.DocumentOut:
    html, rest = render_document(d)
    sha = d.get("raw_sha256")
    return schemas.DocumentOut(
        index=d["index"],
        id=d["id"],
        kind=d["kind"],
        mime=d["mime"],
        filename=d["filename"],
        byte_size=d["byte_size"],
        raw_sha256=sha,
        raw_url=d["raw_url"],
        download_url=f"/raw/{sha}" if sha else None,
        template_id=(d["structure"] or {}).get("template_id"),
        text_length=len(d["text"] or ""),
        html=str(html),
        spans=[schemas.SpanOut.model_validate(s) for s in d["spans"]],
        unplaced_span_ids=[s["id"] for s in rest],
    )


def _summary(value: dict[str, Any] | None) -> schemas.SummaryOut | None:
    return schemas.SummaryOut.model_validate(value) if value else None


@router.get("/events/{event_id}/detail", response_model=schemas.EventDetailOut, responses=NOT_FOUND)
def get_event_detail(event_id: str, conn: Conn) -> schemas.EventDetailOut:
    d = queries.event_detail(conn, event_id, queries.load_scope(conn))
    if d is None:
        raise _not_found("olay")
    summaries = d["summaries"]
    return schemas.EventDetailOut(
        item=schemas.FeedItem.model_validate(d["item"]),
        event=schemas.EventMeta.model_validate(d["event"]),
        disclosures=[schemas.DisclosureRef.model_validate(x) for x in d["disclosures"]],
        revision_chain=[schemas.RevisionLink.model_validate(x) for x in d["revision_chain"]],
        superseded=[schemas.SupersededEvent.model_validate(x) for x in d["superseded"]],
        summaries=schemas.SummariesOut(tr=_summary(summaries.get("tr")), en=_summary(summaries.get("en"))),
        analyses=[schemas.AnalysisOut.model_validate(x) for x in d["analyses"]],
        key_facts=[schemas.KeyFact.model_validate(x) for x in d["key_facts"]],
        documents=[_document_out(x) for x in d["documents"]],
        forecasts=[schemas.ForecastOut.model_validate(x) for x in d["forecasts"]],
        for_me=schemas.ForMeOut.model_validate(d["for_me"]),
        feedback_counts=d["feedback_counts"],
    )


@router.post(
    "/events/{event_id}/feedback",
    response_model=schemas.FeedbackOut,
    responses=NOT_FOUND,
    status_code=200,
)
def post_event_feedback(event_id: str, body: schemas.FeedbackIn) -> schemas.FeedbackOut:
    with db.transaction() as conn:
        if conn.execute(select(event.c.id).where(event.c.id == event_id)).first() is None:
            raise _not_found("olay")
        conn.execute(
            feedback.insert().values(
                id=uuid7(),
                target_type=body.target_type,
                target_id=body.target_id or event_id,
                kind=body.kind,
                comment=(body.comment or "").strip() or None,
                created_at=now_utc(),
            )
        )
    with db.connect() as conn:
        return schemas.FeedbackOut(event_id=event_id, feedback_counts=_feedback_counts(conn, event_id))


# ------------------------------------------------------------------------------------------ portfolio


def _instrument_meta(conn: Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = conn.execute(
        select(
            instrument.c.id,
            instrument.c.type,
            issuer.c.short_name,
            issuer.c.legal_name,
            issuer.c.sector_code,
        )
        .select_from(instrument.outerjoin(issuer, issuer.c.id == instrument.c.issuer_id))
        .where(instrument.c.id.in_(ids))
    ).all()
    return {
        r.id: {
            "kind": _kind(r.type),
            "name": r.short_name or r.legal_name,
            "sector": r.sector_code,
        }
        for r in rows
    }


def _positions(conn: Connection, scope: queries.Scope) -> list[schemas.PositionOut]:
    ids = [p.instrument_id for p in scope.positions]
    meta = _instrument_meta(conn, ids)
    series = _closes(conn, ids, ist_date() - timedelta(days=60))
    out: list[schemas.PositionOut] = []
    for p in scope.positions:
        base = queries.position_row(p)
        prev_close = prev_date = None
        day_change = day_pct = None
        if p.last_close is not None and p.last_close_date is not None:
            earlier = [pt for pt in series.get(p.instrument_id, []) if pt[0] < p.last_close_date]
            if earlier:
                prev_date, prev_close = earlier[-1]
                if prev_close:
                    day_change = float(p.quantity) * (float(p.last_close) - prev_close)
                    day_pct = (float(p.last_close) / prev_close - 1) * 100
        m = meta.get(p.instrument_id, {})
        out.append(
            schemas.PositionOut(
                instrument_id=p.instrument_id,
                ticker=p.ticker,
                name=m.get("name"),
                kind=m.get("kind", "other"),
                quantity=float(p.quantity),
                avg_cost=_f(p.avg_cost),
                cost_total=float(p.cost_total),
                last_close=_f(p.last_close),
                last_close_date=p.last_close_date,
                prev_close=prev_close,
                prev_close_date=prev_date,
                market_value=_f(p.market_value),
                weight=p.weight,
                pnl=_f(base["pnl"]),
                pnl_pct=base["pnl_pct"],
                day_change=day_change,
                day_change_pct=day_pct,
                badge=scope.badge(p.instrument_id),
            )
        )
    out.sort(key=lambda r: -(r.market_value or 0.0))
    return out


def _totals(positions: list[schemas.PositionOut]) -> schemas.PortfolioTotals:
    valued = [p for p in positions if p.market_value is not None]
    value = sum(p.market_value or 0.0 for p in valued)
    cost = sum(p.cost_total for p in valued)
    moved = [p for p in positions if p.day_change is not None and p.prev_close]
    day = sum(p.day_change or 0.0 for p in moved)
    prev_value = sum(p.quantity * (p.prev_close or 0.0) for p in moved)
    return schemas.PortfolioTotals(
        value=value if valued else None,
        cost=cost if valued else None,
        pnl=value - cost if valued else None,
        pnl_pct=(value - cost) / cost * 100 if valued and cost else None,
        day_change=day if moved else None,
        day_change_pct=day / prev_value * 100 if moved and prev_value else None,
        positions=len(positions),
        unvalued=[p.ticker for p in positions if p.market_value is None],
    )


def _allocation(conn: Connection, positions: list[schemas.PositionOut]) -> schemas.AllocationOut:
    valued = [p for p in positions if p.market_value]
    total = sum(p.market_value or 0.0 for p in valued)
    meta = _instrument_meta(conn, [p.instrument_id for p in valued])

    def slices(groups: dict[str, tuple[str, float]]) -> list[schemas.AllocationSlice]:
        rows = [
            schemas.AllocationSlice(key=k, label=label, value=v, weight=v / total if total else 0.0)
            for k, (label, v) in groups.items()
        ]
        return sorted(rows, key=lambda s: -s.value)

    by_inst = {p.ticker: (p.name or p.ticker, p.market_value or 0.0) for p in valued}
    by_kind: dict[str, tuple[str, float]] = {}
    by_sector: dict[str, tuple[str, float]] = {}
    for p in valued:
        by_kind[p.kind] = (p.kind, by_kind.get(p.kind, ("", 0.0))[1] + (p.market_value or 0.0))
        sector = meta.get(p.instrument_id, {}).get("sector") or "unknown"
        by_sector[sector] = (sector, by_sector.get(sector, ("", 0.0))[1] + (p.market_value or 0.0))
    return schemas.AllocationOut(
        by_instrument=slices(by_inst), by_kind=slices(by_kind), by_sector=slices(by_sector)
    )


def _txn_out(t: dict[str, Any]) -> schemas.TxnOut:
    return schemas.TxnOut.model_validate(t)


def _pending(conn: Connection, scope: queries.Scope) -> list[schemas.PendingActionOut]:
    rows = integrations.pending_actions(conn) if scope.portfolio_available else None
    return [schemas.PendingActionOut.model_validate(r) for r in rows or []]


def _watchlists(conn: Connection, scope: queries.Scope) -> list[schemas.WatchlistOut]:
    rows = integrations.watchlists(conn)
    if rows is None:
        rows = [
            {
                "id": w.id,
                "name": w.name,
                "instrument_ids": list(w.instrument_ids or []),
                "tickers": [],
                "sectors": list(w.sectors or []),
                "indices": list(w.indices or []),
            }
            for w in conn.execute(select(watchlist).order_by(watchlist.c.name))
        ]
    ids = [i for w in rows for i in w["instrument_ids"]]
    meta = _instrument_meta(conn, ids)
    tick = queries.tickers_for(conn, ids)
    return [
        schemas.WatchlistOut(
            id=w["id"],
            name=w["name"],
            members=[
                schemas.WatchMember(
                    instrument_id=i,
                    ticker=tick.get(i, "?"),
                    name=meta.get(i, {}).get("name"),
                    badge=scope.badge(i),
                )
                for i in w["instrument_ids"]
            ],
            sectors=w["sectors"],
            indices=w["indices"],
        )
        for w in rows
    ]


@router.get("/portfolio/overview", response_model=schemas.PortfolioOverviewOut)
def get_portfolio_overview(conn: Conn) -> schemas.PortfolioOverviewOut:
    scope = queries.load_scope(conn)
    positions = _positions(conn, scope)
    return schemas.PortfolioOverviewOut(
        available=scope.portfolio_available,
        positions=positions,
        totals=_totals(positions),
        allocation=_allocation(conn, positions),
        transactions=[_txn_out(t) for t in queries.recent_txns(conn, limit=50)],
        pending_actions=_pending(conn, scope),
        watchlists=_watchlists(conn, scope),
    )


def _range_start(range_: str, today: date) -> date | None:
    if range_ == "max":
        return None
    if range_ == "ytd":
        return date(today.year, 1, 1)
    return today - timedelta(days={"1m": 30, "3m": 91, "6m": 182, "1y": 365}[range_])


@router.get("/portfolio/performance", response_model=schemas.PerformanceOut)
def get_portfolio_performance(
    conn: Conn, range_: Annotated[schemas.PerformanceRange, Query(alias="range")] = "3m"
) -> schemas.PerformanceOut:
    """Daily portfolio value reconstructed from the transaction ledger x daily closes (last known close is
    carried over days without a bar). ``return_index`` is a price-return index (flows excluded)."""
    today = ist_date()
    start = _range_start(range_, today)

    def empty() -> schemas.PerformanceOut:
        return schemas.PerformanceOut(
            range=range_,
            method="transactions_x_closes",
            start=None,
            end=None,
            points=[],
            benchmark=[],
            unpriced=[],
        )

    svc = integrations._optional("papi.portfolio.service")
    if svc is None:
        return empty()
    pid = svc.default_portfolio_id(conn)
    holdings: dict[str, Any] = {}
    steps: dict[str, list[tuple[date, Decimal, Decimal]]] = defaultdict(list)
    for r in svc._txn_rows(conn, portfolio_id=pid):
        h = holdings.get(r.instrument_id)
        if h is None:
            h = holdings[r.instrument_id] = svc.Holding(pid, r.instrument_id)
        h.apply(r)
        steps[r.instrument_id].append((r.trade_date, h.quantity, h.cost_total))
    if not steps:
        return empty()
    first_txn = min(s[0][0] for s in steps.values())
    lo = max(start, first_txn) if start else first_txn
    ids = list(steps)
    closes = _closes(conn, ids, lo - timedelta(days=14), today)
    days = sorted({d for series in closes.values() for d, _ in series if d >= lo})
    tick = queries.tickers_for(conn, ids)
    step_dates = {i: [s[0] for s in v] for i, v in steps.items()}
    close_dates = {i: [p[0] for p in v] for i, v in closes.items()}

    def state(i: str, day: date) -> tuple[Decimal, Decimal]:
        k = bisect.bisect_right(step_dates[i], day)
        return (steps[i][k - 1][1], steps[i][k - 1][2]) if k else (Decimal(0), Decimal(0))

    def close_at(i: str, day: date) -> float | None:
        k = bisect.bisect_right(close_dates.get(i, []), day)
        return closes[i][k - 1][1] if k else None

    points: list[schemas.PerformancePoint] = []
    unpriced: set[str] = set()
    index = 100.0
    prev_day: date | None = None
    for day in days:
        value = cost = 0.0
        for i in ids:
            qty, cst = state(i, day)
            if qty <= 0:
                continue
            c = close_at(i, day)
            if c is None:
                unpriced.add(tick.get(i, i))
                continue
            value += float(qty) * c
            cost += float(cst)
        if value <= 0:
            continue
        if prev_day is not None and points:
            gain = base = 0.0
            for i in ids:
                qty, _ = state(i, prev_day)
                p0, p1 = close_at(i, prev_day), close_at(i, day)
                if qty > 0 and p0 and p1:
                    gain += float(qty) * (p1 - p0)
                    base += float(qty) * p0
            if base:
                index *= 1 + gain / base
        points.append(
            schemas.PerformancePoint(date=day, value=value, cost=cost, pnl=value - cost, return_index=index)
        )
        prev_day = day
    benchmark: list[schemas.BenchmarkPoint] = []
    if points:
        bench_ids = queries.instruments_for_ticker(conn, "XU100")[:1]
        bs = _closes(conn, bench_ids, points[0].date - timedelta(days=14), today).get(
            bench_ids[0] if bench_ids else "", []
        )
        bdates = [b[0] for b in bs]
        base0 = None
        k0 = bisect.bisect_right(bdates, points[0].date)
        if k0:
            base0 = bs[k0 - 1][1]
        if base0:
            for p in points:
                k = bisect.bisect_right(bdates, p.date)
                if k:
                    benchmark.append(schemas.BenchmarkPoint(date=p.date, value=bs[k - 1][1] / base0 * 100))
    return schemas.PerformanceOut(
        range=range_,
        method="transactions_x_closes",
        start=points[0].date if points else None,
        end=points[-1].date if points else None,
        points=points,
        benchmark=benchmark,
        unpriced=sorted(unpriced),
    )


# ------------------------------------------------------------------------------------------ instrument


def _run_brief(meta: dict[str, Any]) -> schemas.AgentRunBrief:
    def num(value: Any, cast: Any) -> Any:
        try:
            return cast(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return schemas.AgentRunBrief(
        run_id=str(meta.get("run_id")),
        agent=str(meta.get("agent")),
        subject=meta.get("subject") or None,
        status=str(meta.get("status") or "unknown"),
        started_at=_dt(meta.get("started_at")),
        finished_at=_dt(meta.get("finished_at")),
        cost_usd=num(meta.get("cost_usd"), float),
        turns=num(meta.get("turns"), int),
        error=str(meta["error"]) if meta.get("error") else None,
    )


def _theses_for(ticker: str, all_runs: list[dict[str, Any]]) -> list[schemas.ThesisRef]:
    pat = re.compile(rf"^{re.escape(ticker)}-\d{{4}}-\d{{2}}-\d{{2}}(-\d+)?$")
    out: list[schemas.ThesisRef] = []
    for name in thesis_importer.list_names():
        if not pat.match(name):
            continue
        direction = horizon = None
        day: date | None = None
        try:
            t = thesis_doc.load(name)
            direction, horizon, day = t.direction, t.horizon, t.date
        except (OSError, thesis_doc.ThesisError, FileNotFoundError):
            pass
        mine = [r for r in all_runs if r.get("agent") == thesis_importer.AGENT and r.get("subject") == name]
        last = mine[0] if mine else None  # list_runs is newest first
        verdict = ((last or {}).get("output") or {}).get("verdict") if last else None
        out.append(
            schemas.ThesisRef(
                name=name,
                date=day,
                direction=direction,
                horizon=horizon,
                runs=len(mine),
                last_run_status=(last or {}).get("status") if last else None,
                last_verdict=verdict if isinstance(verdict, str) else None,
            )
        )
    return out


def _runs_for_instrument(
    conn: Connection, ticker: str, issuer_id: str | None, all_runs: list[dict[str, Any]], limit: int = 10
) -> list[schemas.AgentRunBrief]:
    pat = re.compile(rf"^{re.escape(ticker)}-\d{{4}}-\d{{2}}-\d{{2}}(-\d+)?$")
    picked = [r for r in all_runs if pat.match(str(r.get("subject") or ""))]
    refs = {
        str(r["subject"]).split()[0] for r in all_runs if r.get("agent") == "kap-analyst" and r.get("subject")
    }
    if issuer_id and refs:
        mine = set(
            conn.execute(
                select(disclosure.c.source_ref).where(
                    disclosure.c.source_ref.in_(sorted(refs)), disclosure.c.filer_issuer_id == issuer_id
                )
            ).scalars()
        )
        picked += [
            r
            for r in all_runs
            if r.get("agent") == "kap-analyst"
            and str(r.get("subject") or "").split()[:1]
            and str(r["subject"]).split()[0] in mine
        ]
    picked.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return [_run_brief(r) for r in picked[:limit]]


def _capital(
    inst_extra: dict[str, Any] | None, iss_extra: dict[str, Any] | None, last: float | None
) -> schemas.ShareCapitalOut:
    ie, se = inst_extra or {}, iss_extra or {}

    def num(value: Any) -> float | None:
        try:
            return float(Decimal(str(value))) if value not in (None, "") else None
        except (ArithmeticError, ValueError):
            return None

    shares = num(ie.get("shares_outstanding"))
    return schemas.ShareCapitalOut(
        paid_in_capital=num(se.get("paid_in_capital")),
        registered_capital_ceiling=num(se.get("registered_capital_ceiling")),
        paid_in_capital_as_of=se.get("paid_in_capital_as_of"),
        shares_outstanding=shares,
        shares_as_of=ie.get("shares_as_of"),
        free_float_shares=num(ie.get("free_float_shares")),
        free_float_pct=num(ie.get("free_float_pct")),
        market_cap=shares * last if shares is not None and last is not None else None,
    )


def _resolve(conn: Connection, ticker: str) -> Any:
    ids = queries.instruments_for_ticker(conn, ticker)
    inst = conn.execute(select(instrument).where(instrument.c.id == ids[0])).first() if ids else None
    if inst is None:
        raise _not_found(f"enstrüman {ticker}")
    return inst


@router.get("/instruments/{ticker}", response_model=schemas.InstrumentOut, responses=NOT_FOUND)
def get_instrument(
    ticker: str,
    conn: Conn,
    disclosure_limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> schemas.InstrumentOut:
    inst = _resolve(conn, ticker)
    scope = queries.load_scope(conn)
    tick = tr_upper(ticker.strip())
    iss = (
        conn.execute(select(issuer).where(issuer.c.id == inst.issuer_id)).first() if inst.issuer_id else None
    )
    today = ist_date()
    indices = list(
        conn.execute(
            select(index_membership.c.index_code)
            .where(
                index_membership.c.instrument_id == inst.id,
                index_membership.c.valid_from <= today,
                or_(index_membership.c.valid_to.is_(None), index_membership.c.valid_to >= today),
            )
            .order_by(index_membership.c.index_code)
        ).scalars()
    )
    pairs = [("instrument", inst.id)] + ([("issuer", iss.id)] if iss else [])
    series = _closes(conn, [inst.id], today - timedelta(days=30)).get(inst.id, [])
    (last_pt, prev_pt) = _last_two(series)
    last = None
    if last_pt:
        change = last_pt[1] - prev_pt[1] if prev_pt else None
        last = schemas.LastClose(
            date=last_pt[0],
            close=last_pt[1],
            prev_close=prev_pt[1] if prev_pt else None,
            day_change=change,
            day_change_pct=change / prev_pt[1] * 100
            if change is not None and prev_pt and prev_pt[1]
            else None,
        )
    pos = next((p for p in _positions(conn, scope) if p.instrument_id == inst.id), None)
    wl_names = [
        w.name
        for w in conn.execute(select(watchlist.c.name, watchlist.c.instrument_ids))
        if inst.id in (w.instrument_ids or [])
    ]
    items = queries.feed_items(
        conn, queries.FeedFilters(scope="all", ticker=tick, limit=disclosure_limit), scope
    )
    all_runs = runs.list_runs(200)
    return schemas.InstrumentOut(
        ticker=tick,
        name=(iss.short_name or iss.legal_name) if iss else INDEX_NAMES.get(tick),
        kind=_kind(inst.type),
        instrument=schemas.InstrumentInfo(
            id=inst.id, type=inst.type, currency=inst.currency, status=inst.status
        ),
        issuer=schemas.IssuerInfo(
            id=iss.id,
            legal_name=iss.legal_name,
            short_name=iss.short_name,
            sector_code=iss.sector_code,
            status=iss.status,
            kind=iss.kind,
        )
        if iss
        else None,
        badge=scope.badge(inst.id),
        watch=inst.id in scope.watch,
        watchlists=wl_names,
        indices=indices,
        identifiers=[schemas.IdentifierOut.model_validate(x) for x in queries._identifiers(conn, pairs)],
        share_capital=_capital(inst.extra, iss.extra if iss else None, last.close if last else None),
        last_close=last,
        position=pos,
        disclosures=_items(items),
        theses=_theses_for(tick, all_runs),
        agent_runs=_runs_for_instrument(conn, tick, iss.id if iss else None, all_runs),
    )


@router.get("/instruments/{ticker}/prices", response_model=schemas.PricesOut, responses=NOT_FOUND)
def get_instrument_prices(
    ticker: str,
    conn: Conn,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> schemas.PricesOut:
    """Daily bars (default: the last 365 days) with disclosure and transaction markers for the chart."""
    inst = _resolve(conn, ticker)
    tick = tr_upper(ticker.strip())
    to = date_to or ist_date()
    frm = date_from or to - timedelta(days=365)
    if frm > to:
        raise HTTPException(status_code=400, detail="from, to'dan sonra olamaz")
    primary = get_settings().prices.primary
    rows = conn.execute(
        select(
            price_bar.c.ts,
            price_bar.c.open,
            price_bar.c.high,
            price_bar.c.low,
            price_bar.c.close,
            price_bar.c.volume,
            source.c.code,
        )
        .join(source, source.c.id == price_bar.c.source_id)
        .where(
            price_bar.c.instrument_id == inst.id,
            price_bar.c.interval == "1d",
            price_bar.c.ts >= frm,
            price_bar.c.ts <= to,
        )
        .order_by(price_bar.c.ts)
    ).all()
    best: dict[date, Any] = {}
    for r in rows:
        cur = best.get(r.ts)
        if cur is None or (r.code == primary and cur.code != primary and r.close is not None):
            best[r.ts] = r
    bars = [
        schemas.PriceBarOut(
            date=d, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume, source=r.code
        )
        for d, r in sorted(best.items())
    ]
    scope = queries.load_scope(conn)
    events = queries.feed_items(
        conn,
        queries.FeedFilters(scope="all", ticker=tick, date_from=frm, date_to=to, limit=500),
        scope,
    )
    disc_markers = [
        schemas.DisclosureMarker(
            date=to_ist(e["announced_at"]).date(),
            event_id=e["id"],
            code_label=e["code_label"],
            priority=e["priority"],
            materiality=e["materiality"],
            headline=e["headline"],
        )
        for e in events
    ]
    txn_markers = [
        schemas.TxnMarker(
            date=t["trade_date"],
            side="sell" if t["type"] == "sell" else "buy",
            type=t["type"],
            quantity=float(t["quantity"]),
            price=_f(t["price"]),
        )
        for t in sorted(
            queries.recent_txns(conn, instrument_id=inst.id, limit=500), key=lambda t: t["trade_date"]
        )
        if frm <= t["trade_date"] <= to
        and not t["reverses_id"]
        and not t["reversed"]
        and t["type"] in ("buy", "sell", "opening_balance")
    ]
    return schemas.PricesOut(
        ticker=tick,
        from_=frm,
        to=to,
        primary_source=primary,
        bars=bars,
        markers_disclosures=disc_markers,
        markers_txns=txn_markers,
    )


# ------------------------------------------------------------------------------------------ today


def _quote(code: str, label: str, series: list[tuple[date, float]]) -> schemas.MarketQuote | None:
    last, prev = _last_two(series)
    if last is None:
        return None
    change = last[1] - prev[1] if prev else None
    return schemas.MarketQuote(
        code=code,
        label=label,
        last=last[1],
        prev=prev[1] if prev else None,
        change=change,
        change_pct=change / prev[1] * 100 if change is not None and prev and prev[1] else None,
        as_of=last[0],
    )


def _fx_series(conn: Connection, code: str) -> list[tuple[date, float]]:
    """Latest ``<code>*`` FX series (``fx_rate``), else a ``macro_series`` of that name; [] if none."""
    since = ist_date() - timedelta(days=20)
    rows = conn.execute(
        select(fx_rate.c.series, fx_rate.c.ts, fx_rate.c.value)
        .where(fx_rate.c.series.like(f"{code}%"), fx_rate.c.ts >= since)
        .order_by(fx_rate.c.ts)
    ).all()
    if rows:
        newest = max(rows, key=lambda r: (r.ts, r.series)).series
        return [(r.ts, float(r.value)) for r in rows if r.series == newest]
    rows = conn.execute(
        select(macro_series.c.ts, macro_series.c.value)
        .where(macro_series.c.series == code, macro_series.c.ts >= since)
        .order_by(macro_series.c.ts)
    ).all()
    return [(r.ts, float(r.value)) for r in rows]


def _last_line(error: str | None) -> str | None:
    """The exception line of a stored traceback (the stored text can run to many lines)."""
    lines = [ln.strip() for ln in (error or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else None


def _jobs_summary(conn: Connection) -> schemas.JobsSummary:

    load_plugins()
    settings = get_settings()
    cfg = run_config(settings)
    lanes = run_status._lane_counts(conn, settings)
    last: dict[str, Any] = {}
    for r in conn.execute(
        select(job.c.kind, job.c.last_error, job.c.updated_at, job.c.finished_at, job.c.created_at)
        .where(job.c.status == "dead")
        .order_by(func.coalesce(job.c.updated_at, job.c.finished_at, job.c.created_at).desc())
        .limit(300)
    ):
        lane = lane_for(r.kind, cfg)
        last.setdefault(lane, r)
    out = []
    for lane in sorted(set(lanes) | set(last)):
        c = lanes.get(lane, {})
        r = last.get(lane)
        out.append(
            schemas.LaneJobs(
                lane=lane,
                dead=c.get("dead", 0),
                queued=c.get("queued", 0),
                running=c.get("running", 0),
                last_error=_last_line(r.last_error) if r else None,
                last_error_at=(r.updated_at or r.finished_at or r.created_at) if r else None,
                last_error_kind=r.kind if r else None,
            )
        )
    return schemas.JobsSummary(
        dead_total=sum(x.dead for x in out),
        lanes=out,
        analyses_waiting=lanes.get("llm", {}).get("queued", 0),
    )


def _recent_runs(limit_active: int = 10, recent: int = 5) -> list[schemas.AgentRunBrief]:
    metas = runs.list_runs(40)
    active = [m for m in metas if m.get("status") in runs.ACTIVE]
    rest = [m for m in metas if m.get("status") not in runs.ACTIVE][:recent]
    return [_run_brief(m) for m in active[:limit_active] + rest]


ATTENTION_HOURS = 48


@router.get("/today", response_model=schemas.TodayOut)
def get_today(conn: Conn, feed_limit: Annotated[int, Query(ge=1, le=100)] = 15) -> schemas.TodayOut:
    """The Today page in one call. papi stores no read/ack state, so ``attention`` returns every P1/P2
    event on holdings/watchlist of the last 48 h and the client tracks what the user has seen."""
    now = now_utc()
    scope = queries.load_scope(conn)
    positions = _positions(conn, scope)
    totals = _totals(positions)
    today = ist_date()
    cutoff = now - timedelta(hours=ATTENTION_HOURS)
    seen: dict[str, dict[str, Any]] = {}
    for sc in ("holdings", "watchlist"):
        for it in queries.feed_items(
            conn,
            queries.FeedFilters(scope=sc, priority="P2", date_from=ist_date(cutoff), limit=50),
            scope,
        ):
            if it["published_key"] >= iso_utc(cutoff):
                seen.setdefault(it["id"], it)
    attention = sorted(seen.values(), key=lambda i: i["published_key"], reverse=True)[:30]
    pending = _pending(conn, scope)
    mk_ids = queries.instruments_for_ticker(conn, "XU100")[:1]
    xu = _closes(conn, mk_ids, today - timedelta(days=20)).get(mk_ids[0], []) if mk_ids else []
    return schemas.TodayOut(
        generated_at=now,
        xu100=_quote("XU100", "BIST 100", xu),
        usdtry=_quote("USDTRY", "USD/TRY", _fx_series(conn, "USDTRY")),
        portfolio=schemas.TodayPortfolio(
            available=scope.portfolio_available,
            value=totals.value,
            cost=totals.cost,
            pnl=totals.pnl,
            day_change=totals.day_change,
            day_change_pct=totals.day_change_pct,
            positions=totals.positions,
        ),
        attention=schemas.AttentionOut(window_hours=ATTENTION_HOURS, items=_items(attention)),
        pending_actions=schemas.PendingSummary(count=len(pending), items=pending),
        jobs=_jobs_summary(conn),
        feed=_items(queries.feed_items(conn, queries.FeedFilters(scope="mine", limit=feed_limit), scope)),
        agent_runs=_recent_runs(),
    )


# ------------------------------------------------------------------------------------------ search


def _score(ticker: str, name: str, nq: str) -> int:
    t = normalize_tr(ticker)
    if t == nq:
        return 100
    if t.startswith(nq):
        return 80
    if any(w.startswith(nq) for w in name.split()) or name.startswith(nq):
        return 60
    if nq in t:
        return 50
    if nq in name:
        return 40
    return 0


@router.get("/search", response_model=schemas.SearchOut)
def search(
    conn: Conn,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=30)] = 8,
    disclosure_limit: Annotated[int, Query(ge=0, le=30)] = 10,
) -> schemas.SearchOut:
    """Command-palette search: instruments/issuers (holdings first, then watchlist, then by match
    quality) and disclosures (full text, best first)."""
    nq = normalize_tr(q)
    scope = queries.load_scope(conn)
    hits: list[schemas.InstrumentHit] = []
    if nq:
        rows = conn.execute(
            select(
                identifier.c.entity_id,
                identifier.c.value,
                instrument.c.type,
                issuer.c.short_name,
                issuer.c.legal_name,
            )
            .select_from(
                identifier.join(instrument, instrument.c.id == identifier.c.entity_id).outerjoin(
                    issuer, issuer.c.id == instrument.c.issuer_id
                )
            )
            .where(
                identifier.c.entity_type == "instrument",
                identifier.c.scheme == "TICKER",
                identifier.c.valid_to.is_(None),
            )
        ).all()
        for r in rows:
            name = r.short_name or r.legal_name or ""
            score = _score(r.value, normalize_tr(f"{name} {r.legal_name or ''}"), nq)
            if score:
                hits.append(
                    schemas.InstrumentHit(
                        instrument_id=r.entity_id,
                        ticker=r.value,
                        name=name or None,
                        kind=_kind(r.type),
                        badge=scope.badge(r.entity_id),
                        score=score,
                    )
                )
        rank = {"HOLDING": 0, "WATCH": 1, None: 2}
        hits.sort(key=lambda h: (rank[h.badge], -h.score, h.ticker))
    disclosures: list[schemas.DisclosureHit] = []
    if disclosure_limit and nq:
        try:
            found = fts.search(conn, "disclosure_fts", q, limit=disclosure_limit)
        except OperationalError:
            found = []
        if found:
            by_id = {h.id: h for h in found}
            rows2 = conn.execute(
                select(
                    disclosure.c.id,
                    disclosure.c.title,
                    disclosure.c.source_subject,
                    disclosure.c.source_ref,
                    disclosure.c.published_at,
                    event.c.id.label("event_id"),
                    event.c.priority,
                    event.c.instrument_ids,
                )
                .select_from(
                    disclosure.outerjoin(
                        event,
                        and_(
                            event.c.primary_disclosure_id == disclosure.c.id, event.c.superseded_by.is_(None)
                        ),
                    )
                )
                .where(disclosure.c.id.in_(list(by_id)))
            ).all()
            tick = queries.tickers_for(conn, {i for r in rows2 for i in (r.instrument_ids or [])})
            first: dict[str, Any] = {}
            for r in rows2:
                first.setdefault(r.id, r)
            for h in found:
                r = first.get(h.id)
                if r is None:
                    continue
                disclosures.append(
                    schemas.DisclosureHit(
                        disclosure_id=r.id,
                        event_id=r.event_id,
                        headline=r.title or r.source_subject or "",
                        snippet=h.snippet,
                        source_ref=r.source_ref,
                        kap_url=queries.kap_url(r.source_ref),
                        published_at=r.published_at,
                        priority=r.priority,
                        tickers=[tick[i] for i in (r.instrument_ids or []) if i in tick],
                    )
                )
    return schemas.SearchOut(q=q, instruments=hits[:limit], disclosures=disclosures)
