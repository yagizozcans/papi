"""``papi prices`` — günlük fiyat verisi (İş Yatırım + Yahoo) senkronizasyonu ve kontrolü."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from papi.core import db
from papi.core.timeutil import ist_date
from papi.sources.prices import service

app = typer.Typer(
    help="Günlük fiyat verisi: senkronizasyon, görüntüleme, çapraz kontrol.", no_args_is_help=True
)
console = Console(soft_wrap=True, markup=False, highlight=False)


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"tarih YYYY-AA-GG olmalı: {value}") from exc


def _num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _print_report(report: dict[str, Any]) -> None:
    c = report["counts"]
    console.print(
        f"Çapraz kontrol ({report['primary']} / {report['secondary'] or '—'}, "
        f"tolerans %{report['tolerance_pct']}): "
        f"uyumlu {c['ok']}, uyumsuz {c['mismatch']}, tek kaynak {c['single_source']}"
    )
    if report["mismatches"]:
        t = Table("Sembol", "Tarih", report["primary"], report["secondary"], "Fark %", title="Uyumsuzluklar")
        for m in report["mismatches"]:
            t.add_row(
                m["ticker"],
                m["date"],
                _num(m["primary_close"]),
                _num(m["secondary_close"]),
                _num(m["diff_pct"], 3),
            )
        console.print(t)
    for key, label in (("missing_primary", report["primary"]), ("missing_secondary", report["secondary"])):
        if report[key]:
            items = ", ".join(f"{x['ticker']} {x['date']}" for x in report[key])
            console.print(f"{label} kaynağında eksik: {items}")


@app.command("sync")
def sync_cmd(
    tickers: str = typer.Option(..., "--tickers", help="Virgülle ayrılmış semboller, ör. THYAO,ASELS,XU100"),
    from_: str | None = typer.Option(None, "--from", help="Başlangıç (YYYY-AA-GG); varsayılan 30 gün önce"),
    to: str | None = typer.Option(None, "--to", help="Bitiş (YYYY-AA-GG); varsayılan bugün"),
) -> None:
    """Fiyatları kaynaklardan indirip kaydet ve çapraz kontrol et."""
    today = ist_date()
    end = _parse_date(to, today)
    start = _parse_date(from_, end - timedelta(days=30))
    if start > end:
        raise typer.BadParameter("--from, --to tarihinden sonra olamaz")
    with db.transaction() as conn:
        found, unknown = service.resolve_tickers(conn, tickers.split(","))
    if unknown:
        console.print(
            f"Bilinmeyen sembol(ler): {', '.join(unknown)} — önce şirket listesini senkronize edin."
        )
    if not found:
        raise typer.Exit(1)
    result = service.sync_prices_isolated(list(found.values()), start, end)
    console.print(f"{start.isoformat()} → {end.isoformat()}: {', '.join(result['instruments'])}")
    for code, n in result["rows"].items():
        console.print(f"  {code}: {n} satır")
    for e in result["errors"]:
        console.print(f"  HATA {e['ticker']} ({e['source']}): {e['outcome']} — {e['error']}")
    _print_report(result["cross_check"])
    if result["errors"]:
        raise typer.Exit(2)


@app.command("show")
def show_cmd(
    ticker: str = typer.Argument(..., help="Sembol, ör. THYAO"),
    days: int = typer.Option(20, "--days", help="Kaç takvim günü geriye"),
) -> None:
    """Bir sembolün son günlük kapanışlarını kaynak bazında göster."""
    with db.connect() as conn:
        iid = service.instrument_for_ticker(conn, ticker)
        if iid is None:
            console.print(f"Bilinmeyen sembol: {ticker.upper()}")
            raise typer.Exit(1)
        end = ist_date()
        data = service.bars(conn, iid, end - timedelta(days=days), end)
        last = service.last_close(conn, iid)
    codes = service.source_codes()
    t = Table(
        "Tarih",
        *[f"{c} kapanış" for c in codes],
        "Yüksek",
        "Düşük",
        "Hacim (TL)",
        "Kontrol",
        title=ticker.upper(),
    )
    for ts, by_src in sorted(data.items(), reverse=True):
        first = next((by_src[c] for c in codes if c in by_src), next(iter(by_src.values())))
        value = next(
            (by_src[c]["value_traded"] for c in codes if c in by_src and by_src[c]["value_traded"]), None
        )
        t.add_row(
            ts.isoformat(),
            *[_num(by_src[c]["close"]) if c in by_src else "—" for c in codes],
            _num(first["high"]),
            _num(first["low"]),
            _num(value, 0),
            first["cross_check"] or "—",
        )
    console.print(t)
    if last:
        console.print(f"Son kapanış: {_num(last[1])} ({last[0].isoformat()})")
    else:
        console.print("Fiyat verisi yok.")


@app.command("check")
def check_cmd(
    days: int = typer.Option(30, "--days", help="Kaç takvim günü geriye"),
    tickers: str | None = typer.Option(None, "--tickers", help="Yalnızca bu semboller (varsayılan: tümü)"),
) -> None:
    """Kayıtlı fiyatlarda birincil/ikincil kaynak çapraz kontrol raporu."""
    end = ist_date()
    start = end - timedelta(days=days)
    with db.transaction() as conn:
        if tickers:
            found, unknown = service.resolve_tickers(conn, tickers.split(","), create_indices=False)
            if unknown:
                console.print(f"Bilinmeyen sembol(ler): {', '.join(unknown)}")
            ids = list(found.values())
        else:
            ids = service.priced_instrument_ids(conn, start, end)
        report = service.cross_check(conn, ids, start, end)
    _print_report(report.as_dict())
