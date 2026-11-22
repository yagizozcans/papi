from __future__ import annotations

from typing import Any

from papi import kb
from papi.analyst.context import kb_excerpt
from papi.core.db import connect


def kb_search(query: str, as_of: str = "", k: int = 5) -> dict[str, Any]:
    """Search the regulatory knowledge base (SPK/KAP/BIST provisions in force on `as_of`, YYYY-MM-DD,
    default today). Returns provisions with a `citation_id` you can cite as evidence for obligations
    (e.g. disclosure deadlines). Query in Turkish, e.g. "pay geri alım bildirim süresi"."""
    with connect() as conn:
        hits = kb.search(conn, query, as_of or None, k=max(1, min(k, 10)))
    return {
        "success": True,
        "hits": [
            {
                "citation_id": h.version.citation_id,
                "label": h.version.label_tr() + (f" — {h.version.heading}" if h.version.heading else ""),
                "text": kb_excerpt(h.version.text),
            }
            for h in hits
        ],
    }
