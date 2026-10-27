"""Table-driven trade-parser evaluation (``trade_phrases.yaml``, PRD 11): 100 % on supported patterns."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from papi.core.timeutil import IST
from papi.telegram.trade_parser import (
    NeedsInfo,
    NotATrade,
    ParsedTrade,
    TradeDraft,
    parse,
    parse_date_phrase,
    parse_side_word,
)

DATA = yaml.safe_load((Path(__file__).parent / "trade_phrases.yaml").read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(DATA["now"])
CASES = DATA["cases"]


def _fraction(raw: Any) -> Decimal:
    text = str(raw)
    if "/" in text:
        a, b = text.split("/")
        return Decimal(a) / Decimal(b)
    return Decimal(text)


def test_eval_set_size() -> None:
    assert len(CASES) >= 120
    assert len({c["text"] for c in CASES}) == len(CASES), "duplicate phrasings"


@pytest.mark.parametrize("case", CASES, ids=[c["text"] or "<empty>" for c in CASES])
def test_phrase(case: dict[str, Any]) -> None:
    result = parse(case["text"], now=NOW)
    expect = case["expect"]
    if expect == "trade":
        assert isinstance(result, ParsedTrade), result
        assert result.side == case["side"]
        assert result.instrument_ref == case["ref"]
        assert result.price == Decimal(case["price"])
        if "fraction" in case:
            assert result.quantity is None
            assert result.fraction == _fraction(case["fraction"])
        else:
            assert result.fraction is None
            assert result.quantity == Decimal(case["qty"])
        expected_date = date.fromisoformat(case.get("date", NOW.date().isoformat()))
        assert result.trade_date == expected_date
        if "time" in case:
            assert result.traded_at is not None
            assert result.traded_at.astimezone(IST).strftime("%H:%M") == case["time"]
            assert result.traded_at.astimezone(IST).date() == expected_date
        elif "date" in case:
            assert result.traded_at is None
        else:
            assert result.traded_at == NOW  # trades use the message time
        assert result.fees == (Decimal(case["fees"]) if "fees" in case else None)
    elif expect == "needs_info":
        assert isinstance(result, NeedsInfo), result
        assert set(result.missing) == set(case["missing"]), result
    else:
        assert isinstance(result, NotATrade), result
        assert result.reason == case["reason"]


def test_now_must_be_aware() -> None:
    with pytest.raises(ValueError):
        parse("THYAO 100 lot 285,40'tan aldım", now=datetime(2026, 10, 28, 14, 0))


def test_message_time_in_other_timezone_resolves_istanbul_date() -> None:
    # 22:30 UTC on the 27th is 01:30 on the 28th in Istanbul
    now = datetime.fromisoformat("2026-10-27T22:30:00+00:00")
    result = parse("THYAO 100 lot 285,40'tan aldım", now=now)
    assert isinstance(result, ParsedTrade)
    assert result.trade_date == date(2026, 10, 28)
    result = parse("dün THYAO 100 lot 285,40'tan aldım", now=now)
    assert isinstance(result, ParsedTrade) and result.trade_date == date(2026, 10, 27)


def test_draft_json_roundtrip() -> None:
    result = parse("ASELS'in yarısını sattım", now=NOW)
    assert isinstance(result, NeedsInfo)
    assert TradeDraft.from_json(result.draft.as_json()) == result.draft


@pytest.mark.parametrize(
    ("text", "side"),
    [
        ("al", "buy"),
        ("AL", "buy"),
        ("alış", "buy"),
        ("sat", "sell"),
        ("satış", "sell"),
        ("sold", "sell"),
        ("x", None),
    ],
)
def test_parse_side_word(text: str, side: str | None) -> None:
    assert parse_side_word(text) == side


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("dün", date(2026, 10, 27)),
        ("bugün", date(2026, 10, 28)),
        ("27.10.2026", date(2026, 10, 27)),
        ("27 Ekim", date(2026, 10, 27)),
        ("önceki gün", date(2026, 10, 26)),
        ("cuma", None),
        ("01.11.2026", None),
        ("dün 27.10.2025", None),
    ],
)
def test_parse_date_phrase(text: str, expected: date | None) -> None:
    assert parse_date_phrase(text, now=NOW.astimezone(IST)) == expected
