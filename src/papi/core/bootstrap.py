"""``papi init``: create home dirs, default config, database (migrated) and seed reference rows.

Idempotent: every step checks before writing; a second run changes nothing.
Seeds: jurisdiction ``TR``; market ``BIST_EQ``; venue ``XIST``; sources ``kap``, ``legal_corpus``,
``isyatirim_prices``, ``yahoo_prices``, ``tefas_prices``; the default portfolio (``portfolio.default_name``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Table, select

from papi.core import db
from papi.core.config import Settings, get_settings, reset_settings, write_default_config
from papi.core.ids import uuid7
from papi.core.schema import jurisdiction, market, portfolio, source, venue
from papi.core.timeutil import now_utc

TR_PACK_VERSION = "0.1.0"

SOURCES: tuple[dict[str, Any], ...] = (
    {
        "code": "kap",
        "access_mode": "public_web",
        "rate_limit": "1 concurrent request; >= kap.min_request_interval s between requests",
        "terms_note": "Public KAP web; personal-volume polling, honest User-Agent, backoff (01, 02 §3).",
    },
    {
        "code": "legal_corpus",
        "access_mode": "public_web",
        "rate_limit": "manifest refresh cadence only",
        "terms_note": "mevzuat.gov.tr / SPK / BIST public texts per packs/tr/legal/manifest.yaml.",
    },
    {
        "code": "isyatirim_prices",
        "access_mode": "public_web",
        "rate_limit": "daily EOD batch",
        "terms_note": "Personal use EOD data; not redistributed.",
    },
    {
        "code": "yahoo_prices",
        "access_mode": "public_web",
        "rate_limit": "daily EOD batch",
        "terms_note": "Cross-check source; personal use.",
    },
    {
        "code": "tefas_prices",
        "access_mode": "public_web",
        "rate_limit": "daily EOD batch",
        "terms_note": "TEFAS fund unit prices for held funds; personal use.",
    },
)


@dataclass
class InitReport:
    created_dirs: list[str] = field(default_factory=list)
    config_written: bool = False
    db_created: bool = False
    revision_before: str | None = None
    revision_after: str | None = None
    seeded: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.created_dirs
            or self.config_written
            or self.db_created
            or self.revision_before != self.revision_after
            or any(self.seeded.values())
        )


def _get_or_insert(conn: Connection, table: Table, key: str, values: dict[str, Any]) -> tuple[str, bool]:
    existing = conn.execute(select(table.c.id).where(table.c[key] == values[key])).scalar_one_or_none()
    if existing is not None:
        return existing, False
    rid = uuid7()
    conn.execute(table.insert().values(id=rid, **values))
    return rid, True


def seed(conn: Connection, settings: Settings | None = None) -> dict[str, int]:
    """Insert reference rows that are missing. Returns rows inserted per table."""
    s = settings or get_settings()
    now = now_utc()
    counts = {"jurisdiction": 0, "market": 0, "venue": 0, "source": 0, "portfolio": 0}

    jid, new = _get_or_insert(
        conn,
        jurisdiction,
        "code",
        {
            "code": "TR",
            "name": "Türkiye",
            "default_tz": "Europe/Istanbul",
            "default_currency": "TRY",
            "languages": ["tr", "en"],
            "pack_version": TR_PACK_VERSION,
            "created_at": now,
        },
    )
    counts["jurisdiction"] += new
    mid, new = _get_or_insert(
        conn,
        market,
        "code",
        {
            "jurisdiction_id": jid,
            "code": "BIST_EQ",
            "name": "Borsa İstanbul Pay Piyasası",
            "asset_class": "equity",
            "created_at": now,
        },
    )
    counts["market"] += new
    _, new = _get_or_insert(
        conn,
        venue,
        "mic",
        {
            "market_id": mid,
            "mic": "XIST",
            "name": "Borsa İstanbul",
            "calendar_id": "XIST",
            "created_at": now,
        },
    )
    counts["venue"] += new
    for src in SOURCES:
        _, new = _get_or_insert(
            conn, source, "code", {**src, "jurisdiction_id": jid, "status": "active", "created_at": now}
        )
        counts["source"] += new
    has_portfolio = conn.execute(select(portfolio.c.id).limit(1)).first() is not None
    if not has_portfolio:
        conn.execute(
            portfolio.insert().values(
                id=uuid7(),
                name=s.portfolio.default_name,
                base_currency=s.portfolio.base_currency,
                account_label=s.portfolio.account_label,
                is_default=True,
                created_at=now,
            )
        )
        counts["portfolio"] += 1
    return counts


def init_home() -> InitReport:
    """Create ``$PAPI_HOME`` layout, default config, migrated DB and seed rows (idempotent)."""
    report = InitReport()
    reset_settings()
    home = get_settings().paths.home
    if not home.exists():
        home.mkdir(parents=True)
        report.created_dirs.append(str(home))
    report.config_written = write_default_config(home)
    reset_settings()  # pick up the file we may just have written
    s = get_settings()
    for d in (s.paths.raw, s.paths.logs, s.paths.backups, s.paths.claude_sandbox, s.paths.db.parent):
        if not d.exists():
            d.mkdir(parents=True)
            report.created_dirs.append(str(d))
    report.db_created = not s.paths.db.exists()
    report.revision_before = None if report.db_created else db.current_revision()
    db.migrate()
    report.revision_after = db.current_revision()
    with db.transaction() as conn:
        report.seeded = seed(conn, s)
    return report
