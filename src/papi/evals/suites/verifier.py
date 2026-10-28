"""Verifier suite: canned analyst outputs → expected ``papi.verify.verify`` verdicts.

Cases in ``tests/golden/verifier.yaml`` cover a clean output, hallucinated numbers (claim, summary, wrong
span), missing / unresolvable spans, payload fields without or contradicting their spans, advice
language (TR/EN), ordinals, derived values (good, wrong, no formula), KB citations, Turkish number and
date formats, and schema violations. ``outcome_accuracy`` must be 100 % (PRD 11 §3 "Summaries: every
number traceable 100 %"); ``defect_catch_rate`` / ``false_alarm_rate`` summarise the same cases.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

from papi.analyst.spec import analyst_spec
from papi.evals.core import Gate, Metric, SuiteResult, golden_dir, ratio
from papi.triage import taxonomy as tax_mod
from papi.verify import VerifyReport, VerifySources, verify

FILE = "verifier.yaml"


def _sources(base: dict[str, Any], override: dict[str, Any] | None, tax: tax_mod.Taxonomy) -> VerifySources:
    s = {**base, **(override or {})}
    schemas = {c: sch for c in s.get("payload_schemas_from") or [] if (sch := tax.schema_for(c)) is not None}
    return VerifySources(
        spans={int(k): v for k, v in (s.get("spans") or {}).items()},
        provisions=dict(s.get("provisions") or {}),
        context_text=s.get("context_text") or "",
        allowed_codes=set(s["allowed_codes"]) if s.get("allowed_codes") is not None else None,
        payload_schemas=schemas,
        form_payloads=dict(s.get("form_payloads") or {}),
    )


def _actual(rep: VerifyReport) -> dict[str, Any]:
    failed = sorted(rep.failed_claims())
    first = next((c for c in rep.claims if c.status == "fail"), None)
    return {
        "ok": rep.ok,
        "failed_claims": failed,
        "claim_issue": " | ".join(first.issues) if first else "",
        "dropped": sorted(name for name, t in rep.texts.items() if t.dropped),
        "payload_fields": {code: sorted(f) for code, f in rep.payload_fields.items()},
        "payload_errors": sorted(rep.payload_errors),
        "advice": len(rep.advice),
        "unknown_codes": sorted(rep.unknown_codes),
        "schema_error": bool(rep.schema_errors),
        "watch_next_dropped": sum(1 for w in rep.watch_next if not w["kept"]),
    }


def compare(expect: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    bad = []
    for key, want in expect.items():
        if key == "bad":
            continue
        got = actual[key]
        if key == "claim_issue":
            if want not in got:
                bad.append(f"claim issue “{want}” not in “{got}”")
            continue
        if key == "advice" and want > 0:
            if got < want:
                bad.append(f"advice: expected ≥ {want} hits, got {got}")
            continue
        if key == "payload_fields":
            want = {c: sorted(f) for c, f in want.items()}
        elif isinstance(want, list):
            want = sorted(want)
        if got != want:
            bad.append(f"{key}: expected {want}, got {got}")
    return bad


def run(**_: Any) -> SuiteResult:
    spec = yaml.safe_load((golden_dir() / FILE).read_text(encoding="utf-8"))
    tax = tax_mod.load()
    schema = analyst_spec().schema
    res = SuiteResult("verifier")
    passed = caught = bad_cases = false_alarms = clean_cases = 0
    for case in spec["cases"]:
        out = copy.deepcopy(spec["base_output"])
        out.update(copy.deepcopy(case.get("output") or {}))
        for key in case.get("drop") or []:
            out.pop(key, None)
        rep = verify(
            out,
            _sources(spec["sources"], case.get("sources"), tax),
            schema=schema if case.get("schema") else None,
        )
        problems = compare(case["expect"], _actual(rep))
        passed += not problems
        if problems:
            res.failures.append(f"{case['id']}: " + "; ".join(problems))
        if case["expect"].get("bad"):
            bad_cases += 1
            caught += not rep.ok
        else:
            clean_cases += 1
            false_alarms += not rep.ok
    n = len(spec["cases"])
    res.metrics = [
        Metric("outcome_accuracy", ratio(passed, n), Gate("==", 1.0)),
        Metric("defect_catch_rate", ratio(caught, bad_cases)),
        Metric("false_alarm_rate", ratio(false_alarms, clean_cases)),
        Metric("cases", n, kind="count"),
    ]
    return res
