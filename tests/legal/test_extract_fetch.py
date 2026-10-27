"""Extraction (HTML/DOCX/text, page furniture), manifest, and the polite fetcher (respx, no network)."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx

from papi.core import db as core_db
from papi.core import rawstore
from papi.core.state import get_state
from papi.sources.legal.extract import DOCX, ExtractionError, extract, sniff_mime, strip_page_furniture
from papi.sources.legal.fetch import PoliteClient, due_entries, fetch_entries
from papi.sources.legal.manifest import load_manifest, refresh_days
from tests.kb.helpers import (
    II151_RG_URL,
    II151_URL,
    fetch_fixture,
    fixture_bytes,
    ii151_entry,
    no_sleep_client,
    text_response,
)


def test_strip_running_headers_and_page_numbers():
    pages = [
        ["ÖZEL DURUMLAR TEBLİĞİ", "(II-15.1)", "MADDE 1 – (1) Bu Tebliğin amacı,", "II. KISIM 1"],
        ["(4) Yurt dışında sermaye piyasası aracı", "Dayanak", "II. KISIM 2"],
        ["Tanımlar ve kısaltmalar", "MADDE 4 – (1) Bu Tebliğde geçen;", "II. KISIM 3"],
        ["e) İçsel bilgi:", "II. KISIM 4"],
    ]
    kept, removed = strip_page_furniture(pages)
    assert removed == ["II. KISIM #"]
    assert kept[0][-1] == "MADDE 1 – (1) Bu Tebliğin amacı," and kept[3] == ["e) İçsel bilgi:"]


def test_word_html_per_character_spans_are_joined():
    lines = extract(fixture_bytes("ii15_1_original_rg_excerpt.html"), "text/html").lines
    texts = [ln.text for ln in lines]
    assert "ÖZEL DURUMLAR TEBLİĞİ" in texts
    assert "İdari sorumluluğu bulunan kişilerin işlemlerinin açıklanması" in texts
    assert any(t.startswith("MADDE 2 – (1) İşlem sırası geçici olarak kapatılmış olanlar") for t in texts)


def test_docx_extraction():
    body = "".join(
        f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"
        for t in ("Kayıtlı sermaye sistemi", "MADDE 18 – (1) Halka açık ortaklıklar ile paylarını")
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    data = buf.getvalue()
    assert sniff_mime(data) == DOCX
    assert [ln.text for ln in extract(data).lines] == [
        "Kayıtlı sermaye sistemi",
        "MADDE 18 – (1) Halka açık ortaklıklar ile paylarını",
    ]


def test_text_form_feeds_are_pages_and_unknown_formats_rejected():
    ex = extract(b"A\nB\fC", "text/plain")
    assert [(ln.text, ln.page) for ln in ex.lines] == [("A", 1), ("B", 1), ("C", 2)]
    with pytest.raises(ExtractionError):
        extract(b"\x00\x01binary", "application/octet-stream")


def test_repo_manifest_lists_p0_documents():
    m = load_manifest()
    p0 = {d.id for d in m.select(priority="P0")}
    assert {
        "spk-ii-15-1",
        "spk-rehber-ozel-durumlar",
        "law-6362",
        "spk-vii-128-1",
        "spk-ii-18-1",
        "spk-ii-19-1",
        "spk-ii-22-1",
        "spk-vii-128-6",
        "kap-bildirim-yonergesi",
    } <= p0
    for d in m.documents:
        assert all(u.startswith("https://") for u in d.source_urls)
    assert m.get("spk-ii-15-1").citation_prefix == "tr:spk:II-15.1"
    assert m.get("law-6362").citation_prefix == "tr:law:6362"
    assert m.get("spk-rehber-ozel-durumlar").citation_prefix == "tr:spk-guide:ozel-durumlar"
    assert refresh_days("weekly") == 7 and refresh_days("10d") == 10
    with pytest.raises(ValueError):
        refresh_days("sometimes")


def test_fetch_falls_back_archives_and_skips_unchanged(db):
    entry = ii151_entry(with_history=False)
    entry = entry.model_copy(update={"source_urls": ["https://down.example.test/x.pdf", II151_URL]})
    with respx.mock() as mock:
        mock.get("https://down.example.test/x.pdf").mock(return_value=httpx.Response(503))
        mock.get(II151_URL).mock(return_value=text_response("ii15_1_consolidated.txt"))
        out = fetch_entries([entry], client=no_sleep_client())
        again = fetch_entries([entry], client=no_sleep_client())
    assert out[0].status == "changed" and out[0].url == II151_URL
    assert [a.get("http_status") for a in out[0].attempts] == [503, 200]
    assert again[0].status == "unchanged" and again[0].sha == out[0].sha
    assert rawstore.get_bytes(out[0].sha) == fixture_bytes("ii15_1_consolidated.txt")
    db.rollback()
    outcomes = [r[0] for r in db.exec_driver_sql("SELECT outcome FROM fetch_log ORDER BY started_at").all()]
    assert outcomes.count("http_error") == 2 and outcomes.count("ok") == 2
    st = get_state(db, "legal:doc:spk-ii-15-1")
    assert st["sha"] == out[0].sha and st["url"] == II151_URL


def test_fetch_rejects_spa_shell_and_fetches_history_once(db):
    shell = (
        b'<!doctype html><html lang="tr"><head><title>SPK</title></head>'
        b'<body><div id="root"></div></body></html>'
    )
    entry = ii151_entry()
    with respx.mock() as mock:
        mock.get(II151_URL).mock(
            return_value=httpx.Response(200, content=shell, headers={"content-type": "text/html"})
        )
        mock.get(II151_RG_URL).mock(return_value=text_response("ii15_1_original_rg_excerpt.html"))
        with no_sleep_client() as client:
            out = fetch_entries([entry], client=client)
    assert out[0].status == "failed" and out[0].attempts[0]["http_status"] == 200 and out[0].sha is None
    assert out[1].role == "history:0" and out[1].status == "changed"
    hist = fetch_fixture(entry, {II151_URL: "ii15_1_consolidated.txt"})
    assert hist[0].status == "changed"
    assert hist[1].status == "unchanged" and hist[1].attempts == []  # not re-downloaded


def test_rate_limit_and_request_budget():
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    with respx.mock() as mock:
        mock.get("https://a.example.test/1").mock(return_value=httpx.Response(200, content=b"x"))
        client = PoliteClient(min_interval=1.5, max_requests=2, sleep=sleep, clock=lambda: clock[0])
        client.get("https://a.example.test/1")
        clock[0] += 0.4
        client.get("https://a.example.test/1")
        with pytest.raises(RuntimeError, match="budget"):
            client.get("https://a.example.test/1")
        client.close()
    assert sleeps == [pytest.approx(1.1)]
    assert client.requests_made == 2


def test_due_entries_respects_refresh(db):
    entry = ii151_entry(with_history=False)
    assert due_entries([entry]) == [entry]
    fetch_fixture(entry, {II151_URL: "ii15_1_consolidated.txt"})
    assert due_entries([entry]) == []
    with core_db.transaction() as conn:
        conn.exec_driver_sql(
            "UPDATE kv_state SET value = json_set(value, '$.checked_at', '2000-01-01T00:00:00.000000Z') "
            "WHERE key = 'legal:doc:spk-ii-15-1'"
        )
    assert due_entries([entry]) == [entry]
