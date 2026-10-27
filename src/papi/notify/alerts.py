"""Alert engine: instant alerts on ``event.classified``, threaded follow-ups on ``analysis.completed``,
revision notices on ``disclosure.revised``, ops alerts, quiet hours, catch-up batching and the
per-minute rate limit (08 §3).

Held alerts are ``notification`` rows with ``delivered = 0`` and ``error`` set to a ``held:*`` marker:

- ``held:quiet`` — quiet hours; delivered with the daily digest;
- ``held:catchup`` — seen while papi was offline; the ``notify:flush`` job sends them individually, or
  as one batch message when there are more than ``alerts.catchup_batch_threshold``;
- ``held:rate`` — over ``telegram.max_messages_per_minute``; ``notify:flush`` sends them as one
  combined message when the window frees up.

All Telegram sends happen inside the caller's transaction, so a failed send rolls the bookkeeping back
and the job is retried (at-least-once; ``notification.dedupe_key`` makes repeats no-ops).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, and_, select, update

from papi.core.config import get_settings
from papi.core.events import ALERT_TRIGGERED, publish
from papi.core.ids import uuid7
from papi.core.jobs import Deferred, enqueue
from papi.core.schema import analysis, claim, disclosure, job, notification, summary
from papi.core.timeutil import IST, now_utc, to_ist
from papi.notify import macos
from papi.notify.context import EventFacts, load_event_facts, ops_context
from papi.notify.store import enabled_rules
from papi.telegram import outbox
from papi.telegram.api import TelegramError, esc

log = logging.getLogger("papi.notify")

HELD_QUIET = "held:quiet"
HELD_RATE = "held:rate"
HELD_CATCHUP = "held:catchup"
FLUSH_JOB_KIND = "notify:flush"
CATCHUP_SETTLE = timedelta(seconds=20)
NOT_DELIVERED_RETRY = timedelta(seconds=60)

PRIORITY_ICON = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "⚪"}
BAND_TR = {"low": "düşük", "medium": "orta", "high": "yüksek", "critical": "kritik"}
REVISION_TR = {"correction": "Düzeltme", "update": "Güncelleme", "blocked": "Erişim kısıtlandı"}
OPS_ICON = {"info": "ℹ️", "warning": "⚠️", "error": "❗", "critical": "🚨"}


# ---------------------------------------------------------------------------------------- helpers


def _hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def quiet_until(now: datetime, quiet: dict[str, Any] | None = None) -> datetime | None:
    """End of the current quiet period (UTC) if ``now`` falls inside one, else ``None``.

    ``quiet`` is a rule's own ``{"start","end"}``; ``None`` uses the global ``alerts`` settings.
    """
    a = get_settings().alerts
    if quiet:
        start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
    elif a.quiet_hours_enabled:
        start, end = _hhmm(a.quiet_start), _hhmm(a.quiet_end)
    else:
        return None
    if start == end:
        return None
    local = to_ist(now)
    t, d = local.time(), local.date()
    if start < end:
        inside, end_date = start <= t < end, d
    else:  # crosses midnight
        inside = t >= start or t < end
        end_date = d + timedelta(days=1) if t >= start else d
    if not inside:
        return None
    return datetime.combine(end_date, end, IST).astimezone(UTC)


def fmt_local(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return "—"
    return to_ist(dt).strftime("%d.%m.%Y %H:%M" if with_date else "%H:%M")


def _trunc(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def dashboard_url(path: str = "/") -> str:
    w = get_settings().web
    return f"http://{w.host}:{w.port}{path}"


def _badge(f: EventFacts) -> str:
    if f.is_holding:
        return "💼 HOLDING"
    if f.is_watched or "watchlist" in f.ctx["scope"]:
        return "👁 WATCH"
    return ""


def _override(f: EventFacts) -> bool:
    return bool(
        get_settings().alerts.holdings_override_quiet and f.is_holding and f.event["priority"] == "P1"
    )


# ---------------------------------------------------------------------------------------- formatting


def format_instant(f: EventFacts) -> str:
    ev, disc = f.event, f.disclosure
    prio = ev["priority"]
    head = f"{PRIORITY_ICON.get(prio or '', '🔔')} <b>{esc(', '.join(f.tickers) or '—')}</b>"
    if badge := _badge(f):
        head += f" · {badge}"
    if prio:
        head += f" · {esc(prio)}"
    lines = [head, f"<b>{esc(f.headline)}</b>"]
    if disc is not None:
        subject = disc["source_subject"]
        detail = disc["summary"] or disc["title"]
        if subject:
            lines.append(f"KAP: {esc(_trunc(subject, 200))}")
        if detail and detail != subject:
            lines.append(esc(_trunc(detail, 500)))
    if ev["materiality"] is not None or ev["materiality_band"]:
        band = BAND_TR.get(ev["materiality_band"] or "", ev["materiality_band"] or "—")
        score = f" ({ev['materiality']}/100)" if ev["materiality"] is not None else ""
        lines.append(f"Önem: {esc(band)}{score}")
    if f.ctx["catchup"]:
        lines.append(f"⏰ late (catch-up) · yayın: {fmt_local(f.published_at)}")
    if f.ctx["revision"]:
        kind = disc["revision_kind"] if disc is not None else ""
        lines.append(f"🔁 {esc(REVISION_TR.get(kind, 'Düzeltme/güncelleme'))}")
    if link := f.link:
        lines.append(f'<a href="{esc(link)}">KAP bildirimi</a>')
    return "\n".join(lines)


def format_line(f: EventFacts) -> str:
    """One-line form used in batches and the digest."""
    prio = f.event["priority"]
    parts = [f"{PRIORITY_ICON.get(prio or '', '•')} <b>{esc(', '.join(f.tickers) or '—')}</b>"]
    if badge := _badge(f):
        parts[0] += f" {badge.split(' ', 1)[1]}"
    if prio:
        parts[0] += f" {esc(prio)}"
    parts.append(esc(_trunc(f.headline, 120)))
    if f.event["materiality_band"]:
        parts.append(f"önem {esc(BAND_TR.get(f.event['materiality_band'], f.event['materiality_band']))}")
    if f.ctx["catchup"]:
        parts.append(f"yayın {fmt_local(f.published_at)}")
    if link := f.link:
        parts.append(f'<a href="{esc(link)}">KAP</a>')
    return " · ".join(parts)


def format_follow_up(conn: Connection, f: EventFacts, analysis_ids: list[str]) -> str | None:
    """Summary + key facts of a completed analysis; ``None`` if there is nothing to say."""
    srow = conn.execute(
        select(summary.c.headline, summary.c.body).where(
            summary.c.event_id == f.event["id"], summary.c.lang == "tr"
        )
    ).first()
    headline, body = (srow.headline, srow.body) if srow else (None, None)
    outputs = [
        r.output or {}
        for r in conn.execute(select(analysis.c.output).where(analysis.c.id.in_(analysis_ids))).all()
    ]
    for out in outputs:
        headline = headline or out.get("headline_tr")
        body = body or out.get("summary_tr")
    facts = [
        r.text_tr
        for r in conn.execute(
            select(claim.c.text_tr)
            .where(claim.c.analysis_id.in_(analysis_ids), claim.c.kind.in_(("fact", "computation")))
            .order_by(claim.c.id)
        ).all()
        if r.text_tr
    ]
    if not facts:
        for out in outputs:
            for c in out.get("claims") or []:
                if isinstance(c, dict) and c.get("kind") in ("fact", "computation") and c.get("text_tr"):
                    facts.append(str(c["text_tr"]))
    if not (headline or body or facts):
        return None
    lines = [f"🧠 <b>Analiz</b> · {esc(', '.join(f.tickers) or '—')}"]
    if headline:
        lines.append(f"<b>{esc(_trunc(headline, 200))}</b>")
    if body:
        lines.append(esc(_trunc(body, 1500)))
    if facts:
        lines.append("")
        lines.append("<b>Öne çıkanlar</b>")
        lines.extend(f"• {esc(_trunc(x, 300))}" for x in facts[:5])
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- storage


def _find(conn: Connection, dedupe_key: str) -> Any:
    return (
        conn.execute(select(notification).where(notification.c.dedupe_key == dedupe_key)).mappings().first()
    )


def _insert(conn: Connection, **values: Any) -> str:
    nid = uuid7()
    conn.execute(notification.insert().values(id=nid, created_at=now_utc(), **values))
    return nid


def instant_for_event(conn: Connection, event_id: str) -> Any:
    return (
        conn.execute(
            select(notification)
            .where(
                notification.c.event_id == event_id,
                notification.c.kind == "instant",
                notification.c.channel == "telegram",
            )
            .order_by(notification.c.delivered.desc(), notification.c.created_at)
        )
        .mappings()
        .first()
    )


def _alerted_in_revision_chain(conn: Connection, disclosure_id: str | None) -> Any:
    """Instant Telegram notification of ``disclosure_id`` or any disclosure it revises."""
    seen: set[str] = set()
    cur = disclosure_id
    while cur and cur not in seen:
        seen.add(cur)
        row = (
            conn.execute(
                select(notification)
                .where(
                    notification.c.disclosure_id == cur,
                    notification.c.kind == "instant",
                    notification.c.channel == "telegram",
                )
                .order_by(notification.c.delivered.desc(), notification.c.created_at)
            )
            .mappings()
            .first()
        )
        if row is not None:
            return row
        cur = conn.execute(
            select(disclosure.c.revision_of).where(disclosure.c.id == cur)
        ).scalar_one_or_none()
    return None


def schedule_flush(conn: Connection, at: datetime) -> None:
    """Make sure a ``notify:flush`` job is queued (one at a time)."""
    queued = conn.execute(
        select(job.c.id).where(job.c.kind == FLUSH_JOB_KIND, job.c.status.in_(("queued", "running")))
    ).first()
    if queued is None:
        enqueue(conn, FLUSH_JOB_KIND, {"at": at.isoformat()}, priority=15, not_before=at)


# ---------------------------------------------------------------------------------------- delivery


def deliver(
    conn: Connection,
    *,
    kind: str,
    text: str,
    dedupe_base: str,
    channels: tuple[str, ...] | list[str] = ("telegram", "macos"),
    event_id: str | None = None,
    disclosure_id: str | None = None,
    alert_rule_id: str | None = None,
    quiet: dict[str, Any] | None = None,
    override_quiet: bool = False,
    catchup: bool = False,
    reply_to: Any = None,
    mac: tuple[str, str, str | None, str | None] | None = None,
    now: datetime | None = None,
) -> str | None:
    """Send (or hold) one alert on each channel; returns the Telegram notification id (if any)."""
    now = now or now_utc()
    common = {
        "alert_rule_id": alert_rule_id,
        "event_id": event_id,
        "disclosure_id": disclosure_id,
        "kind": kind,
    }
    quiet_end = quiet_until(now, quiet)
    held_quiet = quiet_end is not None and not override_quiet
    tg_id: str | None = None

    if "telegram" in channels and outbox.is_ready():
        key = f"{dedupe_base}:telegram"
        existing = _find(conn, key)
        if existing is not None:
            tg_id = existing["id"]
        else:
            held: str | None = None
            if held_quiet:
                held = HELD_QUIET
            elif catchup:
                held = HELD_CATCHUP
                schedule_flush(conn, now + CATCHUP_SETTLE)
            elif outbox.rate_budget(conn, now) <= 0:
                held = HELD_RATE
                schedule_flush(conn, outbox.window_frees_at(conn, now))
            reply_ref = reply_to["id"] if reply_to is not None else None
            if held:
                tg_id = _insert(
                    conn, channel="telegram", text=text, delivered=False, error=held, dedupe_key=key,
                    reply_to_notification_id=reply_ref, **common,
                )  # fmt: skip
            else:
                reply_msg = (
                    int(reply_to["external_id"]) if reply_to is not None and reply_to["external_id"] else None
                )
                try:
                    sent = outbox.send(conn, text, reply_to_message_id=reply_msg, related_event_id=event_id)
                except TelegramError as exc:
                    log.error("telegram %s alert failed: %s", kind, exc)
                    tg_id = _insert(
                        conn, channel="telegram", text=text, delivered=False, error=str(exc)[:500],
                        dedupe_key=key, reply_to_notification_id=reply_ref, **common,
                    )  # fmt: skip
                else:
                    tg_id = _insert(
                        conn, channel="telegram", text=text, delivered=True, sent_at=now_utc(),
                        external_id=str(sent.message_id), dedupe_key=key, reply_to_notification_id=reply_ref,
                        **common,
                    )  # fmt: skip

    if "macos" in channels and mac is not None and macos.enabled() and not held_quiet and not catchup:
        key = f"{dedupe_base}:macos"
        if _find(conn, key) is None:
            title, message, subtitle, url = mac
            ok = macos.notify(title, message, subtitle=subtitle, url=url)
            _insert(
                conn,
                channel="macos",
                text=f"{title}: {message}",
                delivered=ok,
                sent_at=now_utc() if ok else None,
                error=None if ok else "osascript failed",
                dedupe_key=key,
                **common,
            )
    return tg_id


def _mac_for(f: EventFacts) -> tuple[str, str, str | None, str | None]:
    title = f"papi · {', '.join(f.tickers) or f.event['code']}"
    subject = f.disclosure["source_subject"] if f.disclosure is not None else None
    return title, f.headline, subject, dashboard_url(f"/events/{f.event['id']}")


def _matching(conn: Connection, ctx: dict[str, Any]) -> list[Any]:
    return [r for r in enabled_rules(conn) if r.rule.matches(ctx)]


def _channels(rules: list[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for r in rules:
        for c in r.channels:
            if c not in out:
                out.append(c)
    return tuple(out)


def on_event_classified(conn: Connection, event_id: str, *, now: datetime | None = None) -> str | None:
    """Evaluate rules for an event and send the instant alert; returns the notification id."""
    now = now or now_utc()
    f = load_event_facts(conn, event_id)
    if f is None or f.event["superseded_by"] or f.event["status"] == "retracted":
        return None
    if instant_for_event(conn, event_id) is not None:
        return None
    revises = f.disclosure["revision_of"] if f.disclosure is not None else None
    if revises and _alerted_in_revision_chain(conn, revises) is not None:
        return None  # the disclosure.revised handler threads a revision notice instead
    rules = _matching(conn, f.ctx)
    if not rules:
        return None
    return _send_instant(conn, f, rules, format_instant(f), now)


def _send_instant(conn: Connection, f: EventFacts, rules: list[Any], text: str, now: datetime) -> str | None:
    nid = deliver(
        conn,
        kind="instant",
        text=text,
        dedupe_base=f"instant:{f.event['id']}",
        channels=_channels(rules),
        event_id=f.event["id"],
        disclosure_id=f.disclosure["id"] if f.disclosure is not None else None,
        alert_rule_id=rules[0].id,
        quiet=rules[0].quiet_hours,
        override_quiet=_override(f),
        catchup=bool(f.ctx["catchup"]),
        mac=_mac_for(f),
        now=now,
    )
    publish(
        conn,
        ALERT_TRIGGERED,
        f"event/{f.event['id']}",
        {"alert_rule_id": rules[0].id, "event_id": f.event["id"], "notification_id": nid},
        "notify",
    )
    return nid


def _gate(conn: Connection, f: EventFacts | None, now: datetime) -> None:
    """Quiet hours / rate limit for replies in an existing thread: raise ``Deferred`` to wait."""
    end = quiet_until(now)
    if end is not None and not (f is not None and _override(f)):
        raise Deferred(end, "quiet hours")
    if outbox.rate_budget(conn, now) <= 0:
        raise Deferred(outbox.window_frees_at(conn, now), "telegram rate limit")


def _wait_for_original(orig: Any, now: datetime) -> bool:
    """True if a reply to ``orig`` can be sent now; False if it never will (held for the digest or
    failed); raises ``Deferred`` while it is still waiting to be flushed."""
    if orig["delivered"] and orig["external_id"]:
        return True
    if orig["error"] in (HELD_RATE, HELD_CATCHUP):
        raise Deferred(now + NOT_DELIVERED_RETRY, "original alert not delivered yet")
    return False


def on_analysis_completed(
    conn: Connection, event_id: str, analysis_ids: list[str], *, now: datetime | None = None
) -> str | None:
    """Threaded follow-up (summary + key facts) to the instant alert. If no alert was sent, the rules
    are re-evaluated (payload conditions may match now) and a full alert is sent instead."""
    now = now or now_utc()
    if not analysis_ids:
        return None
    f = load_event_facts(conn, event_id)
    if f is None or f.event["superseded_by"]:
        return None
    follow = format_follow_up(conn, f, analysis_ids)
    orig = instant_for_event(conn, event_id)
    if orig is None:
        rules = _matching(conn, f.ctx)
        if not rules:
            return None
        text = format_instant(f) + (f"\n\n{follow}" if follow else "")
        return _send_instant(conn, f, rules, text, now)
    if follow is None:
        return None
    key = f"follow_up:{event_id}:{analysis_ids[0]}:telegram"
    if _find(conn, key) is not None or not _wait_for_original(orig, now) or not outbox.is_ready():
        return None
    _gate(conn, f, now)
    sent = outbox.send(conn, follow, reply_to_message_id=int(orig["external_id"]), related_event_id=event_id)
    return _insert(
        conn, alert_rule_id=orig["alert_rule_id"], event_id=event_id, disclosure_id=orig["disclosure_id"],
        kind="follow_up", channel="telegram", text=follow, sent_at=now_utc(), delivered=True,
        external_id=str(sent.message_id), reply_to_notification_id=orig["id"], dedupe_key=key,
    )  # fmt: skip


def on_disclosure_revised(
    conn: Connection, disclosure_id: str, revision_of: str, revision_kind: str, *, now: datetime | None = None
) -> str | None:
    """Revision notice as a reply to the original alert (if one was sent)."""
    now = now or now_utc()
    key = f"revision:{disclosure_id}:telegram"
    if _find(conn, key) is not None or not outbox.is_ready():
        return None
    orig = _alerted_in_revision_chain(conn, revision_of)
    if orig is None or not _wait_for_original(orig, now):
        return None
    f = load_event_facts(conn, orig["event_id"]) if orig["event_id"] else None
    new = conn.execute(select(disclosure).where(disclosure.c.id == disclosure_id)).mappings().first()
    tickers = ", ".join(f.tickers) if f is not None else ""
    lines = [
        f"🔁 <b>{esc(REVISION_TR.get(revision_kind, 'Revizyon'))}</b>"
        + (f" · <b>{esc(tickers)}</b>" if tickers else "")
    ]
    if new is not None:
        if new["source_subject"]:
            lines.append(f"KAP: {esc(_trunc(new['source_subject'], 200))}")
        detail = new["summary"] or new["title"]
        if detail and detail != new["source_subject"]:
            lines.append(esc(_trunc(detail, 500)))
        if new["source_ref"]:
            lines.append(
                f'<a href="https://www.kap.org.tr/tr/Bildirim/{esc(new["source_ref"])}">KAP bildirimi</a>'
            )
    text = "\n".join(lines)
    _gate(conn, f, now)
    sent = outbox.send(
        conn, text, reply_to_message_id=int(orig["external_id"]), related_event_id=orig["event_id"]
    )
    return _insert(
        conn, alert_rule_id=orig["alert_rule_id"], event_id=orig["event_id"], disclosure_id=disclosure_id,
        kind="revision", channel="telegram", text=text, sent_at=now_utc(), delivered=True,
        external_id=str(sent.message_id), reply_to_notification_id=orig["id"], dedupe_key=key,
    )  # fmt: skip


def send_ops(
    conn: Connection, payload: dict[str, Any], *, job_id: str, now: datetime | None = None
) -> str | None:
    """Execute a ``notify:ops`` job (see :func:`papi.notify.ops.ops_alert`)."""
    level = str(payload.get("level") or "warning")
    rules = _matching(conn, ops_context(level))
    if not rules:
        log.info("ops alert dropped (no enabled ops rule): %s", payload.get("text"))
        return None
    message = str(payload.get("text") or "")
    text = f"{OPS_ICON.get(level, '⚠️')} <b>papi ops</b> · {esc(level)}\n{esc(message)}"
    return deliver(
        conn,
        kind="ops",
        text=text,
        dedupe_base=f"ops:{job_id}",
        channels=_channels(rules),
        alert_rule_id=rules[0].id,
        quiet=rules[0].quiet_hours,
        override_quiet=level == "critical",
        mac=("papi · ops", _trunc(message, 200), level, dashboard_url("/ops")),
        now=now,
    )


# ---------------------------------------------------------------------------------------- flush


def _held(conn: Connection, marker: str) -> list[Any]:
    return list(
        conn.execute(
            select(notification)
            .where(and_(notification.c.channel == "telegram", notification.c.error == marker))
            .order_by(notification.c.created_at)
        ).mappings()
    )


def _mark_delivered(conn: Connection, ids: list[str], message_id: int) -> None:
    conn.execute(
        update(notification)
        .where(notification.c.id.in_(ids))
        .values(delivered=True, error=None, sent_at=now_utc(), external_id=str(message_id))
    )


def flush(conn: Connection, *, now: datetime | None = None) -> dict[str, int]:
    """Deliver catch-up and rate-limited alerts. Returns counts sent per marker."""
    now = now or now_utc()
    counts = {"catchup": 0, "rate": 0}
    if not outbox.is_ready():
        return counts
    threshold = get_settings().alerts.catchup_batch_threshold

    catchup = _held(conn, HELD_CATCHUP)
    if catchup:
        if len(catchup) > threshold:
            if outbox.rate_budget(conn, now) <= 0:
                schedule_flush(conn, outbox.window_frees_at(conn, now))
                return counts
            lines = [
                f"⏰ <b>late (catch-up)</b> · papi çevrimdışıyken yayımlanan {len(catchup)} bildirim:",
                "",
            ]
            for row in catchup:
                f = load_event_facts(conn, row["event_id"]) if row["event_id"] else None
                lines.append(format_line(f) if f is not None else esc(_trunc(row["text"] or "", 200)))
            sent = outbox.send(conn, "\n".join(lines), intent=None)
            _mark_delivered(conn, [r["id"] for r in catchup], sent.message_id)
            counts["catchup"] = len(catchup)
            if macos.enabled():
                macos.notify("papi · catch-up", f"{len(catchup)} gecikmeli bildirim", url=dashboard_url("/"))
        else:
            for row in catchup:
                if outbox.rate_budget(conn, now) <= 0:
                    conn.execute(
                        update(notification).where(notification.c.id == row["id"]).values(error=HELD_RATE)
                    )
                    continue
                sent = outbox.send(conn, row["text"] or "", related_event_id=row["event_id"])
                _mark_delivered(conn, [row["id"]], sent.message_id)
                counts["catchup"] += 1

    rate = _held(conn, HELD_RATE)
    if rate:
        if outbox.rate_budget(conn, now) <= 0:
            schedule_flush(conn, outbox.window_frees_at(conn, now))
            return counts
        if len(rate) == 1:
            row = rate[0]
            reply_msg = None
            if row["reply_to_notification_id"]:
                parent = conn.execute(
                    select(notification.c.external_id).where(
                        notification.c.id == row["reply_to_notification_id"]
                    )
                ).scalar_one_or_none()
                reply_msg = int(parent) if parent else None
            sent = outbox.send(
                conn, row["text"] or "", reply_to_message_id=reply_msg, related_event_id=row["event_id"]
            )
        else:
            header = f"📦 <b>{len(rate)} bildirim</b> (dakikalık mesaj sınırı aşıldı, toplu gönderim)"
            body = "\n\n— — —\n\n".join(r["text"] or "" for r in rate)
            sent = outbox.send(conn, f"{header}\n\n{body}")
        _mark_delivered(conn, [r["id"] for r in rate], sent.message_id)
        counts["rate"] = len(rate)
    return counts
