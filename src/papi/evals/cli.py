"""``papi eval`` — evaluation suites, baseline diff and gates (PRD 11)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from papi.evals import runner
from papi.evals.core import accept_baseline
from papi.evals.suites import ALL_SUITES

app = typer.Typer(
    help="Değerlendirme paketleri: metrik tablosu, kabul edilen tabana göre fark, eşikler (PRD 11).",
    invoke_without_command=True,
)
console = Console(soft_wrap=True, markup=False, highlight=False)


@app.callback()
def main(
    ctx: typer.Context,
    suite: Annotated[
        list[str] | None, typer.Option("--suite", "-s", help=f"Paket(ler): {', '.join(ALL_SUITES)}")
    ] = None,
    baseline: Annotated[
        str | None, typer.Option("--baseline", help="'accept': mevcut metrikleri yeni taban olarak yaz")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Eşikler geçmese de tabanı yaz")] = False,
    note: Annotated[
        str | None, typer.Option("--note", help="Taban notu (ör. maliyet artışının nedeni)")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON çıktı")] = False,
    allow_claude: Annotated[
        bool,
        typer.Option("--allow-claude", help="Claude çağıran 'analyst' paketine izin ver (abonelik kullanır)"),
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="analyst: en fazla N öğe (N çağrı)")
    ] = None,
    priority: Annotated[
        str | None, typer.Option("--priority", help="analyst: yönetici önceliği (varsayılan BACKLOG)")
    ] = None,
    max_failures: Annotated[int, typer.Option("--failures", help="Paket başına gösterilen örnek hata")] = 12,
) -> None:
    """Paketleri çalıştır; herhangi bir PRD 11 §3 eşiği başarısızsa çıkış kodu 1."""
    if ctx.invoked_subcommand is not None:
        return
    if baseline not in (None, "accept"):
        raise typer.BadParameter("yalnızca 'accept' desteklenir", param_hint="--baseline")
    try:
        result = runner.run(suite, allow_claude=allow_claude, limit=limit, priority=priority)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--suite") from exc
    if as_json:
        runner.render_json(result, console)
    else:
        runner.render(result, console, max_failures=max_failures)
    if baseline == "accept":
        if result.passed or force:
            reason = note or ("forced: gates failing" if not result.passed else None)
            path = accept_baseline(result.results, note=reason)
            console.print(f"taban yazıldı: {path}")
        else:
            console.print("taban yazılmadı: eşikler başarısız (yine de kaydetmek için --force).")
    if not result.passed:
        raise typer.Exit(1)


@app.command("review")
def review_cmd(
    item_ids: Annotated[
        list[str] | None, typer.Argument(help="Öğe kimlikleri (boş: incelenmemişlerin hepsi)")
    ] = None,
    show_all: Annotated[bool, typer.Option("--all", help="İncelenmiş öğeleri de göster")] = False,
) -> None:
    """Analyst altın setini tek tek göster; onaylananları `reviewed: true` yap."""
    from papi.evals.suites import analyst

    items = analyst.load_items()
    if item_ids:
        known = {i.id for i in items}
        missing = [i for i in item_ids if i not in known]
        if missing:
            raise typer.BadParameter(f"bilinmeyen öğe: {', '.join(missing)}")
        items = [i for i in items if i.id in item_ids]
    elif not show_all:
        items = [i for i in items if not i.reviewed]
    if not items:
        console.print("İncelenecek öğe yok.")
        return
    confirmed = 0
    for item in items:
        console.print("")
        for line in analyst.review_lines(item):
            console.print(line)
        if item.reviewed:
            console.print("(zaten incelenmiş)")
            continue
        answer = (
            typer.prompt("Altın doğru mu? [e]vet / [h]ayır / [a]tla / [ç]ık", default="a").strip().lower()
        )
        if answer in ("e", "evet", "y", "yes"):
            analyst.mark_reviewed(item)
            confirmed += 1
            console.print(f"onaylandı: {item.path.name}")
        elif answer in ("h", "hayır", "hayir", "n", "no"):
            console.print(f"düzeltmek için dosyayı düzenleyin: {item.path}")
        elif answer in ("ç", "c", "q", "quit", "çık", "cik"):
            break
    console.print(f"\n{confirmed} öğe onaylandı.")


@app.command("snapshot")
def snapshot_cmd() -> None:
    """Ayrıştırıcı altın görüntülerini yeniden yaz (bilinçli bir ayrıştırıcı değişikliğinden sonra)."""
    from papi.evals.suites import parsers

    status = parsers.write_snapshots()
    changed = {k: v for k, v in status.items() if v != "unchanged"}
    for name, st in sorted(changed.items()):
        console.print(f"{st}: {name}")
    console.print(f"{len(status)} fikstür, {len(changed)} değişiklik.")


@app.command("kb-build")
def kb_build_cmd() -> None:
    """KB fikstürünü (tests/golden/kb/kb.json) tests/kb/fixtures metinlerinden yeniden üret."""
    from papi.evals import kbfixture

    path, counts = kbfixture.write()
    console.print(f"yazıldı: {path}  " + ", ".join(f"{k}={v}" for k, v in counts.items()))
