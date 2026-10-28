"""KAP test helpers: recorded fixtures served through respx (no network), hermetic deps."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from papi.sources.kap import cadence, deps
from papi.sources.kap.client import KapPublicWebClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kap"
BASE = "https://www.kap.org.tr"

# Detail fixtures that are also present in the recorded 2-day list.
LISTED = [
    1687750,
    1688011,
    1688254,
    1688272,
    1688363,
    1688368,
    1688379,
    1688504,
    1688533,
    1688545,
    1688549,
    1688576,
    1688616,
]


def load_json(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".gz":
        return json.loads(gzip.decompress(path.read_bytes()))
    return json.loads(path.read_text(encoding="utf-8"))


def detail_json(index: int) -> list[dict[str, Any]] | None:
    for name in (f"detail_{index}.json", f"detail_{index}.json.gz", f"detail_{index}_fund.json"):
        if (FIXTURES / name).exists():
            return load_json(name)
    return None


def list_items() -> list[dict[str, Any]]:
    return load_json("list_2026-10-27_2026-10-28.json.gz")


def fund_items() -> list[dict[str, Any]]:
    return load_json("funds_2026-10-27_2026-10-28.json.gz")


def fixture_list(indices: list[int] | None = None) -> list[dict[str, Any]]:
    wanted = set(indices or LISTED)
    return [it for it in list_items() if it["disclosureIndex"] in wanted]


def _item_day(item: dict[str, Any]) -> date:
    d, m, y = item["publishDate"][:10].split(".")
    return date(int(y), int(m), int(d))


@dataclass
class FakeKap:
    """Mutable fake KAP backed by recorded fixtures."""

    items: list[dict[str, Any]] = field(default_factory=fixture_list)
    details: dict[int, Any] = field(default_factory=dict)
    fund_items: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    default_file: bytes = b""
    companies: list[dict[str, Any]] = field(default_factory=list)
    list_status: int = 200
    calls: list[str] = field(default_factory=list)

    def detail(self, index: int) -> Any:
        if index in self.details:
            return self.details[index]
        return detail_json(index)


@pytest.fixture
def fake_kap() -> FakeKap:
    default_file = (FIXTURES / "file_4028328ca16c7de401a17de5e8772c7f.bin").read_bytes()
    return FakeKap(
        files={
            "4028328ca16c7de401a17de5e8772c7f": default_file,
            "4028328da16c7fe501a17db833540bc8": (
                FIXTURES / "file_4028328da16c7fe501a17db833540bc8.bin"
            ).read_bytes(),
        },
        default_file=default_file,
        companies=load_json("companies_IGS.json.gz"),
    )


@pytest.fixture
def kap_http(fake_kap: FakeKap) -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:

        def by_criteria(request: httpx.Request) -> httpx.Response:
            fake_kap.calls.append("list")
            return _listing(request, fake_kap.items)

        def funds_by_criteria(request: httpx.Request) -> httpx.Response:
            fake_kap.calls.append("funds")
            return _listing(request, fake_kap.fund_items)

        def _listing(request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
            if fake_kap.list_status != 200:
                return httpx.Response(fake_kap.list_status, text="blocked")
            body = json.loads(request.content)
            lo, hi = date.fromisoformat(body["fromDate"]), date.fromisoformat(body["toDate"])
            out = [it for it in items if lo <= _item_day(it) <= hi]
            out.sort(key=lambda x: -x["disclosureIndex"])
            return httpx.Response(200, json=out[:2000])

        def detail(request: httpx.Request, index: str) -> httpx.Response:
            fake_kap.calls.append(f"detail:{index}")
            data = fake_kap.detail(int(index))
            if data is None:
                recorded = (FIXTURES / "detail_1689500_nonexistent_http500.json").read_bytes()
                return httpx.Response(500, content=recorded, headers={"content-type": "application/json"})
            return httpx.Response(200, json=data)

        def download(request: httpx.Request, obj_id: str) -> httpx.Response:
            fake_kap.calls.append(f"file:{obj_id}")
            return httpx.Response(
                200,
                content=fake_kap.files.get(obj_id, fake_kap.default_file),
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": f"inline; filename*=UTF-8''{obj_id}.pdf",
                },
            )

        def kap_pdf(request: httpx.Request, index: str) -> httpx.Response:
            fake_kap.calls.append(f"pdf:{index}")
            return httpx.Response(
                200,
                content=(FIXTURES / "bildirimpdf_1688616.pdf").read_bytes(),
                headers={"content-type": "application/pdf"},
            )

        def companies(request: httpx.Request) -> httpx.Response:
            fake_kap.calls.append("companies")
            return httpx.Response(200, json=fake_kap.companies)

        def member_filter(request: httpx.Request, code: str) -> httpx.Response:
            fake_kap.calls.append(f"filter:{code}")
            path = FIXTURES / f"member_filter_{code}.json"
            return httpx.Response(200, content=path.read_bytes() if path.exists() else b"[]")

        def genel(request: httpx.Request, link: str) -> httpx.Response:
            fake_kap.calls.append(f"genel:{link}")
            ticker = {
                "956-ford-otomotiv-sanayi-a-s": "FROTO",
                "2425-turkiye-is-bankasi-a-s": "ISCTR",
                "5826-pasifik-eurasia-lojistik-dis-ticaret-a-s": "PASEU",
            }.get(link, "bad_956")  # KAP answers 200 with a generic page for unknown links
            html = gzip.decompress((FIXTURES / f"company_genel_{ticker}.html.gz").read_bytes())
            return httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"})

        router.post("/tr/api/disclosure/members/byCriteria").mock(side_effect=by_criteria)
        router.post("/tr/api/disclosure/funds/byCriteria").mock(side_effect=funds_by_criteria)
        router.get(path__regex=r"/tr/api/notification/attachment-detail/(?P<index>\d+)").mock(
            side_effect=detail
        )
        router.get(path__regex=r"/tr/api/file/download/(?P<obj_id>\w+)").mock(side_effect=download)
        router.get(path__regex=r"/tr/api/BildirimPdf/(?P<index>\d+)").mock(side_effect=kap_pdf)
        router.get("/tr/api/company/items/IGS/A").mock(side_effect=companies)
        router.get(path__regex=r"/tr/api/member/filter/(?P<code>\w+)").mock(side_effect=member_filter)
        router.get(path__regex=r"/tr/sirket-bilgileri/genel/(?P<link>[\w-]+)").mock(side_effect=genel)
        yield router


@pytest.fixture
def client(kap_http: respx.MockRouter) -> Iterator[KapPublicWebClient]:
    c = KapPublicWebClient(min_interval=0, sleep=lambda s: None)
    yield c
    c.close()


@dataclass
class Deps:
    alerts: list[dict[str, Any]] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)  # instrument_id → "P1"/"P2"


@pytest.fixture
def kap_deps(monkeypatch: pytest.MonkeyPatch, clean_registry: Any) -> Deps:
    """Hermetic cross-slice calls: ops alerts recorded, holdings priority from ``held``."""
    d = Deps()

    def ops_alert(conn: Any, text: str, *, level: str = "warning", dedupe_key: str | None = None) -> None:
        d.alerts.append({"text": text, "level": level, "dedupe_key": dedupe_key})

    def holding_priority(conn: Any, instrument_ids: list[str], at: datetime | None = None) -> str | None:
        labels = [d.held[i] for i in instrument_ids if i in d.held]
        return min(labels) if labels else None

    monkeypatch.setattr(deps, "ops_alert", ops_alert)
    monkeypatch.setattr(deps, "holding_priority", holding_priority)
    cadence.clear_cache()
    return d


@pytest.fixture
def fixed_now() -> Callable[[str], datetime]:
    from papi.core.timeutil import parse_iso

    return lambda s: parse_iso(s)
