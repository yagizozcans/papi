"""The pure-Python eval suites as regression tests (PRD 11 §3: parser/rules evals run on every commit).

Every gated metric must pass its gate and must not be worse than the accepted baseline
(``tests/golden/baseline.json``). A gate the accepted baseline already recorded as failing (a known gap,
accepted with ``papi eval --baseline accept --force --note …``) is tolerated as long as it does not get
worse. Improving a metric never fails; re-accept the baseline to lock it in.
"""

from __future__ import annotations

import pytest

from papi.evals.core import Metric, load_baseline, metric_key
from papi.evals.runner import run_suite
from papi.evals.suites import PURE_SUITES

EPS = 1e-9


def higher_is_better(m: Metric) -> bool:
    assert m.gate is not None
    if m.gate.op in (">=", ">"):
        return True
    if m.gate.op in ("<=", "<"):
        return False
    return m.gate.threshold > 0  # "== 1" (100 %) vs "== 0" (count of bad things)


def worse(m: Metric, base: float) -> bool:
    if m.value is None:
        return True
    return m.value < base - EPS if higher_is_better(m) else m.value > base + EPS


def regressions(suite: str, metrics: list[Metric], baseline: dict) -> list[str]:
    accepted = baseline.get("metrics") or {}
    out = []
    for m in metrics:
        if m.gate is None:
            continue
        base = accepted.get(metric_key(suite, m.name))
        if base is not None and worse(m, base):
            out.append(f"{suite}.{m.name} regressed: {m.display()} (accepted {m.display(base)})")
        elif not m.passed and (base is None or m.gate.check(base)):
            out.append(f"{suite}.{m.name} fails its gate {m.gate}: {m.display()}")
    return out


@pytest.mark.parametrize("suite", PURE_SUITES)
def test_pure_suite_gates_and_baseline(suite: str) -> None:
    res = run_suite(suite)
    assert res.skipped is None
    problems = regressions(suite, res.metrics, load_baseline())
    assert not problems, "\n".join([*problems, "examples:", *res.failures[:20]])


def test_analyst_gold_files_consistent() -> None:
    res = run_suite("analyst")  # no Claude: gold files only
    assert res.skipped and res.metric("gold_consistency").value == 1.0, res.failures


def test_baseline_covers_every_gated_pure_metric() -> None:
    accepted = load_baseline().get("metrics") or {}
    assert accepted, "no accepted baseline (papi eval --baseline accept)"
    assert any(k.startswith("parsers.") for k in accepted)
    for suite in PURE_SUITES:
        assert any(k.startswith(f"{suite}.") for k in accepted), suite


def test_regression_rules() -> None:
    from papi.evals.core import Gate

    acc = Metric("code_accuracy", 0.93, Gate(">=", 0.90))
    assert regressions("triage", [acc], {"metrics": {"triage.code_accuracy": 0.94}})
    assert not regressions("triage", [acc], {"metrics": {"triage.code_accuracy": 0.93}})
    known_gap = Metric("recall_at_5", 0.87, Gate(">=", 0.9))
    assert not regressions("kb", [known_gap], {"metrics": {"kb.recall_at_5": 0.87}})
    assert regressions("kb", [known_gap], {"metrics": {"kb.recall_at_5": 0.88}})
    assert regressions("kb", [known_gap], {"metrics": {}})
    silent = Metric("silent_guesses", 1, Gate("==", 0), kind="count")
    assert regressions("trade_parsing", [silent], {"metrics": {"trade_parsing.silent_guesses": 0}})
