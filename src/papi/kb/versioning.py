"""Point-in-time dating of parsed provisions (PRD 03 §3.2).

For each provision of a consolidated text the current wording is in force since the latest
amendment that touched it: its own markers, any marker inside its sub-provisions, and any marker
that replaced a whole ancestor ("MADDE 2 – (Değişik:RG-…)"). Without amendments the provision dates
from the document's entry into force (Yürürlük article). When any relevant amendment cannot be dated,
or the Yürürlük wording is not a plain one, ``effective_from`` stays ``None`` (= unknown, flagged).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from papi.core.text import normalize_tr
from papi.sources.legal.amendments import base_effective, parse_revision_appendix, resolve_marker
from papi.sources.legal.structure import ParsedDocument, ParsedProvision


@dataclass
class Dating:
    effective_from: date | None
    amended_by: str | None
    first_change: date | None = None  # earliest dated amendment touching the provision
    added_on: date | None = None  # the provision was inserted ("Ek …") on this date
    basis: str = ""
    unresolved: list[str] = field(default_factory=list)
    repealed: bool = False


@dataclass
class DocumentDating:
    base: date | None
    base_basis: str
    provisions: dict[str, Dating]
    revisions: list[dict]


_HEAD_RE = re.compile(
    r"^(?:(?:GEÇİCİ\s+MADDE|EK\s+MADDE|MADDE|Geçici\s+Madde|Ek\s+Madde|Madde)\s+\S+\s*[-–—]|\(\d+\)|[a-zçğıöşü]{1,2}\)|\d+\))\s*"
)


def _article_segment(path: str) -> str:
    return path.split("/", 1)[0]


def _article_number(seg: str) -> str | None:
    m = re.fullmatch(r"m(\d+)[A-ZÇĞİÖŞÜ]?", seg)
    return m.group(1) if m else None


def _is_repealed(p: ParsedProvision) -> bool:
    if not any(m.kind == "mulga" and m.leading for m in p.markers):
        return False
    body = _HEAD_RE.sub("", p.own_text, count=1)
    for m in p.markers:
        body = body.replace(m.raw, "")
    return len(body.strip(" .…()")) < 3 and not p.text.count("\n")


def date_document(doc: ParsedDocument) -> DocumentDating:
    listed = doc.amendment_list
    yururluk = next(
        (
            p
            for p in doc.provisions
            if p.depth == 0
            and p.heading
            and normalize_tr(p.heading).startswith("yururluk")
            and not normalize_tr(p.heading).startswith("yururlukten")
        ),
        None,
    )
    base, excluded, basis = base_effective(yururluk.text if yururluk else None, doc.header.gazette)
    revisions = parse_revision_appendix(doc.appendix_lines)
    revised: dict[str, list[date]] = {}
    for rev in revisions:
        for seg in rev.articles:
            revised.setdefault(seg, []).append(rev.date)

    by_path = {p.path: p for p in doc.provisions}
    descendants: dict[str, list[ParsedProvision]] = {p.path: [] for p in doc.provisions}
    for p in doc.provisions:
        for a in p.ancestors:
            descendants.setdefault(a, []).append(p)

    resolved_cache: dict[int, tuple[date | None, str | None]] = {}

    def res(mk):
        key = id(mk)
        if key not in resolved_cache:
            resolved_cache[key] = resolve_marker(mk, listed)
        return resolved_cache[key]

    out: dict[str, Dating] = {}
    for p in doc.provisions:
        relevant = list(p.markers)
        for d in descendants.get(p.path, []):
            relevant.extend(d.markers)
        for a in p.ancestors:
            anc = by_path.get(a)
            if anc:
                relevant.extend(m for m in anc.markers if m.leading)
        dates: list[tuple[date, str | None]] = []
        unresolved: list[str] = []
        added_on = None
        for mk in relevant:
            d, label = res(mk)
            if d is None:
                unresolved.append(mk.raw)
            else:
                dates.append((d, label))
        for mk in p.markers:
            if mk.leading and mk.kind == "ek":
                d, _ = res(mk)
                added_on = d if added_on is None or (d and d < added_on) else added_on
        art = _article_segment(p.path)
        rev_dates = revised.get(art, [])
        if rev_dates and p.depth == 0:
            dates.extend((d, f"Revizyon {d.strftime('%d/%m/%Y')}") for d in rev_dates)
        elif rev_dates:
            unresolved.append(
                f"article {art} revised on {', '.join(str(d) for d in rev_dates)} (sub-provision unknown)"
            )
        first_change = min((d for d, _ in dates), default=None)
        if unresolved:
            dating = Dating(None, None, first_change, added_on, "amendment date unknown", unresolved)
        elif dates:
            latest = max(d for d, _ in dates)
            labels = sorted({lbl for d, lbl in dates if d == latest and lbl})
            dating = Dating(latest, "; ".join(labels) or None, first_change, added_on, "latest amendment")
        elif base and _article_number(art) not in excluded:
            dating = Dating(base, None, None, None, basis)
        else:
            dating = Dating(None, None, None, None, basis if base is None else "own entry-into-force date")
        dating.repealed = _is_repealed(p)
        out[p.path] = dating
    return DocumentDating(
        base=base,
        base_basis=basis,
        provisions=out,
        revisions=[{"date": r.date.isoformat(), "articles": sorted(r.articles)} for r in revisions],
    )


def renumbering_risk(doc: ParsedDocument, dating: DocumentDating, since: date) -> set[str]:
    """Paths whose position may have shifted since ``since``: a sibling inserted ("Ek") after that date
    makes it and every later sibling (with their sub-provisions) unsafe to map onto an older text."""
    children: dict[str, list[ParsedProvision]] = {}
    for p in doc.provisions:
        parent = p.ancestors[0] if p.ancestors else ""
        children.setdefault(parent, []).append(p)
    risky: set[str] = set()
    for sibs in children.values():
        shifted = False
        for p in sibs:
            d = dating.provisions[p.path]
            if d.added_on is not None and d.added_on > since:
                shifted = True
            if shifted:
                risky.add(p.path)
    for p in doc.provisions:
        if any(a in risky for a in p.ancestors):
            risky.add(p.path)
    return risky
