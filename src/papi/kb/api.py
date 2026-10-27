"""KB retrieval API (contract C8, PRD 03 §5–6).

All reads are against a snapshot: by default the current published one (``current_snapshot``);
pass ``snapshot_id`` to reproduce what an analysis saw, or ``include_draft=True`` to preview
unpublished parse results. Retrieval is point-in-time: ``as_of`` (a date, an aware datetime — taken
in Istanbul time — or an ISO string) selects the version in force on that day.

Version choice for one provision at ``as_of``: among visible versions with ``effective_from`` ≤
``as_of`` (or unknown) and ``effective_to`` > ``as_of`` (or open), the latest ``effective_from``
wins, unknown dates rank as newest (the text is current but its start is not known, so the result
carries ``effective_from_unknown=True`` → "law version uncertain"), ties go to the latest recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select, text

from papi.core.schema import obligation, provision, provision_version, regulation
from papi.core.text import normalize_tr
from papi.core.timeutil import now_ist, to_ist
from papi.kb.snapshot import current_snapshot as _current_snapshot
from papi.kb.snapshot import visible_snapshot_ids

AsOf = date | datetime | str | None
_HEAD = re.compile(
    r"^(?:(?:GEÇİCİ\s+MADDE|EK\s+MADDE|MADDE|Geçici\s+Madde|Ek\s+Madde|Madde)\s+\S+\s*[-–—]|\(\d+\)|"
    r"[a-zçğıöşü]{1,2}\)|\d+\))\s*"
)


@dataclass(frozen=True)
class ProvisionVersion:
    id: str
    provision_id: str
    citation_id: str
    path: str
    heading: str | None
    text: str
    effective_from: date | None
    effective_to: date | None
    amended_by: str | None
    kb_snapshot_id: str | None
    source_span_id: str | None
    recorded_at: datetime
    regulation_id: str
    official_code: str
    regulation_title: str
    authority: str | None
    kind: str

    @property
    def effective_from_unknown(self) -> bool:
        return self.effective_from is None

    @property
    def repealed(self) -> bool:
        """The provision only reads "(Mülga:…)" — repealed, kept as a placeholder in the consolidated text."""
        body = _HEAD.sub("", self.text.strip(), count=1).strip()
        return "mülga" in body.lower() and bool(re.fullmatch(r"(?:\([^()]*\)\s*[.…]*\s*)+", body))

    def label_tr(self) -> str:
        """Human citation, e.g. "II-15.1 m.5/1-a", "6362 geçici m.3", "Özel Durumlar Rehberi II.2.1"."""
        parts = self.path.split("/")
        m = re.fullmatch(r"(gm|em|mk|m)(\d+)([A-ZÇĞİÖŞÜ]?)", parts[0])
        if m is None:
            return f"{self.regulation_title} {self.path}"
        kind, num, suf = m.groups()
        head = (
            {"gm": "geçici m.", "em": "ek m.", "mk": "mükerrer m.", "m": "m."}[kind]
            + num
            + (f"/{suf}" if suf else "")
        )
        tail = ""
        if len(parts) > 1:
            tail = "/" + parts[1].removeprefix("f")
        if len(parts) > 2:
            tail += "-" + "-".join(parts[2:])
        return f"{self.official_code} {head}{tail}"


@dataclass(frozen=True)
class ProvisionHit:
    version: ProvisionVersion
    snippet: str
    rank: float


@dataclass(frozen=True)
class Obligation:
    id: str
    key: str
    provision_id: str
    citation_id: str
    citation_ids: list[str]
    event_codes: list[str]
    title_tr: str | None
    actor: str | None
    trigger: str | None
    deadline_rule: str | None
    required_fields: list[str]
    downstream_steps: list[dict[str, Any]]
    approved: bool
    approved_at: str | None
    note: str | None
    provision: ProvisionVersion | None = None


@dataclass(frozen=True)
class ExpectedStep:
    order: int
    key: str
    label_tr: str
    actor: str | None
    deadline_rule: str | None
    after: str | None
    citation_ids: list[str] = field(default_factory=list)
    obligation_id: str | None = None
    note: str | None = None


# --------------------------------------------------------------------------------------------- helpers


def to_as_of(value: AsOf) -> date:
    """Normalise ``as_of`` to an Istanbul calendar date (``None`` → today in Istanbul)."""
    if value is None:
        return now_ist().date()
    if isinstance(value, datetime):
        return to_ist(value).date()
    if isinstance(value, date):
        return value
    s = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    return to_ist(s).date()


_VERSION_COLS = (
    provision_version.c.id,
    provision_version.c.provision_id,
    provision.c.citation_id,
    provision.c.path,
    provision.c.heading,
    provision_version.c.text,
    provision_version.c.effective_from,
    provision_version.c.effective_to,
    provision_version.c.amended_by,
    provision_version.c.kb_snapshot_id,
    provision_version.c.source_span_id,
    provision_version.c.recorded_at,
    regulation.c.id.label("regulation_id"),
    regulation.c.official_code,
    regulation.c.title.label("regulation_title"),
    regulation.c.issuer_authority.label("authority"),
    regulation.c.kind,
)


def _row_to_version(r: Any) -> ProvisionVersion:
    return ProvisionVersion(**dict(r._mapping))


def _versions_query(snapshot_ids: list[str]):
    return (
        select(*_VERSION_COLS)
        .select_from(provision_version.join(provision).join(regulation))
        .where(provision_version.c.kb_snapshot_id.in_(snapshot_ids))
    )


def pick_version(versions: list[ProvisionVersion], as_of: date) -> ProvisionVersion | None:
    """Version in force on ``as_of``: the latest dated candidate, unless a version with an unknown start
    was recorded after it (then the newer text wins, flagged ``effective_from_unknown``)."""
    cands = [
        v
        for v in versions
        if (v.effective_from is None or v.effective_from <= as_of)
        and (v.effective_to is None or as_of < v.effective_to)
    ]
    dated = [v for v in cands if v.effective_from is not None]
    undated = [v for v in cands if v.effective_from is None]
    best = max(dated, key=lambda v: (v.effective_from, v.recorded_at), default=None)
    latest_undated = max(undated, key=lambda v: v.recorded_at, default=None)
    if latest_undated is not None and (best is None or latest_undated.recorded_at > best.recorded_at):
        return latest_undated
    return best


def _snapshots(conn: Connection, snapshot_id: str | None, include_draft: bool) -> list[str]:
    return visible_snapshot_ids(conn, snapshot_id, include_draft=include_draft)


# ------------------------------------------------------------------------------------------------ C8 API


def current_snapshot(conn: Connection) -> str | None:
    """Current published ``kb_snapshot`` id (what new analyses should record)."""
    return _current_snapshot(conn)


def versions(
    conn: Connection, citation_id: str, *, snapshot_id: str | None = None, include_draft: bool = False
) -> list[ProvisionVersion]:
    """All visible versions of one provision, oldest first."""
    snaps = _snapshots(conn, snapshot_id, include_draft)
    if not snaps:
        return []
    rows = conn.execute(_versions_query(snaps).where(provision.c.citation_id == citation_id)).all()
    out = [_row_to_version(r) for r in rows]
    return sorted(out, key=lambda v: (v.effective_from or date.max, v.recorded_at))


def lookup(
    conn: Connection,
    citation_id: str,
    as_of: AsOf = None,
    *,
    snapshot_id: str | None = None,
    include_draft: bool = False,
) -> ProvisionVersion | None:
    """The version of ``citation_id`` in force on ``as_of`` (``None`` if none is known for that day)."""
    return pick_version(
        versions(conn, citation_id, snapshot_id=snapshot_id, include_draft=include_draft), to_as_of(as_of)
    )


_SUFFIXES = sorted(
    {
        "larinin",
        "lerinin",
        "larin",
        "lerin",
        "lari",
        "leri",
        "lar",
        "ler",
        "sinin",
        "sunun",
        "nin",
        "nun",
        "inin",
        "unun",
        "ini",
        "unu",
        "si",
        "su",
        "in",
        "un",
        "dan",
        "den",
        "tan",
        "ten",
        "da",
        "de",
        "ta",
        "te",
        "i",
        "u",
        "a",
        "e",
    },
    key=len,
    reverse=True,
)


def _stem(token: str) -> str:
    for suf in _SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def fts_query(query: str) -> str:
    """FTS5 MATCH expression: Turkish-normalised tokens, light suffix stemming, prefix match each."""
    tokens = [t for t in normalize_tr(query).split() if t]
    parts = []
    for t in tokens:
        stem = _stem(t) if len(t) > 3 else t
        parts.append(f'"{stem}"*')
    return " ".join(parts)


def _matches_filters(
    v: ProvisionVersion, filters: dict[str, Any] | None, manifest_ids: dict[str, str]
) -> bool:
    if not filters:
        return True
    for key, want in filters.items():
        if want is None:
            continue
        wanted = {want} if isinstance(want, str) else set(want)
        if key == "authority" and (v.authority or "") not in wanted:
            return False
        if key == "kind" and v.kind not in wanted:
            return False
        if key == "official_code" and v.official_code not in wanted:
            return False
        if key == "regulation" and manifest_ids.get(v.regulation_id) not in wanted:
            return False
        if key == "prefix" and not any(v.citation_id.startswith(w) for w in wanted):
            return False
    return True


def search(
    conn: Connection,
    query: str,
    as_of: AsOf = None,
    k: int = 5,
    filters: dict[str, Any] | None = None,
    *,
    snapshot_id: str | None = None,
    include_draft: bool = False,
) -> list[ProvisionHit]:
    """Full-text search over provisions in force on ``as_of`` (filters: authority, kind, official_code,
    regulation = manifest id, prefix = citation prefix). The most specific matching provision is kept
    when a provision and its sub-provisions both match."""
    match = fts_query(query)
    snaps = _snapshots(conn, snapshot_id, include_draft)
    if not match or not snaps:
        return []
    day = to_as_of(as_of)
    rows = conn.execute(
        text(
            "SELECT id, snippet(provision_fts, 1, '[', ']', '…', 16) AS snip, rank FROM provision_fts "
            "WHERE provision_fts MATCH :q ORDER BY rank LIMIT :lim"
        ),
        {"q": match, "lim": max(200, k * 40)},
    ).all()
    if not rows:
        return []
    hit_rank = {r.id: (r.snip, r.rank) for r in rows}
    hit_versions = conn.execute(
        _versions_query(snaps).where(provision_version.c.id.in_(list(hit_rank)))
    ).all()
    prov_ids = {r.provision_id for r in hit_versions}
    all_versions: dict[str, list[ProvisionVersion]] = {}
    for r in conn.execute(_versions_query(snaps).where(provision_version.c.provision_id.in_(prov_ids))).all():
        v = _row_to_version(r)
        all_versions.setdefault(v.provision_id, []).append(v)
    manifest_ids: dict[str, str] = {}
    if filters and "regulation" in filters:
        for rid, extra in conn.execute(select(regulation.c.id, regulation.c.extra)):
            manifest_ids[rid] = (extra or {}).get("manifest_id")
    best: dict[str, ProvisionHit] = {}
    for r in hit_versions:
        v = _row_to_version(r)
        chosen = pick_version(all_versions.get(v.provision_id, []), day)
        if chosen is None or chosen.id != v.id or not _matches_filters(v, filters, manifest_ids):
            continue
        snip, rank = hit_rank[v.id]
        prev = best.get(v.provision_id)
        if prev is None or rank < prev.rank:
            best[v.provision_id] = ProvisionHit(version=v, snippet=snip, rank=rank)
    hits = sorted(best.values(), key=lambda h: h.rank)
    citations = {h.version.citation_id for h in hits}
    specific = [
        h
        for h in hits
        if not any(
            c != h.version.citation_id and c.startswith(h.version.citation_id + ":") for c in citations
        )
    ]
    return specific[:k]


def _obligation_from_row(row: Any) -> tuple[Obligation, dict[str, Any]]:
    wrapper = row.required_fields or {}
    meta = wrapper.get("meta", {})
    ob = Obligation(
        id=row.id,
        key=meta.get("key", row.id),
        provision_id=row.provision_id,
        citation_id=row.citation_id,
        citation_ids=list(meta.get("citation_ids") or [row.citation_id]),
        event_codes=list(row.event_codes or []),
        title_tr=meta.get("title_tr"),
        actor=row.actor,
        trigger=meta.get("trigger"),
        deadline_rule=row.deadline_rule,
        required_fields=list(wrapper.get("fields") or []),
        downstream_steps=list(meta.get("downstream_steps") or []),
        approved=bool(meta.get("approved")),
        approved_at=meta.get("approved_at"),
        note=meta.get("note"),
    )
    return ob, meta


def all_obligations(conn: Connection) -> list[Obligation]:
    rows = conn.execute(
        select(obligation, provision.c.citation_id)
        .select_from(obligation.join(provision))
        .order_by(obligation.c.id)
    ).all()
    return [_obligation_from_row(r)[0] for r in rows]


def obligations(
    conn: Connection,
    event_code: str,
    as_of: AsOf = None,
    approved_only: bool = True,
    *,
    snapshot_id: str | None = None,
    include_draft: bool = False,
) -> list[Obligation]:
    """Obligations for ``event_code`` whose provision is in force on ``as_of`` (approved only by default).

    Each result carries the provision version it rests on (``provision``)."""
    out = []
    for ob in all_obligations(conn):
        if event_code not in ob.event_codes or (approved_only and not ob.approved):
            continue
        pv = lookup(conn, ob.citation_id, as_of, snapshot_id=snapshot_id, include_draft=include_draft)
        if pv is None or pv.repealed:
            continue
        out.append(replace(ob, provision=pv))
    return out


def timeline(
    conn: Connection,
    event_code: str,
    as_of: AsOf = None,
    *,
    approved_only: bool = True,
    snapshot_id: str | None = None,
    include_draft: bool = False,
) -> list[ExpectedStep]:
    """Expected sequence of steps after an event (05 timelines): the disclosure obligation(s) first,
    then each obligation's ``downstream_steps`` in their listed order (deduplicated by step key)."""
    steps: list[ExpectedStep] = []
    seen: set[str] = set()
    obs = obligations(
        conn, event_code, as_of, approved_only, snapshot_id=snapshot_id, include_draft=include_draft
    )
    for ob in obs:
        if ob.key not in seen:
            seen.add(ob.key)
            steps.append(
                ExpectedStep(
                    order=len(steps) + 1,
                    key=ob.key,
                    label_tr=ob.title_tr or ob.key,
                    actor=ob.actor,
                    deadline_rule=ob.deadline_rule,
                    after=None,
                    citation_ids=ob.citation_ids,
                    obligation_id=ob.id,
                    note=ob.trigger,
                )
            )
    for ob in obs:
        prev_key = ob.key
        for st in ob.downstream_steps:
            key = st["key"]
            if key in seen:
                prev_key = key
                continue
            seen.add(key)
            steps.append(
                ExpectedStep(
                    order=len(steps) + 1,
                    key=key,
                    label_tr=st.get("label_tr", key),
                    actor=st.get("actor"),
                    deadline_rule=st.get("deadline_rule"),
                    after=st.get("after", prev_key),
                    citation_ids=list(st.get("cites") or []),
                    obligation_id=ob.id,
                    note=st.get("note"),
                )
            )
            prev_key = key
    return steps
