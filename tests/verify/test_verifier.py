from __future__ import annotations

import copy
from typing import Any

import pytest

from papi.analyst.spec import analyst_spec
from papi.verify import VerifySources, verify

SPANS = {
    5: "Karar Tarihi: 27.10.2026",
    13: "3,0700000",
    14: "Şirketimiz bugün (27.10.2026) sipariş almıştır. Sipariş bedeli KDV hariç 1.325.000 EUR'dır.",
    20: "Önceki yıl cirosu 26.500.000 EUR",
}
PROVISIONS = {"tr:spk:II-15.1:m5": "II-15.1 m.5 tr:spk:II-15.1:m5\nİçsel bilgiler gecikmeksizin açıklanır."}


def sources(**kw: Any) -> VerifySources:
    base: dict[str, Any] = {
        "spans": SPANS,
        "provisions": PROVISIONS,
        "context_text": "Son kapanış (MEKAG, 24.10.2026): 41,2 TL\nMEKAG: 1.200 adet",
        "allowed_codes": {"business.contract.award", "distribution.dividend.cash"},
        "payload_schemas": {},
        "form_payloads": {},
    }
    base.update(kw)
    return VerifySources(**base)


def claim(kind: str, text: str, spans: list[int], cits: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "kind": kind, "text_tr": text, "text_en": None, "span_ordinals": spans, "citation_ids": cits or [],
        "value": kw.get("value"), "unit": kw.get("unit"), "formula_id": kw.get("formula_id"),
    }  # fmt: skip


def output(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_codes": ["business.contract.award"],
        "payload": {},
        "payload_spans": {},
        "fundamental_channels": ["revenue"],
        "fundamental_direction": "positive",
        "materiality": 40,
        "materiality_rationale": "Orta.",
        "claims": [],
        "summary_tr": "Sipariş alındı.",
        "summary_en": "Order received.",
        "headline_tr": "Sipariş",
        "watch_next": [],
        "confidence": 0.8,
    }
    out.update(kw)
    return out


def test_clean_output_ok() -> None:
    rep = verify(
        output(claims=[claim("fact", "Bedel 1.325.000 EUR.", [14])]), sources(), schema=analyst_spec().schema
    )
    assert rep.ok and rep.issues == []


def test_kb_citation_supports_fact_without_span() -> None:
    rep = verify(output(claims=[claim("fact", "SPK II-15.1 m.5 içsel bilginin açıklanmasını düzenler.", [],
                                      ["tr:spk:II-15.1:m5"])]), sources())  # fmt: skip
    assert rep.ok, rep.issues
    assert rep.claims[0].citations == ["tr:spk:II-15.1:m5"]


def test_fact_numbers_must_be_in_cited_span_not_elsewhere() -> None:
    # 26.500.000 exists in the pack (S20) but not in the cited span S14
    rep = verify(output(claims=[claim("fact", "Önceki yıl cirosu 26.500.000 EUR.", [14])]), sources())
    assert rep.claims[0].status == "fail"


def test_computation_reproduced_from_cited_inputs() -> None:
    good = claim("computation", "Sipariş, önceki yıl cirosunun yaklaşık %5'i.", [14, 20],
                 value="%5", formula_id="percent_of")  # fmt: skip
    bad = claim("computation", "Sipariş, cironun %12'si.", [14, 20], value="%12", formula_id="percent_of")
    missing = claim("computation", "Oran 5.", [14, 20], value="5")
    rep = verify(output(claims=[good, bad, missing], summary_tr="Tutar cironun yaklaşık %5'i."), sources())
    assert [c.status for c in rep.claims] == ["pass", "fail", "fail"]
    assert "formula_id yok" in rep.claims[2].issues[0]
    assert "summary_tr" not in {k for k, t in rep.texts.items() if t.dropped}  # verified result usable


def test_inference_without_citation_uses_whole_pack() -> None:
    c = claim("inference", "1.200 adetlik pozisyon için etki sınırlı olabilir.", [])
    rep = verify(output(claims=[c]), sources())
    assert rep.ok
    rep = verify(output(claims=[claim("inference", "Hisse 50 TL'ye çıkabilir.", [])]), sources())
    assert rep.claims[0].status == "fail"


def test_payload_numeric_and_date_fields_checked_against_their_spans() -> None:
    out = output(
        payload={"business.contract.award": {"value": 1325000, "currency": "EUR", "start_date": "2026-10-27",
                                             "duration_months": 12, "counterparty": {"name": "X"}}},
        payload_spans={"business.contract.award": {"value": [14], "start_date": [5]}},
    )  # fmt: skip
    rep = verify(out, sources())
    assert rep.payload_fields == {"business.contract.award": {"duration_months": "alan için span yok"}}
    out2 = copy.deepcopy(out)
    out2["payload"]["business.contract.award"].update(value=1300000, duration_months=None)
    rep2 = verify(out2, sources())
    assert rep2.payload_fields["business.contract.award"]["value"].startswith("1300000")


def test_rules_payload_fields_are_not_rechecked() -> None:
    out = output(event_codes=["distribution.dividend.cash"],
                 payload={"distribution.dividend.cash": {"gross_per_share": 3.07, "period_year": 2025}},
                 payload_spans={"distribution.dividend.cash": {"gross_per_share": [13]}})  # fmt: skip
    rep = verify(out, sources(form_payloads={"distribution.dividend.cash": {"period_year": 2025}}))
    assert rep.payload_fields == {}


def test_payload_schema_validation_per_code() -> None:
    schema = {"type": "object", "properties": {"value": {"type": ["number", "null"]}}, "required": ["value"]}
    out = output(payload={"business.contract.award": {"value": "çok"}})
    rep = verify(out, sources(payload_schemas={"business.contract.award": schema}))
    assert "business.contract.award" in rep.payload_errors and not rep.ok


def test_unknown_codes_and_schema_errors() -> None:
    rep = verify(output(event_codes=["made.up.code"]), sources())
    assert rep.unknown_codes == ["made.up.code"] and not rep.ok
    rep = verify(output(confidence=2, claims=[{"kind": "fact"}]), sources(), schema=analyst_spec().schema)
    assert rep.schema_errors and rep.claims == []


@pytest.mark.parametrize(
    ("summary", "kept"),
    [
        ("Sipariş 1.325.000 EUR. Kesin yükselir.", "Sipariş 1.325.000 EUR."),
        ("Sipariş 9.999.999 EUR. Karar 27.10.2026 tarihli.", "Karar 27.10.2026 tarihli."),
        ("A.Ş. Yönetim Kurulu sipariş aldı.", "A.Ş. Yönetim Kurulu sipariş aldı."),
    ],
)
def test_summary_sentences_dropped(summary: str, kept: str) -> None:
    rep = verify(output(summary_tr=summary), sources())
    assert rep.texts["summary_tr"].kept == kept
