from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from papi.core import rawstore
from papi.core.schema import fetch_log, raw_object


def test_put_and_dedupe(db, settings):
    data = b"<html>KAP bildirim</html>"
    sha = rawstore.put_bytes(
        data,
        source_code="kap",
        url="https://www.kap.org.tr/tr/Bildirim/1",
        http_status=200,
        content_type="text/html",
        request_meta={"method": "GET"},
        response_headers={"etag": "x"},
    )
    assert sha == hashlib.sha256(data).hexdigest()
    path = settings.paths.raw / sha[:2] / sha
    assert path.read_bytes() == data
    sha2 = rawstore.put_bytes(
        data, source_code="kap", url="https://www.kap.org.tr/tr/Bildirim/1", http_status=200
    )
    assert sha2 == sha

    assert db.execute(select(func.count()).select_from(raw_object)).scalar() == 1
    assert db.execute(select(func.count()).select_from(fetch_log)).scalar() == 2
    obj = db.execute(select(raw_object)).one()
    assert obj.byte_size == len(data)
    assert obj.storage_uri == f"raw/{sha[:2]}/{sha}"
    assert obj.request_meta == {"method": "GET"}
    assert rawstore.get_bytes(sha, verify=True) == data
    assert rawstore.exists(sha)


def test_put_in_caller_transaction_rolls_back_rows(db):
    sha = rawstore.put_bytes(b"abc", source_code="kap", url="u", conn=db)
    db.rollback()
    assert db.execute(select(func.count()).select_from(raw_object)).scalar() == 0
    # bytes are content-addressed and harmless to keep; a later put records the row
    assert rawstore.exists(sha)


def test_unknown_source(db):
    with pytest.raises(rawstore.UnknownSourceError):
        rawstore.put_bytes(b"x", source_code="nope")


def test_failure_log(db):
    fid = rawstore.log_fetch_failure(
        source_code="kap", url="u", outcome="http_error", http_status=503, error="WAF"
    )
    row = db.execute(select(fetch_log).where(fetch_log.c.id == fid)).one()
    assert row.raw_sha256 is None and row.outcome == "http_error" and row.http_status == 503


def test_bad_sha(db):
    with pytest.raises(ValueError):
        rawstore.get_bytes("../etc/passwd")
