"""Agent runs page: start a run, list runs, watch one live (13 · agent base system).

Reads the run files the runner writes (``papi.agents.runs``): the transcript grows with every tool
call, so the detail page polls a fragment every 2 s while the run is active.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from papi.agents import runs
from papi.agents.definition import AgentError, list_agents, load_agent
from papi.core.ids import uuid7
from papi.core.timeutil import parse_iso
from papi.web.app import LEGACY
from papi.web.deps import templates

router = APIRouter()
RUN_ID = re.compile(r"^[0-9a-f-]{36}$")
REF = re.compile(r"^[0-9A-Za-z-]{1,40}$")
STALE_AFTER_S = 20 * 60  # an "active" run whose files stopped changing this long ago is not running
RESULT_EXCERPT = 400
OPTIONAL_FIELDS = ("cost_usd", "turns", "max_budget_usd", "gate", "error", "output", "text", "subject")

STATUS_TR = {
    "starting": "başlıyor",
    "running": "çalışıyor",
    "resuming": "eksik okuma: yeniden başlatıldı",
    "ok": "tamamlandı",
    "incomplete": "tamamlanmadı (okuma eksik)",
    "failed": "hata",
    "deferred": "ertelendi (limit)",
    "stale": "yarım kaldı",
}


def _dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def _view(meta: dict[str, Any]) -> dict[str, Any]:
    """Meta + derived display fields (status incl. stale detection, elapsed, trimmed call results)."""
    started, finished = _dt(meta.get("started_at")), _dt(meta.get("finished_at"))
    status = meta.get("status", "starting")
    now = datetime.now(UTC)
    transcript = runs.transcript_path(meta["agent"], meta["run_id"])
    last_touch = max(
        (
            p.stat().st_mtime
            for p in (transcript, transcript.with_name(transcript.stem + ".meta.json"))
            if p.exists()
        ),
        default=0.0,
    )
    if status in runs.ACTIVE and last_touch and now.timestamp() - last_touch > STALE_AFTER_S:
        status = "stale"
    end = finished or now
    calls = []
    for c in meta.get("calls") or []:
        result = c.get("result") or {}
        shown = {k: v for k, v in result.items() if k != "text"}
        if "text" in result:
            shown["text"] = f"<{len(result['text'])} karakter>"
        excerpt = str(shown)
        calls.append(
            {
                "at": _dt(c.get("at")),
                "tool": c.get("tool"),
                "args": c.get("args") or {},
                "ms": c.get("ms"),
                "ok": result.get("success", True),
                "blocking": result.get("blocking"),
                "excerpt": excerpt[:RESULT_EXCERPT] + ("…" if len(excerpt) > RESULT_EXCERPT else ""),
            }
        )
    return {
        **dict.fromkeys(OPTIONAL_FIELDS),
        **meta,
        "status": status,
        "status_tr": STATUS_TR.get(status, status),
        "active": status in runs.ACTIVE,
        "started": started,
        "elapsed_s": int((end - started).total_seconds()) if started else None,
        "calls": calls,
        "usage": meta.get("usage") or {},
    }


@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request) -> HTMLResponse:
    rows = [_view({**m, "calls": []}) for m in runs.list_runs(50)]
    return templates.TemplateResponse(
        request, "agents.html", {"nav": "agents.html", "runs": rows, "agents": list_agents()}
    )


@router.post("/agents/run")
def agents_start(agent: Annotated[str, Form()], ref: Annotated[str, Form()]) -> Response:
    ref = ref.strip()
    try:
        load_agent(agent)
    except (AgentError, ValueError) as exc:
        return HTMLResponse(f'<div class="notice error">{exc}</div>', status_code=400)
    if not REF.match(ref):
        return HTMLResponse(
            '<div class="notice error">Geçersiz konu (bildirim no / tez adı)</div>', status_code=400
        )
    run_id = uuid7()
    runs.write_meta(agent, run_id, subject=ref, started_at=runs.now_iso(), status="starting")
    log = runs.base_path(agent, run_id).with_suffix(".log").open("wb")
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from papi.cli import main; main()",
            "agent",
            "run",
            agent,
            ref,
            "--run-id",
            run_id,
            "--no-calls",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return RedirectResponse(f"{LEGACY}/agents/runs/{run_id}", status_code=303)


def _load(run_id: str) -> dict[str, Any] | None:
    if not RUN_ID.match(run_id):
        return None
    meta = runs.load_run(run_id)
    return _view(meta) if meta else None


@router.get("/agents/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str) -> HTMLResponse:
    run = _load(run_id)
    if run is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"nav": "agents.html", "what": f"Ajan çalışması {run_id}"},
            status_code=404,
        )
    return templates.TemplateResponse(request, "agent_run.html", {"nav": "agents.html", "r": run})


@router.get("/fragments/agents/runs/{run_id}", response_class=HTMLResponse)
def run_fragment(request: Request, run_id: str) -> HTMLResponse:
    run = _load(run_id)
    if run is None:
        return HTMLResponse('<div class="notice error">Çalışma bulunamadı</div>', status_code=404)
    return templates.TemplateResponse(request, "_agent_run_body.html", {"r": run})
