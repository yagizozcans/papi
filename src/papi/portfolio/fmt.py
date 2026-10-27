"""Turkish number/date formatting and date parsing for portfolio I/O (``1.234,56``, ``28.10.2026``)."""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_DMY = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")
_YMD = re.compile(r"^(\d{4})[./-](\d{1,2})[./-](\d{1,2})$")


def parse_date_tr(text: str) -> date:
    """``2026-10-01``, ``2026.10.01``, ``01.10.2026``, ``01/10/2026`` or ``1-10-2026`` → ``date``."""
    s = text.strip()
    if m := _YMD.match(s):
        y, mo, d = (int(g) for g in m.groups())
    elif m := _DMY.match(s):
        d, mo, y = (int(g) for g in m.groups())
    else:
        raise ValueError(f"tarih anlaşılamadı: {text!r} (GG.AA.YYYY veya YYYY-AA-GG)")
    try:
        return date(y, mo, d)
    except ValueError:
        raise ValueError(f"geçersiz tarih: {text!r}") from None


def fmt_num(value: Decimal | float | int | None, places: int = 2, *, trim: bool = False) -> str:
    """``Decimal('1234.5')`` → ``1.234,50``; ``trim`` drops trailing fractional zeros (``100``)."""
    if value is None:
        return "—"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    q = (
        d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if places > 0
        else d.quantize(Decimal(1))
    )
    sign = "-" if q < 0 else ""
    int_part, _, frac = f"{abs(q):f}".partition(".")
    groups = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    out = ".".join(groups)
    if trim:
        frac = frac.rstrip("0")
    return f"{sign}{out},{frac}" if frac else f"{sign}{out}"


def fmt_qty(value: Decimal | None) -> str:
    """Share quantities: no decimals unless fractional."""
    return fmt_num(value, 6, trim=True)


def fmt_pct(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"%{fmt_num(Decimal(str(value)), places)}"


def fmt_date(value: date | None) -> str:
    return "—" if value is None else value.strftime("%d.%m.%Y")
