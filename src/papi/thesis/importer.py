"""Uploaded paper → pages → thesis file in the template → (optionally) a strategy-analyst run.

Everything about one upload lives in ``$PAPI_HOME/theses/sources/<name>/``: the originals under
``files/`` (upload order kept by a numeric prefix), ``pages.json`` (text per page with the method
and the words the transcriber was unsure of) and ``status.json`` (what the dashboard polls). The
thesis itself is ``$PAPI_HOME/theses/<name>.md`` like any hand-written one.

Building the thesis from pages is deterministic: lines that are section names (``heading_in_line``)
open a section, everything until the next one belongs to it; a section named twice is merged; text
before the first heading is kept as a comment (not analysed) and reported; missing sections are
added empty with their guidance so the user can fill them in.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from papi.agents.definition import load_agent
from papi.agents.runner import explain_deferral, run_agent
from papi.core.db import connect
from papi.core.ids import uuid7
from papi.core.jobs import Deferred
from papi.core.text import normalize_tr
from papi.llm.backend import LlmBackend
from papi.thesis import document as doc
from papi.thesis.extract import Page, extract_file

ACTIVE = ("starting", "extracting", "analysing")
AGENT = "strategy-analyst"
METHOD_TR = {"text_layer": "PDF metni", "ocr": "görüntüden okundu", "docx": "Word", "text": "metin"}


def files_dir(name: str) -> Path:
    path = doc.sources_dir(name) / "files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_status(name: str) -> dict[str, Any] | None:
    path = doc.sources_dir(name) / "status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_status(name: str, **fields: Any) -> dict[str, Any]:
    folder = doc.sources_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    data = read_status(name) or {"name": name}
    data.update(fields, updated_at=datetime.now(UTC).isoformat())
    tmp = folder / "status.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "status.json")
    return data


def read_pages(name: str) -> list[dict[str, Any]]:
    path = doc.sources_dir(name) / "pages.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def list_names() -> list[str]:
    """Every thesis: written ones (``*.md``) and uploads still being processed."""
    names = {p.stem for p in doc.theses_dir().glob("*.md")}
    sources = doc.theses_dir() / "sources"
    if sources.is_dir():
        names |= {p.name for p in sources.iterdir() if p.is_dir()}
    return sorted(names, reverse=True)


def _comment(text: str) -> str:
    return text.replace("-->", "—>")


def build(
    pages: list[Page], *, ticker: str, direction: str, horizon: str, day: date
) -> tuple[str, list[str]]:
    """Thesis Markdown from page texts, plus warnings for the user."""
    warnings: list[str] = []
    sections: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: str | None = None
    for k, page in enumerate(pages, start=1):
        marker = f"<!-- sayfa {k}: {_comment(page.file)} s.{page.number} ({METHOD_TR[page.method]}) -->"
        (sections[current] if current else preamble).append(marker)
        for raw in page.text.splitlines():
            line = raw.strip()
            key, rest = doc.heading_in_line(line) if line else (None, "")
            if key:
                if key in sections:
                    warnings.append(
                        f"'{doc.SPEC_BY_KEY[key].title}' başlığı birden çok kez geçiyor; "
                        "metinler birleştirildi"
                    )
                current = key
                sections.setdefault(key, [])
                if rest:
                    sections[key].append(rest)
            elif current:
                sections[current].append(raw.rstrip())
            elif line and normalize_tr(line).upper() != ticker:
                preamble.append(_comment(line))

    out = [
        "---",
        f"ticker: {ticker}",
        f"date: {day.isoformat()}",
        f"direction: {direction}  # long | short | izle",
        f"horizon: {horizon or 'belirtilmedi'}",
        "---",
        "",
        f"# {ticker} ({', '.join(dict.fromkeys(p.file for p in pages))} dosyasından)",
    ]
    loose = [line for line in preamble if not line.startswith("<!--")]
    if loose:
        warnings.append(
            f"İlk başlıktan önceki {len(loose)} satır analize girmez; tez metninde uygun bölüme taşı"
        )
        out += ["<!-- Başlıktan önceki metin (analize girmez, uygun bölüme taşı):", *loose, "-->"]
    else:
        out += preamble
    found = [s for s in doc.SECTIONS if s.key in sections]
    if not found:
        warnings.append("Hiç bölüm başlığı bulunamadı; metni tez metninde bölümlere ayır")
    for key, lines in sections.items():
        while lines and not lines[-1].strip():
            lines.pop()
        out += ["", f"## {doc.SPEC_BY_KEY[key].title}", *lines]
    for spec in doc.SECTIONS:
        if spec.key not in sections:
            optional = "" if spec.required else " (isteğe bağlı)"
            out += ["", f"## {spec.title}", f"<!-- {spec.guidance}{optional} -->"]
    return "\n".join(out) + "\n", warnings


def check_summary(name: str) -> dict[str, Any]:
    """Deterministic check of the thesis file: ``{errors, gaps}`` (``errors`` stop a review)."""
    try:
        t = doc.load(name)
    except FileNotFoundError:
        return {"errors": ["tez dosyası henüz yok"], "gaps": []}
    except doc.ThesisError as exc:
        return {"errors": exc.errors, "gaps": []}
    return {"errors": [], "gaps": t.gaps}


def run_import(
    name: str,
    files: list[Path],
    *,
    ticker: str,
    direction: str,
    horizon: str,
    day: date,
    analyse: bool,
    backend: LlmBackend | None = None,
) -> dict[str, Any]:
    """Extract every page, write ``<name>.md``, check it and (``analyse``) run the strategy analyst.
    Progress and the outcome go to ``status.json``; the final status is returned."""
    stored: list[Path] = []
    target = files_dir(name)
    for i, f in enumerate(files, start=1):
        if f.parent.resolve() == target.resolve():
            stored.append(f)
            continue
        dest = target / f"{i:02d}-{f.name}"
        shutil.copyfile(f, dest)
        stored.append(dest)
    write_status(
        name,
        status="extracting",
        ticker=ticker,
        files=[p.name for p in stored],
        files_done=0,
        pages_done=0,
        error=None,
    )
    pages: list[Page] = []
    try:
        with connect() as conn:
            for n, f in enumerate(stored, start=1):

                def done(page: Page) -> None:
                    pages.append(page)
                    _save_pages(name, pages)
                    write_status(name, pages_done=len(pages))

                extract_file(conn, f, backend=backend, on_page=done)
                write_status(name, files_done=n)
            text, warnings = build(pages, ticker=ticker, direction=direction, horizon=horizon, day=day)
            path = doc.theses_dir() / f"{name}.md"
            path.write_text(text, encoding="utf-8")
            check = check_summary(name)
            status = write_status(
                name, status="needs_fix" if check["errors"] else "ready", warnings=warnings, **check
            )
            if analyse and not check["errors"]:
                status = _analyse(conn, name, backend)
    except Deferred as exc:
        status = write_status(name, status="failed", error=explain_deferral(exc))
    except Exception as exc:  # shown on the dashboard
        status = write_status(name, status="failed", error=f"{type(exc).__name__}: {exc}")
    return status


def _save_pages(name: str, pages: list[Page]) -> None:
    path = doc.sources_dir(name) / "pages.json"
    path.write_text(json.dumps([p.as_dict() for p in pages], ensure_ascii=False, indent=1), encoding="utf-8")


def _analyse(conn: Any, name: str, backend: LlmBackend | None) -> dict[str, Any]:

    agent = load_agent(AGENT)
    run_id = uuid7()
    write_status(name, status="analysing", run_id=run_id)
    run = run_agent(conn, agent, agent.prompt.format(ref=name), subject=name, run_id=run_id, backend=backend)
    return write_status(name, status="done", run_status=run.meta.get("status"))
