"""Agent runs page: listing, live fragment polling, run start validation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from papi.agents import runs
from papi.web import agents as agents_page
from papi.web.app import create_app

RID = "01a1a30a-7438-71aa-a106-ecc3318fd0de"


@pytest.fixture
def client() -> Any:
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


def test_fragment_polls_only_while_the_run_is_active(client: TestClient) -> None:
    runs.write_meta("kap-analyst", RID, subject="1691891", started_at=runs.now_iso(), status="running")
    with runs.transcript_path("kap-analyst", RID).open("w") as fh:
        fh.write(
            json.dumps({"at": runs.now_iso(), "tool": "use_skill", "args": {}, "ms": 1, "result": {}}) + "\n"
        )
    live = client.get(f"/fragments/agents/runs/{RID}")
    assert live.status_code == 200 and 'hx-trigger="every 2s"' in live.text and "1 araç çağrısı" in live.text
    runs.write_meta("kap-analyst", RID, status="ok", finished_at=runs.now_iso())
    done = client.get(f"/fragments/agents/runs/{RID}")
    assert "hx-trigger" not in done.text and "tamamlandı" in done.text


def test_listing_and_detail_handle_a_run_that_never_got_a_summary(client: TestClient) -> None:
    runs.write_meta(
        "kap-analyst", RID, subject="1", started_at=runs.now_iso(), status="deferred", error="Ertelendi: x"
    )
    assert client.get("/agents").status_code == 200
    assert "Ertelendi: x" in client.get(f"/agents/runs/{RID}").text


def test_unknown_or_malformed_run_ids_are_404(client: TestClient) -> None:
    assert client.get("/agents/runs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/fragments/agents/runs/..%2f..%2fetc").status_code == 404


def test_start_validates_input_and_spawns_the_cli(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(agents_page.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    assert client.post("/agents/run", data={"agent": "nope", "ref": "1"}).status_code == 400
    assert client.post("/agents/run", data={"agent": "kap-analyst", "ref": "1; rm -rf /"}).status_code == 400
    assert not spawned
    r = client.post("/agents/run", data={"agent": "kap-analyst", "ref": "1691891"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[1]
    assert spawned[0][spawned[0].index("--run-id") + 1] == run_id
    assert runs.load_run(run_id)["status"] == "starting"
