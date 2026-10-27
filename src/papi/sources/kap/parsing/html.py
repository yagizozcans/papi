"""KAP disclosure body HTML → C1 document.

KAP renders bodies in three families, recognised by structure (the template id is the ``tbl_*`` class
of the outer table):

* **taxonomy** (``tbl_oda-<n>_<Name>``, ``tbl_<sector>_role_<n>`` financial statements): rows carry a
  field id in ``.taxonomy-field-name`` (``oda_CorrectionAnnouncementFlag|``), a bilingual title cell
  and value cells marked ``content-tr`` / ``content-en``; free text lives in ``*-summernote`` cells;
  dimensional tables use a ``with-plus-row`` header and ``new-type-row`` data rows; financial
  statements have several ``context-header`` period columns.
* **report** (``tbl_<X>_Report_ID``, ``tbl_GENEL_DUYURU_GONDERIMI``): label/value rows
  (``div.bold`` label + ``div.control-label`` value), bordered data tables, green section headers.
* anything else → **generic** table/paragraph extraction (``template_unknown`` is decided by the
  template registry, not here).

Bilingual bodies yield one document per language (``content-tr`` / ``content-en`` cells).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from papi.sources.kap.parsing.doc import DocBuilder, ParsedDocument, clean

_TBL_CLASS = re.compile(r"^tbl_")
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
)
_SKIP_CLASSES = frozenset({"taxonomy-field-documentation"})
_LANGS = ("tr", "en")


def _classes(el: Tag) -> list[str]:
    return el.get("class") or []  # type: ignore[return-value]


def _hidden(el: Tag) -> bool:
    style = (el.get("style") or "").replace(" ", "").lower()  # type: ignore[union-attr]
    return "display:none" in style and not any(c.startswith("content-") for c in _classes(el))


def _lang_of(el: Tag) -> str | None:
    for c in _classes(el):
        if c in ("content-tr", "content-en"):
            return c.removeprefix("content-")
    return None


def _excluded(el: Tag, lang: str) -> bool:
    other = _lang_of(el)
    if other is not None and other != lang:
        return True
    return any(c in _SKIP_CLASSES for c in _classes(el))


def _text(el: Tag | None, lang: str) -> str:
    """Inline text of ``el`` with the other language's elements removed."""
    if el is None:
        return ""
    out: list[str] = []
    _collect(el, lang, out, blocks=False)
    return clean("".join(out))


def _paragraphs(el: Tag, lang: str) -> list[str]:
    """Block-aware text split into paragraphs (embedded tables flattened to lines)."""
    return [s for s in _segments(el, lang, keep_tables=False) if isinstance(s, str)]


def _segments(el: Tag, lang: str, *, keep_tables: bool = True) -> list[str | list[list[str]]]:
    """Paragraph strings and (with ``keep_tables``) leaf tables as cell grids, in document order."""
    segs: list[str | list[list[str]]] = []
    buf: list[str] = []

    def flush() -> None:
        segs.extend(p for p in (clean(x) for x in "".join(buf).split("\n")) if p)
        buf.clear()

    def on_table(table: Tag) -> bool:
        if not keep_tables or table.find("table") is not None:
            return False
        grid = [[_text(c, lang) for c in _cells(tr)] for tr in _own_rows(table)]
        grid = [r for r in grid if any(r)]
        if grid:
            flush()
            segs.append(grid)
        return True

    _collect(el, lang, buf, blocks=True, on_table=on_table)
    flush()
    return segs


def _collect(
    el: Tag, lang: str, out: list[str], *, blocks: bool, on_table: Callable[[Tag], bool] | None = None
) -> None:
    for child in el.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            out.append(str(child))
            continue
        if not isinstance(child, Tag) or _excluded(child, lang):
            continue
        if child.name in ("script", "style"):
            continue
        if child.name == "table" and on_table is not None and on_table(child):
            continue
        is_block = child.name in _BLOCK_TAGS
        if child.name == "br":
            out.append("\n" if blocks else " ")
            continue
        if is_block:
            out.append("\n" if blocks else " ")
        elif child.name in ("td", "th"):
            out.append(" ")
        _collect(child, lang, out, blocks=blocks, on_table=on_table)
        if is_block:
            out.append("\n" if blocks else " ")


def _own_rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _cells(tr: Tag) -> list[Tag]:
    return [c for c in tr.find_all(["td", "th"], recursive=False)]


def template_ids(parts: list[str]) -> list[str]:
    ids: list[str] = []
    for html in parts:
        soup = BeautifulSoup(html or "", "lxml")
        for t in soup.find_all("table", class_=_TBL_CLASS):
            for c in _classes(t):
                if c.startswith("tbl_") and c not in ids:
                    ids.append(c)
    return ids


def languages(parts: list[str]) -> list[str]:
    """``['tr']`` plus ``'en'`` when the body carries non-empty English free text."""
    langs = ["tr"]
    for html in parts:
        soup = BeautifulSoup(html or "", "lxml")
        for cell in soup.select("td.content-en"):
            classes = _classes(cell)
            if any("summernote" in c for c in classes) and _text(cell, "en"):
                return ["tr", "en"]
    return langs


# ------------------------------------------------------------------------------------------- families


def _family(table: Tag) -> str:
    if table.select_one(".taxonomy-field-name") is not None:
        return "taxonomy"
    if table.select_one("div.control-label") is not None:
        return "report"
    return "generic"


class _TaxonomyParser:
    def __init__(self, b: DocBuilder, lang: str) -> None:
        self.b = b
        self.lang = lang

    def parse(self, table: Tag) -> None:
        section: str | None = None
        ctx_header: list[str] | None = None
        fin_rows: list[list[str]] = []
        fin_ids: list[str | None] = []
        fin_caption: str | None = None
        pending_abstract: tuple[list[str], str | None] | None = None
        rows = _own_rows(table)
        i = 0
        while i < len(rows):
            tr = rows[i]
            classes = _classes(tr)
            cells = _cells(tr)
            if _hidden(tr):
                i += 1
                continue
            ctx_cells = [c for c in cells if "context-header" in _classes(c)]
            if ctx_cells:
                note = [c for c in cells if "taxonomy-footnote-header" in _classes(c)]
                ctx_header = ["Kalem" if self.lang == "tr" else "Item"]
                ctx_header += [_text(c, self.lang) for c in note]
                ctx_header += [_text(c, self.lang) for c in ctx_cells]
                i += 1
                continue
            if "with-plus-row" in classes:
                i = self._dimensional(rows, i, section)
                continue
            info = [c for c in cells if "info-table-title-cell" in _classes(c)]
            if len(info) == 2:
                self.b.form_field(_text(info[0], self.lang), _text(info[1], self.lang))
                i += 1
                continue
            name_cell = next((c for c in cells if "taxonomy-field-name-cell" in _classes(c)), None)
            if name_cell is None:
                i += 1
                continue
            field_id = clean(name_cell.get_text()).split("|")[0].strip() or None
            title_cell = next((c for c in cells if "taxonomy-field-title" in _classes(c)), None)
            label = _text(title_cell, self.lang) if title_cell is not None else ""
            if "abstract-row" in classes:
                section = label or section
                if ctx_header is not None:
                    fin_caption = fin_caption or label
                    pending_abstract = ([label], field_id)
                else:
                    self.b.heading(label)
                i += 1
                continue
            text_cells = [
                c
                for c in cells
                if any("summernote" in k for k in _classes(c)) and not _excluded(c, self.lang)
            ]
            if text_cells:
                segs: list[str | list[list[str]]] = []
                for c in text_cells:
                    segs += _segments(c, self.lang)
                self.b.text_block(label or section or "", segs, field_id)
                i += 1
                continue
            values = [
                c for c in cells if "taxonomy-context-value" in _classes(c) and not _excluded(c, self.lang)
            ]
            footnote = [c for c in cells if "taxonomy-footnote-cell" in _classes(c)]
            if ctx_header is not None and (len(values) > 1 or footnote):
                vals = [_text(c, self.lang) for c in values]
                if any(vals):
                    if pending_abstract is not None:
                        fin_rows.append(pending_abstract[0] + [""] * (len(ctx_header) - 1))
                        fin_ids.append(pending_abstract[1])
                        pending_abstract = None
                    fin_rows.append([label] + [_text(c, self.lang) for c in footnote] + vals)
                    fin_ids.append(field_id)
                i += 1
                continue
            if values:
                self.b.form_field(label, " ".join(_text(c, self.lang) for c in values), field_id)
            i += 1
        if fin_rows:
            width = max(len(r) for r in fin_rows)
            header = (ctx_header or [])[:width] if ctx_header else None
            norm = [r + [""] * (width - len(r)) for r in fin_rows]
            self.b.table(norm, header=header, caption=fin_caption, row_field_ids=fin_ids)

    def _dimensional(self, rows: list[Tag], i: int, section: str | None) -> int:
        caption = section
        for back in range(i - 1, max(i - 4, -1), -1):
            cands = [c for c in _cells(rows[back]) if "taxonomy-title-panel" in _classes(c)]
            text = " ".join(_text(c, self.lang) for c in cands).strip()
            if text:
                caption = text
                break
        header = [
            _text(c, self.lang)
            for c in _cells(rows[i])
            if "taxonomy-dimensional-header-cell" in _classes(c) and not _excluded(c, self.lang)
        ]
        data: list[list[str]] = []
        j = i + 1
        while j < len(rows) and "new-type-row" in _classes(rows[j]):
            cells = [c for c in _cells(rows[j]) if not _excluded(c, self.lang)]
            vals = [_text(c, self.lang) for c in cells if "taxonomy-context-value" in _classes(c)]
            if not vals:
                vals = [_text(c, self.lang) for c in cells]
            data.append(vals)
            j += 1
        if data:
            width = max(len(header), *(len(r) for r in data))
            self.b.table([r + [""] * (width - len(r)) for r in data], header=header or None, caption=caption)
        return j


class _ReportParser:
    def __init__(self, b: DocBuilder, lang: str) -> None:
        self.b = b
        self.lang = lang
        self.caption: str | None = None

    def parse(self, table: Tag) -> None:
        self._walk(table)

    def _walk(self, node: Tag) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "table":
                self._table(child)
            elif child.name == "div" and "bgGreen" in _classes(child):
                self.caption = _text(child, self.lang)
                self.b.heading(self.caption)
            elif child.name == "div" and "control-label" in _classes(child):
                self._free_text(child)
            else:
                self._walk(child)

    def _free_text(self, el: Tag) -> None:
        for p in _paragraphs(el, self.lang):
            self.b.paragraph(p)

    def _table(self, table: Tag) -> None:
        rows = _own_rows(table)
        leaf = [tr for tr in rows if tr.find("table") is None]
        if len(leaf) != len(rows):
            # Layout table: recurse into cells (in document order).
            for tr in rows:
                for cell in _cells(tr):
                    self._walk(cell)
            return
        kv_table = self._is_kv_table(table, rows)
        labelled = [tr for tr in rows if self._label_value(tr, kv_table) is not None]
        if labelled and len(labelled) * 2 >= len(rows):
            for tr in rows:
                lv = self._label_value(tr, kv_table)
                if lv is not None:
                    label, value_cell = lv
                    segs = _segments(value_cell, self.lang)
                    paras = [s for s in segs if isinstance(s, str)]
                    if len(paras) != len(segs) or len(paras) > 1 or sum(len(p) for p in paras) > 300:
                        self.b.text_block(label, segs)
                    else:
                        self.b.form_field(label, " ".join(paras))
                else:
                    for cell in _cells(tr):
                        self._walk(cell)
            return
        grid = [[_text(c, self.lang) for c in _cells(tr)] for tr in rows]
        grid = [r for r in grid if any(r)]
        if len(grid) == 1 and len(grid[0]) == 1:
            for tr in rows:
                for cell in _cells(tr):
                    if cell.select_one("div.control-label") is not None:
                        self._free_text(cell)
                    else:
                        self._walk(cell)
            return
        if not grid:
            return
        header_row = rows[0]
        is_header = (
            "bold" in _classes(header_row)
            or "th" in _classes(header_row)
            or header_row.find("th") is not None
            or table.get("border") is not None
            or "totalTableStyle" in _classes(table)
        )
        header = grid[0] if is_header and len(grid) > 1 else None
        body = grid[1:] if header else grid
        width = max(len(r) for r in grid)
        self.b.table(
            [r + [""] * (width - len(r)) for r in body],
            header=(header + [""] * (width - len(header))) if header else None,
            caption=self.caption,
        )
        self.caption = None

    @staticmethod
    def _is_kv_table(table: Tag, rows: list[Tag]) -> bool:
        """Two-column label/value layout without data-table markers (e.g. ``tbl_GK_Report_ID``)."""
        if table.get("border") is not None or not rows:
            return False
        for tr in rows:
            cells = _cells(tr)
            if not cells or not any(c.get_text(strip=True) for c in cells):
                continue
            if len(cells) != 2 or {"txtCenter", "bold", "th"} & set(_classes(tr)) or tr.find("th"):
                return False
            if len(clean(cells[0].get_text(" "))) > 200:
                return False
        return True

    def _label_value(self, tr: Tag, kv_table: bool = False) -> tuple[str, Tag] | None:
        cells = _cells(tr)
        if len(cells) != 2:
            return None
        if cells[0].select_one("div.bold") is None and not kv_table:
            return None
        label = _text(cells[0], self.lang)
        return (label, cells[1]) if label else None


def _generic(b: DocBuilder, root: Tag, lang: str) -> None:
    """Fallback: every leaf table becomes a table; text outside tables becomes paragraphs."""
    for table in root.find_all("table"):
        if table.find("table") is not None:
            continue
        grid = [[_text(c, lang) for c in _cells(tr)] for tr in _own_rows(table)]
        grid = [r for r in grid if any(r)]
        if not grid:
            continue
        width = max(len(r) for r in grid)
        first = _own_rows(table)[0]
        has_header = first.find("th") is not None and len(grid) > 1
        header = grid[0] + [""] * (width - len(grid[0])) if has_header else None
        body = grid[1:] if has_header else grid
        b.table([r + [""] * (width - len(r)) for r in body], header=header)
    for table in root.find_all("table"):
        table.extract()
    for p in _paragraphs(root, lang):
        b.paragraph(p)


def parse_body(parts: list[str], *, language: str = "tr") -> ParsedDocument:
    """Parse the ``disclosureBody`` HTML parts of one disclosure into a single document."""
    if language not in _LANGS:
        raise ValueError(f"unsupported language {language!r}")
    b = DocBuilder()
    families: list[str] = []
    ids = template_ids(parts)
    for html in parts:
        soup = BeautifulSoup(html or "", "lxml")
        root = soup.body or soup
        for hdr in root.select("table.financial-header-table"):
            for tr in _own_rows(hdr):
                cells = _cells(tr)
                if len(cells) == 2 and not _hidden(tr):
                    b.form_field(_text(cells[0], language), _text(cells[1], language))
            hdr.extract()
        tops = [t for t in root.find_all("table", class_=_TBL_CLASS) if t.find_parent("table") is None]
        if not tops:
            families.append("generic")
            _generic(b, root, language)
            continue
        for t in tops:
            fam = _family(t)
            families.append(fam)
            if fam == "taxonomy":
                _TaxonomyParser(b, language).parse(t)
            elif fam == "report":
                _ReportParser(b, language).parse(t)
            else:
                _generic(b, t, language)
    extra_structure: dict[str, Any] = {"families": families}
    if len(ids) > 1:
        extra_structure["template_ids"] = ids
    return b.build(
        kind="disclosure_body",
        language=language,
        mime="text/html",
        template_id=ids[0] if ids else None,
        extra_structure=extra_structure,
    )
