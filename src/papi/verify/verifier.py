"""Deterministic verifier for analyst output (PRD 04 §2 "VD", 06 §5.2 invariants, 11).

Checks, in order:
1. schema validity of the whole output (``analyst.v1.json``);
2. event codes exist in the taxonomy;
3. claims — ``fact``: ≥1 resolvable span or KB citation; ``computation``: a ``formula_id``, ≥1 cited
   input span and a result reproducible from two cited input numbers; every number/date in a claim
   appears in its cited text (``inference`` without citations: anywhere in the pack); no advice language;
4. payload — per-code payload valid against ``event.<code>.v1.json``; each numeric (and date) field
   not already fixed by the rules triage equals a value present in its ``payload_spans`` text;
5. texts (headline, summaries, rationale, watch-next items) — sentence by sentence: numbers/dates
   traceable to the pack or to verified claims, no advice language. Failing sentences are dropped
   from the text that is shown (``TextCheck.kept``).

The result is a :class:`VerifyReport`; ``ok`` is True only when nothing failed.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

import jsonschema

from papi.verify.numbers import Num, Pool, first_number, to_decimal, untraced
from papi.verify.phrases import find_advice

SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"'“(]?[A-ZÇĞİÖŞÜ0-9])")
FORMULAS = ("sum", "difference", "ratio", "percent_of", "percent_change", "product")
MAX_INPUTS = 80
TEXT_FIELDS = ("headline_tr", "summary_tr", "summary_en", "materiality_rationale")


@dataclass
class VerifySources:
    """What the analyst was shown (rebuilt from the context pack manifest)."""

    spans: dict[int, str]  # pack ordinal → span text
    provisions: dict[str, str]  # KB citation id → label + text
    context_text: str = ""  # metadata, triage, issuer, position sections of the pack
    allowed_codes: set[str] | None = None
    payload_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    form_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)  # code → rules payload

    def pool(self) -> Pool:
        p = Pool().add_text(self.context_text)
        for t in self.spans.values():
            p.add_text(t)
        for t in self.provisions.values():
            p.add_text(t)
        return p


@dataclass
class ClaimCheck:
    index: int
    kind: str
    status: str  # pass | fail
    issues: list[str] = field(default_factory=list)
    spans: list[int] = field(default_factory=list)  # resolved pack ordinals
    citations: list[str] = field(default_factory=list)  # resolved KB citation ids


@dataclass
class TextCheck:
    field: str
    original: str
    kept: str
    dropped: list[dict[str, Any]] = field(default_factory=list)  # {"sentence", "issues"}


@dataclass
class VerifyReport:
    ok: bool = True
    schema_errors: list[str] = field(default_factory=list)
    unknown_codes: list[str] = field(default_factory=list)
    claims: list[ClaimCheck] = field(default_factory=list)
    texts: dict[str, TextCheck] = field(default_factory=dict)
    watch_next: list[dict[str, Any]] = field(default_factory=list)  # {"text", "kept", "issues"}
    payload_errors: dict[str, str] = field(default_factory=dict)  # code → schema error
    payload_fields: dict[str, dict[str, str]] = field(default_factory=dict)  # code → {field: issue}
    advice: list[dict[str, str]] = field(default_factory=list)  # {"where", "phrase", "match"}

    @property
    def issues(self) -> list[str]:
        out = [f"şema: {e}" for e in self.schema_errors]
        out += [f"bilinmeyen olay kodu: {c}" for c in self.unknown_codes]
        for c in self.claims:
            out += [f"iddia {c.index}: {i}" for i in c.issues]
        for code, err in self.payload_errors.items():
            out.append(f"payload {code}: {err}")
        for code, fields in self.payload_fields.items():
            out += [f"payload {code}.{f}: {i}" for f, i in fields.items()]
        for t in self.texts.values():
            out += [f"{t.field}: {'; '.join(d['issues'])}" for d in t.dropped]
        out += [f"watch_next: {'; '.join(w['issues'])}" for w in self.watch_next if not w["kept"]]
        return out

    def failed_claims(self) -> set[int]:
        return {c.index for c in self.claims if c.status == "fail"}

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = self.issues
        return d


# ------------------------------------------------------------------------------------------ helpers


def _schema_errors(output: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(output), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "<kök>"
        errors.append(f"{path}: {err.message}"[:300])
    return errors


def _advice(text: str | None, where: str, report: VerifyReport) -> list[str]:
    hits = find_advice(text)
    for h in hits:
        report.advice.append({"where": where, "phrase": h.label, "match": h.match})
    return [f"tavsiye dili: “{h.match}”" for h in hits]


def _untraced_msgs(text: str | None, pool: Pool, also_ok: list[list[Num]] | None = None) -> list[str]:
    return [
        f"kaynakta olmayan {'tarih' if u.kind == 'date' else 'sayı'}: {u.raw}"
        for u in untraced(text, pool, also_ok=also_ok)
    ]


def _ops(a: Decimal, b: Decimal, formula: str) -> list[Decimal]:
    try:
        if formula == "sum":
            return [a + b]
        if formula == "difference":
            return [a - b, abs(a - b)]
        if formula == "product":
            return [a * b]
        if b == 0:
            return []
        if formula == "ratio":
            return [a / b]
        if formula == "percent_of":
            return [a / b * 100]
        if formula == "percent_change":
            x = (a - b) / b * 100
            return [x, abs(x)]
    except (InvalidOperation, DivisionByZero):
        return []
    return []


def _reproducible(result: list[Num], cited: set[Decimal], context: set[Decimal], formula: str) -> bool:
    """Some ``formula(a, b)`` equals the result at its written precision; at least one operand comes
    from the cited spans, the other from cited spans or the pack's context (position, last close)."""
    values = list(dict.fromkeys([*sorted(cited), *sorted(context - cited)]))[:MAX_INPUTS]
    for a, b in itertools.permutations(values, 2):
        if a not in cited and b not in cited:
            continue
        for x in _ops(a, b, formula):
            if any(abs(x - r.value) <= r.tol or abs(abs(x) - abs(r.value)) <= r.tol for r in result):
                return True
    return False


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Leaf values of a payload by dotted path (``counterparty.name``, ``items.0.amount``)."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{path}."))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.update(flatten(item, f"{path}.{i}."))
                else:
                    out[f"{path}.{i}"] = item
        else:
            out[path] = value
    return out


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------------------------------ checks


def _check_claims(output: dict[str, Any], src: VerifySources, global_pool: Pool, rep: VerifyReport) -> None:
    for i, c in enumerate(output.get("claims") or []):
        kind = c.get("kind")
        chk = ClaimCheck(index=i, kind=kind, status="pass")
        for o in c.get("span_ordinals") or []:
            if o in src.spans:
                chk.spans.append(o)
            else:
                chk.issues.append(f"çözümlenemeyen span S{o}")
        for cid in c.get("citation_ids") or []:
            if cid in src.provisions:
                chk.citations.append(cid)
            else:
                chk.issues.append(f"pakette olmayan KB kimliği {cid}")
        cited = Pool()
        for o in chk.spans:
            cited.add_text(src.spans[o])
        for cid in chk.citations:
            cited.add_text(src.provisions[cid])
        text = " ".join(t for t in (c.get("text_tr"), c.get("text_en")) if t)

        if kind == "fact":
            if not (chk.spans or chk.citations):
                chk.issues.append("olgu iddiasında çözümlenebilir kaynak yok")
            chk.issues += _untraced_msgs(f"{text} {c.get('value') or ''}", cited)
        elif kind == "computation":
            formula = c.get("formula_id")
            result = first_number(c.get("value")) or None
            if formula not in FORMULAS:
                chk.issues.append("hesaplama iddiasında formula_id yok")
            if not chk.spans:
                chk.issues.append("hesaplama girdileri için span yok")
            if result is None:
                chk.issues.append("hesaplama sonucu (value) yok")
            elif formula in FORMULAS:
                context = Pool().add_text(src.context_text).values
                if not _reproducible(result, set(cited.values), context, formula):
                    chk.issues.append(f"hesaplama sonucu {c.get('value')} girdilerden üretilemedi")
            chk.issues += _untraced_msgs(
                text, cited.extend(Pool().add_text(src.context_text)), [result] if result else None
            )
        elif kind == "inference":
            pool = cited if (chk.spans or chk.citations) else global_pool
            chk.issues += _untraced_msgs(text, pool)
        chk.issues += _advice(text, f"claims[{i}]", rep)
        if chk.issues:
            chk.status = "fail"
        rep.claims.append(chk)


def _verified_claim_numbers(output: dict[str, Any], rep: VerifyReport) -> list[list[Num]]:
    ok: list[list[Num]] = []
    claims = output.get("claims") or []
    for chk in rep.claims:
        if chk.status != "pass":
            continue
        c = claims[chk.index]
        if chk.kind == "computation":
            r = first_number(c.get("value"))
            if r:
                ok.append(r)
    return ok


def _check_payload(output: dict[str, Any], src: VerifySources, rep: VerifyReport) -> None:
    spans_by_code = output.get("payload_spans") or {}
    for code, payload in (output.get("payload") or {}).items():
        if payload is None:
            continue
        schema = src.payload_schemas.get(code)
        if schema is not None:
            errs = _schema_errors(payload, schema)
            if errs:
                rep.payload_errors[code] = "; ".join(errs[:3])
        rules = flatten(src.form_payloads.get(code) or {})
        field_spans = spans_by_code.get(code) or {}
        bad: dict[str, str] = {}
        for path, value in flatten(payload).items():
            if value is None or isinstance(value, bool) or rules.get(path) is not None:
                continue
            is_date = isinstance(value, str) and bool(_ISO_DATE.match(value))
            if not (isinstance(value, int | float) or is_date):
                continue
            ords = [o for o in field_spans.get(path) or [] if o in src.spans]
            if not ords:
                bad[path] = "alan için span yok"
                continue
            pool = Pool()
            for o in ords:
                pool.add_text(src.spans[o])
            if is_date:
                if value not in pool.dates:
                    bad[path] = f"{value} tarihi span metninde yok"
            else:
                d = to_decimal(value)
                if d is None or not pool.has_exact(d):
                    bad[path] = f"{value} değeri span metninde yok"
        if bad:
            rep.payload_fields[code] = bad


def _check_text(name: str, text: str, pool: Pool, also_ok: list[list[Num]], rep: VerifyReport) -> TextCheck:
    chk = TextCheck(field=name, original=text, kept=text)
    kept: list[str] = []
    for sentence in _sentences(text):
        issues = _untraced_msgs(sentence, pool, also_ok) + _advice(sentence, name, rep)
        if issues:
            chk.dropped.append({"sentence": sentence, "issues": issues})
        else:
            kept.append(sentence)
    chk.kept = " ".join(kept)
    return chk


def verify(
    output: dict[str, Any], sources: VerifySources, *, schema: dict[str, Any] | None = None
) -> VerifyReport:
    """Verify one analyst output against what the analyst was shown."""
    rep = VerifyReport()
    if schema is not None:
        rep.schema_errors = _schema_errors(output, schema)
        if rep.schema_errors:
            rep.ok = False
            return rep
    if sources.allowed_codes is not None:
        rep.unknown_codes = [c for c in output.get("event_codes") or [] if c not in sources.allowed_codes]

    global_pool = sources.pool()
    for payload in sources.form_payloads.values():
        for v in flatten(payload or {}).values():
            if isinstance(v, int | float) and not isinstance(v, bool):
                global_pool.add_value(v)
    _check_claims(output, sources, global_pool, rep)
    _check_payload(output, sources, rep)

    also_ok = _verified_claim_numbers(output, rep)
    for name in TEXT_FIELDS:
        value = output.get(name)
        if isinstance(value, str) and value.strip():
            rep.texts[name] = _check_text(name, value, global_pool, also_ok, rep)
    for item in output.get("watch_next") or []:
        issues = _untraced_msgs(item, global_pool, also_ok) + _advice(item, "watch_next", rep)
        rep.watch_next.append({"text": item, "kept": not issues, "issues": issues})

    rep.ok = not (
        rep.schema_errors
        or rep.unknown_codes
        or rep.failed_claims()
        or rep.payload_errors
        or rep.payload_fields
        or any(t.dropped for t in rep.texts.values())
        or any(not w["kept"] for w in rep.watch_next)
    )
    return rep


__all__ = ["ClaimCheck", "TextCheck", "VerifyReport", "VerifySources", "flatten", "verify"]
