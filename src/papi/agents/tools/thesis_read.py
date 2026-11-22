from __future__ import annotations

from typing import Any

from papi.thesis import document as doc


def thesis_read(ref: str) -> dict[str, Any]:
    """The user's investment thesis (fixed template), parsed deterministically. Returns the front
    matter (ticker, date, direction, horizon), every section in template order (empty ones included)
    with each sentence or bullet prefixed by a marker [T<n>] that you cite as `thesis_markers`, the
    trigger lines with the user's own probability (0–1 or null), the kill-signal lines, and `gaps`:
    format problems the checker found (empty required sections, malformed lines). Call this first."""
    try:
        thesis = doc.load(ref)
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    except doc.ThesisError as exc:
        return {"success": False, "error": "thesis cannot be read", "errors": exc.errors}
    return {"success": True, **thesis.as_dict()}
