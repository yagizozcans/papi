"""Amendment metadata of Turkish consolidated texts: header, amendment list, inline markers, dates.

Consolidated SPK texts carry three kinds of dating evidence (03 §3.2):

* the header: ``(23/1/2014 tarih ve 28891 sayılı Resmi Gazete’de yayımlanmıştır.)``;
* the amendment list (``Tebliğ değişikliklerine ilişkin liste:`` / ``Kanun değişikliğine ilişkin
  liste:``), one numbered entry per amending act with its Resmî Gazete date and number;
* inline markers in the text: ``(Değişik:RG-10/2/2017-29975)``, ``(Ek fıkra: 17.7.2019 tarihli ve
  7186 sayılı Kanun’un 27 nci maddesi ile)``, ``(Mülga cümle:27/12/2018-7159/8 md.)``.

Markers naming a law or KHK are dated through the amendment list (Resmî Gazete publication date of
that act). Borsa İstanbul documents instead append a revision table (``dd/mm/yyyy tarihinde belgede
aşağıdaki değişiklikler yapılmıştır``) naming the changed articles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

_DATE = r"(\d{1,2})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{4})"
_RG_NAME = r"Resm[iîİ]\s*Gazete"
_GAZETTE = re.compile(
    _DATE
    + r"\s*tarih(?:li)?\s*(?:ve|,)?\s*(\d{4,5})\s*say[ıi]l[ıi]\s*(?:(\d)\s*\.?\s*)?(?:m[üu]kerrer\s*)?"
    + _RG_NAME,
    re.IGNORECASE,
)
_RG_INLINE = re.compile(r"RG\s*[-‐–]?\s*" + _DATE + r"\s*[-‐–]\s*(\d{4,5})", re.IGNORECASE)
_LAW_INLINE = re.compile(
    r"(?:" + _DATE + r"\s*(?:tarihli\s*ve\s*|[-–]\s*))?(\d{4})\s*(?:(?:say[ıi]l[ıi]\s*)+Kanun|/\s*\d+\s*md)"
)
_COURT_DECISION = re.compile(r"\bK\s*\.?\s*:?\s*(\d{4}\s*/\s*\d+)")
_KHK = re.compile(
    r"(\d{3})\s*(?:/\s*KHK|say[ıi]l[ıi]\s*KHK|say[ıi]l[ıi]\s*Kanun\s*H[üu]km[üu]nde)", re.IGNORECASE
)
_TEBLIG_AMENDING = re.compile(r"\(([IVX]+\s*[-.]\s*\d+(?:\.\d+)*\.[a-zçğıöşü]{1,2})\)")
_LAW_IN_LIST = re.compile(r"(?<!\d)(\d{4})\s*say[ıi]l[ıi]")
_ADOPTED = re.compile(_DATE + r"\s*tarihli\s*(?:ve\s*)?(\d{4})\s*say[ıi]l[ıi]")

LIST_START = re.compile(r"de[gğ]i[sş]ikli\w*\s+ili[sş]kin\s+liste", re.IGNORECASE)
_LIST_ENTRY = re.compile(r"^(\d{1,3})\s*[-–.)]\s+(.*)$")

# "(Değişik:RG-…)", "(Başlığı ile birlikte değişik:RG-…)", "(Ek fıkra: … ile)", "(Mülga cümle:…)"
MARKER = re.compile(
    r"\((?P<pre>[^():]{0,40}?)(?<![A-Za-zÇĞİÖŞÜçğıöşü])(?P<kind>Değişik|değişik|Ek|ek|Mülga|mülga|İptal|iptal)"
    r"(?P<what>[^():]{0,40}):\s*(?P<body>[^()]{2,260}?)\)"
)

_MONTH_WORDS = {
    "bir": 1,
    "iki": 2,
    "üç": 3,
    "dört": 4,
    "beş": 5,
    "altı": 6,
    "yedi": 7,
    "sekiz": 8,
    "dokuz": 9,
    "on": 10,
    "onbir": 11,
    "on iki": 12,
    "oniki": 12,
}
_YURURLUK_SAME_DAY = re.compile(
    r"yay[ıi]m(?:[ıi]|land[ıi][gğ][ıi])?\s+tarih(?:in)?de\s+y[üu]r[üu]rl[üu][gğ]e\s+girer", re.I
)
_YURURLUK_AFTER = re.compile(
    r"yay[ıi]m[ıi]?\s+tarihinden\s+(?:itibaren\s+)?(\d+|[a-zçğıöşü ]+?)\s+(ay|g[üu]n|y[ıi]l)\s+sonra\s+"
    r"y[üu]r[üu]rl[üu][gğ]e\s+girer",
    re.I,
)
_YURURLUK_ON = re.compile(
    r"\b(?:Bu\s+\w+)\s*,?\s*" + _DATE + r"\s+tarihinde\s+y[üu]r[üu]rl[üu][gğ]e\s+girer", re.I
)
_ARTICLE_REF = re.compile(
    r"(\d+)\s*(?:/[A-Z])?\s*['’]?\s*(?:inci|ıncı|nci|ncı|üncü|uncu|ncü|ncu|'?[iıuü]nc[iıuü])\s+madde", re.I
)

_REVISION_BLOCK = re.compile(
    _DATE + r"\s+tarihinde\s+belgede\s+a[sş]a[gğ][ıi]daki\s+de[gğ]i[sş]iklikler", re.I
)
_REVISION_ARTICLE = re.compile(r"\b(GEÇİCİ\s+MADDE|EK\s+MADDE|MADDE)\s+(\d+)(?:\s*/\s*([A-ZÇĞİÖŞÜ]))?")


def make_date(d: str, m: str, y: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def fmt_date(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


@dataclass(frozen=True, slots=True)
class GazetteRef:
    date: date
    number: str | None = None

    def label(self) -> str:
        return f"RG {fmt_date(self.date)}" + (f"-{self.number}" if self.number else "")


@dataclass
class ListedAmendment:
    """One entry of a consolidated text's amendment list."""

    ordinal: int
    text: str
    gazette: GazetteRef | None = None
    teblig_code: str | None = None
    law_no: str | None = None
    khk_no: str | None = None
    adopted: date | None = None
    court_decision: bool = False
    decision_no: str | None = None  # Anayasa Mahkemesi "K.2013/133"

    def label(self) -> str:
        who = self.teblig_code or (f"{self.law_no} sayılı Kanun" if self.law_no else None)
        if who is None and self.khk_no:
            who = f"{self.khk_no} sayılı KHK"
        if who is None and self.court_decision:
            who = "Anayasa Mahkemesi kararı"
        parts = [p for p in (who, self.gazette.label() if self.gazette else None) if p]
        return " – ".join(parts) if parts else self.text[:80]

    def to_json(self) -> dict:
        return {
            "ordinal": self.ordinal,
            "text": self.text,
            "gazette_date": self.gazette.date.isoformat() if self.gazette else None,
            "gazette_number": self.gazette.number if self.gazette else None,
            "teblig_code": self.teblig_code,
            "law_no": self.law_no,
            "khk_no": self.khk_no,
            "adopted": self.adopted.isoformat() if self.adopted else None,
            "court_decision": self.court_decision,
        }


@dataclass
class Header:
    title: str | None = None
    official_code: str | None = None
    gazette: GazetteRef | None = None

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "official_code": self.official_code,
            "gazette_date": self.gazette.date.isoformat() if self.gazette else None,
            "gazette_number": self.gazette.number if self.gazette else None,
        }


@dataclass
class Marker:
    """An inline amendment marker found in a provision's own text."""

    kind: str  # degisik | ek | mulga | iptal
    what: str  # fıkra / bent / madde / ibare / cümle / başlık … (free text, normalised lower)
    raw: str
    start: int
    end: int
    gazette: GazetteRef | None = None
    law_no: str | None = None
    khk_no: str | None = None
    mention_date: date | None = None
    decision_no: str | None = None
    leading: bool = False  # marker precedes the provision's text → applies to the whole provision

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "what": self.what,
            "raw": self.raw,
            "gazette_date": self.gazette.date.isoformat() if self.gazette else None,
            "law_no": self.law_no,
            "khk_no": self.khk_no,
            "leading": self.leading,
        }


@dataclass
class Resolution:
    effective_from: date | None
    amended_by: str | None
    unresolved: list[str] = field(default_factory=list)


def gazette_in(text: str) -> GazetteRef | None:
    """Last Resmî Gazete date+number mentioned in ``text`` (the publication of the amending act)."""
    found = None
    for m in _GAZETTE.finditer(text):
        d = make_date(m.group(1), m.group(2), m.group(3))
        if d:
            found = GazetteRef(d, m.group(4))
    return found


def parse_header(lines: list[str]) -> Header:
    """Title, official code and publication gazette from the lines before the body starts."""
    h = Header()
    title_parts: list[str] = []
    for ln in lines:
        if h.gazette is None:
            g = gazette_in(ln)
            if g:
                h.gazette = g
                continue
        if h.official_code is None:
            m = re.fullmatch(r"\(?\s*([IVX]+\s*[-.]\s*\d+(?:\.\d+)*)\s*\)?(?:\(\*+\))?", ln)
            if m:
                h.official_code = re.sub(r"\s+", "", m.group(1))
                continue
        if LIST_START.search(ln) or ln.strip() in ("İÇİNDEKİLER", "İÇİNDEKİLER TABLOSU"):
            break
        if not h.gazette and len(title_parts) < 4 and ln.upper() == ln and len(ln) < 120:
            title_parts.append(ln)
    if title_parts:
        h.title = " ".join(title_parts)
    return h


def parse_amendment_list(lines: list[str], own_law_no: str | None = None) -> list[ListedAmendment]:
    """Entries of the "… değişikliklerine ilişkin liste" block (``lines`` = the block only)."""
    entries: list[tuple[int, list[str]]] = []
    for ln in lines:
        m = _LIST_ENTRY.match(ln)
        if m and (not entries or int(m.group(1)) == entries[-1][0] + 1):
            entries.append((int(m.group(1)), [m.group(2)]))
        elif entries:
            entries[-1][1].append(ln)
    out = []
    for ordinal, parts in entries:
        text = join_wrapped(parts)
        item = ListedAmendment(ordinal=ordinal, text=text, gazette=gazette_in(text))
        tm = _TEBLIG_AMENDING.search(text)
        if tm:
            item.teblig_code = re.sub(r"\s+", "", tm.group(1))
        km = _KHK.search(text)
        if km:
            item.khk_no = km.group(1)
        rg_no = item.gazette.number if item.gazette else None
        for lm in _LAW_IN_LIST.finditer(text):
            if lm.group(1) not in (own_law_no, rg_no) and lm.group(1) != item.khk_no:
                item.law_no = lm.group(1)
                break
        am = _ADOPTED.search(text)
        if am and am.group(4) == item.law_no:
            item.adopted = make_date(am.group(1), am.group(2), am.group(3))
        item.court_decision = "anayasa mahkemesi" in text.lower()
        dm = _COURT_DECISION.search(text)
        if dm:
            item.decision_no = re.sub(r"\s+", "", dm.group(1))
        out.append(item)
    return out


def join_wrapped(parts: list[str]) -> str:
    """Join wrapped lines: a trailing ``-`` glued to a letter/digit joins without a space."""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not out:
            out = p
        elif re.search(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]-$", out) and re.match(r"[0-9a-zçğıöşü]", p):
            out += p
        else:
            out += " " + p
    return out


def _kind_key(kind: str) -> str:
    k = kind.lower().replace("ğ", "g").replace("ş", "s").replace("ü", "u").replace("i̇", "i")
    return {"degisik": "degisik", "ek": "ek", "mulga": "mulga", "iptal": "iptal"}.get(k, k)


def find_markers(text: str) -> list[Marker]:
    """Inline amendment markers in ``text`` with their own gazette/law references (unresolved)."""
    out = []
    for m in MARKER.finditer(text):
        body = m.group("body")
        mk = Marker(
            kind=_kind_key(m.group("kind")),
            what=(m.group("pre") + " " + m.group("what")).strip().lower(),
            raw=m.group(0),
            start=m.start(),
            end=m.end(),
        )
        rg = _RG_INLINE.search(body)
        if rg:
            d = make_date(rg.group(1), rg.group(2), rg.group(3))
            if d:
                mk.gazette = GazetteRef(d, rg.group(4))
        km = _KHK.search(body)
        if km:
            mk.khk_no = km.group(1)
        dm = _COURT_DECISION.search(body)
        if dm:
            mk.decision_no = re.sub(r"\s+", "", dm.group(1))
        lm = _LAW_INLINE.search(body)
        if lm and not mk.gazette:
            mk.law_no = lm.group(4)
            if lm.group(1):
                mk.mention_date = make_date(lm.group(1), lm.group(2), lm.group(3))
        out.append(mk)
    return out


def resolve_marker(mk: Marker, listed: list[ListedAmendment]) -> tuple[date | None, str | None]:
    """Effective date (Resmî Gazete publication of the amending act) and a label, or (None, raw)."""
    if mk.gazette:
        for item in listed:
            if item.gazette and item.gazette.date == mk.gazette.date:
                return mk.gazette.date, item.label()
        return mk.gazette.date, mk.gazette.label()
    if mk.law_no:
        for item in listed:
            if item.law_no == mk.law_no and item.gazette:
                return item.gazette.date, item.label()
    if mk.khk_no:
        for item in listed:
            if item.khk_no == mk.khk_no and item.gazette:
                return item.gazette.date, item.label()
    if mk.decision_no:
        for item in listed:
            if item.decision_no == mk.decision_no and item.gazette:
                return item.gazette.date, item.label()
    if mk.mention_date:
        for item in listed:
            if item.gazette and mk.mention_date in (item.gazette.date, item.adopted):
                return item.gazette.date, item.label()
    return None, mk.raw


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def base_effective(
    yururluk_text: str | None, gazette: GazetteRef | None
) -> tuple[date | None, set[str], str]:
    """Entry-into-force date of the original text from its "Yürürlük" article.

    Returns ``(date | None, article numbers with their own dates (excluded), basis)``. Only plain
    forms are trusted: "yayımı tarihinde", "yayımı tarihinden N ay/gün/yıl sonra",
    "dd/mm/yyyy tarihinde yürürlüğe girer". Articles named in the Yürürlük text get their own dates
    and are reported as excluded (their version date stays unknown).
    """
    if not yururluk_text:
        return None, set(), "no Yürürlük article"
    text = re.sub(r"\s+", " ", yururluk_text)
    excluded = {m.group(1) for m in _ARTICLE_REF.finditer(text)}
    m = _YURURLUK_ON.search(text)
    if m:
        d = make_date(m.group(1), m.group(2), m.group(3))
        if d:
            return d, excluded, "explicit date in Yürürlük article"
    if gazette is None:
        return None, excluded, "publication date unknown"
    if _YURURLUK_SAME_DAY.search(text):
        return gazette.date, excluded, "in force on publication"
    m = _YURURLUK_AFTER.search(text)
    if m:
        qty_raw = m.group(1).strip().lower()
        qty = int(qty_raw) if qty_raw.isdigit() else _MONTH_WORDS.get(qty_raw)
        if qty:
            unit = m.group(2).lower()
            if unit.startswith("a"):
                return add_months(gazette.date, qty), excluded, f"{qty} ay after publication"
            if unit.startswith("y"):
                return add_months(gazette.date, 12 * qty), excluded, f"{qty} yıl after publication"
            from datetime import timedelta

            return gazette.date + timedelta(days=qty), excluded, f"{qty} gün after publication"
    return None, excluded, "Yürürlük wording not recognised"


@dataclass
class Revision:
    date: date
    articles: set[str]  # path segments: m5, gm1, em2, m61A


def parse_revision_appendix(lines: list[str]) -> list[Revision]:
    """Borsa İstanbul style revision tables: ``dd/mm/yyyy tarihinde belgede aşağıdaki değişiklikler…``."""
    out: list[Revision] = []
    for ln in lines:
        m = _REVISION_BLOCK.search(ln)
        if m:
            d = make_date(m.group(1), m.group(2), m.group(3))
            if d:
                out.append(Revision(d, set()))
            continue
        if out:
            for am in _REVISION_ARTICLE.finditer(ln):
                kind = am.group(1).split()[0]
                prefix = {"GEÇİCİ": "gm", "EK": "em"}.get(kind, "m")
                out[-1].articles.add(f"{prefix}{am.group(2)}{am.group(3) or ''}")
    return out
