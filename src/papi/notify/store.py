"""``alert_rule`` rows: CRUD, default rules (08 §3.1) and enabled-rule loading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, or_, select, update

from papi.core.ids import uuid7
from papi.core.schema import alert_rule
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc
from papi.notify.rules import Rule, RuleSyntaxError, parse

log = logging.getLogger("papi.notify")

CHANNELS = ("telegram", "macos")
SEEDED_KEY = "notify:default_rules_seeded"

DEFAULT_RULES: tuple[tuple[str, str], ...] = (
    ("Portföy bildirimleri", 'scope = "holdings"'),
    ("İzleme listesi (orta ve üstü önem)", 'scope = "watchlist" AND band >= "medium"'),
    ("Piyasa tedbirleri (portföy/izleme)", 'code = "market.measure" AND scope IN ("holdings", "watchlist")'),
    ("Operasyon sağlığı", 'scope = "ops"'),
)


@dataclass(frozen=True)
class LoadedRule:
    id: str
    name: str
    rule: Rule
    channels: tuple[str, ...]
    quiet_hours: dict[str, Any] | None


def add_rule(
    conn: Connection,
    name: str,
    filter_text: str,
    *,
    channels: list[str] | None = None,
    quiet_hours: dict[str, Any] | None = None,
    enabled: bool = True,
) -> str:
    """Insert a rule after validating its filter (``RuleSyntaxError``) and channels (``ValueError``)."""
    parse(filter_text)
    chans = list(channels or CHANNELS)
    bad = [c for c in chans if c not in CHANNELS]
    if bad:
        raise ValueError(f"bilinmeyen kanal: {', '.join(bad)} (telegram, macos)")
    if conn.execute(select(alert_rule.c.id).where(alert_rule.c.name == name)).first():
        raise ValueError(f"bu adla bir kural zaten var: {name}")
    rid = uuid7()
    conn.execute(
        alert_rule.insert().values(
            id=rid,
            name=name,
            filter=filter_text,
            channels=chans,
            quiet_hours=quiet_hours,
            enabled=enabled,
            created_at=now_utc(),
        )
    )
    return rid


def set_enabled(conn: Connection, ref: str, enabled: bool) -> bool:
    """Enable/disable by id or name; True if a rule was found."""
    res = conn.execute(
        update(alert_rule)
        .where(or_(alert_rule.c.id == ref, alert_rule.c.name == ref))
        .values(enabled=enabled, updated_at=now_utc())
    )
    return res.rowcount > 0


def list_rules(conn: Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(select(alert_rule).order_by(alert_rule.c.created_at)).mappings()]


def seed_default_rules(conn: Connection) -> int:
    """Insert the default rules once (idempotent; a rule deleted later is not re-created). Returns
    the number inserted."""
    if get_state(conn, SEEDED_KEY):
        return 0
    inserted = 0
    for name, text in DEFAULT_RULES:
        if conn.execute(select(alert_rule.c.id).where(alert_rule.c.name == name)).first() is None:
            add_rule(conn, name, text)
            inserted += 1
    set_state(conn, SEEDED_KEY, {"at": now_utc().isoformat(), "inserted": inserted})
    return inserted


def enabled_rules(conn: Connection) -> list[LoadedRule]:
    """Enabled rules with parsed filters; invalid filters are logged and skipped."""
    out = []
    for row in conn.execute(
        select(alert_rule).where(alert_rule.c.enabled.is_(True)).order_by(alert_rule.c.created_at)
    ).mappings():
        try:
            rule = parse(row["filter"])
        except RuleSyntaxError as exc:
            log.error("alert rule %s has an invalid filter: %s", row["name"], exc)
            continue
        out.append(LoadedRule(row["id"], row["name"], rule, tuple(row["channels"] or ()), row["quiet_hours"]))
    return out
