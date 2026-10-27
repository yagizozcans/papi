"""Shared helpers for the KAP pipeline modules."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from papi.core import jobs
from papi.core.rawstore import source_id_for
from papi.core.timeutil import parse_kap_datetime

SOURCE_CODE = "kap"
EVENT_SOURCE = "ingest/kap"

JOB_FETCH = "kap:fetch"
JOB_PROBE = "kap:probe"
JOB_REFRESH = "kap:refresh"

# Job priorities (lower = sooner). Holdings/watchlist use P1/P2 regardless of mode, except backfill.
PRIO_LIVE = 50
PRIO_CATCHUP = 55
PRIO_GAP = 60
PRIO_RECONCILE = 60
PRIO_BACKFILL_HELD = 75
PRIO_BACKFILL = jobs.PRIORITY_BACKFILL

MODES = ("live", "catchup", "reconcile", "gap", "backfill", "manual")


def kap_source_id(conn: Connection) -> str:
    return source_id_for(conn, SOURCE_CODE)


def release(conn: Connection) -> None:
    """End ``conn``'s transaction (commit) before network I/O so no SQLite lock/snapshot is held."""
    if conn.in_transaction():
        conn.commit()


def item_hash(item: dict[str, Any]) -> str:
    blob = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def split_list(value: Any) -> list[str]:
    """``"A, B"`` / ``"[A, B]"`` / list → ``["A", "B"]`` (upper-cased, de-duplicated, order kept)."""
    if value is None:
        return []
    if isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        parts = str(value).strip().strip("[]").replace(";", ",").split(",")
    out: list[str] = []
    for p in parts:
        code = p.strip().upper()
        if code and code != "-" and code not in out:
            out.append(code)
    return out


def item_tickers(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(filer codes, related codes) from a list item or a detail ``disclosureBasic``."""
    filer = split_list(item.get("stockCodes") if "stockCodes" in item else item.get("stockCode"))
    related = [c for c in split_list(item.get("relatedStocks")) if c not in filer]
    return filer, related


def published_at(item: dict[str, Any]) -> datetime | None:
    raw = item.get("publishDate")
    if not raw:
        return None
    try:
        return parse_kap_datetime(str(raw).strip())
    except ValueError:
        return None


def priority_value(label: str | None, mode: str) -> int:
    if mode == "backfill":
        return PRIO_BACKFILL_HELD if label else PRIO_BACKFILL
    if label:
        return jobs.priority_for(label)
    return {
        "live": PRIO_LIVE,
        "manual": PRIO_LIVE,
        "catchup": PRIO_CATCHUP,
        "gap": PRIO_GAP,
        "reconcile": PRIO_RECONCILE,
    }.get(mode, PRIO_LIVE)
