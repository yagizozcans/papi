"""Run eval suites, compare with the accepted baseline, render the metric table."""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from papi.evals.core import (
    DiffRow,
    Gate,
    Metric,
    SuiteResult,
    apply_baseline_gates,
    diff,
    load_baseline,
)
from papi.evals.suites import ALL_SUITES, runner


@dataclass
class EvalRun:
    results: list[SuiteResult]
    baseline: dict[str, Any]
    rows: list[DiffRow] = field(default_factory=list)

    @property
    def failed_gates(self) -> list[tuple[str, Metric]]:
        return [(r.suite, m) for r in self.results for m in r.gates_failed]

    @property
    def passed(self) -> bool:
        return not self.failed_gates

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "baseline_accepted_at": self.baseline.get("accepted_at"),
            "suites": [r.to_json() for r in self.results],
            "diff": [
                {
                    "suite": row.suite,
                    "metric": row.metric.name,
                    "value": row.metric.value,
                    "baseline": row.baseline,
                    "delta": row.delta,
                }
                for row in self.rows
            ],
        }


def select_suites(names: list[str] | None) -> list[str]:
    if not names:
        return list(ALL_SUITES)  # without --allow-claude the analyst suite only checks its gold files
    unknown = [n for n in names if n not in ALL_SUITES]
    if unknown:
        raise ValueError(f"unknown suite(s) {unknown}; available: {', '.join(ALL_SUITES)}")
    return list(dict.fromkeys(names))


def run_suite(name: str, **options: Any) -> SuiteResult:
    try:
        return runner(name)(**options)
    except Exception as exc:  # a crashing suite is a failed gate, with the error as the example
        res = SuiteResult(name)
        res.failures = [
            f"suite crashed: {type(exc).__name__}: {exc}",
            *traceback.format_exc().splitlines()[-6:],
        ]
        res.metrics = [Metric("suite_error", 1, Gate("==", 0), kind="count")]
        return res


def run(
    names: list[str] | None = None,
    *,
    allow_claude: bool = False,
    limit: int | None = None,
    priority: str | None = None,
) -> EvalRun:
    selected = select_suites(names)
    results = [run_suite(n, allow_claude=allow_claude, limit=limit, priority=priority) for n in selected]
    baseline = load_baseline()
    apply_baseline_gates(results, baseline)
    return EvalRun(results, baseline, diff(results, baseline))


def _delta(row: DiffRow) -> str:
    d = row.delta
    if d is None:
        return "yeni" if row.baseline is None and row.metric.value is not None else ""
    if abs(d) < 1e-12:
        return "="
    if row.metric.kind == "ratio":
        return f"{d * 100:+.2f} pp"
    if row.metric.kind == "usd":
        return f"{d:+.4f}"
    return f"{d:+g}"


def _gate(m: Metric) -> str:
    return f"{m.gate.op} {m.display(m.gate.threshold)}" if m.gate else ""


def render(run: EvalRun, console: Console, *, max_failures: int = 12) -> None:
    table = Table(title="papi eval", show_lines=False)
    for col in ("paket", "metrik", "değer", "taban", "Δ", "eşik", ""):
        table.add_column(col, overflow="fold")
    for row in run.rows:
        m = row.metric
        status = {True: "OK", False: "FAIL", None: ""}[m.passed]
        table.add_row(
            row.suite,
            m.name,
            m.display(),
            "—" if row.baseline is None else m.display(row.baseline),
            _delta(row),
            _gate(m),
            status,
        )
    console.print(table)
    for r in run.results:
        if r.skipped:
            console.print(f"[{r.suite}] atlandı: {r.skipped}")
            for m in r.metrics:
                verdict = f"  (eşik {_gate(m)}: {'OK' if m.passed else 'FAIL'})" if m.gate else ""
                console.print(f"  {m.name}: {m.display()}{verdict}")
        if r.failures:
            console.print(f"\n[{r.suite}] {len(r.failures)} örnek hata/uyarı:")
            for f in r.failures[:max_failures]:
                console.print(f"  - {f}")
            if len(r.failures) > max_failures:
                console.print(f"  … {len(r.failures) - max_failures} more (use --json)")
    accepted = run.baseline.get("accepted_at")
    console.print(f"\ntaban: {accepted or 'yok (papi eval --baseline accept)'}")
    if run.passed:
        console.print("Tüm eşikler geçti.")
    else:
        failed = ", ".join(f"{s}.{m.name}={m.display()} (eşik {_gate(m)})" for s, m in run.failed_gates)
        console.print(f"EŞİK BAŞARISIZ: {failed}")


def render_json(run: EvalRun, console: Console) -> None:
    console.print(json.dumps(run.to_json(), ensure_ascii=False, indent=2, default=str))
