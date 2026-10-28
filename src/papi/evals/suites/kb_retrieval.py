"""KB retrieval suite (PRD 11 §3 "KB retrieval: Recall@5 ≥ 0.9; as-of correctness 100 %").

The committed fixture KB (``tests/golden/kb/kb.json``) is restored into a scratch database, the
drafted obligations (``packs/tr/legal/obligations.yaml``) are loaded unapproved, and the items in
``tests/golden/kb_retrieval.yaml`` are retrieved:

- question items: ``papi.kb.search`` top 5;
- ``kind: event`` items: the analyst context pack's own KB selection (``papi.analyst.context._kb_refs``:
  approved obligations of the code, else full-text search on the KAP subject).

- ``recall_at_5``: mean over items of |expected ∩ retrieved| / |expected| (a returned sub-provision of an
  expected citation counts).
- ``as_of_correctness``: share of checks that hold — every hit is the version in force on the item's
  date, ``absent`` citations are not returned, and ``as_of`` lookups return the right version (or nothing
  before a provision existed).
- ``event_recall_obligations_approved`` (info): event items with every drafted obligation treated as
  approved — what approving the obligations would give the analyst.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import yaml

from papi import kb
from papi.analyst.context import KB_PER_CODE, _kb_refs
from papi.core.timeutil import IST
from papi.evals import kbfixture
from papi.evals.core import Gate, Metric, SuiteResult, golden_dir, ratio
from papi.evals.scratch import scratch_db
from papi.kb.obligation_store import load_obligations

FILE = "kb_retrieval.yaml"
K = 5
MAX_FAILURES = 25


def _covers(hit: str, expected: str) -> bool:
    return hit == expected or hit.startswith(expected + ":")


def _in_force(v: kb.ProvisionVersion, day: date) -> bool:
    return (v.effective_from is None or v.effective_from <= day) and (
        v.effective_to is None or day < v.effective_to
    )


def _recall(expected: list[str], got: list[str]) -> tuple[float, list[str]]:
    missing = [e for e in expected if not any(_covers(g, e) for g in got)]
    return (len(expected) - len(missing)) / len(expected), missing


def _event_refs(conn: Any, item: dict[str, Any], day: date, *, approved_only: bool) -> list[str]:
    if approved_only:
        pub = datetime.combine(day, time(12), tzinfo=IST)
        return list(_kb_refs(conn, [item["event_code"]], item["query"], pub))
    obs = kb.obligations(conn, item["event_code"], as_of=day, approved_only=False)[:KB_PER_CODE]
    return [ob.provision.citation_id for ob in obs if ob.provision is not None]


def run(**_: Any) -> SuiteResult:
    spec = yaml.safe_load((golden_dir() / FILE).read_text(encoding="utf-8"))
    default_day = date.fromisoformat(spec["default_as_of"])
    res = SuiteResult("kb_retrieval")
    recalls: list[float] = []
    event_recalls: list[float] = []
    approved_recalls: list[float] = []
    checks = checks_ok = 0
    with scratch_db() as conn:
        dump = kbfixture.load(conn)
        obligations = load_obligations(conn)
        conn.commit()
        for item in spec["search"]:
            day = date.fromisoformat(item["as_of"]) if item.get("as_of") else default_day
            expected = item.get("expect") or []
            if item.get("kind") == "event":
                got = _event_refs(conn, item, day, approved_only=True)
                approved_recalls.append(
                    _recall(expected, _event_refs(conn, item, day, approved_only=False))[0]
                )
                hits = []
            else:
                hits = kb.search(conn, item["query"], as_of=day, k=K, filters=item.get("filters"))
                got = [h.version.citation_id for h in hits]
            if expected:
                recall, missing = _recall(expected, got)
                (event_recalls if item.get("kind") == "event" else recalls).append(recall)
                if missing and len(res.failures) < MAX_FAILURES:
                    res.failures.append(
                        f"{item['id']} “{item['query']}”: missing {missing}; got {got or '∅'}"
                    )
            for h in hits:
                checks += 1
                if _in_force(h.version, day):
                    checks_ok += 1
                else:
                    res.failures.append(
                        f"{item['id']}: {h.version.citation_id} version {h.version.effective_from}–"
                        f"{h.version.effective_to} returned for {day}"
                    )
            for cid in item.get("absent") or []:
                checks += 1
                if any(_covers(g, cid) for g in got):
                    res.failures.append(f"{item['id']}: {cid} returned on {day} although not in force")
                else:
                    checks_ok += 1
        for item in spec["as_of"]:
            checks += 1
            day = date.fromisoformat(item["as_of"])
            v = kb.lookup(conn, item["citation"], day)
            if item.get("none"):
                ok, why = v is None, f"expected no version, got {v.effective_from if v else None}"
            elif v is None:
                ok, why = False, "no version returned"
            else:
                ok = (item.get("contains") is None or item["contains"] in v.text) and (
                    item.get("not_contains") is None or item["not_contains"] not in v.text
                )
                why = f"version from {v.effective_from} has the wrong text"
            checks_ok += ok
            if not ok:
                res.failures.append(f"{item['id']} {item['citation']} @ {day}: {why}")
    all_recalls = recalls + event_recalls
    res.metrics = [
        Metric("recall_at_5", ratio(sum(all_recalls), len(all_recalls)), Gate(">=", 0.9)),
        Metric("as_of_correctness", ratio(checks_ok, checks), Gate("==", 1.0)),
        Metric("question_recall_at_5", ratio(sum(recalls), len(recalls))),
        Metric("event_recall", ratio(sum(event_recalls), len(event_recalls)), note="analyst context KB refs"),
        Metric(
            "event_recall_obligations_approved",
            ratio(sum(approved_recalls), len(approved_recalls)),
            note="if every drafted obligation were approved",
        ),
        Metric("search_items", len(spec["search"]), kind="count"),
        Metric("as_of_checks", checks, kind="count"),
    ]
    res.info = {
        "kb": {t: len(dump[t]) for t in ("regulation", "provision", "provision_version")},
        "entries": sorted(dump["entries"]),
        "obligations_loaded": obligations.inserted,
        "obligations_missing_provision": len(obligations.missing_provisions),
    }
    return res
