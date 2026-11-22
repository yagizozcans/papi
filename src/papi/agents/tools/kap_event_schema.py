from __future__ import annotations

import json
from typing import Any

from papi.analyst.context import _schema_notes, schema_skeleton
from papi.triage import taxonomy as tax_mod


def kap_event_schema(code: str = "", query: str = "") -> dict[str, Any]:
    """Event taxonomy and payload schemas. With `code`: the payload skeleton and field notes of that
    event code (the nested shape you must write into the answer's payload). With `query`: taxonomy
    codes whose code or Turkish/English label contains the text. With neither: all codes."""
    tax = tax_mod.load()
    if code:
        entry = tax.get(code)
        if entry is None:
            return {"success": False, "error": f"unknown event code {code!r}; search with query="}
        schema = tax.schema_for(code)
        return {
            "success": True,
            "code": code,
            "label_tr": tax.label(code, "tr"),
            "payload_skeleton": json.dumps(schema_skeleton(schema), ensure_ascii=False) if schema else None,
            "notes": _schema_notes(schema) if schema else [],
        }
    q = query.casefold()
    rows = [
        {"code": c.code, "label_tr": c.label_tr, "has_payload_schema": tax.schema_for(c.code) is not None}
        for c in tax.codes.values()
        if not q or q in c.code.casefold() or q in c.label_tr.casefold()
    ]
    return {"success": True, "count": len(rows), "codes": rows}
