"""``papi kap`` — KAP capture commands (live network only in these explicit commands)."""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Connection, func, select

from papi.core import db, jobs
from papi.core.metrics import series
from papi.core.schema import disclosure, document, document_span, event_log, job
from papi.core.state import get_state
from papi.core.timeutil import ist_date, now_utc, to_ist
from papi.sources.kap import gaps as gaps_mod
from papi.sources.kap.capital import refresh_batch
from papi.sources.kap.client import CIRCUIT_KEY, KapBlocked, KapError, KapPublicWebClient
from papi.sources.kap.common import kap_source_id, writer
from papi.sources.kap.companies import resolve_tickers, sync_companies
from papi.sources.kap.discovery import BACKFILL_KEY, LAST_INDEX_KEY, LAST_POLL_OK_KEY, backfill, poll_once
from papi.sources.kap.drift import DRIFT_KEY, UNKNOWN_TEMPLATES_KEY
from papi.sources.kap.ingest import FetchOutcome, ParseFailed, process_disclosure
from papi.sources.kap.reprocess import reparse

app = typer.Typer(
    help="KAP bildirimleri: yoklama, şirket listesi, çekme, geçmiş yükleme.", no_args_is_help=True
)
console = Console()


def _fail(msg: str) -> None:
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


@contextmanager
def _writer() -> Iterator[Connection]:
    """Connection whose transactions begin IMMEDIATE (safe next to a running worker)."""
    with db.connect() as conn:
        yield writer(conn)


def _process_refs(
    conn: Connection, client: KapPublicWebClient, new: list[dict[str, Any]], mode: str, limit: int = 0
) -> dict[str, int]:
    """Process freshly discovered refs inline and cancel their queued fetch jobs. Holdings/watchlist
    first, then newest first (live value decays with age); ``limit`` > 0 caps the count (the rest stay
    queued for the worker)."""
    order = {"P1": 0, "P2": 1}
    counts = {"normalized": 0, "failed": 0, "skipped": 0}
    todo = sorted(new, key=lambda n: (order.get(n.get("priority") or "", 9), -int(n["ref"])))
    for item in todo[:limit] if limit else todo:
        try:
            out: FetchOutcome = process_disclosure(
                conn,
                client,
                item["ref"],
                mode=mode,
                is_catchup=item.get("is_catchup"),
                label=item.get("priority"),
            )
            counts[out.status if out.status in counts else "normalized"] += 1
        except KapBlocked:
            raise
        except (KapError, ParseFailed) as exc:
            counts["failed"] += 1
            console.print(f"[yellow]{item['ref']}: {exc}[/yellow]")
            continue
        if item.get("job_id"):
            jobs.cancel(conn, item["job_id"])
            conn.commit()
    return counts


@app.command("poll-once")
def poll_once_cmd(
    process: Annotated[
        bool, typer.Option("--process/--no-process", help="Yeni bildirimleri hemen işle")
    ] = True,
    limit: Annotated[int, typer.Option(help="En fazla bu kadar yeni bildirimi işle (0 = hepsi)")] = 0,
) -> None:
    """Bugün + dün listesini tara (uykudan sonra kaçan pencereyi de), yenileri kaydet ve işle."""
    with KapPublicWebClient() as client, _writer() as conn:
        try:
            report = poll_once(conn, client)
        except KapBlocked as exc:
            _fail(f"KAP erişimi geçici olarak durduruldu (devre kesici): {exc}")
            return
        s = report.summary()
        console.print(
            f"Pencere {s['window'][0]} → {s['window'][1]} ({s['mode']}): {s['listed']} listelendi, "
            f"{s['new']} yeni, {s['changed']} değişmiş, {s['requests']} istek."
        )
        if process and report.new:
            counts = _process_refs(conn, client, report.new, report.mode, limit)
            console.print(f"İşlendi: {counts}; toplam istek {client.request_count}.")
        gr = gaps_mod.detect_gaps(conn)
        conn.commit()
        console.print(f"Boşluk taraması: {len(gr.new_missing)} yeni eksik indeks, {gr.open} açık.")


@app.command("sync-companies")
def sync_companies_cmd() -> None:
    """KAP şirket listesini (IGS) çek, issuer/instrument/identifier kayıtlarını güncelle."""
    with KapPublicWebClient() as client, _writer() as conn:
        report = sync_companies(conn, client)
        conn.commit()
    console.print(f"Şirket eşitleme: {report.as_dict()}")


@app.command("capital")
def capital_cmd(
    tickers: Annotated[
        str | None, typer.Option(help="Virgülle ayrılmış hisse kodları (boşsa vadesi gelenler)")
    ] = None,
    limit: Annotated[int, typer.Option(help="En fazla bu kadar şirket")] = 10,
) -> None:
    """Ödenmiş sermaye ve pay adetlerini KAP şirket sayfasından güncelle (önce portföy/izleme)."""
    with KapPublicWebClient() as client, _writer() as conn:
        ids: list[str] | None = None
        if tickers:
            codes = [t.strip().upper() for t in tickers.split(",") if t.strip()]
            found = resolve_tickers(conn, codes, ist_date())
            missing = [c for c in codes if c not in found]
            if missing:
                _fail(f"bilinmeyen hisse kodu: {', '.join(missing)} (önce `papi kap sync-companies`)")
            ids = list(dict.fromkeys(v[1] for v in found.values() if v[1]))
        try:
            counts = refresh_batch(conn, client, limit=limit, issuer_ids=ids)
        except KapBlocked as exc:
            _fail(f"KAP erişimi geçici olarak durduruldu (devre kesici): {exc}")
            return
    console.print(f"Sermaye güncelleme: {counts}; istek {client.request_count}.")


@app.command("fetch")
def fetch_cmd(
    index: Annotated[int, typer.Argument(help="KAP disclosureIndex")],
    force: Annotated[bool, typer.Option(help="Normalize edilmiş olsa da yeniden çek")] = False,
    kap_pdf: Annotated[bool, typer.Option("--kap-pdf", help="KAP PDF'ini de indir")] = False,
) -> None:
    """Tek bir bildirimi indeks ile çek, ayrıştır ve normalize et."""
    with KapPublicWebClient() as client, _writer() as conn:
        try:
            out = process_disclosure(conn, client, str(index), mode="manual", force=force, kap_pdf=kap_pdf)
        except (KapError, ParseFailed) as exc:
            _fail(f"{index}: {exc}")
            return
    r = out.result
    console.print(
        f"{index}: {out.status}; ekler {out.attachments_fetched} indirildi, {out.attachments_skipped} atlandı"
        + (f"; olay {r.event_type} ({r.priority or '-'}) belgeler {r.documents}" if r else "")
        + (f"; uyarılar {out.errors}" if out.errors else "")
    )


@app.command("backfill")
def backfill_cmd(
    from_: Annotated[str, typer.Option("--from", help="Başlangıç tarihi YYYY-MM-DD")],
    to: Annotated[str, typer.Option("--to", help="Bitiş tarihi YYYY-MM-DD")],
    tickers: Annotated[
        str | None, typer.Option(help="Virgülle ayrılmış hisse kodları (ör. THYAO,ASELS)")
    ] = None,
    max_windows: Annotated[int | None, typer.Option(help="Bu çalıştırmada en fazla pencere sayısı")] = None,
    restart: Annotated[bool, typer.Option(help="İmleci sıfırla, baştan başla")] = False,
) -> None:
    """Geçmiş bildirimleri listele (devam ettirilebilir); çekme işleri düşük öncelikle kuyruğa alınır."""
    start, end = date.fromisoformat(from_), date.fromisoformat(to)
    if start > end:
        _fail("--from, --to'dan sonra olamaz")
    codes = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    with KapPublicWebClient() as client, _writer() as conn:
        try:
            totals = backfill(
                conn, client, start, end, tickers=codes, max_windows=max_windows, restart=restart
            )
        except (KapError, ValueError) as exc:
            _fail(str(exc))
            return
    console.print(f"Geçmiş yükleme: {totals} (işler `papi run` çalışanı tarafından işlenir)")


@app.command("reparse")
def reparse_cmd(
    from_: Annotated[str, typer.Option("--from", help="Başlangıç tarihi YYYY-MM-DD")],
    to: Annotated[str, typer.Option("--to", help="Bitiş tarihi YYYY-MM-DD")],
    template: Annotated[str | None, typer.Option(help="Yalnızca bu şablon (tbl_… sınıfı)")] = None,
    publish: Annotated[
        bool, typer.Option("--publish/--no-publish", help="Değişenler için revised yayınla")
    ] = True,
) -> None:
    """Arşivlenmiş ham veriden yeniden ayrıştır/normalize et; yalnız çıktısı değişenler revize edilir."""
    with _writer() as conn:
        stats = reparse(
            conn, date.fromisoformat(from_), date.fromisoformat(to), template=template, publish=publish
        )
    errors = stats.pop("errors")
    console.print(f"Yeniden ayrıştırma: {stats}")
    for e in errors[:20]:
        console.print(f"[yellow]{e}[/yellow]")


@app.command("show")
def show_cmd(
    index: Annotated[int, typer.Argument(help="KAP disclosureIndex")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON çıktı")] = False,
    lines: Annotated[int, typer.Option(help="Gösterilecek metin satırı")] = 25,
) -> None:
    """Kaydedilmiş bir bildirimi göster."""
    with db.connect() as conn:
        sid = kap_source_id(conn)
        row = conn.execute(
            select(disclosure).where(disclosure.c.source_id == sid, disclosure.c.source_ref == str(index))
        ).first()
        if row is None:
            _fail(f"{index} kayıtlı değil (önce `papi kap fetch {index}`)")
            return
        docs = conn.execute(
            select(
                document.c.id,
                document.c.kind,
                document.c.language,
                document.c.text,
                document.c.structure,
                document.c.extra,
                func.count(document_span.c.id).label("spans"),
            )
            .outerjoin(document_span, document_span.c.document_id == document.c.id)
            .where(document.c.disclosure_id == row.id)
            .group_by(document.c.id)
        ).all()
        evs = conn.execute(
            select(event_log.c.type, event_log.c.time).where(event_log.c.subject == f"disclosure/{row.id}")
        ).all()
    m = row._mapping
    if as_json:
        out = {
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in m.items()
            if k != "source_payload"
        }
        out["documents"] = [
            {
                "id": d.id,
                "kind": d.kind,
                "language": d.language,
                "spans": d.spans,
                "template_id": (d.structure or {}).get("template_id"),
            }
            for d in docs
        ]
        out["events"] = [e.type for e in evs]
        console.print_json(json.dumps(out, ensure_ascii=False, default=str))
        return
    ex = row.extra or {}
    console.print(f"[bold]{row.source_ref}[/bold] {row.title or ''} — {ex.get('kap_title') or ''}")
    console.print(f"https://www.kap.org.tr/tr/Bildirim/{row.source_ref}")
    pub = to_ist(row.published_at).strftime("%d.%m.%Y %H:%M:%S") if row.published_at else "?"
    seen = to_ist(row.first_seen_at).strftime("%d.%m.%Y %H:%M:%S")
    console.print(
        f"Sınıf {row.source_class}/{row.source_type}/{row.source_category} · Konu: {row.source_subject} · "
        f"Yayın {pub} · İlk görülme {seen} · Durum {row.ingest_status} · Revizyon {row.revision_kind}"
    )
    console.print(
        f"Hisseler {ex.get('tickers')} · Çözümleme {ex.get('issuer_resolution')} · Geç {row.is_late} · "
        f"Telafi {ex.get('is_catchup')} · Diller {row.languages} · Şablon {ex.get('template_id')}"
    )
    if row.summary:
        console.print(f"Özet: {row.summary}")
    for d in docs:
        st = d.structure or {}
        console.print(
            f"[cyan]belge {d.kind}/{d.language} · {d.spans} span · {len(st.get('form_fields') or [])} alan · "
            f"{len(st.get('tables') or [])} tablo · {(d.extra or {}).get('file_name') or ''}[/cyan]"
        )
        if d.kind == "disclosure_body" and d.language == "tr":
            for line in (d.text or "").splitlines()[:lines]:
                console.print(f"  {line[:200]}", markup=False)
    console.print(f"Olaylar: {[e.type for e in evs]}")


@app.command("stats")
def stats_cmd() -> None:
    """Alım sağlığı: durumlar, son yoklama, boşluklar, gecikme, bilinmeyen şablonlar."""
    with db.connect() as conn:
        sid = kap_source_id(conn)
        by_status = conn.execute(
            select(disclosure.c.ingest_status, func.count())
            .where(disclosure.c.source_id == sid)
            .group_by(disclosure.c.ingest_status)
        ).all()
        since = now_utc() - timedelta(days=1)
        lat = [r["value"] for r in series(conn, "kap.latency_s", since=since, limit=5000)]
        nlat = [r["value"] for r in series(conn, "kap.normalize_latency_s", since=since, limit=5000)]
        fails = len(series(conn, "kap.fetch.fail", since=since, limit=5000))
        queued = conn.execute(
            select(job.c.kind, job.c.status, func.count())
            .where(job.c.kind.like("kap:%"), job.c.status.in_(("queued", "running", "dead")))
            .group_by(job.c.kind, job.c.status)
        ).all()
        gst = gaps_mod.load_state(conn)
        unknown = get_state(conn, UNKNOWN_TEMPLATES_KEY, {}) or {}
        drift_state = get_state(conn, DRIFT_KEY, {}) or {}
        circuit = get_state(conn, CIRCUIT_KEY, {}) or {}
        last_ok = get_state(conn, LAST_POLL_OK_KEY)
        last_index = get_state(conn, LAST_INDEX_KEY)
        cursor = get_state(conn, BACKFILL_KEY)
    t = Table(title="KAP alım durumu")
    t.add_column("Ölçü")
    t.add_column("Değer")
    t.add_row("Durumlar", ", ".join(f"{s}: {n}" for s, n in by_status) or "-")
    t.add_row("Son başarılı yoklama", str(last_ok or "-"))
    t.add_row("Son indeks", str(last_index or "-"))
    t.add_row("Gecikme (görülme−yayın) 24s", _pct(lat))
    t.add_row("Gecikme (normalize−yayın) 24s", _pct(nlat))
    t.add_row("Hatalar 24s", str(fails))
    t.add_row(
        "Açık boşluk / filigran",
        f"{len(gst['open'])} / {gst['watermark']} "
        f"(fon atlanan {len(gst['skipped'])}, ölü {len(gst['dead'])})",
    )
    t.add_row("Kuyruk", ", ".join(f"{k}/{s}: {n}" for k, s, n in queued) or "-")
    t.add_row("Bilinmeyen şablonlar", ", ".join(f"{k} ({v.get('count')})" for k, v in unknown.items()) or "-")
    t.add_row(
        "Şema sapması", ", ".join(f"{k}: {v.get('missing') or []}" for k, v in drift_state.items()) or "-"
    )
    t.add_row("Devre kesici", f"{circuit.get('state', 'closed')} {circuit.get('open_until') or ''}")
    t.add_row("Geçmiş yükleme imleci", json.dumps(cursor, ensure_ascii=False) if cursor else "-")
    console.print(t)


def _pct(values: list[float]) -> str:
    if not values:
        return "-"
    vals = sorted(values)
    p95 = vals[min(len(vals) - 1, round(0.95 * (len(vals) - 1)))]
    return f"n={len(vals)} p50={statistics.median(vals):.0f}s p95={p95:.0f}s"
