"""Turkish legal structure parser: lines → provision tree with stable paths (03 §3.1, KB-2).

Two document shapes are supported:

* ``articles`` (laws, tebliğler, yönergeler, yönetmelikler): ``KISIM`` → ``BÖLÜM`` → ``MADDE n –``
  (also ``GEÇİCİ MADDE n``, ``EK MADDE n``, ``MADDE 61/A``) → fıkra ``(1)`` → bent ``a)`` (Turkish
  alphabet, then ``aa)``, ``bb)`` …) → alt bent ``1)``. Paths: ``m5/f1/a/1``, ``gm1/f2``, ``em3``,
  ``m61A/f1``.
* ``sections`` (rehberler, Borsa İstanbul prosedürleri): ``II.`` → ``2.`` → ``2.1.`` … Paths are the
  dotted numbers, e.g. ``II.2.1`` or ``4.1.3``. A table of contents (dot leaders) is used as an
  oracle for which numbered lines are real headings.

PDF lines are wrapped, so a line starting with ``(3)`` or ``b)`` is a new provision only when its
number is the next one in sequence (and it does not read like a reference: "(1) numaralı …").
Anything else stays continuation text. Doubtful structure is flagged per provision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from papi.core.text import normalize_tr, tr_lower, tr_upper
from papi.sources.legal.amendments import (
    LIST_START,
    Header,
    ListedAmendment,
    Marker,
    find_markers,
    join_wrapped,
    parse_amendment_list,
    parse_header,
)
from papi.sources.legal.extract import Line

PARSER_VERSION = "1"

Shape = Literal["articles", "sections"]

TR_ALPHABET = "abcçdefgğhıijklmnoöprsştuüvyz"
_ORDINALS = (
    "BİRİNCİ|İKİNCİ|ÜÇÜNCÜ|DÖRDÜNCÜ|BEŞİNCİ|ALTINCI|YEDİNCİ|SEKİZİNCİ|DOKUZUNCU|ONUNCU|"
    "ON BİRİNCİ|ON İKİNCİ|ON ÜÇÜNCÜ|ON DÖRDÜNCÜ|ON BEŞİNCİ|ON ALTINCI|ON YEDİNCİ|ON SEKİZİNCİ|"
    "ON DOKUZUNCU|YİRMİNCİ"
)
_KISIM = re.compile(rf"^(?P<ord>{_ORDINALS})\s+KISIM\b\s*(?P<title>.*)$")
_BOLUM = re.compile(rf"^(?P<ord>{_ORDINALS})\s+BÖLÜM\b\s*(?P<title>.*)$")
_MADDE = re.compile(
    r"^(?P<kind>GEÇİCİ\s+MADDE|EK\s+MADDE|MÜKERRER\s+MADDE|MAD{1,3}E|Geçici\s+Madde|Ek\s+Madde|Madde)\s+"
    r"(?P<num>\d{1,3})(?:\s*/\s*(?P<suf>[A-ZÇĞİÖŞÜ]))?\s*[-–—]\s*(?P<rest>.*)$"
)
_FIKRA = re.compile(r"^\((?P<n>\d{1,2})\)\s*(?P<rest>.*)$")
_BENT = re.compile(r"^(?P<l>[a-zçğıöşü]{1,2})\)\s+(?P<rest>\S.*)$")
_ALTBENT = re.compile(r"^(?P<n>\d{1,2})\)\s+(?P<rest>\S.*)$")
_LEADING_MARKERS = re.compile(
    r"^(?:\([^()]*(?:Değişik|değişik|Ek|ek|Mülga|mülga|İptal|iptal)[^()]*:[^()]*\)\s*)+"
)
_REFERENCE_WORDS = re.compile(
    r"^(?:numaralı|sayılı|inci|ıncı|nci|ncı|üncü|uncu|ncü|ncu|bend|bent|fıkra|alt\s+bent|ve\s|veya\s|ile\s|ila\s)",
    re.IGNORECASE,
)
_TOC_LINE = re.compile(r"(?:\.{4,}|…{2,}|(?:\. ){4,})\s*\d*\s*$")
_TOC_GLUED = re.compile(r"[A-ZÇĞİÖŞÜ?]\s?\d{1,3}$")  # "…YAYINLANMASI26": TOC entry without leaders
_FOOTNOTE_RULE = re.compile(r"^_{5,}$")
_ANNEX = re.compile(
    r"^(?:\(?\s*EK\s*[-–]?\s*\d*\s*\)?|EKLER|\(\d+\)\s*SAYILI\s+(?:LİSTE|CETVEL)|.*\bREVİZYONLARI|"
    r"ESKİ METİN\s+YENİ METİN|DEĞİŞİKLİK\s+TABLOSU)\s*$"
)
_ROMAN = re.compile(r"^(?P<r>[IVXL]{1,6})\s*[.\-–]\s*(?P<title>\S.*)$")
_DECIMAL = re.compile(r"^(?P<n>\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+(?P<title>\S.*)$")
_TERMINATORS = ".;:"

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN_VALUES[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def join_paragraphs(parts: list[str]) -> str:
    """Like ``join_wrapped`` but starts a new paragraph after a sentence end or before a list item."""
    paras: list[list[str]] = []
    for p in parts:
        starts_item = bool(
            _BENT.match(p) or _ALTBENT.match(p) or _FIKRA.match(p) or p.startswith(("•", "-", "–"))
        )
        if not paras or starts_item or (paras[-1][-1].rstrip()[-1:] in ".:" and p[:1].isupper()):
            paras.append([p])
        else:
            paras[-1].append(p)
    return "\n".join(join_wrapped(x) for x in paras)


def tr_title(s: str) -> str:
    """Turkish-aware capitalisation of each word ("BİRİNCİ" → "Birinci", "ON İKİNCİ" → "On İkinci")."""
    return " ".join(tr_upper(w[:1]) + tr_lower(w[1:]) for w in s.split())


def bent_index(label: str) -> int | None:
    """Position of a bent label in the Turkish alphabet sequence (a=0 … z=28, aa=29 …)."""
    if len(label) == 1:
        i = TR_ALPHABET.find(label)
        return i if i >= 0 else None
    if len(label) == 2 and label[0] == label[1]:
        i = TR_ALPHABET.find(label[0])
        return len(TR_ALPHABET) + i if i >= 0 else None
    return None


# ---------------------------------------------------------------------------------------------- model


@dataclass
class Segment:
    line: int  # index into the document lines
    text: str


@dataclass(eq=False)
class Node:
    kind: str  # madde | gecici_madde | ek_madde | fikra | bent | altbent | section
    label: str
    segment: str  # own path segment: m5, gm1, f2, a, 1, II.2.1
    heading: str | None = None
    segments: list[Segment] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)
    flags: list[str] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> str:
        if self.kind == "section":
            return self.segment
        parts = []
        node: Node | None = self
        while node is not None and node.kind != "root":
            parts.append(node.segment)
            node = node.parent
        return "/".join(reversed(parts))

    def own_text(self) -> str:
        if self.kind == "section":
            return join_paragraphs([s.text for s in self.segments])
        return join_wrapped([s.text for s in self.segments])

    def full_text(self) -> str:
        blocks = [self.own_text()] if self.segments else []
        blocks += [c.full_text() for c in self.children]
        return "\n".join(b for b in blocks if b)

    def walk(self) -> Iterable[Node]:
        yield self
        for c in self.children:
            yield from c.walk()

    def first_line(self) -> int | None:
        for n in self.walk():
            if n.segments:
                return n.segments[0].line
        return None

    def last_line(self) -> int | None:
        last = None
        for n in self.walk():
            if n.segments:
                last = max(last or 0, n.segments[-1].line)
        return last

    def add(self, child: Node) -> Node:
        child.parent = self
        self.children.append(child)
        return child


@dataclass
class ParsedProvision:
    path: str
    kind: str
    label: str
    heading: str | None
    text: str
    own_text: str
    line_start: int
    line_end: int
    page: int | None
    flags: list[str]
    context: dict[str, str]
    markers: list[Marker]
    ancestors: list[str]  # paths, nearest first
    depth: int

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "label": self.label,
            "heading": self.heading,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "page": self.page,
            "flags": self.flags,
            "context": self.context,
            "markers": [m.to_json() for m in self.markers],
        }


@dataclass
class ParsedDocument:
    shape: Shape
    header: Header
    amendment_list: list[ListedAmendment]
    provisions: list[ParsedProvision]
    outline: list[dict]
    flags: list[str]
    lines: list[Line]
    appendix_lines: list[str]
    footnotes: list[str]
    body_start: int

    def by_path(self) -> dict[str, ParsedProvision]:
        return {p.path: p for p in self.provisions}

    def low_confidence(self) -> list[ParsedProvision]:
        return [p for p in self.provisions if p.flags]


# ------------------------------------------------------------------------------------------- helpers


def _heading_like(text: str) -> bool:
    t = text.strip()
    return (
        0 < len(t) <= 110
        and t[-1] not in _TERMINATORS + ","
        and not _FIKRA.match(t)
        and not _BENT.match(t)
        and not _MADDE.match(t)
        and not t.startswith("(")
    )


def _ends_clause(text: str | None) -> bool:
    if text is None:
        return True
    t = text.rstrip()
    return not t or t[-1] in _TERMINATORS + ",)–-" or t.endswith("(…)")


def _looks_reference(rest: str) -> bool:
    return bool(_REFERENCE_WORDS.match(rest.strip()))


def _strip_toc(lines: list[Line]) -> tuple[list[int], list[str]]:
    """Indexes of body lines (table of contents dropped) and the TOC entries' text without leaders.

    The TOC region runs from the first to the last dot-leader line when it holds ≥ 3 of them, so
    wrapped entries without leaders are dropped too; elsewhere only dot-leader lines are removed.
    """
    leader = [i for i, ln in enumerate(lines) if _TOC_LINE.search(ln.text)]
    region = range(leader[0], leader[-1] + 1) if len(leader) >= 3 else range(0)
    if region and len(region) > max(60, 4 * len(leader)):
        region = range(0)  # leaders scattered through the body (forms), not one contiguous TOC
    keep, toc = [], []
    for i, ln in enumerate(lines):
        if i in region or _TOC_LINE.search(ln.text):
            toc.append(_TOC_LINE.sub("", ln.text).strip(" ."))
        else:
            keep.append(i)
    return keep, toc


def _footnote_lines(lines: list[Line], idx: list[int]) -> set[int]:
    """Footnotes: from a ``_____`` rule to the end of that page (PDF) — not part of provisions."""
    out: set[int] = set()
    active_page: int | None = None
    active = False
    for i in idx:
        ln = lines[i]
        if _FOOTNOTE_RULE.match(ln.text):
            active, active_page = True, ln.page
            out.add(i)
            continue
        if active:
            if ln.page is not None and ln.page == active_page:
                out.add(i)
                continue
            active = False
    return out


def _split_header(lines: list[Line], idx: list[int], is_body_start) -> tuple[list[str], list[str], int]:
    """(header lines, amendment-list lines, position in ``idx`` where the body starts)."""
    header, alist = [], []
    in_list = False
    for pos, i in enumerate(idx):
        t = lines[i].text
        if is_body_start(t):
            return header, alist, pos
        if LIST_START.search(t):
            in_list = True
            rest = LIST_START.split(t, maxsplit=1)[-1].lstrip(" :")
            if rest:
                alist.append(rest)
            continue
        (alist if in_list else header).append(t)
    return header, alist, len(idx)


# ------------------------------------------------------------------------------------------ articles


class _ArticleParser:
    def __init__(self, lines: list[Line]):
        self.lines = lines
        self.root = Node(kind="root", label="", segment="")
        self.flags: list[str] = []
        self.outline: list[dict] = []
        self.context: dict[str, str] = {}
        self.madde: Node | None = None
        self.fikra: Node | None = None
        self.bent: Node | None = None
        self.altbent: Node | None = None
        self.pending: list[Segment] = []  # lines after a KISIM/BÖLÜM line, before the next MADDE
        self.pending_struct: dict | None = None
        self.last_regular: tuple[int, str] = (0, "")
        self.last_special: dict[str, int] = {}
        self.stopped_at: int | None = None

    # current innermost node receiving continuation text
    def _current(self) -> Node | None:
        return self.altbent or self.bent or self.fikra or self.madde

    def _last_text(self) -> str | None:
        node = self._current()
        if node is None or not node.segments:
            return None
        return node.segments[-1].text

    def feed(self, i: int) -> bool:
        """Consume line ``i``; returns False when the body ended (annex/revision table)."""
        t = self.lines[i].text
        if _ANNEX.match(t) and self.madde is not None:
            self.stopped_at = i
            return False
        m = _KISIM.match(t)
        if m:
            self._flush_pending_struct()
            self.pending_struct = {
                "level": "kisim",
                "label": m.group("ord"),
                "title_parts": [m.group("title")],
            }
            self._close_madde()
            return True
        m = _BOLUM.match(t)
        if m:
            self._flush_pending_struct()
            self.pending_struct = {
                "level": "bolum",
                "label": m.group("ord"),
                "title_parts": [m.group("title")],
            }
            self._close_madde()
            return True
        m = _MADDE.match(t)
        if m and self._accept_madde(m):
            self._start_madde(i, m)
            return True
        if self.pending_struct is not None or self.madde is None:
            self.pending.append(Segment(i, t))
            return True
        self._body_line(i, t)
        return True

    # ---- structure (KISIM/BÖLÜM) titles and article headings
    def _flush_pending_struct(self, heading_lines: int = 0) -> str | None:
        """Attach pending lines: last ``heading_lines`` are the next article's heading."""
        heading = None
        pend = self.pending
        if heading_lines and pend:
            heading = join_wrapped([s.text for s in pend[-heading_lines:]])
            pend = pend[:-heading_lines]
        if self.pending_struct is not None:
            ps = self.pending_struct
            title = join_wrapped([p for p in ps["title_parts"] if p] + [s.text for s in pend])
            self.context = dict(self.context)
            if ps["level"] == "kisim":
                self.context = {"kisim": f"{tr_title(ps['label'])} Kısım" + (f" – {title}" if title else "")}
            else:
                self.context.pop("bolum", None)
                self.context["bolum"] = f"{tr_title(ps['label'])} Bölüm" + (f" – {title}" if title else "")
            self.outline.append({"level": ps["level"], "label": ps["label"], "title": title or None})
        elif pend and self.madde is None:
            pass  # preamble before the first structure line: ignored (header parsed separately)
        self.pending_struct = None
        self.pending = []
        return heading

    def _close_madde(self) -> None:
        self.madde = self.fikra = self.bent = self.altbent = None

    def _accept_madde(self, m: re.Match) -> bool:
        kind = tr_upper(m.group("kind").split()[0])
        num = int(m.group("num"))
        suf = m.group("suf") or ""
        if kind in ("GEÇİCİ", "EK", "MÜKERRER"):
            last = self.last_special.get(kind, 0)
            if num <= last and self.madde is not None:
                self.flags.append(f"{kind.lower()}_madde_order:{num}")
            return True
        last_num, last_suf = self.last_regular
        if num > last_num or (num == last_num and suf > last_suf):
            return True
        if last_num >= 3:
            # numbering restarts → a revision table/annex quoting articles; the body has ended
            self.stopped_at = -1
            return False
        return True

    def _start_madde(self, i: int, m: re.Match) -> None:
        kind_word = tr_upper(m.group("kind").split()[0])
        num = int(m.group("num"))
        suf = m.group("suf") or ""
        heading = self._take_heading()
        if kind_word == "GEÇİCİ":
            kind, seg, label = (
                "gecici_madde",
                f"gm{num}{suf}",
                f"Geçici Madde {num}{'/' + suf if suf else ''}",
            )
        elif kind_word == "EK":
            kind, seg, label = "ek_madde", f"em{num}{suf}", f"Ek Madde {num}{'/' + suf if suf else ''}"
        elif kind_word == "MÜKERRER":
            kind, seg, label = "madde", f"mk{num}{suf}", f"Mükerrer Madde {num}"
        else:
            kind, seg, label = "madde", f"m{num}{suf}", f"Madde {num}{'/' + suf if suf else ''}"
        if kind_word in ("GEÇİCİ", "EK", "MÜKERRER"):
            self.last_special[kind_word] = num
        else:
            last_num = self.last_regular[0]
            if last_num and num > last_num + 1:
                gap_flag = f"madde_gap:{last_num}->{num}"
                self.flags.append(gap_flag)
            self.last_regular = (num, suf)
        existing = {c.segment for c in self.root.children}
        if seg in existing:
            seg = f"{seg}_dup{sum(1 for c in self.root.children if c.segment.startswith(seg))}"
        node = self.root.add(
            Node(kind=kind, label=label, segment=seg, heading=heading, context=dict(self.context))
        )
        if seg.find("_dup") >= 0:
            node.flags.append("duplicate_article_number")
        self.madde, self.fikra, self.bent, self.altbent = node, None, None, None
        rest = m.group("rest").strip()
        line = self.lines[i].text
        head = line[: len(line) - len(rest)].rstrip() if rest else line.rstrip()
        lead = _LEADING_MARKERS.match(rest)
        markers_part = lead.group(0).strip() if lead else ""
        remainder = rest[lead.end() :] if lead else rest
        node.segments.append(Segment(i, (head + (" " + markers_part if markers_part else "")).strip()))
        fm = _FIKRA.match(remainder)
        if fm and int(fm.group("n")) == 1:
            self._start_fikra(i, 1, remainder)
        elif remainder:
            node.segments.append(Segment(i, remainder))

    def _take_heading(self) -> str | None:
        """Pop the article heading from the lines preceding a MADDE line."""
        if self.pending_struct is not None or (self.madde is None and self.pending):
            # lines after a BÖLÜM/KISIM title: the last line(s) are the heading when ≥2 lines are pending;
            # a heading wrapped onto lowercase continuation lines takes those along
            pend = self.pending
            n = 1 if len(pend) >= 2 or (self.pending_struct is None and pend) else 0
            while n and n < 3 and len(pend) > n + 1 and pend[-n].text[:1].islower():
                n += 1
            if n and not all(_heading_like(s.text) for s in pend[-n:]):
                n = 0
            return self._flush_pending_struct(heading_lines=n)
        node = self._current()
        if node is None or not node.segments:
            return None
        # walk back from the tail of the previous article's innermost node
        cand: list[Segment] = []
        segs = node.segments
        k = len(segs) - 1
        while k >= 1 and len(cand) < 3:  # never take the node's first (numbered) segment
            s = segs[k]
            if not _heading_like(s.text):
                break
            cand.insert(0, s)
            k -= 1
            before = segs[k].text
            if cand[0].text[:1].isupper() and _ends_clause(before):
                del segs[k + 1 :]
                return join_wrapped([c.text for c in cand])
        return None

    # ---- body lines inside an article
    def _body_line(self, i: int, t: str) -> None:
        prev = self._last_text()
        fm = _FIKRA.match(t)
        if fm and self._accept_fikra(int(fm.group("n")), fm.group("rest"), prev):
            self._start_fikra(i, int(fm.group("n")), t)
            return
        bm = _BENT.match(t)
        if bm and self._accept_bent(bm.group("l"), bm.group("rest"), prev):
            self._start_bent(i, bm.group("l"), t)
            return
        am = _ALTBENT.match(t)
        if am and self._accept_altbent(int(am.group("n")), am.group("rest"), prev):
            self._start_altbent(i, int(am.group("n")), t)
            return
        node = self._current()
        assert node is not None
        node.segments.append(Segment(i, t))

    def _fikra_numbers(self) -> list[int]:
        assert self.madde is not None
        return [int(c.label) for c in self.madde.children if c.kind == "fikra"]

    def _accept_fikra(self, n: int, rest: str, prev: str | None) -> bool:
        if self.madde is None or _looks_reference(rest):
            return False
        nums = self._fikra_numbers()
        expected = (nums[-1] + 1) if nums else 1
        if n == expected:
            return True
        if not nums and n == 2 and _ends_clause(prev) and len(self.madde.segments) > 1:
            return True  # source omitted "(1)" (e.g. after an amendment marker): implicit first fıkra
        # one fıkra missing (repealed and dropped from the text?) — flagged on start
        return bool(nums) and n == expected + 1 and prev is not None and prev.rstrip()[-1:] in ".:"

    def _start_fikra(self, i: int, n: int, text: str) -> None:
        assert self.madde is not None
        nums = self._fikra_numbers()
        if not nums and n == 2:
            implicit = Node(kind="fikra", label="1", segment="f1", context=self.madde.context)
            implicit.flags.append("implicit_fikra_1")
            implicit.segments = self.madde.segments[1:]
            self.madde.segments = self.madde.segments[:1]
            for child in self.madde.children:
                implicit.add(child)
            self.madde.children = []
            self.madde.add(implicit)
            nums = [1]
        node = self.madde.add(Node(kind="fikra", label=str(n), segment=f"f{n}", context=self.madde.context))
        if nums and n != nums[-1] + 1:
            node.flags.append(f"fikra_gap:{nums[-1]}->{n}")
        node.segments.append(Segment(i, text))
        self.fikra, self.bent, self.altbent = node, None, None

    def _bent_parent(self) -> Node | None:
        return self.fikra or self.madde

    def _accept_bent(self, label: str, rest: str, prev: str | None) -> bool:
        parent = self._bent_parent()
        idx = bent_index(label)
        if parent is None or idx is None or _looks_reference(rest):
            return False
        letters = [c for c in parent.children if c.kind == "bent" and not c.label.isdigit()]
        if not letters:
            return idx == 0 and _ends_clause(prev)
        last = bent_index(letters[-1].label)
        assert last is not None
        if idx == last + 1:
            return True
        return idx == last + 2 and _ends_clause(prev)

    def _start_bent(self, i: int, label: str, text: str) -> None:
        parent = self._bent_parent()
        assert parent is not None
        letters = [c for c in parent.children if c.kind == "bent" and not c.label.isdigit()]
        node = parent.add(Node(kind="bent", label=label, segment=label, context=parent.context))
        if letters:
            last = bent_index(letters[-1].label)
            if last is not None and bent_index(label) != last + 1:
                node.flags.append(f"bent_gap:{letters[-1].label}->{label}")
        if parent.kind != "fikra" and parent.children and any(c.kind == "fikra" for c in parent.children):
            node.flags.append("bent_outside_fikra")
        node.segments.append(Segment(i, text))
        self.bent, self.altbent = node, None

    def _accept_altbent(self, n: int, rest: str, prev: str | None) -> bool:
        if _looks_reference(rest):
            return False
        if self.bent is not None:
            nums = [int(c.label) for c in self.bent.children]
            expected = nums[-1] + 1 if nums else 1
            return n == expected and (_ends_clause(prev) or bool(nums))
        parent = self._bent_parent()
        if parent is None:
            return False
        # numbered items directly under a fıkra (no lettered bents): treated as bents "1", "2", …
        if any(c.kind == "bent" and not c.label.isdigit() for c in parent.children):
            return False
        nums = [int(c.label) for c in parent.children if c.kind == "bent"]
        expected = nums[-1] + 1 if nums else 1
        return n == expected and _ends_clause(prev)

    def _start_altbent(self, i: int, n: int, text: str) -> None:
        if self.bent is not None:
            node = self.bent.add(
                Node(kind="altbent", label=str(n), segment=str(n), context=self.bent.context)
            )
            node.segments.append(Segment(i, text))
            self.altbent = node
            return
        parent = self._bent_parent()
        assert parent is not None
        node = parent.add(Node(kind="bent", label=str(n), segment=str(n), context=parent.context))
        node.flags.append("numbered_items_as_bent")
        node.segments.append(Segment(i, text))
        self.bent, self.altbent = node, None

    def finish(self) -> None:
        if self.pending_struct is not None:
            self._flush_pending_struct()


def _is_article_body_start(t: str) -> bool:
    return bool(_KISIM.match(t) or _BOLUM.match(t) or _MADDE.match(t))


def _finalise_flags(root: Node, shape: Shape) -> None:
    for node in root.walk():
        if node is root:
            continue
        own = node.own_text()
        body = _LEADING_MARKERS.sub(
            "", re.sub(r"^(?:MADDE|GEÇİCİ MADDE|EK MADDE|Madde)\s+\S+\s*[-–—]\s*", "", own)
        )
        body = re.sub(r"^(?:\(\d+\)|[a-zçğıöşü]{1,2}\)|\d+\))\s*", "", body).strip()
        has_children = bool(node.children)
        if not body and not has_children and "mülga" not in own.lower() and "mulga" not in own.lower():
            node.flags.append("empty_text")
        is_article = node.kind in ("madde", "gecici_madde", "ek_madde")
        if shape == "articles" and is_article and not has_children and len(own) > 1500:
            node.flags.append("long_unstructured_article")
        if shape == "sections" and len(node.own_text()) > 12000:
            node.flags.append("long_section")


def _provisions(root: Node, lines: list[Line]) -> list[ParsedProvision]:
    out: list[ParsedProvision] = []
    for node in root.walk():
        if node is root:
            continue
        first, last = node.first_line(), node.last_line()
        if first is None or last is None:
            continue
        ancestors = []
        depth = 0
        p = node.parent
        while p is not None and p is not root:
            ancestors.append(p.path)
            depth += 1
            p = p.parent
        own = node.own_text()
        out.append(
            ParsedProvision(
                path=node.path,
                kind=node.kind,
                label=node.label,
                heading=node.heading if node.kind not in ("fikra", "bent", "altbent") else None,
                text=node.full_text(),
                own_text=own,
                line_start=first,
                line_end=last,
                page=lines[first].page,
                flags=list(node.flags),
                context=dict(node.context),
                markers=find_markers(own),
                ancestors=ancestors,
                depth=depth,
            )
        )
    _mark_leading(out)
    return out


def _mark_leading(provs: list[ParsedProvision]) -> None:
    """A marker before a provision's own text (right after its number) applies to the whole provision."""
    for p in provs:
        own = p.own_text
        stripped = re.sub(
            r"^(?:(?:GEÇİCİ\s+MADDE|EK\s+MADDE|MADDE|Geçici\s+Madde|Ek\s+Madde|Madde)\s+\d+(?:\s*/\s*\S)?\s*[-–—]|"
            r"\(\d+\)|[a-zçğıöşü]{1,2}\)|\d+\))\s*",
            "",
            own,
            count=1,
        )
        offset = len(own) - len(stripped)
        lead = _LEADING_MARKERS.match(stripped)
        if not lead:
            continue
        end = offset + lead.end()
        for mk in p.markers:
            if mk.start < end:
                mk.leading = True


def parse_articles(lines: list[Line]) -> ParsedDocument:
    idx, _toc = _strip_toc(lines)
    foot = _footnote_lines(lines, idx)
    idx = [i for i in idx if i not in foot]
    header_lines, list_lines, body_pos = _split_header(lines, idx, _is_article_body_start)
    header = parse_header(header_lines)
    law_no = None
    for ln in header_lines[:6]:
        m = re.search(r"Kanun\s+No\s*:?\s*(\d{4})", ln) or re.match(r"^(\d{4})\s+SAYILI\b", ln)
        if m:
            law_no = m.group(1)
            break
    listed = parse_amendment_list(list_lines, own_law_no=law_no)
    parser = _ArticleParser(lines)
    appendix: list[str] = []
    for pos in range(body_pos, len(idx)):
        i = idx[pos]
        if not parser.feed(i):
            appendix = [lines[j].text for j in idx[pos:]]
            break
        if parser.stopped_at == -1:
            appendix = [lines[j].text for j in idx[pos:]]
            break
    parser.finish()
    flags = list(parser.flags)
    articles = [c for c in parser.root.children]
    if not articles:
        flags.append("no_articles_found")
    _finalise_flags(parser.root, "articles")
    provs = _provisions(parser.root, lines)
    return ParsedDocument(
        shape="articles",
        header=header,
        amendment_list=listed,
        provisions=provs,
        outline=parser.outline,
        flags=flags,
        lines=lines,
        appendix_lines=appendix,
        footnotes=[lines[i].text for i in sorted(foot)],
        body_start=idx[body_pos] if body_pos < len(idx) else len(lines),
    )


# ------------------------------------------------------------------------------------------ sections


def _upper_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _section_key(roman: str | None, nums: tuple[int, ...]) -> str:
    base = ".".join(str(n) for n in nums)
    if roman:
        return roman + ("." + base if base else "")
    return base


def _toc_keys(toc: list[str]) -> dict[tuple[str | None, tuple[int, ...]], str]:
    """TOC entries keyed by (roman part, numbers) → normalised title prefix."""
    keys: dict[tuple[str | None, tuple[int, ...]], str] = {}
    roman: str | None = None
    for entry in toc:
        m = _ROMAN.match(entry)
        if m and _upper_ratio(m.group("title")) > 0.6:
            roman = m.group("r")
            keys[(roman, ())] = _title_key(m.group("title"))
            continue
        m = _DECIMAL.match(entry)
        if m:
            nums = tuple(int(x) for x in m.group("n").split("."))
            keys.setdefault((roman, nums), _title_key(m.group("title")))
    return keys


def _title_key(title: str) -> str:
    return normalize_tr(_TOC_GLUED.sub("", title))[:20]


def _sane_title(title: str) -> bool:
    return bool(title) and title[-1:] not in ",;" and len(title) <= 160 and title[:1].isupper()


_LETTER_PART = re.compile(r"^(?P<r>[A-ZÇĞİÖŞÜ])\s*[-–.)]\s+(?P<title>\S.*)$")
_ITEM = re.compile(r"^(?P<n>\d{1,2})\)\s+(?P<title>\S.*)$")
_TR_UPPER = "ABCÇDEFGĞHIİJKLMNOÖPRSŞTUÜVYZ"


def _part_index(label: str) -> int:
    if re.fullmatch(r"[IVXL]+", label):
        return roman_to_int(label)
    return _TR_UPPER.find(label) + 1


def parse_sections(lines: list[Line]) -> ParsedDocument:
    idx, toc = _strip_toc(lines)
    foot = _footnote_lines(lines, idx)
    idx = [i for i in idx if i not in foot]
    toc_keys = _toc_keys(toc)
    root, flags, outline, header_lines, appendix, seen = _section_pass(lines, idx, toc_keys)
    if not root.children:
        root, header_lines, appendix = _items_pass(lines, idx)
        if root.children:
            flags.append("numbered_items_as_sections")
    missing = sorted(_section_key(r, n) for r, n in set(toc_keys) - seen)
    if missing:
        flags.append("toc_headings_not_found:" + ",".join(missing[:20]))
    if not root.children:
        flags.append("no_sections_found")
    _finalise_flags(root, "sections")
    provs = _provisions(root, lines)
    header = parse_header(header_lines)
    return ParsedDocument(
        shape="sections",
        header=header,
        amendment_list=[],
        provisions=provs,
        outline=outline,
        flags=flags,
        lines=lines,
        appendix_lines=appendix,
        footnotes=[lines[i].text for i in sorted(foot)],
        body_start=0,
    )


def _section_pass(lines: list[Line], idx: list[int], toc_keys: dict) -> tuple:
    """Headings: parts (``II.`` roman or ``A-`` letter) → ``1.`` → ``1.1.`` …"""
    root = Node(kind="root", label="", segment="")
    flags: list[str] = []
    part: str | None = None
    part_style: str | None = None  # roman | letter (first one seen wins)
    part_node: Node | None = None
    stack: list[tuple[tuple[int, ...], Node]] = []  # numeric headings within the current part
    current: Node | None = None
    header_lines: list[str] = []
    seen: set[tuple[str | None, tuple[int, ...]]] = set()
    outline: list[dict] = []
    appendix: list[str] = []
    last_text: str | None = None

    def accept_decimal(nums: tuple[int, ...], title: str) -> bool:
        if (part, nums) in seen or _TOC_GLUED.search(title) or not _sane_title(title):
            return False
        part_keys = {n for r, n in toc_keys if r == part and n}
        prev = stack[-1][0] if stack else ()
        if part_keys:
            if nums not in part_keys or nums <= prev:
                return False
            a, b = _title_key(title), toc_keys[(part, nums)]
            n = min(len(a), len(b))
            return n >= 4 and a[:n] == b[:n]
        if len(nums) == 1 and _upper_ratio(title) < 0.7:
            # numbered paragraphs (no TOC): strictly sequential, each starting a new sentence
            expected = (prev[0] + 1) if prev else 1
            after_heading = (
                current is not None and current.segments and current.segments[-1].text == last_text
            )
            return nums[0] == expected and (_ends_clause(last_text) or bool(after_heading and not prev))
        if not prev:
            return len(nums) == 1
        if nums == (*prev, 1):
            return True
        k = len(nums) - 1
        return k < len(prev) and nums[:k] == prev[:k] and nums[k] == prev[k] + 1

    def match_part(t: str) -> re.Match | None:
        for style, rx in (("roman", _ROMAN), ("letter", _LETTER_PART)):
            if part_style not in (None, style):
                continue
            m = rx.match(t)
            if m and _upper_ratio(m.group("title")) > 0.7 and not _TOC_GLUED.search(m.group("title")):
                idx_new = _part_index(m.group("r"))
                current_idx = _part_index(part) if part else 0
                in_toc = (m.group("r"), ()) in toc_keys
                sequential = idx_new == current_idx + 1
                if (sequential or (in_toc and idx_new > current_idx)) and (m.group("r"), ()) not in seen:
                    return m
        return None

    open_heading: Node | None = None  # an upper-case heading may wrap onto the next line(s)
    for pos, i in enumerate(idx):
        t = lines[i].text
        if open_heading is not None:
            if _upper_ratio(t) > 0.85 and len(t) <= 100 and not _DECIMAL.match(t) and not _ROMAN.match(t):
                open_heading.heading = f"{open_heading.heading} {t}".strip()
                open_heading.segments.append(Segment(i, t))
                last_text = t
                continue
            open_heading = None
        if _ANNEX.match(t) and current is not None and ("REVİZYON" in t or "ESKİ METİN" in t):
            appendix = [lines[j].text for j in idx[pos:]]
            break
        pm = match_part(t)
        if pm:
            part = pm.group("r")
            part_style = "roman" if re.fullmatch(r"[IVXL]+", part) and _ROMAN.match(t) else "letter"
            seen.add((part, ()))
            title = pm.group("title").strip()
            part_node = root.add(Node(kind="section", label=part, segment=part, heading=title))
            part_node.segments.append(Segment(i, t))
            outline.append({"level": "part", "label": part, "title": title})
            stack = []
            current = open_heading = part_node
            last_text = t
            continue
        dm = _DECIMAL.match(t)
        if dm:
            nums = tuple(int(x) for x in dm.group("n").split("."))
            title = dm.group("title").strip()
            if accept_decimal(nums, title):
                seen.add((part, nums))
                while stack and len(stack[-1][0]) >= len(nums):
                    stack.pop()
                parent = stack[-1][1] if stack else (part_node or root)
                key = _section_key(part, nums)
                node = parent.add(Node(kind="section", label=key, segment=key, heading=title[:160]))
                node.segments.append(Segment(i, t))
                stack.append((nums, node))
                current = node
                open_heading = node if _upper_ratio(title) > 0.85 else None
                last_text = t
                continue
        if current is None:
            header_lines.append(t)
        else:
            current.segments.append(Segment(i, t))
        last_text = t
    return root, flags, outline, header_lines, appendix, seen


def _items_pass(lines: list[Line], idx: list[int]) -> tuple[Node, list[str], list[str]]:
    """Fallback for guidelines numbered only as ``1)``, ``2)`` …: each item is a section; the text
    before the first item becomes section ``giris``."""
    root = Node(kind="root", label="", segment="")
    intro: list[Segment] = []
    current: Node | None = None
    last = 0
    prev_text: str | None = None
    for i in idx:
        t = lines[i].text
        m = _ITEM.match(t)
        if (
            m
            and int(m.group("n")) == last + 1
            and _ends_clause(prev_text)
            and not _looks_reference(m.group("title"))
        ):
            last += 1
            current = root.add(Node(kind="section", label=str(last), segment=str(last), heading=None))
            current.segments.append(Segment(i, t))
        elif current is None:
            intro.append(Segment(i, t))
        else:
            current.segments.append(Segment(i, t))
        prev_text = t
    if not root.children:
        return root, [s.text for s in intro], []
    header = [s.text for s in intro[:3]]
    body = intro[3:] if len(intro) > 3 else []
    if body and sum(len(s.text) for s in body) > 200:
        giris = Node(kind="section", label="giris", segment="giris", heading="Giriş")
        giris.segments = body
        giris.parent = root
        root.children.insert(0, giris)
    return root, header, []


def parse(lines: list[Line], shape: Shape) -> ParsedDocument:
    return parse_articles(lines) if shape == "articles" else parse_sections(lines)
