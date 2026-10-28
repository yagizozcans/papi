"""Turkish query processing for KB full-text search (PRD 03 §5).

Index and queries share ``papi.core.text.normalize_tr`` (Turkish lowercase, diacritics folded). On top
of that a query is reduced to *terms*:

* stopwords (``packs/<jurisdiction>/legal/search_terms.yaml``: function words and legal filler such as
  "ilişkin", "dair", "hakkında") are dropped;
* each token is stemmed: common case/plural/possessive suffixes are stripped, and a final softened
  consonant (ğ/b/d after suffix removal: "üyeliği" → "uyelig") is cut, so the prefix also matches
  the hard form ("üyelik");
* pack synonyms add alternatives ("bildirim" → "açıklama", "dönük" → "yönelik"); a term matches
  when any alternative matches (prefix match).

``and_match`` requires every term; ``or_match`` accepts any (FTS5 bm25 then ranks documents matching
more and rarer terms first).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from papi.core.config import get_settings
from papi.core.text import normalize_tr

_SUFFIXES = sorted(
    {
        "larinin", "lerinin", "larina", "lerine", "larin", "lerin", "lari", "leri", "lar", "ler",
        "sinin", "sunun", "sini", "sunu", "sina", "sine", "nin", "nun", "inin", "unun", "ini", "unu",
        "ina", "ine", "una", "une", "si", "su", "in", "un", "dan", "den", "tan", "ten", "da", "de",
        "ta", "te", "ya", "ye", "yi", "yu", "i", "u", "a", "e",
    },
    key=len,
    reverse=True,
)  # fmt: skip
_SOFTENED = ("g", "b", "d")  # folded ğ/b/d ← k/p/t before a vowel-initial suffix
MIN_STEM = 3


@dataclass(frozen=True)
class Term:
    token: str  # normalised query token
    alternatives: tuple[str, ...]  # stems, prefix-matched


@dataclass(frozen=True)
class SearchTerms:
    stopwords: frozenset[str]
    synonyms: dict[str, tuple[str, ...]]  # stem → extra alternatives (normalised words)
    event_terms: dict[str, str]  # event code → hint query text


def stem(token: str) -> str:
    """Light Turkish stemmer on a normalised token (see module doc)."""
    if len(token) <= MIN_STEM:
        return token
    out = token
    for suf in _SUFFIXES:
        if out.endswith(suf) and len(out) - len(suf) >= MIN_STEM:
            out = out[: -len(suf)]
            break
    if out != token and out.endswith(_SOFTENED) and len(out) > MIN_STEM + 1:
        out = out[:-1]
    return out


def terms_path() -> Path:
    return get_settings().pack_dir / "legal" / "search_terms.yaml"


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> SearchTerms:
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    stop = frozenset(normalize_tr(w) for w in data.get("stopwords") or [])
    syn: dict[str, tuple[str, ...]] = {}
    for word, alts in (data.get("synonyms") or {}).items():
        key = stem(normalize_tr(word))
        extra = tuple(normalize_tr(a) for a in alts)
        syn[key] = tuple(dict.fromkeys(syn.get(key, ()) + extra))
    events = {str(code): str(q) for code, q in (data.get("event_terms") or {}).items()}
    return SearchTerms(stopwords=stop, synonyms=syn, event_terms=events)


def search_terms(path: Path | None = None) -> SearchTerms:
    """The pack's stopwords/synonyms/event hints (reloaded when the file changes)."""
    p = path or terms_path()
    if not p.exists():
        return SearchTerms(frozenset(), {}, {})
    return _load(str(p), p.stat().st_mtime)


def query_terms(query: str, cfg: SearchTerms | None = None) -> list[Term]:
    cfg = cfg or search_terms()
    out: list[Term] = []
    seen: set[str] = set()
    for tok in normalize_tr(query).split():
        if tok in cfg.stopwords or tok in seen:
            continue
        seen.add(tok)
        s = stem(tok)
        alts = [s]
        for syn in cfg.synonyms.get(s, ()):
            alts.extend(stem(w) for w in syn.split())
        out.append(Term(tok, tuple(dict.fromkeys(alts))))
    return out


def _group(t: Term) -> str:
    alts = [f'"{a}"*' for a in t.alternatives]
    return alts[0] if len(alts) == 1 else "(" + " OR ".join(alts) + ")"


def and_match(terms: list[Term]) -> str:
    """FTS5 expression requiring every term (any of its alternatives)."""
    return " AND ".join(_group(t) for t in terms)


def or_match(terms: list[Term]) -> str:
    """FTS5 expression accepting any term."""
    return " OR ".join(_group(t) for t in terms)


def fts_query(query: str) -> str:
    """AND expression for ``query`` (kept for callers that want the strict form)."""
    return and_match(query_terms(query))
