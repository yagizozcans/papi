"""Weekly review (08 §3.2) at ``alerts.weekly_review`` (e.g. ``sun 18:00``, Istanbul): holdings events of
the week, pending corporate actions, an İşCep reconciliation reminder with papi's quantities, papi's
Claude usage and ops issues. Data only (no LLM).

A slot missed by more than a day (Mac off all weekend) is skipped rather than sent days late.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import Connection

from papi.core.config import get_settings
from papi.core.state import get_state, set_state
from papi.core.timeutil import IST, now_utc, to_ist
from papi.llm import governor
from papi.notify.alerts import _insert, format_line
from papi.notify.context import events_since, load_event_facts, load_universe
from papi.notify.digest import pending_lines, sent_ops_lines
from papi.notify.usage import usage_lines
from papi.portfolio.fmt import fmt_date, fmt_qty
from papi.portfolio.service import positions
from papi.telegram import outbox
from papi.telegram.api import esc

WEEKLY_STATE = "notify:weekly_last_slot"
STALE_AFTER = timedelta(hours=24)
_DAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "pzt": 0, "sal": 1, "car": 2, "per": 3, "cum": 4, "cmt": 5, "paz": 6,
}  # fmt: skip


def parse_schedule(spec: str) -> tuple[int, time]:
    """``"sun 18:00"`` → (6, 18:00). Turkish day abbreviations (``paz 18:00``) work too."""
    try:
        day, hhmm = spec.strip().lower().split()
        h, m = hhmm.split(":")
        return _DAYS[day[:3]], time(int(h), int(m))
    except (ValueError, KeyError):
        raise ValueError(f"alerts.weekly_review geçersiz: {spec!r} (ör. 'sun 18:00')") from None


def last_slot(now: datetime, spec: str | None = None) -> datetime:
    """Most recent scheduled review time ≤ ``now`` (aware, Istanbul)."""
    weekday, at = parse_schedule(spec or get_settings().alerts.weekly_review)
    local = to_ist(now)
    slot = datetime.combine(local.date() - timedelta(days=(local.weekday() - weekday) % 7), at, IST)
    return slot if slot <= local else slot - timedelta(days=7)


def build_weekly(conn: Connection, *, now: datetime | None = None) -> str:
    now = now or now_utc()
    since = now - timedelta(days=7)
    universe = load_universe(conn)
    header = (
        f"📅 <b>Haftalık değerlendirme</b> · {fmt_date(to_ist(since).date())}–{fmt_date(to_ist(now).date())}"
    )
    sections: list[list[str]] = []

    lines = [
        format_line(f)
        for ev in events_since(conn, since, universe.holdings)
        if (f := load_event_facts(conn, ev["id"], universe)) is not None
    ]
    sections.append(
        ["💼 <b>Portföy olayları (7 gün)</b>", *(lines or ["Bu hafta portföy hisselerinde yeni olay yok."])]
    )

    if plines := pending_lines(conn):
        sections.append(plines)

    pos = sorted(positions(conn), key=lambda p: p.ticker)
    recon = ["🧾 <b>İşCep ile mutabakat</b>"]
    if pos:
        recon.append("papi'deki adetleri İşCep ile karşılaştır; fark varsa /mutabakat.")
        table = "\n".join(f"{p.ticker:<7}{fmt_qty(p.quantity):>10}" for p in pos)
        recon.append(f"<pre>{esc(table)}</pre>")
    else:
        recon.append("papi'de açık pozisyon yok; İşCep'te hisse varsa /mutabakat ile ekle.")
    sections.append(recon)

    st = governor.status(conn, now=now)
    sections.append(["🤖 <b>Claude kullanımı</b>", *usage_lines(st, include_tasks=True)])

    ops = sent_ops_lines(conn, since)
    sections.append(["⚠️ <b>Operasyon (7 gün)</b>", *(ops or ["Operasyon sorunu yok."])])
    return header + "\n\n" + "\n\n".join("\n".join(s) for s in sections)


def send_weekly_review(conn: Connection, *, now: datetime | None = None, force: bool = False) -> str | None:
    """Send the review once per scheduled slot; returns the notification id (None if not due)."""
    now = now or now_utc()
    if not outbox.is_ready():
        return None
    slot = last_slot(now)
    key = slot.date().isoformat()
    if not force:
        if get_state(conn, WEEKLY_STATE) == key:
            return None
        if now - slot > STALE_AFTER:
            set_state(conn, WEEKLY_STATE, key)  # missed slot: wait for next week
            return None
    text = build_weekly(conn, now=now)
    sent = outbox.send(conn, text)
    nid = _insert(
        conn,
        kind="digest",
        channel="telegram",
        text=text,
        sent_at=now_utc(),
        delivered=True,
        external_id=str(sent.message_id),
        dedupe_key=f"weekly:{key}:telegram" if not force else f"weekly:{key}:{now.isoformat()}:telegram",
    )
    set_state(conn, WEEKLY_STATE, key)
    return nid
