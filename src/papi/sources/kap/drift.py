"""Schema-drift guard for the unofficial KAP JSON endpoints + HTML template tracking (ING-N7).

Key sets live in ``packs/tr/kap/schema_keys.yaml``; known body templates in
``packs/tr/kap/templates.yaml``. Missing required keys → ``kap.drift`` metric + ops alert + kv_state
``kap:drift``; unknown keys are only recorded. Unknown templates → ``kap.template_unknown`` metric and
kv_state ``kap:unknown_templates`` ({template_id: {count, first_seen, last_seen, example_ref}}).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection

from papi.core import metrics
from papi.core.config import get_settings
from papi.core.state import get_state, set_state
from papi.core.timeutil import now_utc
from papi.sources.kap import deps

DRIFT_KEY = "kap:drift"
UNKNOWN_TEMPLATES_KEY = "kap:unknown_templates"


def _pack_file(name: str) -> Path:
    return get_settings().pack_dir / "kap" / name


@lru_cache(maxsize=4)
def _load_yaml(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def schema_keys() -> dict[str, Any]:
    return _load_yaml(str(_pack_file("schema_keys.yaml")))


@dataclass(frozen=True)
class DriftReport:
    kind: str
    missing: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing


def inspect_keys(kind: str, objs: Iterable[dict[str, Any]]) -> DriftReport:
    """Compare observed keys with the recorded set (``missing`` = required keys absent in any object)."""
    spec = schema_keys().get(kind) or {}
    required = set(spec.get("required") or ())
    known = set(spec.get("known") or ()) | required
    missing: set[str] = set()
    unknown: set[str] = set()
    for obj in objs:
        if not isinstance(obj, dict):
            missing.add("<not-an-object>")
            continue
        keys = set(obj)
        missing |= required - keys
        if known:
            unknown |= keys - known
    return DriftReport(kind, tuple(sorted(missing)), tuple(sorted(unknown)))


def check(
    conn: Connection, kind: str, objs: Iterable[dict[str, Any]], *, ref: str | None = None
) -> DriftReport:
    """Validate and record drift in ``conn``'s transaction (caller commits)."""
    report = inspect_keys(kind, objs)
    if report.ok and not report.unknown:
        return report
    state = get_state(conn, DRIFT_KEY, {}) or {}
    entry = state.get(kind) or {}
    now = now_utc().isoformat()
    if report.unknown:
        seen_unknown = set(entry.get("unknown") or ())
        entry["unknown"] = sorted(seen_unknown | set(report.unknown))
    if report.missing:
        entry["missing"] = list(report.missing)
        entry["missing_at"] = now
        entry["ref"] = ref
        metrics.record("kap.drift", 1, conn=conn, kind=kind, missing=",".join(report.missing)[:200])
        deps.ops_alert(
            conn,
            f"KAP şema değişikliği: {kind} yanıtında zorunlu alanlar eksik: {', '.join(report.missing)}"
            + (f" (bildirim {ref})" if ref else ""),
            level="error",
            dedupe_key=f"kap:drift:{kind}:{','.join(report.missing)}",
        )
    entry["at"] = now
    state[kind] = entry
    set_state(conn, DRIFT_KEY, state)
    return report


# ----------------------------------------------------------------------------------------- templates


@dataclass(frozen=True)
class TemplateRegistry:
    templates: dict[str, dict[str, Any]]
    patterns: tuple[tuple[re.Pattern[str], dict[str, Any]], ...]

    @classmethod
    def load(cls) -> TemplateRegistry:
        data = _load_yaml(str(_pack_file("templates.yaml")))
        pats = tuple((re.compile(p["pattern"]), p) for p in data.get("patterns") or ())
        return cls(templates=dict(data.get("templates") or {}), patterns=pats)

    def lookup(self, template_id: str | None) -> dict[str, Any] | None:
        if not template_id:
            return None
        if template_id in self.templates:
            return self.templates[template_id]
        for pat, meta in self.patterns:
            if pat.match(template_id):
                return meta
        return None

    def is_known(self, template_id: str | None) -> bool:
        return self.lookup(template_id) is not None


def record_unknown_template(conn: Connection, template_id: str | None, ref: str) -> None:
    key = template_id or "<none>"
    state = get_state(conn, UNKNOWN_TEMPLATES_KEY, {}) or {}
    now = now_utc().isoformat()
    entry = state.get(key) or {"count": 0, "first_seen": now, "example_ref": ref}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_seen"] = now
    entry["last_ref"] = ref
    state[key] = entry
    set_state(conn, UNKNOWN_TEMPLATES_KEY, state)
    metrics.record("kap.template_unknown", 1, conn=conn, template_id=key)
