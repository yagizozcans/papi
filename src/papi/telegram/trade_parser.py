"""Deterministic Turkish/English trade-message parser (08 §4.2–4.3).

``parse(text, *, now)`` returns

- :class:`ParsedTrade` — side, instrument reference, quantity (or a fraction of the current holding
  such as "yarısını"), price, trade date (+ exact time when known) and optional fees;
- :class:`NeedsInfo` — it is a trade but something is missing or ambiguous (``missing`` lists the
  fields, ``draft`` holds what was understood). papi then asks; it never guesses;
- :class:`NotATrade` — questions, what-ifs ("alsam ne olur?"), greetings, other commands.

Supported shapes (examples in ``tests/telegram/trade_phrases.yaml``):

- ``THYAO 100 lot 285,40'tan aldım`` · ``100 adet ASELS 62,5 TL'den satıldı`` · ``ASELS 200 sattim 61.80``
- ``/al THYAO 100 285.40 [tarih]`` · ``/sat ASELS 50 62,5 dün``
- ``ASELS'in yarısını 62'den sattım`` · ``hepsini`` · ``%25'ini`` · ``üçte birini``
- English: ``bought 100 THYAO at 285.40`` · ``sold half of ASELS @ 62``
- Dates: message time by default; ``bugün``, ``dün``, ``önceki gün``, ``27.10.2026``, ``27/10``,
  ``27 Ekim``, ``2026-10-27``; optional ``14:35`` time. Weekday/"geçen hafta" style phrases are
  ambiguous → ``NeedsInfo(date)``.
- Numbers: Turkish or English notation via :func:`papi.core.text.parse_tr_number` (``62.500`` = 62500).

Role of each number: followed by ``lot/adet/hisse/pay/tane/shares`` → quantity; with ``TL/₺/lira``, an
ablative suffix (``'den/'tan``), ``@``/``at``/``fiyat``/``ortalama``/``x`` → price. Two unlabelled
numbers follow the command order *quantity, price* (a non-integer one is the price). A single
unlabelled number in free text is ambiguous (``NeedsInfo``); after ``/al``/``/sat`` it is the quantity.
Quantities must be whole lots (BIST lot = 1 share).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from papi.core.text import fold_diacritics, parse_tr_number, tr_lower
from papi.core.timeutil import IST

Side = Literal["buy", "sell"]

__all__ = [
    "NeedsInfo",
    "NotATrade",
    "ParsedTrade",
    "TradeDraft",
    "parse",
    "parse_date_phrase",
    "parse_side_word",
]


@dataclass(frozen=True)
class TradeDraft:
    """Whatever was understood so far (all optional)."""

    side: Side | None = None
    instrument_ref: str | None = None
    quantity: Decimal | None = None
    fraction: Decimal | None = None  # of the current holding (sell only)
    price: Decimal | None = None
    fees: Decimal | None = None
    trade_date: date | None = None
    traded_at: datetime | None = None
    date_explicit: bool = False  # date came from the text (not the message time)

    def as_json(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "instrument_ref": self.instrument_ref,
            "quantity": None if self.quantity is None else str(self.quantity),
            "fraction": None if self.fraction is None else str(self.fraction),
            "price": None if self.price is None else str(self.price),
            "fees": None if self.fees is None else str(self.fees),
            "trade_date": None if self.trade_date is None else self.trade_date.isoformat(),
            "traded_at": None if self.traded_at is None else self.traded_at.isoformat(),
            "date_explicit": self.date_explicit,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TradeDraft:
        def dec(key: str) -> Decimal | None:
            v = data.get(key)
            return None if v is None else Decimal(str(v))

        return cls(
            side=data.get("side"),
            instrument_ref=data.get("instrument_ref"),
            quantity=dec("quantity"),
            fraction=dec("fraction"),
            price=dec("price"),
            fees=dec("fees"),
            trade_date=date.fromisoformat(data["trade_date"]) if data.get("trade_date") else None,
            traded_at=datetime.fromisoformat(data["traded_at"]) if data.get("traded_at") else None,
            date_explicit=bool(data.get("date_explicit")),
        )

    def missing(self) -> tuple[str, ...]:
        out = []
        if self.side is None:
            out.append("side")
        if not self.instrument_ref:
            out.append("instrument")
        if self.quantity is None and self.fraction is None:
            out.append("quantity")
        if self.price is None:
            out.append("price")
        if self.trade_date is None:
            out.append("date")
        return tuple(out)


@dataclass(frozen=True)
class ParsedTrade:
    side: Side
    instrument_ref: str
    quantity: Decimal | None  # None when ``fraction`` is set
    price: Decimal
    trade_date: date
    traded_at: datetime | None = None
    fraction: Decimal | None = None
    fees: Decimal | None = None
    date_explicit: bool = False

    @property
    def draft(self) -> TradeDraft:
        return TradeDraft(
            side=self.side,
            instrument_ref=self.instrument_ref,
            quantity=self.quantity,
            fraction=self.fraction,
            price=self.price,
            fees=self.fees,
            trade_date=self.trade_date,
            traded_at=self.traded_at,
            date_explicit=self.date_explicit,
        )


@dataclass(frozen=True)
class NeedsInfo:
    missing: tuple[str, ...]
    draft: TradeDraft = field(default_factory=TradeDraft)
    reason: str = ""


@dataclass(frozen=True)
class NotATrade:
    reason: str  # empty | command | what_if | question | no_trade_signal


ParseResult = ParsedTrade | NeedsInfo | NotATrade


def complete(draft: TradeDraft) -> ParseResult:
    """``ParsedTrade`` if nothing is missing, else ``NeedsInfo``."""
    missing = draft.missing()
    if missing:
        return NeedsInfo(missing, draft, "eksik bilgi")
    if draft.fraction is not None and draft.side != "sell":
        return NeedsInfo(("quantity",), replace(draft, fraction=None), "kesirli adet yalnız satışta")
    assert draft.side and draft.instrument_ref and draft.price is not None and draft.trade_date
    return ParsedTrade(
        side=draft.side,
        instrument_ref=draft.instrument_ref,
        quantity=draft.quantity,
        price=draft.price,
        trade_date=draft.trade_date,
        traded_at=draft.traded_at,
        fraction=draft.fraction,
        fees=draft.fees,
        date_explicit=draft.date_explicit,
    )


# ---------------------------------------------------------------------------------------- vocabulary


def _key(word: str) -> str:
    return fold_diacritics(tr_lower(word))


BUY_WORDS = {
    "aldim", "aldik", "aldi", "alindi", "alinmistir", "alis", "alim", "alimi", "alisi", "al", "ekledim",
    "eklendi", "bought", "buy",
}  # fmt: skip
SELL_WORDS = {
    "sattim", "sattik", "satti", "satildi", "satilmistir", "satis", "satisi", "sat", "bozdum", "ciktim",
    "sold", "sell",
}  # fmt: skip
EXIT_WORDS = {"ciktim"}  # "THYAO'dan çıktım" = sold the whole position
WHAT_IF_RE = re.compile(
    r"\b(alsam|satsam|alayim|satayim|alabilir|satabilir|alinir|satilir|almali|satmali|alsak|satsak|"
    r"alacagim|satacagim|alicam|saticam|alacaktim|satacaktim|almak|satmak|almayi|satmayi|"
    r"should i|can i|what if|ne olur|mantikli mi)\b"
)
UNIT_WORDS = ("lot", "adet", "hisse", "pay", "tane", "share")
CURRENCY_WORDS = {"tl", "try", "lira", "liradan", "tlden", "tldan", "₺"}
ABLATIVE = {"dan", "den", "tan", "ten"}
PRICE_BEFORE = {
    "@", "at", "fiyat", "fiyati", "fiyatla", "fiyattan", "fiyatindan", "ortalama", "ort", "from", "x",
    "×", "price", "birim", "ortalamasi",
}  # fmt: skip
PRICE_AFTER = {"fiyatla", "fiyattan", "fiyatindan", "ortalamayla", "seviyesinden", "ortalamadan"}
FEE_WORDS = {"komisyon", "kom", "masraf", "fee", "fees", "commission", "komisyonu"}
FRACTION_WORDS = {
    "yarisini": Decimal("0.5"), "yarisi": Decimal("0.5"), "yari": Decimal("0.5"), "half": Decimal("0.5"),
    "hepsini": Decimal(1), "hepsi": Decimal(1), "tamamini": Decimal(1), "tamami": Decimal(1),
    "tumunu": Decimal(1), "tumu": Decimal(1), "all": Decimal(1), "everything": Decimal(1),
    "tamamen": Decimal(1), "ceyregini": Decimal("0.25"), "ceyregi": Decimal("0.25"),
    "quarter": Decimal("0.25"),
}  # fmt: skip
NTH_WORDS = {"ikide": 2, "ucte": 3, "dortte": 4, "beste": 5}
ONE_WORDS = {"bir", "birini", "biri"}
RELATIVE_DAYS = {"bugun": 0, "bugunku": 0, "today": 0, "dun": 1, "dunku": 1, "yesterday": 1}
TWO_DAYS_FIRST = {"onceki", "evvelki", "evvelsi"}
VAGUE_DATE_WORDS = {
    "pazartesi", "sali", "carsamba", "persembe", "cuma", "cumartesi", "pazar", "gecen", "gecenlerde",
    "monday", "tuesday", "wednesday", "thursday", "friday", "last", "gecenki",
}  # fmt: skip
STOPWORDS = {
    "ben", "biz", "i", "we", "my", "me", "the", "of", "a", "an", "and", "with", "for", "on", "in",
    "shares", "share", "stock", "stocks", "lot", "lotu", "adet", "tane", "hisse", "hissesi",
    "hissesinden", "hisseden", "hisselerinden", "hisselerini", "hissesini", "payi", "paylari",
    "paylarini", "kadar", "daha", "ek", "ilave", "olarak", "ile", "ve", "da", "de", "ta", "te",
    "toplam", "uzerinden", "civarinda", "civari", "yaptim", "yaptik", "ettim", "islem", "islemi",
    "emir", "emri", "emrim", "gerceklesti", "gerceklesen", "sabah", "ogle", "oglen", "aksam",
    "aksamustu", "piyasadan", "borsadan", "az", "once", "simdi", "hemen", "su", "ama",
    "tl", "try", "lira", "fiyat", "fiyati", "fiyatla", "fiyattan", "fiyatindan", "ortalama", "ort",
    "ortalamayla", "seviyesinden", "ortalamadan", "at", "from", "x", "price", "birim", "saat",
    "tarihinde", "tarihli", "gun", "gunu", "today", "yesterday", "pozisyona", "pozisyonuma",
    "portfoye", "portfoyume", "pozisyonu", "pozisyonun", "pozisyonumun", "pozisyonumu", "lotunu",
    "lotun", "adedini", "tanesini", "her", "biri", "yeni", "just", "today's", "kur", "kurdan",
}  # fmt: skip
NON_TICKERS = {"TL", "TRY", "USD", "EUR", "LOT", "ADET", "KAP", "BIST", "SPK", "OK"}

_MONTH_NAMES_TR = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7, "agustos": 8,
    "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}  # fmt: skip
_MONTH_NAMES_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
    "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_TR_MONTH_SPELLINGS = (
    "ocak", "şubat", "subat", "mart", "nisan", "mayıs", "mayis", "haziran", "temmuz", "ağustos", "agustos",
    "eylül", "eylul", "ekim", "kasım", "kasim", "aralık", "aralik",
)  # fmt: skip
# Turkish month names may carry case suffixes ("Ekim'de", "ekimde"); English names must end the word.
_MONTH_ALT = (
    "(?:" + "|".join(sorted(_TR_MONTH_SPELLINGS, key=len, reverse=True)) + r")(?:['’]?[^\W\d_]*)"
    "|(?:" + "|".join(sorted(_MONTH_NAMES_EN, key=len, reverse=True)) + r")\b\.?"
)


def _month_num(word: str) -> int:
    k = _key(word).rstrip(".")
    if k in _MONTH_NAMES_EN:
        return _MONTH_NAMES_EN[k]
    for name, num in _MONTH_NAMES_TR.items():
        if k.startswith(name):
            return num
    raise ValueError(word)


_SUFFIX = r"(?:['’][^\W\d_]*|tarihinde|tarihli)?"
_DATE_DMY = re.compile(r"(?<![\d.,])(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?![\d.,]\d)" + _SUFFIX)
_DATE_ISO = re.compile(r"(?<![\d.,])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d])" + _SUFFIX)
_DATE_DM_SLASH = re.compile(r"(?<![\d.,/])(\d{1,2})/(\d{1,2})(?![\d/])" + _SUFFIX)
_DATE_D_MONTH = re.compile(
    rf"(?<![\d.,])(\d{{1,2}})\s+({_MONTH_ALT})(?:\s+(\d{{4}})(?![\d.,]\d)(?:['’][^\W\d_]*)?)?", re.IGNORECASE
)
_DATE_MONTH_D = re.compile(
    rf"\b({_MONTH_ALT})\s+(\d{{1,2}})(?!\d)(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?![\d.,]\d)", re.IGNORECASE
)
_TIME = re.compile(r"(?:\bsaat\s*)?(?<![\d.,:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])(?:['’][^\W\d_]*)?", re.I)
_COMMAND = re.compile(r"^/(al|sat|buy|sell)(?:@\w+)?(?:\s+|$)(.*)$", re.IGNORECASE | re.DOTALL)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[^\W\d_]+(?:\d+[^\W\d_]+)*")
_APOS = re.compile(r"['’]([^\W\d_]*)")
_TICKER = re.compile(r"^[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ0-9]{2,5}$")


@dataclass
class _Tok:
    text: str
    kind: Literal["num", "word", "sym"]
    suffix: str = ""
    glued_prev: bool = False  # no whitespace before this token
    used: bool = False

    @property
    def key(self) -> str:
        return _key(self.text)


def _tokenize(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    for raw in text.split():
        w = raw.strip('"“”()[]{}<>!?;')
        w = w.rstrip(".,:")
        glued = False
        while w:
            ch = w[0]
            m = _NUMBER.match(w)
            if m:
                num = m.group()
                w = w[m.end() :]
                suffix = ""
                ma = _APOS.match(w)
                if ma:
                    suffix, w = ma.group(1), w[ma.end() :]
                out.append(_Tok(num, "num", suffix=_key(suffix), glued_prev=glued))
            elif (mw := _WORD.match(w)) is not None:
                word = mw.group()
                w = w[mw.end() :]
                suffix = ""
                ma = _APOS.match(w)
                if ma:
                    suffix, w = ma.group(1), w[ma.end() :]
                out.append(_Tok(word, "word", suffix=_key(suffix), glued_prev=glued))
            else:
                if ch not in ",.:'’-–—=/":
                    out.append(_Tok(ch, "sym", glued_prev=glued))
                w = w[1:]
            glued = True
    return out


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return parse_tr_number(raw)
    except (ValueError, InvalidOperation):
        return None


def _is_int(value: Decimal) -> bool:
    return value == value.to_integral_value()


def _trailing_fee_word(toks: list[_Tok], i: int) -> int | None:
    """Index of the fee word in "12,50 komisyon" / "12,50 TL komisyon" (fee word not followed by a
    number, otherwise the number belongs to "komisyon 5")."""
    j = i + 1
    if j < len(toks) and toks[j].key in CURRENCY_WORDS:
        j += 1
    if j >= len(toks) or toks[j].key not in FEE_WORDS:
        return None
    after = toks[j + 1] if j + 1 < len(toks) else None
    if after is not None and (after.kind == "num" or after.text == "₺"):
        return None
    return j


def parse_side_word(text: str) -> Side | None:
    """``buy``/``sell`` from a short reply like "al", "satış", "aldım"; ``None`` if unclear."""
    keys = {_key(t.text) for t in _tokenize(text) if t.kind == "word"}
    buy, sell = bool(keys & BUY_WORDS), bool(keys & SELL_WORDS)
    if buy == sell:
        return None
    return "buy" if buy else "sell"


# ---------------------------------------------------------------------------------------- dates


@dataclass
class _DateInfo:
    dates: list[date] = field(default_factory=list)
    clock: time | None = None
    vague: bool = False
    invalid: bool = False


def _mk_date(y: int, m: int, d: int, info: _DateInfo) -> None:
    try:
        info.dates.append(date(y, m, d))
    except ValueError:
        info.invalid = True


def _extract_dates(text: str, today: date, info: _DateInfo) -> str:
    def year4(raw: str | None) -> int:
        if not raw:
            return today.year
        y = int(raw)
        return 2000 + y if y < 100 else y

    def sub(pattern: re.Pattern[str], fn: Any, s: str) -> str:
        return pattern.sub(lambda m: (fn(m), " ")[1], s)

    text = sub(_DATE_ISO, lambda m: _mk_date(int(m[1]), int(m[2]), int(m[3]), info), text)
    text = sub(_DATE_DMY, lambda m: _mk_date(year4(m[3]), int(m[2]), int(m[1]), info), text)
    text = sub(_DATE_D_MONTH, lambda m: _mk_date(year4(m[3]), _month_num(m[2]), int(m[1]), info), text)
    text = sub(_DATE_MONTH_D, lambda m: _mk_date(year4(m[3]), _month_num(m[1]), int(m[2]), info), text)
    text = sub(_DATE_DM_SLASH, lambda m: _mk_date(today.year, int(m[2]), int(m[1]), info), text)

    def on_time(m: re.Match[str]) -> None:
        if info.clock is not None and info.clock != time(int(m[1]), int(m[2])):
            info.invalid = True
        info.clock = time(int(m[1]), int(m[2]))

    return sub(_TIME, on_time, text)


def parse_date_phrase(text: str, *, now: datetime) -> date | None:
    """A date from a short reply ("dün", "27.10.2026", "27 Ekim"); ``None`` if absent/ambiguous."""
    today = now.astimezone(IST).date()
    info = _DateInfo()
    rest = _extract_dates(text, today, info)
    for i, tok in enumerate(toks := _tokenize(rest)):
        k = tok.key
        if k in RELATIVE_DAYS:
            info.dates.append(today - timedelta(days=RELATIVE_DAYS[k]))
        elif k in TWO_DAYS_FIRST and i + 1 < len(toks) and toks[i + 1].key == "gun":
            info.dates.append(today - timedelta(days=2))
        elif k in VAGUE_DATE_WORDS:
            info.vague = True
    distinct = set(info.dates)
    if info.invalid or info.vague or len(distinct) != 1:
        return None
    d = distinct.pop()
    return None if d > today else d


# ---------------------------------------------------------------------------------------- parser


def parse(text: str, *, now: datetime) -> ParseResult:
    """Parse a trade message. ``now`` is the message time (aware); dates resolve in Istanbul time."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    s = " ".join((text or "").split())
    if not s:
        return NotATrade("empty")
    now_ist = now.astimezone(IST)
    today = now_ist.date()

    command_side: Side | None = None
    if s.startswith("/"):
        m = _COMMAND.match(s)
        if not m:
            return NotATrade("command")
        command_side = "buy" if _key(m[1]) in {"al", "buy"} else "sell"
        s = m[2]

    folded = _key(s)
    if command_side is None and WHAT_IF_RE.search(folded):
        return NotATrade("what_if")
    if command_side is None and "?" in s:
        return NotATrade("question")

    info = _DateInfo()
    s = _extract_dates(s, today, info)
    toks = _tokenize(s)

    sides: set[Side] = set()
    exit_word = False
    fraction: Decimal | None = None
    fraction_conflict = False
    fees: Decimal | None = None
    fee_conflict = False

    def set_fraction(value: Decimal) -> None:
        nonlocal fraction, fraction_conflict
        if fraction is not None and fraction != value:
            fraction_conflict = True
        fraction = value

    n = len(toks)
    for i, tok in enumerate(toks):
        if tok.used:
            continue
        k = tok.key
        nxt = toks[i + 1] if i + 1 < n else None
        if tok.kind == "word":
            if k in BUY_WORDS:
                sides.add("buy")
                tok.used = True
            elif k in SELL_WORDS:
                sides.add("sell")
                exit_word = exit_word or k in EXIT_WORDS
                tok.used = True
            elif k in FRACTION_WORDS:
                set_fraction(FRACTION_WORDS[k])
                tok.used = True
            elif k in NTH_WORDS and nxt is not None and nxt.key in ONE_WORDS:
                set_fraction(Decimal(1) / Decimal(NTH_WORDS[k]))
                tok.used = nxt.used = True
            elif k == "yuzde" and nxt is not None and nxt.kind == "num":
                pct = _to_decimal(nxt.text)
                if pct is not None and 0 < pct <= 100:
                    set_fraction(pct / 100)
                tok.used = nxt.used = True
            elif k in RELATIVE_DAYS:
                info.dates.append(today - timedelta(days=RELATIVE_DAYS[k]))
                tok.used = True
            elif k in TWO_DAYS_FIRST and nxt is not None and nxt.key in {"gun", "gunu"}:
                info.dates.append(today - timedelta(days=2))
                tok.used = nxt.used = True
            elif k in VAGUE_DATE_WORDS:
                info.vague = True
                tok.used = True
            elif k in FEE_WORDS:
                tok.used = True
                j = i + 1
                while j < n and toks[j].kind == "sym" and toks[j].text in "₺":
                    toks[j].used = True
                    j += 1
                if j < n and toks[j].kind == "num":
                    value = _to_decimal(toks[j].text)
                    if value is None or (fees is not None and fees != value):
                        fee_conflict = True
                    fees = value
                    toks[j].used = True
                    if j + 1 < n and toks[j + 1].key in CURRENCY_WORDS:
                        toks[j + 1].used = True
        elif tok.kind == "sym" and tok.text == "%" and nxt is not None and nxt.kind == "num":
            pct = _to_decimal(nxt.text)
            if pct is not None and 0 < pct <= 100:
                set_fraction(pct / 100)
            tok.used = nxt.used = True
        elif tok.kind == "num" and nxt is not None and nxt.kind == "sym" and nxt.text == "%":
            pct = _to_decimal(tok.text)
            if pct is not None and 0 < pct <= 100:
                set_fraction(pct / 100)
            tok.used = nxt.used = True
        elif tok.kind == "num" and nxt is not None and (fee_at := _trailing_fee_word(toks, i)) is not None:
            # "12,50 komisyon" / "12,50 TL komisyon"
            value = _to_decimal(tok.text)
            if value is None or (fees is not None and fees != value):
                fee_conflict = True
            fees = value
            tok.used = True
            for j in range(i + 1, fee_at + 1):
                toks[j].used = True

    # ---- numbers: quantity vs price
    qty_c: list[Decimal] = []
    price_c: list[Decimal] = []
    plain: list[Decimal] = []
    bad_number = False
    for i, tok in enumerate(toks):
        if tok.used or tok.kind != "num":
            continue
        value = _to_decimal(tok.text)
        tok.used = True
        if value is None:
            bad_number = True
            continue
        prev = toks[i - 1] if i > 0 else None
        nxt = toks[i + 1] if i + 1 < n else None
        nxt2 = toks[i + 2] if i + 2 < n else None
        is_qty = nxt is not None and nxt.kind == "word" and nxt.key.startswith(UNIT_WORDS)
        if (
            nxt is not None
            and nxt.kind == "word"
            and nxt.key == "x"
            and nxt2 is not None
            and nxt2.kind == "num"
        ):
            is_qty = True  # "100 x 285,40"
        is_price = (
            tok.suffix in ABLATIVE
            or (prev is not None and (prev.key in PRICE_BEFORE or prev.text in {"₺", "@", "×"}))
            or (nxt is not None and (nxt.key in CURRENCY_WORDS or nxt.key in PRICE_AFTER))
            or (nxt is not None and nxt.kind == "word" and nxt.glued_prev and nxt.key in ABLATIVE)
            or (nxt is not None and nxt.kind == "sym" and nxt.text == "₺")
        )
        if is_qty:
            nxt.used = True  # type: ignore[union-attr]
            qty_c.append(value)
        elif is_price:
            price_c.append(value)
            if nxt is not None and (nxt.key in CURRENCY_WORDS or nxt.key in ABLATIVE or nxt.text == "₺"):
                nxt.used = True
        else:
            plain.append(value)

    quantity: Decimal | None = None
    price: Decimal | None = None
    ambiguous: list[str] = []
    if len(qty_c) > 1:
        ambiguous.append("quantity")
    elif qty_c:
        quantity = qty_c[0]
    if len(price_c) > 1:
        ambiguous.append("price")
    elif price_c:
        price = price_c[0]

    if not ambiguous:
        if fraction is not None:
            if quantity is not None:
                ambiguous.append("quantity")
                quantity = fraction = None
            elif price is None and len(plain) == 1:
                price = plain.pop()
        elif quantity is None and price is None:
            if len(plain) == 2:
                a, b = plain
                if not _is_int(a) and _is_int(b):
                    quantity, price = b, a
                else:
                    quantity, price = a, b
                plain = []
            elif len(plain) == 1 and command_side is not None:
                quantity = plain.pop()
        elif quantity is not None and price is None and len(plain) == 1:
            price = plain.pop()
        elif price is not None and quantity is None and len(plain) == 1:
            quantity = plain.pop()
        if plain:
            ambiguous.extend(f for f in ("quantity", "price") if f not in ambiguous)
            if quantity is not None and price is not None:
                quantity = price = None  # an unexplained extra number: ask instead of guessing

    if quantity is not None and (quantity <= 0 or not _is_int(quantity)):
        ambiguous.append("quantity")
        quantity = None
    if price is not None and price <= 0:
        ambiguous.append("price")
        price = None
    if bad_number:
        ambiguous.extend(f for f in ("quantity", "price") if f not in ambiguous)

    # ---- instrument
    tickers: list[str] = []
    words: list[str] = []
    for tok in toks:
        if tok.used or tok.kind != "word":
            continue
        k = tok.key
        if k in STOPWORDS or k in CURRENCY_WORDS or k in ABLATIVE or len(k) == 1:
            continue
        if _TICKER.match(tok.text) and tok.text not in NON_TICKERS:
            if tok.text not in tickers:
                tickers.append(tok.text)
        else:
            words.append(tok.text)
    instrument: str | None = None
    if len(tickers) == 1:
        instrument = tickers[0]
    elif len(tickers) > 1:
        ambiguous.append("instrument")
    elif words:
        instrument = " ".join(words)

    # ---- side
    side: Side | None = command_side
    if command_side is not None and sides and sides != {command_side}:
        ambiguous.append("side")
    if side is None and len(sides) == 1:
        side = next(iter(sides))
    elif side is None and len(sides) > 1:
        ambiguous.append("side")
    if exit_word and fraction is None and quantity is None and side == "sell":
        fraction = Decimal(1)

    trade_signal = (
        command_side is not None
        or bool(sides)
        or (instrument is not None and bool(qty_c) and len(words) <= 3)
    )
    if not trade_signal:
        return NotATrade("no_trade_signal")

    # ---- date
    trade_date: date | None
    traded_at: datetime | None
    date_explicit = bool(info.dates)
    distinct = set(info.dates)
    if info.invalid or info.vague or len(distinct) > 1:
        trade_date, traded_at = None, None
        ambiguous.append("date")
    elif distinct:
        trade_date = distinct.pop()
        traded_at = datetime.combine(trade_date, info.clock, IST) if info.clock else None
    elif info.clock is not None:
        trade_date = today
        traded_at = datetime.combine(today, info.clock, IST)
        date_explicit = True
    else:
        trade_date, traded_at = today, now
    if trade_date is not None and (trade_date > today or (traded_at is not None and traded_at > now)):
        trade_date = traded_at = None
        ambiguous.append("date")

    if fraction_conflict:
        ambiguous.append("quantity")
        fraction = None
    if fee_conflict:
        fees = None

    draft = TradeDraft(
        side=None if "side" in ambiguous else side,
        instrument_ref=instrument,
        quantity=quantity,
        fraction=fraction,
        price=price,
        fees=fees,
        trade_date=trade_date,
        traded_at=traded_at,
        date_explicit=date_explicit,
    )
    if ambiguous:
        missing = tuple(dict.fromkeys([*ambiguous, *draft.missing()]))
        return NeedsInfo(missing, draft, "belirsiz")
    return complete(draft)
