"""Trade-message suite (PRD 11 §3 "Trade parsing: exact transaction on supported patterns 100 %;
unsupported → asks instead of guessing, 0 silent guesses").

Reads the telegram slice's evaluation set ``tests/telegram/trade_phrases.yaml`` (read-only) and runs
``papi.telegram.trade_parser.parse``. A case is exact when the outcome kind and every expected field
(side, instrument, quantity or fraction, price, date, time, fees; the missing-field set for
``needs_info``; the reason for ``not_trade``) match. A *silent guess* is a ``ParsedTrade`` where the set
expects a question / no trade, or a ``ParsedTrade`` with a wrong field value.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from papi.core.config import REPO_ROOT
from papi.core.timeutil import IST
from papi.evals.core import Gate, GoldenMissing, Metric, SuiteResult, ratio
from papi.telegram.trade_parser import NeedsInfo, NotATrade, ParsedTrade, parse

PHRASES = REPO_ROOT / "tests" / "telegram" / "trade_phrases.yaml"
MAX_FAILURES = 25


def _fraction(raw: Any) -> Decimal:
    text = str(raw)
    if "/" in text:
        a, b = text.split("/")
        return Decimal(a) / Decimal(b)
    return Decimal(text)


def trade_mismatches(case: dict[str, Any], r: ParsedTrade, now: datetime) -> list[str]:
    bad: list[str] = []

    def chk(name: str, got: Any, want: Any) -> None:
        if got != want:
            bad.append(f"{name}: {got} ≠ {want}")

    chk("side", r.side, case["side"])
    chk("ref", r.instrument_ref, case["ref"])
    chk("price", r.price, Decimal(case["price"]))
    if "fraction" in case:
        chk("quantity", r.quantity, None)
        chk("fraction", r.fraction, _fraction(case["fraction"]))
    else:
        chk("fraction", r.fraction, None)
        chk("quantity", r.quantity, Decimal(str(case["qty"])))
    want_date = date.fromisoformat(case.get("date", now.astimezone(IST).date().isoformat()))
    chk("date", r.trade_date, want_date)
    if "time" in case:
        local = r.traded_at.astimezone(IST) if r.traded_at else None
        chk("time", local.strftime("%H:%M") if local else None, case["time"])
        chk("time_date", local.date() if local else None, want_date)
    elif "date" in case:
        chk("traded_at", r.traded_at, None)
    else:
        chk("traded_at", r.traded_at, now)
    chk("fees", r.fees, Decimal(case["fees"]) if "fees" in case else None)
    return bad


def run(**_: Any) -> SuiteResult:
    if not PHRASES.exists():
        raise GoldenMissing(f"trade phrase set not found: {PHRASES}")
    data = yaml.safe_load(Path(PHRASES).read_text(encoding="utf-8"))
    now = datetime.fromisoformat(data["now"])
    cases = data["cases"]
    res = SuiteResult("trade_parsing")
    exact = silent = 0
    by_expect = {"trade": [0, 0], "needs_info": [0, 0], "not_trade": [0, 0]}
    for case in cases:
        result = parse(case["text"], now=now)
        expect = case["expect"]
        issue: str | None = None
        if expect == "trade":
            if isinstance(result, ParsedTrade):
                bad = trade_mismatches(case, result, now)
                if bad:
                    silent += 1
                    issue = "wrong values: " + "; ".join(bad)
            else:
                issue = f"expected a trade, got {type(result).__name__}"
        elif isinstance(result, ParsedTrade):
            silent += 1
            issue = f"silent guess: expected {expect}, got a trade"
        elif expect == "needs_info":
            if not isinstance(result, NeedsInfo):
                issue = f"expected needs_info, got {type(result).__name__}"
            elif set(result.missing) != set(case["missing"]):
                issue = f"missing {sorted(result.missing)} ≠ {sorted(case['missing'])}"
        elif not isinstance(result, NotATrade):
            issue = f"expected not_trade, got {type(result).__name__}"
        elif result.reason != case["reason"]:
            issue = f"reason {result.reason} ≠ {case['reason']}"
        by_expect[expect][1] += 1
        if issue is None:
            exact += 1
            by_expect[expect][0] += 1
        elif len(res.failures) < MAX_FAILURES:
            res.failures.append(f"“{case['text']}”: {issue}")
    res.metrics = [
        Metric("exact", ratio(exact, len(cases)), Gate("==", 1.0)),
        Metric("silent_guesses", silent, Gate("==", 0), kind="count"),
        Metric("cases", len(cases), kind="count"),
    ]
    res.info = {k: {"exact": v[0], "cases": v[1]} for k, v in by_expect.items()}
    return res
