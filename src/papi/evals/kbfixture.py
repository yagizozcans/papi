"""Offline KB for the retrieval suite: built once from the KB slice's text fixtures, committed as JSON.

``build()`` runs the real legal pipeline (``papi.sources.legal.fetch.fetch_entry`` with the fixture
bytes served by an ``httpx.MockTransport``, ``papi.kb.ingest.ingest_entry``, ``papi.kb.snapshot.publish``)
inside a scratch ``PAPI_HOME`` and dumps the published KB rows (regulation, provision,
provision_version, kb_snapshot, ``provision_fts``) to ``tests/golden/kb/kb.json`` (``papi eval kb-build``).
``load(conn)`` restores that dump into a scratch database for ``papi.kb.search`` / ``lookup``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Connection, Date, Table, select, text

from papi.core import db
from papi.core.config import REPO_ROOT
from papi.core.schema import jurisdiction, kb_snapshot, provision, provision_version, regulation
from papi.core.timeutil import iso_utc
from papi.evals.core import GoldenMissing, golden_dir
from papi.evals.scratch import scratch_home

KB_FIXTURES = REPO_ROOT / "tests" / "kb" / "fixtures"
KB_FILE = Path("kb") / "kb.json"
# manifest id → (current text fixture, historical text fixtures in manifest ``history`` order)
SOURCES: dict[str, tuple[str, list[str]]] = {
    "spk-ii-15-1": ("ii15_1_consolidated.txt", ["ii15_1_original_rg_excerpt.html"]),
    "law-6362": ("law6362_excerpt.txt", []),
    "kap-bildirim-yonergesi": ("kap_bildirim_yonergesi_excerpt.txt", []),
    "spk-rehber-ozel-durumlar": ("ozel_durumlar_rehberi_excerpt.txt", []),
    "spk-rehber-kar-payi": ("kar_payi_rehberi_excerpt.txt", []),
    "spk-rehber-onemli-nitelikteki-islemler": ("onemli_nitelikteki_islemler_rehberi_excerpt.txt", []),
    "bist-pay-piyasasi-yonergesi": ("bist_pay_piyasasi_yonergesi_excerpt.txt", []),
}
# Dumped tables; document links are dropped (the source documents are not part of the fixture).
TABLES: tuple[tuple[Table, tuple[str, ...]], ...] = (
    (kb_snapshot, ()),
    (regulation, ("source_document_id",)),
    (provision, ()),
    (provision_version, ("source_span_id",)),
)


def _ctype(name: str) -> str:
    return "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8"


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return iso_utc(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


def dump(conn: Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table, drop in TABLES:
        rows = conn.execute(select(table).order_by(table.c.id)).mappings().all()
        out[table.name] = [
            {k: (None if k in drop else _jsonable(v)) for k, v in r.items() if k != "jurisdiction_id"}
            for r in rows
        ]
    out["provision_fts"] = [
        list(r) for r in conn.execute(text("SELECT id, text FROM provision_fts ORDER BY id")).all()
    ]
    return out


def build(entries: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the KB pipeline on the text fixtures in a scratch home; returns the dump."""
    from papi.kb import snapshot
    from papi.kb.ingest import ingest_entry
    from papi.sources.legal.fetch import PoliteClient, fetch_entry
    from papi.sources.legal.manifest import load_manifest

    if not KB_FIXTURES.is_dir():
        raise GoldenMissing(f"KB text fixtures not found: {KB_FIXTURES}")
    ids = list(entries or SOURCES)
    with scratch_home():
        manifest = {e.id: e for e in load_manifest().documents}
        routes: dict[str, str] = {}
        picked = []
        for mid in ids:
            current, history = SOURCES[mid]
            e = manifest[mid]
            routes[e.source_urls[0]] = current
            for h, name in zip(e.history, history, strict=False):
                routes[h.url] = name
            picked.append(e.model_copy(update={"history": e.history[: len(history)]}))

        def handler(request: httpx.Request) -> httpx.Response:
            name = routes.get(str(request.url))
            if name is None:
                return httpx.Response(404)
            content = (KB_FIXTURES / name).read_bytes()
            return httpx.Response(200, content=content, headers={"content-type": _ctype(name)})

        client = PoliteClient(min_interval=0.0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
        try:
            for e in picked:
                fetch_entry(e, client)
        finally:
            client.close()
        bad = [r for r in (ingest_entry(e) for e in picked) if r.status != "parsed"]
        if bad:
            raise RuntimeError("; ".join(f"{r.entry_id}: {r.status} {r.error or ''}" for r in bad))
        with db.transaction() as conn:
            snapshot.publish(conn, note="papi eval fixture KB")
        with db.connect() as conn:
            out = dump(conn)
    out = {"source": "tests/kb/fixtures", "entries": {m: list(SOURCES[m]) for m in ids}, **out}
    return out


def write(base: Path | None = None) -> tuple[Path, dict[str, int]]:
    data = build()
    p = (base or golden_dir()) / KB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    return p, {t: len(data[t]) for t in ("regulation", "provision", "provision_version", "provision_fts")}


def load(conn: Connection, base: Path | None = None) -> dict[str, Any]:
    """Restore the committed KB dump into ``conn`` (a seeded scratch database); commits."""
    p = (base or golden_dir()) / KB_FILE
    if not p.exists():
        raise GoldenMissing(f"KB fixture not found: {p} (run `papi eval kb-build`)")
    data = json.loads(p.read_text(encoding="utf-8"))
    jid = conn.execute(select(jurisdiction.c.id).where(jurisdiction.c.code == "TR")).scalar_one()
    for table, _ in TABLES:
        dates = {c.name for c in table.columns if isinstance(c.type, Date)}
        rows = []
        for r in data[table.name]:
            row = {k: (date.fromisoformat(v) if k in dates and v else v) for k, v in r.items()}
            if "jurisdiction_id" in table.c:
                row["jurisdiction_id"] = jid
            rows.append(row)
        if rows:
            conn.execute(table.insert(), rows)
    for vid, body in data["provision_fts"]:
        conn.execute(text("INSERT INTO provision_fts (id, text) VALUES (:id, :t)"), {"id": vid, "t": body})
    conn.commit()
    return data
