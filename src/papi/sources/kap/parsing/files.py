"""Attachment parsing: PDF (pdfplumber, pypdf fallback), XLSX/XLS, DOCX, HTML/TXT → C1 documents.

PDF spans are text lines (``pdf:<page>:<line>``, with ``page`` and ``bbox``); PDF tables reference
the line span holding each row. Spreadsheet sheets become tables (caption = sheet name). Formats we
cannot read (legacy ``.doc``, archives, scanned PDFs without a text layer) still yield a document row
with ``extra.unsupported`` / ``extra.ocr_needed`` so the gap is visible.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from papi.sources.kap.parsing.doc import DocBuilder, ParsedDocument, clean

log = logging.getLogger("papi.kap.parse")

MAX_SHEET_ROWS = 5000

_MIME = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "html": "text/html",
    "text": "text/plain",
    "zip": "application/zip",
}

# cp1254 glyphs rendered through a cp1252/Latin-1 font map (common in KAP-generated PDFs/XLS).
_MOJIBAKE = str.maketrans({"Ý": "İ", "ý": "ı", "Þ": "Ş", "þ": "ş", "Ð": "Ğ", "ð": "ğ"})
_MOJIBAKE_HINT = re.compile(r"[ÝýÞþÐð]")
_TURKISH_CHARS = re.compile(r"[ıİşŞğĞ]")


def fix_turkish(text: str) -> str:
    """Repair cp1254-as-cp1252 mojibake (``Ý``→``İ``, ``þ``→``ş`` …) when no real Turkish letters exist."""
    if _MOJIBAKE_HINT.search(text) and not _TURKISH_CHARS.search(text):
        return text.translate(_MOJIBAKE)
    return text


def decode_text(data: bytes) -> str:
    """UTF-8 (with BOM) if valid, else Windows-1254 (superset of ISO-8859-9 for Turkish text)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1254", errors="replace")


_TR_WORDS = frozenset(
    "ve bir ile için bu da de olarak olan şirketimiz tarihli ilişkin göre kadar".split()  # noqa: SIM905
)
_EN_WORDS = frozenset(
    "the and of to in for is on with by our company that this from as are".split()  # noqa: SIM905
)


def guess_language(text: str) -> str | None:
    words = re.findall(r"[a-zçğıöşü]+", text[:20000].lower())
    if not words:
        return None
    tr = sum(1 for w in words if w in _TR_WORDS) + 3 * len(_TURKISH_CHARS.findall(text[:20000]))
    en = sum(1 for w in words if w in _EN_WORDS)
    if tr == en == 0:
        return None
    return "en" if en > tr else "tr"


def sniff(content: bytes, file_name: str | None = None, ext: str | None = None) -> str:
    head = content[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2: xls or doc
        e = (ext or (file_name or "").rsplit(".", 1)[-1]).lower()
        return "doc" if e in ("doc", "dot") else "xls"
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except zipfile.BadZipFile:
            return "unknown"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        return "zip"
    sample = content[:2048].lower()
    if b"<html" in sample or b"<table" in sample or b"<!doctype html" in sample:
        return "html"
    e = (ext or (file_name or "").rsplit(".", 1)[-1]).lower()
    if e in ("txt", "csv"):
        return "text"
    return "unknown"


def parse_file(
    content: bytes, *, kind: str = "attachment", file_name: str | None = None, ext: str | None = None
) -> ParsedDocument:
    fmt = sniff(content, file_name, ext)
    extra: dict[str, Any] = {"format": fmt}
    if file_name:
        extra["file_name"] = file_name
    try:
        if fmt == "pdf":
            doc = parse_pdf(content, kind=kind)
        elif fmt == "xlsx":
            doc = _parse_xlsx(content, kind=kind)
        elif fmt == "xls":
            doc = _parse_xls(content, kind=kind)
        elif fmt == "docx":
            doc = _parse_docx(content, kind=kind)
        elif fmt in ("html", "text"):
            doc = _parse_textual(content, fmt, kind=kind)
        else:
            extra["unsupported"] = True
            return DocBuilder().build(kind=kind, language=None, mime=_MIME.get(fmt), extra=extra)
    except Exception as exc:  # corrupt files must not block the disclosure
        log.warning("attachment parse failed (%s, %s): %s", file_name, fmt, exc)
        extra["parse_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return DocBuilder().build(kind=kind, language=None, mime=_MIME.get(fmt), extra=extra)
    doc.extra = {**extra, **doc.extra}
    if doc.language is None:
        doc.language = guess_language(doc.text)
    return doc


# --------------------------------------------------------------------------------------------- PDF


def parse_pdf(content: bytes, *, kind: str = "attachment") -> ParsedDocument:
    try:
        return _parse_pdf_plumber(content, kind)
    except Exception as exc:
        log.info("pdfplumber failed (%s); falling back to pypdf", exc)
        return _parse_pdf_pypdf(content, kind)


def _parse_pdf_plumber(content: bytes, kind: str) -> ParsedDocument:
    import pdfplumber

    b = DocBuilder()
    pdf_tables: list[dict[str, Any]] = []
    pages = 0
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            pages += 1
            pno = page.page_number
            line_spans: list[tuple[int, str]] = []
            for n, line in enumerate(page.extract_text_lines(strip=True, return_chars=False), start=1):
                text = clean(fix_turkish(line["text"]))
                if not text:
                    continue
                bbox = [round(float(line[k]), 2) for k in ("x0", "top", "x1", "bottom")]
                ordinal = b.line_span(text, f"pdf:{pno}:{n}", page=pno, bbox=bbox)
                line_spans.append((ordinal, text))
            for table in page.extract_tables():
                rows = [[clean(fix_turkish(c or "")) for c in row] for row in table]
                rows = [r for r in rows if any(r)]
                if len(rows) < 2:
                    continue
                pdf_tables.append(
                    {
                        "caption": None,
                        "header": rows[0],
                        "rows": rows[1:],
                        "span_ordinals": [[_row_line(r, line_spans)] * len(r) for r in rows[1:]],
                        "page": pno,
                    }
                )
    doc = b.build(kind=kind, language=None, mime=_MIME["pdf"], extra={"pages": pages})
    doc.structure["tables"] = pdf_tables
    if not doc.text.strip():
        doc.extra["ocr_needed"] = True
    return doc


def _row_line(row: list[str], line_spans: list[tuple[int, str]]) -> int | None:
    cells = [c for c in row if c]
    for ordinal, text in line_spans:
        if all(c in text for c in cells):
            return ordinal
    return None


def _parse_pdf_pypdf(content: bytes, kind: str) -> ParsedDocument:
    from pypdf import PdfReader

    b = DocBuilder()
    reader = PdfReader(io.BytesIO(content))
    for pno, page in enumerate(reader.pages, start=1):
        for n, raw in enumerate((page.extract_text() or "").splitlines(), start=1):
            text = clean(fix_turkish(raw))
            if text:
                b.line_span(text, f"pdf:{pno}:{n}", page=pno)
    doc = b.build(kind=kind, language=None, mime=_MIME["pdf"], extra={"pages": len(reader.pages)})
    doc.parser = f"{doc.parser}+pypdf"
    if not doc.text.strip():
        doc.extra["ocr_needed"] = True
    return doc


# ---------------------------------------------------------------------------------------- sheets


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return fix_turkish(str(value))


def _sheet_table(b: DocBuilder, name: str, rows: list[list[str]]) -> None:
    rows = [r for r in rows if any(r)]
    if not rows:
        return
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = (rows[0], rows[1:]) if len(rows) > 1 else (None, rows)
    b.table(body, header=header, caption=name)


def _parse_xlsx(content: bytes, kind: str) -> ParsedDocument:
    import openpyxl

    b = DocBuilder()
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    break
                rows.append([_cell_str(v) for v in row])
            _sheet_table(b, ws.title, rows)
    finally:
        wb.close()
    return b.build(kind=kind, language=None, mime=_MIME["xlsx"])


def _parse_xls(content: bytes, kind: str) -> ParsedDocument:
    import xlrd

    b = DocBuilder()
    book = xlrd.open_workbook(file_contents=content, on_demand=True)
    for sheet in book.sheets():
        rows = [
            [_cell_str(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
            for r in range(min(sheet.nrows, MAX_SHEET_ROWS))
        ]
        _sheet_table(b, sheet.name, rows)
    return b.build(kind=kind, language=None, mime=_MIME["xls"])


# ------------------------------------------------------------------------------------------ docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _parse_docx(content: bytes, kind: str) -> ParsedDocument:
    b = DocBuilder()
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    for el in list(body) if body is not None else []:
        if el.tag == f"{_W}p":
            text = "".join(t.text or "" for t in el.iter(f"{_W}t"))
            if clean(text):
                b.paragraph(text)
        elif el.tag == f"{_W}tbl":
            rows = [
                [clean("".join(t.text or "" for t in tc.iter(f"{_W}t"))) for tc in tr.iter(f"{_W}tc")]
                for tr in el.iter(f"{_W}tr")
            ]
            rows = [r for r in rows if any(r)]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                b.table(rows[1:] if len(rows) > 1 else rows, header=rows[0] if len(rows) > 1 else None)
    return b.build(kind=kind, language=None, mime=_MIME["docx"])


def _parse_textual(content: bytes, fmt: str, kind: str) -> ParsedDocument:
    text = decode_text(content)
    b = DocBuilder()
    if fmt == "html":
        soup = BeautifulSoup(text, "lxml")
        for table in soup.find_all("table"):
            if table.find("table") is not None:
                continue
            rows = [
                [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])] for tr in table.find_all("tr")
            ]
            rows = [r for r in rows if any(r)]
            if rows:
                width = max(len(r) for r in rows)
                b.table([r + [""] * (width - len(r)) for r in rows])
            table.extract()
        text = soup.get_text("\n")
    for para in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if fmt == "text" else text.split("\n"):
        if clean(para):
            b.paragraph(para)
    return b.build(kind=kind, language=None, mime=_MIME[fmt])
