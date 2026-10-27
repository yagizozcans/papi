"""``papi portfolio``: trades, positions, CSV import, İşCep reconcile, corporate actions, watchlists."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Connection, select
from sqlalchemy import Table as SaTable

from papi.core import db
from papi.core import schema as s
from papi.core.config import get_settings
from papi.core.text import normalize_tr, parse_tr_number
from papi.core.timeutil import ist_date
from papi.portfolio import csv_import, service
from papi.portfolio.fmt import fmt_date, fmt_num, fmt_pct, fmt_qty, parse_date_tr

app = typer.Typer(
    help="Portföy: işlemler, pozisyonlar, CSV içe aktarma, İşCep mutabakatı, kurumsal işlemler.",
    no_args_is_help=True,
)
console = Console(soft_wrap=True, markup=False, highlight=False)

PortfolioOpt = typer.Option(None, "--portfolio", "-p", help="Portföy adı (varsayılan: ana portföy).")
YesOpt = typer.Option(False, "--yes", "-y", help="Onay sormadan uygula.")


def _fail(msg: str) -> None:
    console.print(f"Hata: {msg}", style="red")
    raise typer.Exit(1)


def _ensure_db() -> None:
    if not get_settings().paths.db.exists():
        _fail("Veritabanı bulunamadı; önce `papi init` çalıştırın.")


@contextmanager
def _read() -> Iterator[Connection]:
    _ensure_db()
    with db.connect() as conn:
        try:
            yield conn
        finally:
            if conn.in_transaction():
                conn.rollback()


@contextmanager
def _write() -> Iterator[Connection]:
    _ensure_db()
    with db.transaction() as conn:
        yield conn


def _pid(conn: Connection, name: str | None) -> str:
    try:
        return service.portfolio_id_by_name(conn, name)
    except ValueError as exc:
        _fail(str(exc))
        raise


def _resolve_id(conn: Connection, table: SaTable, token: str, what: str) -> str:
    """Full id, or a unique prefix/suffix of one (ids are long UUIDv7 strings)."""
    token = token.strip()
    if conn.execute(select(table.c.id).where(table.c.id == token)).first():
        return token
    ids = list(
        conn.execute(
            select(table.c.id).where(table.c.id.startswith(token) | table.c.id.endswith(token))
        ).scalars()
    )
    if len(ids) == 1:
        return ids[0]
    _fail(f"{what} bulunamadı: {token}" if not ids else f"{token} birden fazla {what} ile eşleşiyor")
    raise AssertionError


def _num(text: str, what: str) -> Decimal:
    try:
        return parse_tr_number(text)
    except ValueError:
        _fail(f"{what} okunamadı: {text!r}")
        raise


def _is_number(text: str) -> bool:
    try:
        parse_tr_number(text)
    except ValueError:
        return False
    return True


def _parse_date_opt(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return parse_date_tr(text)
    except ValueError as exc:
        _fail(str(exc))
        raise


# --------------------------------------------------------------------------------------------- add / list


def _parse_add_words(words: list[str]) -> dict[str, Any]:
    tokens: list[str] = []
    for w in words:
        w = w.strip()
        if not w or w == "@" or normalize_tr(w) == "lot":
            continue
        tokens.append(w[1:] if w.startswith("@") else w)
    if not tokens:
        _fail("İşlem türü eksik (ör. BUY THYAO 100 @ 285.40).")
    try:
        ttype = service.parse_txn_type(tokens[0])
    except ValueError as exc:
        _fail(str(exc))
        raise
    rest = tokens[1:]
    trade_date = None
    if rest:
        try:
            trade_date = parse_date_tr(rest[-1])
            rest = rest[:-1]
        except ValueError:
            pass
    out: dict[str, Any] = {"type": ttype, "trade_date": trade_date or ist_date(), "ticker": None}
    if ttype in ("fee", "tax"):
        if rest and not _is_number(rest[0]):
            out["ticker"] = rest.pop(0)
        if len(rest) != 1:
            _fail(f"Kullanım: add {tokens[0]} [HİSSE] TUTAR [TARİH]")
        out.update(quantity=Decimal(0), price=None, amount=_num(rest[0], "tutar"))
        return out
    if len(rest) not in (2, 3):
        _fail(f"Kullanım: add {tokens[0]} HİSSE ADET [@] FİYAT [TARİH]")
    out["ticker"] = rest[0]
    out["quantity"] = _num(rest[1], "adet")
    out["price"] = _num(rest[2], "fiyat") if len(rest) == 3 else None
    return out


def _card(p: service.ProposedTxn) -> str:
    label = service.TYPE_LABELS_TR[p.type]
    parts = [label, p.ticker or "—"]
    if p.type not in ("fee", "tax"):
        parts.append(fmt_qty(p.quantity))
    if p.price is not None:
        parts += ["@", fmt_num(p.price, 4, trim=True)]
    text = " ".join(parts) + f" · {fmt_date(p.trade_date)}"
    if p.fees:
        text += f" · {'tutar' if p.type in ('fee', 'tax') else 'komisyon'} {fmt_num(p.fees)}"
    return text


@app.command()
def add(
    words: list[str] = typer.Argument(
        ...,
        metavar="TÜR HİSSE ADET [@] FİYAT [TARİH]",
        help="ör. BUY THYAO 100 @ 285.40 2026-10-01 · SELL ASELS 50 62,00 · FEE 12,5",
    ),
    fees: str = typer.Option("0", "--fees", "--komisyon", help="Komisyon (TL)."),
    note: str | None = typer.Option(None, "--note", "--not", help="Not."),
    portfolio: str | None = PortfolioOpt,
    yes: bool = YesOpt,
) -> None:
    """İşlem ekle (alış/satış/açılış/temettü/masraf …)."""
    parsed = _parse_add_words(words)
    fee_amount = _num(fees, "komisyon")
    if parsed["type"] in ("fee", "tax"):
        fee_amount = parsed["amount"]
    with _read() as conn:
        p = service.ProposedTxn(
            portfolio_id=_pid(conn, portfolio),
            ticker=parsed["ticker"],
            type=parsed["type"],
            quantity=parsed["quantity"],
            price=parsed["price"],
            fees=fee_amount,
            trade_date=parsed["trade_date"],
            source="manual",
            note=note,
        )
        checked = service.validate_proposal(conn, p)
    console.print(_card(checked))
    for w in checked.warnings:
        console.print(f"⚠ {w}", style="yellow")
    if checked.ticker and checked.instrument_id is None:
        _fail("Hisse kodu çözümlenemedi.")
    if checked.warnings and not yes and not typer.confirm("Uyarılara rağmen kaydedilsin mi?", default=False):
        raise typer.Exit(1)
    with _write() as conn:
        try:
            tid = service.record_transaction(conn, checked)
        except ValueError as exc:
            _fail(str(exc))
        pos = (
            service.holding(conn, checked.instrument_id, checked.portfolio_id)
            if checked.instrument_id
            else None
        )
    msg = f"Kaydedildi ({tid})."
    if checked.instrument_id:
        if pos is None:
            msg += f" {checked.ticker}: pozisyon kapandı."
        else:
            msg += f" {pos.ticker}: {fmt_qty(pos.quantity)} pay"
            if pos.avg_cost is not None:
                msg += f", ort. maliyet {fmt_num(pos.avg_cost)}"
            if pos.weight is not None:
                msg += f", portföy ağırlığı {fmt_pct(pos.weight * 100, 0)}"
            msg += "."
    console.print(msg)


@app.command("list")
def list_positions(
    portfolio: str | None = PortfolioOpt,
    as_of: str | None = typer.Option(None, "--as-of", help="Tarih (GG.AA.YYYY / YYYY-AA-GG)."),
) -> None:
    """Pozisyonlar: adet, ortalama maliyet, son kapanış, değer, ağırlık, K/Z."""
    d = _parse_date_opt(as_of) or ist_date()
    with _read() as conn:
        pid = _pid(conn, portfolio)
        rows = service.positions(conn, pid, d)
        realised = service.realised_summary(conn, pid, d)
        n_pending = len([a for a in service.pending_actions(conn) if a["portfolio_id"] == pid])
    if not rows:
        console.print(f"Pozisyon yok ({fmt_date(d)}).")
    else:
        table = Table(title=f"Pozisyonlar · {fmt_date(d)}")
        for col in (
            "Hisse",
            "Adet",
            "Ort. maliyet",
            "Maliyet",
            "Son kapanış",
            "Değer",
            "Ağırlık",
            "K/Z",
            "K/Z %",
        ):
            table.add_column(col, justify="left" if col == "Hisse" else "right")
        total_cost = total_value = Decimal(0)
        for p in rows:
            pnl = p.market_value - p.cost_total if p.market_value is not None else None
            pnl_pct = float(pnl / p.cost_total * 100) if pnl is not None and p.cost_total > 0 else None
            close = (
                f"{fmt_num(p.last_close)} ({fmt_date(p.last_close_date)})"
                if p.last_close is not None
                else "—"
            )
            table.add_row(
                p.ticker,
                fmt_qty(p.quantity),
                fmt_num(p.avg_cost),
                fmt_num(p.cost_total),
                close,
                fmt_num(p.market_value),
                fmt_pct(p.weight * 100 if p.weight is not None else None),
                fmt_num(pnl),
                fmt_pct(pnl_pct),
            )
            total_cost += p.cost_total
            total_value += p.market_value or 0
        table.add_section()
        table.add_row("Toplam", "", "", fmt_num(total_cost), "", fmt_num(total_value), "", "", "")
        console.print(table)
    console.print(
        f"Gerçekleşen K/Z: {fmt_num(realised['realised_pnl'])} TL · "
        f"Temettü geliri (net): {fmt_num(realised['income'])} TL"
    )
    if n_pending:
        console.print(f"{n_pending} bekleyen kurumsal işlem var: papi portfolio pending")


@app.command()
def txns(
    portfolio: str | None = PortfolioOpt,
    ticker: str | None = typer.Option(None, "--ticker", "-t", help="Sadece bu hisse."),
    limit: int = typer.Option(50, "--limit", "-n", help="En fazla satır."),
) -> None:
    """İşlem defteri (en yeni üstte)."""
    with _read() as conn:
        pid = _pid(conn, portfolio)
        iid = None
        if ticker:
            matches = service.resolve_instrument(conn, ticker)
            if not matches or matches[0].score < 1:
                _fail(f"Bilinmeyen hisse kodu: {ticker}")
            iid = matches[0].instrument_id
        rows = service.transactions(conn, pid, limit=limit, instrument_id=iid)
    if not rows:
        console.print("İşlem yok.")
        return
    table = Table(title="İşlemler")
    for col in (
        "ID",
        "Tarih",
        "Tür",
        "Hisse",
        "Adet",
        "Fiyat",
        "Komisyon",
        "Vergi",
        "Kaynak",
        "Durum",
        "Not",
    ):
        table.add_column(col, justify="right" if col in ("Adet", "Fiyat", "Komisyon", "Vergi") else "left")
    for r in rows:
        state = ""
        if r["reverses_id"]:
            state = f"ters kayıt → {r['reverses_id']}"
        elif r["reversed_by"]:
            state = "geri alındı"
        table.add_row(
            r["id"],
            fmt_date(r["trade_date"]),
            service.TYPE_LABELS_TR.get(r["type"], r["type"]),
            r["ticker"] or "—",
            fmt_qty(r["quantity"]),
            fmt_num(r["price"], 4, trim=True) if r["price"] is not None else "—",
            fmt_num(r["fees"]),
            fmt_num(r["tax"]),
            r["source"],
            state,
            r["note"] or "",
        )
    console.print(table)


@app.command()
def undo(
    txn_id: str = typer.Argument(..., help="İşlem id'si (ya da benzersiz başı/sonu)."),
    reason: str | None = typer.Option(None, "--reason", "--neden", help="Neden."),
) -> None:
    """Bir işlemi ters kayıtla geri al (defter silinmez)."""
    with _write() as conn:
        tid = _resolve_id(conn, s.txn, txn_id, "işlem")
        try:
            rid = service.reverse_transaction(conn, tid, reason=reason)
        except ValueError as exc:
            _fail(str(exc))
    console.print(f"Geri alındı: {tid} (ters kayıt {rid}).")


# ------------------------------------------------------------------------------------- import / template


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1254")  # Turkish Windows/Excel export


def _import_table(result: csv_import.ImportResult) -> Table:
    labels = {"new": "yeni", "duplicate": "zaten var", "error": "HATA"}
    table = Table(title="İçe aktarma önizlemesi")
    for col in ("Satır", "Hisse", "Tür", "Adet", "Fiyat", "Tarih", "Komisyon", "Durum", "Uyarı / hata"):
        table.add_column(col)
    for r in sorted(result.rows, key=lambda r: r.line):
        p = r.proposal
        table.add_row(
            str(r.line),
            (p.ticker if p else r.values.get("ticker")) or "",
            service.TYPE_LABELS_TR.get(p.type, p.type) if p else r.values.get("type", ""),
            fmt_qty(p.quantity) if p else r.values.get("quantity", ""),
            fmt_num(p.price, 4, trim=True) if p and p.price is not None else r.values.get("price", ""),
            fmt_date(p.trade_date) if p else r.values.get("date", ""),
            fmt_num(p.fees) if p else r.values.get("fees", ""),
            labels[r.status],
            "; ".join(r.errors + r.warnings),
        )
    return table


@app.command("import")
def import_cmd(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="papi CSV şablonu."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Sadece önizle, kaydetme."),
    portfolio: str | None = PortfolioOpt,
    yes: bool = YesOpt,
) -> None:
    """papi CSV şablonunu içe aktar (önizleme + tekrar korumalı)."""
    text = _read_text(path)
    try:
        with _read() as conn:
            pid = _pid(conn, portfolio)
            preview = csv_import.import_csv(conn, text, portfolio_id=pid, dry_run=True)
    except ValueError as exc:
        _fail(str(exc))
    console.print(_import_table(preview))
    new, dup, err = preview.count("new"), preview.count("duplicate"), preview.count("error")
    console.print(f"{new} yeni, {dup} zaten kayıtlı, {err} hatalı satır.")
    if dry_run:
        return
    if err:
        _fail("Hatalı satırlar var; hiçbir satır kaydedilmedi.")
    if not new:
        console.print("Eklenecek yeni satır yok.")
        return
    if not yes and not typer.confirm(f"{new} işlem kaydedilsin mi?", default=True):
        raise typer.Exit(1)
    with _write() as conn:
        result = csv_import.import_csv(conn, text, portfolio_id=pid)
    if not result.ok:
        _fail("İçe aktarma sırasında hata; hiçbir satır kaydedilmedi.")
    console.print(f"{len(result.inserted)} işlem kaydedildi.")


@app.command()
def template(
    out: Path | None = typer.Option(None, "--out", "-o", help="Dosyaya yaz (yoksa ekrana)."),
) -> None:
    """CSV şablonunu (başlık + örnek satırlar) yazdır."""
    if out is None:
        typer.echo(csv_import.TEMPLATE, nl=False)
        return
    if out.exists():
        _fail(f"{out} zaten var.")
    out.write_text(csv_import.TEMPLATE, encoding="utf-8")
    console.print(f"Şablon yazıldı: {out}")


# --------------------------------------------------------------------------------------------- reconcile


def _prompt_observed(conn: Connection, pid: str) -> dict[str, Decimal]:
    observed: dict[str, Decimal] = {}
    console.print("İşCep'te gördüğünüz adetleri girin (Enter = papi ile aynı).")
    for p in service.positions(conn, pid):
        answer = typer.prompt(f"{p.ticker} (papi {fmt_qty(p.quantity)})", default=fmt_qty(p.quantity))
        observed[p.ticker] = _num(answer, "adet")
    while True:
        ticker = typer.prompt("papi'de olmayan başka hisse (boş = bitti)", default="", show_default=False)
        if not ticker.strip():
            return observed
        observed[ticker.strip()] = _num(typer.prompt(f"{ticker.strip()} adet"), "adet")


@app.command("reconcile")
def reconcile_cmd(
    file: Path | None = typer.Option(
        None, "--file", "-f", exists=True, dir_okay=False, help="ticker,quantity dosyası (İşCep adetleri)."
    ),
    portfolio: str | None = PortfolioOpt,
    yes: bool = YesOpt,
) -> None:
    """İşCep adetleriyle karşılaştır; farklar için mutabakat düzeltmesi öner."""
    with _read() as conn:
        pid = _pid(conn, portfolio)
        if file is not None:
            try:
                observed = csv_import.parse_quantities(_read_text(file))
            except ValueError as exc:
                _fail(str(exc))
        else:
            observed = _prompt_observed(conn, pid)
        diffs = service.reconcile(conn, observed, pid)
    if not diffs:
        console.print("Fark yok — papi ile İşCep uyumlu.")
        return
    table = Table(title="Mutabakat farkları")
    for col in ("Hisse", "papi", "İşCep", "Fark", "Not"):
        table.add_column(col, justify="right" if col in ("papi", "İşCep", "Fark") else "left")
    for d in diffs:
        delta = f"{'+' if d.delta > 0 else ''}{fmt_qty(d.delta)}"
        table.add_row(d.ticker, fmt_qty(d.papi_quantity), fmt_qty(d.observed_quantity), delta, d.hint or "")
    console.print(table)
    applicable = [d for d in diffs if d.instrument_id]
    if not applicable:
        _fail("Uygulanabilir fark yok (bilinmeyen hisse kodları).")
    if not yes and not typer.confirm(
        f"{len(applicable)} fark için mutabakat düzeltmesi kaydedilsin mi?", default=False
    ):
        console.print("Kaydedilmedi.")
        return
    with _write() as conn:
        ids = service.apply_reconcile(conn, service.reconcile(conn, observed, pid))
    console.print(f"{len(ids)} mutabakat düzeltmesi kaydedildi.")


# --------------------------------------------------------------------------------------- pending actions


@app.command()
def pending(all_: bool = typer.Option(False, "--all", "-a", help="Sonuçlanmışları da göster.")) -> None:
    """Onay bekleyen kurumsal işlemler (bedelsiz, bedelli, temettü, bölünme)."""
    with _read() as conn:
        rows = service.pending_actions(conn, "all" if all_ else "expected")
    if not rows:
        console.print("Bekleyen kurumsal işlem yok.")
        return
    table = Table(title="Kurumsal işlemler")
    for col in ("ID", "Hisse", "Tür", "Hak kullanım", "Durum", "Öneri"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["id"],
            r["ticker"] or "—",
            r["kind"],
            fmt_date(r["ex_date"]),
            r["status"],
            (r["proposal"] or {}).get("text_tr", ""),
        )
    console.print(table)


@app.command()
def apply(
    pending_id: str = typer.Argument(..., help="Bekleyen işlem id'si (ya da benzersiz başı/sonu)."),
    quantity: str | None = typer.Option(
        None,
        "--quantity",
        "--adet",
        help="İşlem adedini İşCep'e göre değiştir: alınan bedelsiz/bedelli pay, bölünme farkı "
        "ya da temettü alan pay.",
    ),
) -> None:
    """Bekleyen kurumsal işlemi onayla ve türetilmiş işlemi kaydet."""
    override = _num(quantity, "adet") if quantity is not None else None
    with _write() as conn:
        paid = _resolve_id(conn, s.pending_action, pending_id, "bekleyen işlem")
        try:
            tid = service.apply_pending_action(conn, paid, quantity_override=override)
        except ValueError as exc:
            _fail(str(exc))
    console.print(f"Uygulandı: {paid} → işlem {tid}.")


@app.command()
def dismiss(pending_id: str = typer.Argument(..., help="Bekleyen işlem id'si.")) -> None:
    """Bekleyen kurumsal işlemi reddet (ör. rüçhan hakkı kullanılmadı)."""
    with _write() as conn:
        paid = _resolve_id(conn, s.pending_action, pending_id, "bekleyen işlem")
        try:
            service.dismiss_pending_action(conn, paid)
        except ValueError as exc:
            _fail(str(exc))
    console.print(f"Reddedildi: {paid}.")


# --------------------------------------------------------------------------------------------- watchlists

ListOpt = typer.Option("Ana", "--list", "-l", help="İzleme listesi adı.")


@app.command()
def watch(
    ref: str = typer.Argument(..., help="Hisse (THYAO), endeks (XU030 / endeks:XU030) ya da sektor:<kod>."),
    watchlist: str = ListOpt,
) -> None:
    """İzleme listesine ekle."""
    with _write() as conn:
        try:
            service.watch(conn, ref, watchlist)
        except ValueError as exc:
            _fail(str(exc))
    console.print(f"İzleme listesine eklendi ({watchlist}): {ref}")


@app.command()
def unwatch(
    ref: str = typer.Argument(..., help="Hisse, endeks ya da sektor:<kod>."), watchlist: str = ListOpt
) -> None:
    """İzleme listesinden çıkar."""
    with _write() as conn:
        try:
            removed = service.unwatch(conn, ref, watchlist)
        except ValueError as exc:
            _fail(str(exc))
    console.print(f"Çıkarıldı ({watchlist}): {ref}" if removed else f"{ref} {watchlist} listesinde değil.")


@app.command("watchlists")
def watchlists_cmd() -> None:
    """İzleme listeleri."""
    with _read() as conn:
        rows = service.watchlists(conn)
    if not rows:
        console.print("İzleme listesi yok. Ekle: papi portfolio watch THYAO")
        return
    table = Table(title="İzleme listeleri")
    for col in ("Liste", "Hisseler", "Sektörler", "Endeksler"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["name"], ", ".join(r["tickers"]), ", ".join(r["sectors"]), ", ".join(r["indices"]))
    console.print(table)
