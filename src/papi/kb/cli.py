"""``papi kb`` — legal corpus fetch/parse, KB snapshots, lookup/search, obligation review (PRD 03)."""

from __future__ import annotations

from datetime import date

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from papi.core import db
from papi.core.schema import provision, provision_version, regulation
from papi.kb import api
from papi.kb import obligation_store as obl
from papi.kb import snapshot as snap
from papi.kb.ingest import ingest_entry, parse_error
from papi.sources.legal.fetch import PoliteClient, document_state, fetch_entries, legal_settings
from papi.sources.legal.manifest import ManifestEntry, load_manifest

app = typer.Typer(help="Mevzuat bilgi bankası (KB): çek, ayrıştır, yayımla, ara.", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False, emoji=False)


def _select(ids: list[str] | None, priority: str | None) -> list[ManifestEntry]:
    try:
        return load_manifest().select(ids=ids or None, priority=priority)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"tarih YYYY-AA-GG olmalı: {value}") from exc


@app.command()
def fetch(
    ids: list[str] = typer.Option(None, "--id", help="Manifest id (tekrarlanabilir); varsayılan: hepsi."),
    priority: str = typer.Option(None, "--priority", help="Yalnızca bu öncelik (P0/P1/P2)."),
    max_requests: int = typer.Option(None, "--max-requests", help="Bu çalıştırmadaki en fazla istek sayısı."),
    history: bool = typer.Option(True, "--history/--no-history", help="Manifestteki eski metinleri de çek."),
) -> None:
    """Resmî kaynaklardan belgeleri indir (sıralı, istekler arası ≥1 sn), değişmeyenleri atla."""
    entries = _select(ids, priority)
    cfg = legal_settings()
    with PoliteClient(max_requests=max_requests or cfg["max_requests"]) as client:
        outcomes = fetch_entries(entries, client=client, include_history=history)
        made = client.requests_made
    table = Table("belge", "rol", "durum", "sha", "url / hata")
    for o in outcomes:
        table.add_row(o.entry_id, o.role, o.status, (o.sha or "")[:12], o.url or o.error or "")
    console.print(table)
    failed = [o for o in outcomes if o.status == "failed"]
    console.print(
        f"{made} istek; {sum(o.status == 'changed' for o in outcomes)} değişti, "
        f"{sum(o.status == 'unchanged' for o in outcomes)} aynı, {len(failed)} başarısız."
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def parse(
    ids: list[str] = typer.Option(
        None, "--id", help="Manifest id (tekrarlanabilir); varsayılan: çekilmiş hepsi."
    ),
    priority: str = typer.Option(None, "--priority", help="Yalnızca bu öncelik (P0/P1/P2)."),
    force: bool = typer.Option(False, "--force", help="İçerik değişmemiş olsa da yeniden ayrıştır."),
) -> None:
    """Çekilmiş metinleri yapılandır (Kısım/Bölüm/Madde/fıkra/bent), sürümleri taslak anlık görüntüye yaz."""
    entries = _select(ids, priority)
    table = Table("belge", "durum", "hüküm", "yeni sürüm", "tarihsel", "düşük güven", "süre", "not")
    failures = 0
    for e in entries:
        r = ingest_entry(e, force=force)
        if r.status in ("failed",):
            failures += 1
        if r.status == "not_fetched" and not ids:
            continue
        table.add_row(
            e.id,
            r.status,
            str(r.provisions),
            str(r.new_versions),
            str(r.history_versions),
            str(r.low_confidence),
            f"{r.elapsed_s or 0:.1f}s",
            r.error or ("; ".join(r.flags)[:60] if r.flags else ""),
        )
    console.print(table)
    with db.transaction() as conn:
        rep = obl.load_obligations(conn)
    console.print(
        f"yükümlülükler: {rep.inserted} eklendi, {rep.updated} güncellendi, {rep.unchanged} aynı"
        + (f"; onayı sıfırlanan: {', '.join(rep.approvals_reset)}" if rep.approvals_reset else "")
        + (f"; hükmü henüz yok: {len(rep.missing_provisions)}" if rep.missing_provisions else "")
    )
    if failures:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Belgeler, hüküm sayıları ve anlık görüntüler."""
    manifest = load_manifest()
    with db.connect() as conn:
        regs = {(r.extra or {}).get("manifest_id"): r for r in conn.execute(select(regulation)).all()}
        counts = dict(
            conn.execute(
                select(provision.c.regulation_id, func.count(provision.c.id)).group_by(
                    provision.c.regulation_id
                )
            ).all()
        )
        vcounts = dict(
            conn.execute(
                select(provision.c.regulation_id, func.count(provision_version.c.id))
                .select_from(provision_version.join(provision))
                .group_by(provision.c.regulation_id)
            ).all()
        )
        table = Table(
            "belge", "öncelik", "çekildi", "sha", "hüküm", "sürüm", "tarihli", "düşük güven", "hata"
        )
        for e in manifest.documents:
            st = document_state(conn, e.id)
            reg = regs.get(e.id)
            extra = (reg.extra or {}) if reg is not None else {}
            table.add_row(
                e.id,
                e.priority,
                (st.get("fetched_at") or "-")[:16],
                (st.get("sha") or "")[:10],
                str(counts.get(reg.id, 0)) if reg is not None else "-",
                str(vcounts.get(reg.id, 0)) if reg is not None else "-",
                str(extra.get("counts", {}).get("dated", "-")),
                str(len(extra.get("low_confidence", []))) if reg is not None else "-",
                (st.get("last_error") or parse_error(conn, e.id) or "")[:50],
            )
        console.print(table)
        stable = Table("anlık görüntü", "durum", "etiket", "oluşturma", "yayım", "yeni sürüm")
        for s in snap.list_snapshots(conn):
            stats = s.get("stats") or {}
            added = stats.get("versions_added")
            if added is None:
                added = snap.snapshot_stats(conn, s["id"])["versions_added"]
            stable.add_row(
                s["id"],
                s["status"],
                stats.get("label", ""),
                str(s["created_at"])[:16],
                str(s["published_at"] or "")[:16],
                str(added),
            )
        console.print(stable)
        console.print(f"güncel yayımlanmış: {api.current_snapshot(conn) or 'yok'}")


def _print_version(v: api.ProvisionVersion) -> None:
    console.print(f"{v.citation_id}  ({v.label_tr()})")
    if v.heading:
        console.print(f"Başlık: {v.heading}")
    eff = v.effective_from.isoformat() if v.effective_from else "bilinmiyor (mevzuat sürümü belirsiz)"
    console.print(f"Yürürlük: {eff} → {v.effective_to.isoformat() if v.effective_to else 'açık'}")
    if v.amended_by:
        console.print(f"Değişiklik: {v.amended_by}")
    console.print(f"Sürüm: {v.id}  anlık görüntü: {v.kb_snapshot_id}")
    console.print("")
    console.print(v.text)


@app.command()
def show(
    citation_id: str,
    as_of: str = typer.Option(None, "--as-of", help="Bu tarihte yürürlükteki metin (YYYY-AA-GG)."),
    draft: bool = typer.Option(False, "--draft", help="Yayımlanmamış taslağı da dahil et."),
    all_versions: bool = typer.Option(False, "--all", help="Tüm sürümleri listele."),
) -> None:
    """Bir hükmü atıf kimliğiyle göster (ör. tr:spk:II-15.1:m5:f1)."""
    with db.connect() as conn:
        if all_versions:
            vs = api.versions(conn, citation_id, include_draft=draft)
            if not vs:
                console.print("Sürüm bulunamadı.")
                raise typer.Exit(1)
            for v in vs:
                _print_version(v)
                console.print("-" * 60)
            return
        v = api.lookup(conn, citation_id, _as_of(as_of), include_draft=draft)
    if v is None:
        console.print(
            "Bu tarihte yürürlükte bilinen bir sürüm yok." + ("" if draft else " (taslak için --draft)")
        )
        raise typer.Exit(1)
    _print_version(v)


@app.command()
def search(
    query: str,
    as_of: str = typer.Option(None, "--as-of", help="Bu tarihte yürürlükteki hükümlerde ara."),
    k: int = typer.Option(5, "-k", help="Sonuç sayısı."),
    authority: str = typer.Option(None, "--authority", help="SPK / TBMM / KAP / BIST"),
    code: str = typer.Option(None, "--code", help="Resmî kod (ör. II-15.1)."),
    draft: bool = typer.Option(False, "--draft", help="Yayımlanmamış taslağı da dahil et."),
) -> None:
    """Mevzuatta tam metin arama (Türkçe normalleştirilmiş)."""
    filters = {"authority": authority, "official_code": code}
    with db.connect() as conn:
        hits = api.search(conn, query, _as_of(as_of), k=k, filters=filters, include_draft=draft)
    if not hits:
        console.print("Sonuç yok.")
        return
    for h in hits:
        v = h.version
        eff = v.effective_from.isoformat() if v.effective_from else "?"
        console.print(f"{v.citation_id}  [{v.label_tr()}]  yürürlük {eff}")
        console.print(f"   {h.snippet}")


@app.command()
def review() -> None:
    """İnceleme listesi: düşük güvenli yapı, eksilen hükümler ve onaysız yükümlülükler."""
    with db.connect() as conn:
        regs = conn.execute(select(regulation).order_by(regulation.c.official_code)).all()
        any_item = False
        for r in regs:
            extra = r.extra or {}
            low = extra.get("low_confidence", [])
            missing = extra.get("missing_provisions", [])
            notes = extra.get("history_notes", [])
            if not (low or missing or extra.get("doc_flags") or notes):
                continue
            any_item = True
            console.print(f"== {r.official_code} — {r.title} ({extra.get('manifest_id')})")
            for f in extra.get("doc_flags", []):
                console.print(f"  belge: {f}")
            for item in low:
                console.print(f"  {item['citation_id']}: {', '.join(item['flags'])}")
            for c in missing:
                console.print(f"  güncel metinde yok: {c}")
            for n in notes[:20]:
                console.print(f"  tarihsel metin {n['path']}: {n['note']}")
        pending = [o for o in api.all_obligations(conn) if not o.approved]
    if pending:
        any_item = True
        console.print(f"== Onay bekleyen yükümlülükler ({len(pending)})")
        for o in pending:
            console.print(f"  {o.key}  [{', '.join(o.event_codes)}]  {o.citation_id}  {o.deadline_rule}")
            console.print(f"     {o.title_tr}")
    if not any_item:
        console.print("İncelenecek bir şey yok.")


@app.command("approve-obligation")
def approve_obligation(
    ref: str = typer.Argument(None, help="Yükümlülük id'si (YAML id veya satır id)."),
    all_for: str = typer.Option(None, "--all-for", help="Bu olay koduna ait tüm yükümlülükleri onayla."),
) -> None:
    """Yükümlülük onayla (yalnızca onaylılar analizde kullanılır)."""
    if bool(ref) == bool(all_for):
        raise typer.BadParameter("bir yükümlülük id'si ya da --all-for <olay kodu> verin")
    with db.transaction() as conn:
        if ref:
            if not obl.approve(conn, ref):
                console.print(f"Bulunamadı: {ref}")
                raise typer.Exit(1)
            console.print(f"Onaylandı: {ref}")
        else:
            done = obl.approve_all_for(conn, all_for)
            console.print(f"{len(done)} yükümlülük onaylandı: {', '.join(done)}" if done else "Eşleşen yok.")


@app.command()
def publish(note: str = typer.Option(None, "--note", help="Anlık görüntü notu.")) -> None:
    """Taslak anlık görüntüyü yayımla (kb.version.published.v1)."""
    try:
        with db.transaction() as conn:
            res = snap.publish(conn, note=note)
    except snap.NothingToPublish as exc:
        console.print(str(exc))
        raise typer.Exit(1) from exc
    st = res.stats
    console.print(
        f"Yayımlandı {res.label} ({res.snapshot_id}): {st['versions_added']} yeni sürüm, "
        f"{st['provisions']} hüküm, {st['regulations']} düzenleme."
    )
