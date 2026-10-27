"""Parsed document model (contract C1) and the builder that keeps ``text`` and spans in sync.

Every citable unit (form field line, table cell, paragraph, PDF line) becomes a span with an ordinal
(``[S<n>]``), its exact ``char_start/char_end`` in ``text`` and a ``field_path``:
``form:<field_id or label_norm>``, ``table:<t>:<r>:<c>``, ``para:<i>``, ``pdf:<page>:<line>``,
``sheet:<name>:<r>:<c>``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from papi.core.text import normalize_tr

PARSER_NAME = "papi.kap"
PARSER_VERSION = "1"

_WS = re.compile(r"[ \t\u00a0\u200b\u202f\r\f\v]+")


def clean(text: str | None) -> str:
    """Collapse whitespace (incl. NBSP) to single spaces and strip."""
    if not text:
        return ""
    return _WS.sub(" ", text.replace("\n", " ")).strip()


@dataclass(slots=True)
class Span:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    field_path: str
    page: int | None = None
    bbox: list[float] | None = None


@dataclass
class ParsedDocument:
    kind: str  # disclosure_body | attachment | kap_pdf
    language: str | None
    mime: str | None
    text: str
    structure: dict[str, Any]
    spans: list[Span]
    parser: str = PARSER_NAME
    parser_version: str = PARSER_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def template_id(self) -> str | None:
        return self.structure.get("template_id")

    def content_hash(self) -> str:
        """Stable hash of what downstream consumers see (text + structure + span layout)."""
        payload = {
            "text": self.text,
            "structure": self.structure,
            "spans": [(s.ordinal, s.char_start, s.char_end, s.field_path, s.page) for s in self.spans],
            "language": self.language,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()


class DocBuilder:
    """Accumulates text lines and spans; structure entries reference span ordinals."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._pos = 0
        self.spans: list[Span] = []
        self.form_fields: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.paragraphs: list[dict[str, Any]] = []

    # -- low level ---------------------------------------------------------------------------------
    def _append(self, s: str) -> int:
        start = self._pos
        self._parts.append(s)
        self._pos += len(s)
        return start

    def _newline(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._append("\n")

    def _span(self, text: str, field_path: str, *, page: int | None = None, bbox: Any = None) -> int:
        start = self._append(text)
        ordinal = len(self.spans) + 1
        self.spans.append(Span(ordinal, text, start, start + len(text), field_path, page, bbox))
        return ordinal

    # -- public ------------------------------------------------------------------------------------
    def heading(self, text: str) -> None:
        """Non-citable heading line (section titles, captions); a repeat of the previous line is dropped."""
        text = clean(text)
        if not text or (self._parts and self._parts[-1] == text + "\n"):
            return
        self._newline()
        self._append(text + "\n")

    def form_field(self, label: str, value: str, field_id: str | None = None) -> int:
        label, value = clean(label), clean(value)
        self._newline()
        key = field_id or normalize_tr(label) or f"field{len(self.form_fields) + 1}"
        ordinal = self._span(f"{label}: {value}" if label else value, f"form:{key}")
        self._append("\n")
        self.form_fields.append(
            {
                "label": label,
                "label_norm": normalize_tr(label),
                "value": value,
                "field_id": field_id,
                "span_ordinal": ordinal,
            }
        )
        return ordinal

    def text_block(
        self, label: str, segments: list[str | list[list[str]]], field_id: str | None = None
    ) -> list[int]:
        """Free-text field: one span per paragraph (embedded tables become tables) plus a form field
        entry pointing at the paragraph spans. ``segments`` items are paragraph strings or table grids."""
        items: list[str | list[list[str]]] = []
        for seg in segments:
            if isinstance(seg, str):
                if clean(seg):
                    items.append(clean(seg))
            elif any(any(clean(c) for c in row) for row in seg):
                items.append(seg)
        if not items:
            return []
        if label:
            self.heading(label)
        ordinals: list[int] = []
        paras: list[str] = []
        for seg in items:
            if isinstance(seg, str):
                ordinals.append(self.paragraph(seg))
                paras.append(seg)
            else:
                width = max(len(r) for r in seg)
                grid = [r + [""] * (width - len(r)) for r in seg]
                if len(grid) > 1:
                    self.table(grid[1:], header=grid[0], caption=None)
                else:
                    self.table(grid)
        self.form_fields.append(
            {
                "label": clean(label),
                "label_norm": normalize_tr(label),
                "value": "\n".join(paras),
                "field_id": field_id,
                "span_ordinal": ordinals[0] if ordinals else None,
                "span_ordinals": ordinals,
                "kind": "text",
            }
        )
        return ordinals

    def paragraph(self, text: str) -> int:
        text = clean(text)
        self._newline()
        idx = len(self.paragraphs)
        ordinal = self._span(text, f"para:{idx}")
        self._append("\n")
        self.paragraphs.append({"text": text, "span_ordinal": ordinal})
        return ordinal

    def table(
        self,
        rows: list[list[str]],
        *,
        header: list[str] | None = None,
        caption: str | None = None,
        row_field_ids: list[str | None] | None = None,
        path_prefix: str = "table",
    ) -> dict[str, Any] | None:
        cleaned = [[clean(c) for c in r] for r in rows]
        fids = list(row_field_ids) if row_field_ids is not None else [None] * len(cleaned)
        pairs = [(r, f) for r, f in zip(cleaned, fids, strict=True) if any(r)]
        if not pairs:
            return None
        rows = [r for r, _ in pairs]
        t = len(self.tables)
        caption = clean(caption) or None
        header = [clean(h) for h in header] if header else None
        if caption:
            self.heading(caption)
        if header and any(header):
            self._newline()
            self._append(" | ".join(header) + "\n")
        span_rows: list[list[int | None]] = []
        for r, row in enumerate(rows):
            self._newline()
            ords: list[int | None] = []
            for c, cell in enumerate(row):
                if c:
                    self._append(" | ")
                ords.append(self._span(cell, f"{path_prefix}:{t}:{r}:{c}") if cell else None)
            self._append("\n")
            span_rows.append(ords)
        entry: dict[str, Any] = {
            "caption": caption,
            "header": header,
            "rows": rows,
            "span_ordinals": span_rows,
        }
        if row_field_ids is not None:
            entry["row_field_ids"] = [f for _, f in pairs]
        self.tables.append(entry)
        return entry

    def line_span(self, text: str, field_path: str, *, page: int | None = None, bbox: Any = None) -> int:
        """One citable line (PDF text lines)."""
        self._newline()
        ordinal = self._span(text, field_path, page=page, bbox=bbox)
        self._append("\n")
        return ordinal

    def build(
        self,
        *,
        kind: str,
        language: str | None,
        mime: str | None,
        template_id: str | None = None,
        extra_structure: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ParsedDocument:
        text = "".join(self._parts).rstrip("\n")
        structure: dict[str, Any] = {
            "template_id": template_id,
            "form_fields": self.form_fields,
            "tables": self.tables,
            "paragraphs": self.paragraphs,
        }
        if extra_structure:
            structure.update(extra_structure)
        return ParsedDocument(
            kind=kind,
            language=language,
            mime=mime,
            text=text,
            structure=structure,
            spans=self.spans,
            extra=extra or {},
        )
