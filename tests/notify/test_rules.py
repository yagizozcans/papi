"""Alert-rule DSL (08 §3.1): every field and operator, Turkish keyword matching, syntax errors."""

from __future__ import annotations

from typing import Any

import pytest

from papi.notify.rules import FIELDS, RuleSyntaxError, parse

CTX: dict[str, Any] = {
    "scope": ["all", "holdings", "watchlist"],
    "ticker": ["ASELS"],
    "sector": ["savunma"],
    "index": ["XU030", "XU100"],
    "watchlist": ["Savunma"],
    "code": "ownership.insider_trade",
    "family": "ownership",
    "materiality": 72,
    "band": "high",
    "priority": "P1",
    "revision": False,
    "late": True,
    "catchup": False,
    "payload": {"net_value_try": "6.250.000", "buyer": {"type": "insider"}, "ratio": 1.5},
    "text": "İŞ ORTAKLIĞI ve geri alım programı — Şirket İçi Kaynak",
}


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ('scope = "holdings"', True),
        ('scope = "ops"', False),
        ('scope != "ops"', True),
        ('scope IN ("holdings", "watchlist")', True),
        ('scope NOT IN ("ops")', True),
        ('ticker = "ASELS"', True),
        ('ticker = "asels"', True),
        ('ticker != "THYAO"', True),
        ('ticker IN ("THYAO", "ASELS")', True),
        ('ticker NOT IN ("THYAO", "ASELS")', False),
        ('ticker ~ "ASE"', True),
        ('sector = "Savunma"', True),
        ('index = "XU030"', True),
        ('index = "XU050"', False),
        ('watchlist = "savunma"', True),
        ('code = "ownership.insider_trade"', True),
        ('code = "ownership.*"', True),
        ('code = "business.*"', False),
        ('code ~ "insider"', True),
        ('family IN ("business", "ownership")', True),
        ('family = "capital"', False),
        ("materiality >= 60", True),
        ("materiality > 72", False),
        ("materiality < 73", True),
        ("materiality <= 72", True),
        ("materiality = 72", True),
        ("materiality != 72", False),
        ('band >= "medium"', True),
        ('band > "high"', False),
        ('band = "high"', True),
        ('band IN ("high", "critical")', True),
        ('band < "critical"', True),
        ('priority = "P1"', True),
        ('priority <= "P2"', True),
        ('priority > "P1"', False),
        ("revision = false", True),
        ("revision = true", False),
        ("late = true", True),
        ("late != true", False),
        ("catchup = false", True),
        ("payload.net_value_try >= 5_000_000", True),
        ("payload.net_value_try < 5_000_000", False),
        ("payload.ratio > 1", True),
        ('payload.buyer.type = "insider"', True),
        ("payload.missing >= 1", False),
        ('payload.missing != "x"', False),
        ('text ~ "geri alım"', True),
        ('text ~ "is ortakligi"', True),  # İ/ı and ş folded
        ('text ~ "şirket içi"', True),
        ('text ~ "SIRKET ICI"', True),
        ('text ~ "temettü"', False),
        (
            'scope = "holdings" AND code = "ownership.insider_trade" AND payload.net_value_try >= 5_000_000',
            True,
        ),
        ('watchlist = "Savunma" AND family IN ("business", "capital") AND materiality >= 60', False),
        ('index = "XU030" AND code = "market.measure"', False),
        ('ticker = "THYAO" OR ticker = "ASELS"', True),
        ('NOT ticker = "ASELS"', False),
        ('NOT (ticker = "THYAO" OR band = "low")', True),
        ('ticker = "THYAO" OR ticker = "X" AND materiality > 1', False),
        ('(ticker = "THYAO" OR ticker = "ASELS") AND materiality > 1', True),
        ('scope = "holdings" and band >= "medium"', True),  # keywords are case-insensitive
        ("ticker = 'ASELS'", True),
    ],
)
def test_eval(rule: str, expected: bool) -> None:
    assert parse(rule).matches(CTX) is expected


def test_missing_values_never_match() -> None:
    ctx = {"scope": ["all"], "ticker": [], "materiality": None, "band": None, "priority": None, "payload": {}}
    assert not parse('ticker != "X"').matches(ctx)
    assert not parse("materiality >= 0").matches(ctx)
    assert not parse('band >= "low"').matches(ctx)
    assert parse('NOT band >= "low"').matches(ctx)


def test_every_field_is_supported() -> None:
    samples = {
        "scope": 'scope = "all"',
        "ticker": 'ticker = "A"',
        "sector": 'sector = "x"',
        "index": 'index = "XU100"',
        "watchlist": 'watchlist = "Ana"',
        "code": 'code = "x.y"',
        "family": 'family = "x"',
        "materiality": "materiality >= 1",
        "band": 'band = "low"',
        "priority": 'priority = "P3"',
        "revision": "revision = true",
        "late": "late = false",
        "catchup": "catchup = true",
        "text": 'text ~ "x"',
    }
    assert set(samples) == FIELDS
    for text in samples.values():
        assert parse(text).fields <= FIELDS


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "foo = 1",
        'scope = "holdings" AND',
        'scope "holdings"',
        'text = "x"',
        "text ~ 5",
        'materiality >= "high"',
        'ticker > "A"',
        'band >= "huge"',
        'priority = "P9"',
        'revision = "yes"',
        "late > true",
        'payload.x > "a"',
        "payload. = 1",
        '(scope = "all"',
        'scope = "all")',
        'scope IN "all"',
        "scope = holdings",
        'scope = "all" # comment',
    ],
)
def test_syntax_errors(rule: str) -> None:
    with pytest.raises(RuleSyntaxError):
        parse(rule)


def test_error_messages_are_turkish() -> None:
    with pytest.raises(RuleSyntaxError, match="bilinmeyen alan"):
        parse("foo = 1")
