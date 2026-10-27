from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from papi.core.ids import uuid7, uuid7_time
from papi.core.text import fold_diacritics, normalize_tr, parse_tr_number, tr_lower, tr_upper
from papi.core.timeutil import IST, iso_utc, parse_kap_datetime, to_ist, to_utc


@pytest.mark.parametrize(
    ("src", "lower"),
    [
        ("I", "ı"),
        ("İ", "i"),
        ("ISPARTA", "ısparta"),
        ("İSTANBUL", "istanbul"),
        ("ŞEKER ÇAĞ ÖĞÜ", "şeker çağ öğü"),
        ("Iİıi", "ıiıi"),
    ],
)
def test_tr_lower(src, lower):
    assert tr_lower(src) == lower


def test_tr_upper():
    assert tr_upper("istanbul ılık") == "İSTANBUL ILIK"
    assert tr_upper("çiğdem") == "ÇİĞDEM"
    assert tr_upper(tr_lower("İzmir Iğdır")) == "İZMİR IĞDIR"


def test_fold_and_normalize():
    assert fold_diacritics("Çağrı Şükrü Öğüt İnal ılgaz Âşık") == "Cagri Sukru Ogut Inal ilgaz Asik"
    assert normalize_tr("İSTANBUL Şehir Hatları A.Ş.") == "istanbul sehir hatlari a s"
    assert normalize_tr("ASELSAN") == normalize_tr("aselsan") == "aselsan"
    assert normalize_tr("Iğdır") == normalize_tr("ığdır") == "igdir"
    assert normalize_tr(normalize_tr("Özel Durum Açıklaması")) == "ozel durum aciklamasi"
    # decomposed input (i + combining dot) normalises the same as precomposed
    assert normalize_tr("I\u0307stanbul") == "istanbul"


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("1.234.567,89", "1234567.89"),
        ("285,40", "285.40"),
        ("1,234.56", "1234.56"),
        ("1,234,567", "1234567"),
        ("1.234.567", "1234567"),
        ("62.500", "62500"),
        ("285.40", "285.40"),
        ("1.5", "1.5"),
        ("1,234", "1.234"),
        ("100", "100"),
        ("-12,5", "-12.5"),
        ("(1.000,00)", "-1000.00"),
        ("−3,2", "-3.2"),
        ("+7", "7"),
        ("₺1.250,75", "1250.75"),
        ("1.250,75 TL", "1250.75"),
        ("%12,5", "12.5"),
        ("1 234 567,8", "1234567.8"),
        ("1\u00a0234,5", "1234.5"),
        (",5", "0.5"),
    ],
)
def test_parse_tr_number(src, expected):
    assert parse_tr_number(src) == Decimal(expected)


def test_parse_tr_number_forced_modes():
    assert parse_tr_number("1.234", decimal="dot") == Decimal("1.234")
    assert parse_tr_number("1.234", decimal="comma") == Decimal("1234")
    assert parse_tr_number("1,234", decimal="dot") == Decimal("1234")


@pytest.mark.parametrize("bad", ["", "abc", "1,2,3", "12.34.5", "1.234,5,6", "1,23.4", "--1", "1.2.3,4.5"])
def test_parse_tr_number_rejects(bad):
    with pytest.raises(ValueError):
        parse_tr_number(bad)


def test_parse_kap_datetime_formats():
    expected = datetime(2026, 10, 28, 7, 10, 35, tzinfo=UTC)  # Istanbul is UTC+3
    assert parse_kap_datetime("28.10.2026 10:10:35") == expected
    assert parse_kap_datetime("2026.10.28 10:10:35") == expected
    assert parse_kap_datetime("  28.10.2026   10:10:35 ") == expected
    assert parse_kap_datetime("28.10.2026 10:10") == expected.replace(second=0)
    assert parse_kap_datetime("1.2.2016") == datetime(2016, 1, 31, 22, 0, tzinfo=UTC)
    # Before 2016-09 Türkiye observed DST: summer 2015 was UTC+3, winter UTC+2
    assert parse_kap_datetime("15.01.2015 10:00:00") == datetime(2015, 1, 15, 8, 0, tzinfo=UTC)
    for bad in ("2026-10-28 10:10:35", "28/10/2026", "32.10.2026 10:00:00", "garbage"):
        with pytest.raises(ValueError):
            parse_kap_datetime(bad)


def test_time_conversions():
    naive = datetime(2026, 10, 28, 10, 0)
    assert to_utc(naive) == datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    assert to_utc("2026-10-28T07:00:00Z") == datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    assert to_ist(datetime(2026, 10, 28, 7, 0, tzinfo=UTC)).hour == 10
    assert to_ist(datetime(2026, 10, 28, 7, 0, tzinfo=UTC)).tzinfo == IST
    assert iso_utc(datetime(2026, 10, 28, 10, 0, tzinfo=IST)) == "2026-10-28T07:00:00.000000Z"
    with pytest.raises(ValueError):
        iso_utc(naive)


def test_uuid7_monotonic_and_valid():
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    u = uuid.UUID(ids[0])
    assert u.version == 7 and u.variant == uuid.RFC_4122
    assert abs((uuid7_time(ids[-1]) - datetime.now(UTC)).total_seconds()) < 5
