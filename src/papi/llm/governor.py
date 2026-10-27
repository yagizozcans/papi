"""Usage governor (04 §6): ledger, rolling caps, priority admission, focus, pause, concurrency.

Units: every cap is in "cost-equivalent" units, i.e. sums of the CLI's ``total_cost_usd`` estimate
in ``llm_usage_ledger``; calibration (``papi llm calibrate``) maps them to % of the claude.ai bars.

Buckets: Telegram chat (priority ``CHAT``) has its own rolling 5 h bucket (``share_chat_5h``); the
pipeline 5 h cap (``share_5h``) counts all non-chat calls; the weekly cap (``share_week``) counts all
papi calls. Functions taking ``conn`` never commit.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, case, func, select

from papi.core.config import FocusWindow, Settings, get_settings
from papi.core.ids import uuid7
from papi.core.schema import job, llm_usage_ledger
from papi.core.state import delete_state, get_state, set_state
from papi.core.timeutil import IST, now_utc, parse_iso
from papi.llm.types import LlmResult

WINDOW_5H = timedelta(hours=5)
WINDOW_WEEK = timedelta(days=7)
LIMIT_PAUSE_DEFAULT = timedelta(hours=1)
MANUAL_PAUSE_RECHECK = timedelta(hours=1)
CALIBRATION_RECHECK = timedelta(hours=6)
CALIBRATION_FRACTION_PCT = 10.0  # week-1 target: ≤ 10 % of each claude.ai bar
SLOT_RETRY = timedelta(seconds=15)

PAUSE_KEY = "llm:pause"
CALIBRATION_MARKER_KEY = "llm:calibration_marker"
RATE_LIMIT_KEY = "llm:rate_limit"

PRIORITIES = ("CHAT", "P1", "P2", "P3", "P4", "BACKLOG")
_RANK = {p: i for i, p in enumerate(PRIORITIES)}
# Ledger ``priority`` column values (schema comment: P1..P4 | chat).
_LEDGER_LABEL = {"CHAT": "chat", "BACKLOG": "backlog"}
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Admission:
    ok: bool
    retry_at: datetime | None
    reason: str


def normalize_priority(priority: str) -> str:
    p = priority.strip().upper()
    if p == "BACKFILL":
        p = "BACKLOG"
    if p not in _RANK:
        raise ValueError(f"unknown llm priority {priority!r}; expected one of {', '.join(PRIORITIES)}")
    return p


def ledger_label(priority: str) -> str:
    p = normalize_priority(priority)
    return _LEDGER_LABEL.get(p, p)


# ---------------------------------------------------------------------------------------------
# Ledger


def _outcome(result: LlmResult) -> str:
    return {None: "ok", "schema": "schema_fail", "limit": "limit_hit"}.get(result.error_kind, "error")


def record_usage(
    conn: Connection,
    result: LlmResult,
    *,
    task: str,
    priority: str,
    analysis_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Append a ledger row; a usage-limit result also pauses all Claude work (auto-pause)."""
    now = now or now_utc()
    error = None if result.error_kind is None else f"{result.error_kind}: {result.error or ''}".strip()
    conn.execute(
        llm_usage_ledger.insert().values(
            id=uuid7(),
            analysis_id=analysis_id,
            task=task[:16],
            priority=ledger_label(priority),
            model_ids=result.model_ids or None,
            cost_usd_est=float(result.cost_usd_est or 0.0),
            duration_ms=result.duration_ms,
            outcome=_outcome(result),
            error=error[:2000] if error else None,
            at=now,
        )
    )
    if result.rate_limit:
        set_state(conn, RATE_LIMIT_KEY, {"at": now.isoformat(), "info": result.rate_limit})
    if result.error_kind == "limit":
        until = result.limit_reset_at if result.limit_reset_at and result.limit_reset_at > now else None
        pause(conn, until or now + LIMIT_PAUSE_DEFAULT, reason="limit", now=now)


def subscription_bars(conn: Connection) -> dict[str, Any] | None:
    """Last subscription bar utilisation reported by the CLI (all Claude use, not only papi)."""
    raw = get_state(conn, RATE_LIMIT_KEY)
    if not raw:
        return None
    windows = (raw.get("info") or {}).get("unifiedWindows") or {}

    def bar(name: str) -> dict[str, Any] | None:
        w = windows.get(name)
        if not isinstance(w, dict):
            return None
        resets = w.get("resetsAt")
        return {
            "utilization": w.get("utilization"),
            "resets_at": datetime.fromtimestamp(resets, UTC) if isinstance(resets, int | float) else None,
        }

    return {
        "observed_at": parse_iso(raw["at"]),
        "status": (raw.get("info") or {}).get("status"),
        "five_hour": bar("five_hour"),
        "seven_day": bar("seven_day"),
    }


def _sum(conn: Connection, since: datetime, *, chat: bool | None) -> float:
    q = select(func.coalesce(func.sum(llm_usage_ledger.c.cost_usd_est), 0.0)).where(
        llm_usage_ledger.c.at >= since
    )
    if chat is True:
        q = q.where(llm_usage_ledger.c.priority == "chat")
    elif chat is False:
        q = q.where((llm_usage_ledger.c.priority.is_(None)) | (llm_usage_ledger.c.priority != "chat"))
    return float(conn.execute(q).scalar_one())


def usage_sums(conn: Connection, now: datetime | None = None) -> dict[str, float]:
    """Rolling sums of ``cost_usd_est``: pipeline 5 h, chat 5 h, all-papi 7 d."""
    now = now or now_utc()
    return {
        "used_5h": _sum(conn, now - WINDOW_5H, chat=False),
        "used_chat_5h": _sum(conn, now - WINDOW_5H, chat=True),
        "used_week": _sum(conn, now - WINDOW_WEEK, chat=None),
    }


def _drain_time(
    conn: Connection, now: datetime, window: timedelta, threshold: float, *, chat: bool | None
) -> datetime:
    """Earliest time the rolling ``window`` sum drops below ``threshold`` with no new usage."""
    q = select(llm_usage_ledger.c.at, llm_usage_ledger.c.cost_usd_est).where(
        llm_usage_ledger.c.at >= now - window
    )
    if chat is True:
        q = q.where(llm_usage_ledger.c.priority == "chat")
    elif chat is False:
        q = q.where((llm_usage_ledger.c.priority.is_(None)) | (llm_usage_ledger.c.priority != "chat"))
    rows = conn.execute(q.order_by(llm_usage_ledger.c.at)).all()
    used = sum(float(r.cost_usd_est) for r in rows)
    for r in rows:
        used -= float(r.cost_usd_est)
        if used < threshold:
            return r.at + window + timedelta(seconds=1)
    if threshold <= 0:
        return now + window
    return now + timedelta(minutes=15)


# ---------------------------------------------------------------------------------------------
# Caps, windows and pause


@dataclass
class Caps:
    cap_5h: float
    cap_week: float
    cap_chat_5h: float


def caps(settings: Settings | None = None) -> Caps:
    """Configured caps; in calibration mode additionally ≤ 10 % of each bar when calibrated."""
    llm = (settings or get_settings()).llm
    cap_5h, cap_week = llm.share_5h, llm.share_week
    if llm.calibration_mode:
        if llm.calibration_cost_per_pct_5h:
            cap_5h = min(cap_5h, CALIBRATION_FRACTION_PCT * llm.calibration_cost_per_pct_5h)
        if llm.calibration_cost_per_pct_week:
            cap_week = min(cap_week, CALIBRATION_FRACTION_PCT * llm.calibration_cost_per_pct_week)
    return Caps(cap_5h=cap_5h, cap_week=cap_week, cap_chat_5h=llm.share_chat_5h)


def _hhmm(value: str) -> dtime:
    h, m = value.strip().split(":")
    return dtime(int(h), int(m))


def _window_bounds(day: datetime, start: dtime, end: dtime) -> tuple[datetime, datetime]:
    """Concrete [start, end) of a daily window starting on ``day``'s date (Istanbul)."""
    s = datetime.combine(day.date(), start, tzinfo=IST)
    e = datetime.combine(day.date(), end, tzinfo=IST)
    if e <= s:
        e += timedelta(days=1)
    return s, e


def _active_window_end(
    now: datetime, start: dtime, end: dtime, days: tuple[str, ...] | None
) -> datetime | None:
    """End of the window instance containing ``now``; ``days`` restricts the start day."""
    local = now.astimezone(IST)
    for offset in (0, -1):
        day = local + timedelta(days=offset)
        if days is not None and _DAYS[day.weekday()] not in days:
            continue
        s, e = _window_bounds(day, start, end)
        if s <= local < e:
            return e
    return None


def night_window_end(now: datetime, settings: Settings | None = None) -> datetime | None:
    """End of the current night (backlog) window, or None when outside it."""
    a, b = (settings or get_settings()).llm.night_window.split("-")
    return _active_window_end(now, _hhmm(a), _hhmm(b), None)


def next_night_start(now: datetime, settings: Settings | None = None) -> datetime:
    a, b = (settings or get_settings()).llm.night_window.split("-")
    local = now.astimezone(IST)
    for offset in (0, 1):
        s, _ = _window_bounds(local + timedelta(days=offset), _hhmm(a), _hhmm(b))
        if s > local:
            return s
    return local + timedelta(days=1)


def active_focus(now: datetime, settings: Settings | None = None) -> tuple[FocusWindow, datetime] | None:
    """The strictest focus window active at ``now`` and its end."""
    best: tuple[FocusWindow, datetime] | None = None
    for w in (settings or get_settings()).llm.focus:
        end = _active_window_end(now, _hhmm(w.start), _hhmm(w.end), tuple(w.days))
        if end is None:
            continue
        if best is None or _RANK[w.max_priority] < _RANK[best[0].max_priority]:
            best = (w, end)
    return best


def pause(
    conn: Connection,
    until: datetime | None = None,
    reason: str = "manual",
    *,
    now: datetime | None = None,
) -> None:
    """Pause all Claude calls until ``until`` (None = until ``resume``)."""
    now = now or now_utc()
    set_state(
        conn,
        PAUSE_KEY,
        {"until": until.isoformat() if until else None, "reason": reason, "since": now.isoformat()},
    )


def resume(conn: Connection) -> None:
    delete_state(conn, PAUSE_KEY)


def pause_state(conn: Connection, now: datetime | None = None) -> dict[str, Any] | None:
    """Active pause ``{"until": datetime|None, "reason", "since"}`` or None (expired pauses ignored)."""
    now = now or now_utc()
    raw = get_state(conn, PAUSE_KEY)
    if not raw:
        return None
    until = parse_iso(raw["until"]) if raw.get("until") else None
    if until is not None and until <= now:
        return None
    return {"until": until, "reason": raw.get("reason") or "manual", "since": raw.get("since")}


# ---------------------------------------------------------------------------------------------
# Admission


def admit(conn: Connection, task: str, priority: str, *, now: datetime | None = None) -> Admission:
    """May a ``task`` call at ``priority`` (CHAT | P1..P4 | BACKLOG) run now?"""
    now = now or now_utc()
    s = get_settings()
    llm = s.llm
    p = normalize_priority(priority)

    paused = pause_state(conn, now)
    if paused:
        retry = paused["until"] or now + MANUAL_PAUSE_RECHECK
        return Admission(False, retry, f"paused:{paused['reason']}")

    c = caps(s)
    used = usage_sums(conn, now)

    if used["used_week"] >= c.cap_week:
        return Admission(False, _drain_time(conn, now, WINDOW_WEEK, c.cap_week, chat=None), "cap:week")

    if p == "CHAT":
        if used["used_chat_5h"] >= c.cap_chat_5h:
            return Admission(
                False, _drain_time(conn, now, WINDOW_5H, c.cap_chat_5h, chat=True), "cap:chat_5h"
            )
        return Admission(True, None, "ok")

    if llm.calibration_mode and p not in ("P1", "P2"):
        return Admission(False, now + CALIBRATION_RECHECK, "calibration:p1_p2_only")

    focus = active_focus(now, s)
    if focus and _RANK[p] > _RANK[focus[0].max_priority]:
        return Admission(False, focus[1], f"focus:max_{focus[0].max_priority}")

    if p in ("P4", "BACKLOG"):
        if not llm.backlog_enabled:
            return Admission(False, now + CALIBRATION_RECHECK, "backlog:disabled")
        if night_window_end(now, s) is None:
            return Admission(False, next_night_start(now, s), "backlog:outside_night_window")
        limit = llm.backlog_max_week_fraction * c.cap_week
        if used["used_week"] >= limit:
            return Admission(
                False, _drain_time(conn, now, WINDOW_WEEK, limit, chat=None), "backlog:week_fraction"
            )

    threshold = {
        "P1": c.cap_5h,
        "P2": llm.p2_cap_fraction * c.cap_5h,
        "P3": llm.p3_cap_fraction * c.cap_5h,
        "P4": llm.p3_cap_fraction * c.cap_5h,
        "BACKLOG": llm.p3_cap_fraction * c.cap_5h,
    }[p]
    if used["used_5h"] >= threshold:
        return Admission(False, _drain_time(conn, now, WINDOW_5H, threshold, chat=False), f"cap:5h:{p}")
    return Admission(True, None, "ok")


# ---------------------------------------------------------------------------------------------
# Concurrency: ``llm.concurrency`` claude processes across all workers (flock slots)


class SlotBusy(Exception):
    """All ``llm.concurrency`` slots are taken."""


def _lock_dir(settings: Settings) -> Path:
    path = settings.paths.home / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def concurrency_slot(wait_s: float = 2.0, *, poll_s: float = 0.1) -> Iterator[int]:
    """Hold one of ``llm.concurrency`` process-wide/cross-process slots (``flock``, crash-safe)."""
    s = get_settings()
    n_slots = max(1, int(s.llm.concurrency))
    lock_dir = _lock_dir(s)
    deadline = time.monotonic() + wait_s
    while True:
        for n in range(n_slots):
            fd = os.open(lock_dir / f"llm-slot-{n}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield n
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if time.monotonic() >= deadline:
            raise SlotBusy(f"{n_slots} claude slot(s) busy")
        time.sleep(poll_s)


# ---------------------------------------------------------------------------------------------
# Status


def _queue(conn: Connection) -> dict[str, Any]:
    labels = {5: "CHAT", 10: "P1", 20: "P2", 30: "P3", 40: "P4", 80: "BACKLOG"}
    rows = conn.execute(
        select(job.c.priority, func.count())
        .where(job.c.status == "queued", job.c.last_error.like("llm:%"))
        .group_by(job.c.priority)
    ).all()
    deferred: dict[str, int] = {}
    for prio, n in rows:
        key = labels.get(prio, str(prio))
        deferred[key] = deferred.get(key, 0) + n
    return {"llm_deferred": deferred, "llm_deferred_total": sum(deferred.values())}


def _by_task(conn: Connection, since: datetime) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        select(
            llm_usage_ledger.c.task,
            func.count(),
            func.coalesce(func.sum(llm_usage_ledger.c.cost_usd_est), 0.0),
            func.sum(case((llm_usage_ledger.c.outcome != "ok", 1), else_=0)),
        )
        .where(llm_usage_ledger.c.at >= since)
        .group_by(llm_usage_ledger.c.task)
    ).all()
    return {
        task: {
            "calls": n,
            "cost": round(float(cost), 6),
            "avg_cost": round(float(cost) / n, 6) if n else 0.0,
            "failures": int(failures or 0),
        }
        for task, n, cost, failures in rows
    }


def status(conn: Connection, *, now: datetime | None = None) -> dict:
    now = now or now_utc()
    s = get_settings()
    c = caps(s)
    paused = pause_state(conn, now)
    focus = active_focus(now, s)
    used = usage_sums(conn, now)
    return {
        "paused": paused is not None,
        "paused_until": paused["until"] if paused else None,
        "pause_reason": paused["reason"] if paused else None,
        "used_5h": round(used["used_5h"], 6),
        "used_week": round(used["used_week"], 6),
        "used_chat_5h": round(used["used_chat_5h"], 6),
        "cap_5h": c.cap_5h,
        "cap_week": c.cap_week,
        "cap_chat_5h": c.cap_chat_5h,
        "calibration_mode": s.llm.calibration_mode,
        "focus": {"max_priority": focus[0].max_priority, "until": focus[1]} if focus else None,
        "night_window_active": night_window_end(now, s) is not None,
        "concurrency": s.llm.concurrency,
        "subscription_bars": subscription_bars(conn),
        "by_task": _by_task(conn, now - WINDOW_WEEK),
        "queue": _queue(conn),
    }
