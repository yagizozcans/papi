"""Paid-in capital and share counts per listed company (materiality size signal for triage).

Source (verified live 2026-10-28): the server-rendered company page
``GET /tr/sirket-bilgileri/genel/<permaLink>`` (permaLink from ``GET /tr/api/member/filter/<code>``),
section "SERMAYE VE ORTAKLIK YAPISI BİLGİLERİ":

* ``Ödenmiş/Çıkarılmış Sermaye`` (TRY nominal) and ``Kayıtlı Sermaye Tavanı``;
* "Sermayeyi Temsil Eden Paylara İlişkin Bilgi": share groups (nominal, ratio, privilege, traded?);
* "Fiili Dolaşımdaki Paylar": per **Borsa Kodu** the total share count (TRY nominal; BIST prices are
  per 1 TL nominal, so market cap = count × price), traded / non-traded counts and free float.

The company list's ``paidCapital`` is not used: it is stale (FROTO 350.910.000 vs 3.509.100.000 on
the company page after its bonus issue) and empty for ~175 companies.

Written: ``issuer.extra.paid_in_capital`` / ``registered_capital_ceiling`` (Decimal strings),
``paid_in_capital_as_of``, ``share_groups``, ``kap_permalink``; per equity instrument
``extra.shares_outstanding`` (Decimal string), ``shares_as_of``, ``shares_source``
(``free_float_table`` | ``paid_in_capital`` for single-class issuers), ``free_float_shares``,
``free_float_pct``. Freshness per issuer in kv_state ``kap:capital:<issuer_id>``
(``{at, status, error?}``); ``ok`` refreshes after 7 days, failures retry after 1 day.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from bs4 import BeautifulSoup, Tag
from sqlalchemy import Connection, select, update

from papi.core import metrics
from papi.core.schema import identifier, instrument, issuer, kv_state
from papi.core.state import set_state
from papi.core.text import normalize_tr, parse_tr_number
from papi.core.timeutil import ist_date, now_utc, parse_iso
from papi.sources.kap import deps
from papi.sources.kap.client import KapClient, KapNotFound
from papi.sources.kap.common import release

log = logging.getLogger("papi.kap.capital")

STATE_PREFIX = "kap:capital:"
REFRESH_OK = timedelta(days=7)
REFRESH_FAILED = timedelta(days=1)
HELD_BATCH = 50  # daily sync: held/watched issuers due
SLOW_BATCH = 8  # hourly periodic: everyone else

_PAID = "Ödenmiş/Çıkarılmış Sermaye"
_CEILING = "Kayıtlı Sermaye Tavanı"
_GROUPS = "Sermayeyi Temsil Eden Paylara İlişkin Bilgi"
_FLOAT = "Fiili Dolaşımdaki Paylar"
_NUM = re.compile(r"^-?[\d.,]+$")


class CapitalSectionMissing(ValueError):
    """The page has no capital section (wrong/stale permaLink or page layout drift)."""


@dataclass
class CompanyCapital:
    paid_in_capital: Decimal
    registered_capital_ceiling: Decimal | None = None
    share_groups: list[dict[str, Any]] = field(default_factory=list)
    free_float: dict[str, dict[str, Decimal | None]] = field(default_factory=dict)  # by Borsa Kodu


def _num(text: str | None) -> Decimal | None:
    t = (text or "").strip()
    if not t or not _NUM.match(t):
        return None
    try:
        return parse_tr_number(t)
    except ValueError:
        return None


def _labelled_value(soup: BeautifulSoup, label: str) -> Decimal | None:
    for s in soup.find_all(string=lambda t: bool(t) and t.strip() == label):
        node = s.parent
        for _ in range(4):
            if node is None:
                break
            for span in node.find_all(["span", "div"]):
                txt = span.get_text(" ", strip=True)
                if txt != label and _NUM.match(txt or ""):
                    return _num(txt)
            node = node.parent
    return None


def _table_after(soup: BeautifulSoup, title: str) -> list[dict[str, str]]:
    marker = soup.find(string=lambda t: bool(t) and t.strip() == title)
    if marker is None:
        return []
    table = marker.find_next("table")
    if not isinstance(table, Tag):
        return []
    header = [normalize_tr(th.get_text(" ", strip=True)) for th in table.find_all("th")]
    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if cells and len(cells) <= len(header):  # KAP sometimes serves truncated rows (ISCTR group C)
            rows.append(dict(zip(header, cells, strict=False)))
    return rows


def parse_company_general(html: str | bytes) -> CompanyCapital:
    soup = BeautifulSoup(html, "lxml")
    paid = _labelled_value(soup, _PAID)
    if paid is None:
        raise CapitalSectionMissing("no 'Ödenmiş/Çıkarılmış Sermaye' on the company page")
    cap = CompanyCapital(paid_in_capital=paid, registered_capital_ceiling=_labelled_value(soup, _CEILING))
    for r in _table_after(soup, _GROUPS):
        cap.share_groups.append(
            {
                "group": r.get("pay grubu"),
                "registered": r.get("nama hamiline"),
                "nominal_per_share": _str(_num(r.get("beher payin nominal degeri tl"))),
                "nominal_total": _str(_num(r.get("paylarin nominal degeri"))),
                "capital_pct": _str(_num(r.get("sermayeye orani"))),
                "privilege": r.get("imtiyaz turu"),
                "traded": normalize_tr(r.get("borsada islem gorup gormedigi") or "") == "islem goruyor",
            }
        )
    for r in _table_after(soup, _FLOAT):
        code = (r.get("borsa kodu") or "").strip().upper()
        if not code:
            continue
        cap.free_float[code] = {
            "total_shares": _num(r.get("toplam pay adedi")),
            "non_traded_shares": _num(r.get("borsada islem gormeyen pay adedi")),
            "traded_shares": _num(r.get("borsada islem goren pay adedi")),
            "free_float_shares": _num(r.get("fiili dolasimdaki pay adedi")),
            "free_float_pct": _num(r.get("fiili dolasimdaki pay orani")),
        }
    return cap


def _str(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


# ------------------------------------------------------------------------------------------- persistence


def _issuer_codes(conn: Connection, issuer_id: str, at: date) -> dict[str, str]:
    """``{ticker: instrument_id}`` of the issuer's current equity codes."""
    rows = conn.execute(
        select(identifier.c.value, identifier.c.entity_id)
        .join(instrument, instrument.c.id == identifier.c.entity_id)
        .where(
            instrument.c.issuer_id == issuer_id,
            instrument.c.type == "equity",
            identifier.c.entity_type == "instrument",
            identifier.c.scheme == "TICKER",
            identifier.c.valid_from <= at,
            identifier.c.valid_to.is_(None),
        )
    ).all()
    return {r.value: r.entity_id for r in rows}


def apply_capital(
    conn: Connection, issuer_id: str, cap: CompanyCapital, *, as_of: date, url: str | None
) -> int:
    """Write capital to the issuer and share counts to its instruments; returns instruments updated."""
    now = now_utc()
    row = conn.execute(select(issuer.c.extra).where(issuer.c.id == issuer_id)).scalar_one()
    extra = dict(row or {})
    extra.update(
        {
            "paid_in_capital": str(cap.paid_in_capital),
            "registered_capital_ceiling": _str(cap.registered_capital_ceiling),
            "paid_in_capital_as_of": as_of.isoformat(),
            "share_groups": cap.share_groups,
            "capital_source": url,
        }
    )
    conn.execute(update(issuer).where(issuer.c.id == issuer_id).values(extra=extra, updated_at=now))
    codes = _issuer_codes(conn, issuer_id, as_of)
    updated = 0
    for code, inst_id in codes.items():
        ff = cap.free_float.get(code)
        if ff is not None and ff.get("total_shares") is not None:
            values: dict[str, Any] = {
                "shares_outstanding": str(ff["total_shares"]),
                "shares_source": "free_float_table",
                "free_float_shares": _str(ff.get("free_float_shares")),
                "free_float_pct": _str(ff.get("free_float_pct")),
                "traded_shares": _str(ff.get("traded_shares")),
            }
        elif len(codes) == 1:
            values = {"shares_outstanding": str(cap.paid_in_capital), "shares_source": "paid_in_capital"}
        else:
            continue
        values["shares_as_of"] = as_of.isoformat()
        iextra = dict(
            conn.execute(select(instrument.c.extra).where(instrument.c.id == inst_id)).scalar_one() or {}
        )
        for k in ("free_float_shares", "free_float_pct", "traded_shares"):
            iextra.pop(k, None)
        iextra.update({k: v for k, v in values.items() if v is not None})
        conn.execute(
            update(instrument).where(instrument.c.id == inst_id).values(extra=iextra, updated_at=now)
        )
        updated += 1
    return updated


def _permalink(
    conn: Connection, client: KapClient, issuer_id: str, extra: dict[str, Any], at: date
) -> str | None:
    if extra.get("kap_permalink"):
        return str(extra["kap_permalink"])
    kap = extra.get("kap") or {}
    oid = kap.get("mkkMemberOid")
    codes = list(_issuer_codes(conn, issuer_id, at)) + list(extra.get("kap_member_codes") or [])
    release(conn)
    for code in codes:
        try:
            res = client.member_lookup(code)
        except KapNotFound:
            continue
        for m in res.data:
            if isinstance(m, dict) and m.get("permaLink") and (not oid or m.get("mkkMemberOid") == oid):
                return str(m["permaLink"])
    return None


def refresh_issuer(
    conn: Connection, client: KapClient, issuer_id: str, *, now: datetime | None = None
) -> str:
    """Fetch + apply one issuer's capital. Returns ``ok`` | ``missing`` | ``no_permalink``. Commits."""
    now = now or now_utc()
    as_of = ist_date(now)
    extra = dict(conn.execute(select(issuer.c.extra).where(issuer.c.id == issuer_id)).scalar_one() or {})
    link = _permalink(conn, client, issuer_id, extra, as_of)
    key = f"{STATE_PREFIX}{issuer_id}"
    if link is None:
        set_state(conn, key, {"at": now.isoformat(), "status": "no_permalink"})
        conn.commit()
        return "no_permalink"
    release(conn)
    page = client.company_general_page(link)
    try:
        cap = parse_company_general(page.content)
    except CapitalSectionMissing as exc:
        # Stale permaLink or layout drift: forget the link so the next try looks it up again.
        cur = dict(conn.execute(select(issuer.c.extra).where(issuer.c.id == issuer_id)).scalar_one() or {})
        cur.pop("kap_permalink", None)
        conn.execute(update(issuer).where(issuer.c.id == issuer_id).values(extra=cur))
        set_state(
            conn, key, {"at": now.isoformat(), "status": "missing", "error": str(exc), "raw": page.raw_sha256}
        )
        metrics.record("kap.capital.missing", 1, conn=conn)
        conn.commit()
        return "missing"
    cur = dict(conn.execute(select(issuer.c.extra).where(issuer.c.id == issuer_id)).scalar_one() or {})
    cur["kap_permalink"] = link
    conn.execute(update(issuer).where(issuer.c.id == issuer_id).values(extra=cur))
    n = apply_capital(conn, issuer_id, cap, as_of=as_of, url=page.url)
    set_state(conn, key, {"at": now.isoformat(), "status": "ok", "instruments": n, "raw": page.raw_sha256})
    conn.commit()
    return "ok"


def due_issuers(conn: Connection, *, now: datetime | None = None, held_only: bool = False) -> list[str]:
    """Issuers whose capital is missing or stale; held/watched first, then never-fetched, then oldest."""
    now = now or now_utc()
    rows = conn.execute(
        select(issuer.c.id, issuer.c.extra).where(issuer.c.kind == "company", issuer.c.status == "active")
    ).all()
    states = {
        r.key.removeprefix(STATE_PREFIX): r.value or {}
        for r in conn.execute(
            select(kv_state.c.key, kv_state.c.value).where(kv_state.c.key.like(f"{STATE_PREFIX}%"))
        )
    }
    at = ist_date(now)
    ranked: list[tuple[int, str, str]] = []
    for r in rows:
        if not (r.extra or {}).get("kap"):
            continue
        st = states.get(r.id)
        last = st.get("at") if st else None
        if last:
            wait = REFRESH_OK if st.get("status") == "ok" else REFRESH_FAILED
            if now < parse_iso(last) + wait:
                continue
        label = deps.holding_priority(conn, list(_issuer_codes(conn, r.id, at).values()), now)
        rank = {"P1": 0, "P2": 1}.get(label or "", 2)
        if held_only and rank == 2:
            continue
        ranked.append((rank, last or "", r.id))
    ranked.sort()
    return [i for _, _, i in ranked]


def refresh_batch(
    conn: Connection,
    client: KapClient,
    *,
    limit: int,
    held_only: bool = False,
    issuer_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Refresh up to ``limit`` due issuers (or the given ones). Commits per issuer."""
    todo = issuer_ids if issuer_ids is not None else due_issuers(conn, now=now, held_only=held_only)
    counts: dict[str, int] = {"ok": 0, "missing": 0, "no_permalink": 0, "error": 0}
    for issuer_id in todo[:limit] if limit else todo:
        try:
            counts[refresh_issuer(conn, client, issuer_id, now=now)] += 1
        except KapNotFound as exc:
            conn.rollback()
            set_state(
                conn,
                f"{STATE_PREFIX}{issuer_id}",
                {"at": (now or now_utc()).isoformat(), "status": "error", "error": str(exc)},
            )
            conn.commit()
            counts["error"] += 1
    if any(counts.values()):
        metrics.record("kap.capital.refreshed", counts["ok"], conn=conn)
        conn.commit()
    return counts
