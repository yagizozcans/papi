"""``/v1`` JSON API (08 §1) — the read routes the dashboard needs plus governor control.

Money/quantities are serialised as strings (exact ``Decimal``); timestamps as ISO-8601 UTC.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import Connection, select

from papi import __version__
from papi.core import db
from papi.core.schema import instrument
from papi.core.timeutil import now_utc, parse_iso
from papi.web import integrations, queries
from papi.web.deps import get_conn, parse_feed_filters

router = APIRouter(prefix="/v1")
Conn = Annotated[Connection, Depends(get_conn)]


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def json_response(data: Any, status_code: int = 200) -> Response:
    return Response(
        json.dumps(data, default=_default, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )


def _error(detail: str, status_code: int) -> Response:
    return json_response({"detail": detail}, status_code=status_code)


class KapStatus(BaseModel):
    last_poll_at: str | None
    last_poll_ok_at: str | None
    lag_seconds: float | None


class GovernorStatus(BaseModel):
    paused: bool
    until: str | None
    reason: str | None


class TelegramStatus(BaseModel):
    enabled: bool
    ready: bool


class StatusOut(BaseModel):
    version: str
    running: bool
    heartbeat_at: str | None
    heartbeat_stale: bool | None
    kap: KapStatus
    jobs: dict[str, int]
    lanes: dict[str, dict[str, int]]
    governor: GovernorStatus
    telegram: TelegramStatus
    last_backup_at: str | None
    server_time: str


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


@router.get("/status", response_model=StatusOut)
def get_status(conn: Conn) -> StatusOut:
    """Cheap service status (no launchctl, no network): lock, heartbeat, KAP poll lag, jobs, governor."""
    from papi.run import status

    info = status.collect(conn, include_service=False)
    now = now_utc()
    kap = info["kap"]
    ok_at = _iso(kap.get("last_poll_ok_at"))
    lag: float | None = None
    if ok_at:
        try:
            lag = max(0.0, (now - parse_iso(ok_at)).total_seconds())
        except ValueError:
            lag = None
    hb = info.get("heartbeat") or {}
    backup = info.get("last_backup")
    backup_at = backup.get("at") if isinstance(backup, dict) else backup
    return StatusOut(
        version=__version__,
        running=bool(info["running"]),
        heartbeat_at=_iso(hb.get("heartbeat_at")),
        heartbeat_stale=info["heartbeat_stale"],
        kap=KapStatus(last_poll_at=_iso(kap.get("last_poll_at")), last_poll_ok_at=ok_at, lag_seconds=lag),
        jobs=info["jobs"],
        lanes=info["lanes"],
        governor=GovernorStatus(**info["governor"]),
        telegram=TelegramStatus(**info["telegram"]),
        last_backup_at=_iso(backup_at),
        server_time=now.isoformat(),
    )


@router.get("/events")
def list_events(request: Request, conn: Conn) -> Response:
    """Events with payloads. Query: code, ticker, family, scope (holdings|watchlist|mine|all, default all),
    materiality_gte, priority, from, to (Istanbul dates), as_of (ISO time), limit."""
    filters = parse_feed_filters(request.query_params, default_scope="all")
    scope = queries.load_scope(conn)
    return json_response({"items": queries.feed_items(conn, filters, scope), "scope": filters.scope})


@router.get("/events/{event_id}")
def get_event(event_id: str, conn: Conn) -> Response:
    detail = queries.event_detail(conn, event_id, queries.load_scope(conn))
    if detail is None:
        return _error("olay bulunamadı", 404)
    return json_response(detail)


@router.get("/disclosures/{disclosure_id}")
def get_disclosure(disclosure_id: str, conn: Conn) -> Response:
    view = queries.disclosure_view(conn, disclosure_id)
    if view is None:
        return _error("bildirim bulunamadı", 404)
    return json_response(view)


@router.get("/issuers")
def find_issuer(conn: Conn, ticker: Annotated[str, Query(min_length=1)]) -> Response:
    ids = queries.instruments_for_ticker(conn, ticker)
    issuer_id = (
        conn.execute(select(instrument.c.issuer_id).where(instrument.c.id == ids[0])).scalar()
        if ids
        else None
    )
    if not issuer_id:
        return _error(f"{ticker} için ihraççı bulunamadı", 404)
    return json_response(queries.issuer_detail(conn, issuer_id, queries.load_scope(conn)))


@router.get("/issuers/{issuer_id}")
def get_issuer(issuer_id: str, conn: Conn) -> Response:
    view = queries.issuer_detail(conn, issuer_id, queries.load_scope(conn))
    if view is None:
        return _error("ihraççı bulunamadı", 404)
    return json_response(view)


@router.get("/portfolio")
def get_portfolio(conn: Conn) -> Response:
    view = queries.portfolio_view(conn, queries.load_scope(conn))
    return json_response(view)


@router.get("/portfolio/positions")
def get_positions(conn: Conn) -> Response:
    scope = queries.load_scope(conn)
    if not scope.portfolio_available:
        return _error("portföy modülü yüklü değil", 503)
    return json_response({"positions": [queries.position_row(p) for p in scope.positions]})


@router.get("/portfolio/transactions")
def get_transactions(conn: Conn, limit: Annotated[int, Query(ge=1, le=5000)] = 200) -> Response:
    return json_response({"transactions": queries.recent_txns(conn, limit=limit)})


@router.get("/usage")
def get_usage(conn: Conn) -> Response:
    return json_response(queries.usage_view(conn))


def _governor(action: str) -> Response:
    try:
        with db.transaction() as conn:
            (integrations.governor_pause if action == "pause" else integrations.governor_resume)(conn)
        with db.connect() as conn:
            return json_response({"ok": True, "governor": integrations.governor_status(conn)})
    except integrations.SliceUnavailable as exc:
        return _error(str(exc), 503)


@router.post("/llm/pause")
def llm_pause() -> Response:
    return _governor("pause")


@router.post("/llm/resume")
def llm_resume() -> Response:
    return _governor("resume")
