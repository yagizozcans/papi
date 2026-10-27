from __future__ import annotations

import json

import jsonschema
import pytest
import yaml

from papi.core.config import resource_dir
from papi.triage import taxonomy
from papi.triage.taxonomy import TaxonomyError

# Codes named in PRD 06 §5.1 (example column) — all must exist.
PRD_CODES = [
    "capital.increase.rights",
    "capital.increase.bonus",
    "capital.decrease",
    "capital.registered_ceiling",
    "distribution.dividend.cash",
    "distribution.dividend.stock",
    "distribution.policy",
    "buyback.program.start",
    "buyback.execution",
    "buyback.program.end",
    "ownership.insider_trade",
    "ownership.threshold_crossing",
    "ownership.tender_offer",
    "ownership.squeeze_out",
    "business.contract.award",
    "business.contract.termination",
    "business.capex",
    "business.capacity",
    "business.new_market",
    "business.tender_result",
    "mna.acquisition",
    "mna.disposal",
    "mna.merger",
    "mna.demerger",
    "mna.jv",
    "finance.debt.issuance",
    "finance.debt.restructuring",
    "finance.rating.change",
    "finance.loan",
    "report.financial.periodic",
    "report.financial.guidance",
    "report.activity",
    "report.sustainability",
    "governance.board.change",
    "governance.executive.change",
    "governance.gm.notice",
    "governance.gm.result",
    "governance.articles.change",
    "governance.auditor.change",
    "legal.lawsuit",
    "legal.investigation",
    "legal.sanction",
    "legal.bankruptcy",
    "legal.concordat",
    "market.measure",
    "market.halt",
    "market.segment.change",
    "market.index.change",
    "market.ipo",
    "market.delisting",
    "macro.policy_rate",
    "macro.inflation",
    "macro.fx_regulation",
    "macro.law_change",
    "other.unclassified",
]

# First ten event types of PRD 04 §7 (rows with two codes count both).
TOP10_CODES = [
    "business.contract.award",
    "capital.increase.bonus",
    "capital.increase.rights",
    "distribution.dividend.cash",
    "buyback.program.start",
    "buyback.execution",
    "ownership.insider_trade",
    "ownership.threshold_crossing",
    "mna.acquisition",
    "mna.disposal",
    "report.financial.periodic",
    "report.financial.guidance",
]


def test_load_has_every_prd_code_with_labels_and_prior():
    tax = taxonomy.load()
    assert tax.version
    missing = [c for c in PRD_CODES if c not in tax]
    assert missing == []
    for ec in tax.codes.values():
        assert ec.family == ec.code.split(".")[0]
        assert ec.label_tr and ec.label_en
        assert 0 <= ec.prior <= 100
    assert set(tax.families) >= {c.split(".")[0] for c in PRD_CODES}


def test_helpers_label_family_and_cache():
    tax = taxonomy.load()
    assert tax.label("distribution.dividend.cash") == "Nakit kâr payı"
    assert tax.label("distribution.dividend.cash", "en") == "Cash dividend"
    assert tax.label("nope.unknown") == "nope.unknown"
    assert tax.family_of("mna.merger") == "mna"
    assert tax.get("market.measure").prior == 70
    assert tax.get("buyback.program.end").prior == 30  # code override
    assert tax.get("capital.decrease").prior == 60
    assert taxonomy.load() is tax


def test_top10_codes_have_valid_draft2020_schemas():
    tax = taxonomy.load()
    for code in TOP10_CODES:
        ec = tax.get(code)
        assert ec.schema == f"event.{code}.v1", code
        schema = tax.schema_for(code)
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"event.{code}/1"
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        if "currency" in schema["properties"]:
            assert schema["properties"]["currency"]["pattern"] == "^[A-Z]{3}$"
        assert "span" not in json.dumps(schema["properties"])  # spans live outside the payload


def test_every_schema_file_is_referenced_by_a_code():
    tax = taxonomy.load()
    referenced = {ec.schema for ec in tax.codes.values() if ec.schema}
    files = {
        p.name.removesuffix(".json") for p in (resource_dir("contracts") / "schemas").glob("event.*.json")
    }
    assert files == referenced


def _raw() -> dict:
    return yaml.safe_load(taxonomy.default_path().read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["codes"].update({"nofamily.x": {"label_tr": "a", "label_en": "b"}}), "unknown family"),
        (lambda r: r["codes"]["market.measure"].update({"prior": 140}), "prior"),
        (lambda r: r["codes"]["market.measure"].pop("label_tr"), "label_tr"),
        (lambda r: r["codes"].update({"Bad Code": {"label_tr": "a", "label_en": "b"}}), "syntax"),
        (lambda r: r["codes"]["market.measure"].update({"schema": "event.market.measure.v1"}), "schema file"),
        (lambda r: r["codes"].pop("other.unclassified"), "other.unclassified"),
        (lambda r: r.pop("taxonomy_version"), "taxonomy_version"),
    ],
)
def test_validation_errors(mutate, message):
    raw = _raw()
    mutate(raw)
    with pytest.raises(TaxonomyError, match=message):
        taxonomy.parse(raw, schemas_root=taxonomy.schema_dir())
