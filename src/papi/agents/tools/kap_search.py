from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select

from papi.agents.tools._kap import document_offsets
from papi.analyst.context import _one_line, _render_doc_lines
from papi.core.db import connect
from papi.core.schema import disclosure, instrument, source
from papi.core.text import normalize_tr
from papi.core.timeutil import to_ist
from papi.sources.prices.service import instrument_for_ticker

LINE_CHARS = 500
CONTEXT_CHARS = 250
PER_DISCLOSURE = 4
MAX_RESULT_CHARS = 30_000


def kap_search(
    ticker: str, query: str = "", since: str = "", until: str = "", limit: int = 10
) -> dict[str, Any]:
    """Search the company's own KAP disclosures stored in papi (body and attachments: financial
    reports, activity reports, traffic/operating results …), newest first. `since`/`until` are
    YYYY-MM-DD. Without `query`: list the disclosures (ref, date, subject, title). With `query`
    (Turkish words; every word must appear in the same line, case and diacritics ignored, word starts
    match, e.g. "doluluk", "dis hat yolcu", "yakit gider"): matching lines with their span markers
    [S<n>] and one line of context before/after. Cite as `KAP <ref> S<n>`. Coverage: disclosures papi
    has ingested (check the dates returned); an empty result means "not found in papi", not "never
    disclosed"."""
    want = max(1, min(int(limit), 40))
    tokens = normalize_tr(query).split()
    start = date.fromisoformat(since) if since else None
    stop = date.fromisoformat(until) if until else None
    with connect() as conn:
        iid = instrument_for_ticker(conn, ticker.strip().upper())
        issuer_id = (
            conn.execute(select(instrument.c.issuer_id).where(instrument.c.id == iid)).scalar_one_or_none()
            if iid
            else None
        )
        if issuer_id is None:
            return {"success": False, "error": f"no issuer for ticker {ticker!r}"}
        q = (
            select(disclosure)
            .join(source, source.c.id == disclosure.c.source_id)
            .where(source.c.code == "kap", disclosure.c.filer_issuer_id == issuer_id)
            .order_by(disclosure.c.published_at.desc())
        )
        rows = [
            d
            for d in conn.execute(q).mappings()
            if d["published_at"]
            and (start is None or to_ist(d["published_at"]).date() >= start)
            and (stop is None or to_ist(d["published_at"]).date() <= stop)
        ]
        coverage = {
            "disclosures_in_window": len(rows),
            "oldest": to_ist(rows[-1]["published_at"]).date().isoformat() if rows else None,
            "newest": to_ist(rows[0]["published_at"]).date().isoformat() if rows else None,
        }
        if not tokens:
            return {
                "success": True,
                "coverage": coverage,
                "disclosures": [_head(d) for d in rows[:want]],
            }
        hits: list[dict[str, Any]] = []
        for d in rows:
            found = 0
            for k, (doc, base, spans) in enumerate(document_offsets(conn, d)):
                if not all(t in normalize_tr(doc["text"] or "") for t in tokens):
                    continue
                if spans:
                    lines = [line for line, _ in _render_doc_lines(doc, spans, base)]
                else:
                    lines = [_one_line(x) for x in (doc["text"] or "").splitlines() if x.strip()]
                for i, line in enumerate(lines):
                    key = normalize_tr(line)
                    if not all(t in key for t in tokens):
                        continue
                    hits.append(
                        {
                            **_head(d),
                            "document": k,
                            "line": line[:LINE_CHARS],
                            "before": lines[i - 1][:CONTEXT_CHARS] if i else None,
                            "after": lines[i + 1][:CONTEXT_CHARS] if i + 1 < len(lines) else None,
                        }
                    )
                    found += 1
                    if found >= PER_DISCLOSURE or len(hits) >= want:
                        break
                if found >= PER_DISCLOSURE or len(hits) >= want:
                    break
            if len(hits) >= want:
                break
    return {"success": True, "coverage": coverage, "hits": hits}


def _head(d: Any) -> dict[str, Any]:
    return {
        "ref": d["source_ref"],
        "date": to_ist(d["published_at"]).date().isoformat(),
        "subject": d["source_subject"],
        "title": d["title"] if d["title"] != d["source_subject"] else None,
    }
