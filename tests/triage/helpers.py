"""Seeding helpers for triage tests (issuers, instruments, disclosures with C1 structures, holdings)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.schema import (
    OPEN_START,
    disclosure,
    document,
    identifier,
    instrument,
    issuer,
    portfolio,
    price_bar,
    source,
    txn,
    watchlist,
)
from papi.core.text import normalize_tr
from papi.core.timeutil import now_utc, to_utc

DATA = Path(__file__).parent / "data"
KAP_FIXTURES = Path(__file__).parents[1] / "fixtures" / "kap"


def seed_company(
    conn: Connection,
    ticker: str,
    *,
    name: str | None = None,
    kind: str = "company",
    sector: str | None = None,
    shares: float | None = None,
) -> tuple[str, str]:
    """issuer + equity instrument + TICKER identifier; returns (issuer_id, instrument_id)."""
    now = now_utc()
    iss = uuid7()
    conn.execute(
        issuer.insert().values(
            id=iss,
            legal_name=name or f"{ticker} A.Ş.",
            short_name=ticker,
            kind=kind,
            sector_code=sector,
            status="active",
            extra={"shares_outstanding": shares} if shares else None,
            created_at=now,
        )
    )
    inst = uuid7()
    conn.execute(
        instrument.insert().values(
            id=inst, issuer_id=iss, type="equity", currency="TRY", status="active", created_at=now
        )
    )
    conn.execute(
        identifier.insert().values(
            id=uuid7(),
            entity_type="instrument",
            entity_id=inst,
            scheme="TICKER",
            value=ticker,
            valid_from=OPEN_START,
            recorded_at=now,
        )
    )
    return iss, inst


def seed_price(conn: Connection, instrument_id: str, day: date, close: float) -> None:
    kap_source = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    conn.execute(
        price_bar.insert().values(
            instrument_id=instrument_id,
            interval="1d",
            ts=day,
            source_id=kap_source,
            close=close,
            recorded_at=now_utc(),
        )
    )


def form_field(
    label: str, value: Any, ordinal: int | None = None, field_id: str | None = None
) -> dict[str, Any]:
    return {
        "label": label,
        "label_norm": normalize_tr(label),
        "value": value,
        "field_id": field_id,
        "span_ordinal": ordinal,
    }


def seed_disclosure(
    conn: Connection,
    *,
    subject: str,
    summary: str | None = None,
    issuer_ids: list[str] | None = None,
    filer_issuer_id: str | None = None,
    source_ref: str | None = None,
    source_class: str = "ODA",
    published_at: datetime | str = "2026-10-27T15:30:00+03:00",
    is_late: bool = False,
    revision_of: str | None = None,
    revision_kind: str = "original",
    structure: dict[str, Any] | None = None,
    text: str | None = None,
    list_meta: dict[str, Any] | None = None,
) -> str:
    """A normalised KAP disclosure (+ body document when ``structure``/``text`` is given)."""
    kap_source = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    did = uuid7()
    pub = to_utc(published_at) if isinstance(published_at, str) else published_at
    meta = {"subject": subject, "summary": summary, "disclosureClass": source_class, **(list_meta or {})}
    conn.execute(
        disclosure.insert().values(
            id=did,
            source_id=kap_source,
            source_ref=source_ref or str(abs(hash(did)) % 10_000_000),
            revision_of=revision_of,
            revision_kind=revision_kind,
            published_at=pub,
            first_seen_at=now_utc(),
            source_class=source_class,
            source_subject=subject,
            summary=summary,
            filer_issuer_id=filer_issuer_id if filer_issuer_id else (issuer_ids or [None])[0],
            related_issuer_ids=issuer_ids or [],
            is_late=is_late,
            source_payload={"list": meta, "detail": {}},
            ingest_status="normalized",
            created_at=now_utc(),
        )
    )
    if structure is not None or text is not None:
        doc = uuid7()
        conn.execute(
            document.insert().values(
                id=doc,
                disclosure_id=did,
                kind="disclosure_body",
                language="tr",
                text=text,
                structure=structure,
                parsed_at=now_utc(),
            )
        )
        conn.execute(disclosure.update().where(disclosure.c.id == did).values(body_document_id=doc))
    return did


def seed_holding(
    conn: Connection, instrument_id: str, qty: str = "100", trade_date: date = date(2026, 1, 5)
) -> None:
    pid = conn.execute(select(portfolio.c.id).where(portfolio.c.is_default.is_(True))).scalar_one()
    conn.execute(
        txn.insert().values(
            id=uuid7(),
            portfolio_id=pid,
            instrument_id=instrument_id,
            type="buy",
            trade_date=trade_date,
            quantity=Decimal(qty),
            price=Decimal("10"),
            fees=Decimal("0"),
            tax=Decimal("0"),
            currency="TRY",
            source="manual",
            created_at=now_utc(),
        )
    )


def seed_watch(conn: Connection, instrument_ids: list[str], name: str = "Ana") -> None:
    conn.execute(
        watchlist.insert().values(id=uuid7(), name=name, instrument_ids=instrument_ids, created_at=now_utc())
    )


def load_harvest() -> Iterator[dict[str, Any]]:
    with gzip.open(DATA / "kap_list_harvest.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def kap_fixture_structure(index: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(C1 structure, disclosureBasic) of a recorded KAP detail fixture, parsed by the KAP slice's parser."""
    html = pytest.importorskip("papi.sources.kap.parsing.html")
    path = KAP_FIXTURES / f"detail_{index}.json"
    if not path.is_file():
        pytest.skip(f"KAP fixture {path.name} not present")
    item = json.loads(path.read_text(encoding="utf-8"))[0]
    body = item["disclosureBody"]
    parsed = html.parse_body(body if isinstance(body, list) else [body])
    return parsed.structure, item["disclosure"]["disclosureBasic"]
