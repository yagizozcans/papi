"""``papi thesis`` — write a thesis from the fixed template or import a paper, check, list."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from papi.core.timeutil import ist_date
from papi.thesis import document as doc
from papi.thesis import importer
from papi.thesis.extract import SUPPORTED

app = typer.Typer(help="Yatırım tezleri: şablon, yükleme, kontrol, liste.", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False)


@app.command()
def new(
    ticker: Annotated[str, typer.Argument(help="BIST kodu, ör. THYAO.")],
    day: Annotated[str | None, typer.Option("--date", help="YYYY-AA-GG (varsayılan bugün).")] = None,
) -> None:
    """Create a thesis file from the template and print its path."""
    code = ticker.strip().upper()
    if not doc.TICKER_RE.match(code):
        raise typer.BadParameter("BIST kodu olmalı (ör. THYAO)")
    when = date.fromisoformat(day) if day else ist_date()
    path = doc.new_path(code, when)
    path.write_text(doc.template(code, when), encoding="utf-8")
    console.print(str(path))


@app.command("import")
def import_(
    files: Annotated[list[Path], typer.Argument(help="PDF, görüntü (JPG/PNG/HEIC), DOCX veya TXT; sırayla.")],
    ticker: Annotated[str, typer.Option(help="BIST kodu, ör. THYAO.")],
    direction: Annotated[str, typer.Option(help="long | short | izle")] = "long",
    horizon: Annotated[str, typer.Option(help="Vade, ör. '6-12 ay' (boş: belirtilmedi).")] = "",
    day: Annotated[str | None, typer.Option("--date", help="YYYY-AA-GG (varsayılan bugün).")] = None,
    name: Annotated[str | None, typer.Option(help="Tez adı (panel kendi ayırdığını verir).")] = None,
    analyse: Annotated[bool, typer.Option("--run/--no-run", help="Sonra strategy-analyst çalışsın.")] = True,
) -> None:
    """Extract a paper page by page, build the thesis file and (default) run the strategy analyst."""
    code = ticker.strip().upper()
    if not doc.TICKER_RE.match(code):
        raise typer.BadParameter("BIST kodu olmalı (ör. THYAO)")
    if direction not in doc.DIRECTIONS:
        raise typer.BadParameter(f"direction: {' | '.join(doc.DIRECTIONS)}")
    for f in files:
        if not f.is_file() or f.suffix.lower() not in SUPPORTED:
            raise typer.BadParameter(f"{f}: yok ya da desteklenmeyen tür ({', '.join(sorted(SUPPORTED))})")
    when = date.fromisoformat(day) if day else ist_date()
    name = name or doc.new_path(code, when).stem
    console.print(f"{name}: {len(files)} dosya okunuyor…")
    st = importer.run_import(
        name, files, ticker=code, direction=direction, horizon=horizon, day=when, analyse=analyse
    )
    console.print(f"{name} · {st['status']} · {st.get('pages_done', 0)} sayfa · {doc.theses_dir() / name}.md")
    for key, label in (("error", "HATA"), ("errors", "HATA"), ("warnings", "Uyarı"), ("gaps", "Eksik")):
        values = st.get(key)
        for v in [values] if isinstance(values, str) else values or []:
            console.print(f"  {label}: {v}")
    if st.get("run_id"):
        console.print(f"ajan çalışması: {st['run_id']} ({st.get('run_status')})")
    if st["status"] in ("failed", "needs_fix"):
        raise typer.Exit(1)


@app.command()
def check(ref: Annotated[str, typer.Argument(help="Tez adı (ör. THYAO-2026-11-06) veya .md yolu.")]) -> None:
    """Check a thesis deterministically: errors stop a review, gaps are reported."""
    try:
        t = doc.load(ref)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except doc.ThesisError as exc:
        console.print("HATA (analiz edilemez):")
        for e in exc.errors:
            console.print(f"  - {e}")
        raise typer.Exit(1) from exc
    filled = sum(1 for s in doc.SECTIONS if t.sections.get(s.key))
    console.print(
        f"{t.ref} · {t.ticker} · {t.direction} · {t.horizon} · {filled}/{len(doc.SECTIONS)} bölüm · "
        f"{len(t.triggers)} tetikleyici · {len(t.kill_signals)} çürütücü sinyal"
    )
    if t.gaps:
        console.print("Eksikler:")
        for g in t.gaps:
            console.print(f"  - {g}")
    else:
        console.print("Eksik yok.")


@app.command("list")
def list_() -> None:
    """Theses in $PAPI_HOME/theses."""
    for path in sorted(doc.theses_dir().glob("*.md")):
        try:
            t = doc.parse(path.read_text(encoding="utf-8"), path.stem)
            state = f"{len(t.gaps)} eksik" if t.gaps else "tam"
        except doc.ThesisError as exc:
            state = f"{len(exc.errors)} hata"
        console.print(f"{path.stem}  {state}")
