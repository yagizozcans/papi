"""``papi analyst`` — Claude analyst pipeline (PRD 04 §4.1, §4.3)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from sqlalchemy import Connection, and_, exists, select

from papi.analyst.context import build_context, estimate_tokens
from papi.analyst.pipeline import ANALYSED_PRIORITIES, analyse_event, reverify, skip_reason
from papi.analyst.spec import analyst_spec
from papi.core.db import connect
from papi.core.jobs import Deferred, PermanentError, RetryLater
from papi.core.schema import analysis, citation, claim, disclosure, event, event_log, job, source, summary
from papi.core.timeutil import to_ist
from papi.llm import governor

app = typer.Typer(help="Claude analist hattı (bağlam paketi, analiz, doğrulama).", no_args_is_help=True)
console = Console(soft_wrap=True, markup=False, highlight=False)
HANDLER_KIND = "event:analyst.analyse"


def _fmt(dt: datetime | None) -> str:
    return f"{to_ist(dt):%d.%m.%Y %H:%M}" if dt else "—"


def resolve_event(conn: Connection, ref: str) -> str:
    """``ref`` = event id, or a KAP disclosure index (first live event of that disclosure)."""
    if conn.execute(select(event.c.id).where(event.c.id == ref)).first():
        return ref
    if ref.isdigit():
        row = conn.execute(
            select(event.c.id)
            .join(disclosure, disclosure.c.id == event.c.primary_disclosure_id)
            .join(source, source.c.id == disclosure.c.source_id)
            .where(source.c.code == "kap", disclosure.c.source_ref == ref, event.c.superseded_by.is_(None))
            .order_by(event.c.priority, event.c.created_at)
        ).first()
        if row is not None:
            return row.id
    raise typer.BadParameter(f"olay bulunamadı: {ref} (event id veya KAP bildirim no)")


@app.command("run")
def run_cmd(
    ref: Annotated[str, typer.Argument(help="event id veya KAP bildirim no")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Bağlam paketini ve token tahminini yazdır; Claude çağırma")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Analiz edilmiş / P4 olsa da yeniden analiz et (Claude çağrısı)")
    ] = False,
) -> None:
    """Bir olayın bildirimini analiz et (governor kurallarına tabi)."""
    with connect() as conn:
        eid = resolve_event(conn, ref)
        if dry_run:
            pack = build_context(conn, eid)
            conn.rollback()
            spec = analyst_spec()
            ev = conn.execute(select(event).where(event.c.id == eid)).mappings().first()
            console.print(pack.text)
            console.print("—" * 60)
            console.print(
                f"Bağlam paketi ≈ {pack.token_estimate} token ({len(pack.text)} karakter) · sistem istemi ≈ "
                f"{estimate_tokens(spec.system_prompt)} token · span {len(pack.spans)} · kırpılan span "
                f"{pack.trimmed_spans} · KB {len(pack.kb)} · olay {len(pack.events)}"
            )
            console.print(
                f"input_hash {pack.input_hash} · agent_version {spec.agent_version} · "
                f"öncelik {pack.priority} · "
                f"atlama nedeni: {skip_reason(ev) or 'yok'}"
            )
            return
        try:
            out = analyse_event(conn, eid, force=force)
        except Deferred as exc:
            console.print(f"Ertelendi ({exc.reason}); yeniden deneme: {_fmt(exc.until)}")
            raise typer.Exit(2) from exc
        except (PermanentError, RetryLater) as exc:
            console.print(f"Analiz başarısız: {exc}")
            raise typer.Exit(1) from exc
        conn.commit()
        console.print(f"Durum: {out.status}" + (f" ({out.reason})" if out.reason else ""))
        if out.status == "skipped":
            if out.reason and out.reason.startswith(("priority", "historical")):
                console.print("Zorlamak için --force kullanın.")
            return
        _print_outcome(conn, out)


def _print_outcome(conn: Connection, out: Any) -> None:
    if out.pack is not None:
        console.print(
            f"Bağlam paketi ≈ {out.pack.token_estimate} token · input_hash {out.pack.input_hash[:16]}"
        )
    if out.result is not None:
        r = out.result
        console.print(
            f"Model {','.join(r.model_ids) or '—'} · maliyet ≈ ${r.cost_usd_est:.4f} · "
            f"süre {r.duration_ms} ms"
        )
    if out.report is not None:
        rep = out.report
        console.print(f"Doğrulayıcı: {'temiz' if rep.ok else 'İŞARETLİ'} ({len(rep.issues)} sorun)")
        for issue in rep.issues:
            console.print(f"  - {issue}")
    if out.checker:
        console.print(f"Denetçi (checker): {out.checker}")
    if out.created_event_ids:
        console.print(f"Analistin eklediği olaylar: {', '.join(out.created_event_ids)}")
    for aid, eid in zip(out.analysis_ids, out.event_ids, strict=False):
        console.print(f"analysis {aid} → event {eid}")
    if out.event_ids:
        _print_summary(conn, out.event_ids[0])


def _print_summary(conn: Connection, event_id: str) -> None:
    rows = {r.lang: r for r in conn.execute(select(summary).where(summary.c.event_id == event_id))}
    if "tr" in rows:
        console.print("")
        console.print(f"Başlık: {rows['tr'].headline or '—'}")
        console.print(f"Özet (TR): {rows['tr'].body or '—'}")
    if "en" in rows and rows["en"].body:
        console.print(f"Summary (EN): {rows['en'].body}")


@app.command("show")
def show_cmd(
    ref: Annotated[str, typer.Argument(help="event id veya KAP bildirim no")],
    as_json: Annotated[bool, typer.Option("--json", help="JSON çıktı")] = False,
) -> None:
    """Bir olayın analizlerini, iddialarını ve özetini göster."""
    with connect() as conn:
        eid = resolve_event(conn, ref)
        ev = conn.execute(select(event).where(event.c.id == eid)).mappings().first()
        rows = (
            conn.execute(
                select(analysis).where(analysis.c.event_id == eid).order_by(analysis.c.created_at.desc())
            )
            .mappings()
            .all()
        )
        claims_by: dict[str, list[Any]] = {}
        for r in rows:
            claims_by[r["id"]] = list(
                conn.execute(
                    select(claim).where(claim.c.analysis_id == r["id"]).order_by(claim.c.id)
                ).mappings()
            )
        cits: dict[str, list[Any]] = {}
        all_claims = [c["id"] for cs in claims_by.values() for c in cs]
        if all_claims:
            for c in conn.execute(select(citation).where(citation.c.claim_id.in_(all_claims))).mappings():
                cits.setdefault(c["claim_id"], []).append(c)
        if as_json:
            data = {
                "event": {k: ev[k] for k in ("id", "code", "priority", "materiality", "materiality_band",
                                             "payload", "payload_source", "status", "superseded_by")},
                "analyses": [
                    {
                        **{k: r[k] for k in ("id", "agent", "agent_version", "model_ids", "prompt_version",
                                             "input_hash", "confidence", "latency_ms", "cost_usd_est",
                                             "priority", "review_state", "created_at")},
                        "claims": [
                            {**dict(c), "citations": [dict(x) for x in cits.get(c["id"], [])]}
                            for c in claims_by[r["id"]]
                        ],
                    }
                    for r in rows
                ],
            }  # fmt: skip
            console.print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            return
        console.print(
            f"Olay {eid} · {ev['code']} · {ev['priority']} · önem {ev['materiality']} "
            f"({ev['materiality_band']}) · "
            f"payload kaynağı {ev['payload_source'] or '—'}"
            + (" · YERİNE GEÇİLDİ" if ev["superseded_by"] else "")
        )
        if not rows:
            console.print("Analiz yok.")
            return
        for r in rows:
            meta = (r["output"] or {}).get("_papi") or {}
            console.print("")
            console.print(
                f"[{r['agent']}] {r['id']} · {_fmt(r['created_at'])} · {r['review_state']} · "
                f"güven {r['confidence']} · ${(r['cost_usd_est'] or 0):.4f} · {r['latency_ms']} ms · "
                f"{','.join(r['model_ids'] or [])} · "
                f"denetçi {(meta.get('checker') or {}).get('state', '—')}"
            )
            for c in claims_by[r["id"]]:
                refs = ", ".join(
                    f"{x['target_type'].split('_')[0]}:{(x['quote'] or '')[:40]}"
                    for x in cits.get(c["id"], [])
                )
                console.print(
                    f"  - ({c['kind']}, doğrulama {c['verify_status'] or '—'}, "
                    f"denetim {c['check_status'] or '—'}) "
                    f"{c['text_tr']}" + (f"  ⟵ {refs}" if refs else "")
                )
        _print_summary(conn, eid)


@app.command("verify")
def verify_cmd(analysis_id: Annotated[str, typer.Argument(help="analysis.id")]) -> None:
    """Saklı bir analizi deterministik doğrulayıcıdan yeniden geçir (iddia durumlarını günceller)."""
    with connect() as conn:
        try:
            rep = reverify(conn, analysis_id)
        except LookupError as exc:
            raise typer.BadParameter(f"analiz bulunamadı: {analysis_id}") from exc
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        conn.commit()
        state = conn.execute(select(analysis.c.review_state).where(analysis.c.id == analysis_id)).scalar_one()
    console.print(f"Doğrulayıcı: {'temiz' if rep.ok else 'İŞARETLİ'} · review_state {state}")
    for c in rep.claims:
        console.print(
            f"  iddia {c.index} ({c.kind}): {c.status}" + (f" — {'; '.join(c.issues)}" if c.issues else "")
        )
    for issue in rep.issues:
        if not issue.startswith("iddia "):
            console.print(f"  - {issue}")


@app.command("pending")
def pending_cmd(limit: Annotated[int, typer.Option(help="En fazla satır")] = 30) -> None:
    """Analiz bekleyen olaylar (P1–P3) ve governor durumu."""
    with connect() as conn:
        has_analysis = exists().where(and_(analysis.c.event_id == event.c.id, analysis.c.agent == "analyst"))
        rows = conn.execute(
            select(
                event.c.id,
                event.c.code,
                event.c.priority,
                event.c.announced_at,
                event.c.materiality,
                event.c.extra,
            )
            .where(
                event.c.priority.in_(ANALYSED_PRIORITIES),
                event.c.superseded_by.is_(None),
                event.c.status != "retracted",
                ~has_analysis,
            )
            .order_by(event.c.priority, event.c.announced_at.desc())
            .limit(limit)
        ).all()
        job_rows = {
            (r.payload or {}).get("event_id"): r
            for r in conn.execute(
                select(job.c.payload, job.c.status, job.c.not_before, job.c.last_error, job.c.attempts).where(
                    job.c.kind == HANDLER_KIND, job.c.status.in_(("queued", "running", "dead"))
                )
            )
        }
        jobs_by_event = {}  # job payload event_id = CloudEvent id → our event id via event_log.data
        if job_rows:
            for r in conn.execute(
                select(event_log.c.id, event_log.c.data).where(event_log.c.id.in_(list(job_rows)))
            ):
                jobs_by_event[(r.data or {}).get("event_id")] = job_rows[r.id]
        st = governor.status(conn)
    console.print(f"Analiz bekleyen olaylar (P1–P3): {len(rows)}")
    for r in rows:
        j = jobs_by_event.get(r.id)
        job_s = "iş yok"
        if j is not None:
            job_s = f"iş {j.status}, deneme {j.attempts}" + (
                f", sonraki {_fmt(j.not_before)}" if j.not_before else ""
            )
            if j.last_error:
                job_s += f", son hata: {j.last_error[:80]}"
        console.print(
            f"  {r.priority} · {r.id} · {r.code} · yayın {_fmt(r.announced_at)} · "
            f"önem {r.materiality} · {job_s}"
        )
    console.print(
        f"Governor: {'DURDURULDU' if st['paused'] else 'aktif'}"
        + (f" ({st['pause_reason']}, {_fmt(st['paused_until'])} kadar)" if st["paused"] else "")
        + f" · 5 sa {st['used_5h']:.4f}/{st['cap_5h']} · hafta {st['used_week']:.4f}/{st['cap_week']}"
        + f" · kalibrasyon {'açık (yalnız P1/P2)' if st['calibration_mode'] else 'kapalı'}"
        + (f" · odak ≤{st['focus']['max_priority']}" if st["focus"] else "")
        + f" · ertelenen iş {st['queue']['llm_deferred_total']}"
    )


__all__ = ["app", "resolve_event"]
