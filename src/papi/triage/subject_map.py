"""KAP subject (+ class / template / summary keywords / form fields) → event code(s).

File format: ``packs/tr/kap_subject_map.yaml`` (documented at its top). All subject names, keywords and
labels are compared after ``papi.core.text.normalize_tr``; keywords match at a word start and may be
word prefixes (``sözleşme`` matches ``sözleşmesi``).

Evaluation per disclosure:
1. Find the entry for the subject (exact normalised match, else longest matching ``subject_prefixes``),
   else the ``fallback`` entry.
2. An entry is either unconditional (``codes``) or an ordered rule list (``rules`` + ``default``).
   Rules are tested in order. A matching rule with ``continue: true`` adds its codes and evaluation
   goes on; a matching rule without it adds its codes and stops. ``use: <rule_set>`` inlines a shared
   rule list. No match → ``default`` (``other.unclassified`` unless set).
3. Keyword conditions are tried on the KAP summary first; if that yields only the default and a body
   text is available, the rules are tried again on the head of the body text.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from papi.core.config import get_settings
from papi.core.text import normalize_tr, parse_tr_number
from papi.triage.taxonomy import UNCLASSIFIED, Taxonomy

BODY_HEAD_CHARS = 800
_NEGATIVE_VALUES = {"", "-", "yok", "hayir", "hayir no", "0", "none", "null"}
_NEGATIVE_PREFIXES = ("odenmeyecek", "dagitilmayacak", "yapilmayacak", "bulunmamaktadir", "yoktur")


class SubjectMapError(ValueError):
    """The subject map file is malformed or references unknown codes."""


@dataclass(frozen=True)
class Rule:
    codes: tuple[str, ...]
    classes: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()
    templates: tuple[str, ...] = ()
    any_kw: tuple[str, ...] = ()
    all_kw: tuple[str, ...] = ()
    none_kw: tuple[str, ...] = ()
    fields_nonzero: tuple[str, ...] = ()
    cont: bool = False
    rid: str = ""

    @property
    def uses_text(self) -> bool:
        return bool(self.any_kw or self.all_kw or self.none_kw)


@dataclass(frozen=True)
class Entry:
    key: str  # normalised subject (or prefix / "*fallback*")
    rules: tuple[Rule, ...]
    default: tuple[str, ...]


@dataclass(frozen=True)
class MatchInput:
    subject: str | None
    source_class: str | None = None
    source_type: str | None = None
    summary: str | None = None
    body_text: str | None = None
    template_id: str | None = None
    form_fields: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MatchResult:
    codes: tuple[str, ...]
    entry: str  # entry key that handled the subject
    mapped_by: str  # subject | prefix | fallback
    rule_ids: tuple[str, ...]  # rules that fired ("default" when none)
    matched_on: str | None  # summary | body | None (no keyword rule fired)


def _kw(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SubjectMapError(f"{where}: keyword list expected")
    out = tuple(k for k in (normalize_tr(v) for v in value) if k)
    if len(out) != len(value):
        raise SubjectMapError(f"{where}: empty keyword after normalisation")
    return out


def _codes(value: Any, where: str, taxonomy: Taxonomy) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise SubjectMapError(f"{where}: codes must be a non-empty list")
    for code in value:
        if code not in taxonomy:
            raise SubjectMapError(f"{where}: unknown event code {code!r}")
    return tuple(dict.fromkeys(value))


def _upper_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    return frozenset(str(v).strip().upper() for v in value)


_RULE_KEYS = {
    "codes",
    "class",
    "type",
    "template",
    "any",
    "all",
    "none",
    "fields_nonzero",
    "continue",
    "use",
    "id",
}


class SubjectMap:
    """Compiled subject map."""

    def __init__(self, raw: Any, taxonomy: Taxonomy, *, path: Path | None = None) -> None:
        if not isinstance(raw, dict):
            raise SubjectMapError("subject map root must be a mapping")
        self.path = path
        self.version = raw.get("version")
        self.taxonomy = taxonomy
        sets_raw = raw.get("rule_sets") or {}
        if not isinstance(sets_raw, dict):
            raise SubjectMapError("rule_sets must be a mapping")
        self._sets_raw = sets_raw
        self.rule_sets: dict[str, tuple[Rule, ...]] = {}
        for name in sets_raw:
            self.rule_sets[name] = self._compile_rules(sets_raw[name], f"rule_set {name}", stack=(name,))
        self.subjects: dict[str, Entry] = {}
        self.display: dict[str, str] = {}  # normalised subject → subject as written in the file
        for subj, spec in (raw.get("subjects") or {}).items():
            key = normalize_tr(subj)
            if key in self.subjects:
                raise SubjectMapError(f"duplicate subject after normalisation: {subj!r}")
            self.subjects[key] = self._entry(key, spec, f"subject {subj!r}")
            self.display[key] = subj
        self.prefixes: list[tuple[str, Entry]] = []
        for subj, spec in (raw.get("subject_prefixes") or {}).items():
            key = normalize_tr(subj)
            self.prefixes.append((key, self._entry(key, spec, f"prefix {subj!r}")))
        self.prefixes.sort(key=lambda kv: -len(kv[0]))
        fb = raw.get("fallback")
        self.fallback = (
            self._entry("*fallback*", fb, "fallback")
            if fb is not None
            else Entry("*fallback*", (), (UNCLASSIFIED,))
        )

    # ----------------------------------------------------------------------------------- compile
    def _entry(self, key: str, spec: Any, where: str) -> Entry:
        if isinstance(spec, (str, list)):
            return Entry(key, (), _codes(spec, where, self.taxonomy))
        if not isinstance(spec, dict):
            raise SubjectMapError(f"{where}: must be codes or a mapping")
        if "codes" in spec:
            if "rules" in spec:
                raise SubjectMapError(f"{where}: use either codes or rules")
            return Entry(key, (), _codes(spec["codes"], where, self.taxonomy))
        rules = self._compile_rules(spec.get("rules"), where, stack=())
        default = _codes(spec.get("default", UNCLASSIFIED), f"{where} default", self.taxonomy)
        return Entry(key, rules, default)

    def _compile_rules(self, raw: Any, where: str, *, stack: tuple[str, ...]) -> tuple[Rule, ...]:
        if not isinstance(raw, list) or not raw:
            raise SubjectMapError(f"{where}: rules must be a non-empty list")
        out: list[Rule] = []
        for i, r in enumerate(raw):
            w = f"{where} rule {i}"
            if not isinstance(r, dict):
                raise SubjectMapError(f"{w}: must be a mapping")
            unknown = set(r) - _RULE_KEYS
            if unknown:
                raise SubjectMapError(f"{w}: unknown keys {sorted(unknown)}")
            if "use" in r:
                name = r["use"]
                if name in stack:
                    raise SubjectMapError(f"{w}: recursive rule_set {name!r}")
                if name not in self._sets_raw:
                    raise SubjectMapError(f"{w}: unknown rule_set {name!r}")
                if name not in self.rule_sets:
                    self.rule_sets[name] = self._compile_rules(
                        self._sets_raw[name], f"rule_set {name}", stack=(*stack, name)
                    )
                out.extend(self.rule_sets[name])
                continue
            out.append(
                Rule(
                    codes=_codes(r.get("codes"), w, self.taxonomy),
                    classes=_upper_set(r.get("class")),
                    types=_upper_set(r.get("type")),
                    templates=_kw(r.get("template"), w),
                    any_kw=_kw(r.get("any"), w),
                    all_kw=_kw(r.get("all"), w),
                    none_kw=_kw(r.get("none"), w),
                    fields_nonzero=_kw(r.get("fields_nonzero"), w),
                    cont=bool(r.get("continue", False)),
                    rid=str(r.get("id") or f"{where}#{i}"),
                )
            )
        return tuple(out)

    # ------------------------------------------------------------------------------------- match
    def entry_for(self, subject: str | None) -> tuple[Entry, str]:
        key = normalize_tr(subject or "")
        if key in self.subjects:
            return self.subjects[key], "subject"
        for prefix, entry in self.prefixes:
            if key.startswith(prefix):
                return entry, "prefix"
        return self.fallback, "fallback"

    def is_mapped(self, subject: str | None) -> bool:
        return self.entry_for(subject)[1] != "fallback"

    def match(self, inp: MatchInput) -> MatchResult:
        entry, how = self.entry_for(inp.subject)
        if not entry.rules:
            return MatchResult(entry.default, entry.key, how, ("subject",), None)
        cls = (inp.source_class or "").upper()
        typ = (inp.source_type or "").upper()
        template = normalize_tr(inp.template_id or "")
        fields = _fields_nonzero_index(inp.form_fields, inp.tables)
        summary = normalize_tr(inp.summary or "")
        if how == "fallback":  # unknown subject: its name carries meaning too
            summary = f"{normalize_tr(inp.subject or '')} {summary}".strip()
        texts = [("summary", summary)]
        if inp.body_text:
            texts.append(("body", normalize_tr(inp.body_text[:BODY_HEAD_CHARS])))
        for label, text in texts:
            codes, fired, used_text = _run(entry.rules, cls, typ, template, fields, f" {text} ")
            if fired:
                return MatchResult(codes, entry.key, how, fired, label if used_text else None)
        return MatchResult(entry.default, entry.key, how, ("default",), None)


def _has(text: str, kw: str) -> bool:
    return f" {kw}" in text


def _numeric_nonzero(value: Any) -> bool:
    try:
        return value is not None and parse_tr_number(str(value)) != 0
    except (ValueError, InvalidOperation, ArithmeticError):
        return False


def _nonzero(value: Any) -> bool:
    if value is None:
        return False
    norm = normalize_tr(str(value))
    if norm in _NEGATIVE_VALUES or norm.startswith(_NEGATIVE_PREFIXES):
        return False
    try:
        return parse_tr_number(str(value)) != 0
    except (ValueError, InvalidOperation, ArithmeticError):
        return True


def _fields_nonzero_index(
    form_fields: list[dict[str, Any]], tables: list[dict[str, Any]] | None = None
) -> list[str]:
    """Labels carrying a present, non-zero, non-negative ("Hayır", "Ödenmeyecek", "-") value: form field
    labels, table header cells with such a value in a data row, and captions of tables with one."""
    out: list[str] = []
    for f in form_fields or []:
        if _nonzero(f.get("value")):
            label = f.get("label_norm") or normalize_tr(str(f.get("label") or ""))
            out.append(f" {label} ")
    for t in tables or []:
        header = t.get("header") or []
        rows = [r for r in t.get("rows") or [] if r and not normalize_tr(str(r[0])).startswith("toplam")]
        live_cols = {c for r in rows for c, cell in enumerate(r) if c > 0 and _numeric_nonzero(cell)}
        out.extend(f" {normalize_tr(str(header[c]))} " for c in sorted(live_cols) if c < len(header))
        if live_cols and t.get("caption"):
            out.append(f" {normalize_tr(str(t['caption']))} ")
    return out


def _run(
    rules: tuple[Rule, ...], cls: str, typ: str, template: str, fields: list[str], text: str
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    codes: list[str] = []
    fired: list[str] = []
    used_text = False
    for rule in rules:
        if rule.classes and cls not in rule.classes:
            continue
        if rule.types and typ not in rule.types:
            continue
        if rule.templates and not any(t in template for t in rule.templates):
            continue
        if rule.any_kw and not any(_has(text, k) for k in rule.any_kw):
            continue
        if rule.all_kw and not all(_has(text, k) for k in rule.all_kw):
            continue
        if rule.none_kw and any(_has(text, k) for k in rule.none_kw):
            continue
        if rule.fields_nonzero and not any(_has(f, k) for f in fields for k in rule.fields_nonzero):
            continue
        codes.extend(c for c in rule.codes if c not in codes)
        fired.append(rule.rid)
        used_text = used_text or rule.uses_text
        if not rule.cont:
            break
    return tuple(codes), tuple(fired), used_text


def default_path() -> Path:
    return get_settings().pack_dir / "kap_subject_map.yaml"


def load(path: Path | None = None, taxonomy: Taxonomy | None = None) -> SubjectMap:
    from papi.triage import taxonomy as tax_mod

    tax = taxonomy or tax_mod.load()
    return _load_cached(str((path or default_path()).resolve()), tax)


@functools.cache
def _load_cached(path_str: str, taxonomy: Taxonomy) -> SubjectMap:
    path = Path(path_str)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SubjectMap(raw, taxonomy, path=path)


def clear_cache() -> None:
    _load_cached.cache_clear()
