"""Numbers and dates in Turkish/English text, and precision-aware matching against source text.

A number written in model output (``1,3 milyon EUR``) is matched against numbers found in the source
text (``1.325.000 EUR``) within the precision the writer used: half a unit of the last written digit,
times the scale word (``bin``/``milyon``/``milyar``/…). Source numbers are exact. Ambiguous notations
(``1,234``, ``1.234``) keep both readings (Turkish and English) so neither language is penalised.
Dates (``27.10.2026``, ``27/10/2026``, ``2026-10-27``, ``27 Ekim 2026``) are matched as ISO dates;
their day/month/year parts also count as source numbers. Clock times are ignored. Digits glued to
letters (``XU100``, ``II-15.1``, ``S12``) are identifiers, not numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from papi.core.text import fold_diacritics, parse_tr_number, tr_lower

_MARKERS = re.compile(r"\[(?:S\d+|KB:[^\]]*)\]")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_DATE_DMY = re.compile(r"(?<![\d.,/])(\d{1,2})[./](\d{1,2})[./](\d{4})(?![\d/])")
_DATE_ISO = re.compile(r"(?<![\d.,])(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTHS = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
    "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}  # fmt: skip
_DATE_TEXT = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{4})(?!\d)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_SCALES = {
    "bin": Decimal(10) ** 3,
    "thousand": Decimal(10) ** 3,
    "milyon": Decimal(10) ** 6,
    "million": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "mio": Decimal(10) ** 6,
    "milyar": Decimal(10) ** 9,
    "billion": Decimal(10) ** 9,
    "mlr": Decimal(10) ** 9,
    "mlyr": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "trilyon": Decimal(10) ** 12,
    "trillion": Decimal(10) ** 12,
}
_SCALE_AFTER = re.compile(r"\s*(" + "|".join(sorted(_SCALES, key=len, reverse=True)) + r")(?![a-z])")
_HALF = Decimal("0.5")
_ORDINAL_DOT = re.compile(r"\.\s+[a-zçğıöşü]")  # on the original text: "4. çeyrek" (not "… 5. Bu")
_ORDINAL_SUFFIX = re.compile(r"['’]?(?:i|u)?nc[iu](?![a-z])")  # on folded text: "3'üncü", "2nci"


@dataclass(frozen=True)
class Num:
    """One reading of a number token: ``value`` (scaled) ± ``tol`` (writer's precision)."""

    raw: str
    value: Decimal
    tol: Decimal
    scaled: bool = False


@dataclass
class Extracted:
    numbers: list[list[Num]]  # per token: its readings
    dates: list[str]  # ISO dates
    date_parts: list[Decimal]

    def all_values(self) -> list[Decimal]:
        return [n.value for readings in self.numbers for n in readings]


def _fold(text: str) -> str:
    return fold_diacritics(tr_lower(text))


def _iso(y: str, m: str | int, d: str) -> str | None:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _decimals(token: str, mode: str) -> int:
    """Digits after the decimal mark of ``token`` read in ``mode`` (0 when integer)."""
    dots, commas = token.count("."), token.count(",")
    if dots and commas:
        mark = "." if token.rfind(".") > token.rfind(",") else ","
    elif mode == "dot":
        mark = "." if dots == 1 else ""
    elif commas == 1:
        mark = ","
    elif dots == 1 and len(token) - token.rfind(".") - 1 != 3:
        mark = "."
    else:
        mark = ""
    return len(token) - token.rfind(mark) - 1 if mark else 0


def _readings(token: str, scale: Decimal | None) -> list[Num]:
    out: list[Num] = []
    seen: set[Decimal] = set()
    for mode in ("auto", "dot"):
        try:
            v = parse_tr_number(token, decimal=mode)  # type: ignore[arg-type]
        except (ValueError, InvalidOperation):
            continue
        if v in seen:
            continue
        seen.add(v)
        tol = _HALF * Decimal(10) ** -_decimals(token, mode)
        if scale is not None:
            out.append(Num(token, v * scale, tol * scale, True))
        out.append(Num(token, v, tol, False))
    return out


def extract(text: str | None) -> Extracted:
    """Numbers (with readings), ISO dates and date parts found in ``text``."""
    if not text:
        return Extracted([], [], [])
    s = _MARKERS.sub(" ", text)
    dates: list[str] = []
    parts: list[Decimal] = []

    def take_date(m: re.Match[str], y: str, mo: str | int, d: str) -> str:
        iso = _iso(y, mo, d)
        if iso is None:
            return m.group(0)
        dates.append(iso)
        parts.extend(Decimal(int(x)) for x in (y, mo, d))
        return " " * len(m.group(0))

    s = _DATE_ISO.sub(lambda m: take_date(m, m.group(1), m.group(2), m.group(3)), s)
    s = _DATE_DMY.sub(lambda m: take_date(m, m.group(3), m.group(2), m.group(1)), s)
    folded = _fold(s)
    if len(folded) == len(s):  # month names are matched on the folded text (same offsets)
        spans = [(m.start(), m.end(), m) for m in _DATE_TEXT.finditer(folded)]
        for start, end, m in reversed(spans):
            iso = _iso(m.group(3), _MONTHS[m.group(2)], m.group(1))
            if iso:
                dates.append(iso)
                parts.extend(Decimal(int(x)) for x in (m.group(3), _MONTHS[m.group(2)], m.group(1)))
                s = s[:start] + " " * (end - start) + s[end:]
    s = _TIME.sub(lambda m: " " * len(m.group(0)), s)
    folded = _fold(s)
    if len(folded) != len(s):
        folded = s.lower()

    numbers: list[list[Num]] = []
    for m in _NUMBER.finditer(s):
        start = m.start()
        prev = s[start - 1] if start else ""
        if prev.isalpha() or prev == "_":
            continue
        if prev == "-" and start >= 2 and s[start - 2].isalpha():  # II-15.1, B-23
            continue
        if _ORDINAL_DOT.match(s, m.end()) or _ORDINAL_SUFFIX.match(folded, m.end()):  # ordinals
            continue
        sm = _SCALE_AFTER.match(folded, m.end())
        scale = _SCALES[sm.group(1)] if sm else None
        readings = _readings(m.group(0), scale)
        if readings:
            numbers.append(readings)
    return Extracted(numbers, dates, parts)


class Pool:
    """Source numbers and dates that output numbers may be traced to. ``values`` are numbers written
    in the source; ``parts`` are day/month/year parts of source dates (traceable, but never used as
    computation inputs)."""

    def __init__(self) -> None:
        self.values: set[Decimal] = set()
        self.parts: set[Decimal] = set()
        self.dates: set[str] = set()

    def add_text(self, text: str | None) -> Pool:
        ex = extract(text)
        self.values.update(ex.all_values())
        self.parts.update(ex.date_parts)
        self.dates.update(ex.dates)
        return self

    def add_value(self, value: Decimal | float | int | str | None) -> Pool:
        d = to_decimal(value)
        if d is not None:
            self.values.add(d)
        return self

    def extend(self, other: Pool) -> Pool:
        self.values |= other.values
        self.parts |= other.parts
        self.dates |= other.dates
        return self

    def has_number(self, readings: list[Num]) -> bool:
        candidates = self.values | self.parts
        for n in readings:
            if n.value in candidates:
                return True
            if any(abs(n.value - v) <= n.tol for v in candidates):
                return True
        return False

    def has_exact(self, value: Decimal) -> bool:
        return any(_close(value, v) for v in self.values)


def to_decimal(value: Decimal | float | int | str | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    try:
        return parse_tr_number(str(value))
    except (ValueError, InvalidOperation):
        ex = extract(str(value))
        return ex.numbers[0][0].value if ex.numbers else None


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(abs(b) * Decimal("1e-9"), Decimal("1e-9"))


@dataclass
class Untraced:
    raw: str
    kind: str  # number | date


def untraced(text: str | None, pool: Pool, *, also_ok: list[list[Num]] | None = None) -> list[Untraced]:
    """Numbers / dates in ``text`` that cannot be traced to ``pool``."""
    ex = extract(text)
    bad: list[Untraced] = []
    for iso in ex.dates:
        if iso not in pool.dates:
            bad.append(Untraced(iso, "date"))
    for readings in ex.numbers:
        if pool.has_number(readings):
            continue
        if also_ok and any(_overlaps(readings, ok) for ok in also_ok):
            continue
        bad.append(Untraced(readings[0].raw, "number"))
    return bad


def _overlaps(a: list[Num], b: list[Num]) -> bool:
    return any(abs(x.value - y.value) <= max(x.tol, y.tol) for x in a for y in b)


def first_number(text: str | None) -> list[Num] | None:
    ex = extract(text)
    return ex.numbers[0] if ex.numbers else None
