"""``papi eval`` harness: gates, baseline, CLI exit codes, review flow, suite failure detection and the
analyst plumbing with a scripted LLM (no Claude)."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from papi.core.jobs import Deferred
from papi.evals import core, kbfixture
from papi.evals.cli import app
from papi.evals.core import Gate, Metric, SuiteResult
from papi.evals.scratch import scratch_db
from papi.evals.suites import analyst, parsers, triage, verifier
from papi.llm.types import LlmResult

runner = CliRunner()
GOLDEN = core.GOLDEN_DIR


@pytest.fixture
def tmp_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of tests/golden (baseline, analyst items, KB fixture) as the golden dir."""
    base = tmp_path / "golden"
    shutil.copytree(GOLDEN / "analyst", base / "analyst")
    shutil.copytree(GOLDEN / "kb", base / "kb")
    shutil.copy(GOLDEN / "baseline.json", base / "baseline.json")
    monkeypatch.setattr(core, "GOLDEN_DIR", base)
    return base


# ------------------------------------------------------------------------------------------- core


def test_gates_and_display() -> None:
    assert Gate(">=", 0.9).check(0.9) and not Gate(">=", 0.9).check(0.89)
    assert Gate("<", 0.05).check(0.0499) and not Gate("<", 0.05).check(0.05)
    assert Gate("==", 1.0).check(1.0) and not Gate("==", 0).check(1)
    assert not Gate(">=", 0.9).check(None)
    assert Metric("a", 0.9448).display() == "94.48 %"
    assert Metric("c", 0.0675, kind="usd").display() == "$0.0675"
    assert Metric("n", 308, kind="count").display() == "308"


def test_accept_baseline_merges_and_diff(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"metrics": {"triage.code_accuracy": 0.9, "kb.recall_at_5": 0.8}}))
    r1 = SuiteResult("triage", [Metric("code_accuracy", 0.95, Gate(">=", 0.9))])
    skipped = SuiteResult("analyst", [Metric("items", 8, kind="count")], skipped="no claude")
    core.accept_baseline([r1, skipped], p, note="test")
    data = json.loads(p.read_text())
    assert data["metrics"] == {"kb.recall_at_5": 0.8, "triage.code_accuracy": 0.95}
    assert data["note"] == "test" and data["accepted_at"]
    rows = core.diff([SuiteResult("triage", [Metric("code_accuracy", 0.96)])], data)
    assert rows[0].baseline == 0.95 and rows[0].delta == pytest.approx(0.01)


def test_cost_gate_is_baseline_plus_ten_percent() -> None:
    res = [SuiteResult("analyst", [Metric("avg_cost_usd", 0.12, kind="usd")])]
    core.apply_baseline_gates(res, {"metrics": {"analyst.avg_cost_usd": 0.10}})
    m = res[0].metrics[0]
    assert m.gate == Gate("<=", 0.11) and m.passed is False
    fresh = [SuiteResult("analyst", [Metric("avg_cost_usd", 0.12, kind="usd")])]
    core.apply_baseline_gates(fresh, {"metrics": {}})
    assert fresh[0].metrics[0].gate is None


# -------------------------------------------------------------------------------------------- CLI


def _fake_suite(value: float):
    def run(**_: Any) -> SuiteResult:
        return SuiteResult("parsers", [Metric("field_exact_match", value, Gate("==", 1.0))], ["parser drift"])

    return run


def test_cli_exit_code_and_baseline_accept(tmp_golden: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("papi.evals.runner.runner", lambda name: _fake_suite(0.98))
    res = runner.invoke(app, ["--suite", "parsers"])
    assert res.exit_code == 1, res.output
    assert "EŞİK BAŞARISIZ" in res.output and "parser drift" in res.output
    before = (tmp_golden / "baseline.json").read_text()
    res = runner.invoke(app, ["--suite", "parsers", "--baseline", "accept"])
    assert res.exit_code == 1 and "taban yazılmadı" in res.output
    assert (tmp_golden / "baseline.json").read_text() == before
    res = runner.invoke(app, ["--suite", "parsers", "--baseline", "accept", "--force", "--note", "known gap"])
    data = json.loads((tmp_golden / "baseline.json").read_text())
    assert data["metrics"]["parsers.field_exact_match"] == 0.98 and data["note"] == "known gap"

    monkeypatch.setattr("papi.evals.runner.runner", lambda name: _fake_suite(1.0))
    res = runner.invoke(app, ["--suite", "parsers", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["passed"] is True and out["diff"][0]["baseline"] == 0.98
    assert runner.invoke(app, ["--suite", "nope"]).exit_code == 2


def test_crashing_suite_fails_the_run(tmp_golden: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> SuiteResult:
        raise RuntimeError("fixture missing")

    monkeypatch.setattr("papi.evals.runner.runner", lambda name: boom)
    res = runner.invoke(app, ["--suite", "triage"])
    assert res.exit_code == 1 and "suite crashed: RuntimeError: fixture missing" in res.output


def test_review_flips_reviewed_flag(tmp_golden: Path) -> None:
    target = "01-contract-award-1688011"
    res = runner.invoke(app, ["review", target], input="e\n")
    assert res.exit_code == 0, res.output
    assert "value = 1325000" in res.output and "[S14]" in res.output and "1.325.000" in res.output
    text = (tmp_golden / "analyst" / f"{target}.yaml").read_text(encoding="utf-8")
    assert "reviewed: true  # confirmed by the owner" in text
    item = next(i for i in analyst.load_items(tmp_golden) if i.id == target)
    assert item.reviewed
    with pytest.raises(ValueError):
        analyst.mark_reviewed(item)
    res = runner.invoke(app, ["review", "02-dividend-1688363"], input="h\n")
    assert "düzeltmek için" in res.output
    assert not next(i for i in analyst.load_items(tmp_golden) if i.id == "02-dividend-1688363").reviewed


# ---------------------------------------------------------------------------------- suite failures


def test_parsers_detect_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    real = parsers.snapshot_all()
    drifted = copy.deepcopy(real)
    doc = drifted["detail_1688011.json"][0]
    doc["form_fields"][10][1] = "28/10/2026"
    doc["span_count"] += 1
    monkeypatch.setattr(parsers, "snapshot_all", lambda fx=None: drifted)
    res = parsers.run()
    assert res.metric("field_exact_match").value < 1 and res.gates_failed
    assert any("detail_1688011.json#body:tr form[10]" in f and "28/10/2026" in f for f in res.failures)
    assert any("span_count" in f for f in res.failures)


def test_parser_snapshots_write_roundtrip(tmp_path: Path) -> None:
    status = parsers.write_snapshots(tmp_path)
    assert set(status.values()) == {"created"} and len(status) >= 38
    assert set(parsers.write_snapshots(tmp_path).values()) == {"unchanged"}
    committed = sorted(p.name for p in (GOLDEN / "parsers").glob("*.json"))
    assert sorted(p.name for p in (tmp_path / "parsers").glob("*.json")) == committed


def test_triage_correctness_rules() -> None:
    item = {"expect": ["governance.gm.notice"], "alt": [["governance.gm.result"]]}
    assert triage.is_correct(item, ["governance.gm.result"])
    assert not triage.is_correct(item, ["governance.gm.notice", "governance.gm.result"])
    from papi.triage import subject_map, taxonomy

    tax = taxonomy.load()
    smap = subject_map.load(taxonomy=tax)
    fx = {"subject": "Sermaye Artırımı - Azaltımı İşlemlerine İlişkin Bildirim", "class": "ODA",
          "summary": None, "fixture": "detail_1687750.json"}  # fmt: skip
    assert triage.predict(fx, tax, smap) == ["capital.increase.rights"]  # summary from the form


def test_verifier_compare_semantics() -> None:
    actual = {"ok": False, "failed_claims": [0], "claim_issue": "çözümlenemeyen span S99", "advice": 2}
    assert verifier.compare({"ok": False, "claim_issue": "S99", "advice": 1, "bad": True}, actual) == []
    assert verifier.compare({"failed_claims": [1]}, actual)
    assert verifier.compare({"advice": 3}, actual)


def test_kb_fixture_loads_and_matches_pipeline_build() -> None:
    committed = json.loads((GOLDEN / kbfixture.KB_FILE).read_text(encoding="utf-8"))
    rebuilt = kbfixture.build()

    def key(d: dict) -> list:
        provisions = {p["id"]: p["citation_id"] for p in d["provision"]}
        return sorted(
            (provisions[v["provision_id"]], v["text"], v["effective_from"]) for v in d["provision_version"]
        )

    assert key(rebuilt) == key(committed)
    from papi import kb

    with scratch_db() as conn:
        kbfixture.load(conn)
        hits = kb.search(conn, "bilgi suistimali", k=3)
        assert hits and hits[0].version.citation_id == "tr:law:6362:m106"


# ---------------------------------------------------------------------------------------- analyst


def test_check_gold_catches_bad_items(tmp_golden: Path) -> None:
    from papi.triage import taxonomy

    tax = taxonomy.load()
    item = next(i for i in analyst.load_items(tmp_golden) if i.id.startswith("01-"))
    assert analyst.check_gold(item, tax) == []
    wrong = copy.deepcopy(item)
    wrong.payload["value"] = 1300000
    wrong.spans["start_date"] = [99]
    issues = analyst.check_gold(wrong, tax)
    assert any("value: 1300000 not in spans" in i for i in issues)
    assert any("span(s) [99] not in the pack" in i for i in issues)
    bad_type = copy.deepcopy(item)
    bad_type.payload["currency"] = 978
    assert any(i.startswith("payload:") for i in analyst.check_gold(bad_type, tax))


def test_values_equal() -> None:
    assert analyst.values_equal("value", 1325000, 1325000.0)
    assert not analyst.values_equal("value", 1325000, 1300000)
    assert analyst.values_equal("currency", "EUR", "eur")
    assert analyst.values_equal("person.name", "Corpus Sigorta", "CORPUS SİGORTA ANONİM ŞİRKETİ")
    assert analyst.values_equal("avg_price", None, None) and not analyst.values_equal("avg_price", None, 7.56)
    assert not analyst.values_equal("vat_included", False, None)


def _gold_output(item: analyst.GoldItem, *, break_value: bool = False) -> dict[str, Any]:
    payload = {
        "counterparty": {"name": None, "issuer_id": None, "is_related_party": None, "is_public_sector": None},
        "is_framework": None,
        "ratios": {"value_to_ttm_revenue": None, "value_to_market_cap": None},
        **analyst._unflatten(dict(item.payload)),
    }
    if break_value:
        payload["value"] = 1500000
    claim = {
        "kind": "fact",
        "text_tr": "Sipariş bedeli KDV hariç 1.325.000 EUR.",
        "text_en": None,
        "span_ordinals": [14],
        "citation_ids": [],
        "value": None,
        "unit": None,
        "formula_id": None,
    }
    return {
        "event_codes": [item.event_code],
        "payload": {item.event_code: payload},
        "payload_spans": {item.event_code: item.spans},
        "fundamental_channels": ["revenue"],
        "fundamental_direction": "positive",
        "materiality": 40,
        "materiality_rationale": "Orta büyüklükte sipariş.",
        "claims": [claim],
        "summary_tr": "MEKAG yurt içinden sipariş aldı.",
        "summary_en": "MEKAG received a domestic order.",
        "headline_tr": "MEKAG sipariş aldı",
        "watch_next": [],
        "confidence": 0.8,
    }


def test_analyst_run_with_scripted_llm(db, tmp_golden: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items = analyst.load_items(tmp_golden)
    first = items[0]
    analyst.mark_reviewed(first)
    calls: list[dict[str, Any]] = []

    def fake_call(conn, **kw: Any) -> LlmResult:
        calls.append(kw)
        out = _gold_output(first, break_value=len(calls) == 2)
        return LlmResult(out, None, ["scripted"], 0.05, 10, 0, None, None, None)

    monkeypatch.setattr("papi.llm.call.call", fake_call)
    res = analyst.run(allow_claude=True, limit=2)
    assert len(calls) == 2
    assert calls[0]["priority"] == "BACKLOG" and calls[0]["task"] == "eval.analyst"
    assert calls[0]["repair_retry"] is False and "[S14]" in calls[0]["prompt"]
    # reviewed item 01 scored perfectly and gated
    assert res.metric("field_accuracy").value == 1.0 and res.metric("field_accuracy").passed
    assert res.metric("span_correctness").value == 1.0
    assert res.metric("citation_coverage").value == 1.0
    assert res.metric("reviewed_run").value == 1 and res.metric("calls").value == 2
    assert res.metric("avg_cost_usd").value == pytest.approx(0.05)
    # item 02 (dividend) answered with the contract payload: wrong code and fields, but only "all_" metrics
    assert res.metric("all_event_code_hit").value == 0.5
    assert any(f.startswith("02-dividend-1688363: event_codes") for f in res.failures)


def test_analyst_untraced_value_is_dropped_and_scored_wrong(db, tmp_golden, monkeypatch) -> None:
    first = analyst.load_items(tmp_golden)[0]
    monkeypatch.setattr(
        "papi.llm.call.call",
        lambda conn, **kw: LlmResult(
            _gold_output(first, break_value=True), None, ["s"], 0.0, 1, 0, None, None, None
        ),
    )
    res = analyst.run(allow_claude=True, limit=1)
    assert res.metric("all_field_accuracy").value < 1
    assert res.metric("all_verifier_clean_rate").value == 0.0
    assert any("value: gold 1325000, got None" in f for f in res.failures)  # verifier nulled 1.500.000


def test_analyst_stops_when_governor_defers(db, tmp_golden, monkeypatch) -> None:
    def deferred(conn, **kw: Any) -> LlmResult:
        raise Deferred(
            __import__("papi.core.timeutil", fromlist=["now_utc"]).now_utc(), "llm:backlog:disabled"
        )

    monkeypatch.setattr("papi.llm.call.call", deferred)
    res = analyst.run(allow_claude=True, limit=3, priority="P2")
    assert res.metric("calls").value == 0
    assert any("not admitted by the governor" in f for f in res.failures)


def test_analyst_requires_initialised_database(tmp_golden) -> None:
    with pytest.raises(core.GoldenMissing, match="papi init"):
        analyst.run(allow_claude=True, limit=1)
