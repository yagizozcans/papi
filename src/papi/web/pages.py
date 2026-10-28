"""HTML pages: feed, event detail, portfolio, company, ops, Claude usage (07 §3.1–3.4, §3.8)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import Connection, select

from papi.core import db, rawstore
from papi.core.config import get_settings
from papi.core.ids import uuid7
from papi.core.schema import feedback, raw_object
from papi.core.timeutil import now_utc
from papi.web import integrations, queries
from papi.web import stream as live
from papi.web.deps import get_conn, parse_feed_filters, templates

router = APIRouter()
Conn = Annotated[Connection, Depends(get_conn)]


def _page(request: Request, name: str, status_code: int = 200, **ctx: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"nav": name, **ctx}, status_code=status_code)


def _not_found(request: Request, what: str) -> HTMLResponse:
    return _page(request, "not_found.html", status_code=404, what=what)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _back(request: Request, fallback: str) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _notice(text: str, *, level: str = "ok", status_code: int = 200) -> HTMLResponse:
    from markupsafe import escape

    return HTMLResponse(f'<div class="notice {level}">{escape(text)}</div>', status_code=status_code)


# ------------------------------------------------------------------------------------------ feed


@router.get("/", response_class=HTMLResponse)
def feed_page(request: Request, conn: Conn) -> HTMLResponse:
    filters = parse_feed_filters(request.query_params)
    cursor = live.current_cursor(conn)  # before the items: anything later arrives via /stream/poll
    scope = queries.load_scope(conn)
    items = queries.feed_items(conn, filters, scope)
    qs = filters.query_string()
    clear = filters.query_string(**{k: None for k in filters.params() if k not in ("scope", "sort")})
    no_dates = {"range": None, "from": None, "to": None}
    range_urls = {key: "/?" + filters.query_string(**{**no_dates, "range": key}) for key in queries.RANGES}
    range_urls["all"] = "/?" + filters.query_string(**no_dates)
    active_range = filters.range or ("custom" if filters.date_from or filters.date_to else "all")
    return _page(
        request,
        "feed.html",
        filters=filters,
        groups=queries.group_bursts(items),
        count=len(items),
        has_more=len(items) >= filters.limit,
        families=queries.feed_families(conn),
        priorities=queries.PRIORITIES,
        bands=queries.BAND_LABELS,
        band_floors=integrations.band_floors(),
        sorts=queries.SORTS,
        ranges=queries.RANGES,
        dates=filters.dates(),
        range_urls=range_urls,
        active_range=active_range,
        scope=scope,
        poll_url="/stream/poll" + (f"?{qs}" if qs else ""),
        cursor=cursor,
        query_string=qs,
        clear_url="/" + (f"?{clear}" if clear else ""),
    )


# ------------------------------------------------------------------------------------------ event detail


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(request: Request, event_id: str, conn: Conn) -> HTMLResponse:
    scope = queries.load_scope(conn)
    detail = queries.event_detail(conn, event_id, scope)
    if detail is None:
        return _not_found(request, "Olay")
    return _page(request, "event.html", d=detail, feedback_kinds=queries.FEEDBACK_KINDS)


@router.get("/fragments/events/{event_id}", response_class=HTMLResponse)
def feed_item_fragment(request: Request, event_id: str, conn: Conn) -> HTMLResponse:
    scope = queries.load_scope(conn)
    items = queries.feed_items(conn, queries.FeedFilters(scope="all"), scope, event_ids=[event_id])
    if not items:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_feed_item.html", {"it": items[0]})


@router.post("/events/{event_id}/feedback", response_class=HTMLResponse)
def event_feedback(
    request: Request,
    event_id: str,
    kind: Annotated[str, Form()],
    comment: Annotated[str | None, Form()] = None,
    target_type: Annotated[str, Form()] = "event",
    target_id: Annotated[str | None, Form()] = None,
) -> Response:
    if kind not in queries.FEEDBACK_KINDS:
        return _notice("Bilinmeyen geri bildirim türü", level="error", status_code=400)
    if target_type not in ("event", "analysis", "claim", "disclosure", "forecast"):
        return _notice("Bilinmeyen hedef", level="error", status_code=400)
    with db.transaction() as conn:
        conn.execute(
            feedback.insert().values(
                id=uuid7(),
                target_type=target_type,
                target_id=target_id or event_id,
                kind=kind,
                comment=(comment or "").strip() or None,
                created_at=now_utc(),
            )
        )
    if not _is_htmx(request):
        return _back(request, f"/events/{event_id}")
    return _notice(f"Kaydedildi: {queries.FEEDBACK_KINDS[kind]}")


@router.get("/raw/{sha}")
def raw_download(sha: str, conn: Conn) -> Response:
    """Stored source bytes (attachments, original HTML/PDF) from the raw store."""
    row = conn.execute(select(raw_object).where(raw_object.c.sha256 == sha)).first()
    if row is None:
        return Response("Bulunamadı", status_code=404, media_type="text/plain; charset=utf-8")
    try:
        data = rawstore.get_bytes(sha)
    except (FileNotFoundError, ValueError):
        return Response("Ham dosya diskte yok", status_code=404, media_type="text/plain; charset=utf-8")
    ctype = row.content_type or "application/octet-stream"
    headers = {"Content-Disposition": f'inline; filename="{sha[:12]}"', "X-Content-Type-Options": "nosniff"}
    if any(t in ctype for t in ("html", "xml", "svg", "javascript")):
        # Raw KAP HTML is untrusted: never let it run scripts or reach the network from our origin.
        headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
        )
    return Response(data, media_type=ctype, headers=headers)


# ------------------------------------------------------------------------------------------ portfolio


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request, conn: Conn) -> HTMLResponse:
    scope = queries.load_scope(conn)
    return _page(request, "portfolio.html", p=queries.portfolio_view(conn, scope))


def _decimal(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    from papi.core.text import parse_tr_number

    return parse_tr_number(value)


@router.post("/portfolio/pending/{pending_id}/apply", response_class=HTMLResponse)
def pending_apply(
    request: Request, pending_id: str, quantity_override: Annotated[str | None, Form()] = None
) -> Response:
    try:
        override = _decimal(quantity_override)
    except (ValueError, InvalidOperation):
        return _notice("Geçersiz miktar", level="error", status_code=400)
    try:
        with db.transaction() as conn:
            txn_id = integrations.apply_pending_action(conn, pending_id, override)
    except integrations.SliceUnavailable as exc:
        return _notice(f"Portföy modülü yok: {exc}", level="error", status_code=503)
    except (KeyError, LookupError, ValueError) as exc:
        return _notice(f"Uygulanamadı: {exc}", level="error", status_code=409)
    if not _is_htmx(request):
        return _back(request, "/portfolio")
    return _notice(f"Uygulandı (işlem {txn_id[:8]}…)")


@router.post("/portfolio/pending/{pending_id}/dismiss", response_class=HTMLResponse)
def pending_dismiss(request: Request, pending_id: str) -> Response:
    try:
        with db.transaction() as conn:
            integrations.dismiss_pending_action(conn, pending_id)
    except integrations.SliceUnavailable as exc:
        return _notice(f"Portföy modülü yok: {exc}", level="error", status_code=503)
    except (KeyError, LookupError, ValueError) as exc:
        return _notice(f"Reddedilemedi: {exc}", level="error", status_code=409)
    if not _is_htmx(request):
        return _back(request, "/portfolio")
    return _notice("Reddedildi")


# ------------------------------------------------------------------------------------------ company


@router.get("/company/{ticker}", response_class=HTMLResponse)
def company_page(request: Request, ticker: str, conn: Conn) -> HTMLResponse:
    scope = queries.load_scope(conn)
    view = queries.company_view(conn, ticker, scope, get_settings().prices.primary)
    if view is None:
        return _not_found(request, f"Şirket {ticker}")
    return _page(request, "company.html", c=view)


# ------------------------------------------------------------------------------------------ ops


@router.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request, conn: Conn) -> HTMLResponse:
    return _page(request, "ops.html", o=queries.ops_view(conn))


@router.get("/ops/usage", response_class=HTMLResponse)
def usage_page(request: Request, conn: Conn) -> HTMLResponse:
    return _page(request, "usage.html", u=queries.usage_view(conn))


@router.post("/ops/usage/{action}", response_class=HTMLResponse)
def usage_action(request: Request, action: str) -> Response:
    if action not in ("pause", "resume"):
        return _notice("Bilinmeyen işlem", level="error", status_code=404)
    try:
        with db.transaction() as conn:
            (integrations.governor_pause if action == "pause" else integrations.governor_resume)(conn)
    except integrations.SliceUnavailable as exc:
        return _notice(f"LLM modülü yok: {exc}", level="error", status_code=503)
    if not _is_htmx(request):
        return _back(request, "/ops/usage")
    return _notice(
        "Claude analizleri duraklatıldı" if action == "pause" else "Claude analizleri sürdürülüyor"
    )
