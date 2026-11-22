"""Theses page: upload a paper (PDF, image, DOCX, TXT), watch it being read page by page, edit the
thesis text, start the strategy analyst.

The upload is stored under ``$PAPI_HOME/theses/sources/<name>/files/`` and processed by a detached
``papi thesis import`` process (``papi.thesis.importer``), which writes ``status.json`` and
``pages.json``; the detail page polls a fragment while it runs, like the agent run page.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from papi.agents import runs
from papi.core.timeutil import ist_date
from papi.thesis import document as doc
from papi.thesis import importer
from papi.thesis.extract import SUPPORTED
from papi.web.app import LEGACY
from papi.web.deps import templates

router = APIRouter()
NAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{0,59}$")
SAFE_CHARS = re.compile(r"[^\w.\- ]+")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 30
MAX_TEXT_CHARS = 200_000
STALE_AFTER_S = 20 * 60
CHUNK = 1024 * 1024

STATUS_TR = {
    "starting": "başlıyor",
    "extracting": "sayfalar okunuyor",
    "analysing": "ajan çalışıyor",
    "ready": "hazır",
    "done": "tamamlandı",
    "needs_fix": "düzeltme gerekli",
    "failed": "hata",
    "manual": "elle yazıldı",
    "stale": "yarım kaldı",
}
VERDICT_TR = {"supported": "destekleniyor", "mixed": "karışık", "weak": "zayıf", "refuted": "çürüdü"}


def _error(msg: str, code: int = 400) -> HTMLResponse:
    return HTMLResponse(f'<div class="notice error">{msg}</div>', status_code=code)


def _runs_for(name: str) -> list[dict[str, Any]]:
    """This thesis's strategy-analyst runs, newest first, with a Turkish label (verdict or status)."""
    out = []
    for r in runs.list_runs(200):
        if r.get("agent") == importer.AGENT and r.get("subject") == name:
            verdict = (r.get("output") or {}).get("verdict")
            out.append({**r, "label": VERDICT_TR.get(verdict, verdict) if verdict else r.get("status")})
    return out


def _status(name: str) -> dict[str, Any]:
    st = importer.read_status(name) or {"name": name, "status": "manual"}
    status = st.get("status", "manual")
    path = doc.sources_dir(name) / "status.json"
    stale = path.exists() and datetime.now(UTC).timestamp() - path.stat().st_mtime > STALE_AFTER_S
    if status in importer.ACTIVE and stale:
        status = "stale"
    return {
        **st,
        "status": status,
        "status_tr": STATUS_TR.get(status, status),
        "active": status in importer.ACTIVE,
    }


def _view(name: str) -> dict[str, Any] | None:
    if name not in importer.list_names():
        return None
    st = _status(name)
    md = doc.theses_dir() / f"{name}.md"
    text = md.read_text(encoding="utf-8") if md.exists() else None
    check = importer.check_summary(name) if text is not None and not st["active"] else None
    folder = doc.sources_dir(name) / "files"
    return {
        "name": name,
        "st": st,
        "text": text,
        "check": check,
        "pages": importer.read_pages(name),
        "files": sorted(p.name for p in folder.iterdir()) if folder.is_dir() else [],
        "runs": _runs_for(name),
        "method_tr": importer.METHOD_TR,
    }


@router.get("/theses", response_class=HTMLResponse)
def theses_page(request: Request) -> HTMLResponse:
    rows = []
    for name in importer.list_names():
        st = _status(name)
        check = None if st["active"] else importer.check_summary(name)
        last = next(iter(_runs_for(name)), None)
        rows.append({"name": name, "st": st, "check": check, "last": last})
    return templates.TemplateResponse(
        request,
        "theses.html",
        {"nav": "theses.html", "rows": rows, "accept": ",".join(sorted(SUPPORTED)), "today": ist_date()},
    )


@router.post("/theses/upload")
async def theses_upload(
    files: Annotated[list[UploadFile], File()],
    ticker: Annotated[str, Form()],
    direction: Annotated[str, Form()] = "long",
    horizon: Annotated[str, Form()] = "",
    run: Annotated[str | None, Form()] = None,
) -> Response:
    code = ticker.strip().upper()
    if not doc.TICKER_RE.match(code):
        return _error("Geçersiz BIST kodu (ör. THYAO)")
    if direction not in doc.DIRECTIONS:
        return _error("Yön long, short ya da izle olmalı")
    files = [f for f in files if f.filename]
    if not files or len(files) > MAX_FILES:
        return _error(f"1 ile {MAX_FILES} arasında dosya seç")
    for f in files:
        if "." + (f.filename or "").rsplit(".", 1)[-1].lower() not in SUPPORTED:
            return _error(f"{f.filename}: desteklenmeyen tür ({', '.join(sorted(SUPPORTED))})")
    name = doc.new_path(code, ist_date()).stem
    target = importer.files_dir(name)
    stored = []
    for i, f in enumerate(files, start=1):
        safe = SAFE_CHARS.sub("_", (f.filename or "dosya").rsplit("/", 1)[-1]).strip() or "dosya"
        dest = target / f"{i:02d}-{safe}"
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(CHUNK):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return _error(f"{f.filename}: {MAX_FILE_BYTES // (1024 * 1024)} MB sınırını aşıyor")
                out.write(chunk)
        stored.append(dest)
    importer.write_status(name, status="starting", ticker=code, files=[p.name for p in stored])
    log = (doc.sources_dir(name) / "import.log").open("wb")
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from papi.cli import main; main()",
            "thesis",
            "import",
            *map(str, stored),
            "--name",
            name,
            "--ticker",
            code,
            "--direction",
            direction,
            "--horizon",
            horizon.strip()[:60],
            "--run" if run else "--no-run",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return RedirectResponse(f"{LEGACY}/theses/{name}", status_code=303)


@router.get("/theses/{name}", response_class=HTMLResponse)
def thesis_page(request: Request, name: str) -> HTMLResponse:
    t = _view(name) if NAME.match(name) else None
    if t is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"nav": "theses.html", "what": f"Tez {name}"}, status_code=404
        )
    return templates.TemplateResponse(request, "thesis.html", {"nav": "theses.html", "t": t})


@router.get("/fragments/theses/{name}", response_class=HTMLResponse)
def thesis_fragment(request: Request, name: str) -> HTMLResponse:
    t = _view(name) if NAME.match(name) else None
    if t is None:
        return _error("Tez bulunamadı", 404)
    return templates.TemplateResponse(request, "_thesis_body.html", {"t": t})


@router.post("/theses/{name}/save")
def thesis_save(name: str, content: Annotated[str, Form()]) -> Response:
    if not NAME.match(name) or name not in importer.list_names():
        return _error("Tez bulunamadı", 404)
    if len(content) > MAX_TEXT_CHARS:
        return _error("Metin çok uzun")
    if importer.read_status(name) and _status(name)["active"]:
        return _error("Dosya şu an işleniyor; bitince kaydet", 409)
    (doc.theses_dir() / f"{name}.md").write_text(content.replace("\r\n", "\n"), encoding="utf-8")
    return RedirectResponse(f"{LEGACY}/theses/{name}", status_code=303)


@router.get("/theses/{name}/files/{file_name}")
def thesis_file(name: str, file_name: str) -> Response:
    folder = doc.sources_dir(name) / "files"
    if not NAME.match(name) or not folder.is_dir() or file_name not in {p.name for p in folder.iterdir()}:
        return _error("Dosya bulunamadı", 404)
    return FileResponse(folder / file_name)
