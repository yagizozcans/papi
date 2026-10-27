"""Shared request dependencies and the Jinja2 environment (filters for Istanbul time / TR numbers)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import Connection

from papi.core import db
from papi.core.timeutil import to_ist, to_utc
from papi.web.app import WEB_DIR
from papi.web.queries import PRIORITIES, SCOPES, FeedFilters


def get_conn() -> Iterator[Connection]:
    """One connection per request (deferred transaction); writers call ``commit()`` themselves."""
    with db.connect() as conn:
        try:
            yield conn
        finally:
            if conn.in_transaction():
                conn.rollback()


def fmt_ist(value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, str | datetime):
        value = to_ist(value)
    elif isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return value.strftime(fmt)


def fmt_num(value: Any, digits: int = 2) -> str:
    """Turkish number format: ``1.234.567,89``."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except ArithmeticError:
            return value
    q = f"{float(value) if not isinstance(value, Decimal) else value:,.{digits}f}"
    return q.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    return "%" + fmt_num(value, digits)


def fmt_latency(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f} sn"
    if seconds < 5400:
        return f"{seconds / 60:.0f} dk"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} sa".replace(".", ",")
    return f"{seconds / 86400:.0f} gün"


def fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Evet" if value else "Hayır"
    if isinstance(value, int | float | Decimal):
        return fmt_num(value, 0 if float(value).is_integer() else 2)
    if isinstance(value, list):
        return ", ".join(fmt_value(v) for v in value)
    if value is None:
        return "—"
    return str(value)


_SREF = re.compile(r"\[S\s?(\d+)\]")


def span_refs(text: str | None, doc_index: int = 0) -> Markup:
    """Escape ``text`` and turn ``[S12]`` markers into links that highlight the span in the viewer."""
    if not text:
        return Markup("")
    out = str(escape(text))
    return Markup(
        _SREF.sub(
            lambda m: (
                f'<a class="sref" href="#d{doc_index}s{m[1]}" data-anchor="d{doc_index}s{m[1]}">[S{m[1]}]</a>'
            ),
            out,
        )
    )


def render_document(doc: dict[str, Any]) -> tuple[Markup, list[dict[str, Any]]]:
    """Document text with every span wrapped as an anchor (``id=d<i>s<ordinal>``).

    Returns ``(html, rest)`` where ``rest`` are spans that could not be placed inline (no offsets,
    overlapping or out of range); the template lists them separately so every span stays addressable.
    """
    text: str = doc.get("text") or ""
    placed = sorted(
        (s for s in doc["spans"] if s["char_start"] is not None and s["char_end"] is not None),
        key=lambda s: (s["char_start"], -(s["char_end"])),
    )
    inline: set[str] = set()
    parts: list[str] = []
    pos = 0
    for s in placed:
        start, end = s["char_start"], s["char_end"]
        if start < pos or end > len(text) or end <= start:
            continue
        parts.append(str(escape(text[pos:start])))
        parts.append(
            f'<span class="span" id="{s["anchor"]}" data-s="{s["ordinal"]}" title="[S{s["ordinal"]}]">'
            f"{escape(text[start:end])}</span>"
        )
        inline.add(s["id"])
        pos = end
    parts.append(str(escape(text[pos:])))
    rest = [s for s in doc["spans"] if s["id"] not in inline]
    return Markup("".join(parts)), rest


def to_json(value: Any) -> Markup:
    """JSON for embedding in ``<script type="application/json">`` (``</`` escaped)."""
    return Markup(json.dumps(value, default=str, ensure_ascii=False).replace("</", "<\\/"))


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters.update(
    ist=fmt_ist,
    num=fmt_num,
    pct=fmt_pct,
    latency=fmt_latency,
    val=fmt_value,
    srefs=span_refs,
    tojson_safe=to_json,
)
templates.env.globals["render_document"] = render_document


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return to_utc(value)
    except ValueError:
        return None


def parse_feed_filters(params: Mapping[str, str], *, default_scope: str = "mine") -> FeedFilters:
    """Feed / ``/v1/events`` / stream filters from query parameters (invalid values are ignored)."""
    scope = params.get("scope") or default_scope
    priority = (params.get("priority") or "").upper() or None
    limit = _int(params.get("limit")) or 100
    return FeedFilters(
        scope=scope if scope in SCOPES else default_scope,
        ticker=(params.get("ticker") or "").strip() or None,
        family=(params.get("family") or "").strip() or None,
        code=(params.get("code") or "").strip() or None,
        materiality_gte=_int(params.get("materiality_gte")),
        priority=priority if priority in PRIORITIES else None,
        hide_routine=params.get("hide_routine") in ("1", "true", "on"),
        date_from=_date(params.get("from") or params.get("date_from")),
        date_to=_date(params.get("to") or params.get("date_to")),
        as_of=_datetime(params.get("as_of")),
        limit=max(1, min(limit, 500)),
    )
