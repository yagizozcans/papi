"""Rule-evaluation context for an event and the facts alerts are built from."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, and_, or_, select

from papi.core.events import DISCLOSURE_NORMALIZED
from papi.core.schema import disclosure, event, event_log, index_membership, instrument, issuer, watchlist
from papi.core.timeutil import ist_date
from papi.portfolio.service import positions, ticker_for

CATCHUP_LAG = timedelta(minutes=15)  # seen this long after publication → papi was offline (catch-up)
KAP_LINK = "https://www.kap.org.tr/tr/Bildirim/{ref}"


@dataclass
class Universe:
    """My holdings and watch lists (instrument ids), resolved once per evaluation."""

    holdings: set[str] = field(default_factory=set)
    watch_instruments: dict[str, set[str]] = field(default_factory=dict)  # watchlist name → ids
    watch_sectors: dict[str, set[str]] = field(default_factory=dict)
    watch_indices: dict[str, set[str]] = field(default_factory=dict)

    @property
    def watched(self) -> set[str]:
        out: set[str] = set()
        for ids in self.watch_instruments.values():
            out |= ids
        return out


def holdings_ids(conn: Connection) -> set[str]:
    return {p.instrument_id for p in positions(conn) if p.quantity and p.quantity > Decimal(0)}


def load_universe(conn: Connection) -> Universe:
    u = Universe(holdings=holdings_ids(conn))
    for row in conn.execute(select(watchlist)).mappings():
        u.watch_instruments[row["name"]] = set(row["instrument_ids"] or [])
        u.watch_sectors[row["name"]] = {str(s) for s in row["sectors"] or []}
        u.watch_indices[row["name"]] = {str(s) for s in row["indices"] or []}
    return u


def event_instruments(conn: Connection, ev: Any) -> list[str]:
    ids = list(ev["instrument_ids"] or [])
    if not ids and ev["issuer_ids"]:
        ids = list(
            conn.execute(
                select(instrument.c.id).where(
                    instrument.c.issuer_id.in_(ev["issuer_ids"]), instrument.c.type == "equity"
                )
            ).scalars()
        )
    return ids


def is_catchup(conn: Connection, disc: Any) -> bool:
    """Disclosure was first seen well after publication (papi was asleep/offline)."""
    if disc is None:
        return False
    extra = disc["extra"] or {}
    if "is_catchup" in extra:
        return bool(extra["is_catchup"])
    ev = conn.execute(
        select(event_log.c.data)
        .where(event_log.c.type == DISCLOSURE_NORMALIZED, event_log.c.subject == f"disclosure/{disc['id']}")
        .order_by(event_log.c.time.desc())
        .limit(1)
    ).scalar_one_or_none()
    if ev is not None and "is_catchup" in ev:
        return bool(ev["is_catchup"])
    published, seen = disc["published_at"], disc["first_seen_at"]
    return bool(published and seen and seen - published > CATCHUP_LAG)


def kap_link(disc: Any) -> str | None:
    if disc is None or not disc["source_ref"]:
        return None
    return KAP_LINK.format(ref=disc["source_ref"])


@dataclass
class EventFacts:
    """Everything an alert needs about one event."""

    event: Any
    disclosure: Any | None
    instrument_ids: list[str]
    tickers: list[str]
    holding_ids: set[str]
    watched_ids: set[str]
    ctx: dict[str, Any]

    @property
    def is_holding(self) -> bool:
        return bool(self.holding_ids)

    @property
    def is_watched(self) -> bool:
        return bool(self.watched_ids)

    @property
    def headline(self) -> str:
        extra = self.event["extra"] or {}
        return str(extra.get("headline_tr") or self.event["code"])

    @property
    def link(self) -> str | None:
        return kap_link(self.disclosure)

    @property
    def published_at(self) -> datetime | None:
        if self.disclosure is not None and self.disclosure["published_at"]:
            return self.disclosure["published_at"]
        return self.event["announced_at"]


def load_event_facts(conn: Connection, event_id: str, universe: Universe | None = None) -> EventFacts | None:
    ev = conn.execute(select(event).where(event.c.id == event_id)).mappings().first()
    if ev is None:
        return None
    disc_id = ev["primary_disclosure_id"] or (ev["disclosure_ids"] or [None])[0]
    disc = (
        conn.execute(select(disclosure).where(disclosure.c.id == disc_id)).mappings().first()
        if disc_id
        else None
    )
    u = universe or load_universe(conn)
    inst_ids = event_instruments(conn, ev)
    at = ev["announced_at"] or (disc["published_at"] if disc is not None else None) or None
    at_date = ist_date(at) if at else ist_date()
    extra = ev["extra"] or {}
    tickers = [str(t) for t in extra.get("tickers") or []]
    if not tickers:
        tickers = [t for t in (ticker_for(conn, i, at_date) for i in inst_ids) if t]

    issuer_ids = list(ev["issuer_ids"] or [])
    if not issuer_ids and inst_ids:
        issuer_ids = [
            r
            for r in conn.execute(
                select(instrument.c.issuer_id).where(instrument.c.id.in_(inst_ids))
            ).scalars()
            if r
        ]
    sectors = (
        sorted(
            {
                s
                for s in conn.execute(
                    select(issuer.c.sector_code).where(issuer.c.id.in_(issuer_ids))
                ).scalars()
                if s
            }
        )
        if issuer_ids
        else []
    )
    indices = (
        sorted(
            set(
                conn.execute(
                    select(index_membership.c.index_code).where(
                        index_membership.c.instrument_id.in_(inst_ids),
                        index_membership.c.valid_from <= at_date,
                        or_(index_membership.c.valid_to.is_(None), index_membership.c.valid_to >= at_date),
                    )
                ).scalars()
            )
        )
        if inst_ids
        else []
    )
    inst_set = set(inst_ids)
    holding = inst_set & u.holdings
    watchlists = sorted(
        name
        for name in u.watch_instruments
        if inst_set & u.watch_instruments[name]
        or set(sectors) & u.watch_sectors.get(name, set())
        or set(indices) & u.watch_indices.get(name, set())
    )
    watched = inst_set & u.watched
    scope = ["all"]
    if holding:
        scope.append("holdings")
    if watchlists:
        scope.append("watchlist")
    code = str(ev["code"])
    text_parts = [
        extra.get("headline_tr"),
        disc["title"] if disc is not None else None,
        disc["source_subject"] if disc is not None else None,
        disc["summary"] if disc is not None else None,
        " ".join(tickers),
    ]
    ctx: dict[str, Any] = {
        "scope": scope,
        "ticker": tickers,
        "sector": sectors,
        "index": indices,
        "watchlist": watchlists,
        "code": code,
        "family": str(extra.get("family") or code.split(".", 1)[0]),
        "materiality": ev["materiality"],
        "band": ev["materiality_band"],
        "priority": ev["priority"],
        "revision": bool(disc is not None and (disc["revision_of"] or disc["revision_kind"] != "original")),
        "late": bool(disc is not None and disc["is_late"]),
        "catchup": is_catchup(conn, disc),
        "payload": ev["payload"] or {},
        "text": " ".join(str(p) for p in text_parts if p),
    }
    if watchlists and not watched:
        watched = inst_set  # matched through a watched sector/index
    return EventFacts(ev, disc, inst_ids, tickers, holding, watched, ctx)


def ops_context(level: str = "warning") -> dict[str, Any]:
    return {"scope": ["ops"], "code": "ops.health", "family": "ops", "priority": None, "text": level}


def events_since(conn: Connection, since: datetime, instrument_ids: set[str]) -> list[Any]:
    """Non-superseded events announced since ``since`` touching any of ``instrument_ids``."""
    if not instrument_ids:
        return []
    rows = conn.execute(
        select(event)
        .where(
            and_(event.c.superseded_by.is_(None), event.c.status != "retracted"),
            or_(
                event.c.announced_at >= since,
                and_(event.c.announced_at.is_(None), event.c.created_at >= since),
            ),
        )
        .order_by(event.c.announced_at)
    ).mappings()
    out = []
    for row in rows:
        ids = set(event_instruments(conn, row))
        if ids & instrument_ids:
            out.append(row)
    return out
