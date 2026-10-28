"""Seeding helpers and a scripted LLM backend for analyst tests."""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select

from papi.core.ids import uuid7
from papi.core.schema import (
    OPEN_START,
    disclosure,
    document,
    document_span,
    identifier,
    instrument,
    issuer,
    portfolio,
    price_bar,
    source,
    txn,
)
from papi.core.timeutil import now_utc, to_utc
from papi.llm.types import LlmResult
from papi.sources.kap.parsing.doc import ParsedDocument
from papi.sources.kap.parsing.html import parse_body

KAP_FIXTURES = Path(__file__).parents[1] / "fixtures" / "kap"


def seed_company(conn: Connection, ticker: str, *, name: str | None = None, shares: float | None = None):
    now = now_utc()
    iss, inst = uuid7(), uuid7()
    conn.execute(
        issuer.insert().values(
            id=iss,
            legal_name=name or f"{ticker} A.Ş.",
            short_name=ticker,
            kind="company",
            sector_code="Makine",
            status="active",
            extra={"shares_outstanding": shares} if shares else None,
            created_at=now,
        )
    )
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
    kap = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    conn.execute(
        price_bar.insert().values(
            instrument_id=instrument_id,
            interval="1d",
            ts=day,
            source_id=kap,
            close=close,
            recorded_at=now_utc(),
        )
    )


def seed_holding(conn: Connection, instrument_id: str, qty: str = "1200", price: str = "40.5") -> None:
    pid = conn.execute(select(portfolio.c.id).where(portfolio.c.is_default.is_(True))).scalar_one()
    conn.execute(
        txn.insert().values(
            id=uuid7(),
            portfolio_id=pid,
            instrument_id=instrument_id,
            type="buy",
            trade_date=date(2026, 1, 5),
            quantity=Decimal(qty),
            price=Decimal(price),
            fees=Decimal(0),
            tax=Decimal(0),
            currency="TRY",
            source="manual",
            created_at=now_utc(),
        )
    )


def fixture_doc(index: str) -> tuple[ParsedDocument, dict[str, Any]]:
    item = json.loads((KAP_FIXTURES / f"detail_{index}.json").read_text(encoding="utf-8"))[0]
    body = item["disclosureBody"]
    return parse_body(body if isinstance(body, list) else [body]), item["disclosure"]["disclosureBasic"]


def insert_document(
    conn: Connection,
    disclosure_id: str,
    doc: ParsedDocument,
    *,
    kind: str | None = None,
    name: str | None = None,
) -> str:
    did = uuid7()
    conn.execute(
        document.insert().values(
            id=did,
            disclosure_id=disclosure_id,
            kind=kind or doc.kind,
            language=doc.language,
            text=doc.text,
            structure=doc.structure,
            parser=doc.parser,
            parser_version=doc.parser_version,
            parsed_at=now_utc(),
            extra={"file_name": name} if name else {},
        )
    )
    if doc.spans:
        conn.execute(
            document_span.insert(),
            [
                {
                    "id": uuid7(),
                    "document_id": did,
                    "ordinal": s.ordinal,
                    "page": s.page,
                    "char_start": s.char_start,
                    "char_end": s.char_end,
                    "field_path": s.field_path,
                    "text": s.text,
                }
                for s in doc.spans
            ],
        )
    return did


def seed_disclosure(
    conn: Connection,
    *,
    index: str,
    issuer_id: str,
    subject: str | None = None,
    source_ref: str | None = None,
    published_at: str = "2026-10-27T15:30:00+03:00",
    revision_of: str | None = None,
    revision_kind: str = "original",
    attachments: list[tuple[str, ParsedDocument]] | None = None,
) -> str:
    """A normalised KAP disclosure whose body is the parsed recorded fixture ``detail_<index>.json``."""
    doc, basic = fixture_doc(index)
    kap = conn.execute(select(source.c.id).where(source.c.code == "kap")).scalar_one()
    did = uuid7()
    subj = subject or basic.get("subject") or basic.get("title")
    conn.execute(
        disclosure.insert().values(
            id=did,
            source_id=kap,
            source_ref=source_ref or index,
            revision_of=revision_of,
            revision_kind=revision_kind,
            published_at=to_utc(published_at),
            first_seen_at=now_utc(),
            source_class=basic.get("disclosureClass") or "ODA",
            source_subject=subj,
            title=basic.get("title"),
            summary=basic.get("summary"),
            filer_issuer_id=issuer_id,
            related_issuer_ids=[issuer_id],
            is_late=False,
            source_payload={"list": {"subject": subj, "summary": basic.get("summary")}, "detail": {}},
            ingest_status="normalized",
            created_at=now_utc(),
        )
    )
    body_id = insert_document(conn, did, doc, kind="disclosure_body")
    att_ids = [insert_document(conn, did, d, kind="attachment", name=n) for n, d in attachments or []]
    conn.execute(
        disclosure.update()
        .where(disclosure.c.id == did)
        .values(body_document_id=body_id, attachment_document_ids=att_ids)
    )
    return did


# ------------------------------------------------------------------------------------------- backend


def ok_result(so: dict[str, Any] | None, *, cost: float = 0.05, duration_ms: int = 1500) -> LlmResult:
    return LlmResult(
        structured_output=copy.deepcopy(so),
        text=None,
        model_ids=["claude-sonnet-test"],
        cost_usd_est=cost,
        duration_ms=duration_ms,
        exit_code=0,
        raw_json={"type": "result", "structured_output": copy.deepcopy(so), "total_cost_usd": cost},
        error_kind=None,
        error=None,
    )


class ScriptedBackend:
    """``LlmBackend`` returning canned outputs per task (``{"analyst": [...], "checker": [...]}``)."""

    def __init__(self, **outputs: list[dict[str, Any] | LlmResult]) -> None:
        self.outputs = {k: list(v) for k, v in outputs.items()}
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kw: Any) -> LlmResult:
        self.calls.append(kw)
        queue = self.outputs.get(kw["task"]) or []
        if not queue:
            raise AssertionError(f"unexpected {kw['task']} call")
        item = queue.pop(0)
        return item if isinstance(item, LlmResult) else ok_result(item)

    def tasks(self) -> list[str]:
        return [c["task"] for c in self.calls]


def contract_output(**overrides: Any) -> dict[str, Any]:
    """A clean analyst output for the MEKAG 'Yeni İş İlişkisi' fixture (1688011)."""
    out: dict[str, Any] = {
        "event_codes": ["business.contract.award"],
        "payload": {
            "business.contract.award": {
                "counterparty": {"name": "Yurt İçi Yerleşik Şirket", "issuer_id": None,
                                 "is_related_party": None, "is_public_sector": None},
                "value": 1325000,
                "currency": "EUR",
                "vat_included": False,
                "duration_months": None,
                "start_date": "2026-10-27",
                "scope": "kırma eleme tesisi siparişi",
                "is_framework": None,
                "ratios": {"value_to_ttm_revenue": None, "value_to_market_cap": None},
            }
        },
        "payload_spans": {"business.contract.award": {"value": [14], "start_date": [11], "currency": [14]}},
        "fundamental_channels": ["revenue"],
        "fundamental_direction": "positive",
        "materiality": 45,
        "materiality_rationale": "Sipariş tutarı 1.325.000 EUR; şirketin ölçeğine göre orta düzeyde.",
        "claims": [
            {"kind": "fact",
             "text_tr": "Şirket yurt içi yerleşik bir şirketten 1.325.000 EUR tutarında sipariş aldı.",
             "text_en": "The company received an order of EUR 1,325,000.", "span_ordinals": [14],
             "citation_ids": [], "value": "1.325.000", "unit": "EUR", "formula_id": None},
            {"kind": "fact", "text_tr": "İş ilişkisinin başlangıç tarihi 27/10/2026.", "text_en": None,
             "span_ordinals": [11], "citation_ids": [], "value": None, "unit": None, "formula_id": None},
            {"kind": "inference", "text_tr": "Sipariş kısa vadede ciroya olumlu katkı sağlayabilir.",
             "text_en": None, "span_ordinals": [13], "citation_ids": [], "value": None, "unit": None,
             "formula_id": None},
        ],
        "summary_tr": "MEKAG, yurt içi yerleşik bir şirketten KDV hariç 1.325.000 EUR tutarında kırma eleme "
        "tesisi siparişi aldığını açıkladı. Şirket, iş ilişkisinin ciro ve karlılığı olumlu etkileyeceğini "
        "belirtiyor.",
        "summary_en": "MEKAG received a crushing-screening plant order of EUR 1,325,000 excluding VAT from a "
        "domestic company.",
        "headline_tr": "MEKAG'a 1.325.000 EUR'luk sipariş",
        "watch_next": ["Siparişin teslim takvimi ve gelir tablosuna yansıması"],
        "confidence": 0.85,
    }  # fmt: skip
    out.update(overrides)
    return out
