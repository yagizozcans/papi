from __future__ import annotations

from typing import Any

from papi.agents.tools._kap import NotFound, first_event, resolve_disclosure
from papi.analyst.context import _issuer_section
from papi.core.db import connect
from papi.triage import taxonomy as tax_mod


def kap_issuer_context(ref: str) -> dict[str, Any]:
    """Who filed a KAP disclosure: legal name, sector, ticker, last close, approximate market cap and
    the issuer's other events over the previous 365 days (newest first, with materiality). Use it to
    judge how large the news is relative to the company."""
    with connect() as conn:
        try:
            d = resolve_disclosure(conn, ref)
            ev = first_event(conn, d)
        except NotFound as exc:
            return {"success": False, "error": str(exc)}
        lines = _issuer_section(conn, ev, d, d["published_at"], tax_mod.load(), {ev["id"]})
    return {"success": True, "text": "\n".join(lines[1:])}
