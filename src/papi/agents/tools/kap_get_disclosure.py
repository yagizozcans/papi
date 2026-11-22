from __future__ import annotations

from typing import Any

from papi.agents.registry import state_get
from papi.agents.tools._kap import NotFound, resolve_disclosure
from papi.analyst.context import _documents, _pack_event, live_events
from papi.core.db import connect
from papi.core.timeutil import to_ist
from papi.triage import taxonomy as tax_mod


def kap_get_disclosure(ref: str) -> dict[str, Any]:
    """Metadata of one KAP disclosure: subject, class, publish time, its documents (body and
    attachments, with sizes) and the rules-triage events already derived from it (code, priority,
    materiality, deterministic payload). `ref` is the KAP disclosure number (e.g. "1688483") or an
    event id. Call this first; then read the text with kap_read_document."""
    tax = tax_mod.load()
    with connect() as conn:
        try:
            d = resolve_disclosure(conn, ref)
        except NotFound as exc:
            return {"success": False, "error": str(exc)}
        docs = [
            {
                "index": i,
                "kind": doc["kind"],
                "language": doc["language"],
                "file_name": (doc["extra"] or {}).get("file_name"),
                "chars": len(doc["text"] or ""),
            }
            for i, doc in enumerate(_documents(conn, d))
        ]
        events = [_pack_event(r, tax) for r in live_events(conn, d["id"])]
        pub = d["published_at"]
        progress: dict[str, list[int]] = state_get(f"kap_read:{d['id']}", {})
        unread = [
            x["index"]
            for x in docs
            if str(x["index"]) not in progress or progress[str(x["index"])][0] < progress[str(x["index"])][1]
        ]
        return {
            "success": True,
            "blocking": f"Documents not fully read yet: {unread}" if unread else None,
            "disclosure_ref": d["source_ref"],
            "kap_url": f"https://www.kap.org.tr/tr/Bildirim/{d['source_ref']}",
            "published_at_ist": f"{to_ist(pub):%Y-%m-%d %H:%M}" if pub else None,
            "subject": d["source_subject"],
            "title": d["title"],
            "kap_summary": d["summary"],
            "class": d["source_class"],
            "type": d["source_type"],
            "is_late": bool(d["is_late"]),
            "revision_kind": d["revision_kind"],
            "documents": docs,
            "events": [
                {
                    "event_id": e.id,
                    "code": e.code,
                    "label_tr": e.label_tr,
                    "priority": e.priority,
                    "materiality": e.materiality,
                    "band": e.materiality_band,
                    "payload_source": e.payload_source,
                    "payload": e.payload,
                    "payload_spans": e.payload_spans,
                }
                for e in events
            ],
        }
