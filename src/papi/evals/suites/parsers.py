"""Parser suite (PRD 11 §3 "Parsers: field exact match on fixtures, 100 %").

Every recorded KAP fixture in ``tests/fixtures/kap`` that is a parser input goes through the KAP slice's
parser; the structured output is compared with the snapshot in ``tests/golden/parsers/<fixture>.json``
(form fields, table shapes, span counts, text hash, …). Each snapshot key / form field / table is one
"field". ``papi eval snapshot`` rewrites the snapshots after an intended parser change.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from papi.core.config import REPO_ROOT
from papi.evals.core import Gate, GoldenMissing, Metric, SuiteResult, golden_dir, ratio
from papi.sources.kap.capital import CapitalSectionMissing, parse_company_general
from papi.sources.kap.client import unwrap_java_bytes
from papi.sources.kap.common import item_tickers, published_at
from papi.sources.kap.companies import split_codes
from papi.sources.kap.parsing.doc import ParsedDocument
from papi.sources.kap.parsing.files import parse_file
from papi.sources.kap.parsing.html import languages, parse_body

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "kap"
SUBDIR = "parsers"
# Recorded responses that are not parser inputs (an HTTP 500 body; member lookups used as-is).
NOT_PARSED = ("detail_1689500_nonexistent_http500.json", "member_filter_")
MAX_FAILURES = 25


def fixture_dir() -> Path:
    if not FIXTURE_DIR.is_dir():
        raise GoldenMissing(f"KAP fixtures not found: {FIXTURE_DIR}")
    return FIXTURE_DIR


def _read(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def _json(path: Path) -> Any:
    return json.loads(_read(path).decode("utf-8"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def doc_snapshot(doc: ParsedDocument) -> dict[str, Any]:
    st = doc.structure
    prefixes = Counter(s.field_path.split(":", 1)[0] for s in doc.spans)
    return {
        "kind": doc.kind,
        "language": doc.language,
        "mime": doc.mime,
        "template_id": doc.template_id,
        "families": st.get("families"),
        "span_count": len(doc.spans),
        "span_paths": dict(sorted(prefixes.items())),
        "text_len": len(doc.text),
        "text_sha": _sha(doc.text),
        "paragraphs": len(st.get("paragraphs") or []),
        "form_fields": [
            [f.get("field_id") or f.get("label_norm"), f.get("value")] for f in st.get("form_fields") or []
        ],
        "tables": [
            {
                "caption": t.get("caption"),
                "header_len": len(t.get("header") or []),
                "rows": len(t.get("rows") or []),
                "cols": max((len(r) for r in t.get("rows") or []), default=0),
            }
            for t in st.get("tables") or []
        ],
        "extra": {k: doc.extra[k] for k in sorted(doc.extra) if k in ("pages", "unsupported", "ocr_needed")},
    }


def _attachment_names(fx: Path) -> dict[str, str]:
    """objId → fileName from the recorded detail responses."""
    names: dict[str, str] = {}
    for p in sorted(fx.glob("detail_*.json*")):
        data = _json(p)
        if not isinstance(data, list):
            continue
        for att in data[0].get("attachments") or []:
            if att.get("objId"):
                names[att["objId"]] = att.get("fileName") or ""
    return names


def _detail_targets(path: Path) -> list[dict[str, Any]]:
    data = _json(path)
    parts = data[0]["disclosureBody"]
    parts = parts if isinstance(parts, list) else [parts]
    out = []
    for lang in languages(parts):
        out.append({"target": f"body:{lang}", **doc_snapshot(parse_body(parts, language=lang))})
    return out


def _capital_target(path: Path) -> list[dict[str, Any]]:
    try:
        cap = parse_company_general(_read(path))
    except CapitalSectionMissing as exc:
        return [{"target": "capital", "error": type(exc).__name__}]
    return [
        {
            "target": "capital",
            "paid_in_capital": str(cap.paid_in_capital),
            "registered_capital_ceiling": None
            if cap.registered_capital_ceiling is None
            else str(cap.registered_capital_ceiling),
            "share_groups": cap.share_groups,
            "free_float": {
                code: {k: None if v is None else str(v) for k, v in vals.items()}
                for code, vals in sorted(cap.free_float.items())
            },
        }
    ]


def _digest(rows: list[Any]) -> str:
    return _sha(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str))


def _companies_target(path: Path) -> list[dict[str, Any]]:
    rows = [[i.get("stockCode"), *split_codes(i.get("stockCode"))] for i in _json(path)]
    return [
        {
            "target": "companies",
            "items": len(rows),
            "tickers": sum(len(r[1]) for r in rows),
            "member_codes": sum(len(r[2]) for r in rows),
            "multi_code_items": sum(1 for r in rows if len(r[1]) + len(r[2]) > 1),
            "digest": _digest(rows),
        }
    ]


def _list_target(path: Path) -> list[dict[str, Any]]:
    items = _json(path)
    rows = []
    for i in items:
        filer, related = item_tickers(i)
        ts = published_at(i)
        rows.append([i.get("disclosureIndex"), filer, related, ts.isoformat() if ts else None])
    stamps = [r[3] for r in rows if r[3]]
    return [
        {
            "target": "list",
            "items": len(rows),
            "unparsed_dates": sum(1 for r in rows if r[3] is None),
            "filer_codes": sum(len(r[1]) for r in rows),
            "related_codes": sum(len(r[2]) for r in rows),
            "first_published": min(stamps) if stamps else None,
            "last_published": max(stamps) if stamps else None,
            "digest": _digest(rows),
        }
    ]


def snapshot_fixture(path: Path, names: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    """Structured parser output for one fixture file (``None`` = not a parser input)."""
    name = path.name
    if any(name.startswith(p) for p in NOT_PARSED):
        return None
    if name.startswith("detail_"):
        return _detail_targets(path)
    if name.startswith("file_"):
        obj = name.removeprefix("file_").split(".")[0]
        fname = (names or {}).get(obj) or None
        doc = parse_file(unwrap_java_bytes(_read(path)), file_name=fname)
        return [{"target": "attachment", "file_name": fname, **doc_snapshot(doc)}]
    if name.startswith("bildirimpdf_"):
        return [{"target": "kap_pdf", **doc_snapshot(parse_file(_read(path), kind="kap_pdf"))}]
    if name.startswith("company_genel_"):
        return _capital_target(path)
    if name.startswith("companies_"):
        return _companies_target(path)
    if name.startswith("list_"):
        return _list_target(path)
    return None


def fixture_files(fx: Path | None = None) -> list[Path]:
    fx = fx or fixture_dir()
    return sorted(p for p in fx.iterdir() if p.is_file() and p.name != "README.md")


def golden_name(path: Path) -> str:
    return path.name.split(".")[0] + ".json"


def snapshot_all(fx: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    fx = fx or fixture_dir()
    names = _attachment_names(fx)
    out: dict[str, list[dict[str, Any]]] = {}
    for p in fixture_files(fx):
        snap = snapshot_fixture(p, names)
        if snap is not None:
            out[p.name] = snap
    return out


def write_snapshots(base: Path | None = None) -> dict[str, str]:
    """Rewrite ``tests/golden/parsers/*.json``; returns {fixture: created|changed|unchanged|removed}."""
    target = (base or golden_dir()) / SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    wanted: set[str] = set()
    for fixture, snap in snapshot_all().items():
        gp = target / golden_name(Path(fixture))
        wanted.add(gp.name)
        blob = json.dumps({"fixture": fixture, "documents": snap}, ensure_ascii=False, indent=1) + "\n"
        old = gp.read_text(encoding="utf-8") if gp.exists() else None
        status[fixture] = "created" if old is None else ("unchanged" if old == blob else "changed")
        if old != blob:
            gp.write_text(blob, encoding="utf-8")
    for gp in target.glob("*.json"):
        if gp.name not in wanted:
            gp.unlink()
            status[gp.name] = "removed"
    return status


def _fields(doc: dict[str, Any]) -> dict[str, Any]:
    """One comparable "field" per snapshot key, form field and table."""
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if k == "form_fields":
            for i, f in enumerate(v):
                out[f"form[{i}]:{f[0]}"] = f[1]
        elif k == "tables":
            for i, t in enumerate(v):
                out[f"table[{i}]"] = t
        else:
            out[k] = v
    return out


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= 90 else s[:87] + "…"


def run(**_: Any) -> SuiteResult:
    base = golden_dir() / SUBDIR
    res = SuiteResult("parsers")
    actual = snapshot_all()
    expected_files = {p.name: p for p in base.glob("*.json")}
    total = equal = docs = docs_ok = 0
    seen: set[str] = set()
    for fixture, docs_actual in actual.items():
        gname = golden_name(Path(fixture))
        seen.add(gname)
        gp = expected_files.get(gname)
        if gp is None:
            res.failures.append(f"{fixture}: no golden snapshot (run `papi eval snapshot`)")
            total += 1
            continue
        docs_expected = json.loads(gp.read_text(encoding="utf-8"))["documents"]
        by_target = {d["target"]: d for d in docs_actual}
        for exp in docs_expected:
            docs += 1
            got = by_target.pop(exp["target"], None)
            ef = _fields(exp)
            gf = _fields(got) if got is not None else {}
            bad = [k for k in sorted(set(ef) | set(gf)) if ef.get(k, "<missing>") != gf.get(k, "<missing>")]
            total += len(set(ef) | set(gf))
            equal += len(set(ef) | set(gf)) - len(bad)
            if not bad:
                docs_ok += 1
            for k in bad[:3]:
                if len(res.failures) < MAX_FAILURES:
                    res.failures.append(
                        f"{fixture}#{exp['target']} {k}: expected {_short(ef.get(k, '<missing>'))}, "
                        f"got {_short(gf.get(k, '<missing>'))}"
                    )
        for extra in by_target:
            docs += 1
            total += 1
            res.failures.append(f"{fixture}#{extra}: document not in the golden snapshot")
    for gname in sorted(set(expected_files) - seen):
        total += 1
        docs += 1
        res.failures.append(f"{gname}: golden snapshot without fixture")
    res.metrics = [
        Metric("field_exact_match", ratio(equal, total), Gate("==", 1.0)),
        Metric("documents_exact", ratio(docs_ok, docs)),
        Metric("fields", total, kind="count"),
        Metric("documents", docs, kind="count"),
    ]
    res.info = {"fixtures": len(actual), "not_parser_inputs": list(NOT_PARSED)}
    return res
