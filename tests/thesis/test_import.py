"""Paper import: page extraction, deterministic thesis building, the upload flow."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Connection

from papi.llm.backend import sandbox_dir
from papi.llm.types import LlmResult
from papi.thesis import document as doc
from papi.thesis import importer
from papi.thesis.extract import Page, docx_pages, extract_file
from papi.web import theses as theses_page
from papi.web.app import create_app


def _page(text: str, n: int = 1, file: str = "a.jpg") -> Page:
    return Page(file, n, "ocr", text)


def test_build_maps_paper_headings_into_the_template() -> None:
    pages = [
        _page(
            "THYAO\nNot defterimden\n→ Fikir:\nPetrol yükselirken hisse yatay.\n"
            "Dipnot: Yerli yolcu oranına bak.\nKarşı Taraf Neden Satıyor?\nDış yatırımcı.",
            1,
        ),
        _page("Fikir\nİkinci sayfada devam eden fikir.\nKonsensus\nPiyasa düşüş bekliyor.", 2),
    ]
    text, warnings = importer.build(
        pages, ticker="THYAO", direction="long", horizon="", day=date(2026, 11, 6)
    )
    t = doc.parse(text, "x")
    got = {k: [i.text for i in v] for k, v in t.sections.items() if v}
    assert got == {
        "fikir": ["Petrol yükselirken hisse yatay.", "İkinci sayfada devam eden fikir."],  # merged
        "konsensus": ["Piyasa düşüş bekliyor."],
        "karsi_taraf": ["Dış yatırımcı."],
        "dipnot": ["Yerli yolcu oranına bak."],  # text after "Dipnot:" on the same line
    }
    assert t.horizon == "belirtilmedi"
    # ticker title line dropped; other text before the first heading is kept out of the analysis
    assert "Not defterimden" in text and not any(
        "Not defterimden" in i.text for v in t.sections.values() for i in v
    )
    assert any("analize girmez" in w for w in warnings)
    assert any("birden çok kez" in w for w in warnings)
    # sections the paper lacks are added empty, so they show up as gaps to fill
    assert "'Pre-mortem' bölümü boş veya yok" in t.gaps


def test_paper_without_any_heading_is_reported() -> None:
    text, warnings = importer.build(
        [_page("Sadece düz metin.")], ticker="THYAO", direction="long", horizon="", day=date(2026, 11, 6)
    )
    assert any("Hiç bölüm başlığı" in w for w in warnings)
    assert doc.parse(text, "x").gaps  # still a readable document


def _docx(paragraphs: list[str], break_before: int) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(
        f"<w:p><w:r>{'<w:br w:type="page"/>' if i == break_before else ''}<w:t>{p}</w:t></w:r></w:p>"
        for i, p in enumerate(paragraphs)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def test_docx_is_split_at_page_breaks() -> None:
    assert docx_pages(_docx(["Fikir", "a", "Konsensüs", "b"], break_before=2)) == ["Fikir\na", "Konsensüs\nb"]


class OcrStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.seen_files: list[bool] = []

    def complete(self, **kw: Any) -> LlmResult:
        self.calls.append(kw)
        image = kw["prompt"].rsplit("./", 1)[-1]
        self.seen_files.append((sandbox_dir() / image).is_file())
        return LlmResult(
            structured_output={"text": "Fikir\nokunan metin", "uncertain": ["metin"]},
            text=None,
            model_ids=["m"],
            cost_usd_est=0.01,
            duration_ms=1,
            exit_code=0,
            raw_json={},
            error_kind=None,
            error=None,
        )


def test_images_are_transcribed_with_only_the_read_tool_and_removed_afterwards(
    db: Connection, tmp_path: Path
) -> None:
    img = tmp_path / "sayfa.png"
    Image.new("RGB", (40, 30), "white").save(img)
    stub = OcrStub()
    pages = extract_file(db, img, backend=stub)  # type: ignore[arg-type]
    assert [(p.method, p.text, p.uncertain) for p in pages] == [("ocr", "Fikir\nokunan metin", ["metin"])]
    assert stub.calls[0]["builtin_tools"] == ["Read"] and stub.calls[0]["allowed_tools"] == ["Read"]
    assert stub.seen_files == [True]
    assert not list(sandbox_dir().glob("ocr-*.jpg"))


def test_import_writes_pages_thesis_and_status(db: Connection, tmp_path: Path) -> None:
    paper = tmp_path / "tez.txt"
    paper.write_text(
        "THYAO\nFikir\nBir fikir.\nTetikleyiciler\n- Bilanço | 2027-03 | %40\n", encoding="utf-8"
    )
    st = importer.run_import(
        "THYAO-2026-11-06",
        [paper],
        ticker="THYAO",
        direction="long",
        horizon="6 ay",
        day=date(2026, 11, 6),
        analyse=False,
    )
    assert st["status"] == "ready" and st["pages_done"] == 1 and not st["errors"]
    t = doc.load("THYAO-2026-11-06")
    assert [(tr.event, tr.probability) for tr in t.triggers] == [("Bilanço", 0.40)]
    assert importer.read_pages("THYAO-2026-11-06")[0]["method"] == "text"
    assert (doc.sources_dir("THYAO-2026-11-06") / "files" / "01-tez.txt").is_file()
    # the name stays taken even before a .md exists for the next upload
    assert doc.new_path("THYAO", date(2026, 11, 6)).stem == "THYAO-2026-11-06-2"


@pytest.fixture
def client() -> Any:
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


def test_upload_stores_files_in_order_and_starts_the_import(client: TestClient, monkeypatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(theses_page.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    r = client.post(
        "/theses/upload",
        data={"ticker": "thyao", "direction": "long", "horizon": "6 ay", "run": "1"},
        files=[("files", ("b sayfa.jpg", b"x", "image/jpeg")), ("files", ("a.pdf", b"y", "application/pdf"))],
        follow_redirects=False,
    )
    assert r.status_code == 303
    name = r.headers["location"].rsplit("/", 1)[-1]
    assert name.startswith("THYAO-")
    assert sorted(p.name for p in importer.files_dir(name).iterdir()) == ["01-b sayfa.jpg", "02-a.pdf"]
    argv = started[0]
    assert argv[argv.index("--name") + 1] == name and "--run" in argv
    assert client.get(f"/theses/{name}").status_code == 200


def test_upload_rejects_unsupported_files(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(theses_page.subprocess, "Popen", lambda *a, **k: pytest.fail("started"))
    r = client.post(
        "/theses/upload",
        data={"ticker": "THYAO"},
        files=[("files", ("x.exe", b"x", "application/octet-stream"))],
    )
    assert r.status_code == 400 and "desteklenmeyen" in r.text


def test_save_replaces_the_thesis_text(client: TestClient) -> None:
    path = doc.theses_dir() / "THYAO-2026-11-06.md"
    path.write_text(doc.template("THYAO", date(2026, 11, 6)), encoding="utf-8")
    r = client.post("/theses/THYAO-2026-11-06/save", data={"content": "yeni"}, follow_redirects=False)
    assert r.status_code == 303 and path.read_text(encoding="utf-8") == "yeni"
    assert client.post("/theses/YOK/save", data={"content": "x"}).status_code == 404
