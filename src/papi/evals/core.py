"""Eval result model, PRD 11 §3 gates, the accepted baseline and its diff.

A suite returns a :class:`SuiteResult` with named :class:`Metric` values. A metric may carry a
:class:`Gate`; ``papi eval`` exits non-zero when any gate fails. ``tests/golden/baseline.json`` holds the
last accepted metric values (``papi eval --baseline accept``); every run prints the diff against it.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from papi.core.config import REPO_ROOT
from papi.core.timeutil import iso_utc, now_utc

GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
BASELINE_FILE = "baseline.json"
COST_TOLERANCE = 0.10  # PRD 11 §3: avg cost not above the previous baseline +10 %

_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}


class GoldenMissing(FileNotFoundError):
    """The golden data a suite needs is not present (e.g. running from an installed wheel)."""


def golden_dir() -> Path:
    if not GOLDEN_DIR.is_dir():
        raise GoldenMissing(
            f"golden set directory not found: {GOLDEN_DIR} (run from the repository checkout)"
        )
    return GOLDEN_DIR


@dataclass(frozen=True)
class Gate:
    op: str
    threshold: float

    def check(self, value: float | None) -> bool:
        if value is None:
            return False
        if self.op == "==":
            return abs(value - self.threshold) < 1e-12
        return _OPS[self.op](value, self.threshold)

    def __str__(self) -> str:
        return f"{self.op} {_fmt_plain(self.threshold)}"


@dataclass
class Metric:
    name: str
    value: float | None
    gate: Gate | None = None
    kind: str = "ratio"  # ratio | count | usd | number
    note: str = ""

    @property
    def passed(self) -> bool | None:
        return None if self.gate is None else self.gate.check(self.value)

    def display(self, value: float | None = None) -> str:
        v = self.value if value is None else value
        if v is None:
            return "—"
        if self.kind == "ratio":
            return f"{v * 100:.2f} %"
        if self.kind == "count":
            return str(int(v))
        if self.kind == "usd":
            return f"${v:.4f}"
        return _fmt_plain(v)


def _fmt_plain(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.4g}"


@dataclass
class SuiteResult:
    suite: str
    metrics: list[Metric] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # concrete examples of what went wrong
    info: dict[str, Any] = field(default_factory=dict)
    skipped: str | None = None

    def metric(self, name: str) -> Metric:
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(name)

    @property
    def gates_failed(self) -> list[Metric]:
        return [m for m in self.metrics if m.passed is False]

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "skipped": self.skipped,
            "metrics": {
                m.name: {
                    "value": m.value,
                    "gate": str(m.gate) if m.gate else None,
                    "passed": m.passed,
                    "note": m.note or None,
                }
                for m in self.metrics
            },
            "failures": self.failures,
            "info": self.info,
        }


def ratio(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


# ------------------------------------------------------------------------------------------ baseline


def baseline_path(base: Path | None = None) -> Path:
    return (base or GOLDEN_DIR) / BASELINE_FILE


def load_baseline(path: Path | None = None) -> dict[str, Any]:
    p = path or baseline_path()
    if not p.exists():
        return {"metrics": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def metric_key(suite: str, name: str) -> str:
    return f"{suite}.{name}"


def accept_baseline(results: list[SuiteResult], path: Path | None = None, *, note: str | None = None) -> Path:
    """Write the metrics of the suites that ran (others keep their accepted values)."""
    p = path or baseline_path()
    data = load_baseline(p)
    metrics = dict(data.get("metrics") or {})
    for r in results:
        if r.skipped:
            continue
        for m in r.metrics:
            if m.value is not None:
                metrics[metric_key(r.suite, m.name)] = m.value
    out = {"accepted_at": iso_utc(now_utc()), "note": note, "metrics": dict(sorted(metrics.items()))}
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def apply_baseline_gates(results: list[SuiteResult], baseline: dict[str, Any]) -> None:
    """Gates relative to the accepted baseline: average analyst cost ≤ baseline × (1 + 10 %)."""
    accepted = baseline.get("metrics") or {}
    for r in results:
        for m in r.metrics:
            if m.kind == "usd" and m.gate is None:
                prev = accepted.get(metric_key(r.suite, m.name))
                if prev is not None and m.value is not None:
                    m.gate = Gate("<=", round(prev * (1 + COST_TOLERANCE), 6))
                    m.note = (m.note + "; " if m.note else "") + "gate = accepted baseline +10 %"


@dataclass
class DiffRow:
    suite: str
    metric: Metric
    baseline: float | None

    @property
    def delta(self) -> float | None:
        if self.baseline is None or self.metric.value is None:
            return None
        return self.metric.value - self.baseline


def diff(results: list[SuiteResult], baseline: dict[str, Any]) -> list[DiffRow]:
    accepted = baseline.get("metrics") or {}
    return [
        DiffRow(r.suite, m, accepted.get(metric_key(r.suite, m.name)))
        for r in results
        if not r.skipped
        for m in r.metrics
    ]
