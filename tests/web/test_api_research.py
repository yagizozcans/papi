"""Research API (agents + theses): endpoints with the CLI spawn mocked (no Claude call, no subprocess)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from papi.agents import runs
from papi.thesis import document as doc
from papi.thesis import importer
from papi.web import agents as agents_web
from papi.web import theses as theses_web
from papi.web.app import create_app

RID = "01a1a30a-7438-71aa-a106-ecc3318fd0de"
RID2 = "01a1a30a-7438-71aa-a106-ecc3318fd0df"
PDF = Path(__file__).parent.parent / "fixtures" / "kap" / "bildirimpdf_1688616.pdf"

GOOD = """---
ticker: THYAO
date: 2026-11-06
direction: long
horizon: 6-12 ay
---

## Fikir
Yolcu geliri artıyor.

## Konsensüs
Piyasa büyümenin biteceğini varsayıyor.

## Farklı Görüş
Kapasite artışı fiyatlanmamış.

## Edge
Analitik.

## Karşı Taraf
Kurumsal satıcı.

## Tetikleyiciler
- Rekor yaz sezonu | 2026 Q3 | %60

## Aşağı Yönlü Senaryo
Petrol fiyatı sıçrar.

## Tezi Çürütecek Sinyaller
- Doluluk düşer | %80 altı

## Pre-mortem
Savaş çıkar.
"""


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(agents_web.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(theses_web.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    return calls


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


def _transcript(agent: str, run_id: str) -> None:
    rows = [
        {
            "at": runs.now_iso(),
            "tool": "use_skill",
            "args": {"name": "x"},
            "ms": 3,
            "result": {"success": True},
        },
        {
            "at": runs.now_iso(),
            "tool": "read_kap",
            "args": {"no": "1"},
            "ms": 40,
            "result": {"success": False, "text": "x" * 500, "blocking": "oku"},
        },
    ]
    runs.transcript_path(agent, run_id).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


# ------------------------------------------------------------------------------------------ agents


def test_agent_catalogue(client: TestClient) -> None:
    body = client.get("/v1/agents").json()
    by_name = {a["name"]: a for a in body["agents"]}
    assert {"kap-analyst", "strategy-analyst"} <= set(by_name)
    kap = by_name["kap-analyst"]
    assert kap["max_budget_usd"] > 0 and kap["max_turns"] > 0 and kap["model"]
    assert kap["ref_kind"] == "disclosure" and by_name["strategy-analyst"]["ref_kind"] == "thesis"
    assert kap["skills"] and kap["tools"] and "version" in kap


def test_run_list_filters_and_paginates(client: TestClient) -> None:
    runs.write_meta(
        "kap-analyst",
        RID,
        subject="1691891",
        started_at="2026-11-20T10:00:00+00:00",
        status="ok",
        cost_usd=0.12,
    )
    runs.write_meta(
        "strategy-analyst",
        RID2,
        subject="THYAO-2026-11-06",
        started_at="2026-11-21T10:00:00+00:00",
        status="failed",
        error="boom",
    )
    body = client.get("/v1/agent-runs").json()
    assert body["total"] == 2 and [r["run_id"] for r in body["items"]] == [RID2, RID]  # newest first
    assert client.get("/v1/agent-runs", params={"agent": "kap-analyst"}).json()["total"] == 1
    assert client.get("/v1/agent-runs", params={"ref": "1691891"}).json()["items"][0]["cost_usd"] == 0.12
    assert client.get("/v1/agent-runs", params={"status": "failed"}).json()["items"][0]["error"] == "boom"
    assert client.get("/v1/agent-runs", params={"status": "active"}).json()["total"] == 0
    page = client.get("/v1/agent-runs", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and [r["run_id"] for r in page["items"]] == [RID]


def test_run_detail_shape(client: TestClient) -> None:
    out = {
        "headline_tr": "Başlık",
        "positives": [{"text_tr": "iyi", "span_ordinals": [1]}],
        "negatives": [],
    }
    runs.write_meta(
        "kap-analyst",
        RID,
        subject="1691891",
        started_at=runs.now_iso(),
        status="ok",
        finished_at=runs.now_iso(),
        cost_usd=0.2,
        max_budget_usd=0.6,
        turns=5,
        max_turns=14,
        usage={"input_tokens": 10, "cache_read_input_tokens": 99},
        output=out,
        agent_version="abc",
    )
    _transcript("kap-analyst", RID)
    d = client.get(f"/v1/agent-runs/{RID}").json()
    assert d["status"] == "ok" and not d["active"] and d["turns"] == 5 and d["max_turns"] == 14
    assert d["usage"] == {
        "input_tokens": 10,
        "output_tokens": 0,
        "cache_read_input_tokens": 99,
        "cache_creation_input_tokens": 0,
    }
    assert d["output"]["positives"][0]["text_tr"] == "iyi" and d["text"] is None
    first, second = d["calls"]
    assert first["tool"] == "use_skill" and first["ok"] and first["args"] == {"name": "x"}
    assert not second["ok"] and second["blocking"] == "oku" and "<500 karakter>" in second["result_excerpt"]


def test_run_detail_active_and_unknown(client: TestClient) -> None:
    runs.write_meta("kap-analyst", RID, subject="1", started_at=runs.now_iso(), status="running")
    d = client.get(f"/v1/agent-runs/{RID}").json()
    assert d["active"] and d["calls"] == [] and d["usage"]["input_tokens"] == 0
    assert client.get("/v1/agent-runs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/v1/agent-runs/not-a-run").status_code == 404


def test_start_run_enqueues_and_returns_id(client: TestClient, spawned: list[list[str]]) -> None:
    r = client.post("/v1/agent-runs", json={"agent": "kap-analyst", "ref": " 1691891 "})
    assert r.status_code == 202
    body = r.json()
    assert body["ref"] == "1691891" and body["max_budget_usd"] > 0
    assert spawned[0][spawned[0].index("--run-id") + 1] == body["run_id"]
    assert runs.load_run(body["run_id"])["status"] == "starting"
    assert client.get(f"/v1/agent-runs/{body['run_id']}").json()["status"] == "starting"


def test_start_run_validates(client: TestClient, spawned: list[list[str]]) -> None:
    assert client.post("/v1/agent-runs", json={"agent": "nope", "ref": "1"}).status_code == 400
    assert (
        client.post("/v1/agent-runs", json={"agent": "kap-analyst", "ref": "1; rm -rf /"}).status_code == 400
    )
    assert client.post("/v1/agent-runs", json={"agent": "kap-analyst"}).status_code == 422
    assert not spawned


# ------------------------------------------------------------------------------------------ theses


def _write(name: str, text: str) -> None:
    (doc.theses_dir() / f"{name}.md").write_text(text, encoding="utf-8")


def test_new_thesis_from_template_and_list(client: TestClient) -> None:
    assert client.post("/v1/theses", json={"ticker": "kötü!"}).status_code == 400
    r = client.post("/v1/theses", json={"ticker": "thyao", "date": "2026-11-06"})
    assert r.status_code == 201
    d = r.json()
    assert d["name"] == "THYAO-2026-11-06" and d["ticker"] == "THYAO" and d["status"] == "manual"
    assert d["check"]["ok"] and d["check"]["gaps"]  # an empty template parses but has gaps
    assert [o["title"] for o in d["outline"]][:2] == ["Fikir", "Konsensüs"] and d["outline"][0]["empty"]
    assert (
        client.post("/v1/theses", json={"ticker": "THYAO", "date": "2026-11-06"}).json()["name"]
        == "THYAO-2026-11-06-2"
    )
    lst = client.get("/v1/theses").json()
    assert [t["name"] for t in lst["items"]] == ["THYAO-2026-11-06-2", "THYAO-2026-11-06"]
    assert ".pdf" in lst["accept"] and ".txt" in lst["accept"]


def test_save_reports_parse_errors_with_lines(client: TestClient) -> None:
    _write("THYAO-2026-11-06", GOOD)
    bad = GOOD.replace("direction: long", "direction: sideways").replace("## Edge", "## Bilinmeyen")
    r = client.put("/v1/theses/THYAO-2026-11-06/text", json={"content": bad})
    assert r.status_code == 200
    d = r.json()
    assert d["text"] == bad and d["check"]["ok"] is False and d["error_count"] == 2
    lines = {e["message"].split(":")[0]: e["line"] for e in d["check"]["errors"]}
    assert lines["direction"] == 4
    unknown = next(e for e in d["check"]["errors"] if "'## Bilinmeyen'" in e["message"])
    assert unknown["line"] == bad.split("\n").index("## Bilinmeyen") + 1
    assert (doc.theses_dir() / "THYAO-2026-11-06.md").read_text() == bad  # saved even when invalid
    ok = client.put("/v1/theses/THYAO-2026-11-06/text", json={"content": GOOD}).json()
    assert ok["check"]["ok"] and ok["check"]["triggers"] == 1 and ok["check"]["kill_signals"] == 1
    assert ok["check"]["sections_filled"] == 9 and ok["gap_count"] == len(ok["check"]["gaps"])
    assert ok["outline"][0] == {
        "line": 8,
        "title": "Fikir",
        "key": "fikir",
        "required": True,
        "items": 1,
        "empty": False,
    }


def test_check_saved_and_draft(client: TestClient) -> None:
    _write("THYAO-2026-11-06", GOOD)
    assert client.post("/v1/theses/THYAO-2026-11-06/check").json()["ok"]
    draft = client.post("/v1/theses/THYAO-2026-11-06/check", json={"content": "merhaba"}).json()
    assert not draft["ok"] and draft["errors"][0]["line"] == 1
    assert (doc.theses_dir() / "THYAO-2026-11-06.md").read_text() == GOOD  # draft check does not save
    assert client.post("/v1/theses/NOPE-2026-01-01/check").status_code == 404


def test_thesis_404_and_bad_names(client: TestClient) -> None:
    assert client.get("/v1/theses/NOPE-2026-01-01").status_code == 404
    assert client.get("/v1/theses/..%2fetc").status_code == 404
    assert client.put("/v1/theses/NOPE/text", json={"content": "x"}).status_code == 404


def test_save_refused_while_the_import_runs(client: TestClient) -> None:
    importer.write_status("THYAO-2026-11-06", status="extracting", ticker="THYAO", files=["01-a.txt"])
    d = client.get("/v1/theses/THYAO-2026-11-06").json()
    assert d["active"] and d["text"] is None and d["check"] is None and d["error_count"] is None
    assert client.put("/v1/theses/THYAO-2026-11-06/text", json={"content": GOOD}).status_code == 409
    assert client.post("/v1/theses/THYAO-2026-11-06/review").status_code == 409


def test_upload_txt_and_pdf_in_order(client: TestClient, spawned: list[list[str]]) -> None:
    pdf = PDF.read_bytes()
    r = client.post(
        "/v1/theses/upload",
        data={"ticker": "thyao", "direction": "izle", "horizon": "3 ay"},
        files=[
            ("files", ("not a.txt", b"## Fikir\nmetin", "text/plain")),
            ("files", ("paper.pdf", pdf, "application/pdf")),
        ],
    )
    assert r.status_code == 202
    body = r.json()
    assert body["files"] == ["01-not a.txt", "02-paper.pdf"]
    folder = doc.sources_dir(body["name"]) / "files"
    assert (folder / "01-not a.txt").read_bytes() == b"## Fikir\nmetin" and (
        folder / "02-paper.pdf"
    ).read_bytes() == pdf
    argv = spawned[0]
    assert argv[argv.index("--ticker") + 1] == "THYAO" and argv[argv.index("--direction") + 1] == "izle"
    assert argv[argv.index("--horizon") + 1] == "3 ay" and argv[-1] == "--no-run"
    d = client.get(f"/v1/theses/{body['name']}").json()
    assert d["status"] == "starting" and d["active"] and len(d["source_files"]) == 2
    file = client.get(d["source_files"][0]["url"])
    assert file.status_code == 200 and file.content == b"## Fikir\nmetin"
    assert client.get(f"/v1/theses/{body['name']}/files/zzz").status_code == 404
    again = client.post(
        "/v1/theses/upload",
        data={"ticker": "THYAO", "run": "true"},
        files=[("files", ("a.txt", b"x", "text/plain"))],
    ).json()
    assert again["name"] == f"{body['name']}-2" and spawned[1][-1] == "--run"


def test_upload_validation(client: TestClient, spawned: list[list[str]]) -> None:
    txt = ("files", ("a.txt", b"x", "text/plain"))
    assert client.post("/v1/theses/upload", data={"ticker": "kötü!"}, files=[txt]).status_code == 400
    assert (
        client.post("/v1/theses/upload", data={"ticker": "THYAO", "direction": "up"}, files=[txt]).status_code
        == 400
    )
    exe = ("files", ("a.exe", b"x", "application/octet-stream"))
    assert client.post("/v1/theses/upload", data={"ticker": "THYAO"}, files=[exe]).status_code == 400
    assert client.post("/v1/theses/upload", data={"ticker": "THYAO"}).status_code == 422
    assert not spawned


def test_upload_size_limit(
    client: TestClient, spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(theses_web, "MAX_FILE_BYTES", 10)
    r = client.post(
        "/v1/theses/upload", data={"ticker": "THYAO"}, files=[("files", ("a.txt", b"x" * 50, "text/plain"))]
    )
    assert r.status_code == 413 and not spawned


def test_review_starts_strategy_analyst_and_lists_history(
    client: TestClient, spawned: list[list[str]]
) -> None:
    _write("THYAO-2026-11-06", GOOD)
    r = client.post("/v1/theses/THYAO-2026-11-06/review")
    assert r.status_code == 202
    body = r.json()
    assert (
        body["agent"] == "strategy-analyst"
        and body["ref"] == "THYAO-2026-11-06"
        and body["max_budget_usd"] > 0
    )
    assert spawned[0][spawned[0].index("run") + 1 : spawned[0].index("run") + 3] == [
        "strategy-analyst",
        "THYAO-2026-11-06",
    ]
    runs.write_meta(
        "strategy-analyst",
        RID,
        subject="THYAO-2026-11-06",
        started_at="2026-11-20T10:00:00+00:00",
        status="ok",
        cost_usd=0.9,
        output={"verdict": "mixed", "confidence": 0.55},
    )
    revs = client.get("/v1/theses/THYAO-2026-11-06/reviews").json()
    assert {r["run_id"] for r in revs} == {body["run_id"], RID}
    mine = next(r for r in revs if r["run_id"] == RID)
    assert mine["verdict"] == "mixed" and mine["confidence"] == 0.55 and mine["cost_usd"] == 0.9
    row = client.get("/v1/theses").json()["items"][0]
    assert row["reviews"] == 2 and row["last_review"]["status"] == "starting"  # newest first
    detail = client.get(
        "/v1/agent-runs", params={"ref": "THYAO-2026-11-06", "agent": "strategy-analyst"}
    ).json()
    assert detail["total"] == 2


def test_review_refused_for_unparsable_thesis(client: TestClient, spawned: list[list[str]]) -> None:
    _write("THYAO-2026-11-06", "merhaba")
    r = client.post("/v1/theses/THYAO-2026-11-06/review")
    assert r.status_code == 422 and not spawned
