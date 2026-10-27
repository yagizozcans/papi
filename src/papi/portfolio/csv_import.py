"""papi CSV template import (12 §2) and İşCep quantity files for reconcile.

Template columns: ``ticker,type,quantity,price,date,fees,note``. The delimiter may be ``,``, ``;`` or a
tab (Turkish Excel saves ``;``); numbers may use Turkish decimals (``285,40``; quote them when the
delimiter is a comma); dates ``YYYY-MM-DD`` or ``GG.AA.YYYY``; types in English or Turkish (``AL``,
``SAT``, ``AÇILIŞ``…). Turkish headers (``hisse,tür,adet,fiyat,tarih,komisyon,not``) are accepted;
without a header the template column order is assumed.

Idempotency: each row gets ``dedupe_key = "csv:" + sha256(portfolio, canonical row, occurrence)`` where
``occurrence`` numbers identical rows within the file, so re-importing the same file adds nothing while
two genuinely identical trades in one file are both kept.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from sqlalchemy import Connection

from papi.core.text import normalize_tr, parse_tr_number
from papi.portfolio import service
from papi.portfolio.fmt import parse_date_tr

COLUMNS = ("ticker", "type", "quantity", "price", "date", "fees", "note")
TEMPLATE = (
    "ticker,type,quantity,price,date,fees,note\n"
    'THYAO,opening_balance,300,"279,10",2026-01-02,0,İşCep açılış bakiyesi\n'
    "THYAO,buy,100,285.40,2026-10-01,4.20,\n"
    'ASELS,buy,200,"58,20",15.09.2026,"2,10",\n'
    'ASELS,sell,50,"62,00",01.10.2026,"1,55",kısmi satış\n'
)

_HEADER_ALIASES = {
    **{c: c for c in COLUMNS},
    "hisse": "ticker",
    "kod": "ticker",
    "sembol": "ticker",
    "tur": "type",
    "islem": "type",
    "islem turu": "type",
    "adet": "quantity",
    "miktar": "quantity",
    "lot": "quantity",
    "fiyat": "price",
    "tarih": "date",
    "trade date": "date",
    "komisyon": "fees",
    "masraf": "fees",
    "fee": "fees",
    "not": "note",
    "aciklama": "note",
}


@dataclass
class ImportRow:
    line: int
    values: dict[str, str]
    proposal: service.ProposedTxn | None = None
    dedupe_key: str | None = None
    status: Literal["new", "duplicate", "error"] = "new"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    txn_id: str | None = None


@dataclass
class ImportResult:
    rows: list[ImportRow]
    dry_run: bool
    inserted: list[str] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)

    @property
    def ok(self) -> bool:
        return self.count("error") == 0


def _delimiter(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
    if "\t" in first:
        return "\t"
    if ";" in first:
        return ";"
    return ","


def read_rows(text: str, columns: tuple[str, ...] = COLUMNS) -> list[tuple[int, dict[str, str]]]:
    """Parse CSV text to ``(line_no, {column: value})``; header optional, ``#`` comment lines skipped."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=_delimiter(text))
    rows: list[tuple[int, dict[str, str]]] = []
    header: list[str] | None = None
    for cells in reader:
        line = reader.line_num
        if not any(c.strip() for c in cells) or cells[0].lstrip().startswith("#"):
            continue
        if header is None:
            keys = [_HEADER_ALIASES.get(normalize_tr(c)) for c in cells]
            if keys and keys[0] is not None and all(k in columns for k in keys if k):
                unknown = [c for c, k in zip(cells, keys, strict=True) if k is None and c.strip()]
                if unknown:
                    raise ValueError(f"satır {line}: bilinmeyen sütun(lar): {', '.join(unknown)}")
                header = [k or "" for k in keys]
                continue
            header = list(columns)
        if len(cells) > len(header) and any(c.strip() for c in cells[len(header) :]):
            raise ValueError(f"satır {line}: {len(header)} sütun bekleniyordu, {len(cells)} bulundu")
        rows.append((line, {k: v.strip() for k, v in zip(header, cells, strict=False) if k}))
    return rows


def _canonical(d: Decimal | None) -> str:
    return "" if d is None else format(d.normalize(), "f")


def _parse_row(line: int, values: dict[str, str], portfolio_id: str) -> ImportRow:
    row = ImportRow(line, values)
    ticker = values.get("ticker", "")
    if not ticker:
        row.errors.append("hisse kodu boş")
    try:
        ttype = service.parse_txn_type(values.get("type", ""))
    except ValueError as exc:
        row.errors.append(str(exc))
        ttype = None
    try:
        quantity = parse_tr_number(values.get("quantity", ""))
    except ValueError:
        row.errors.append(f"adet okunamadı: {values.get('quantity', '')!r}")
        quantity = None
    price = fees = None
    if values.get("price"):
        try:
            price = parse_tr_number(values["price"])
        except ValueError:
            row.errors.append(f"fiyat okunamadı: {values['price']!r}")
    try:
        fees = parse_tr_number(values["fees"]) if values.get("fees") else Decimal(0)
    except ValueError:
        row.errors.append(f"komisyon okunamadı: {values['fees']!r}")
    try:
        trade_date = parse_date_tr(values.get("date", ""))
    except ValueError as exc:
        row.errors.append(str(exc))
        trade_date = None
    if row.errors:
        row.status = "error"
        return row
    assert ttype is not None and quantity is not None and fees is not None and trade_date is not None
    row.proposal = service.ProposedTxn(
        portfolio_id=portfolio_id,
        ticker=ticker,
        type=ttype,  # type: ignore[arg-type]
        quantity=quantity,
        price=price,
        fees=fees,
        trade_date=trade_date,
        source="csv_template",
        note=values.get("note") or None,
    )
    return row


def import_csv(
    conn: Connection, text: str, *, portfolio_id: str | None = None, dry_run: bool = False
) -> ImportResult:
    """Preview (``dry_run``) or import a template file atomically.

    Rows are validated and inserted in (date, line) order inside a savepoint so that sells see earlier
    buys from the same file. Any error row aborts the whole import; ``dry_run`` always rolls back.
    One ``portfolio.changed.v1`` (reason ``import``) is published for the inserted rows.
    """
    pid = portfolio_id or service.default_portfolio_id(conn)
    rows = [_parse_row(line, values, pid) for line, values in read_rows(text)]
    occurrences: dict[str, int] = {}
    for row in rows:
        p = row.proposal
        if p is None:
            continue
        canon = "|".join(
            [
                pid,
                service._ticker_key(p.ticker or ""),
                p.type,
                _canonical(p.quantity),
                _canonical(p.price),
                p.trade_date.isoformat(),
                _canonical(p.fees),
                p.note or "",
            ]
        )
        n = occurrences.get(canon, 0)
        occurrences[canon] = n + 1
        row.dedupe_key = "csv:" + hashlib.sha256(f"{canon}|{n}".encode()).hexdigest()

    result = ImportResult(rows, dry_run)
    savepoint = conn.begin_nested()
    instrument_ids: list[str] = []
    try:
        for row in sorted((r for r in rows if r.proposal), key=lambda r: (r.proposal.trade_date, r.line)):  # type: ignore[union-attr]
            assert row.proposal is not None and row.dedupe_key is not None
            if service._txn_by_dedupe(conn, row.dedupe_key):
                row.status = "duplicate"
                continue
            checked = service.validate_proposal(conn, row.proposal)
            row.proposal = checked
            if checked.instrument_id is None:
                row.status = "error"
                row.errors.extend(checked.warnings)
                continue
            row.warnings = list(checked.warnings)
            try:
                tid, _pid, iid = service._insert_txn(conn, checked, dedupe_key=row.dedupe_key)
            except ValueError as exc:
                row.status = "error"
                row.errors.append(str(exc))
                continue
            row.txn_id = tid
            instrument_ids.append(iid or "")
    except BaseException:
        savepoint.rollback()
        raise
    if dry_run or not result.ok:
        savepoint.rollback()
        for row in rows:
            row.txn_id = None
        return result
    savepoint.commit()
    result.inserted = [r.txn_id for r in sorted(rows, key=lambda r: r.line) if r.txn_id]
    if result.inserted:
        service._publish_changed(conn, pid, instrument_ids, result.inserted, "import")
    return result


def parse_quantities(text: str) -> dict[str, Decimal]:
    """İşCep quantities for reconcile: ``ticker,quantity`` CSV (``;``/tab ok, header optional) or
    whitespace-separated ``THYAO 300`` lines. Repeated tickers are an error."""
    out: dict[str, Decimal] = {}
    lines = [
        ln for ln in text.lstrip("\ufeff").splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if lines and not any(d in lines[0] for d in (",", ";", "\t")):
        pairs = [(i, ln.split()) for i, ln in enumerate(lines, 1)]
        parsed = [(i, {"ticker": p[0], "quantity": p[1] if len(p) > 1 else ""}) for i, p in pairs]
        if parsed and normalize_tr(parsed[0][1]["ticker"]) in ("ticker", "hisse"):
            parsed = parsed[1:]
    else:
        parsed = read_rows(text, ("ticker", "quantity"))
    for line, values in parsed:
        ticker = service._ticker_key(values.get("ticker", ""))
        if not ticker:
            raise ValueError(f"satır {line}: hisse kodu boş")
        try:
            qty = parse_tr_number(values.get("quantity", ""))
        except ValueError:
            raise ValueError(f"satır {line}: adet okunamadı: {values.get('quantity', '')!r}") from None
        if ticker in out:
            raise ValueError(f"satır {line}: {ticker} birden fazla kez yazılmış")
        out[ticker] = qty
    return out
