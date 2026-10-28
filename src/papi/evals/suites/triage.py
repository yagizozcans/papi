"""Rules-triage suite (PRD 11 §3 "Rules triage: code accuracy ≥ 90 %; other.unclassified share < 5 %").

``tests/golden/triage.yaml`` holds real KAP disclosures with hand-assigned event codes. Each item is
classified with the triage slice's subject map exactly as ``papi.triage.classify`` does (subject, class,
type, summary — falling back to the form's "Özet Bilgi" — and, for recorded fixtures, the parsed body:
template, form fields, tables, body text; plus the derived threshold crossing of insider trades).
An item is correct when the predicted code set equals ``expect`` or one of its ``alt`` sets.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from papi.core.text import normalize_tr
from papi.evals.core import Gate, Metric, SuiteResult, golden_dir, ratio
from papi.evals.suites.parsers import fixture_dir
from papi.sources.kap.parsing.html import parse_body
from papi.triage import payload as payload_mod
from papi.triage import subject_map as sm_mod
from papi.triage import taxonomy as tax_mod
from papi.triage.classify import FORM_SUMMARY_LABELS, INSIDER_CODE, THRESHOLD_CODE

FILE = "triage.yaml"
UNCLASSIFIED = "other.unclassified"
MAX_FAILURES = 40


def load_items(base: Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load(((base or golden_dir()) / FILE).read_text(encoding="utf-8"))
    return data["items"]


def _form_value(structure: dict[str, Any], labels: tuple[str, ...]) -> str | None:
    for f in structure.get("form_fields") or []:
        if f.get("label_norm") in labels and f.get("value") not in (None, "", "-"):
            return str(f["value"])
    return None


def _body(fixture: str) -> tuple[str, dict[str, Any]]:
    path = fixture_dir() / fixture
    raw = path.read_bytes()
    data = json.loads((gzip.decompress(raw) if path.suffix == ".gz" else raw).decode("utf-8"))
    parts = data[0]["disclosureBody"]
    doc = parse_body(parts if isinstance(parts, list) else [parts])
    return doc.text, doc.structure


def predict(item: dict[str, Any], tax: tax_mod.Taxonomy, smap: sm_mod.SubjectMap) -> list[str]:
    body_text, structure = (None, {})
    if item.get("fixture"):
        body_text, structure = _body(item["fixture"])
    summary = item.get("summary") or _form_value(structure, FORM_SUMMARY_LABELS)
    match = smap.match(
        sm_mod.MatchInput(
            subject=item.get("subject"),
            source_class=item.get("class"),
            source_type=item.get("type"),
            summary=summary,
            body_text=body_text,
            template_id=structure.get("template_id"),
            form_fields=structure.get("form_fields") or [],
            tables=structure.get("tables") or [],
        )
    )
    codes = list(match.codes)
    if INSIDER_CODE in codes and THRESHOLD_CODE not in codes and structure:
        meta = {"subject": item.get("subject"), "summary": item.get("summary")}
        res = payload_mod.build(INSIDER_CODE, tax, structure=structure, meta=meta, summary=summary)
        if payload_mod.threshold_crossing(res.payload) is not None:
            codes.append(THRESHOLD_CODE)
    return codes


def is_correct(item: dict[str, Any], predicted: list[str]) -> bool:
    got = set(predicted)
    return any(got == set(s) for s in [item["expect"], *(item.get("alt") or [])])


def run(**_: Any) -> SuiteResult:
    items = load_items()
    tax = tax_mod.load()
    smap = sm_mod.load(taxonomy=tax)
    res = SuiteResult("triage")
    correct = unclassified = overlap = 0
    by_subject: Counter[str] = Counter()
    unknown: set[str] = set()
    for item in items:
        for codes in [item["expect"], *(item.get("alt") or [])]:
            unknown |= {c for c in codes if c not in tax.codes}
        pred = predict(item, tax, smap)
        ok = is_correct(item, pred)
        correct += ok
        unclassified += UNCLASSIFIED in pred
        overlap += bool(set(pred) & set(item["expect"]))
        if not ok:
            by_subject[normalize_tr(item.get("subject") or "")] += 1
            if len(res.failures) < MAX_FAILURES:
                res.failures.append(
                    f"{item['id']} [{item.get('subject')} | {item.get('summary') or '-'}]: "
                    f"expected {', '.join(item['expect'])}, got {', '.join(pred)}"
                )
    if unknown:
        res.failures.insert(0, f"golden labels use codes missing from the taxonomy: {sorted(unknown)}")
    n = len(items)
    res.metrics = [
        Metric("code_accuracy", ratio(correct, n), Gate(">=", 0.90)),
        Metric("unclassified_share", ratio(unclassified, n), Gate("<", 0.05)),
        Metric("any_code_overlap", ratio(overlap, n), note="at least one expected code predicted"),
        Metric("items", n, kind="count"),
    ]
    if unknown:
        res.metrics.append(Metric("unknown_label_codes", len(unknown), Gate("==", 0), kind="count"))
    res.info = {
        "taxonomy_version": tax.version,
        "sources": dict(Counter(i["source"].split(":")[0] for i in items)),
        "misses_by_subject": dict(by_subject.most_common(10)),
    }
    return res
