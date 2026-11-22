from __future__ import annotations

from typing import Any

from papi.agents.registry import state_get, state_set
from papi.agents.tools._kap import NotFound, document_offsets, resolve_disclosure
from papi.analyst.context import _one_line, _render_doc_lines
from papi.core.db import connect

PAGE_CHARS = 40_000
MAX_RESULT_CHARS = 60_000


def kap_read_document(ref: str, document: int = 0, offset: int = 0) -> dict[str, Any]:
    """Read the full text of one document of a KAP disclosure (0 = body, 1.. = attachments, as listed
    by kap_get_disclosure). Every line starts with a span marker [S<n>] that you cite as evidence;
    attachment markers start at 1001, 2001, ... The text is returned in pages: while `next_offset` is
    not null, call again with that offset. `reading_status` lists what is still unread across the
    disclosure; while `blocking` is set you must keep reading before you answer."""
    with connect() as conn:
        try:
            d = resolve_disclosure(conn, ref)
        except NotFound as exc:
            return {"success": False, "error": str(exc)}
        parts = document_offsets(conn, d)
        if not 0 <= document < len(parts):
            return {"success": False, "error": f"document {document} out of range (0..{len(parts) - 1})"}
        doc, base, spans = parts[document]
        if spans:
            lines = [line for line, _ in _render_doc_lines(doc, spans, base)]
        else:  # no span markers: citeable only through the text itself
            lines = [_one_line(x) for x in (doc["text"] or "").splitlines() if x.strip()]
    page: list[str] = []
    size = 0
    i = offset
    while i < len(lines) and (size + len(lines[i]) + 1 <= PAGE_CHARS or not page):
        page.append(lines[i])
        size += len(lines[i]) + 1
        i += 1

    # lines read / total per document of this disclosure (per-run state, survives a resume)
    progress: dict[str, list[int]] = state_get(f"kap_read:{d['id']}", {})
    progress[str(document)] = [max(i, progress.get(str(document), [0, 0])[0]), len(lines)]
    state_set(f"kap_read:{d['id']}", progress)
    status: dict[str, str] = {}
    for k in range(len(parts)):
        read, total = progress.get(str(k), [0, None])
        if total is None:
            status[str(k)] = "not opened"
        elif read < total:
            status[str(k)] = f"{read}/{total} lines read; continue at offset {read}"
    return {
        "success": True,
        "document": document,
        "has_span_markers": bool(spans),
        "first_line": offset,
        "total_lines": len(lines),
        "next_offset": i if i < len(lines) else None,
        "reading_status": status or "all documents fully read",
        "blocking": (
            "Unread KAP text remains: " + "; ".join(f"document {k}: {v}" for k, v in status.items())
            if status
            else None
        ),
        "text": "\n".join(page),
    }
