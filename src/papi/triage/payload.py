"""Deterministic event payloads from parsed KAP forms (C1 ``document.structure``) — PRD 04 §3.

Field mapping lives in ``packs/tr/triage_payload.yaml`` (format documented there). Every payload is
completed to the full key set of its JSON Schema (unknown = ``None``) and validated with
``jsonschema``; a validation failure yields ``payload=None`` plus the reason.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from papi.core.config import get_settings
from papi.core.text import normalize_tr, parse_tr_number
from papi.triage.taxonomy import Taxonomy

PAYLOAD_SOURCE = "form_fields"
THRESHOLDS_PCT = (5, 10, 15, 20, 25, 33, 50, 67, 95)  # SPK II-15.1 md. 12 disclosure thresholds
_TYPES = {"number", "int", "year", "date", "text", "bool", "side", "currency"}
_EMPTY = {"", "-", "--", "yok", "none", "null", "n a", "na"}
_NUM_RE = re.compile(r"-?\d[\d.,\s]*")
_DATE_NUM = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_TXT = re.compile(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_MONTHS = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}  # fmt: skip
_CURRENCY = re.compile(r"(?<![A-Za-z])(TL|TRY|USD|EUR|GBP|CHF|JPY)(?![A-Za-z])|([₺$€£])")
_CURRENCY_MAP = {"TL": "TRY", "₺": "TRY", "$": "USD", "€": "EUR", "£": "GBP"}


class PayloadSpecError(ValueError):
    """``triage_payload.yaml`` is malformed."""


# ------------------------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class LabelSpec:
    patterns: tuple[tuple[str, ...], ...]
    excluded: tuple[str, ...]
    type: str
    row: str = "first"  # header tables: first | last | total (the "Toplam" row)

    def matches(self, label_norm: str) -> bool:
        return any(self.matches_pattern(label_norm, pat) for pat in self.patterns)

    def matches_pattern(self, label_norm: str, pat: tuple[str, ...]) -> bool:
        text = f" {label_norm} "
        if any(f" {x}" in text for x in self.excluded):
            return False
        return all(f" {p}" in text for p in pat)


@dataclass(frozen=True)
class ListSpec:
    header_any: tuple[tuple[str, ...], ...]
    columns: dict[str, LabelSpec]


@dataclass(frozen=True)
class Extractor:
    code: str
    templates: tuple[str, ...]
    fields: dict[str, LabelSpec]
    lists: dict[str, ListSpec]


def _patterns(raw: Any, where: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list) or not raw:
        raise PayloadSpecError(f"{where}: labels must be a non-empty list")
    out = []
    for item in raw:
        parts = tuple(p for p in (normalize_tr(x) for x in str(item).split("&")) if p)
        if not parts:
            raise PayloadSpecError(f"{where}: empty label pattern")
        out.append(parts)
    return tuple(out)


def _label_spec(raw: Any, where: str) -> LabelSpec:
    if not isinstance(raw, dict):
        raise PayloadSpecError(f"{where}: must be a mapping")
    typ = raw.get("type", "text")
    if typ not in _TYPES:
        raise PayloadSpecError(f"{where}: unknown type {typ!r}")
    row = raw.get("row", "first")
    if row not in ("first", "last", "total"):
        raise PayloadSpecError(f"{where}: row must be first, last or total")
    excluded = tuple(normalize_tr(x) for x in raw.get("not") or [])
    return LabelSpec(_patterns(raw.get("labels"), where), excluded, typ, row)


def parse_spec(raw: Any) -> dict[str, Extractor]:
    if not isinstance(raw, dict) or not isinstance(raw.get("extractors"), dict):
        raise PayloadSpecError("extractors mapping missing")
    out: dict[str, Extractor] = {}
    for code, spec in raw["extractors"].items():
        where = f"extractor {code}"
        if not isinstance(spec, dict):
            raise PayloadSpecError(f"{where}: must be a mapping")
        fields = {k: _label_spec(v, f"{where}.{k}") for k, v in (spec.get("fields") or {}).items()}
        lists: dict[str, ListSpec] = {}
        for key, lspec in (spec.get("lists") or {}).items():
            w = f"{where}.lists.{key}"
            if not isinstance(lspec, dict) or not isinstance(lspec.get("columns"), dict):
                raise PayloadSpecError(f"{w}: columns missing")
            lists[key] = ListSpec(
                header_any=_patterns(lspec.get("header_any"), w),
                columns={c: _label_spec(v, f"{w}.{c}") for c, v in lspec["columns"].items()},
            )
        out[code] = Extractor(
            code=code,
            templates=tuple(normalize_tr(t) for t in spec.get("templates") or []),
            fields=fields,
            lists=lists,
        )
    return out


def default_spec_path() -> Path:
    return get_settings().pack_dir / "triage_payload.yaml"


def load_spec(path: Path | None = None) -> dict[str, Extractor]:
    return _load_spec_cached(str((path or default_spec_path()).resolve()))


@functools.cache
def _load_spec_cached(path_str: str) -> dict[str, Extractor]:
    return parse_spec(yaml.safe_load(Path(path_str).read_text(encoding="utf-8")))


def clear_cache() -> None:
    _load_spec_cached.cache_clear()


# ----------------------------------------------------------------------------------------- values


def _is_empty(value: Any) -> bool:
    return value is None or normalize_tr(str(value)) in _EMPTY


def parse_number(value: Any) -> float | None:
    if _is_empty(value):
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    try:
        return float(parse_tr_number(text))
    except (ValueError, InvalidOperation, ArithmeticError):
        pass
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(parse_tr_number(m.group(0).strip()))
    except (ValueError, InvalidOperation, ArithmeticError):
        return None


def parse_date(value: Any) -> str | None:
    if _is_empty(value):
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    candidates: list[tuple[int, int, int]] = []
    if m := _DATE_ISO.search(text):
        candidates.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if m := _DATE_NUM.search(text):
        candidates.append((int(m.group(3)), int(m.group(2)), int(m.group(1))))
    if m := _DATE_TXT.search(normalize_tr(text)):
        month = _MONTHS.get(m.group(2))
        if month:
            candidates.append((int(m.group(3)), month, int(m.group(1))))
    for y, mo, d in candidates:
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def _parse_bool(value: Any) -> bool | None:
    """ "Evet" / "Hayır" (also bilingual "Hayır (No)")."""
    first = (normalize_tr(str(value)).split() or [""])[0] if value is not None else ""
    if first in {"evet", "var", "yes", "true", "e"}:
        return True
    if first in {"hayir", "yok", "no", "false", "h"}:
        return False
    return None


def _parse_side(value: Any) -> str | None:
    norm = normalize_tr(str(value)) if value is not None else ""
    if norm.startswith(("alis", "alim", "al ", "buy")) or norm in {"al", "a"}:
        return "buy"
    if norm.startswith(("satis", "satim", "sat ", "sell")) or norm in {"sat", "s"}:
        return "sell"
    return None


def convert(value: Any, typ: str) -> Any:
    if _is_empty(value):
        return None
    if typ == "number":
        return parse_number(value)
    if typ == "int":
        num = parse_number(value)
        return int(num) if num is not None and float(num).is_integer() else None
    if typ == "year":
        m = _YEAR.search(str(value))
        return int(m.group(0)) if m else None
    if typ == "date":
        return parse_date(value)
    if typ == "bool":
        return _parse_bool(value)
    if typ == "side":
        return _parse_side(value)
    if typ == "currency":
        code = currency_hint(value)
        if code:
            return code
        token = str(value).strip().upper()
        return token if re.fullmatch(r"[A-Z]{3}", token) else None
    text = " ".join(str(value).split())
    return text or None


def currency_hint(*texts: Any) -> str | None:
    for t in texts:
        if t is None:
            continue
        m = _CURRENCY.search(str(t))
        if m:
            token = m.group(1) or m.group(2)
            return _CURRENCY_MAP.get(token, token)
    return None


# ------------------------------------------------------------------------------------- extraction


@dataclass
class Extraction:
    values: dict[str, Any] = field(default_factory=dict)  # dotted path → value
    spans: dict[str, list[int]] = field(default_factory=dict)
    currency: str | None = None

    @property
    def matched(self) -> int:
        return sum(1 for v in self.values.values() if v not in (None, []))


def _span_of(table: dict[str, Any], r: int, c: int) -> list[int]:
    ords = table.get("span_ordinals") or []
    if r < len(ords):
        row = ords[r]
        if isinstance(row, list) and c < len(row) and isinstance(row[c], int):
            return [row[c]]
        if isinstance(row, int):
            return [row]
    return []


def _norm_cell(cell: Any) -> str:
    return normalize_tr(str(cell)) if cell is not None else ""


def _find_scalar(structure: dict[str, Any], spec: LabelSpec) -> tuple[Any, list[int], str | None] | None:
    """(value, span ordinals, currency hint) of the first non-empty field matching the earliest pattern."""
    fields = structure.get("form_fields") or []
    tables = structure.get("tables") or []
    for pat in spec.patterns:
        for f in fields:
            label = f.get("label_norm") or normalize_tr(str(f.get("label") or ""))
            if not spec.matches_pattern(label, pat):
                continue
            value = convert(f.get("value"), spec.type)
            if value is None:
                continue
            span = [f["span_ordinal"]] if isinstance(f.get("span_ordinal"), int) else []
            return value, span, currency_hint(f.get("label"), f.get("value"))
        for t in tables:  # key/value layout
            for r, row in enumerate(t.get("rows") or []):
                if len(row) < 2 or not spec.matches_pattern(_norm_cell(row[0]), pat):
                    continue
                value = convert(row[1], spec.type)
                if value is not None:
                    return value, _span_of(t, r, 1), currency_hint(row[0], row[1])
        for t in tables:  # header layout
            header = t.get("header") or []
            rows = list(enumerate(t.get("rows") or []))
            if spec.row == "total":
                rows = [(r, row) for r, row in rows if _is_total_row(row)]
            else:
                rows = [(r, row) for r, row in rows if not _is_total_row(row)]
                if spec.row == "last":
                    rows.reverse()
            for c, h in enumerate(header):
                if not spec.matches_pattern(_norm_cell(h), pat):
                    continue
                for r, row in rows:
                    if c < len(row):
                        value = convert(row[c], spec.type)
                        if value is not None:
                            return value, _span_of(t, r, c), currency_hint(h, row[c])
    return None


def _is_total_row(row: list[Any]) -> bool:
    return bool(row) and _norm_cell(row[0]).startswith(("toplam", "genel toplam", "total"))


def _find_list(structure: dict[str, Any], spec: ListSpec, path: str, ex: Extraction) -> list[dict[str, Any]]:
    for t in structure.get("tables") or []:
        header = [_norm_cell(h) for h in t.get("header") or []]
        if not any(all(f" {p}" in f" {h} " for p in pat) for pat in spec.header_any for h in header):
            continue
        col_of: dict[str, int] = {}
        for key, cspec in spec.columns.items():
            for c, h in enumerate(header):
                if c not in col_of.values() and cspec.matches(h):
                    col_of[key] = c
                    break
        if not col_of:
            continue
        items: list[dict[str, Any]] = []
        for r, row in enumerate(t.get("rows") or []):
            if _is_total_row(row):
                continue
            item = {key: None for key in spec.columns}
            for key, c in col_of.items():
                if c < len(row):
                    item[key] = convert(row[c], spec.columns[key].type)
                    if item[key] is not None:
                        span = _span_of(t, r, c)
                        if span:
                            ex.spans[f"{path}.{len(items)}.{key}"] = span
                        if spec.columns[key].type == "number" and ex.currency is None:
                            ex.currency = currency_hint(t.get("header")[c], row[c])
            if any(v is not None for v in item.values()):
                items.append(item)
        if items:
            return items
    return []


def extract_fields(structure: dict[str, Any] | None, extractor: Extractor) -> Extraction:
    ex = Extraction()
    if not structure:
        return ex
    if extractor.templates:
        template = normalize_tr(str(structure.get("template_id") or ""))
        if not any(t in template for t in extractor.templates):
            return ex
    for path, spec in extractor.fields.items():
        found = _find_scalar(structure, spec)
        if found is None:
            continue
        value, span, cur = found
        ex.values[path] = value
        if span:
            ex.spans[path] = span
        if spec.type == "number" and cur and ex.currency is None:
            ex.currency = cur
    for path, lspec in extractor.lists.items():
        items = _find_list(structure, lspec, path, ex)
        if items:
            ex.values[path] = items
    return ex


# ---------------------------------------------------------------------------------- post-processing


def _sum(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present), 6) if present else None


def _post_dividend_cash(p: dict[str, Any], ctx: dict[str, Any]) -> None:
    groups = p.get("share_groups") or []
    if groups:
        first = groups[0]
        for key in ("gross_per_share", "net_per_share", "gross_rate_pct"):
            if p.get(key) is None and first.get(key) is not None:
                p[key] = first[key]
    summary = f" {normalize_tr(ctx.get('summary') or '')} "
    if p.get("decision_body") is None:
        if " genel kurul" in summary:
            p["decision_body"] = "general_meeting"
        elif any(k in summary for k in (" yonetim kurulu", " oneri", " teklif")):
            p["decision_body"] = "board"
    if p.get("period_year") is None:
        for t in (ctx.get("structure") or {}).get("tables") or []:
            caption = normalize_tr(str(t.get("caption") or ""))
            if "dagitim tablosu" in caption:
                years = [int(m.group(0)) for m in _YEAR.finditer(caption)]
                if years:
                    p["period_year"] = years[-1]
                    break


def _post_capital(amount_key: str, rate_key: str) -> Any:
    def hook(p: dict[str, Any], ctx: dict[str, Any]) -> str | None:
        cur, tgt, amt = p.get("current_capital"), p.get("target_capital"), p.get(amount_key)
        if amt is None and cur is not None and tgt is not None and tgt >= cur:
            p[amount_key] = round(tgt - cur, 6)
        amt = p.get(amount_key)
        if tgt is None and cur is not None and amt is not None:
            p["target_capital"] = round(cur + amt, 6)
        if p.get(rate_key) is None and cur and amt is not None:
            p[rate_key] = round(amt / cur * 100, 6)
        # Without an amount or ratio the payload would describe a zero-factor corporate action.
        if not p.get(amount_key) and not p.get(rate_key):
            return f"no_increase_terms: {amount_key} / {rate_key} not stated"
        return None

    return hook


def _split_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise trade rows: KAP share-transaction tables give separate buy / sell nominal columns
    (``_buy_nominal`` / ``_sell_nominal``); emit one row per side. Other ``_*`` helper keys are kept
    for the caller and stripped from the final payload by ``build``."""
    out: list[dict[str, Any]] = []
    for t in trades:
        buy, sell = t.pop("_buy_nominal", None), t.pop("_sell_nominal", None)
        if t.get("shares_nominal") is None and (buy or sell):
            for side, qty in (("buy", buy), ("sell", sell)):
                if qty:
                    out.append({**t, "side": side, "shares_nominal": abs(qty)})
            continue
        if t.get("shares_nominal") is not None or t.get("amount") is not None:
            out.append(t)
    for t in out:
        if t.get("amount") is None and t.get("shares_nominal") is not None and t.get("price") is not None:
            t["amount"] = round(t["shares_nominal"] * t["price"], 6)
    return out


def _post_buyback_execution(p: dict[str, Any], ctx: dict[str, Any]) -> None:
    trades = _split_trades(p.get("trades") or [])
    pcts = [t.pop("_pct", None) for t in trades]
    p["trades"] = trades or None
    if not trades:
        return
    dates = sorted({t["date"] for t in trades if t.get("date")})
    latest = dates[-1] if dates else None
    day = [t for t in trades if latest is None or t.get("date") == latest]
    if p.get("trade_date") is None:
        p["trade_date"] = latest
    if p.get("shares_nominal") is None:
        p["shares_nominal"] = _sum([t.get("shares_nominal") for t in day])
    if p.get("total_amount") is None:
        p["total_amount"] = _sum([t.get("amount") for t in day])
    if p.get("avg_price") is None and p.get("shares_nominal") and p.get("total_amount") is not None:
        p["avg_price"] = round(p["total_amount"] / p["shares_nominal"], 6)
    prices = [t["price"] for t in day if t.get("price") is not None]
    if prices:
        p["min_price"] = p["min_price"] if p.get("min_price") is not None else min(prices)
        p["max_price"] = p["max_price"] if p.get("max_price") is not None else max(prices)
    if len(dates) > 1:  # the table lists the programme's executions so far
        if p.get("cumulative_shares_nominal") is None:
            p["cumulative_shares_nominal"] = _sum([t.get("shares_nominal") for t in trades])
        if p.get("cumulative_pct_of_capital") is None:
            p["cumulative_pct_of_capital"] = _sum(pcts)


def _post_insider_trade(p: dict[str, Any], ctx: dict[str, Any]) -> None:
    trades = _split_trades(p.get("trades") or [])
    p["trades"] = trades or None
    if not trades:
        return
    if p.get("total_shares_nominal") is None:
        p["total_shares_nominal"] = _sum([t.get("shares_nominal") for t in trades])
    if p.get("total_amount") is None:
        p["total_amount"] = _sum([t.get("amount") for t in trades])
    shares, amount = p.get("total_shares_nominal"), p.get("total_amount")
    if p.get("avg_price") is None and shares and amount is not None:
        p["avg_price"] = round(amount / shares, 6)
    if p.get("side") is None:
        sides = {t.get("side") for t in trades if t.get("side")}
        p["side"] = sides.pop() if len(sides) == 1 else ("mixed" if sides else None)


_POST = {
    "distribution.dividend.cash": _post_dividend_cash,
    "capital.increase.bonus": _post_capital("bonus_amount", "bonus_rate_pct"),
    "capital.increase.rights": _post_capital("rights_amount", "rights_rate_pct"),
    "buyback.execution": _post_buyback_execution,
    "ownership.insider_trade": _post_insider_trade,
}

_RULE_TYPE_MONTHS = {"3 aylik": 3, "6 aylik": 6, "9 aylik": 9, "yillik": 12, "12 aylik": 12}


def periodic_report_values(meta: dict[str, Any]) -> dict[str, Any]:
    """``report.financial.periodic`` facts from the KAP list item (year, ruleType/period, subject)."""
    vals: dict[str, Any] = {}
    year = meta.get("year")
    if isinstance(year, int) or (isinstance(year, str) and year.isdigit()):
        vals["period_year"] = int(year)
    months = _RULE_TYPE_MONTHS.get(normalize_tr(str(meta.get("ruleType") or "")))
    if months is None and str(meta.get("period") or "").isdigit():
        months = {1: 3, 2: 6, 3: 9, 4: 12}.get(int(meta["period"]))
    if months is not None:
        vals["period_months"] = months
    subject = normalize_tr(str(meta.get("subject") or ""))
    if "konsolide olmayan" in subject:
        vals["consolidated"] = False
    elif "konsolide" in subject:
        vals["consolidated"] = True
    return vals


# ----------------------------------------------------------------------------------------- schema


def skeleton(schema: dict[str, Any]) -> dict[str, Any]:
    """All schema properties set to ``None`` (nested object properties expanded)."""
    out: dict[str, Any] = {}
    for key, prop in (schema.get("properties") or {}).items():
        types = prop.get("type")
        types = types if isinstance(types, list) else [types]
        if "object" in types and prop.get("properties"):
            out[key] = skeleton(prop)
        else:
            out[key] = None
    return out


def _set_path(obj: dict[str, Any], path: str, value: Any) -> bool:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            return False
        cur = nxt
    if parts[-1] not in cur:
        return False
    cur[parts[-1]] = value
    return True


def _validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def validate(schema: dict[str, Any], payload: dict[str, Any]) -> str | None:
    """``None`` when valid, else a short reason."""
    errors = sorted(_validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.path) or "<root>"
    return f"schema: {where}: {first.message}"[:500]


# ------------------------------------------------------------------------------------------- build


@dataclass
class PayloadResult:
    payload: dict[str, Any] | None
    source: str | None
    schema: str | None
    spans: dict[str, list[int]] = field(default_factory=dict)
    error: str | None = None
    from_: str | None = None  # form | kap_list | derived


def build(
    code: str,
    taxonomy: Taxonomy,
    *,
    structure: dict[str, Any] | None,
    meta: dict[str, Any] | None = None,
    summary: str | None = None,
    spec: dict[str, Extractor] | None = None,
) -> PayloadResult:
    """Deterministic payload for ``code`` (or ``payload=None`` with the reason in ``error``)."""
    ec = taxonomy.get(code)
    schema = taxonomy.schema_for(code)
    if ec is None or schema is None:
        return PayloadResult(None, None, None)
    extractors = spec if spec is not None else load_spec()
    origin: str
    if code == "report.financial.periodic":
        values = periodic_report_values(meta or {})
        ex = Extraction(values=values)
        origin = "kap_list"
    elif code in extractors:
        if not structure:
            return PayloadResult(None, None, ec.schema, error="no_structure")
        ex = extract_fields(structure, extractors[code])
        origin = "form"
    else:
        return PayloadResult(None, None, ec.schema)
    if ex.matched == 0:
        return PayloadResult(None, None, ec.schema, error="no_fields_matched")
    payload = skeleton(schema)
    for path, value in ex.values.items():
        _set_path(payload, path, value)
    if "currency" in payload and payload.get("currency") is None and ex.currency:
        payload["currency"] = ex.currency
    hook = _POST.get(code)
    if hook and (
        hook_error := hook(payload, {"summary": summary, "meta": meta or {}, "structure": structure or {}})
    ):
        return PayloadResult(None, None, ec.schema, error=hook_error, from_=origin)
    _strip_helpers(payload)
    reason = validate(schema, payload)
    if reason:
        return PayloadResult(None, None, ec.schema, error=reason, from_=origin)
    spans: dict[str, list[int]] = {}
    for path, ords in ex.spans.items():
        path = path.replace("._buy_nominal", ".shares_nominal").replace("._sell_nominal", ".shares_nominal")
        if not any(part.startswith("_") for part in path.split(".")):
            spans.setdefault(path, ords)
    return PayloadResult(payload, PAYLOAD_SOURCE, ec.schema, spans=spans, from_=origin)


def _strip_helpers(obj: Any) -> None:
    """Drop extraction helper keys (``_*``) from nested dicts / lists in place."""
    if isinstance(obj, dict):
        for k in [k for k in obj if isinstance(k, str) and k.startswith("_")]:
            del obj[k]
        for v in obj.values():
            _strip_helpers(v)
    elif isinstance(obj, list):
        for v in obj:
            _strip_helpers(v)


def threshold_crossing(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """``ownership.threshold_crossing`` values implied by an insider-trade payload's before/after %.

    Capital share is used, else voting rights. When several thresholds are crossed at once the
    farthest one is reported (highest going up, lowest going down)."""
    if not payload:
        return None
    for before_key, after_key in (
        ("holding_before_pct", "holding_after_pct"),
        ("voting_before_pct", "voting_after_pct"),
    ):
        before, after = payload.get(before_key), payload.get(after_key)
        if before is None or after is None or before == after:
            continue
        crossed = [t for t in THRESHOLDS_PCT if before < t <= after or after < t <= before]
        if not crossed:
            continue
        up = after > before
        person = payload.get("person") or {}
        return {
            "holder_name": person.get("name"),
            "threshold_pct": float(max(crossed) if up else min(crossed)),
            "direction": "up" if up else "down",
            "holding_before_pct": payload.get("holding_before_pct"),
            "holding_after_pct": payload.get("holding_after_pct"),
            "voting_before_pct": payload.get("voting_before_pct"),
            "voting_after_pct": payload.get("voting_after_pct"),
        }
    return None


def build_derived(code: str, values: dict[str, Any], taxonomy: Taxonomy) -> PayloadResult:
    """Validate a payload derived from another event's payload (e.g. a threshold crossing)."""
    ec = taxonomy.get(code)
    schema = taxonomy.schema_for(code)
    if ec is None or schema is None:
        return PayloadResult(None, None, None)
    payload = skeleton(schema)
    for k, v in values.items():
        _set_path(payload, k, v)
    reason = validate(schema, payload)
    if reason:
        return PayloadResult(None, None, ec.schema, error=reason, from_="derived")
    return PayloadResult(payload, PAYLOAD_SOURCE, ec.schema, from_="derived")
