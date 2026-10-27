"""``papi triage`` — rules triage tools (PRD 04 §3)."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import timedelta
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from papi.core.db import connect
from papi.core.schema import disclosure, event, source
from papi.core.timeutil import now_utc
from papi.triage import subject_map as sm_mod
from papi.triage import taxonomy as tax_mod
from papi.triage.classify import DisclosureNotFound, classify

app = typer.Typer(help="Kural tabanlı sınıflandırma (triage).", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False)


def _resolve_disclosure(conn: Any, disclosure_id: str | None, source_ref: str | None) -> str:
    if disclosure_id:
        return disclosure_id
    row = conn.execute(
        select(disclosure.c.id)
        .select_from(disclosure.join(source, disclosure.c.source_id == source.c.id))
        .where(source.c.code == "kap", disclosure.c.source_ref == str(source_ref))
    ).first()
    if row is None:
        raise typer.BadParameter(f"KAP bildirimi bulunamadı: {source_ref}", param_hint="--source-ref")
    return row.id


@app.command("classify")
def classify_cmd(
    disclosure_id: Annotated[str | None, typer.Argument(help="disclosure.id")] = None,
    source_ref: Annotated[
        str | None, typer.Option("--source-ref", help="KAP bildirim no (disclosureIndex)")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON çıktı")] = False,
) -> None:
    """Bir bildirimi sınıflandır ve sonucu yazdır (kuru çalıştırma; veritabanına yazmaz)."""
    if not disclosure_id and not source_ref:
        raise typer.BadParameter("disclosure_id veya --source-ref gerekli")
    with connect() as conn:
        did = _resolve_disclosure(conn, disclosure_id, source_ref)
        try:
            drafts = classify(conn, did)
        except DisclosureNotFound as exc:
            raise typer.BadParameter(f"bildirim bulunamadı: {exc}") from exc
        conn.rollback()
    if as_json:
        out = [
            {
                "code": d.code,
                "priority": d.priority,
                "materiality": d.materiality,
                "materiality_band": d.materiality_band,
                "headline_tr": d.headline_tr,
                "tickers": d.tickers,
                "payload": d.payload,
                "payload_schema": d.payload_schema,
                "payload_error": d.payload_error,
                "payload_spans": d.payload_spans,
                "rules": d.rules,
            }
            for d in drafts
        ]
        console.print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return
    console.print(f"bildirim {did}: {len(drafts)} olay")
    for d in drafts:
        console.print(f"\n[{d.priority}] {d.code}  önem {d.materiality} ({d.materiality_band})")
        console.print(f"  başlık: {d.headline_tr}")
        r = d.rules
        console.print(
            f"  eşleşme: {r['mapped_by']} → {', '.join(r['rule_ids'])} (metin: {r['matched_on'] or '-'})"
        )
        m = r["materiality"]
        console.print(
            f"  önem: öncül {m['prior']} × boyut {m['size']:g} ({m['size_basis'] or 'yok'})"
            f" × yenilik {m['novelty']:g} × bayrak {m['flags']:g} {list(m['flag_names']) or ''}"
        )
        console.print(f"  öncelik nedeni: {r['priority']['reason']}")
        if d.payload is not None:
            console.print("  payload: " + json.dumps(d.payload, ensure_ascii=False, default=str))
        elif d.payload_error:
            console.print(f"  payload yok: {d.payload_error}")


@app.command("stats")
def stats_cmd(
    days: Annotated[int, typer.Option("--days", help="Son N gün (0 = tümü)")] = 30,
    limit: Annotated[int, typer.Option("--limit", help="Gösterilecek kod sayısı")] = 40,
) -> None:
    """Olay kodu dağılımı ve other.unclassified payı."""
    tax = tax_mod.load()
    with connect() as conn:
        q = select(event.c.code, event.c.primary_disclosure_id).where(event.c.superseded_by.is_(None))
        if days > 0:
            q = q.where(event.c.announced_at >= now_utc() - timedelta(days=days))
        rows = conn.execute(q).all()
    if not rows:
        console.print("olay yok")
        return
    codes = Counter(r.code for r in rows)
    by_disc: dict[str, set[str]] = {}
    for r in rows:
        by_disc.setdefault(r.primary_disclosure_id or "", set()).add(r.code)
    other_only = sum(1 for cs in by_disc.values() if all(c.startswith("other.") for c in cs))
    table = Table("kod", "etiket", "adet", "%")
    total = len(rows)
    for code, n in codes.most_common(limit):
        table.add_row(code, tax.label(code), str(n), f"{n / total * 100:.1f}")
    console.print(table)
    unc = codes.get(tax_mod.UNCLASSIFIED, 0)
    console.print(
        f"olay: {total}, bildirim: {len(by_disc)}; other.unclassified: {unc} olay ({unc / total * 100:.1f}%),"
        f" yalnız 'other' kodlu bildirim payı: {other_only / len(by_disc) * 100:.1f}%"
    )


@app.command("subjects")
def subjects_cmd(
    days: Annotated[int, typer.Option("--days", help="Son N gün (0 = tümü)")] = 90,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    from_csv: Annotated[
        bool, typer.Option("--from-csv", help="Veritabanı yerine packs/tr/kap_subject_frequencies.csv kullan")
    ] = False,
) -> None:
    """Eşlenmemiş (other.unclassified'a düşen) KAP konuları, sıklığa göre."""
    smap = sm_mod.load()
    counts: Counter[tuple[str, str]] = Counter()
    unclassified: Counter[tuple[str, str]] = Counter()
    if from_csv:
        from papi.core.config import get_settings

        path = get_settings().pack_dir / "kap_subject_frequencies.csv"
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                counts[(row["subject"], row["disclosure_class"])] += int(row["count"])
    else:
        with connect() as conn:
            q = select(disclosure.c.source_subject, disclosure.c.source_class, func.count()).group_by(
                disclosure.c.source_subject, disclosure.c.source_class
            )
            u = (
                select(disclosure.c.source_subject, disclosure.c.source_class, func.count())
                .select_from(disclosure.join(event, event.c.primary_disclosure_id == disclosure.c.id))
                .where(event.c.code == tax_mod.UNCLASSIFIED, event.c.superseded_by.is_(None))
                .group_by(disclosure.c.source_subject, disclosure.c.source_class)
            )
            if days > 0:
                since = now_utc() - timedelta(days=days)
                q = q.where(disclosure.c.published_at >= since)
                u = u.where(disclosure.c.published_at >= since)
            for subj, cls, n in conn.execute(q):
                counts[(subj or "", cls or "")] += n
            for subj, cls, n in conn.execute(u):
                unclassified[(subj or "", cls or "")] += n
    total = sum(counts.values())
    if not total:
        console.print("bildirim yok")
        return
    rows = []
    for key, n in counts.items():
        mapped = smap.is_mapped(key[0])
        if not mapped or unclassified.get(key):
            rows.append((key, n, mapped, unclassified.get(key, 0)))
    rows.sort(key=lambda r: (-(r[3] if r[2] else r[1]), r[0]))
    table = Table("konu", "sınıf", "adet", "eşlenmiş", "other.unclassified")
    for (subj, cls), n, mapped, unc in rows[:limit]:
        table.add_row(
            subj or "(boş)", cls, str(n), "evet" if mapped else "hayır", str(unc) if mapped else "-"
        )
    console.print(table)
    unmapped_volume = sum(n for _, n, mapped, _ in rows if not mapped)
    console.print(
        f"toplam {total} bildirim; haritada olmayan konu hacmi {unmapped_volume}"
        f" ({unmapped_volume / total * 100:.1f}%)"
    )
