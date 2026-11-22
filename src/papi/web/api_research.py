"""Typed ``/v1`` endpoints of the Research section: agents and theses (14 §3.5, phase P2 slice B).

Thin layer over the same backend the legacy dashboard pages use: agent runs are the files written by
``papi.agents.runs`` (started detached by ``papi agent run``), theses are the Markdown files and import
status of ``papi.thesis``. Nothing here calls Claude directly; starting a run or an import spawns the
CLI process, exactly like ``/legacy/agents/run`` and ``/legacy/theses/upload``.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from papi.agents import runs
from papi.agents.definition import AgentDef, AgentError, list_agents, load_agent
from papi.core.timeutil import ist_date, parse_iso
from papi.thesis import document as doc
from papi.thesis import importer
from papi.thesis.extract import SUPPORTED
from papi.web import agents as agents_web
from papi.web import theses as theses_web
from papi.web.schemas import ErrorOut
from papi.web.schemas_research import (
    AgentDefOut,
    AgentListOut,
    AgentRunDetail,
    AgentRunListOut,
    AgentRunRow,
    AgentSkillOut,
    CheckThesisIn,
    NewThesisIn,
    RunCall,
    RunUsage,
    SaveThesisIn,
    StartRunIn,
    StartRunOut,
    ThesisCheckOut,
    ThesisDetail,
    ThesisFile,
    ThesisIssue,
    ThesisListOut,
    ThesisOutlineItem,
    ThesisPage,
    ThesisReviewRow,
    ThesisRow,
    UploadThesisOut,
)

router = APIRouter(prefix="/v1")
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut}}
BAD: dict[int | str, dict[str, Any]] = {400: {"model": ErrorOut}, 404: {"model": ErrorOut}}
CONFLICT: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}
REF_KIND = {"kap-analyst": "disclosure", importer.AGENT: "thesis"}
COMMENT_OPEN, COMMENT_CLOSE = "<!--", "-->"


def _dt(value: Any) -> datetime | None:
    try:
        return parse_iso(value) if value else None
    except (TypeError, ValueError):
        return None


def _num(value: Any, cast: Any) -> Any:
    try:
        return cast(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------ agents


def _agent_def(a: AgentDef) -> AgentDefOut:
    return AgentDefOut(
        name=a.name,
        description=a.description,
        version=a.agent_version,
        model=a.model,
        max_turns=a.max_turns,
        max_budget_usd=a.max_budget_usd,
        timeout_s=a.timeout_s,
        priority=a.priority,
        skills=[
            AgentSkillOut(name=s.name, description=s.description, tools=list(s.allowed_tools))
            for s in a.skills
        ],
        tools=[t.name for t in a.tools],
        builtin_tools=list(a.builtin_tools),
        ref_kind=REF_KIND.get(a.name, "other"),  # type: ignore[arg-type]
    )


@router.get("/agents", response_model=AgentListOut)
def list_agent_defs() -> AgentListOut:
    out: list[AgentDefOut] = []
    for name in list_agents():
        try:
            out.append(_agent_def(load_agent(name)))
        except (AgentError, ValueError):  # an invalid definition must not hide the others
            continue
    return AgentListOut(agents=out)


def _row(view: dict[str, Any]) -> AgentRunRow:
    output = view.get("output")
    verdict = output.get("verdict") if isinstance(output, dict) else None
    return AgentRunRow(
        run_id=str(view["run_id"]),
        agent=str(view["agent"]),
        subject=view.get("subject") or None,
        status=view["status"],
        active=bool(view["active"]),
        started_at=view.get("started"),
        finished_at=_dt(view.get("finished_at")),
        elapsed_s=view.get("elapsed_s"),
        cost_usd=_num(view.get("cost_usd"), float),
        max_budget_usd=_num(view.get("max_budget_usd"), float),
        turns=_num(view.get("turns"), int),
        error=str(view["error"]) if view.get("error") else None,
        verdict=verdict if isinstance(verdict, str) else None,
    )


def _sorted_runs() -> list[dict[str, Any]]:
    views = [agents_web._view({**m, "calls": []}) for m in runs.list_runs(100_000) if m.get("run_id")]
    views.sort(key=lambda v: v.get("started_at") or "", reverse=True)
    return views


@router.get("/agent-runs", response_model=AgentRunListOut)
def list_agent_runs(
    agent: str | None = None,
    ref: str | None = Query(None, description="Exact run subject (KAP no, thesis name)."),
    status: str | None = Query(None, description="A run status, or `active` for runs in progress."),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentRunListOut:
    views = _sorted_runs()
    if agent:
        views = [v for v in views if v["agent"] == agent]
    if ref:
        views = [v for v in views if v.get("subject") == ref]
    if status:
        views = [v for v in views if (v["active"] if status == "active" else v["status"] == status)]
    return AgentRunListOut(
        items=[_row(v) for v in views[offset : offset + limit]], total=len(views), limit=limit, offset=offset
    )


def _call(c: dict[str, Any]) -> RunCall:
    return RunCall(
        at=c.get("at"),
        tool=str(c.get("tool")),
        args=c.get("args") or {},
        ms=_num(c.get("ms"), int),
        ok=bool(c.get("ok", True)),
        blocking=str(c["blocking"]) if c.get("blocking") else None,
        result_excerpt=c.get("excerpt") or "",
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunDetail, responses=NOT_FOUND)
def get_agent_run(run_id: str) -> AgentRunDetail:
    if not agents_web.RUN_ID.match(run_id):
        raise HTTPException(404, "Ajan çalışması bulunamadı")
    meta = runs.load_run(run_id)
    if meta is None:
        raise HTTPException(404, "Ajan çalışması bulunamadı")
    view = agents_web._view(meta)
    usage = view["usage"]
    output = view.get("output")
    return AgentRunDetail(
        **_row(view).model_dump(),
        agent_version=view.get("agent_version"),
        max_turns=_num(view.get("max_turns"), int),
        gate=str(view["gate"]) if view.get("gate") else None,
        resumed=bool(view.get("resumed")),
        usage=RunUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        ),
        calls=[_call(c) for c in view["calls"]],
        output=output if isinstance(output, dict) else None,
        text=view.get("text") if isinstance(view.get("text"), str) else None,
    )


@router.post("/agent-runs", response_model=StartRunOut, status_code=202, responses=BAD)
def start_agent_run(body: StartRunIn) -> StartRunOut:
    """Start a run in the background (the CLI process); poll ``GET /agent-runs/{id}`` for progress."""
    ref = body.ref.strip()
    try:
        agent = load_agent(body.agent)
    except (AgentError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not agents_web.REF.match(ref):
        raise HTTPException(400, "Geçersiz konu (bildirim no / tez adı)")
    run_id = agents_web.spawn_run(agent.name, ref)
    return StartRunOut(run_id=run_id, agent=agent.name, ref=ref, max_budget_usd=agent.max_budget_usd)


# ------------------------------------------------------------------------------------------ theses


def _check_name(name: str) -> None:
    if not theses_web.NAME.match(name) or name not in importer.list_names():
        raise HTTPException(404, "Tez bulunamadı")


def _key(title: str) -> str | None:
    return doc._heading_key(title)


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _outline_lines(text: str) -> list[tuple[int, str]]:
    """``(line, title)`` of every ``## `` heading outside HTML comments (1-based lines)."""
    out: list[tuple[int, str]] = []
    in_comment = False
    for n, raw in enumerate(_lines(text), start=1):
        line = raw
        if in_comment:
            if COMMENT_CLOSE in line:
                in_comment = False
            continue
        if COMMENT_OPEN in line and COMMENT_CLOSE not in line.split(COMMENT_OPEN, 1)[1]:
            in_comment = True
            line = line.split(COMMENT_OPEN, 1)[0]
        if line.startswith("## "):
            out.append((n, line[3:].strip().rstrip(":?").strip()))
    return out


def _issue(text: str, message: str) -> ThesisIssue:
    """Attach a line to a parse error message where it can be told from the text."""
    lines = _lines(text)
    headings = _outline_lines(text)
    line: int | None = None
    meta_key = re.match(r"(ticker|date|direction|horizon):", message)
    heading = re.search(r"'## (.+?)'", message)
    twice = re.match(r"'(.+?)' bölümü iki kez", message)
    if meta_key:
        line = next((i + 1 for i, s in enumerate(lines) if s.startswith(meta_key.group(1) + ":")), 1)
    elif message.startswith(("ön bilgi", "bilinmeyen ön bilgi", "dosya '---'")):
        line = 1
    elif heading:
        line = next((n for n, title in headings if title == heading.group(1)), None)
    elif twice:
        hits = [
            n
            for n, title in headings
            if (_key(title) or "") and doc.SPEC_BY_KEY[_key(title) or ""].title == twice.group(1)
        ]
        line = hits[1] if len(hits) > 1 else None
    elif message.startswith("satır "):  # prose before the first section heading
        first = headings[0][0] if headings else len(lines) + 1
        start = 0
        if lines and lines[0] == "---":
            start = next((i + 1 for i in range(1, len(lines)) if lines[i] == "---"), 0)
        line = next(
            (
                i + 1
                for i in range(start, first - 1)
                if lines[i].strip() and not lines[i].startswith("# ") and COMMENT_OPEN not in lines[i]
            ),
            None,
        )
    return ThesisIssue(message=message, line=line)


def check_text(text: str, name: str) -> tuple[ThesisCheckOut, doc.Thesis | None]:
    try:
        thesis = doc.parse(text, name)
    except doc.ThesisError as exc:
        return (
            ThesisCheckOut(
                ok=False,
                errors=[_issue(text, e) for e in exc.errors],
                gaps=[],
                sections_filled=0,
                sections_total=len(doc.SECTIONS),
                triggers=0,
                kill_signals=0,
            ),
            None,
        )
    filled = sum(1 for s in doc.SECTIONS if thesis.sections.get(s.key))
    return (
        ThesisCheckOut(
            ok=True,
            errors=[],
            gaps=list(thesis.gaps),
            sections_filled=filled,
            sections_total=len(doc.SECTIONS),
            triggers=len(thesis.triggers),
            kill_signals=len(thesis.kill_signals),
        ),
        thesis,
    )


def _outline(text: str, thesis: doc.Thesis | None) -> list[ThesisOutlineItem]:
    out: list[ThesisOutlineItem] = []
    for line, title in _outline_lines(text):
        key = _key(title)
        spec = doc.SPEC_BY_KEY.get(key) if key else None
        items = len(thesis.sections.get(key, [])) if thesis is not None and key else None
        out.append(
            ThesisOutlineItem(
                line=line,
                title=title,
                key=key,
                required=bool(spec and spec.required),
                items=items,
                empty=items == 0,
            )
        )
    return out


def _reviews(name: str, all_views: list[dict[str, Any]]) -> list[ThesisReviewRow]:
    out: list[ThesisReviewRow] = []
    for v in all_views:
        if v["agent"] != importer.AGENT or v.get("subject") != name:
            continue
        output = v.get("output") if isinstance(v.get("output"), dict) else {}
        verdict = output.get("verdict")
        out.append(
            ThesisReviewRow(
                run_id=str(v["run_id"]),
                status=v["status"],
                verdict=verdict if isinstance(verdict, str) else None,
                confidence=_num(output.get("confidence"), float),
                started_at=v.get("started"),
                cost_usd=_num(v.get("cost_usd"), float),
            )
        )
    return out


def _file_list(name: str) -> list[ThesisFile]:
    folder = doc.sources_dir(name) / "files"
    if not folder.is_dir():
        return []
    return [
        ThesisFile(name=p.name, size=p.stat().st_size, url=f"/v1/theses/{name}/files/{p.name}")
        for p in sorted(folder.iterdir())
        if p.is_file()
    ]


def _read(name: str) -> str | None:
    path = doc.theses_dir() / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _row_for(
    name: str,
    all_views: list[dict[str, Any]],
    text: str | None,
    st: dict[str, Any],
    checked: tuple[ThesisCheckOut | None, doc.Thesis | None],
) -> ThesisRow:
    check, thesis = checked
    prefix = name.split("-", 1)[0]
    revs = _reviews(name, all_views)
    return ThesisRow(
        name=name,
        ticker=thesis.ticker if thesis else (prefix if doc.TICKER_RE.match(prefix) else None),
        date=thesis.date if thesis else None,
        direction=thesis.direction if thesis else None,
        horizon=thesis.horizon if thesis else None,
        status=st["status"],
        active=bool(st["active"]),
        error_count=len(check.errors) if check else None,
        gap_count=len(check.gaps) if check else None,
        reviews=len(revs),
        last_review=revs[0] if revs else None,
        files=len(_file_list(name)),
    )


def _checked(
    name: str, text: str | None, st: dict[str, Any]
) -> tuple[ThesisCheckOut | None, doc.Thesis | None]:
    if text is None or st["active"]:
        return None, None
    return check_text(text, name)


def _detail(name: str) -> ThesisDetail:
    text = _read(name)
    st = theses_web._status(name)
    check, thesis = _checked(name, text, st)
    row = _row_for(name, _sorted_runs(), text, st, (check, thesis))
    return ThesisDetail(
        **row.model_dump(),
        text=text,
        check=check,
        outline=_outline(text, thesis) if text is not None else [],
        warnings=[str(w) for w in st.get("warnings") or []],
        status_error=str(st["error"]) if st.get("error") else None,
        files_done=_num(st.get("files_done"), int),
        pages_done=_num(st.get("pages_done"), int),
        run_id=str(st["run_id"]) if st.get("run_id") else None,
        source_files=_file_list(name),
        pages=[
            ThesisPage(
                file=str(p.get("file")),
                number=int(p.get("number") or 0),
                method=str(p.get("method") or ""),
                text=str(p.get("text") or ""),
                uncertain=[str(w) for w in p.get("uncertain") or []],
            )
            for p in importer.read_pages(name)
        ],
    )


@router.get("/theses", response_model=ThesisListOut)
def list_theses() -> ThesisListOut:
    views = _sorted_runs()
    items = []
    for name in importer.list_names():
        text, st = _read(name), theses_web._status(name)
        items.append(_row_for(name, views, text, st, _checked(name, text, st)))
    return ThesisListOut(items=items, accept=sorted(SUPPORTED))


@router.post("/theses", response_model=ThesisDetail, status_code=201, responses=BAD)
def new_thesis(body: NewThesisIn) -> ThesisDetail:
    """Create ``<TICKER>-<date>.md`` from the fixed template (like ``papi thesis new``)."""
    code = body.ticker.strip().upper()
    if not doc.TICKER_RE.match(code):
        raise HTTPException(400, "Geçersiz BIST kodu (ör. THYAO)")
    day = body.date or ist_date()
    path = doc.new_path(code, day)
    path.write_text(doc.template(code, day), encoding="utf-8")
    return _detail(path.stem)


@router.post("/theses/upload", response_model=UploadThesisOut, status_code=202, responses=BAD)
async def upload_thesis(
    files: Annotated[list[UploadFile], File()],
    ticker: Annotated[str, Form()],
    direction: Annotated[str, Form()] = "long",
    horizon: Annotated[str, Form()] = "",
    run: Annotated[bool, Form()] = False,
) -> UploadThesisOut:
    """Store the papers in upload order and read them in the background (``papi thesis import``).

    ``run`` also starts the strategy analyst once the text is read (costs money); off by default."""
    code = ticker.strip().upper()
    if not doc.TICKER_RE.match(code):
        raise HTTPException(400, "Geçersiz BIST kodu (ör. THYAO)")
    if direction not in doc.DIRECTIONS:
        raise HTTPException(400, "Yön long, short ya da izle olmalı")
    files = [f for f in files if f.filename]
    if not files or len(files) > theses_web.MAX_FILES:
        raise HTTPException(400, f"1 ile {theses_web.MAX_FILES} arasında dosya seç")
    for f in files:
        if "." + (f.filename or "").rsplit(".", 1)[-1].lower() not in SUPPORTED:
            raise HTTPException(400, f"{f.filename}: desteklenmeyen tür ({', '.join(sorted(SUPPORTED))})")
    name = doc.new_path(code, ist_date()).stem
    target = importer.files_dir(name)
    stored: list[Path] = []
    for i, f in enumerate(files, start=1):
        safe = theses_web.SAFE_CHARS.sub("_", (f.filename or "dosya").rsplit("/", 1)[-1]).strip() or "dosya"
        dest = target / f"{i:02d}-{safe}"
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(theses_web.CHUNK):
                size += len(chunk)
                if size > theses_web.MAX_FILE_BYTES:
                    out.close()
                    for p in [*stored, dest]:
                        p.unlink(missing_ok=True)
                    limit = theses_web.MAX_FILE_BYTES // (1024 * 1024)
                    raise HTTPException(413, f"{f.filename}: {limit} MB sınırını aşıyor")
                out.write(chunk)
        stored.append(dest)
    theses_web.spawn_import(name, stored, code, direction, horizon, run)
    return UploadThesisOut(name=name, files=[p.name for p in stored])


@router.get("/theses/{name}", response_model=ThesisDetail, responses=NOT_FOUND)
def get_thesis(name: str) -> ThesisDetail:
    _check_name(name)
    return _detail(name)


@router.put("/theses/{name}/text", response_model=ThesisDetail, responses=CONFLICT)
def save_thesis(name: str, body: SaveThesisIn) -> ThesisDetail:
    """Save the edited Markdown as is; the response carries the parse check of the saved text."""
    _check_name(name)
    if importer.read_status(name) and theses_web._status(name)["active"]:
        raise HTTPException(409, "Dosya şu an işleniyor; bitince kaydet")
    path = doc.theses_dir() / f"{name}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(body.content.replace("\r\n", "\n"), encoding="utf-8")
    tmp.replace(path)
    return _detail(name)


@router.post("/theses/{name}/check", response_model=ThesisCheckOut, responses=NOT_FOUND)
def check_thesis(name: str, body: CheckThesisIn | None = None) -> ThesisCheckOut:
    """Deterministic check of the saved file, or of ``content`` (an unsaved draft)."""
    _check_name(name)
    text = body.content if body and body.content is not None else _read(name)
    if text is None:
        raise HTTPException(404, "Tez dosyası henüz yok")
    return check_text(text, name)[0]


@router.get("/theses/{name}/files/{file_name}", responses=NOT_FOUND)
def download_thesis_file(name: str, file_name: str) -> FileResponse:
    folder = doc.sources_dir(name) / "files"
    if (
        not theses_web.NAME.match(name)
        or not folder.is_dir()
        or file_name not in {p.name for p in folder.iterdir()}
    ):
        raise HTTPException(404, "Dosya bulunamadı")
    return FileResponse(folder / file_name, filename=file_name)


@router.get("/theses/{name}/reviews", response_model=list[ThesisReviewRow], responses=NOT_FOUND)
def list_thesis_reviews(name: str) -> list[ThesisReviewRow]:
    _check_name(name)
    return _reviews(name, _sorted_runs())


@router.post("/theses/{name}/review", response_model=StartRunOut, status_code=202, responses=CONFLICT)
def start_thesis_review(name: str) -> StartRunOut:
    """Start a strategy-analyst run on the thesis (costs up to the agent's budget)."""
    _check_name(name)
    if theses_web._status(name)["active"]:
        raise HTTPException(409, "Dosya şu an işleniyor; bitince incele")
    text = _read(name)
    if text is None:
        raise HTTPException(404, "Tez dosyası henüz yok")
    check, _ = check_text(text, name)
    if not check.ok:
        raise HTTPException(
            422, "Önce şablon hatalarını düzelt: " + "; ".join(e.message for e in check.errors)
        )
    agent = load_agent(importer.AGENT)
    run_id = agents_web.spawn_run(agent.name, name)
    return StartRunOut(run_id=run_id, agent=agent.name, ref=name, max_budget_usd=agent.max_budget_usd)
