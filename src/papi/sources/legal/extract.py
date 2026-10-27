"""Plain-text extraction for legal documents (PDF, HTML, DOCX) into ordered lines.

The structure parser works line by line, so extraction keeps one entry per visual line (PDF) or per
block element (HTML/DOCX), tagged with its page. Running page headers/footers (``II. KISIM 12``,
``BORSA İSTANBUL A.Ş. PAY PİYASASI YÖNERGESİ``, ``3/40``) are removed: a line at the top or bottom of
a page whose digit-masked form repeats on a large share of pages is page furniture, not law text.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field

from lxml import etree

EXTRACTOR_VERSION = "1"

PDF = "application/pdf"
HTML = "text/html"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = "text/plain"

_WS = re.compile(r"[ \t\u00a0\u2000-\u200b\u202f\u3000]+")
_PAGE_NO = re.compile(r"^(?:sayfa\s*)?\d{1,4}(?:\s*/\s*\d{1,4})?$", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_HTML_BLOCKS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "pre", "blockquote", "dt", "dd")
_BR = "\u2028"  # stands in for <br> while joining inline text


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    page: int | None = None


@dataclass
class Extracted:
    mime: str
    lines: list[Line]
    pages: int | None = None
    parser: str = ""
    removed_furniture: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


class ExtractionError(ValueError):
    pass


def sniff_mime(data: bytes, content_type: str | None = None) -> str:
    """Decide the format from magic bytes first, then the HTTP content type."""
    head = data[:1024].lstrip()
    if head.startswith(b"%PDF"):
        return PDF
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return DOCX
        except zipfile.BadZipFile:
            pass
    lowered = head.lower()
    if (
        lowered.startswith((b"<!doctype html", b"<html", b"<?xml"))
        or b"<html" in lowered
        or b"<body" in lowered
    ):
        return HTML
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in (PDF, HTML, DOCX, TEXT):
        return ct
    if ct == "application/xhtml+xml":
        return HTML
    raise ExtractionError(f"unsupported document format (content-type {content_type!r})")


def clean_line(text: str) -> str:
    return _WS.sub(" ", text.replace("\r", "")).strip()


def extract(data: bytes, content_type: str | None = None) -> Extracted:
    """Extract ordered lines from ``data``; raises ``ExtractionError`` for unreadable input."""
    mime = sniff_mime(data, content_type)
    if mime == PDF:
        return _extract_pdf(data)
    if mime == HTML:
        return _extract_html(data)
    if mime == DOCX:
        return _extract_docx(data)
    return _extract_text(data.decode("utf-8", errors="replace"))


def _extract_text(text: str) -> Extracted:
    """Plain text; form feeds (``\\f``, as written by pdftotext) separate pages."""
    pages = text.split("\f")
    if len(pages) == 1:
        return Extracted(mime=TEXT, lines=_plain_lines(text), parser="text")
    lines = [ln for n, page in enumerate(pages, start=1) for ln in _plain_lines(page, n)]
    return Extracted(mime=TEXT, lines=lines, pages=len(pages), parser="text")


def _plain_lines(text: str, page: int | None = None) -> list[Line]:
    out = []
    for raw in text.splitlines():
        t = clean_line(raw)
        if t:
            out.append(Line(t, page))
    return out


# ----------------------------------------------------------------------------------------------- PDF


def _extract_pdf(data: bytes) -> Extracted:
    import pdfplumber

    pages: list[list[str]] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append([t for t in (clean_line(x) for x in text.splitlines()) if t])
                page.flush_cache()
    except Exception as exc:  # pdfminer raises a zoo of exception types for damaged files
        raise ExtractionError(f"PDF could not be read: {exc}") from exc
    if not any(pages):
        raise ExtractionError("PDF has no extractable text (scanned image?)")
    kept, removed = strip_page_furniture(pages)
    lines = [Line(t, i + 1) for i, page_lines in enumerate(kept) for t in page_lines]
    return Extracted(mime=PDF, lines=lines, pages=len(pages), parser="pdfplumber", removed_furniture=removed)


def strip_page_furniture(pages: list[list[str]], *, edge: int = 3) -> tuple[list[list[str]], list[str]]:
    """Drop repeated running headers/footers and bare page numbers from the first/last ``edge`` lines."""
    n = len(pages)
    counts: Counter[str] = Counter()
    for lines in pages:
        seen = set()
        for t in lines[:edge] + lines[-edge:]:
            key = _DIGITS.sub("#", t)
            if key not in seen:
                seen.add(key)
                counts[key] += 1
    threshold = max(3, int(n * 0.4 + 0.999))
    furniture = {k for k, c in counts.items() if n >= 3 and c >= threshold}
    kept: list[list[str]] = []
    removed: set[str] = set()
    for lines in pages:
        out = list(lines)
        top = [t for t in out[:edge] if not _is_furniture(t, furniture, removed)]
        rest = out[edge:]
        tail = rest[-edge:] if len(rest) > edge else rest
        body = rest[: len(rest) - len(tail)]
        bottom = [t for t in tail if not _is_furniture(t, furniture, removed)]
        kept.append(top + body + bottom)
    return kept, sorted(removed)


def _is_furniture(line: str, furniture: set[str], removed: set[str]) -> bool:
    key = _DIGITS.sub("#", line)
    if key in furniture or _PAGE_NO.match(line):
        removed.add(key)
        return True
    return False


# ---------------------------------------------------------------------------------------------- HTML


def _extract_html(data: bytes) -> Extracted:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "lxml")
    for tag in soup(["script", "style", "noscript", "head", "nav", "footer"]):
        tag.decompose()
    lines: list[Line] = []
    blocks = soup.find_all(_HTML_BLOCKS)
    for block in blocks:
        if block.find(_HTML_BLOCKS):
            continue  # a container: its leaf blocks carry the text
        for br in block.find_all("br"):
            br.replace_with(_BR)
        # Word-exported HTML splits words into per-character spans: join inline text without separators;
        # newlines in the HTML source are plain whitespace, only <br> breaks a line.
        for part in block.get_text("").split(_BR):
            t = clean_line(part.replace("\n", " "))
            if t:
                lines.append(Line(t, None))
    if not lines:
        lines = _plain_lines(soup.get_text("\n"))
    if not lines:
        raise ExtractionError("HTML has no text")
    return Extracted(mime=HTML, lines=lines, parser="bs4-blocks")


# ---------------------------------------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx(data: bytes) -> Extracted:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractionError(f"DOCX could not be read: {exc}") from exc
    root = etree.fromstring(xml)
    lines: list[Line] = []
    for para in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append(" ")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        for piece in "".join(parts).split("\n"):
            t = clean_line(piece)
            if t:
                lines.append(Line(t, None))
    if not lines:
        raise ExtractionError("DOCX has no text")
    return Extracted(mime=DOCX, lines=lines, parser="docx-xml")
