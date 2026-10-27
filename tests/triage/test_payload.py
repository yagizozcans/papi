from __future__ import annotations

import pytest

from papi.triage import payload as P
from papi.triage import taxonomy

from .helpers import form_field, kap_fixture_structure


@pytest.fixture
def tax():
    return taxonomy.load()


def test_value_parsing():
    assert P.parse_number("1.234.567,89 TL") == 1234567.89
    assert P.parse_number("% 10,2194") == 10.2194
    assert P.parse_number("3,0700000") == 3.07
    assert P.parse_number("-") is None
    assert P.parse_date("27/10/2026") == "2026-10-27"
    assert P.parse_date("12 Mayıs 2025") == "2025-05-12"
    assert P.parse_date("2026-11-30") == "2026-11-30"
    assert P.parse_date("31.02.2026") is None
    assert P.convert("Satış", "side") == "sell"
    assert P.convert("Hayır (No)", "bool") is False
    assert P.convert("Evet", "bool") is True
    assert P.convert("TL", "currency") == "TRY"
    assert P.convert("USD", "currency") == "USD"


def _dividend_structure() -> dict:
    return {
        "template_id": "tbl_KP_Report_ID",
        "form_fields": [
            form_field("Özet Bilgi", "Kar Dağıtım Önerisi", 1),
            form_field("Para Birimi", "TRY", 7),
            form_field("Pay Biçiminde Ödeme", "Ödenmeyecek", 8),
        ],
        "tables": [
            {
                "caption": "Nakit Kar Payı Ödeme Tutar ve Oranları",
                "header": [
                    "Pay Grup Bilgileri",
                    "1 TL Nominal Değerli Paya Ödenecek Nakit Kar Payı - Brüt(TL)",
                    "1 TL Nominal Değerli Paya Ödenecek Nakit Kar Payı - Brüt(%)",
                    "1 TL Nominal Değerli Paya Ödenecek Nakit Kar Payı - Net(TL)",
                ],
                "rows": [["A Grubu, ABC, TRAABC", "2,5000000", "250", "2,1250000"]],
                "span_ordinals": [[10, 11, 12, 13]],
            },
            {
                "caption": "Kar Payı Ödeme Tarihleri",
                "header": ["Ödeme", "Teklif Edilen Nakit Kar Payı Hak Kullanım Tarihi (1)",
                           "Kesinleşen Nakit Kar Payı Hak Kullanım Tarihi (2)", "Ödeme Tarihi (3)"],
                "rows": [["Peşin", "26.11.2026", "", "30.11.2026"]],
                "span_ordinals": [[20, 21, None, 22]],
            },
            {
                "caption": "ABC A.Ş. 01.01.2025/31.12.2025 Dönemi Kâr Payı Dağıtım Tablosu",
                "header": ["*", "SPK'ya Göre (TL)"],
                "rows": [["3. Dönem Kârı", "1.000"]],
            },
        ],
    }  # fmt: skip


def test_dividend_payload_from_synthetic_form(tax):
    r = P.build(
        "distribution.dividend.cash", tax, structure=_dividend_structure(), summary="Kar Dağıtım Önerisi"
    )
    assert r.error is None
    assert r.source == "form_fields"
    assert r.schema == "event.distribution.dividend.cash.v1"
    p = r.payload
    assert p["gross_per_share"] == 2.5
    assert p["net_per_share"] == 2.125
    assert p["gross_rate_pct"] == 250
    assert p["currency"] == "TRY"
    assert p["ex_date"] == "2026-11-26"  # proposed date used while the final one is empty
    assert p["payment_date"] == "2026-11-30"
    assert p["period_year"] == 2025
    assert p["decision_body"] == "board"
    assert p["installments"] is None  # unknown stays null
    assert p["share_groups"][0]["group"] == "A Grubu, ABC, TRAABC"
    assert r.spans["gross_per_share"] == [11]
    assert r.spans["payment_date"] == [22]
    assert set(p) == set(tax.schema_for("distribution.dividend.cash")["properties"])


def test_invalid_payload_is_nulled_with_reason(tax):
    s = _dividend_structure()
    s["form_fields"][1] = form_field("Para Birimi", "Türk Lirası (yerel)", 7)  # not ISO → falls back to hint
    r = P.build("distribution.dividend.cash", tax, structure=s)
    assert r.payload["currency"] == "TRY"  # (TL) header hint
    bad = P.build_derived("ownership.threshold_crossing", {"direction": "sideways"}, tax)
    assert bad.payload is None
    assert bad.error.startswith("schema: direction")


def test_schema_failure_on_extracted_value(tax):
    spec = P.parse_spec(
        {
            "extractors": {
                "capital.increase.bonus": {
                    "fields": {"currency": {"labels": ["para birimi"], "type": "text"}}
                }
            }
        }
    )
    s = {"form_fields": [form_field("Para Birimi", "Türk Lirası", 1)], "tables": []}
    r = P.build("capital.increase.bonus", tax, structure=s, spec=spec)
    assert r.payload is None
    assert r.source is None
    assert "currency" in r.error


def test_no_structure_and_no_match(tax):
    assert P.build("distribution.dividend.cash", tax, structure=None).error == "no_structure"
    r = P.build("distribution.dividend.cash", tax, structure={"form_fields": [], "tables": []})
    assert (r.payload, r.error) == (None, "no_fields_matched")
    # codes without an extractor (LLM fills them) → null payload, no error
    r = P.build("business.contract.award", tax, structure={"form_fields": [form_field("X", "1")]})
    assert (r.payload, r.error, r.schema) == (None, None, "event.business.contract.award.v1")
    # codes without schema
    assert P.build("market.measure", tax, structure={}).schema is None


def test_periodic_report_from_list_meta(tax):
    meta = {"subject": "Finansal Rapor", "year": 2026, "ruleType": "9 Aylık", "period": 3}
    r = P.build("report.financial.periodic", tax, structure=None, meta=meta)
    assert r.payload["period_year"] == 2026
    assert r.payload["period_months"] == 9
    assert r.payload["consolidated"] is None
    assert r.payload["revenue"] is None
    meta = {"subject": "Faaliyet Raporu (Konsolide Olmayan)", "year": 2025, "ruleType": "Yıllık"}
    r = P.build("report.financial.periodic", tax, structure=None, meta=meta)
    assert (r.payload["period_months"], r.payload["consolidated"]) == (12, False)


def test_insider_trade_split_and_threshold(tax):
    s = {
        "form_fields": [form_field("İlgili Şirketler", "[XYZ]", 1)],
        "tables": [
            {
                "header": [
                    "İşlem Tarihi",
                    "Alım İşlemine Konu Payların Toplam Nominal Tutarı (TL)",
                    "Satım İşlemine Konu Payların Toplam Nominal Tutarı (TL)",
                    "İşlemlerin Net Nominal Tutarı (TL)",
                    "Sahip Olunan Payların Gün Başı Bakiyesinin Sermayeye Oranı (%)",
                    "Sahip Olunan Oy Haklarının Gün Başı Bakiyesinin Oy Haklarına Oranı (%)",
                    "Sahip Olunan Payların Gün Sonu Bakiyesinin Sermayeye Oranı (%)",
                    "Sahip Olunan Oy Haklarının Gün Sonu Bakiyesinin Oy Haklarına Oranı (%)",
                ],
                "rows": [
                    ["26.10.2026", "100.000", "0", "100.000", "% 4,80", "% 4,80", "% 4,90", "% 4,90"],
                    ["27.10.2026", "200.000", "50.000", "150.000", "% 4,90", "% 4,90", "% 5,20", "% 5,20"],
                ],
                "span_ordinals": [[1, 2, 3, 4, 5, 6, 7, 8], [9, 10, 11, 12, 13, 14, 15, 16]],
            }
        ],
    }
    r = P.build("ownership.insider_trade", tax, structure=s)
    p = r.payload
    assert [(t["date"], t["side"], t["shares_nominal"]) for t in p["trades"]] == [
        ("2026-10-26", "buy", 100000.0),
        ("2026-10-27", "buy", 200000.0),
        ("2026-10-27", "sell", 50000.0),
    ]
    assert p["side"] == "mixed"
    assert p["total_shares_nominal"] == 350000.0
    assert (p["holding_before_pct"], p["holding_after_pct"]) == (4.8, 5.2)  # first / last row
    assert "trades.0.shares_nominal" in r.spans
    assert not any(k[0] == "_" for t in p["trades"] for k in t)
    crossing = P.threshold_crossing(p)
    assert crossing["threshold_pct"] == 5.0 and crossing["direction"] == "up"
    assert P.build_derived("ownership.threshold_crossing", crossing, tax).error is None
    assert P.threshold_crossing({**p, "holding_after_pct": 4.95, "voting_after_pct": 4.95}) is None
    assert P.threshold_crossing({"holding_before_pct": 4, "holding_after_pct": 21})["threshold_pct"] == 20


# ---------------------------------------------------------------- recorded live KAP forms (KAP slice parser)


def test_kap_fixture_dividend_form(tax):
    s, _ = kap_fixture_structure("1688363")
    r = P.build("distribution.dividend.cash", tax, structure=s, summary="Kar Dağıtım Önerisi")
    p = r.payload
    assert r.error is None
    assert (p["gross_per_share"], p["net_per_share"], p["gross_rate_pct"]) == (3.07, 2.6095, 307.0)
    assert (p["ex_date"], p["payment_date"], p["currency"], p["period_year"]) == (
        "2026-11-26",
        "2026-11-30",
        "TRY",
        2025,
    )
    assert p["total_net_amount"] == 10234498360.36
    assert len(p["share_groups"]) == 3


def test_kap_fixture_rights_issue_form(tax):
    s, _ = kap_fixture_structure("1687750")
    p = P.build("capital.increase.rights", tax, structure=s).payload
    assert p["current_capital"] == 791_000_000
    assert p["target_capital"] == 1_582_000_000
    assert p["rights_amount"] == 791_000_000
    assert p["rights_rate_pct"] == 100
    assert p["subscription_price"] == 1.0
    assert p["registered_capital_ceiling"] == 2_000_000_000
    assert p["board_decision_date"] == "2026-10-26"


def test_kap_fixture_buyback_forms(tax):
    s, _ = kap_fixture_structure("1688504")
    p = P.build("buyback.execution", tax, structure=s).payload
    assert p["trade_date"] == "2026-10-27"
    assert p["shares_nominal"] == 286_000
    assert p["avg_price"] == 86.0
    assert p["cumulative_shares_nominal"] == 876_000
    assert len(p["trades"]) == 3
    s, _ = kap_fixture_structure("1688545")
    p = P.build("buyback.program.start", tax, structure=s).payload
    assert (p["max_shares_nominal"], p["max_fund_amount"], p["board_decision_date"]) == (
        40_000_000,
        500_000_000,
        "2026-09-30",
    )


def test_kap_fixture_share_transactions(tax):
    s, _ = kap_fixture_structure("1688533")
    p = P.build("ownership.insider_trade", tax, structure=s).payload
    assert p["side"] == "sell"
    assert p["total_shares_nominal"] == 750_000
    assert (p["holding_before_pct"], p["holding_after_pct"]) == (1.15, 1.01)
    assert P.threshold_crossing(p) is None
    s, _ = kap_fixture_structure("1688581")
    p = P.build("ownership.insider_trade", tax, structure=s).payload
    crossing = P.threshold_crossing(p)
    assert crossing["threshold_pct"] == 10.0 and crossing["direction"] == "down"


def test_spec_validation():
    with pytest.raises(P.PayloadSpecError, match="unknown type"):
        P.parse_spec({"extractors": {"x.y": {"fields": {"a": {"labels": ["b"], "type": "blob"}}}}})
    with pytest.raises(P.PayloadSpecError, match="labels"):
        P.parse_spec({"extractors": {"x.y": {"fields": {"a": {"type": "text"}}}}})
    assert set(P.load_spec()) >= {
        "distribution.dividend.cash",
        "capital.increase.bonus",
        "capital.increase.rights",
        "buyback.program.start",
        "buyback.execution",
        "ownership.insider_trade",
    }
