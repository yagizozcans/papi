"""Full-text search over disclosures and provisions (SQLite FTS5).

Tables (created by migration 0001): ``disclosure_fts(id, text)`` keyed by ``disclosure.id`` and
``provision_fts(id, text)`` keyed by ``provision_version.id``. Text is stored as ``normalize_tr()``
output, and queries are normalised the same way, so ``İ/ı/ş/ğ…`` match regardless of case/diacritics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, text

from papi.core.text import normalize_tr

FtsTable = Literal["disclosure_fts", "provision_fts"]


@dataclass(frozen=True, slots=True)
class FtsHit:
    id: str
    snippet: str
    rank: float


def index_text(conn: Connection, table: FtsTable, row_id: str, content: str) -> None:
    """Insert or replace the indexed text for ``row_id`` (normalised with ``normalize_tr``)."""
    delete_text(conn, table, row_id)
    conn.execute(
        text(f"INSERT INTO {table}(id, text) VALUES (:id, :t)"), {"id": row_id, "t": normalize_tr(content)}
    )


def delete_text(conn: Connection, table: FtsTable, row_id: str) -> None:
    conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})


def build_query(query: str, *, prefix: bool = True) -> str:
    """Normalised AND-query of quoted tokens; the last token gets ``*`` when ``prefix``."""
    tokens = normalize_tr(query).split()
    if not tokens:
        return ""
    parts = [f'"{t}"' for t in tokens]
    if prefix:
        parts[-1] += "*"
    return " ".join(parts)


def search(
    conn: Connection, table: FtsTable, query: str, *, limit: int = 50, prefix: bool = True
) -> list[FtsHit]:
    """Ranked hits (best first) with a short snippet; empty query → no hits."""
    match = build_query(query, prefix=prefix)
    if not match:
        return []
    rows = conn.execute(
        text(
            f"SELECT id, snippet({table}, 1, '[', ']', '…', 12) AS snip, rank FROM {table} "
            f"WHERE {table} MATCH :q ORDER BY rank LIMIT :lim"
        ),
        {"q": match, "lim": limit},
    ).all()
    return [FtsHit(id=r.id, snippet=r.snip, rank=r.rank) for r in rows]
