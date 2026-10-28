"""Eval suites (PRD 11 §3). Pure-Python suites run by default; ``analyst`` calls Claude only when
explicitly allowed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from papi.evals.core import SuiteResult

PURE_SUITES = ("parsers", "triage", "trade_parsing", "kb_retrieval", "verifier")
CLAUDE_SUITES = ("analyst",)
ALL_SUITES = PURE_SUITES + CLAUDE_SUITES


def runner(name: str) -> Callable[..., SuiteResult]:
    if name not in ALL_SUITES:
        raise KeyError(name)
    from importlib import import_module

    module: Any = import_module(f"papi.evals.suites.{name}")
    return module.run
