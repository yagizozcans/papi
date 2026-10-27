"""Sample rows for dashboard tests and the live smoke run (direct inserts; no other slice needed)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, select

from papi.core import events as core_events
from papi.core import metrics, rawstore
from papi.core.ids import uuid7
from papi.core.schema import (
    OPEN_START,
    analysis,
    citation,
    claim,
    disclosure,
    document,
    document_span,
    event,
    identifier,
    instrument,
    issuer,
    job,
    llm_usage_ledger,
    portfolio,
    price_bar,
    source,
    summary,
    txn,
    watchlist,
)
from papi.core.state import set_state
from papi.core.timeutil import IST, now_utc

BODY = (
    "Şirketimiz Yönetim Kurulu 2026 yılı için hisse başına 2,50 TL brüt nakit kâr payı dağıtılmasına "
    "karar vermiştir. Ödeme tarihi 15.11.2026'dır."
)


@dataclass
class Position:
    """Shape of ``papi.portfolio.service.Position`` (C3) for monkeypatching."""

    instrument_id: str
    ticker: str
    quantity: Decimal
    avg_cost: Decimal | None
    cost_total: Decimal
    last_close: Decimal | None
    last_close_date: date | None
    market_value: Decimal | None
    weight: float | None


def _company(conn: Connection, ticker: str, name: str, now: datetime) -> tuple[str, str]:
    iss = uuid7()
    conn.execute(
        issuer.insert().values(id=iss, legal_name=name, short_name=ticker, kind="company", created_at=now)
    )
    ins = uuid7()
    conn.execute(
        instrument.insert().values(id=ins, issuer_id=iss, type="equity", currency="TRY", created_at=now)
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=ins,
            scheme="TICKER",
            value=ticker,
            valid_from=OPEN_START,
            recorded_at=now,
        )
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="issuer",
            entity_id=iss,
            scheme="KAP_MEMBER_OID",
            value=f"oid-{ticker.lower()}",
            valid_from=OPEN_START,
            recorded_at=now,
        )
    )
    return iss, ins


def _disclosure(
    conn: Connection,
    *,
    kap: str,
    ref: str,
    iss: str,
    published: datetime,
    seen: datetime,
    title: str,
    body_doc: str | None = None,
    revision_of: str | None = None,
    revision_kind: str = "original",
    status: str = "normalized",
    extra: dict | None = None,
) -> str:
    did = uuid7()
    conn.execute(
        disclosure.insert().values(
            id=did,
            source_id=kap,
            source_ref=ref,
            revision_of=revision_of,
            revision_kind=revision_kind,
            published_at=published,
            first_seen_at=seen,
            title=title,
            source_subject=title,
            filer_issuer_id=iss,
            ingest_status=status,
            ingest_error="parse: unexpected table" if status.startswith("failed") else None,
            extra=extra,
            created_at=seen,
        )
    )
    if body_doc:
        conn.execute(disclosure.update().where(disclosure.c.id == did).values(body_document_id=body_doc))
    return did


def _event(
    conn: Connection,
    *,
    disc: str,
    iss: str,
    ins: str,
    ticker: str,
    code: str,
    at: datetime,
    materiality: int,
    priority: str,
    headline: str,
    payload: dict | None = None,
    extra: dict | None = None,
) -> str:
    eid = uuid7()
    conn.execute(
        event.insert().values(
            id=eid,
            disclosure_ids=[disc],
            primary_disclosure_id=disc,
            code=code,
            taxonomy_version="tr-2026.1",
            issuer_ids=[iss],
            instrument_ids=[ins],
            announced_at=at,
            payload=payload,
            payload_source="form_fields" if payload else None,
            materiality=materiality,
            materiality_band="high" if materiality >= 70 else "medium" if materiality >= 40 else "low",
            priority=priority,
            status="provisional",
            extra={"headline_tr": headline, "tickers": [ticker], "rules": {"subject": code}, **(extra or {})},
            created_at=at + timedelta(seconds=5),
        )
    )
    return eid


def seed(conn: Connection) -> dict[str, Any]:
    """Insert a small coherent world. Returns ids; caller commits."""
    now = now_utc()
    kap = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    prices_src = conn.execute(select(source.c.id).where(source.c.code == "isyatirim_prices")).scalar()
    pf = conn.execute(select(portfolio.c.id)).scalar_one()
    asels_iss, asels = _company(conn, "ASELS", "Aselsan Elektronik Sanayi ve Ticaret A.Ş.", now)
    thy_iss, thyao = _company(conn, "THYAO", "Türk Hava Yolları A.O.", now)
    garan_iss, garan = _company(conn, "GARAN", "Türkiye Garanti Bankası A.Ş.", now)

    # Raw object + body document with spans (C1).
    sha = rawstore.put_bytes(
        f"<html><body>{BODY}</body></html>".encode(),
        source_code="kap",
        url="https://www.kap.org.tr/x",
        content_type="text/html; charset=utf-8",
        conn=conn,
    )
    doc = uuid7()
    i_amount = BODY.index("2,50 TL")
    i_date = BODY.index("15.11.2026")
    conn.execute(
        document.insert().values(
            id=doc,
            raw_sha256=sha,
            kind="disclosure_body",
            mime="text/html",
            language="tr",
            text=BODY,
            structure={
                "template_id": "tbl_dividend",
                "form_fields": [
                    {"label": "Brüt Tutar", "label_norm": "brut tutar", "value": "2,50", "span_ordinal": 1},
                    {
                        "label": "Ödeme Tarihi",
                        "label_norm": "odeme tarihi",
                        "value": "15.11.2026",
                        "span_ordinal": 2,
                    },
                ],
            },
            parser="kap_html",
            parsed_at=now,
        )
    )
    conn.execute(
        document_span.insert().values(
            id=(span1 := uuid7()),
            document_id=doc,
            ordinal=1,
            char_start=i_amount,
            char_end=i_amount + 7,
            field_path="form:brut tutar",
            text="2,50 TL",
        )
    )
    conn.execute(
        document_span.insert().values(
            id=uuid7(),
            document_id=doc,
            ordinal=2,
            char_start=i_date,
            char_end=i_date + 10,
            field_path="form:odeme tarihi",
            text="15.11.2026",
        )
    )
    conn.execute(
        document_span.insert().values(
            id=uuid7(), document_id=doc, ordinal=3, text="Ek tablo satırı (ofset yok)"
        )
    )

    t0 = now - timedelta(hours=2)
    d_div = _disclosure(
        conn,
        kap=kap,
        ref="1500001",
        iss=asels_iss,
        published=t0,
        seen=t0 + timedelta(seconds=40),
        title="Kâr Payı Dağıtım İşlemlerine İlişkin Bildirim",
        body_doc=doc,
    )
    conn.execute(document.update().where(document.c.id == doc).values(disclosure_id=d_div))
    e_div = _event(
        conn,
        disc=d_div,
        iss=asels_iss,
        ins=asels,
        ticker="ASELS",
        code="distribution.dividend.cash",
        at=t0,
        materiality=75,
        priority="P1",
        headline="ASELS hisse başına 2,50 TL nakit temettü",
        payload={"gross_per_share": "2,50", "payment_date": "15.11.2026", "currency": "TRY"},
        extra={"payload_spans": {"gross_per_share": [1]}},
    )
    conn.execute(
        summary.insert().values(
            id=uuid7(),
            event_id=e_div,
            lang="tr",
            headline="Nakit temettü",
            body="Brüt 2,50 TL temettü [S1], ödeme 15.11.2026 [S2].",
            created_at=now,
        )
    )
    an = uuid7()
    conn.execute(
        analysis.insert().values(
            id=an,
            event_id=e_div,
            agent="analyst",
            backend="claude_code_cli",
            model_ids=["sonnet"],
            output={"ok": True},
            created_at=now,
            review_state="flagged",
        )
    )
    cl = uuid7()
    conn.execute(
        claim.insert().values(
            id=cl,
            analysis_id=an,
            text_tr="Brüt temettü 2,50 TL [S1]",
            kind="fact",
            value="2,50",
            check_status="unsupported",
        )
    )
    conn.execute(
        citation.insert().values(
            id=uuid7(), claim_id=cl, target_type="document_span", target_id=span1, quote="2,50 TL"
        )
    )

    # Market-wide P3 event (visible in default scope), a P4 routine event (only in 'all').
    t1 = now - timedelta(hours=1)
    d_thy = _disclosure(
        conn,
        kap=kap,
        ref="1500002",
        iss=thy_iss,
        published=t1,
        seen=t1 + timedelta(hours=3),
        title="Yeni İş İlişkisi",
        extra={"is_catchup": True},
    )
    e_thy = _event(
        conn,
        disc=d_thy,
        iss=thy_iss,
        ins=thyao,
        ticker="THYAO",
        code="business.contract.award",
        at=t1,
        materiality=65,
        priority="P3",
        headline="THYAO yeni uçak kiralama sözleşmesi",
    )
    t2 = now - timedelta(minutes=30)
    d_gar = _disclosure(
        conn,
        kap=kap,
        ref="1500003",
        iss=garan_iss,
        published=t2,
        seen=t2 + timedelta(seconds=20),
        title="Faaliyet Raporu",
    )
    e_gar = _event(
        conn,
        disc=d_gar,
        iss=garan_iss,
        ins=garan,
        ticker="GARAN",
        code="reporting.annual_report",
        at=t2,
        materiality=10,
        priority="P4",
        headline="GARAN faaliyet raporu",
    )
    # Correction of the dividend disclosure (revision chain) — its event supersedes nothing here.
    d_fix = _disclosure(
        conn,
        kap=kap,
        ref="1500004",
        iss=asels_iss,
        published=t2,
        seen=t2 + timedelta(seconds=30),
        title="Kâr Payı (Düzeltme)",
        revision_of=d_div,
        revision_kind="correction",
    )
    e_fix = _event(
        conn,
        disc=d_fix,
        iss=asels_iss,
        ins=asels,
        ticker="ASELS",
        code="distribution.dividend.cash",
        at=t2,
        materiality=70,
        priority="P1",
        headline="ASELS temettü düzeltmesi",
    )
    _disclosure(
        conn,
        kap=kap,
        ref="1500005",
        iss=garan_iss,
        published=t2,
        seen=t2,
        title="Bozuk bildirim",
        status="failed:parse",
    )
    _disclosure(
        conn,
        kap=kap,
        ref="1500006",
        iss=garan_iss,
        published=t2,
        seen=t2,
        title="Yeni şablon",
        extra={"template_unknown": True},
    )

    # Portfolio: one ASELS buy; THYAO on the watchlist.
    conn.execute(
        txn.insert().values(
            id=uuid7(),
            portfolio_id=pf,
            instrument_id=asels,
            type="buy",
            trade_date=(now - timedelta(days=3)).astimezone(IST).date(),
            quantity=Decimal("100"),
            price=Decimal("62.50"),
            fees=Decimal("0"),
            tax=Decimal("0"),
            currency="TRY",
            source="manual",
            created_at=now,
        )
    )
    conn.execute(watchlist.insert().values(id=uuid7(), name="Ana", instrument_ids=[thyao], created_at=now))
    if prices_src:
        for i in range(10):
            day = (now - timedelta(days=9 - i)).astimezone(IST).date()
            conn.execute(
                price_bar.insert().values(
                    instrument_id=asels,
                    interval="1d",
                    ts=day,
                    source_id=prices_src,
                    close=60.0 + i,
                    recorded_at=now,
                )
            )

    # Ops: dead job, ledger rows, KAP state + metrics.
    conn.execute(
        job.insert().values(
            id=uuid7(),
            kind="event:triage.classify",
            priority=20,
            attempts=5,
            max_attempts=5,
            status="dead",
            last_error="ValueError: boom",
            created_at=now,
            updated_at=now,
        )
    )
    for k, (task, cost, outcome) in enumerate(
        [("analyst", 0.12, "ok"), ("explain", 0.03, "ok"), ("analyst", 0.0, "limit_hit")]
    ):
        conn.execute(
            llm_usage_ledger.insert().values(
                id=uuid7(),
                task=task,
                priority="P1",
                model_ids=["sonnet"],
                cost_usd_est=cost,
                duration_ms=1500,
                outcome=outcome,
                at=now - timedelta(minutes=10 * (k + 1)),
            )
        )
    conn.execute(
        llm_usage_ledger.insert().values(
            id=uuid7(), task="backlog", cost_usd_est=1.0, outcome="ok", at=now - timedelta(days=2)
        )
    )
    set_state(conn, "kap:last_poll_at", (now - timedelta(seconds=20)).isoformat())
    set_state(conn, "kap:last_poll_ok_at", (now - timedelta(seconds=20)).isoformat())
    set_state(conn, "kap:gaps", {"open": [1499990, 1499991], "tries": {"1499990": 2}, "dead": []})
    set_state(
        conn,
        "kap:unknown_templates",
        {
            "tbl_new_form": {
                "count": 3,
                "first_seen": now.isoformat(),
                "last_seen": now.isoformat(),
                "example_ref": "1500006",
            }
        },
    )
    for v in (12.0, 35.0, 48.0, 400.0):
        metrics.record("kap.latency_s", v, conn=conn, at=now - timedelta(minutes=v), cls="ODA", catchup=False)
    metrics.record("kap.poll.ok", 3, conn=conn, at=now - timedelta(minutes=1), mode="live")

    positions = [
        Position(
            instrument_id=asels,
            ticker="ASELS",
            quantity=Decimal("100"),
            avg_cost=Decimal("62.50"),
            cost_total=Decimal("6250"),
            last_close=Decimal("69"),
            last_close_date=now.date(),
            market_value=Decimal("6900"),
            weight=1.0,
        ),
    ]
    return {
        "kap": kap,
        "portfolio": pf,
        "asels": asels,
        "thyao": thyao,
        "garan": garan,
        "asels_issuer": asels_iss,
        "e_div": e_div,
        "e_thy": e_thy,
        "e_gar": e_gar,
        "e_fix": e_fix,
        "d_div": d_div,
        "d_fix": d_fix,
        "doc": doc,
        "sha": sha,
        "span1": span1,
        "analysis": an,
        "positions": positions,
    }


def publish_classified(conn: Connection, event_id: str, code: str, priority: str) -> str:
    """Publish ``event.classified.v1`` as triage would (handlers may be empty)."""
    return core_events.publish(
        conn,
        core_events.EVENT_CLASSIFIED,
        f"event/{event_id}",
        {"event_id": event_id, "code": code, "priority": priority},
        "triage",
    )
