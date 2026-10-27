"""Turkish-aware text handling.

Python's ``str.lower()``/``upper()`` are locale-independent and wrong for Turkish dotted/dotless I:
``"I".lower() == "i"`` (Turkish: ``ı``) and ``"İ".lower() == "i̇"`` (``i`` + U+0307). The helpers here
apply Turkish rules first.

Number parsing (``parse_tr_number``) — rules, in order:

1. Surrounding whitespace, currency/percent tokens (``TL``, ``TRY``, ``₺``, ``%``, ``USD``, ``EUR``,
   ``$``, ``€``) and spaces/NBSP/apostrophes used as group separators are removed. A leading ``-``,
   ``+``, ``−`` (U+2212) or wrapping parentheses ``(…)`` set the sign.
2. Both ``.`` and ``,`` present → the separator that occurs **last** is the decimal mark, the other is a
   thousands separator (``1.234.567,89`` → 1234567.89; ``1,234.56`` → 1234.56).
3. Only ``,`` present → one comma is the decimal mark (Turkish default: ``285,40`` → 285.40,
   ``1,234`` → 1.234); several commas are thousands separators (``1,234,567``).
4. Only ``.`` present → several dots are thousands separators (``1.234.567``); a single dot followed
   by exactly three digits is a thousands separator in ``auto`` mode (Turkish convention: ``62.500`` →
   62500); otherwise it is a decimal mark (``285.40`` → 285.40, ``1.5`` → 1.5).
5. Thousands groups must be exactly three digits (first group 1–3), otherwise ``ValueError``.

``decimal="comma"`` / ``decimal="dot"`` force the decimal mark and skip the guessing in rules 3–4.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Literal

_TR_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})
_TR_UPPER_MAP = str.maketrans({"i": "İ", "ı": "I"})
_FOLD_MAP = str.maketrans(
    {
        "ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I", "ö": "o", "Ö": "O",
        "ş": "s", "Ş": "S", "ü": "u", "Ü": "U", "â": "a", "Â": "A", "î": "i", "Î": "I",
        "û": "u", "Û": "U",
    }
)  # fmt: skip


def tr_lower(text: str) -> str:
    """Lowercase with Turkish rules (``I``→``ı``, ``İ``→``i``)."""
    return unicodedata.normalize("NFC", text).translate(_TR_LOWER_MAP).lower()


def tr_upper(text: str) -> str:
    """Uppercase with Turkish rules (``i``→``İ``, ``ı``→``I``)."""
    return unicodedata.normalize("NFC", text).translate(_TR_UPPER_MAP).upper()


def fold_diacritics(text: str) -> str:
    """Map Turkish letters to ASCII (ç→c, ğ→g, ı→i, İ→I, ö→o, ş→s, ü→u, â→a …) and strip other marks.

    Case is preserved; apply ``tr_lower`` first for case-insensitive keys.
    """
    folded = unicodedata.normalize("NFC", text).translate(_FOLD_MAP)
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_NON_WORD = re.compile(r"[^0-9a-z]+")


def normalize_tr(text: str) -> str:
    """Search key: Turkish lowercase → diacritic fold → non-alphanumerics to single spaces.

    ``"İSTANBUL Şehir Hatları A.Ş."`` → ``"istanbul sehir hatlari a s"``. Used for FTS indexing,
    fuzzy name matching and DSL ``text ~`` comparisons. Idempotent.
    """
    key = fold_diacritics(tr_lower(text))
    return _NON_WORD.sub(" ", key).strip()


_STRIP_TOKENS = re.compile(r"(?i)(?:\bTRY\b|\bTL\b|\bUSD\b|\bEUR\b|₺|\$|€|%)")
_GROUP_SPACES = re.compile(r"[\s\u00a0\u202f'’_]")


def parse_tr_number(text: str, *, decimal: Literal["auto", "comma", "dot"] = "auto") -> Decimal:
    """Parse a number written in Turkish or English notation to ``Decimal`` (see module docstring)."""
    raw = text
    s = _STRIP_TOKENS.sub("", text.strip())
    s = _GROUP_SPACES.sub("", s)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s[:1] in {"-", "−", "+"}:
        negative = negative or s[0] != "+"
        s = s[1:]
    if not s or not re.fullmatch(r"[0-9.,]+", s) or not any(c.isdigit() for c in s):
        raise ValueError(f"not a number: {raw!r}")

    has_dot, has_comma = "." in s, "," in s
    if decimal == "comma":
        int_part, frac = _split_decimal(s, ",", ".")
    elif decimal == "dot":
        int_part, frac = _split_decimal(s, ".", ",")
    elif has_dot and has_comma:
        dec, thou = (",", ".") if s.rfind(",") > s.rfind(".") else (".", ",")
        int_part, frac = _split_decimal(s, dec, thou)
    elif has_comma:
        if s.count(",") == 1:
            int_part, frac = _split_decimal(s, ",", ".")
        else:
            int_part, frac = _check_groups(s.split(","), raw), ""
    elif has_dot:
        if s.count(".") > 1:
            int_part, frac = _check_groups(s.split("."), raw), ""
        else:
            left, right = s.split(".")
            if len(right) == 3 and left:
                int_part, frac = _check_groups([left, right], raw), ""
            else:
                int_part, frac = left, right
    else:
        int_part, frac = s, ""

    number = f"{int_part or '0'}.{frac}" if frac else (int_part or "0")
    try:
        value = Decimal(number)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise ValueError(f"not a number: {raw!r}") from exc
    return -value if negative else value


def _split_decimal(s: str, dec: str, thou: str) -> tuple[str, str]:
    if s.count(dec) > 1:
        raise ValueError(f"multiple decimal marks {dec!r} in {s!r}")
    int_raw, _, frac = s.partition(dec)
    if thou in frac:
        raise ValueError(f"thousands separator after decimal mark in {s!r}")
    int_part = _check_groups(int_raw.split(thou), s) if thou in int_raw else int_raw
    return int_part, frac


def _check_groups(groups: list[str], raw: str) -> str:
    first, rest = groups[0], groups[1:]
    if not (1 <= len(first) <= 3) or any(len(g) != 3 for g in rest):
        raise ValueError(f"bad digit grouping in {raw!r}")
    return "".join(groups)
