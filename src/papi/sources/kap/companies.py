"""Issuer master from the KAP company list (contract C2) + entity resolution helpers.

Per listed company (``/tr/api/company/items/IGS/A``): one ``issuer`` (kind ``company``,
``extra.kap`` = raw row), one ``equity`` ``instrument`` per BIST share code in ``stockCode``
(e.g. ``"ISATR, ISBTR, ISCTR, ISKUR, TIB"``; 4–6 character codes are tickers, shorter ones are KAP
member codes and are kept in ``issuer.extra.kap_member_codes``), and identifiers:

* instrument ``TICKER`` (upper case; ``valid_from`` = ``OPEN_START`` when first seen, the sync date for
  a later rename; ``valid_to`` set — exclusive — when the code disappears),
* issuer ``KAP_MEMBER_OID`` = ``mkkMemberOid`` (the id used by disclosures and list filters),
* issuer ``KAP_COMPANY_OID`` = ``kapMemberOid``, issuer ``VKN`` = ``taxNo`` when present,
* instrument ``ISIN`` is not in any company endpoint; it is harvested from disclosure bodies
  (``"SAHOL, TRASAHOL91Q5"`` cells) by the normalizer.

Changes are bitemporal: business validity in ``valid_from``/``valid_to``, knowledge time in
``recorded_at``; nothing is deleted.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, and_, or_, select, update

from papi.core import metrics
from papi.core.ids import uuid7
from papi.core.schema import OPEN_START, identifier, instrument, issuer, listing, venue
from papi.core.state import get_state, set_state
from papi.core.text import normalize_tr
from papi.core.timeutil import ist_date, now_utc
from papi.sources.kap import deps, drift
from papi.sources.kap.client import KapClient, KapJson, default_client
from papi.sources.kap.common import release

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{3,5}$")
ISIN_RE = re.compile(r"^TR[A-Z0-9]{9}[0-9]$")
LAST_COUNT_KEY = "kap:companies:last_count"
LAST_SYNC_KEY = "kap:companies:last_sync"
_LEGAL_SUFFIX = re.compile(
    r"\s*(A\.?\s?Ş\.?|A\.?\s?S\.?|ANON[İI]M\s+Ş[İI]RKET[İI]|T\.?A\.?Ş\.?)\s*$", re.IGNORECASE
)
NAME_MATCH_THRESHOLD = 0.92


@dataclass
class SyncReport:
    companies: int = 0
    issuers_created: int = 0
    issuers_updated: int = 0
    instruments_created: int = 0
    tickers_added: int = 0
    tickers_closed: int = 0
    renames: int = 0
    delisted: int = 0
    raw_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_codes(value: str | None) -> tuple[list[str], list[str]]:
    """``"AKM, AKMEN"`` → (tickers ``["AKMEN"]``, member codes ``["AKM"]``)."""
    tickers: list[str] = []
    others: list[str] = []
    for raw in (value or "").replace(";", ",").split(","):
        code = raw.strip().upper()
        if not code or code == "-":
            continue
        (tickers if TICKER_RE.match(code) else others).append(code)
    return tickers, others


def short_name(title: str) -> str:
    return _LEGAL_SUFFIX.sub("", title.strip()).strip() or title.strip()


def fetch_companies(client: KapClient | None = None, member_type: str = "IGS") -> KapJson:
    return (client or default_client()).companies(member_type)


def sync_companies(
    conn: Connection, client: KapClient | None = None, *, today: date | None = None
) -> SyncReport:
    """Fetch the listed-company list and apply it. ``conn``'s open transaction is committed first (the
    fetch archives bytes in its own transaction); the apply runs in ``conn`` (caller commits)."""
    release(conn)
    res = fetch_companies(client)
    report = apply_companies(conn, res.data, today=today)
    report.raw_sha256 = res.raw_sha256
    return report


def apply_companies(
    conn: Connection, items: list[dict[str, Any]], *, today: date | None = None
) -> SyncReport:
    today = today or ist_date()
    now = now_utc()
    report = SyncReport(companies=len(items))
    drift.check(conn, "company", items)
    venue_id = conn.execute(select(venue.c.id).where(venue.c.mic == "XIST")).scalar_one_or_none()
    first_sync = get_state(conn, LAST_SYNC_KEY) is None
    seen_issuers: set[str] = set()
    for item in items:
        oid = (item.get("mkkMemberOid") or "").strip()
        title = (item.get("kapMemberTitle") or "").strip()
        if not oid or not title:
            continue
        tickers, member_codes = split_codes(item.get("stockCode"))
        issuer_id, created = _upsert_issuer(conn, item, oid, title, member_codes, now, report)
        seen_issuers.add(issuer_id)
        _ensure_identifier(conn, "issuer", issuer_id, "KAP_MEMBER_OID", oid, OPEN_START, now)
        if item.get("kapMemberOid"):
            _ensure_identifier(
                conn, "issuer", issuer_id, "KAP_COMPANY_OID", item["kapMemberOid"], OPEN_START, now
            )
        if item.get("taxNo"):
            _ensure_identifier(conn, "issuer", issuer_id, "VKN", str(item["taxNo"]).strip(), OPEN_START, now)
        trading = str(item.get("payIslemDurumu") or "") == "1"
        _sync_instruments(
            conn,
            issuer_id,
            tickers,
            trading=trading,
            new_issuer=created or first_sync,
            today=today,
            now=now,
            venue_id=venue_id,
            report=report,
        )
    _mark_missing(conn, seen_issuers, len(items), today, now, report)
    set_state(conn, LAST_SYNC_KEY, {"at": now.isoformat(), **report.as_dict()})
    metrics.record("kap.companies.synced", report.companies, conn=conn)
    return report


def _upsert_issuer(
    conn: Connection,
    item: dict[str, Any],
    oid: str,
    title: str,
    member_codes: list[str],
    now: Any,
    report: SyncReport,
) -> tuple[str, bool]:
    existing = conn.execute(
        select(issuer.c.id, issuer.c.legal_name, issuer.c.status, issuer.c.extra)
        .join(identifier, and_(identifier.c.entity_id == issuer.c.id, identifier.c.entity_type == "issuer"))
        .where(
            identifier.c.scheme == "KAP_MEMBER_OID",
            identifier.c.value == oid,
            identifier.c.valid_to.is_(None),
        )
    ).first()
    if existing is None:
        iid = uuid7()
        conn.execute(
            issuer.insert().values(
                id=iid,
                legal_name=title,
                short_name=short_name(title),
                country="TR",
                status="active",
                kind="company",
                extra={"kap": item, "kap_member_codes": member_codes},
                created_at=now,
                updated_at=now,
            )
        )
        report.issuers_created += 1
        return iid, True
    extra = dict(existing.extra or {})
    changed = existing.status != "active" or extra.get("kap") != item
    if existing.legal_name != title:
        history = list(extra.get("name_history") or [])
        history.append({"name": existing.legal_name, "until": now.isoformat()})
        extra["name_history"] = history
        changed = True
    extra["kap"] = item
    extra["kap_member_codes"] = member_codes
    if changed:
        conn.execute(
            update(issuer)
            .where(issuer.c.id == existing.id)
            .values(
                legal_name=title, short_name=short_name(title), status="active", extra=extra, updated_at=now
            )
        )
        report.issuers_updated += 1
    return existing.id, False


def _current_tickers(conn: Connection, issuer_id: str) -> dict[str, str]:
    rows = conn.execute(
        select(identifier.c.value, identifier.c.entity_id)
        .join(instrument, instrument.c.id == identifier.c.entity_id)
        .where(
            instrument.c.issuer_id == issuer_id,
            identifier.c.entity_type == "instrument",
            identifier.c.scheme == "TICKER",
            identifier.c.valid_to.is_(None),
        )
    ).all()
    return {r.value: r.entity_id for r in rows}


def _sync_instruments(
    conn: Connection,
    issuer_id: str,
    tickers: list[str],
    *,
    trading: bool,
    new_issuer: bool,
    today: date,
    now: Any,
    venue_id: str | None,
    report: SyncReport,
) -> None:
    current = _current_tickers(conn, issuer_id)
    new = [t for t in tickers if t not in current]
    gone = [t for t in current if t not in tickers]
    status = "active" if trading else "inactive"
    for t in tickers:
        if t in current:
            conn.execute(
                update(instrument)
                .where(instrument.c.id == current[t], instrument.c.status != status)
                .values(status=status, updated_at=now)
            )
    if len(new) == 1 and len(gone) == 1:
        inst_id = current[gone[0]]
        _close_identifier(conn, "TICKER", gone[0], today, now)
        _release_ticker(conn, new[0], today, now, report)
        _ensure_identifier(conn, "instrument", inst_id, "TICKER", new[0], today, now)
        row = conn.execute(select(instrument.c.extra).where(instrument.c.id == inst_id)).scalar_one()
        extra = dict(row or {})
        extra["ticker_history"] = [
            *extra.get("ticker_history", []),
            {"ticker": gone[0], "until": today.isoformat()},
        ]
        conn.execute(update(instrument).where(instrument.c.id == inst_id).values(extra=extra, updated_at=now))
        report.renames += 1
        report.tickers_added += 1
        report.tickers_closed += 1
        return
    for t in gone:
        inst_id = current[t]
        _close_identifier(conn, "TICKER", t, today, now)
        conn.execute(
            update(instrument).where(instrument.c.id == inst_id).values(status="delisted", updated_at=now)
        )
        report.tickers_closed += 1
    for t in new:
        _release_ticker(conn, t, today, now, report)
        inst_id = uuid7()
        conn.execute(
            instrument.insert().values(
                id=inst_id,
                issuer_id=issuer_id,
                type="equity",
                currency="TRY",
                status=status,
                extra={"kap_code": t},
                created_at=now,
                updated_at=now,
            )
        )
        valid_from = OPEN_START if new_issuer else today
        _ensure_identifier(conn, "instrument", inst_id, "TICKER", t, valid_from, now)
        if venue_id is not None:
            conn.execute(
                listing.insert().values(
                    id=uuid7(), instrument_id=inst_id, venue_id=venue_id, segment=None, valid_from=valid_from
                )
            )
        report.instruments_created += 1
        report.tickers_added += 1


def _release_ticker(conn: Connection, ticker: str, today: date, now: Any, report: SyncReport) -> None:
    """A code moving to another member: close its open assignment elsewhere first."""
    if _close_identifier(conn, "TICKER", ticker, today, now):
        report.tickers_closed += 1


def _close_identifier(conn: Connection, scheme: str, value: str, today: date, now: Any) -> bool:
    rows = conn.execute(
        select(identifier.c.id, identifier.c.valid_from).where(
            identifier.c.scheme == scheme, identifier.c.value == value, identifier.c.valid_to.is_(None)
        )
    ).all()
    for r in rows:
        if r.valid_from >= today:
            # Opened and closed on the same day: the assignment never held; supersede the record.
            conn.execute(
                update(identifier)
                .where(identifier.c.id == r.id)
                .values(valid_to=r.valid_from, superseded_at=now)
            )
        else:
            conn.execute(update(identifier).where(identifier.c.id == r.id).values(valid_to=today))
    return bool(rows)


def _ensure_identifier(
    conn: Connection, entity_type: str, entity_id: str, scheme: str, value: str, valid_from: date, now: Any
) -> None:
    open_row = conn.execute(
        select(identifier.c.id, identifier.c.entity_id, identifier.c.entity_type).where(
            identifier.c.scheme == scheme, identifier.c.value == value, identifier.c.valid_to.is_(None)
        )
    ).first()
    if open_row is not None and open_row.entity_id == entity_id:
        return
    same_start = conn.execute(
        select(identifier.c.id).where(
            identifier.c.scheme == scheme, identifier.c.value == value, identifier.c.valid_from == valid_from
        )
    ).first()
    if same_start is not None:
        conn.execute(
            update(identifier)
            .where(identifier.c.id == same_start.id)
            .values(
                entity_type=entity_type,
                entity_id=entity_id,
                valid_to=None,
                superseded_at=None,
                recorded_at=now,
            )
        )
        return
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type=entity_type,
            entity_id=entity_id,
            scheme=scheme,
            value=value,
            valid_from=valid_from,
            valid_to=None,
            recorded_at=now,
        )
    )


def _mark_missing(
    conn: Connection, seen: set[str], count: int, today: date, now: Any, report: SyncReport
) -> None:
    previous = int(get_state(conn, LAST_COUNT_KEY, 0) or 0)
    if previous and count < previous * 0.8:
        deps.ops_alert(
            conn,
            f"KAP şirket listesi beklenenden kısa ({count} < {previous}); kottan çıkma işaretlenmedi.",
            dedupe_key="kap:companies:short",
        )
        return
    set_state(conn, LAST_COUNT_KEY, count)
    rows = conn.execute(
        select(issuer.c.id, issuer.c.extra).where(issuer.c.kind == "company", issuer.c.status == "active")
    ).all()
    for r in rows:
        if r.id in seen or not (r.extra or {}).get("kap"):
            continue
        conn.execute(update(issuer).where(issuer.c.id == r.id).values(status="delisted", updated_at=now))
        for t, inst_id in _current_tickers(conn, r.id).items():
            _close_identifier(conn, "TICKER", t, today, now)
            conn.execute(
                update(instrument).where(instrument.c.id == inst_id).values(status="delisted", updated_at=now)
            )
            report.tickers_closed += 1
        report.delisted += 1


# ----------------------------------------------------------------------------------------- resolution


def _valid_at(at: date) -> Any:
    return and_(
        identifier.c.valid_from <= at,
        or_(identifier.c.valid_to.is_(None), identifier.c.valid_to > at),
        identifier.c.superseded_at.is_(None),
    )


def resolve_tickers(conn: Connection, tickers: list[str], at: date) -> dict[str, tuple[str, str | None]]:
    """``{ticker: (instrument_id, issuer_id)}`` for codes valid at ``at``."""
    codes = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not codes:
        return {}
    rows = conn.execute(
        select(identifier.c.value, identifier.c.entity_id, instrument.c.issuer_id)
        .join(instrument, instrument.c.id == identifier.c.entity_id)
        .where(
            identifier.c.scheme == "TICKER",
            identifier.c.entity_type == "instrument",
            identifier.c.value.in_(codes),
            _valid_at(at),
        )
    ).all()
    return {r.value: (r.entity_id, r.issuer_id) for r in rows}


def resolve_member_oid(conn: Connection, oid: str | None, at: date) -> str | None:
    if not oid:
        return None
    return conn.execute(
        select(identifier.c.entity_id)
        .where(
            identifier.c.scheme == "KAP_MEMBER_OID",
            identifier.c.entity_type == "issuer",
            identifier.c.value == oid,
            _valid_at(at),
        )
        .limit(1)
    ).scalar_one_or_none()


def resolve_name(conn: Connection, title: str | None) -> str | None:
    """Turkish-normalised legal-name match; below ``NAME_MATCH_THRESHOLD`` → ``None`` (pending)."""
    if not title:
        return None
    key = normalize_tr(title)
    best: tuple[float, str | None] = (0.0, None)
    for r in conn.execute(select(issuer.c.id, issuer.c.legal_name).where(issuer.c.kind == "company")):
        cand = normalize_tr(r.legal_name)
        if cand == key:
            return r.id
        ratio = difflib.SequenceMatcher(None, key, cand).ratio()
        if ratio > best[0]:
            best = (ratio, r.id)
    return best[1] if best[0] >= NAME_MATCH_THRESHOLD else None


def instrument_tickers(conn: Connection, instrument_ids: list[str], at: date) -> dict[str, str]:
    if not instrument_ids:
        return {}
    rows = conn.execute(
        select(identifier.c.entity_id, identifier.c.value).where(
            identifier.c.scheme == "TICKER",
            identifier.c.entity_type == "instrument",
            identifier.c.entity_id.in_(instrument_ids),
            _valid_at(at),
        )
    ).all()
    return {r.entity_id: r.value for r in rows}


def issuer_instruments(conn: Connection, issuer_ids: list[str]) -> list[str]:
    if not issuer_ids:
        return []
    rows = conn.execute(
        select(instrument.c.id).where(instrument.c.issuer_id.in_(issuer_ids), instrument.c.type == "equity")
    ).all()
    return [r.id for r in rows]


def record_isin(conn: Connection, instrument_id: str, isin: str) -> bool:
    """Attach an ``ISIN`` harvested from a disclosure body (no-op if already known)."""
    if not ISIN_RE.match(isin):
        return False
    exists = conn.execute(
        select(identifier.c.id).where(identifier.c.scheme == "ISIN", identifier.c.value == isin)
    ).first()
    if exists is not None:
        return False
    _ensure_identifier(conn, "instrument", instrument_id, "ISIN", isin, OPEN_START, now_utc())
    return True
