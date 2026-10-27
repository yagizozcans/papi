"""Shared helpers for legal/KB tests: real-text fixtures and a fetch+parse pipeline on respx mocks.

Fixtures in ``tests/kb/fixtures`` are verbatim excerpts of the official texts fetched on 2026-10-28
(SPK consolidated PDFs via mevzuat.spk.gov.tr, KAP, Borsa İstanbul, the 23/01/2014 Resmî Gazete
HTML of II-15.1). Text fixtures keep the PDF page breaks as form feeds (``\\f``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import respx

from papi.sources.legal.extract import Line, extract
from papi.sources.legal.fetch import PoliteClient, fetch_entry
from papi.sources.legal.manifest import HistoricalSource, ManifestEntry

FIXTURES = Path(__file__).parent / "fixtures"

II151_URL = "https://mevzuat.example.test/api/Mevzuat/File/81"
II151_RG_URL = "https://rg.example.test/eskiler/2014/01/20140123-5.htm"
LAW6362_URL = "https://mevzuat.example.test/api/Mevzuat/File/112"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_lines(name: str) -> list[Line]:
    ctype = "text/html" if name.endswith(".html") else "text/plain"
    return extract(fixture_bytes(name), ctype).lines


def ii151_entry(*, with_history: bool = True, url: str = II151_URL) -> ManifestEntry:
    return ManifestEntry(
        id="spk-ii-15-1",
        official_code="II-15.1",
        title="Özel Durumlar Tebliği",
        authority="SPK",
        kind="communique",
        source_urls=[url],
        priority="P0",
        refresh="weekly",
        citation_prefix="tr:spk:II-15.1",
        shape="articles",
        history=[
            HistoricalSource(
                url=II151_RG_URL, effective_from=date(2014, 2, 23), label="Asıl metin – RG 23/01/2014-28891"
            )
        ]
        if with_history
        else [],
    )


def law6362_entry() -> ManifestEntry:
    return ManifestEntry(
        id="law-6362",
        official_code="6362",
        title="Sermaye Piyasası Kanunu",
        authority="TBMM",
        kind="law",
        source_urls=[LAW6362_URL],
        priority="P0",
        refresh="weekly",
        citation_prefix="tr:law:6362",
        shape="articles",
    )


def text_response(name: str) -> httpx.Response:
    ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8"
    return httpx.Response(200, content=fixture_bytes(name), headers={"content-type": ctype})


def no_sleep_client(**kw) -> PoliteClient:
    return PoliteClient(min_interval=0.0, sleep=lambda s: None, **kw)


def fetch_fixture(entry: ManifestEntry, routes: dict[str, str | bytes]) -> list:
    """Fetch ``entry`` with its URLs answered from fixtures (name) or raw bytes."""
    with respx.mock(assert_all_called=False) as mock:
        for url, body in routes.items():
            if isinstance(body, bytes):
                mock.get(url).mock(
                    return_value=httpx.Response(200, content=body, headers={"content-type": "text/plain"})
                )
            else:
                mock.get(url).mock(return_value=text_response(body))
        with no_sleep_client() as client:
            return fetch_entry(entry, client)
