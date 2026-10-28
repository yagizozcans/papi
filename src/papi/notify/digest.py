"""Daily digest (08 §3.2): holdings events of the last 24 h, alerts held during quiet hours, pending
corporate actions and ops issues. Generated from data only (no LLM)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, and_, select, update

from papi.core.config import get_settings
from papi.core.schema import notification
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc, to_ist
from papi.notify.alerts import HELD_QUIET, _hhmm, _insert, format_line
from papi.notify.context import events_since, load_event_facts, load_universe
from papi.portfolio.fmt import fmt_date
from papi.portfolio.service import pending_actions
from papi.telegram import outbox
from papi.telegram.api import esc

DIGEST_STATE = "notify:digest_last_date"


def build_digest(conn: Connection, *, now: datetime | None = None) -> tuple[str, list[str]]:
    """Digest text and the ids of quiet-hours notifications it delivers."""
    now = now or now_utc()
    since = now - timedelta(hours=24)
    universe = load_universe(conn)
    sections: list[list[str]] = []

    events = events_since(conn, since, universe.holdings)
    lines: list[str] = []
    seen_events: set[str] = set()
    for ev in events:
        f = load_event_facts(conn, ev["id"], universe)
        if f is not None:
            lines.append(format_line(f))
            seen_events.add(ev["id"])
    if lines:
        sections.append(["💼 <b>Portföy olayları (son 24 saat)</b>", *lines])

    held = list(
        conn.execute(
            select(notification)
            .where(and_(notification.c.channel == "telegram", notification.c.error == HELD_QUIET))
            .order_by(notification.c.created_at)
        ).mappings()
    )
    held_lines: list[str] = []
    ops_lines: list[str] = []
    for row in held:
        if row["kind"] == "ops":
            ops_lines.append("• " + (row["text"] or "").split("\n", 1)[-1])
        elif row["event_id"] and row["event_id"] not in seen_events:
            f = load_event_facts(conn, row["event_id"], universe)
            if f is not None:
                held_lines.append(format_line(f))
                seen_events.add(row["event_id"])
    if held_lines:
        sections.append(["🔕 <b>Sessiz saatlerde bekletilenler</b>", *held_lines])

    if plines := pending_lines(conn):
        sections.append(plines)

    for line in sent_ops_lines(conn, since):
        if line not in ops_lines:
            ops_lines.append(line)
    if ops_lines:
        sections.append(["⚠️ <b>Operasyon</b>", *ops_lines])

    header = f"🗞 <b>Günlük özet</b> · {fmt_date(to_ist(now).date())}"
    if not sections:
        body = "Son 24 saatte portföyünüzde yeni olay yok; bekleyen işlem ve operasyon sorunu yok."
        return f"{header}\n\n{body}", [r["id"] for r in held]
    return header + "\n\n" + "\n\n".join("\n".join(s) for s in sections), [r["id"] for r in held]


def pending_lines(conn: Connection) -> list[str]:
    """Section listing expected corporate actions (empty if none)."""
    pending = pending_actions(conn)
    if not pending:
        return []
    lines = ["📌 <b>Bekleyen kurumsal işlemler</b> (/bekleyen)"]
    for pa in pending:
        proposal = pa.get("proposal") or {}
        desc = proposal.get("text_tr") or pa.get("kind")
        when = f" · hak kullanım {fmt_date(pa['ex_date'])}" if pa.get("ex_date") else ""
        lines.append(f"• <b>{esc(pa.get('ticker') or '—')}</b>: {esc(desc)}{when}")
    return lines


def sent_ops_lines(conn: Connection, since: datetime) -> list[str]:
    """Distinct ops alerts sent (or delivered via a digest) since ``since``, as bullet lines."""
    out: list[str] = []
    rows = conn.execute(
        select(notification.c.text)
        .where(
            notification.c.kind == "ops",
            notification.c.channel == "telegram",
            notification.c.created_at >= since,
            notification.c.delivered.is_(True),
        )
        .order_by(notification.c.created_at)
    ).scalars()
    for text in rows:
        line = "• " + (text or "").split("\n", 1)[-1]
        if line not in out:
            out.append(line)
    return out


def digest_due(conn: Connection, now: datetime) -> bool:
    local = to_ist(now)
    if local.time() < _hhmm(get_settings().alerts.digest_time):
        return False
    return get_state(conn, DIGEST_STATE) != local.date().isoformat()


def send_digest(conn: Connection, *, now: datetime | None = None, force: bool = False) -> str | None:
    """Send today's digest if due (once per Istanbul day after ``alerts.digest_time``); returns the
    notification id."""
    now = now or now_utc()
    if not outbox.is_ready() or not (force or digest_due(conn, now)):
        return None
    day = to_ist(now).date().isoformat()
    text, held_ids = build_digest(conn, now=now)
    sent = outbox.send(conn, text)
    key = f"digest:{day}:telegram" if not force else f"digest:{day}:{now.isoformat()}:telegram"
    nid = _insert(
        conn, kind="digest", channel="telegram", text=text, sent_at=now_utc(), delivered=True,
        external_id=str(sent.message_id), dedupe_key=key,
    )  # fmt: skip
    if held_ids:
        conn.execute(
            update(notification)
            .where(notification.c.id.in_(held_ids))
            .values(
                delivered=True,
                error=None,
                sent_at=now_utc(),
                external_id=str(sent.message_id),
                reply_to_notification_id=nid,
            )
        )
    set_state(conn, DIGEST_STATE, day)
    return nid
