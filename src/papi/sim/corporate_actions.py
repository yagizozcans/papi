"""Corporate actions from classified events (12 §3, 05 §5): ``event`` → ``corporate_action`` rows.

Sources
- ``capital.increase.bonus`` → ``bonus`` (factor ``b`` = new shares per held share = rate % / 100)
- ``capital.increase.rights`` → ``rights`` (factor ``r``; ``cash_amount`` = subscription price per new
  share; ex date = first day of the rights period)
- ``distribution.dividend.cash`` → ``dividend_cash`` (``cash_amount`` = gross per share; ``details``
  ``net_amount`` per share and the implied ``withholding_rate``)
- ``distribution.dividend.stock`` → ``dividend_stock`` (factor = rate % / 100)
- ``market.corporate_action_notice`` (MKK "Pay Mali Hak Kullanım İşlemi - Nakit Ödeme") → marks the cash
  dividend ``executed``; the ex date is estimated from the payment date (T+2 settlement: payment = ex
  + 2 sessions) when no announced row exists.
- ``capital.action.cancel`` → the open capital/dividend action of the instrument becomes ``cancelled``.

The TR taxonomy has no split code (BIST shares have nominal 1 TL; "bölünme" is a demerger), so
``split`` / ``reverse_split`` rows are not produced here.

Payloads are the deterministic form payloads written by triage; analyst payloads
(``analysis.completed.v1``) only fill fields that are still null. Status follows the lifecycle
``announced`` < ``approved`` < ``executed`` (never moves back); ``cancelled`` is set by cancellation
notices and by corrections that no longer describe the action.

Identity: one row per (instrument, type, action). An event updates the row already linked to it or to
an event it supersedes (``event.superseded_by`` chain); otherwise it joins an open row of the same
instrument and type whose ex date is compatible; otherwise a new row is inserted. Superseded events are
skipped (their successor is processed instead).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.logging import get_logger
from papi.core.schema import analysis, corporate_action, disclosure, document, event
from papi.core.text import normalize_tr
from papi.core.timeutil import ist_date, now_utc, to_ist
from papi.sim.calculators import D
from papi.sources.prices.service import instrument_for_ticker, is_closed_day, ticker_of

log = get_logger("sim.corporate_actions")

CODE_TYPES: dict[str, str] = {
    "capital.increase.bonus": "bonus",
    "capital.increase.rights": "rights",
    "distribution.dividend.cash": "dividend_cash",
    "distribution.dividend.stock": "dividend_stock",
}
NOTICE_CODE = "market.corporate_action_notice"
CANCEL_CODE = "capital.action.cancel"
HANDLED_CODES = frozenset({*CODE_TYPES, NOTICE_CODE, CANCEL_CODE})

STATUS_RANK = {"announced": 0, "approved": 1, "executed": 2}
MATCH_WINDOW_DAYS = 400
NOTICE_MATCH_DAYS = 7
SETTLEMENT_SESSIONS = 2  # BIST T+2: cash dividend payment date = ex date + 2 sessions

EX_PAYLOAD = "payload"
EX_STRUCTURE = "structure"
EX_ESTIMATED = "estimated_from_payment_date"

# Status cues on the normalised summary ("Özet Bilgi") of the disclosure.
_EXECUTED_RE = re.compile(
    r"\b(tamamlan(di|mistir|masi)|sonuclan(di|mistir|masi)|odemesi yapil|dagitimi yapil)"
)
_APPROVED_RE = re.compile(
    r"\b(genel kurul(u)? sonuc|genel kurul(ca| tarafindan) onay|onaylan(di|mistir|masi|an)|onayi alin|"
    r"spk onay|kurul onay|izahname(nin)? onay|tescil|kesinles)"
)
_PENDING_RE = re.compile(r"\b(basvur|sunul|onayina|onayindan sonra|onaylanmasi halinde|onaylanmasi icin)")


@dataclass
class Draft:
    instrument_id: str
    type: str
    status: str = "announced"
    ex_date: date | None = None
    ex_source: str | None = None
    factor: Decimal | None = None
    cash_amount: Decimal | None = None
    currency: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    is_notice: bool = False


@dataclass
class EventInfo:
    id: str
    code: str
    instrument_ids: list[str]
    announced_at: datetime | None
    payload: dict[str, Any] | None
    superseded_by: str | None
    disclosure_ids: list[str]
    summary_norm: str
    structure: dict[str, Any] | None

    @property
    def announced_date(self) -> date | None:
        return to_ist(self.announced_at).date() if self.announced_at else None


# ------------------------------------------------------------------------------------------- helpers


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return D(value)
    except (ArithmeticError, ValueError, TypeError):
        return None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _iso(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_tr_date(text: Any) -> date | None:
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", str(text or ""))
    if not m:
        return _iso(text)
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _ratio(rate_pct: Any, amount: Any = None, base: Any = None) -> Decimal | None:
    rate = _dec(rate_pct)
    if rate is not None and rate > 0:
        return rate / 100
    a, b = _dec(amount), _dec(base)
    if a is not None and b:
        return a / b
    return None


def _structure_date(
    structure: dict[str, Any] | None, *terms: str, exclude: tuple[str, ...] = ()
) -> date | None:
    """First date whose form label / table header contains all ``terms`` (normalised)."""
    if not structure:
        return None
    norm_terms = [normalize_tr(t) for t in terms]
    norm_excl = [normalize_tr(t) for t in exclude]

    def hit(label: str) -> bool:
        n = normalize_tr(label)
        return all(t in n for t in norm_terms) and not any(x in n for x in norm_excl)

    for f in structure.get("form_fields") or []:
        if hit(str(f.get("label") or "")):
            d = _parse_tr_date(f.get("value"))
            if d:
                return d
    for t in structure.get("tables") or []:
        header = t.get("header") or []
        for ci, h in enumerate(header):
            if not hit(str(h or "")):
                continue
            for row in t.get("rows") or []:
                if ci < len(row):
                    d = _parse_tr_date(row[ci])
                    if d:
                        return d
    return None


def _summary_norm(summary: str | None, structure: dict[str, Any] | None) -> str:
    parts = [summary or ""]
    for f in (structure or {}).get("form_fields") or []:
        if normalize_tr(str(f.get("label") or "")) == "ozet bilgi":
            parts.append(str(f.get("value") or ""))
    return normalize_tr(" ".join(parts))


def status_from_text(summary_norm: str) -> str:
    """``executed`` / ``approved`` / ``announced`` from summary cues (pending-approval wording wins)."""
    if _EXECUTED_RE.search(summary_norm):
        return "executed"
    if _APPROVED_RE.search(summary_norm) and not _PENDING_RE.search(summary_norm):
        return "approved"
    return "announced"


def subtract_sessions(conn: Connection, day: date, sessions: int) -> date:
    d = day
    left = sessions
    while left > 0:
        d -= timedelta(days=1)
        if not is_closed_day(conn, d):
            left -= 1
    return d


# ------------------------------------------------------------------------------------------- loading


def load_event(conn: Connection, event_id: str) -> EventInfo | None:
    ev = conn.execute(select(event).where(event.c.id == event_id)).first()
    if ev is None:
        return None
    summary: str | None = None
    structure: dict[str, Any] | None = None
    if ev.primary_disclosure_id:
        d = conn.execute(
            select(disclosure.c.summary, disclosure.c.body_document_id).where(
                disclosure.c.id == ev.primary_disclosure_id
            )
        ).first()
        if d is not None:
            summary = d.summary
            if d.body_document_id:
                structure = conn.execute(
                    select(document.c.structure).where(document.c.id == d.body_document_id)
                ).scalar_one_or_none()
    return EventInfo(
        id=ev.id,
        code=ev.code,
        instrument_ids=list(ev.instrument_ids or []),
        announced_at=ev.announced_at,
        payload=dict(ev.payload) if isinstance(ev.payload, dict) else None,
        superseded_by=ev.superseded_by,
        disclosure_ids=list(ev.disclosure_ids or []),
        summary_norm=_summary_norm(summary, structure),
        structure=structure,
    )


def predecessors(conn: Connection, event_id: str) -> set[str]:
    """Events superseded (directly or transitively) by ``event_id``."""
    out: set[str] = set()
    frontier = [event_id]
    while frontier:
        rows = conn.execute(select(event.c.id).where(event.c.superseded_by.in_(frontier))).scalars().all()
        frontier = [r for r in rows if r not in out and r != event_id]
        out.update(frontier)
    return out


def analyst_payload(conn: Connection, analysis_ids: list[str], code: str) -> dict[str, Any]:
    """Payload fields proposed by analyses (``output.payload``, optionally keyed by event code)."""
    merged: dict[str, Any] = {}
    if not analysis_ids:
        return merged
    rows = conn.execute(
        select(analysis.c.output).where(analysis.c.id.in_(analysis_ids)).order_by(analysis.c.created_at)
    ).scalars()
    for output in rows:
        if not isinstance(output, dict):
            continue
        p = output.get("payload")
        if isinstance(p, dict) and isinstance(p.get(code), dict):
            p = p[code]
        if isinstance(p, dict):
            merged.update({k: v for k, v in p.items() if v is not None and not isinstance(v, dict | list)})
    return merged


# ------------------------------------------------------------------------------------------- drafts


def _group_values(conn: Connection, instrument_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Dividend amounts of the share group of this instrument's ticker (else the top-level values)."""
    ticker = ticker_of(conn, instrument_id)
    for g in payload.get("share_groups") or []:
        label = str(g.get("group") or "").upper()
        if ticker and re.search(rf"\b{re.escape(ticker)}\b", label):
            return {
                k: g.get(k) if g.get(k) is not None else payload.get(k)
                for k in ("gross_per_share", "net_per_share", "gross_rate_pct")
            }
    return {k: payload.get(k) for k in ("gross_per_share", "net_per_share", "gross_rate_pct")}


def drafts_from_payload(conn: Connection, ev: EventInfo, payload: dict[str, Any]) -> list[Draft]:
    ca_type = CODE_TYPES[ev.code]
    status = status_from_text(ev.summary_norm)
    currency = payload.get("currency") or "TRY"
    out: list[Draft] = []
    for iid in ev.instrument_ids:
        d = Draft(instrument_id=iid, type=ca_type, status=status, currency=currency)
        if ca_type == "bonus":
            d.factor = _ratio(
                payload.get("bonus_rate_pct"), payload.get("bonus_amount"), payload.get("current_capital")
            )
            d.details.update(
                rate_pct=payload.get("bonus_rate_pct"),
                bonus_amount=payload.get("bonus_amount"),
                current_capital=payload.get("current_capital"),
                target_capital=payload.get("target_capital"),
                board_decision_date=payload.get("board_decision_date"),
            )
            ex = _iso(payload.get("ex_date")) or _iso(payload.get("distribution_date"))
            src = EX_PAYLOAD if ex else None
            if ex is None:
                ex = _structure_date(ev.structure, "bedelsiz", "tarih", exclude=("karar",))
                src = EX_STRUCTURE if ex else None
            d.ex_date, d.ex_source = ex, src
        elif ca_type == "rights":
            d.factor = _ratio(
                payload.get("rights_rate_pct"), payload.get("rights_amount"), payload.get("current_capital")
            )
            price = _dec(payload.get("subscription_price"))
            d.cash_amount = price
            start, end = _iso(payload.get("rights_start_date")), _iso(payload.get("rights_end_date"))
            d.details.update(
                rate_pct=payload.get("rights_rate_pct"),
                rights_amount=payload.get("rights_amount"),
                subscription_price=_s(price),
                rights_start_date=start.isoformat() if start else None,
                rights_end_date=end.isoformat() if end else None,
                private_placement_amount=payload.get("private_placement_amount"),
                current_capital=payload.get("current_capital"),
                target_capital=payload.get("target_capital"),
                board_decision_date=payload.get("board_decision_date"),
            )
            ex = _iso(payload.get("ex_date")) or start
            d.ex_date, d.ex_source = ex, (EX_PAYLOAD if ex else None)
        elif ca_type == "dividend_cash":
            g = _group_values(conn, iid, payload)
            gross = _dec(g["gross_per_share"])
            if gross is None and _dec(g["gross_rate_pct"]) is not None:
                gross = _dec(g["gross_rate_pct"]) / 100  # % of nominal 1 TL
            net = _dec(g["net_per_share"])
            d.cash_amount = gross
            final_ex = _structure_date(ev.structure, "kesinlesen", "hak kullanim")
            if payload.get("decision_body") == "general_meeting" or final_ex is not None:
                d.status = max(d.status, "approved", key=STATUS_RANK.__getitem__)
            record = _structure_date(ev.structure, "kayit tarihi")
            d.details.update(
                gross_per_share=_s(gross),
                net_amount=_s(net),
                withholding_rate=_s(1 - net / gross) if gross and net is not None else None,
                gross_rate_pct=payload.get("gross_rate_pct"),
                period_year=payload.get("period_year"),
                decision_body=payload.get("decision_body"),
                installments=payload.get("installments"),
                payment_date=payload.get("payment_date"),
                record_date=record.isoformat() if record else None,
                ex_date_final=final_ex is not None,
            )
            ex = final_ex or _iso(payload.get("ex_date"))
            d.ex_date, d.ex_source = ex, (EX_PAYLOAD if ex else None)
        else:  # dividend_stock
            d.factor = _ratio(payload.get("rate_pct"))
            d.details.update(
                rate_pct=payload.get("rate_pct"),
                amount=payload.get("amount"),
                period_year=payload.get("period_year"),
            )
            ex = _iso(payload.get("ex_date"))
            d.ex_date, d.ex_source = ex, (EX_PAYLOAD if ex else None)
        out.append(d)
    return out


def _cell(row: list[Any], header_norm: list[str], *terms: str) -> Any:
    for i, h in enumerate(header_norm):
        if all(t in h for t in terms) and i < len(row):
            return row[i]
    return None


def drafts_from_notice(conn: Connection, ev: EventInfo) -> list[Draft]:
    """MKK cash payment records (``tbl_MKK_CSV_Report_ID``): one executed dividend per paid share row."""
    out: list[Draft] = []
    for t in (ev.structure or {}).get("tables") or []:
        header = [normalize_tr(str(h or "")) for h in t.get("header") or []]
        if not any("islem tipi" in h for h in header):
            continue
        for row in t.get("rows") or []:
            kind = normalize_tr(str(_cell(row, header, "islem tipi") or ""))
            if "nakit temettu" not in kind:
                continue
            ticker = str(_cell(row, header, "borsa kodu") or "").strip().upper()
            iid = instrument_for_ticker(conn, ticker) if ticker else None
            if iid is None and len(ev.instrument_ids) == 1:
                iid = ev.instrument_ids[0]
            if iid is None:
                log.warning(
                    "MKK notice row without a known instrument", extra={"ticker": ticker, "event": ev.id}
                )
                continue
            gross_pct = _parse_rate(_cell(row, header, "brut oran"))
            net_pct = _parse_rate(_cell(row, header, "net oran"))
            paid = _parse_tr_date(_cell(row, header, "odeme tarihi"))
            gross = gross_pct / 100 if gross_pct is not None else None
            net = net_pct / 100 if net_pct is not None else None
            d = Draft(
                instrument_id=iid,
                type="dividend_cash",
                status="executed",
                cash_amount=gross,
                currency=str(_cell(row, header, "para birimi") or "TRY").strip() or "TRY",
                is_notice=True,
            )
            if paid is not None:
                d.ex_date, d.ex_source = subtract_sessions(conn, paid, SETTLEMENT_SESSIONS), EX_ESTIMATED
            d.details.update(
                gross_per_share=_s(gross),
                net_amount=_s(net),
                withholding_rate=_s(1 - net / gross) if gross and net is not None else None,
                payment_date=paid.isoformat() if paid else None,
                isin=_cell(row, header, "isin kod") if "isin kod" in header else None,
            )
            out.append(d)
    return out


def _parse_rate(value: Any) -> Decimal | None:
    from papi.core.text import parse_tr_number

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parse_tr_number(text)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------- upsert


def _rows_for(conn: Connection, instrument_ids: list[str]) -> list[Any]:
    if not instrument_ids:
        return []
    return conn.execute(
        select(corporate_action)
        .where(corporate_action.c.instrument_id.in_(instrument_ids))
        .order_by(corporate_action.c.created_at.desc())
    ).all()


def _linked(row: Any, event_ids: set[str]) -> bool:
    ids = set((row.details or {}).get("event_ids") or [])
    if row.source_event_id:
        ids.add(row.source_event_id)
    return bool(ids & event_ids)


def _matches(row: Any, d: Draft, announced: date | None) -> bool:
    if row.status == "cancelled" or row.instrument_id != d.instrument_id or row.type != d.type:
        return False
    det = row.details or {}
    row_ann = _iso(det.get("announced_at"))
    if announced and row_ann and abs((announced - row_ann).days) > MATCH_WINDOW_DAYS:
        return False
    row_estimated = det.get("ex_date_source") == EX_ESTIMATED
    if d.is_notice:
        paid = det.get("payment_date")
        if paid and paid == d.details.get("payment_date"):
            return True
        return bool(row.ex_date and d.ex_date and abs((row.ex_date - d.ex_date).days) <= NOTICE_MATCH_DAYS)
    if row.ex_date and announced and row.ex_date < announced and not row_estimated:
        return False  # that action is already past its ex date: a new announcement is a new action
    py_row, py_new = det.get("period_year"), d.details.get("period_year")
    if py_row is not None and py_new is not None and py_row != py_new:
        return False
    if row.ex_date and d.ex_date and row.ex_date != d.ex_date:
        if row_estimated or d.ex_source == EX_ESTIMATED:
            return abs((row.ex_date - d.ex_date).days) <= NOTICE_MATCH_DAYS
        if row.status == "executed":
            return False
        return STATUS_RANK.get(d.status, 0) >= STATUS_RANK.get(row.status, 0)  # date revised by a later step
    return True


def _merge_status(old: str, new: str) -> str:
    if old == "executed":
        return old
    if new == "cancelled" or old == "cancelled":
        return "cancelled" if new == "cancelled" else new
    return new if STATUS_RANK[new] > STATUS_RANK[old] else old


def _append(lst: list[Any] | None, *items: Any) -> list[Any]:
    out = list(lst or [])
    for it in items:
        if it is not None and it not in out:
            out.append(it)
    return out


def upsert(conn: Connection, ev: EventInfo, d: Draft, chain: set[str]) -> str:
    rows = [r for r in _rows_for(conn, [d.instrument_id]) if r.type == d.type]
    announced = ev.announced_date
    row = next((r for r in rows if _linked(r, chain | {ev.id})), None)
    if row is None:
        row = next((r for r in rows if _matches(r, d, announced)), None)
    now = now_utc()
    history_item = {"status": d.status, "event_id": ev.id, "at": now.isoformat()}
    if row is None:
        details = {k: v for k, v in d.details.items() if v is not None}
        details.update(
            event_ids=[ev.id],
            disclosure_ids=list(ev.disclosure_ids),
            announced_at=announced.isoformat() if announced else None,
            ex_date_source=d.ex_source,
            status_history=[history_item],
        )
        cid = uuid7()
        conn.execute(
            corporate_action.insert().values(
                id=cid,
                instrument_id=d.instrument_id,
                type=d.type,
                ex_date=d.ex_date,
                factor=d.factor,
                cash_amount=d.cash_amount,
                currency=d.currency,
                source_event_id=ev.id,
                status=d.status,
                details=details,
                created_at=now,
            )
        )
        return cid
    det = dict(row.details or {})
    det.update({k: v for k, v in d.details.items() if v is not None})
    ex_date, ex_source = row.ex_date, det.get("ex_date_source")
    keep_real = ex_source not in (None, EX_ESTIMATED) and d.ex_source == EX_ESTIMATED and row.ex_date
    if d.ex_date and not keep_real:
        ex_date, ex_source = d.ex_date, d.ex_source
    det["ex_date_source"] = ex_source
    if d.ex_source == EX_ESTIMATED and keep_real:
        det["ex_date_estimated_from_payment"] = d.ex_date.isoformat() if d.ex_date else None
    det["event_ids"] = _append(det.get("event_ids"), ev.id)
    det["disclosure_ids"] = _append(det.get("disclosure_ids"), *ev.disclosure_ids)
    det["announced_at"] = det.get("announced_at") or (announced.isoformat() if announced else None)
    status = _merge_status(row.status, d.status)
    if status != row.status or not any(h.get("event_id") == ev.id for h in det.get("status_history") or []):
        det["status_history"] = [*(det.get("status_history") or []), history_item]
    conn.execute(
        corporate_action.update()
        .where(corporate_action.c.id == row.id)
        .values(
            ex_date=ex_date,
            factor=d.factor if d.factor is not None else row.factor,
            cash_amount=d.cash_amount if d.cash_amount is not None else row.cash_amount,
            currency=d.currency or row.currency,
            source_event_id=ev.id,
            status=status,
            details=det,
            updated_at=now,
        )
    )
    return row.id


def _cancel(conn: Connection, row: Any, ev_id: str, reason: str) -> None:
    det = dict(row.details or {})
    det["cancel_reason"] = reason
    det["event_ids"] = _append(det.get("event_ids"), ev_id)
    det["status_history"] = [
        *(det.get("status_history") or []),
        {"status": "cancelled", "event_id": ev_id, "at": now_utc().isoformat()},
    ]
    conn.execute(
        corporate_action.update()
        .where(corporate_action.c.id == row.id)
        .values(status="cancelled", details=det, updated_at=now_utc())
    )


_CANCEL_HINTS = (
    ("bedelsiz", "bonus"),
    ("bedelli", "rights"),
    ("ruchan", "rights"),
    ("kar payi", "dividend_cash"),
    ("temettu", "dividend_cash"),
)


def cancel_from_notice(conn: Connection, ev: EventInfo) -> list[str]:
    """``capital.action.cancel``: cancel the newest open (not executed) action of each instrument,
    preferring the type named in the summary."""
    hinted = {t for word, t in _CANCEL_HINTS if word in ev.summary_norm}
    out: list[str] = []
    for iid in ev.instrument_ids:
        open_rows = [r for r in _rows_for(conn, [iid]) if r.status in ("announced", "approved")]
        if hinted:
            open_rows = [r for r in open_rows if r.type in hinted] or open_rows
        if open_rows:
            _cancel(conn, open_rows[0], ev.id, "hak_kullanim_iptal")
            out.append(open_rows[0].id)
    return out


# ------------------------------------------------------------------------------------------- entry


def process_event(
    conn: Connection,
    event_id: str,
    *,
    analysis_ids: list[str] | None = None,
    today: date | None = None,
    propose: bool = True,
) -> list[str]:
    """Create/update the corporate actions described by ``event_id``; returns the touched ids.

    When an action's ex date has been reached, pending actions are proposed right away (the portfolio
    slice's hourly ``portfolio.corporate_actions`` periodic covers ex dates reached later). Caller commits.
    """
    ev = load_event(conn, event_id)
    if ev is None or ev.superseded_by:
        return []
    chain = predecessors(conn, ev.id)
    if ev.code not in HANDLED_CODES and not chain:
        return []
    touched: list[str] = []
    if ev.code in CODE_TYPES:
        payload = dict(ev.payload or {})
        for k, v in analyst_payload(conn, analysis_ids or [], ev.code).items():
            if payload.get(k) is None:
                payload[k] = v
        if payload:
            touched = [upsert(conn, ev, d, chain) for d in drafts_from_payload(conn, ev, payload)]
    elif ev.code == NOTICE_CODE:
        touched = [upsert(conn, ev, d, chain) for d in drafts_from_notice(conn, ev)]
    elif ev.code == CANCEL_CODE:
        cancel_from_notice(conn, ev)
    if chain:
        instruments = list(ev.instrument_ids)
        for pid in chain:
            p = conn.execute(select(event.c.instrument_ids).where(event.c.id == pid)).scalar_one_or_none()
            instruments.extend(p or [])
        for row in _rows_for(conn, list(dict.fromkeys(instruments))):
            if row.id in touched or row.status in ("executed", "cancelled") or not _linked(row, chain):
                continue
            _cancel(conn, row, ev.id, "superseded_by_correction")
    if propose:
        _propose_reached(conn, touched, today or ist_date())
    return touched


def _propose_reached(conn: Connection, ca_ids: list[str], today: date) -> None:
    if not ca_ids:
        return
    rows = conn.execute(
        select(corporate_action.c.id, corporate_action.c.ex_date, corporate_action.c.status).where(
            corporate_action.c.id.in_(ca_ids)
        )
    ).all()
    due = [r.id for r in rows if r.ex_date and r.ex_date <= today and r.status != "cancelled"]
    if not due:
        return
    from papi.portfolio.service import propose_for_corporate_action  # C3

    for cid in due:
        propose_for_corporate_action(conn, cid)
